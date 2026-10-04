#ifndef WJJH_XHR_H
#define WJJH_XHR_H

#include "lua.h"

// 用 NSURLSession 原生实现 cc.XMLHttpRequest（替代 cocos 的 LuaMinXmlHttpRequest
// —— 其 tolua 绑定在 Release 下 self 为 NULL 时直接解引用崩溃，b71 .ips 实证）。
// 游戏侧契约（third/http/Request.lua）：
//   cc.XMLHttpRequest:new()
//   xhr.responseType = <const>      / xhr.timeout = <sec>
//   xhr:open(method, url, async)
//   xhr:registerScriptHandler(fn)   -- 无参调用；回调里读 xhr.status/.response
//   xhr:setRequestHeader(k, v)
//   xhr:send(postData)
//   xhr:getAllResponseHeaders()
// 完成后由 GL 线程回调（performFunctionInCocosThread）。
#ifdef __cplusplus
extern "C" {
#endif

// 压入 class table（含 new + 元方法），赋给 cc.XMLHttpRequest 即可
void wjjh_xhr_push_class(lua_State* L);

#ifdef __cplusplus
}
#endif

#endif /* WJJH_XHR_H */
