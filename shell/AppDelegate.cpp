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

-- ===== getChildByTag 纯 Lua 替代(gh59 实测 tagTest=false,原生查找不可靠) =====
if cc and cc.Node then
  __WJJH_nativeGetChildByTag = cc.Node.getChildByTag
  cc.Node.getChildByTag = function(self, tag)
    local kids = self:getChildren()
    if type(kids) == 'table' then
      for i = 1, #kids do
        local k = kids[i]
        if k and type(k.getTag) == 'function' and k:getTag() == tag then return k end
      end
    end
    return nil
  end
  __wjjhlog('WJJH_BOOT: getChildByTag polyfill installed')
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

-- ===== 协程压力测试(gh68:判别 Rosetta 下 LuaJIT 高频协程切换是否为 SIGSEGV 根因) =====
do
  local __co = coroutine.create(function()
    for __i = 1, 100000 do coroutine.yield(__i) end
  end)
  local __ok, __n = true, 0
  for __i = 1, 100000 do
    __ok, __n = coroutine.resume(__co)
    if not __ok then break end
  end
  __wjjhlog('WJJH_STRESS: boot 100k switches ok=' .. tostring(__ok) .. ' last=' .. tostring(__n))
end
local __frameCo = coroutine.create(function()
  while true do coroutine.yield() end
end)
local __frameN = 0
cc.Director:getInstance():getScheduler():scheduleScriptFunc(function()
  __frameN = __frameN + 1
  for __i = 1, 200 do coroutine.resume(__frameCo) end
  if __frameN % 60 == 1 then
    __wjjhlog('WJJH_STRESS: frame ' .. __frameN .. ' x200 ok mem=' .. tostring(math.floor(collectgarbage('count'))))
  end
end, 0, false)
__wjjhlog('WJJH_STRESS: frame stress scheduled')

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

-- ===== 骨骼桩 v2(真 cc.Node+peer 骨骼方法)=====
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
end
cc.Director:getInstance():getScheduler():scheduleScriptFunc(__wjjh_tryHook, 0.5, false)

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
