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

    // main.lua 路径注入给 chunk
    lua_pushlstring(L, path.c_str(), path.size());
    lua_setglobal(L, "__WJJH_MAINLUA");
    wjjh_bootlog(("WJJH_BOOT: src/main.lua -> " + path).c_str());
    wjjh_bootlog(("WJJH_BOOT: res/LuaExtend.lua -> " + probeRes).c_str());
    if (path.empty()) {
        wjjh_bootlog("WJJH_BOOT: FATAL src/main.lua not found in bundle");
    } else {
        // pcall 包住 main.lua；print 重定向：cocos 的 print 在 COCOS2D_DEBUG=0 下静默，
        // 会吞掉游戏 xpcall 的 LUA ERROR
        std::string chunk = R"wjjh(
__wjjhlog('WJJH_BOOT: print redirect installed, jit=' .. tostring(jit and jit.status and jit.status() or '?'))

-- ===== isclass 补章(安卓自定义 so 带 .isclass,官方 3.15.1 缺)=====
local function __wjjh_stamp1(tbl)
  if type(tbl) == 'table' and type(tbl.create) == 'function' and tbl['.isclass'] ~= true then
    tbl['.isclass'] = true
    return 1
  end
  return 0
end
local __ns, __detail = 0, ''
local __list = { 'cc.Node', 'cc.Layer', 'cc.LayerColor', 'cc.Sprite', 'cc.Scene',
  'ccui.Widget', 'ccui.Layout', 'ccui.Text', 'ccui.Button', 'ccui.ImageView',
  'ccui.ScrollView', 'ccui.ListView', 'ccui.PageView', 'cc.MenuItemSprite' }
for _i, path in ipairs(__list) do
  local cur = _G
  for w in string.gmatch(path, '[%a]+') do
    if type(cur) == 'table' then cur = cur[w] end
  end
  if type(cur) == 'table' then
    __ns = __ns + __wjjh_stamp1(cur)
    __detail = __detail .. path .. '=' .. tostring(cur['.isclass'] == true) .. ' '
  else
    __detail = __detail .. path .. '=miss '
  end
end
__wjjhlog('WJJH_BOOT: isclass stamped ' .. __ns .. ' [' .. __detail .. ']')

-- ===== addChild 兼容包装(ccui 系 tolua 绑定固定三参,游戏大量 1/2 参调用)=====
if cc and cc.Node and type(cc.Node.addChild) == 'function' then
  local __origAddChild = cc.Node.addChild
  cc.Node.addChild = function(self, child, z, tag)
    if child == nil then return end
    if z == nil then return __origAddChild(self, child, 0, 0) end
    if tag == nil then return __origAddChild(self, child, z, 0) end
    return __origAddChild(self, child, z, tag)
  end
end

-- ===== A 类桩 =====
if not UpdateManager then
  local um = {}
  um.getDomain = function() return 'http://127.0.0.1:7900/' end
  um.getSocketDomain = function() return '' end
  um.getVersion = function() return '0' end
  um.getStringVersion = function() return '0' end
  um.getUpdatePath = function() return '' end
  setmetatable(um, {__index = function(t, k) local f = function() return false end; rawset(t, k, f); return f end})
  UpdateManager = um
  __wjjhlog('WJJH_BOOT: UpdateManager stub installed')
end
if not SdkMethod then
  local sm = {}
  setmetatable(sm, {__index = function(t, k) local f = function() return false end; rawset(t, k, f); return f end})
  SdkMethod = sm
  __wjjhlog('WJJH_BOOT: SdkMethod stub installed')
end

-- ===== C/D 桩工厂(普通类=函数 no-op;骨骼族=链式)=====
local __noop = function() return nil end
local __chain = setmetatable({}, {__index = function() return __chain end, __call = function() return __chain end})
local __stubClass = function(name)
  if _G[name] then return _G[name] end
  local t = {}
  t.create = function(...)
    local node = cc.Node:create()
    if tolua and tolua.setpeer then
      local peer = {}
      setmetatable(peer, {__index = function(tt, k) local f = function() return nil end; rawset(tt, k, f); return f end})
      tolua.setpeer(node, peer)
      for k, v in pairs(t) do if k ~= 'create' then peer[k] = v end end
    end
    return node
  end
  setmetatable(t, {__index = function(tt, k) local f = __noop; rawset(tt, k, f); return f end})
  _G[name] = t
  return t
end
for _, n in ipairs({'ExtRichText','ExtPageView','YXShaderSprite','YXMotionStreak','YXEaseAction','YXHelper','encrypt','LogManager'}) do
  __stubClass(n)
end
local __stubClassChain = function(name)
  if _G[name] then return _G[name] end
  local t = {}
  t.create = function(...)
    local node = cc.Node:create()
    if tolua and tolua.setpeer then
      local peer = {}
      setmetatable(peer, {__index = function(tt, k) local f = function() return __chain end; rawset(tt, k, f); return f end})
      tolua.setpeer(node, peer)
      for k, v in pairs(t) do if k ~= 'create' then peer[k] = v end end
    end
    return node
  end
  setmetatable(t, {__index = function(tt, k) local f = function() return __chain end; rawset(tt, k, f); return f end})
  _G[name] = t
  return t
end
for _, n in ipairs({'YXSkeletonAnimation','YXSkeletonAnimationCache'}) do
  __stubClassChain(n)
end

-- ===== 骨骼对象工厂:真 cc.Node+peer(菜单标题动画 addChild 需要真节点)=====
local __mkSkel = function(...)
  local sk = cc.Node:create()
  local events = { { name = 'Hurt', stringValue = 'chest', time = 0, floatValue = 0, intValue = 0 } }
  tolua.setpeer(sk, {
    getAnimEvents = function(self, animName) return events end,
    getAnimDuration = function(self, animName) return 0.1 end,
    setAnimation = function(self, a, n, l) end,
    addAnimation = function(self, ...) end,
    setTrackTime = function(self, t) end,
    setTimeScale = function(self, s) end,
    setCompleteListener = function(self, cb) end,
    setEventCallback = function(self, cb) end,
    registerScriptHandler = function(self, cb) end,
    getBoneSetupPosePosition = function(self, a, b) return {x = 0, y = 0} end,
    updateWorldTransform = function(self) end,
    setToSetupPose = function(self) end,
  })
  return sk
end
spine38 = setmetatable({}, {__index = function(t, k)
  if k == 'NewSkeletonAnimation' then
    return { createWithBinaryFile = __mkSkel, createWithFile = __mkSkel, create = __mkSkel }
  end
  return nil
end})
__wjjhlog('WJJH_BOOT: C/D stubs + skeleton factory installed')

-- ===== cpp.Game 桩 =====
if not cpp then
  cpp = {}
  local g = { _uid = 0, _time = os.time(), _channel = 'guanfang' }
  g.getInstance = function() return g end
  g.getTime = function() return g._time end
  g.setTime = function(t) g._time = t end
  g.getUserId = function() return g._uid end
  g.setUserId = function(id) g._uid = id end
  g.getChannelId = function() return g._channel end
  g.setChannelId = function(c) g._channel = c end
  g.getPackageId = function() return g._channel end
  g.connectServer = function(cb)
    if type(cb) == 'function' then cb('TRUE') end
    return 'TRUE'
  end
  setmetatable(g, {__index = function(t, k) local f = function() return 0 end; rawset(t, k, f); return f end})
  cpp.Game = g
  __wjjhlog('WJJH_BOOT: cpp.Game stub installed')
end

-- ===== luaTableEncode/Decode 桩 =====
if not luaTableEncode then
  local __enc
  __enc = function(v, seen)
    seen = seen or {}
    local t = type(v)
    if t == 'nil' then return 'nil'
    elseif t == 'boolean' then return tostring(v)
    elseif t == 'number' then return string.format('%.17g', v)
    elseif t == 'string' then return string.format('%q', v)
    elseif t == 'table' then
      if seen[v] then return 'nil' end
      seen[v] = true
      local parts = {}
      local n = 0
      for _i, val in ipairs(v) do
        n = n + 1
        parts[#parts+1] = __enc(val, seen)
      end
      for k, val in pairs(v) do
        if not (type(k) == 'number' and math.floor(k) == k and k >= 1 and k <= n) then
          local ks
          if type(k) == 'string' and k:match('^[A-Za-z_][A-Za-z0-9_]*$') then ks = k
          else ks = '[' .. __enc(k, seen) .. ']' end
          parts[#parts+1] = ks .. '=' .. __enc(val, seen)
        end
      end
      seen[v] = nil
      return '{' .. table.concat(parts, ',') .. '}'
    else return 'nil' end
  end
  luaTableEncode = function(t) return 'return ' .. __enc(t, {}) end
  luaTableDecode = function(s)
    if type(s) ~= 'string' or s == '' then return nil end
    local f = loadstring(s)
    if not f then return nil end
    local ok, t = pcall(f)
    if not ok then return nil end
    return t
  end
  __wjjhlog('WJJH_BOOT: luaTableEncode/Decode stubs installed')
end

-- ===== ExtRichTextScroll 真实现(真节点+peer,getRichText 返回哑富文本)=====
if ExtRichTextScroll and rawget(ExtRichTextScroll, '__wjjh_real') ~= true then
  local __inner = { setVerticalSpace = function() end,
    getNewContentSizeHeight = function() return 0 end,
    pushBackText = function() end, pushBackNewLine = function() end, removeAllChildren = function() end }
  setmetatable(__inner, {__index = function() return function() return __inner end end})
  local __peer = {
    getRichText = function() return __inner end,
    setBounceEnabled = function() end, setDirection = function() end,
    setSize = function() end, pushBackText = function() end,
    pushBackNewLine = function() end, setDirectionEnabled = function() end }
  local __node = cc.Node:create()
  tolua.setpeer(__node, __peer)
  ExtRichTextScroll.create = function(...)
    local n = cc.Node:create()
    tolua.setpeer(n, __peer)
    return n
  end
  rawset(ExtRichTextScroll, '__wjjh_real', true)
  __wjjhlog('WJJH_BOOT: ExtRichTextScroll real impl installed')
end

-- ===== LoadingLayer.update 守卫(setTotalCount 之前不执行)=====
local __wjjh_llTried = false
local function __wjjh_tryHook()
  if __wjjh_llTried then return end
  local LL = package.loaded['app.views.layer.LoadingLayer']
  if type(LL) ~= 'table' or LL.__wjjh_wrap then return end
  LL.__wjjh_wrap = true
  __wjjh_llTried = true
  local oup = LL.update
  LL.update = function(self, ft)
    if self._loadingIndex == nil or self._totalCount == nil then return end
    return oup(self, ft)
  end
  __wjjhlog('WJJH_BOOT: LoadingLayer.update guarded')
end
cc.Director:getInstance():getScheduler():scheduleScriptFunc(__wjjh_tryHook, 0.5, false)

"// ===== XMLHttpRequest(NSURLSession 原生实现)=====\nif cc then\n  cc.XMLHttpRequest = __wjjh_xhr_class()\n  cc.XMLHTTPREQUEST_RESPONSE_STRING = 0\n  cc.XMLHTTPREQUEST_RESPONSE_JSON = 1\n  cc.XMLHTTPREQUEST_RESPONSE_ARRAY_BUFFER = 2\n  __wjjhlog('WJJH_BOOT: native XMLHttpRequest (NSURLSession) installed')\nend\n\n// ===== JM 兜底桩(native wjjh_jm_install 失败时才生效)=====\nif not JM then\n  local jm = {}\n  jm.stringDecrypt = function(self, s) return s end\n  jm.stringEncrypt = function(self, s, v) return s end\n  jm.getKey = function(self) return '' end\n  jm.isEncrypted = function(self, s) return false end\n  setmetatable(jm, {__index = function(t, k) local f = function() return false end; rawset(t, k, f); return f end})\n  JM = jm\n  __wjjhlog('WJJH_BOOT: JM stub installed')\nend\n\n// ===== print 重定向(游戏错误上报走 print,不重定向则全被静默吞掉)=====\nlocal _origprint = print\nprint = function(...)\n  local n = select('#', ...)\n  local parts = {}\n  for i = 1, n do parts[i] = tostring(select(i, ...)) end\n  __wjjhlog(table.concat(parts, '\t'))\n  _origprint(...)\nend\n\n-- ===== main.lua =====
local f, err = loadfile(__WJJH_MAINLUA)
if not f then
  __wjjhlog('WJJH_BOOTERR loadfile: ' .. tostring(err))
else
  local ok, e = pcall(f)
  if ok then __wjjhlog('WJJH_BOOT: main.lua finished OK')
  else __wjjhlog('WJJH_BOOTERR runtime: ' .. tostring(e)) end
end
)wjjh";

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
