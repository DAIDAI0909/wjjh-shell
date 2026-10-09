#include "AppDelegate.h"
#include "scripting/lua-bindings/manual/CCLuaEngine.h"
#include "scripting/lua-bindings/manual/tolua_fix.h"
#include "scripting/lua-bindings/manual/lua_module_register.h"
#include "cocos2d.h"
#include "WjjhJM.h"
#include "WjjhXHR.h"
#include <os/log.h>
#include <sys/time.h>
#include <unistd.h>

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

// ---- gh151: LuaSocket 最小替身 ----
// iOS 游戏包内没有 LuaSocket 的 Lua 文件（src/packages/ 只有 mvc），但
// app/Definition.lua:1 与 cocos/cocos2d/functions.lua 都 require("socket")，
// 且全树只用到 socket.gettime（17 处计时）与 socket.select（1 处 sleep）。
// 这里装一个原生小模块进 package.loaded['socket']（require 直接命中，不查文件）
// + 全局 socket（游戏直接 socket.gettime()）。真网络不走 LuaSocket（走自研 XHR）。
static double wjjh_now_sec()
{
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    return (double)tv.tv_sec + (double)tv.tv_usec / 1000000.0;
}

static int wjjh_lua_socket_gettime(lua_State* LS)
{
    lua_pushnumber(LS, (lua_Number)wjjh_now_sec());
    return 1;
}

static int wjjh_lua_socket_select(lua_State* LS)
{
    // socket.select(recvt, sendt, timeout)：游戏只用 (nil,nil,n) 当 sleep；
    // 保持"超时返回 nil,'timeout'"语义
    lua_Number t = luaL_optnumber(LS, 3, 0);
    if (t > 0) usleep((useconds_t)(t * 1000000.0));
    lua_pushnil(LS);
    lua_pushstring(LS, "timeout");
    return 2;
}

static void wjjh_socket_install(lua_State* L)
{
    lua_newtable(L);
    lua_pushcfunction(L, wjjh_lua_socket_gettime);
    lua_setfield(L, -2, "gettime");
    lua_pushcfunction(L, wjjh_lua_socket_select);
    lua_setfield(L, -2, "select");
    lua_pushstring(L, "LuaSocket-stub(wjjh)");
    lua_setfield(L, -2, "_VERSION");

    lua_pushvalue(L, -1);
    lua_setglobal(L, "socket");          // 全局 socket = M

    lua_getglobal(L, "package");         // [M, package]
    if (lua_istable(L, -1)) {
        lua_getfield(L, -1, "loaded");   // [M, package, loaded]
        if (lua_istable(L, -1)) {
            lua_pushvalue(L, -3);        // [M, package, loaded, M]
            lua_setfield(L, -2, "socket");   // loaded.socket = M
        }
        lua_pop(L, 1);                   // [M, package]
    }
    lua_pop(L, 1);                       // [M]
    lua_pop(L, 1);                       // []
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

    // LuaSocket 替身（包里没有 socket.lua；游戏只用 gettime/select 计时与 sleep）
    wjjh_socket_install(L);
    wjjh_bootlog("WJJH_BOOT: socket stub installed (gettime/select)");

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
    // gh154: CI 自动点按开关（只由 CMake 的 WJJH_CI_DIAG 打开；真机包恒 false）
#if defined(WJJH_CI_DIAG)
    lua_pushboolean(L, 1);
#else
    lua_pushboolean(L, 0);
#endif
    lua_setglobal(L, "__WJJH_AUTOSTART");
    wjjh_bootlog(("WJJH_BOOT: src/main.lua -> " + path).c_str());
    wjjh_bootlog(("WJJH_BOOT: res/LuaExtend.lua -> " + probeRes).c_str());
    if (path.empty()) {
        wjjh_bootlog("WJJH_BOOT: FATAL src/main.lua not found in bundle");
    } else {
        // pcall 包住 main.lua；print 重定向：cocos 的 print 在 COCOS2D_DEBUG=0 下静默，
        // 会吞掉游戏 xpcall 的 LUA ERROR
        std::string chunk = R"wjjh(__wjjhlog('WJJH_BOOT: print redirect installed, jit=' .. tostring(jit and jit.status and jit.status() or '?'))

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

-- ===== addChild 兼容包装(gh61 修正:原实现把无 tag 的 addChild 一律传 tag=0,
--      cocos 3 参数 addChild 会把子节点 tag 重置为 0 -> tag 查重全灭的最初源头) =====
if cc and cc.Node and type(cc.Node.addChild) == 'function' then
  local __origAddChild = cc.Node.addChild
  cc.Node.addChild = function(self, child, z, tag)
    if child == nil then return end
    if z == nil then return __origAddChild(self, child) end
    if tag == nil then return __origAddChild(self, child, z) end
    return __origAddChild(self, child, z, tag)
  end
end

-- ===== gh153: tolua 守卫基础（安装期探测 tolua.isnull；chunk 早于 cocos Lua init，
--      该函数可能还没定义 —— 没有就优雅降级为"不拦"，保住原行为）=====
local __wjjh_isnull = nil
do
  local t = rawget(_G, 'tolua')
  if type(t) == 'table' and type(t.isnull) == 'function' then
    local ok, r = pcall(t.isnull, cc and cc.Director and cc.Director:getInstance() or nil)
    if ok then __wjjh_isnull = t.isnull end
  end
  __wjjhlog('WJJH_BOOT: tolua.isnull available=' .. tostring(__wjjh_isnull ~= nil))
end

-- ===== getChildByTag 纯 Lua 替代(gh59 实测 tagTest=false,原生查找不可靠) =====
-- gh153: 加 tolua 守卫。gh152 崩溃实锤 = lua_cocos2dx_Node_getTag 收到失效/空 userdata
-- （tolua_tousertype 在 Release 不做有效性检查，直接 self->getTag() -> SIGSEGV@0x0）。
-- 守卫同时"自证"：把非法子节点的类型 + Lua 调用点 traceback 打出来，限流 20 条。
if cc and cc.Node then
  __WJJH_nativeGetChildByTag = cc.Node.getChildByTag
  local __wjjh_badkid = 0
  local function __wjjh_kid_ok(k)
    if k == nil then return false end
    if __wjjh_isnull == nil then return true end
    local ok, bad = pcall(__wjjh_isnull, k)
    if not ok then return true end
    return not bad
  end
  cc.Node.getChildByTag = function(self, tag)
    if self == nil then return nil end
    local kids = self:getChildren()
    if type(kids) == 'table' then
      for i = 1, #kids do
        local k = kids[i]
        if k and type(k.getTag) == 'function' then
          if __wjjh_kid_ok(k) then
            if k:getTag() == tag then return k end
          elseif __wjjh_badkid < 20 then
            __wjjh_badkid = __wjjh_badkid + 1
            __wjjhlog('WJJH_TAGGUARD: child#' .. i .. ' invalid (type=' .. type(k) ..
                      ') bt=' .. tostring(debug.traceback('', 2)):gsub('\n', ' | '):sub(1, 700))
          end
        end
      end
    end
    return nil
  end
  __wjjhlog('WJJH_BOOT: getChildByTag polyfill installed (v2 tolua-guarded)')
end

-- ===== gh153: cc.Node.getTag 全局守卫 =====
-- 覆盖所有调用点（游戏自身 + 我们的探针）：self 非法时记 traceback 并返回 0，不再进 native。
if cc and cc.Node and type(cc.Node.getTag) == 'function' and not cc.Node.__wjjh_gettag_guarded then
  cc.Node.__wjjh_gettag_guarded = true
  local __wjjh_orig_getTag = cc.Node.getTag
  local __wjjh_badtag = 0
  cc.Node.getTag = function(self, ...)
    local bad = false
    if self == nil then
      bad = true
    elseif __wjjh_isnull ~= nil then
      local ok, r = pcall(__wjjh_isnull, self)
      if ok then bad = r end
    end
    if bad then
      if __wjjh_badtag < 20 then
        __wjjh_badtag = __wjjh_badtag + 1
        __wjjhlog('WJJH_TAGGUARD: getTag invalid self#' .. __wjjh_badtag .. ' (type=' .. type(self) ..
                  ') bt=' .. tostring(debug.traceback('', 2)):gsub('\n', ' | '):sub(1, 700))
      end
      return 0
    end
    return __wjjh_orig_getTag(self, ...)
  end
  __wjjhlog('WJJH_BOOT: cc.Node.getTag guard installed')
end

-- ===== SimpleAudioEngine 音频守卫(gh64 定案:res 树只有 Image/,音频文件全缺席;
--      首次 playEffect(DaijiBGM) 播缺失文件 -> cocos 3.15.1 AudioEngine 模拟器崩溃;
--      缺文件一律跳过并记日志) =====
if cc and cc.SimpleAudioEngine then
  local __SAE = cc.SimpleAudioEngine
  local __FU = cc.FileUtils:getInstance()
  local __audioNames = { 'playEffect', 'playMusic', 'preloadEffect', 'preloadMusic' }
  for __i = 1, #__audioNames do
    local __an = __audioNames[__i]
    local __of = __SAE[__an]
    if type(__of) == 'function' then
      local __ok = pcall(function()
        rawset(__SAE, __an, function(src, name, loop)
          if type(name) == 'string' and name ~= '' and not __FU:isFileExist(name) then
            __wjjhlog('WJJH_AUDIO: missing ' .. __an .. ' ' .. name)
            return 0
          end
          return __of(src, name, loop)
        end)
      end)
      __wjjhlog('WJJH_AUDIO: guard ' .. __an .. ' ' .. tostring(__ok))
    end
  end
end

-- ===== ccui.Text:setFontName 守卫(gh66 后新假设:TitleUI 刷新协程每帧
--      setFontName("Font/HYCFS.ttf"),字体文件缺失+每帧重建图集=churn;缺文件一律跳过) =====
if ccui and ccui.Text and type(ccui.Text.setFontName) == 'function' then
  local __ofn = ccui.Text.setFontName
  local __fntSeen = {}
  rawset(ccui.Text, 'setFontName', function(self, name)
    if type(name) == 'string' and name ~= '' and string.find(name, '%.ttf') and not __fntSeen[name] then
      if cc and cc.FileUtils and not cc.FileUtils:getInstance():isFileExist(name) then
        __fntSeen[name] = true
        __wjjhlog('WJJH_FONT: missing ' .. name .. ' (skip)')
        return
      end
    end
    return __ofn(self, name)
  end)
  __wjjhlog('WJJH_FONT: setFontName guard installed')
end

-- ===== GC step 守卫(gh65 定案:ControllLayer:collectGarbageStep 只在 iOS 每帧
--      collectgarbage("step",安卓官方注释明说关闭及时内存清理;崩溃窗口=循环安装后
--      ~2 秒内且崩溃点漂移=GC 不确定性。iOS 与安卓口径一致:step 改 no-op) =====
local __wjjh_gcSteps = 0
local __wjjh_origCG = collectgarbage
collectgarbage = function(opt, arg)
  if opt == 'step' then
    __wjjh_gcSteps = __wjjh_gcSteps + 1
    if __wjjh_gcSteps % 300 == 1 then
      __wjjhlog('WJJH_GC: step suppressed #' .. __wjjh_gcSteps)
    end
    return 0
  end
  return __wjjh_origCG(opt, arg)
end
__wjjhlog('WJJH_GC: step guard installed')

-- ===== freeMemorySmart 禁用(gh69 定案:Game:freeMemorySmart 在层切换动画完成回调里
--      全量 removeUnusedSpriteFrames+removeUnusedTextures,token 后首次层切换动画跑完
--      即触发=每轮死亡时刻;tolua/quick-class 节点纹理 retain 怪癖下清除=悬空纹理。
--      改 no-op,内存暂由模拟器扛) =====
local __wjjh_purges = 0

-- ===== 协程压力探针已撤(gh68-70 完成判别使命;其大量分配可能加剧堆损坏) =====

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

-- ===== C/D 桩工厂 =====
local __noop = function() return nil end
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
for _, n in ipairs({'ExtRichText','ExtRichTextScroll','ExtPageView','YXShaderSprite','YXMotionStreak','YXEaseAction','YXHelper','encrypt','LogManager'}) do
  __stubClass(n)
end
-- gh168: 链式桩的"万能返回体"——此前 __chain 全局从未定义，链式桩方法全返回 nil，
-- 游戏里 `X:getInstance():removeAnimCache(...)` 这类写法就崩（gh165 实测 MainLayer.lua:123
-- 的 cannot-resume-dead-coroutine 连锁错即出自 YXSkeletonAnimationCache:getInstance()=nil）。
-- 定义成"自返回"的宽松表：任何方法调用返回它自己（真值），任何字段访问返回函数。
__chain = setmetatable({}, {__index = function(t, k)
  local f = function() return t end
  rawset(t, k, f)
  return f
end})
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
for _, n in ipairs({'YXSkeletonAnimationCache'}) do
  __stubClassChain(n)
end

-- ===== 骨骼桩 v2(真 cc.Node+peer 骨骼方法)=====
-- gh165: peer 方法集扩全——YXSkeletonAnimationEx.lua 会调用一批 YX 专有方法
-- （setSlotColor/setAttachment/setStartTime/setEndTime/resetAnimState/setSpeedScale/
--   setSlotsToSetupPose/setBonesToSetupPose/setBackwards）。
-- ★真 spine 节点只挂"YX 专有补充"（extras），绝不遮 setAnimation/setTimeScale 等原生方法；
--   哑节点才挂全套（全套=原生名+YX 名都变 no-op）。
local __wjjh_skelPeerExtra = function()
  local events = { { name = 'Hurt', stringValue = 'chest', time = 0, floatValue = 0, intValue = 0 } }
  return {
    getAnimEvents = function(self, animName) return events end,
    getAnimDuration = function(self, animName) return 0.1 end,
    getBoneSetupPosePosition = function(self, a, b) return { x = 0, y = 0 } end,
    setSlotsToSetupPose = function(self) end,
    setBonesToSetupPose = function(self) end,
    setSlotColor = function(self, slot, color) end,
    setAttachment = function(self, slot, name) end,
    resetAnimState = function(self, ...) end,
    setBackwards = function(self, b) end,
    setStartTime = function(self, t) end,
    setEndTime = function(self, t) end,
  }
end
local __wjjh_skelPeerFull = function()
  local peer = __wjjh_skelPeerExtra()
  peer.setAnimation = function(self, a, n, l) end
  peer.addAnimation = function(self, ...) end
  peer.setTrackTime = function(self, t) end
  peer.setTimeScale = function(self, s) end
  peer.setSpeedScale = function(self, s) end
  peer.setCompleteListener = function(self, cb) end
  peer.setEventCallback = function(self, cb) end
  peer.registerScriptHandler = function(self, cb) end
  peer.updateWorldTransform = function(self) end
  peer.setToSetupPose = function(self) end
  return peer
end
local __mkSkel = function(...)
  local sk = cc.Node:create()
  tolua.setpeer(sk, __wjjh_skelPeerFull())
  return sk
end
-- ===== gh164/166: 骨骼工厂（哑节点版）=====
-- YXSkeletonAnimation/spine38 是安卓魔改引擎的 C++ 全局，官方 3.15.1 没有。
-- 旧链式桩的 createWithFile 恒返回 nil（__chain 未定义）——资产补全后流程走到
-- HeadView:__initEffectAnimView → Resource:getSkAnim 就断言"动画初始化出错"。
-- gh164 曾试"真实现优先"用 cocos 自带 sp.SkeletonAnimation：**实测崩**
-- （spAtlas_create→spAtlas_dispose，因为游戏 .atlas/.skel 是 spine 3.8 格式，
--   官方 3.15.1 的运行时是 3.5/3.6，解析不了）→ gh166 回到哑节点。
-- 真 3.8 渲染留作后续项（需要 3.8 运行时）。
local __wjjh_mkSpine = function(skel, atlas, scale)
  return __mkSkel()
end
local __wjjhYXSkel = {}
__wjjhYXSkel.createWithFile = function(self, skel, atlas, scale) return __wjjh_mkSpine(skel, atlas, scale) end
__wjjhYXSkel.createWithBinaryFile = function(self, skel, atlas, scale) return __wjjh_mkSpine(skel, atlas, scale) end
__wjjhYXSkel.create = function(self, skel, atlas, scale) return __wjjh_mkSpine(skel, atlas, scale) end
setmetatable(__wjjhYXSkel, {__index = function(tt, k)
  local f = function() return __mkSkel() end
  rawset(tt, k, f)
  return f
end})
_G['YXSkeletonAnimation'] = __wjjhYXSkel

spine38 = setmetatable({}, {__index = function(t, k)
  if k == 'NewSkeletonAnimation' then
    return { createWithBinaryFile = __wjjh_mkSpine, createWithFile = __wjjh_mkSpine, create = __wjjh_mkSpine }
  end
  return nil
end})
__wjjhlog('WJJH_BOOT: C/D stubs + skeleton factory v2 (real-spine-first) installed')

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

-- ===== ExtRichTextScroll 真实现 =====
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

-- ===== XMLHttpRequest(NSURLSession 原生实现)=====
if cc then
  cc.XMLHttpRequest = __wjjh_xhr_class()
  cc.XMLHTTPREQUEST_RESPONSE_STRING = 0
  cc.XMLHTTPREQUEST_RESPONSE_JSON = 1
  cc.XMLHTTPREQUEST_RESPONSE_ARRAY_BUFFER = 2
  __wjjhlog('WJJH_BOOT: native XMLHttpRequest (NSURLSession) installed')
end

-- ===== JM 兜底桩 =====
if not JM then
  local jm = {}
  jm.stringDecrypt = function(self, s) return s end
  jm.stringEncrypt = function(self, s, v) return s end
  jm.getKey = function(self) return '' end
  jm.isEncrypted = function(self, s) return false end
  setmetatable(jm, {__index = function(t, k) local f = function() return false end; rawset(t, k, f); return f end})
  JM = jm
  __wjjhlog('WJJH_BOOT: JM stub installed')
end

-- ===== print 重定向 =====
local _origprint = print
print = function(...)
  local n = select('#', ...)
  local parts = {}
  for i = 1, n do parts[i] = tostring(select(i, ...)) end
  __wjjhlog(table.concat(parts, '\t'))
  _origprint(...)
end

-- ===== LoadingLayer.update 守卫 + 84% 卡死探针(03 会话) =====
local __llSelf, __llUpdates, __llBlocked, __llPass = nil, 0, 0, 0
local __modSeen = {}
-- token 之后的流程里程碑(get_token 应答 -> ControllLayer -> User/BiWu/Task -> MenuLayer -> TitleUI -> StartGame)
-- TitleUI.update 热路径只计数;SIGSEGV 前最后一条 WJJH_FLOW 日志=崩溃点
local __wjjh_flowSpecs = {
  ['app.views.layer.ControllLayer'] = { { 'getLayer' }, { 'pushLayer' }, { 'showLayer' }, { 'popLayer' }, { 'replaceLayer' }, { 'collectGarbageStep' }, { '__updateRoleAttr', 'hot' } },
  ['app.models.user.User'] = { { 'init' } },
  ['app.models.task.Task'] = { { 'init' } },
  ['app.models.BiWu.BiWu'] = { { 'initfightAllData' }, { 'initfightWeekAllData' } },
  ['app.models.game.GameStart'] = { { 'start_game' } },
  ['app.views.ui.TitleUI'] = { { 'init' }, { 'create' }, { 'update', 'hot' }, { 'setTextTitle', 'every' }, { 'setMainLayerActivityInfo', 'every' }, { 'setMailBoxHongdian', 'every' } },
  ['app.controllers.Audio'] = { { 'playMusic' }, { 'playEffect' }, { 'playBackgroundMusic' }, { 'stopMusic' } },
  ['app.views.layer.MainLayer'] = { { 'init' }, { 'create' }, { 'update', 'hot' }, { 'showLayer' }, { 'onShow' }, { 'onAwake' }, { 'initCheck' }, { 'isShow' }, { 'checkIsHaveOfficial' }, { 'checkIsFamilyPrestige' }, { 'checkCanOpenDengLuJiangLi' }, { 'checkCanOpenVisitTask' } },
  ['app.views.layer.MenuLayer.MenuLayer'] = { { 'init' }, { 'create' }, { 'StartGame' }, { 'onAwake' } },
  ['third.coroutine.CoroutinePool'] = { { 'update', 'hot' }, { 'doUpdate', 'hot' }, { 'add' }, { 'addAsync' } },
  ['third.coroutine.Coroutine'] = { { 'resume', 'hot' }, { 'init' } },
  ['third.coroutine.CoroutineStack'] = { { 'resume', 'hot' }, { 'push' } },
  ['third.async.AsyncFunction'] = { { 'create' } },
  ['app.extends.LifeCycleSupport'] = { { 'awake' }, { 'onAwake' }, { 'register' } },
  ['app.models.MonitorPool.MonitorPool'] = { { 'update', 'hot' } },
  ['app.views.ui.MainUI'] = { { 'init' }, { 'create' }, { 'updataSkinAnim', 'hot' } },
  ['app.views.layer.PopLayer.WaitingLayer'] = { { 'createInRunningScene' }, { 'hideAndStopAction' }, { 'hideAndRemoveSelf' } },
  ['third.http.Request'] = { { 'send' } },
}
local function __ts(v)
  if v == nil then return 'nil' end
  return tostring(v)
end

local function __wjjh_instrument(name, M)
  if M.__wjjh_probe then return end
  M.__wjjh_probe = true
  __wjjhlog('WJJH_MOD: ' .. name .. ' loaded, instrumenting')
  if name == 'app.views.layer.MenuLayer.MenuLayer' then
    -- gh154/155: 抓 MenuLayer 实例，供 CI 自动点按（勾协议 + 点「开始游戏」）。
    -- ★必须放在 flowSpec 分支之前——该模块在上表里，走 flowSpec 会 return 掉。
    local oc = M.create
    if type(oc) == 'function' then
      M.create = function(self, ...)
        local o = oc(self, ...)
        __WJJH_MENU_LAYER = o
        __wjjhlog('WJJH_AUTOSTART: MenuLayer captured')
        return o
      end
    end
  end
  if __wjjh_flowSpecs[name] then
    -- 流程里程碑探针:enter 日志,SIGSEGV 前最后一条即崩溃点
    -- ★yield-safe 铁律:被包装函数可能内部 coroutine.yield(CoroutineStack:push 就会),
    --   禁止 pcall/表构造器/unpack 捕获(LuaJIT 跨 C 边界 yield=帧损坏->SIGSEGV,gh63 实测)
    local hotCounters = {}
    for i = 1, #__wjjh_flowSpecs[name] do
      local spec = __wjjh_flowSpecs[name][i]
      local mn = spec[1]
      local mode = spec[2]
      local of = M[mn]
      if type(of) == 'function' then
        if mode == 'hot' or mode == 'every' then
          local cnt = 0
          M[mn] = function(self, ...)
            cnt = cnt + 1
            if mode == 'every' or cnt % 5 == 1 then
              __wjjhlog('WJJH_FLOW: ' .. name .. '.' .. mn .. ' #' .. cnt)
            end
            return of(self, ...)
          end
        else
          M[mn] = function(self, ...)
            __wjjhlog('WJJH_FLOW: ' .. name .. '.' .. mn .. ' enter')
            return of(self, ...)
          end
        end
      end
    end
    return
  end
  if name == 'app.Helper' then
    -- ★核心修复(gh59 定案):getChildByTag 不可靠 -> classDefNodeGetInstance 的 tag 单例
    --   会重复 create(LoadingLayer 双实例 84% 冻结的根因)。改成 Lua 侧真单例。
    local ocd = M.classDefNodeGetInstance
    if type(ocd) == 'function' then
      M.classDefNodeGetInstance = function(self, _class)
        ocd(self, _class)
        local cache = nil
        _class.getInstance = function(sl)
          if cache ~= nil then return cache end
          local runningScene = cc.Director:getInstance():getRunningScene()
          if runningScene then
            local Node = _class:create()
            runningScene:addChild(Node)
            pcall(function() Node:setGlobalZOrder(1) end)
            pcall(function() Node:maxZ() end)
            cache = Node
            return Node
          end
          return nil
        end
        _class.destroyInstance = function(sl)
          if cache ~= nil then
            cache:removeFromParent()
            cache = nil
          end
        end
      end
      __wjjhlog('WJJH_BOOT: Helper singleton patch installed')
    end
  elseif name == 'app.views.layer.LoadingLayer' then
    local oup = M.update
    if type(oup) == 'function' then
      M.update = function(self, ft)
        __llUpdates = __llUpdates + 1
        if __llSelf == nil then __llSelf = self end
        if self._loadingIndex == nil or self._totalCount == nil then
          __llBlocked = __llBlocked + 1
          return
        end
        __llPass = __llPass + 1
        return oup(self, ft)
      end
    end
    local oshow = M.show
    if type(oshow) == 'function' then
      M.show = function(self, funcTab, func, precentList)
        local n1 = '?'
        if type(funcTab) == 'table' then n1 = tostring(#funcTab) end
        __wjjhlog('WJJH_LL: show enter self=' .. tostring(self) .. ' funcTab=' .. type(funcTab) .. ' n=' .. n1 .. ' pct=' .. type(precentList))
        return oshow(self, funcTab, func, precentList)
      end
    end
    local ostc = M.setTotalCount
    if type(ostc) == 'function' then
      M.setTotalCount = function(self, count)
        __wjjhlog('WJJH_LL: setTotalCount self=' .. tostring(self) .. ' count=' .. __ts(count))
        return ostc(self, count)
      end
    end
    local ogeti = M.getInstance
    if type(ogeti) == 'function' then
      M.getInstance = function(self)
        local inst = ogeti(self)
        __wjjhlog('WJJH_LL: getInstance -> ' .. tostring(inst))
        return inst
      end
    end
    local oinit = M.init
    if type(oinit) == 'function' then
      M.init = function(self)
        __wjjhlog('WJJH_LL: init enter self=' .. tostring(self))
        return oinit(self)
      end
    end
  elseif name == 'app.models.loader.Loader' then
    local olc = M.loadCustom
    if type(olc) == 'function' then
      M.loadCustom = function(self, func)
        __wjjhlog('WJJH_MOD: loadCustom enter')
        return olc(self, func)
      end
    end
  elseif name == 'app.models.game.Game' then
    local old = M.load
    if type(old) == 'function' then
      M.load = function(self, func)
        __wjjhlog('WJJH_MOD: Game.load enter')
        return old(self, func)
      end
    end
    local ogi = M.init
    if type(ogi) == 'function' then
      M.init = function(self, func)
        __wjjhlog('WJJH_MOD: Game.init enter')
        return ogi(self, func)
      end
    end
    local osll = M.setLogicLoop
    if type(osll) == 'function' then
      M.setLogicLoop = function(self, func)
        __wjjhlog('WJJH_MOD: Game.setLogicLoop installed')
        local __n = 0
        local __orig = func
        local __wrapped = function(ft)
          __n = __n + 1
          if __n % 120 == 1 then
            __wjjhlog('WJJH_LOOP: logic #' .. __n)
          end
          return __orig(ft)
        end
        return osll(self, __wrapped)
      end
    end
    local osrl = M.setRenderLoop
    if type(osrl) == 'function' then
      M.setRenderLoop = function(self, func)
        __wjjhlog('WJJH_MOD: Game.setRenderLoop installed')
        local __n = 0
        local __orig = func
        local __wrapped = function(ft)
          __n = __n + 1
          if __n % 120 == 1 then
            __wjjhlog('WJJH_LOOP: render #' .. __n)
          end
          return __orig(ft)
        end
        return osrl(self, __wrapped)
      end
    end
    local ofms = M.freeMemorySmart
    if type(ofms) == 'function' then
      M.freeMemorySmart = function(self)
        __wjjh_purges = __wjjh_purges + 1
        if __wjjh_purges % 10 == 1 then
          __wjjhlog('WJJH_PURGE: freeMemorySmart suppressed #' .. __wjjh_purges)
        end
        return
      end
    end
  elseif name == 'app.extends.Http.HttpManager' then
    local names = { 'getToken', 'getTime', 'getWebConfig' }
    for i = 1, #names do
      local nm = names[i]
      local of = M[nm]
      if type(of) == 'function' then
        M[nm] = function(...)
          __wjjhlog('WJJH_HTTP: ' .. nm .. ' called')
          return of(...)
        end
      end
    end
  end
end

local __wjjh_targets = {
  ['app.Helper'] = true,
  ['app.views.layer.LoadingLayer'] = true,
  ['app.models.loader.Loader'] = true,
  ['app.models.game.Game'] = true,
  ['app.extends.Http.HttpManager'] = true,
  ['app.views.layer.ControllLayer'] = true,
  ['app.models.user.User'] = true,
  ['app.models.task.Task'] = true,
  ['app.models.BiWu.BiWu'] = true,
  ['app.models.game.GameStart'] = true,
  ['app.views.ui.TitleUI'] = true,
  ['app.controllers.Audio'] = true,
  ['app.views.layer.MainLayer'] = true,
  ['app.views.layer.MenuLayer.MenuLayer'] = true,
  ['third.coroutine.CoroutinePool'] = true,
  ['third.coroutine.Coroutine'] = true,
  ['third.coroutine.CoroutineStack'] = true,
  ['third.async.AsyncFunction'] = true,
  ['app.extends.LifeCycleSupport'] = true,
  ['app.models.MonitorPool.MonitorPool'] = true,
  ['app.views.ui.MainUI'] = true,
  ['app.views.layer.PopLayer.WaitingLayer'] = true,
  ['third.http.Request'] = true,
}
local __origRequire = require
require = function(name)
  local m = __origRequire(name)
  if type(m) == 'table' and __wjjh_targets[name] then
    __wjjh_instrument(name, m)
  end
  return m
end

local function __wjjh_dumpState()
  local sc = cc.Director:getInstance():getRunningScene()
  if sc == nil then
    __wjjhlog('WJJH_CEN: scene=nil upd=' .. __llUpdates .. ' blk=' .. __llBlocked .. ' WEB_TIME=' .. __ts(WEB_TIME))
    return
  end
  local kids = sc:getChildren()
  local n = 0
  local tags = {}
  if type(kids) == 'table' then
    for i = 1, #kids do
      n = n + 1
      tags[#tags + 1] = __ts(kids[i]:getTag())
    end
  end
  __wjjhlog('WJJH_CEN: kids=' .. n .. ' tags=[' .. table.concat(tags, ',') .. '] upd=' .. __llUpdates .. ' blk=' .. __llBlocked .. ' pass=' .. __llPass .. ' WEB_TIME=' .. __ts(WEB_TIME))
  if __llSelf ~= nil then
    local s = __llSelf
    local ftt = s.funcTab
    local extra = ''
    if type(ftt) == 'table' then
      local n1 = 'nilG'
      if type(ftt[1]) == 'table' then n1 = tostring(#ftt[1]) end
      extra = ' n1=' .. n1 .. ' nT=' .. tostring(#ftt)
    end
    local barp = 'nilBar'
    if s.LoadingBar ~= nil then barp = __ts(s.LoadingBar:getPercent()) end
    local txt = 'nilTxt'
    if s.Text_zaiRuZhong ~= nil then txt = __ts(s.Text_zaiRuZhong:getString()) end
    __wjjhlog('WJJH_LLst: self=' .. tostring(s) .. ' tag=' .. __ts(s:getTag()) .. ' t=' .. __ts(s._updateTime) .. ' ci=' .. __ts(s.currIndex) .. ' cpi=' .. __ts(s.currParentIndex) .. ' li=' .. __ts(s._loadingIndex) .. ' tc=' .. __ts(s._totalCount) .. ' done=' .. __ts(s.isSuccess) .. ' ft=' .. type(ftt) .. extra .. ' bar=' .. barp .. ' txt=' .. txt)
  end
end

local function __wjjh_onceProbes(sc)
  if sc == nil then
    __wjjhlog('WJJH_ENV: scene nil, skip')
    return
  end
  local fu = cc.FileUtils:getInstance()
  __wjjhlog('WJJH_ENV: font=' .. __ts(fu:isFileExist('Font/default.ttf')) .. ',' .. __ts(fu:isFileExist('res/Font/default.ttf')))
  local txt = ccui.Text:create()
  txt:setString('T')
  txt:setOpacity(0)
  __wjjhlog('WJJH_ENV: opacity=' .. __ts(txt:getOpacity()))
  local nd = cc.Node:create()
  nd:setTag(911001)
  __wjjhlog('WJJH_ENV: tagRound=' .. __ts(nd:getTag() == 911001))
  sc:addChild(nd)
  local kids = sc:getChildren()
  local idHit = 0
  if type(kids) == 'table' then
    for i = 1, #kids do
      if kids[i] == nd then idHit = 1 end
    end
  end
  __wjjhlog('WJJH_ENV: kidIdentity=' .. idHit .. ' nativeByTagType=' .. type(__WJJH_nativeGetChildByTag))
  local gotNative = nil
  if type(__WJJH_nativeGetChildByTag) == 'function' then
    gotNative = __WJJH_nativeGetChildByTag(sc, 911001)
  end
  local got = sc:getChildByTag(911001)
  __wjjhlog('WJJH_ENV: tagTest native=' .. __ts(gotNative == nd) .. ' poly=' .. __ts(got == nd))
  nd:removeFromParent()
  __wjjhlog('WJJH_ENV: Loader=' .. type(package.loaded['app.models.loader.Loader']) .. ' Game=' .. type(package.loaded['app.models.game.Game']) .. ' HttpM=' .. type(HttpManagerEx) .. ' ccexpGame=' .. type(cc.exports and cc.exports.Game or nil))
end

-- ===== gh154: CI 自动点按（仅 CI 诊断构建；真机包 __WJJH_AUTOSTART=false，整条路径不跑）=====
-- CI 上没人手点「点击开始游戏」。分两拍：先勾隐私协议，再触发开始按钮。
-- 阶段计数保证各只触发一次；pcall 只包我们自己的调度回调（主线程 Timer 回调，不在协程内，
-- 不会踩 yield-safe 铁律）。
local __wjjh_autoStage = 0
local __wjjh_autoWait = 0
local function __wjjh_autoStartTick()
  if not __WJJH_AUTOSTART then return end
  local menu = __WJJH_MENU_LAYER
  if menu == nil then return end
  if __wjjh_autoStage == 0 then
    __wjjh_autoStage = 1
    local ok, err = pcall(function()
      if menu.CheckBox_Policy ~= nil and menu.CheckBox_Policy:isSelected() == false then
        menu.CheckBox_Policy:setSelected(true)
        menu.CheckBox_Policy:setBright(true)
      end
    end)
    __wjjhlog('WJJH_AUTOSTART: policy checked ok=' .. tostring(ok) .. ' err=' .. __ts(err))
    return
  end
  if __wjjh_autoStage == 1 then
    __wjjh_autoStage = 2
    local ok, err = pcall(function() menu:Text_Start_releaseFunc() end)
    __wjjhlog('WJJH_AUTOSTART: start fired ok=' .. tostring(ok) .. ' err=' .. __ts(err))
    return
  end
  -- 已点过：等 15s 看开始按钮是否隐藏（=流程已推进）；没推进就重试一次
  if __wjjh_autoStage == 2 then
    __wjjh_autoWait = __wjjh_autoWait + 1
    if __wjjh_autoWait == 30 then
      local ok1, vis = pcall(function()
        return menu.Button_startGame ~= nil and menu.Button_startGame:isVisible()
      end)
      if ok1 and vis == true then
        local ok, err = pcall(function() menu:Text_Start_releaseFunc() end)
        __wjjhlog('WJJH_AUTOSTART: start retry ok=' .. tostring(ok) .. ' err=' .. __ts(err))
      else
        __wjjh_autoStage = 3
        __wjjhlog('WJJH_AUTOSTART: start button hidden -> flow advanced')
      end
    elseif __wjjh_autoWait > 30 and __wjjh_autoWait % 60 == 0 then
      __wjjhlog('WJJH_AUTOSTART: waiting, still on menu (t=' .. __wjjh_autoWait .. ')')
    end
  end
end

-- ===== gh156: CI 自动建号（数据层，等价于点「男」+「武学世家」）=====
-- 走到建号流程（游戏 require 了 app.views.layer.CreateRoleLayer）后，直接调它自己的
-- createRoleAndEntryGame → HTTP createRole → 上传存档 → ControllLayer:startGame() → 主界面。
-- 数值/技能照抄 CreateRoleLayer:show() 的 func1（武学世家）；sexFunc 照抄 Dialog4UI 男分支。
-- 真机包 __WJJH_AUTOSTART=false，整条不跑。
local __wjjh_autoRoleFired = false
local function __wjjh_autoRoleTick()
  if not __WJJH_AUTOSTART or __wjjh_autoRoleFired then return end
  if __wjjh_autoStage < 2 then return end          -- 等「开始游戏」点过
  local M = package.loaded['app.views.layer.CreateRoleLayer']
  if M == nil then return end                      -- 等游戏自己 require（=已到建号流程）
  __wjjh_autoRoleFired = true
  local ok, err = pcall(function()
    local inst = M:getInstance()
    local sexFunc = function()
      User:setRoleAttr('name', '无名小辈')
      User:setRoleAttr('sex', '男')
      User:setRoleAttr('age', 14)
    end
    inst:createRoleAndEntryGame(
      24, 22, 18, 16, 19, 21,
      { jibenquanjiao = { id = 'jibenquanjiao', exp = 121 },
        jibenzhaojia  = { id = 'jibenzhaojia',  exp = 121 },
        jibenqinggong = { id = 'jibenqinggong', exp = 121 },
        jibenneigong  = { id = 'jibenneigong',  exp = 121 } },
      nil, sexFunc)
  end)
  __wjjhlog('WJJH_AUTOSTART: create role fired ok=' .. tostring(ok) .. ' err=' .. __ts(err))
  -- gh168: 再等 ~3s 把建号对话框收掉——正常流程由按钮的 setPanelHide 关闭；
  -- 我们走数据层直调 createRoleAndEntryGame（等价「男」+「武学世家」），得补这一刀，
  -- 否则选性别对话框一直盖在主界面上（gh165 截图实证）。
  local dlgTicks = 0
  local function dlgCloser()
    dlgTicks = dlgTicks + 1
    if dlgTicks < 6 then return end
    return true
  end
  __wjjh_autoRoleTick = function()
    if dlgCloser() then
      local ok2, err2 = pcall(function()
        local D = package.loaded['app.views.layer.DialogLayer.DialogDLayer']
        if D ~= nil then
          local inst = D:getInstance()
          if inst ~= nil and inst.hide ~= nil then inst:hide() end
        end
      end)
      __wjjhlog('WJJH_AUTOSTART: hide create-role dialog ok=' .. tostring(ok2) .. ' err=' .. __ts(err2))
      -- gh169: 主界面探路——再等 ~8s，把「江湖」(SelectMapLayer) 层推出来，
      -- 看选地图/进场景链路需要什么（纯探索，CI 门控；真机包不跑）。
      local navTicks = 0
      __wjjh_autoRoleTick = function()
        navTicks = navTicks + 1
        if navTicks == 16 then
          local ok3, err3 = pcall(function()
            local CL = package.loaded['app.views.layer.ControllLayer']
            if CL ~= nil then
              local cl = CL:getInstance()
              if cl ~= nil and cl.pushLayer ~= nil then
                cl:pushLayer('SelectMapLayer')
              end
            end
          end)
          __wjjhlog('WJJH_AUTOSTART: push SelectMapLayer ok=' .. tostring(ok3) .. ' err=' .. __ts(err3))
          return
        end
        -- gh170: 进关卡——「进入关卡」按钮的处理就是 SelectMapModel:entryMap()（纯数据层）
        if navTicks == 44 then
          local ok4, err4 = pcall(function()
            local M = package.loaded['app.models.map.SelectMapModel']
            if M == nil then M = require('app.models.map.SelectMapModel') end
            if M ~= nil and M.entryMap ~= nil then M:entryMap() end
          end)
          __wjjhlog('WJJH_AUTOSTART: entryMap fired ok=' .. tostring(ok4) .. ' err=' .. __ts(err4))
          return
        end
        -- gh172/173: 进图后转储房间出口图 + 自动走一步（房间 UI 出口按钮就是
        -- MapLayer:entryRoom(fromRoomId, toRoomId, direction)；entryRoomByDirection 已被上游禁用）。
        -- ★tick 提前到 70（=entryMap 后 ~13s）：CI 取证窗口约 60-90s，130 太晚（gh172 实测被截）
        if navTicks == 70 then
          local ok5, err5 = pcall(function()
            local CL = package.loaded['app.views.layer.ControllLayer']
            local layer = CL and CL:getInstance() and CL:getInstance():getLayer('MapLayer')
            if layer == nil then
              __wjjhlog('WJJH_AUTOSTART: MapLayer nil, skip walk')
              return
            end
            local room = layer._currRoom
            local map = layer._currMap
            if room == nil then
              __wjjhlog('WJJH_AUTOSTART: _currRoom nil, skip walk')
              return
            end
            __wjjhlog('WJJH_AUTOSTART: room id=' .. __ts(room.id) .. ' name=' ..
                      __ts(room.name) .. ' mapId=' .. __ts(map and map.id))
            local firstDir, firstTo = nil, nil
            if type(room.link) == 'table' then
              local names = {}
              for d, rid in pairs(room.link) do
                names[#names + 1] = tostring(d) .. '->' .. tostring(rid) .. '(' ..
                                    __ts(map and map.getRoomNameById and map:getRoomNameById(rid)) .. ')'
                if firstDir == nil then firstDir, firstTo = d, rid end
              end
              __wjjhlog('WJJH_AUTOSTART: exits [' .. table.concat(names, ', ') .. ']')
            end
            if firstTo ~= nil and layer.entryRoom ~= nil then
              layer:entryRoom(room.id, firstTo, firstDir)
              __wjjhlog('WJJH_AUTOSTART: walked room ' .. __ts(room.id) .. ' -> ' .. __ts(firstTo))
            end
          end)
          __wjjhlog('WJJH_AUTOSTART: walk step done ok=' .. tostring(ok5) .. ' err=' .. __ts(err5))
        end
        -- gh176: NPC 交互——把房内角色解析成对象（拿真名），并自动打开观察层
        -- （点 NPC 按钮的处理就是 PopupLayerController:showLayer("RoleObserveLayer", …:showLayer(role,"MAP"))）
        if navTicks == 92 then
          local ok7, err7 = pcall(function()
            local CL = package.loaded['app.views.layer.ControllLayer']
            local layer = CL and CL:getInstance() and CL:getInstance():getLayer('MapLayer')
            if layer == nil or layer._currRoom == nil then
              __wjjhlog('WJJH_NPC: MapLayer/currRoom nil, skip')
              return
            end
            local room = layer._currRoom
            local map = layer._currMap
            local ids = map and map.getRoomRoleList and map:getRoomRoleList(room.id) or {}
            local names = {}
            local target = nil
            for i, rid in ipairs(ids or {}) do
              local role = map.getRole and map:getRole(rid)
              local nm = role and (role.name or (role.getName and role:getName()))
              names[#names + 1] = tostring(rid) .. '=' .. __ts(nm) .. '(' .. __ts(role and role.type) .. ')'
              if target == nil and role ~= nil and role.type == 'role' then target = role end
            end
            __wjjhlog('WJJH_NPC: room ' .. __ts(room.id) .. ' roles [' .. table.concat(names, ', ') .. ']')
            if target ~= nil then
              -- gh177: 路径修正为 app.views.base.PopupLayerController（全局同名优先）；打开观察层
              local PC = PopupLayerController
              if PC == nil then
                local okP, mod = pcall(require, 'app.views.base.PopupLayerController')
                if okP then PC = mod end
              end
              local tn = target.name or (target.getName and target:getName()) or target.id
              -- gh178: 观察层动作表挂钩（打印动作名 + 自动触发挑战类动作）
              local okR, ROL = pcall(require, 'app.views.layer.RoleLayer.RoleObserveLayer')
              if okR and ROL ~= nil and type(ROL.showBtns) == 'function' and not ROL.__wjjh_btns_hooked then
                ROL.__wjjh_btns_hooked = true
                local origShow = ROL.showBtns
                ROL.showBtns = function(self, funcList)
                  local names = {}
                  for i, v in ipairs(funcList or {}) do
                    names[#names + 1] = i .. ':' .. __ts(v.btnName)
                  end
                  __wjjhlog('WJJH_NPC: btns [' .. table.concat(names, ', ') .. ']')
                  if not __wjjh_fightFired then
                    for i, v in ipairs(funcList or {}) do
                      local nm = tostring(v.btnName or '')
                      if nm:find('挑战') or nm:find('切磋') or nm:find('动手') or nm:find('比试')
                         or nm:find('攻击') or nm:find('战斗') then
                        __wjjh_fightFired = true
                        local okF, errF = pcall(v.btnFunc)
                        __wjjhlog('WJJH_NPC: FIRED action [' .. nm .. '] ok=' ..
                                  tostring(okF) .. ' err=' .. __ts(errF))
                        break
                      end
                    end
                  end
                  return origShow(self, funcList)
                end
                __wjjhlog('WJJH_NPC: showBtns hooked')
              end
              if PC ~= nil and PC.showLayer ~= nil then
                PC:showLayer('RoleObserveLayer', function(obs)
                  obs:showLayer(target, 'MAP')
                  -- 打印观察层功能按钮（挑战/对话等入口就在这里，供下一轮自动点）
                  local kids = nil
                  if obs ~= nil and obs.ListView_FuncBtns ~= nil and obs.ListView_FuncBtns.getChildren ~= nil then
                    kids = obs.ListView_FuncBtns:getChildren()
                  end
                  local btns = {}
                  if type(kids) == 'table' then
                    for i = 1, #kids do
                      local ch = kids[i]
                      local label = nil
                      if ch ~= nil then
                        for _, fld in ipairs({ 'Text_name', 'Text_buttonName', 'Text_title', 'Text' }) do
                          local t = ch[fld]
                          if t ~= nil and t.getString ~= nil then
                            label = t:getString()
                            break
                          end
                        end
                      end
                      btns[#btns + 1] = __ts(ch and ch.getName and ch:getName()) .. '=' .. __ts(label)
                    end
                  end
                  __wjjhlog('WJJH_NPC: observe btns [' .. table.concat(btns, ', ') .. ']')
                end)
                __wjjhlog('WJJH_NPC: RoleObserveLayer opened for ' .. __ts(tn))
              else
                __wjjhlog('WJJH_NPC: PopupLayerController unavailable')
              end
            end
          end)
          __wjjhlog('WJJH_NPC: done ok=' .. tostring(ok7) .. ' err=' .. __ts(err7))
          __wjjh_autoRoleTick = function() end
        end
        -- gh174: 连走多间房（避开回访，每 4 tick 一步）——把整关房间图摸出来，
        -- 每步记录 房间名 + 出口 + 房内 NPC；gh175 起只走进 enterable 的房间。
        if navTicks > 70 and navTicks <= 86 and (navTicks - 70) % 4 == 0 then
          local ok6, err6 = pcall(function()
            local CL = package.loaded['app.views.layer.ControllLayer']
            local layer = CL and CL:getInstance() and CL:getInstance():getLayer('MapLayer')
            if layer == nil or layer._currRoom == nil then return end
            local room = layer._currRoom
            local map = layer._currMap
            __walkVisited = __walkVisited or {}
            __walkVisited[room.id] = true
            local roles = {}
            if map ~= nil and map.getRoomRoleList ~= nil then
              local rl = map:getRoomRoleList(room.id)
              if type(rl) == 'table' then
                for _, r in pairs(rl) do
                  local nm = r
                  if type(r) == 'table' then nm = r.name or r.roleName or (r.getRole and r:getRole() and r:getRole().name) end
                  roles[#roles + 1] = __ts(nm)
                end
              end
            end
            __wjjhlog('WJJH_WALK: at ' .. __ts(room.id) .. ' [' .. __ts(room.name) .. '] npc=[' ..
                      table.concat(roles, ', ') .. ']')
            local lastDir, lastTo = nil, nil
            local skipped = {}
            if type(room.link) == 'table' then
              for d, rid in pairs(room.link) do
                if __walkVisited[rid] == nil then
                  -- gh175: 只挑可进入的房间（enterable ~= 1 会被 checkCanEnterNewMapRoom 拦下）
                  local attr = nil
                  if map ~= nil and map.getRoomAttr ~= nil then attr = map:getRoomAttr(rid) end
                  local ent = attr and attr.enterable
                  if ent == 1 or ent == true or ent == nil then
                    lastDir, lastTo = d, rid; break
                  else
                    skipped[#skipped + 1] = tostring(rid) .. '(enterable=' .. __ts(ent) .. ')'
                  end
                end
              end
            end
            if #skipped > 0 then
              __wjjhlog('WJJH_WALK: locked exits: ' .. table.concat(skipped, ', '))
            end
            if lastTo == nil then
              __wjjhlog('WJJH_WALK: no open unvisited exit at ' .. __ts(room.id) .. ' -> stop')
              return
            end
            layer:entryRoom(room.id, lastTo, lastDir)
            __wjjhlog('WJJH_WALK: move ' .. __ts(room.id) .. ' -> ' .. __ts(lastTo))
          end)
          if not ok6 then
            __wjjhlog('WJJH_WALK: step err=' .. __ts(err6))
          end
        end
      end
    end
  end
end

local function __wjjh_tryHook()
  __wjjh_pollN = (__wjjh_pollN or 0) + 1
  for name, _ in pairs(__wjjh_targets) do
    local M = package.loaded[name]
    if type(M) == 'table' then __wjjh_instrument(name, M) end
  end
  if __wjjh_pollN == 8 then
    local ok, e = pcall(__wjjh_onceProbes, cc.Director:getInstance():getRunningScene())
    if not ok then __wjjhlog('WJJH_ENV err=' .. __ts(e)) end
  end
  if __wjjh_pollN % 4 == 0 and __wjjh_pollN <= 240 then
    local ok, e = pcall(__wjjh_dumpState)
    if not ok then __wjjhlog('WJJH_CEN err=' .. __ts(e)) end
  end
  __wjjh_autoStartTick()
  __wjjh_autoRoleTick()
end
cc.Director:getInstance():getScheduler():scheduleScriptFunc(__wjjh_tryHook, 0.5, false)

-- ===== gh150: WebSocket 绑定已被排除（websockets 库不链接）=====
-- cocos/init.lua -> DeprecatedNetworkClass 把 cc.WebSocket 拷成全局 WebSocket，
-- 为 nil 时 DeprecatedNetworkFunc 紧接着 `WebSocket.sendTextMsg = ...` 直接抛错，
-- main.lua 引导链中止 -> 无场景黑屏（gh149 实证）。给哑类占位：
-- create 返回 nil（私服走 HTTP；在线对战才有真 ws 需求，届时用 NSURLSessionWebSocketTask 原生实现）。
if cc ~= nil and cc.WebSocket == nil then
  cc.WEBSOCKET_OPEN, cc.WEBSOCKET_MESSAGE, cc.WEBSOCKET_CLOSE, cc.WEBSOCKET_ERROR = 0, 1, 2, 3
  cc.WebSocket = { create = function(...) return nil end }
  __wjjhlog('WJJH_BOOT: cc.WebSocket stub installed (binding excluded)')
end

-- ===== gh171: 安卓魔改引擎的 ccui 专有方法补丁 =====
-- 官方 3.15.1 的 ccui.Button 没有 setMoveTouchCancelEnable（安卓定制引擎加的，
-- 语义=滑动即取消点击）。TotalMapUI.lua:150 在造地图按钮时调它 → 地图 setMap 崩。
-- 按需补 no-op shim（返回 self 便于链式）；后续遇到同类再往这里加。
if ccui ~= nil then
  local __wjjh_ccui_shims = {
    setMoveTouchCancelEnable = function(self, enable) return self end,
  }
  local __wjjh_patched = 0
  for _, cls in ipairs({ ccui.Button, ccui.Widget, ccui.Layout, ccui.ScrollView, cc.Node }) do
    if cls ~= nil then
      for k, f in pairs(__wjjh_ccui_shims) do
        if cls[k] == nil then
          cls[k] = f
          __wjjh_patched = __wjjh_patched + 1
        end
      end
    end
  end
  __wjjhlog('WJJH_BOOT: ccui custom-method shims installed (' .. __wjjh_patched .. ')')
end

-- ===== main.lua =====
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
