// WjjhXHR.mm —— cc.XMLHttpRequest 的 NSURLSession 原生实现（契约见 WjjhXHR.h）。
//
// 结构：
//   registry[kObjKey]  = { [lightuserdata(ud)] = 字段表 }   每个 XHR 一份
//   registry[kMKey]    = 方法表（open/setRequestHeader/...）
//   registry[kMTKey]   = userdata 元表（__index/__newindex 转发）
//   registry[kKeepKey] = { [ud] = ud }                      请求在途时钉住地址
// 字段（Request.lua 直接读写）：method/url/headers/timeout/status/response/
//                               readyState/respHeaders/handlerRef
// 完成回调：NSURLSession 线程 → performFunctionInCocosThread → GL 线程写回字段
//           并调用 handler()（无参；Request.lua 在里面读 status/response）。
#import <Foundation/Foundation.h>
#import "cocos2d.h"
#import "scripting/lua-bindings/manual/CCLuaEngine.h"
#include <string>
extern "C" {
#include "lua.h"
#include "lauxlib.h"
}
#include "WjjhXHR.h"

namespace {

const char kObjKey = 'o';
const char kMKey = 'm';
const char kMTKey = 't';
const char kKeepKey = 'k';

void regGet(lua_State* L, const char* key)
{
    lua_pushlightuserdata(L, (void*)key);
    lua_rawget(L, LUA_REGISTRYINDEX);
}

// 字段表取用（存在则留在栈顶并返回 true）
bool objGet(lua_State* L, void* ud)
{
    regGet(L, &kObjKey);            // [reg]
    lua_pushlightuserdata(L, ud);   // [reg, ud]
    lua_rawget(L, -2);              // [reg, obj?]
    lua_remove(L, -2);              // [obj?]
    if (lua_istable(L, -1)) return true;
    lua_pop(L, 1);
    return false;
}

// 新建字段表（留在栈顶）
void objNew(lua_State* L, void* ud)
{
    regGet(L, &kObjKey);            // [reg]
    lua_newtable(L);                // [reg, obj]
    lua_pushlightuserdata(L, ud);   // [reg, obj, ud]
    lua_pushvalue(L, -2);           // [reg, obj, ud, obj]
    lua_rawset(L, -4);              // [reg, obj]   reg[ud] = obj
    lua_remove(L, -2);              // [obj]
}

int xhr_newindex(lua_State* L)
{
    void* ud = lua_touserdata(L, 1);
    if (ud && objGet(L, ud)) {      // [obj]
        lua_pushvalue(L, 2);        // [obj, key]
        lua_pushvalue(L, 3);        // [obj, key, val]
        lua_rawset(L, -3);          // obj[key] = val
        lua_pop(L, 1);
    }
    return 0;
}

int xhr_index(lua_State* L)
{
    void* ud = lua_touserdata(L, 1);
    if (ud && objGet(L, ud)) {      // [obj]
        lua_pushvalue(L, 2);
        lua_rawget(L, -2);
        if (!lua_isnil(L, -1)) { lua_remove(L, -2); return 1; }
        lua_pop(L, 2);              // 字段没有 → 查方法表
    } else {
        lua_pop(L, 1);
    }
    regGet(L, &kMKey);              // [M]
    lua_pushvalue(L, 2);
    lua_rawget(L, -2);
    lua_remove(L, -2);
    return 1;                       // 找不到就是 nil
}

int xhr_open(lua_State* L)
{
    void* ud = lua_touserdata(L, 1);
    if (!ud || !objGet(L, ud)) return 0;
    lua_pushvalue(L, 2); lua_setfield(L, -2, "method");
    lua_pushvalue(L, 3); lua_setfield(L, -2, "url");
    lua_pop(L, 1);
    return 0;
}

int xhr_setRequestHeader(lua_State* L)
{
    void* ud = lua_touserdata(L, 1);
    if (!ud || !objGet(L, ud)) return 0;   // [obj]
    lua_getfield(L, -1, "headers");        // [obj, headers]
    if (!lua_istable(L, -1)) {
        lua_pop(L, 1);
        lua_newtable(L);
        lua_pushvalue(L, -1);
        lua_setfield(L, -3, "headers");
    }
    size_t kl = 0, vl = 0;
    const char* k = lua_tolstring(L, 2, &kl);
    const char* v = lua_tolstring(L, 3, &vl);
    if (k && v) {
        lua_pushlstring(L, v, vl);
        lua_setfield(L, -2, k);
    }
    lua_pop(L, 2);
    return 0;
}

int xhr_registerScriptHandler(lua_State* L)
{
    void* ud = lua_touserdata(L, 1);
    if (!ud || !objGet(L, ud)) return 0;
    if (lua_isfunction(L, 2)) {
        lua_pushvalue(L, 2);
        int ref = (int)luaL_ref(L, LUA_REGISTRYINDEX);
        lua_pushinteger(L, ref);
        lua_setfield(L, -2, "handlerRef");
    }
    lua_pop(L, 1);
    return 0;
}

int xhr_getAllResponseHeaders(lua_State* L)
{
    void* ud = lua_touserdata(L, 1);
    if (ud && objGet(L, ud)) {
        lua_getfield(L, -1, "respHeaders");
        if (lua_isstring(L, -1)) { lua_remove(L, -2); return 1; }
        lua_pop(L, 2);
    }
    lua_pushstring(L, "");
    return 1;
}

int xhr_abort(lua_State* L)
{
    return 0;   // 会话内请求都是短平快；不做真正取消
}

int xhr_send(lua_State* L)
{
    void* ud = lua_touserdata(L, 1);
    if (!ud || !objGet(L, ud)) return 0;

    lua_getfield(L, -1, "method");
    const char* method = lua_isstring(L, -1) ? lua_tostring(L, -1) : "GET";
    bool isPost = method && strcasecmp(method, "POST") == 0;
    lua_pop(L, 1);

    lua_getfield(L, -1, "url");
    const char* url = lua_isstring(L, -1) ? lua_tostring(L, -1) : "";
    lua_pop(L, 1);
    if (!url || !*url) { lua_pop(L, 1); return 0; }

    lua_getfield(L, -1, "timeout");
    double tmo = lua_isnumber(L, -1) ? lua_tonumber(L, -1) : 15.0;
    if (tmo <= 0) tmo = 15.0;
    lua_pop(L, 1);

    size_t bodyLen = 0;
    const char* body = nullptr;
    if (isPost && lua_isstring(L, 2)) body = lua_tolstring(L, 2, &bodyLen);

    NSMutableURLRequest* req = [[NSMutableURLRequest alloc]
        initWithURL:[NSURL URLWithString:[NSString stringWithUTF8String:url]]];
    req.HTTPMethod = isPost ? @"POST" : @"GET";
    req.timeoutInterval = tmo;

    lua_getfield(L, -1, "headers");   // [obj, headers]
    if (lua_istable(L, -1)) {
        lua_pushnil(L);
        while (lua_next(L, -2) != 0) {
            size_t hl = 0;
            const char* hk = lua_tolstring(L, -2, &hl);
            const char* hv = lua_isstring(L, -1) ? lua_tostring(L, -1) : nullptr;
            if (hk && hv) {
                [req setValue:[NSString stringWithUTF8String:hv]
                  forHTTPHeaderField:[NSString stringWithUTF8String:hk]];
            }
            lua_pop(L, 1);
        }
    }
    lua_pop(L, 1);

    if (isPost && bodyLen > 0) {
        req.HTTPBody = [NSData dataWithBytes:body length:bodyLen];
    }
    lua_pop(L, 1);   // obj

    // GL 线程回调要用的拷贝（__block 变量不能被 C++ lambda 捕获，
    // 完成块里先落成普通局部量再按值捕获）
    void* key = ud;

    NSURLSession* session = [NSURLSession
        sessionWithConfiguration:[NSURLSessionConfiguration defaultSessionConfiguration]];
    [[session dataTaskWithRequest:req
                completionHandler:^(NSData* data, NSURLResponse* resp, NSError* err) {
        NSHTTPURLResponse* hr = (NSHTTPURLResponse*)resp;
        long cCode = [hr isKindOfClass:[NSHTTPURLResponse class]] ? hr.statusCode : -1;
        if (cCode == 0 && err) cCode = -1;
        std::string cBody;
        if (data.length) cBody.assign((const char*)data.bytes, data.length);
        std::string cHead;
        if ([hr isKindOfClass:[NSHTTPURLResponse class]]) {
            NSMutableString* hs = [NSMutableString string];
            for (NSString* hk in hr.allHeaderFields) {
                [hs appendFormat:@"%@: %@\r\n", hk, hr.allHeaderFields[hk]];
            }
            cHead = hs.UTF8String ? std::string(hs.UTF8String) : std::string();
        }

        cocos2d::Director::getInstance()->getScheduler()->performFunctionInCocosThread(
            [key, cCode, cBody, cHead] {
                lua_State* LL = cocos2d::LuaEngine::getInstance()->getLuaStack()->getLuaState();
                if (!objGet(LL, key)) return;
                lua_pushinteger(LL, (lua_Integer)cCode);
                lua_setfield(LL, -2, "status");
                lua_pushlstring(LL, cBody.data(), cBody.size());
                lua_setfield(LL, -2, "response");
                lua_pushinteger(LL, 4);          // DONE
                lua_setfield(LL, -2, "readyState");
                lua_pushlstring(LL, cHead.data(), cHead.size());
                lua_setfield(LL, -2, "respHeaders");

                lua_getfield(LL, -1, "handlerRef");
                if (lua_isnumber(LL, -1)) {
                    int ref = (int)lua_tointeger(LL, -1);
                    lua_pop(LL, 1);
                    lua_rawgeti(LL, LUA_REGISTRYINDEX, ref);
                    if (lua_isfunction(LL, -1)) {
                        if (lua_pcall(LL, 0, 0, 0) != 0) {
                            fprintf(stderr, "[WjjhXHR] handler error: %s\n",
                                    lua_tostring(LL, -1));
                            lua_pop(LL, 1);
                        }
                    } else {
                        lua_pop(LL, 1);
                    }
                } else {
                    lua_pop(LL, 1);
                }
                lua_pop(LL, 1);                  // obj
            });
    }] resume];

    return 0;
}

int xhr_new(lua_State* L)
{
    void* ud = lua_newuserdata(L, 8);
    *(void**)ud = nullptr;

    objNew(L, ud);                       // [ud, obj]
    lua_newtable(L); lua_setfield(L, -2, "headers");
    lua_pushinteger(L, 0);  lua_setfield(L, -2, "status");
    lua_pushstring(L, "");  lua_setfield(L, -2, "response");
    lua_pushinteger(L, 0);  lua_setfield(L, -2, "readyState");
    lua_pushinteger(L, 10); lua_setfield(L, -2, "timeout");
    lua_pop(L, 1);                       // [ud]

    regGet(L, &kMTKey);                  // [ud, MT]
    lua_setmetatable(L, -2);             // [ud]

    regGet(L, &kKeepKey);                // [ud, keep]
    lua_pushlightuserdata(L, ud);
    lua_pushlightuserdata(L, ud);
    lua_rawset(L, -3);                   // keep[ud] = ud（钉住地址防复用）
    lua_pop(L, 1);
    return 1;
}

}  // namespace

extern "C" void wjjh_xhr_push_class(lua_State* L)
{
    // registry 四件套
    lua_pushlightuserdata(L, (void*)&kObjKey);
    lua_newtable(L);
    lua_rawset(L, LUA_REGISTRYINDEX);

    lua_pushlightuserdata(L, (void*)&kKeepKey);
    lua_newtable(L);
    lua_rawset(L, LUA_REGISTRYINDEX);

    lua_pushlightuserdata(L, (void*)&kMKey);     // 方法表
    lua_newtable(L);
    lua_pushcfunction(L, xhr_open);
    lua_setfield(L, -2, "open");
    lua_pushcfunction(L, xhr_setRequestHeader);
    lua_setfield(L, -2, "setRequestHeader");
    lua_pushcfunction(L, xhr_registerScriptHandler);
    lua_setfield(L, -2, "registerScriptHandler");
    lua_pushcfunction(L, xhr_getAllResponseHeaders);
    lua_setfield(L, -2, "getAllResponseHeaders");
    lua_pushcfunction(L, xhr_abort);
    lua_setfield(L, -2, "abort");
    lua_pushcfunction(L, xhr_send);
    lua_setfield(L, -2, "send");
    lua_rawset(L, LUA_REGISTRYINDEX);

    lua_pushlightuserdata(L, (void*)&kMTKey);    // userdata 元表
    lua_newtable(L);
    lua_pushcfunction(L, xhr_index);
    lua_setfield(L, -2, "__index");
    lua_pushcfunction(L, xhr_newindex);
    lua_setfield(L, -2, "__newindex");
    lua_rawset(L, LUA_REGISTRYINDEX);

    // class 表：只有 new
    lua_newtable(L);
    lua_pushcfunction(L, xhr_new);
    lua_setfield(L, -2, "new");
}
