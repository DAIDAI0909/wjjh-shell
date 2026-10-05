#include "AppDelegate.h"
#include "scripting/lua-bindings/manual/CCLuaEngine.h"
#include "scripting/lua-bindings/manual/tolua_fix.h"
#include "scripting/lua-bindings/manual/lua_module_register.h"
#include "cocos2d.h"
#include "WjjhJM.h"
#include "WjjhXHR.h"
#include <os/log.h>

USING_NS_CC;
using namespace std;

// 引导期诊断双通道：stderr（console-pty 模式可见）+ os_log（log show 可见，
// 分离启动模式下 stderr 无人接收，统一日志是唯一出口；谓词按进程路径过滤能全收到）
static void wjjh_bootlog(const char* s)
{
    fprintf(stderr, "%s\n", s);
    os_log(OS_LOG_DEFAULT, "WJJH %{public}s", s);
}

static int wjjh_lua_log(lua_State* LS)
{
    const char* s = lua_tostring(LS, 1);
    if (s) wjjh_bootlog(s);
    return 0;
}

AppDelegate::AppDelegate()
{
}

AppDelegate::~AppDelegate()
{
}

void AppDelegate::initGLContextAttrs()
{
    GLContextAttrs glContextAttrs = {8, 8, 8, 8, 24, 8};
    GLView::setGLContextAttrs(glContextAttrs);
}

void AppDelegate::setAnimationInterval(double interval)
{
    Director::getInstance()->setAnimationInterval(interval);
}

bool AppDelegate::applicationDidFinishLaunching()
{
    wjjh_bootlog("WJJH_BOOT: applicationDidFinishLaunching");
    auto director = Director::getInstance();
    auto glview = director->getOpenGLView();
    if (!glview) {
        glview = GLViewImpl::create("WJJH");
        director->setOpenGLView(glview);
    }

    // 游戏资源在 bundle 的 Resources/ 下：src/ 与 res/ 由 main.lua 自己 addSearchPath
    auto engine = LuaEngine::getInstance();
    ScriptEngineManager::getInstance()->setScriptEngine(engine);

    // 官方模板同款：denshion/network/ui/studio/spine/audioengine 等模块在此注册，
    // 漏了会报 "SimpleAudioEngine is nil"
    lua_State* L = engine->getLuaStack()->getLuaState();
    lua_module_register(L);

    // Lua 侧诊断出口：print 重定向到 __wjjhlog（stderr + os_log）
    lua_register(L, "__wjjhlog", wjjh_lua_log);

    // 真 JM 加密（协议与服务端 fz_crypto.py 对齐）；Lua 块里的透传桩因 `if not JM` 自动跳过
    wjjh_jm_install(L);
    wjjh_bootlog("WJJH_BOOT: JM real crypto installed");

    // 原生 XMLHttpRequest（NSURLSession）：cocos 的 LuaMinXmlHttpRequest tolua 绑定
    // 在 Release 下 self 为 NULL 直接解引用（b71 .ips 实证），整体替换
    lua_register(L, "__wjjh_xhr_class", [](lua_State* LS) -> int {
        wjjh_xhr_push_class(LS);
        return 1;
    });

    std::string path = FileUtils::getInstance()->fullPathForFilename("src/main.lua");
    std::string probeRes = FileUtils::getInstance()->fullPathForFilename("res/LuaExtend.lua");
    wjjh_bootlog(("WJJH_BOOT: src/main.lua -> " + path).c_str());
    wjjh_bootlog(("WJJH_BOOT: res/LuaExtend.lua -> " + probeRes).c_str());
    if (path.empty()) {
        wjjh_bootlog("WJJH_BOOT: FATAL src/main.lua not found in bundle");
    } else {
        // pcall 包住 main.lua；print 重定向：cocos 的 print 在 COCOS2D_DEBUG=0 下静默，
        // 会吞掉游戏 xpcall 的 LUA ERROR
        std::string chunk =
            "__wjjhlog('WJJH_BOOT: print redirect installed, jit=' .. tostring(jit and jit.status and jit.status() or '?'))\n"
            // ★ 安卓自定义 so 的 tolua 类带 .isclass 标志(quick class 据此走原生分支),
            //   官方 3.15.1 的 ccui/ccs 系没有 → class("X", ccui.Widget) 走纯 Lua 分支,
            //   实例=无元表裸 table,所有 cocos 方法全 nil(gh33 实证)。补章:
            "local function __wjjh_stamp(ns)\n"
            "  if type(ns) ~= 'table' then return 0 end\n"
            "  local cnt = 0\n"
            "  for _k, v in pairs(ns) do\n"
            "    if type(v) == 'table' and type(v.create) == 'function' and v['.isclass'] ~= true then\n"
            "      v['.isclass'] = true\n"
            "      cnt = cnt + 1\n"
            "    end\n"
            "  end\n"
            "  return cnt\n"
            "end\n"
            "local __ns = 0\n"
            "__ns = __ns + __wjjh_stamp(cc)\n"
            "__ns = __ns + __wjjh_stamp(ccui)\n"
            "__ns = __ns + __wjjh_stamp(ccs)\n"
            "__ns = __ns + __wjjh_stamp(spine)\n"
            "__ns = __ns + __wjjh_stamp(cc.Sprite3D)\n"
            "__wjjhlog('WJJH_BOOT: isclass stamped on ' .. __ns .. ' native classes')\n"
            // A 类空壳绑定：UpdateManager/SdkMethod 在 Android 由 Java/JNI 提供，iOS 离线壳用 Lua 桩；
            // 未列出的方法由 __index 自动生成 no-op（返回 false）
            "if not UpdateManager then\n"
            "  local um = {}\n"
            "  um.getDomain = function() return 'http://127.0.0.1:7900/' end\n"
            "  um.getSocketDomain = function() return '' end\n"
            "  um.getVersion = function() return '0' end\n"
            "  um.getStringVersion = function() return '0' end\n"
            "  um.getUpdatePath = function() return '' end\n"
            "  setmetatable(um, {__index = function(t, k) local f = function() return false end; rawset(t, k, f); return f end})\n"
            "  UpdateManager = um\n"
            "  __wjjhlog('WJJH_BOOT: UpdateManager stub installed')\n"
            "end\n"
            "if not SdkMethod then\n"
            "  local sm = {}\n"
            "  setmetatable(sm, {__index = function(t, k) local f = function() return false end; rawset(t, k, f); return f end})\n"
            "  SdkMethod = sm\n"
            "  __wjjhlog('WJJH_BOOT: SdkMethod stub installed')\n"
            "end\n"
            // C/D 类桩工厂：实例=真 cc.Node（可 addChild）+ tolua peer 自动 no-op（富文本/骨骼等方法名未知），
            // create 时把游戏给类表打的补丁拷进 peer；后续报错会点名真正需要实现的类
            "local __noop = function() return nil end\n"
            "local __newChain = function(parent)\n"
            "    local d = {}\n"
            "    return setmetatable(d, {__index = function(tt, k) local v = parent and parent[k]; if v ~= nil then return v end; return __newChain(parent) end, __call = function() return __newChain(parent) end})\n"
            "  end\n"            "local __stubClass = function(name)\n"
            "  if _G[name] then return _G[name] end\n"
            "  local t = {}\n"
            "  t.create = function(...)\n"
            "    local node = cc.Node:create()\n"
            "    if tolua and tolua.setpeer then\n"
            "      local peer = {}\n"
            "      setmetatable(peer, {__index = function(tt, k) local f = function() return nil end; rawset(tt, k, f); return f end})\n"
            "      tolua.setpeer(node, peer)\n"
            "      for k, v in pairs(t) do if k ~= 'create' then peer[k] = v end end\n"
            "    end\n"
            "    return node\n"
            "  end\n"
            "  setmetatable(t, {__index = function(tt, k) local f = __noop; rawset(tt, k, f); return f end})\n"
            "  _G[name] = t\n"
            "  return t\n"
            "end\n"
            "for _, n in ipairs({'ExtRichText','ExtRichTextScroll','ExtPageView','YXShaderSprite','YXMotionStreak',"
            "'YXEaseAction','YXHelper','encrypt','LogManager'}) do\n"
            "  __stubClass(n)\n"
            "end\n"
            // 骨骼动画族用链式哑表（可无限索引+可调用）：AnimResManager 会
            // spine38.NewSkeletonAnimation:createWithBinaryFile 深链调用（b76 实证）；
            // 普通类不能用链式——与 Decorator 的 __decorator 记账互踩（gh4 实证）
            "local __stubClassChain = function(name)\n"
            "  if _G[name] then return _G[name] end\n"
            "  local t = {}\n"
            "  t.create = function(...)\n"
            "    local node = cc.Node:create()\n"
            "    if tolua and tolua.setpeer then\n"
            "      local peer = {}\n"
            "      setmetatable(peer, {__index = function(tt, k) local f = function() return __newChain(t) end; rawset(tt, k, f); return f end})\n"
            "      tolua.setpeer(node, peer)\n"
            "      for k, v in pairs(t) do if k ~= 'create' then peer[k] = v end end\n"
            "    end\n"
            "    return node\n"
            "  end\n"
            "  t.getAnimEvents = function(self, animName)\n"
            "    return { { name = \"Hurt\", stringValue = \"chest\", time = 0, floatValue = 0, intValue = 0 } }\n"
            "  end\n"
            "  t.getAnimDuration = function(self, animName)\n"
            "    return 0.1\n"
            "  end\n"
            "  setmetatable(t, {__index = function(tt, k) local f = function() return __newChain(t) end; rawset(tt, k, f); return f end})\n"
            "  _G[name] = t\n"
            "  return t\n"
            "end\n"
            "for _, n in ipairs({'YXSkeletonAnimation','YXSkeletonAnimationCache','NewSkeletonAnimation'}) do\n"
            "  __stubClassChain(n)\n"
            "end\n"
            "spine38 = setmetatable({}, {__index = function(tt, k) local g = _G[k]; if g ~= nil then return g end; return __newChain(nil) end})\n"
            "__wjjhlog('WJJH_BOOT: C/D class stubs installed')\n"
            // cpp.Game 桩：native Game 单例（时间/用户/渠道）。渠道值与服务端
            // getWebConfig 模板的 PackageChecklist guanfang 一致
            "if not cpp then\n"
            "  cpp = {}\n"
            "  local g = { _uid = 0, _time = os.time(), _channel = 'guanfang' }\n"
            "  g.getInstance = function() return g end\n"
            "  g.getTime = function() return g._time end\n"
            "  g.setTime = function(t) g._time = t end\n"
            "  g.getUserId = function() return g._uid end\n"
            "  g.setUserId = function(id) g._uid = id end\n"
            "  g.getChannelId = function() return g._channel end\n"
            "  g.setChannelId = function(c) g._channel = c end\n"
            "  g.getPackageId = function() return g._channel end\n"
            "  g.connectServer = function(cb)\n"
            "    if type(cb) == \"function\" then cb(\"TRUE\") end\n"
            "    return \"TRUE\"\n"
            "  end\n"
            "  setmetatable(g, {__index = function(tt, k) local f = function() return 0 end; rawset(tt, k, f); return f end})\n"
            "  cpp.Game = g\n"
            "__wjjhlog('WJJH_BOOT: cpp.Game stub installed')\n"
            "end\n"
            // 诊断：convertUI 后打印走到的子节点名（PrintUI Panel_print nil 排查，gh14）
            "local __wjjh_hookInstalled = false\n"
                        "local __wjjh_tryHook\n"
            "__wjjh_tryHook = function()\n"
            "  if __wjjh_hookInstalled or not (Helper and Helper.convertUI) then return end\n"
            "  __wjjh_hookInstalled = true\n"
            "  local orig = Helper.convertUI\n"
            "  Helper.convertUI = function(self, r)\n"
            "    orig(self, r)\n"
            "    local names = {}\n"
            "    local ok, err = pcall(function() Helper:callChildren(self, function(c) names[#names+1] = tostring(c:getName()) end) end)\n"
            "    local who = type(self)\n"
            "    local realn = '?'\n"
            "    pcall(function()\n"
            "      who = tolua.type(self)\n"
            "      local okr, kids = pcall(self.getChildren, self)\n"
            "      if okr and kids then realn = #kids end\n"
            "    end)\n"
            "    local ident = '?'\n"
            "    pcall(function() ident = tostring(rawget(self, '__cname') or '-') .. '/' .. tostring(rawget(self, 'class') and rawget(self, 'class').__cname or '-') .. '/inh=' .. tostring(rawget(self, '__inherit') ~= nil) .. '/mt=' .. tostring(getmetatable(self) ~= nil) end)\n"
            "    local tb = '?'\n"
            "    pcall(function() tb = string.gsub(debug.traceback('', 3), '[%c]', ' ') end)\n"
            "    __wjjhlog('WJJH_CONVERTUI ident=' .. ident .. ' who=' .. tostring(who) .. ' err=' .. tostring(err) .. ' @' .. string.sub(tb, 1, 140))\n"
            "  end\n"
            "  __wjjhlog('WJJH_BOOT: convertUI hook installed')\n"
            "  pcall(function()\n"
            "      local C = class('WjjhProbe', cc.Node)\n"
            "      local t = C:create()\n"
            "      local ttype = '?'\n"
            "      pcall(function() ttype = tolua.type(t) end)\n"
            "      local gok, gerr = pcall(function() return #t:getChildren() end)\n"
            "      __wjjhlog('WJJH_Q ttype=' .. tostring(ttype) .. ' isFuncGC=' .. tostring(type(t.getChildren)) .. ' gcok=' .. tostring(gok) .. ' gerr=' .. tostring(gerr))\n"
            "      local L = class('WjjhProbeL', cc.Layer)\n"
            "      local li = L:create()\n"
            "      local ltype = '?'\n"
            "      local lok, lerr = pcall(function() ltype = tolua.type(li) end)\n"
            "      local lgok, lgerr = pcall(function() return #li:getChildren() end)\n"
            "      local lctor = '?'\n"
            "      pcall(function() lctor = type(li.ctor) end)\n"
            "      __wjjhlog('WJJH_QL ttype=' .. tostring(ltype) .. ' tok=' .. tostring(lok) .. ' terr=' .. tostring(lerr) .. ' isFuncGC=' .. tostring(type(li.getChildren)) .. ' gcok=' .. tostring(lgok) .. ' gerr=' .. tostring(lgerr) .. ' ctor=' .. tostring(lctor))\n"
            "  end)\n"
            "  pcall(function()\n"
            "    __wjjhlog('WJJH_TOLUA Node.new=' .. tostring(cc.Node.new ~= nil) .. ' Layer.new=' .. tostring(cc.Layer ~= nil and cc.Layer.new ~= nil) .. ' Node.create=' .. tostring(cc.Node.create ~= nil))\n"
            "    local nd = cc.Node:create()\n"
            "    __wjjhlog('WJJH_TOLUA ndtype=' .. tostring(tolua.type(nd)) .. ' getChildren=' .. tostring(nd.getChildren ~= nil) .. ' children=' .. tostring(#nd:getChildren()))\n"
            "  end)\n"
            "end\n"
            "local __req = require\n"
            "local __req_hooked = {}\n"
            "require = function(name)\n"
            "  local m = __req(name)\n"
            "  if name == 'Layer/PrintUI.lua' and type(m) == 'table' and type(m.create) == 'function' and not __req_hooked[name] then\n"
            "    __req_hooked[name] = true\n"
            "    local onw = m.new\n"
            "    m.new = function(...)\n"
            "      local p = onw(...)\n"
            "      local pt = '?'\n"
            "      pcall(function() pt = tolua.type(p) end)\n"
            "      __wjjhlog('WJJH_PNEW type=' .. type(p) .. ' tolua=' .. tostring(pt))\n"
            "      return p\n"
            "    end\n"
            "    local oc = m.create\n"
            "    m.create = function(...)\n"
            "      local r = oc(...)\n"
            "      local n, nm = -1, '?'\n"
            "      if type(r) == 'table' and r.root then\n"
            "        local kids = r.root:getChildren()\n"
            "        n = #kids\n"
            "        nm = {}\n"
            "        for i, c in ipairs(kids) do nm[i] = tostring(c:getName()) end\n"
            "        nm = table.concat(nm, ',')\n"
            "      end\n"
            "      __wjjhlog('WJJH_LAYOUT PrintUI root_children=' .. tostring(n) .. ' [' .. tostring(nm) .. ']')\n"
            "      return r\n"
            "    end\n"
            "  end\n"
            "  if name == 'app.views.ui.PrintUI' and type(m) == 'table' and type(m.create) == 'function' and not __req_hooked[name] then\n"
            "    __req_hooked[name] = true\n"
            "    local oc = m.create\n"
            "    m.create = function(...)\n"
            "      local p = oc(...)\n"
            "      local pi, pt = '?', '?'\n"
            "      pcall(function() pi = tostring(rawget(p, '__cname')) .. '/' .. tostring(rawget(p, '__inherit') ~= nil) .. '/' .. tostring(getmetatable(p) ~= nil) end)\n"
            "      pcall(function() pt = tolua.type(p) end)\n"
            "      __wjjhlog('WJJH_P create -> type=' .. type(p) .. ' tolua=' .. tostring(pt) .. ' id=' .. pi)\n"
            "      if type(p) == 'table' then\n"
            "        local oi = p.init\n"
            "        if oi then\n"
            "          p.init = function(s, ...)\n"
            "            local r = oi(s, ...)\n"
            "            local pp, st = '?', '?'\n"
            "            pcall(function() pp = tostring(s.Panel_print) end)\n"
            "            pcall(function() st = tostring(s._round) end)\n"
            "            __wjjhlog('WJJH_P after init: Panel_print=' .. pp .. ' _round=' .. st)\n"
            "            return r\n"
            "          end\n"
            "        end\n"
            "      end\n"
            "      return p\n"
            "    end\n"
            "  end\n"
            "  return m\n"
            "end\n"
            "cc.Director:getInstance():getScheduler():scheduleScriptFunc(__wjjh_tryHook, 0.5, false)\n"
            // luaTableEncode/Decode：安卓在自定义 libcocos2dlua.so 里提供（全 Lua 树无定义），
            // 本地存档读写（DataBase:getData/getLuaTable→User.lua:21）第一步就要用；
            // 自洽格式：string.format(%q) + loadstring 回读，完整保留键类型/嵌套
            "if not luaTableEncode then\n"
            "  local __enc\n"
            "  __enc = function(v, seen)\n"
            "    seen = seen or {}\n"
            "    local t = type(v)\n"
            "    if t == 'nil' then return 'nil'\n"
            "    elseif t == 'boolean' then return tostring(v)\n"
            "    elseif t == 'number' then return string.format('%.17g', v)\n"
            "    elseif t == 'string' then return string.format('%q', v)\n"
            "    elseif t == 'table' then\n"
            "      if seen[v] then return 'nil' end\n"
            "      seen[v] = true\n"
            "      local parts = {}\n"
            "      local n = 0\n"
            "      for _i, val in ipairs(v) do\n"
            "        n = n + 1\n"
            "        parts[#parts+1] = __enc(val, seen)\n"
            "      end\n"
            "      for k, val in pairs(v) do\n"
            "        if not (type(k) == 'number' and math.floor(k) == k and k >= 1 and k <= n) then\n"
            "          local ks\n"
            "          if type(k) == 'string' and k:match('^[A-Za-z_][A-Za-z0-9_]*$') then ks = k\n"
            "          else ks = '[' .. __enc(k, seen) .. ']' end\n"
            "          parts[#parts+1] = ks .. '=' .. __enc(val, seen)\n"
            "        end\n"
            "      end\n"
            "      seen[v] = nil\n"
            "      return '{' .. table.concat(parts, ',') .. '}'\n"
            "    else return 'nil' end\n"
            "  end\n"
            "  luaTableEncode = function(t) return 'return ' .. __enc(t, {}) end\n"
            "  luaTableDecode = function(s)\n"
            "    if type(s) ~= 'string' or s == '' then return nil end\n"
            "    local f = loadstring(s)\n"
            "    if not f then return nil end\n"
            "    local ok, t = pcall(f)\n"
            "    if not ok then return nil end\n"
            "    return t\n"
            "  end\n"
            "  __wjjhlog('WJJH_BOOT: luaTableEncode/Decode stubs installed')\n"
            "end\n"
            // 原生 XMLHttpRequest（NSURLSession）：强制覆盖 cocos 自带的
            // LuaMinXmlHttpRequest（其 tolua 绑定 Release 下空指针崩溃，b71 实证；
            // 引擎本来就注册了 cc.XMLHttpRequest，所以不能加 not-exists 守卫）
            "if cc then\n"
            "  cc.XMLHttpRequest = __wjjh_xhr_class()\n"
            "  cc.XMLHTTPREQUEST_RESPONSE_STRING = 0\n"
            "  cc.XMLHTTPREQUEST_RESPONSE_JSON = 1\n"
            "  cc.XMLHTTPREQUEST_RESPONSE_ARRAY_BUFFER = 2\n"
            "  __wjjhlog('WJJH_BOOT: native XMLHttpRequest (NSURLSession) installed')\n"
            "end\n"
            // B 类：JM 字符串加密（真实现=私服同款算法，接入内嵌服务端时再做；先原样透传）
            "if not JM then\n"
            "  local jm = {}\n"
            "  jm.stringDecrypt = function(self, s) return s end\n"
            "  jm.stringEncrypt = function(self, s, v) return s end\n"
            "  jm.getKey = function(self) return '' end\n"
            "  jm.isEncrypted = function(self, s) return false end\n"
            "  setmetatable(jm, {__index = function(t, k) local f = function() return false end; rawset(t, k, f); return f end})\n"
            "  JM = jm\n"
            "  __wjjhlog('WJJH_BOOT: JM stub installed')\n"
            "end\n"
            "local _origprint = print\n"
            "print = function(...)\n"
            "  local n = select('#', ...)\n"
            "  local parts = {}\n"
            "  for i = 1, n do parts[i] = tostring(select(i, ...)) end\n"
            "  __wjjhlog(table.concat(parts, '\\t'))\n"
            "  _origprint(...)\n"
            "end\n"
            "local f, err = loadfile('" + path + "')\n"
            "if not f then __wjjhlog('WJJH_BOOTERR loadfile: ' .. tostring(err))\n"
            "else\n"
            "  local ok, e = pcall(f)\n"
            "  if ok then __wjjhlog('WJJH_BOOT: main.lua finished OK')\n"
            "  else __wjjhlog('WJJH_BOOTERR runtime: ' .. tostring(e)) end\n"
            "end\n";
        // executeString 会静默吞掉语法/运行错误；显式 load+pcall 把错误打进 launch.log
        lua_pushlstring(L, chunk.c_str(), chunk.size());
        lua_setglobal(L, "__WJJH_CHUNK");
        engine->executeString(
            "__WJJH_ERR = nil\n"
            "local f, e = load(__WJJH_CHUNK, 'wjjh-chunk')\n"
            "if not f then\n"
            "  __WJJH_ERR = 'syntax: ' .. tostring(e)\n"
            "else\n"
            "  local ok, r = xpcall(f, function(er) return debug.traceback(er, 2) end)\n"
            "  if not ok then __WJJH_ERR = 'runtime: ' .. tostring(r) end\n"
            "end\n"
            "__wjjhlog('WJJH_CHUNK ' .. (__WJJH_ERR or 'OK'))\n");

        auto sc = Director::getInstance()->getRunningScene();
        wjjh_bootlog(("WJJH_BOOT: runningScene=" +
                      (sc ? ("'" + sc->getName() + "'") : string("(null)")) +
                      " paused=" + to_string((int)Director::getInstance()->isPaused())).c_str());
    }
    return true;
}

void AppDelegate::applicationDidEnterBackground()
{
    Director::getInstance()->stopAnimation();
}

void AppDelegate::applicationWillEnterForeground()
{
    Director::getInstance()->startAnimation();
}
