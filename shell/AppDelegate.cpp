#include "AppDelegate.h"
#include "scripting/lua-bindings/manual/CCLuaEngine.h"
#include "scripting/lua-bindings/manual/tolua_fix.h"
#include "scripting/lua-bindings/manual/lua_module_register.h"
#include "cocos2d.h"
#include "WjjhJM.h"
#include "WjjhXHR.h"
#include "WJJHSpine38.h"
#include <os/log.h>
#include <sys/time.h>
#include <sys/sysctl.h>
#include <unistd.h>
#include <mach/mach.h>

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

    // ---- gh240: 低内存设备纹理降级（解决真机载入 96% 被 Jetsam 杀）----
    // JetsamEvent 实证（JetsamEvent-2026-10-15-132645.ips）：
    //   name=WJJH, rpages=92800 (pageSize 16KB) = 1450MB, reason=per-process-limit
    //   （2GB 内存的 A9 iPad，iOS 16 单进程上限约 1.4GB —— 正好撞线）
    //   physicalPages.internal 峰值 57286 页 = 895MB，即纹理解压后的匿名内存。
    // 资源 374MB PNG（2906 张），PNG 解压成 RGBA8888(4B/px) 后约 2GB 量级。
    // 把每像素降到 2B（RGBA4444）⇒ 纹理内存减半 ⇒ 1450MB → ~1100MB，安全通过。
    // 仅在 ≤3GB 设备上降级；4GB+ 设备保持默认 RGBA8888，画质不受影响。
    {
        uint64_t memsize = 0;
        size_t memsz = sizeof(memsize);
        if (sysctlbyname("hw.memsize", &memsize, &memsz, nullptr, 0) == 0) {
            char membuf[160];
            snprintf(membuf, sizeof(membuf),
                     "WJJH_BOOT: hw.memsize = %llu MB",
                     (unsigned long long)(memsize / (1024ULL * 1024ULL)));
            wjjh_bootlog(membuf);
            if (memsize > 0 && memsize <= 3ULL * 1024ULL * 1024ULL * 1024ULL) {
                Texture2D::setDefaultAlphaPixelFormat(Texture2D::PixelFormat::RGBA4444);
                wjjh_bootlog("WJJH_BOOT: low-memory device -> texture format RGBA4444 (texture RAM halved)");
            } else {
                wjjh_bootlog("WJJH_BOOT: ample memory -> keep default texture format RGBA8888");
            }
        } else {
            wjjh_bootlog("WJJH_BOOT: hw.memsize unavailable -> keep default texture format");
        }
    }

    // ---- gh243: 纹理内存探针（PVRTC 改造的量化验证手段）----
    // 前面几轮反复"猜"内存去向（先后误判过 spine 桥 / 存档 / 覆盖度），
    // 所以这次直接把 TextureCache 的真实占用定期写进 launch.log：
    // 形如 `WJJH_MEM: #7 textures=[1234 textures, 56789012 bytes]`。
    // 有了它就能直接看到 PVRTC 前后的纹理内存差，不必再从 JetsamEvent 反推。
    {
        auto* sched = Director::getInstance()->getScheduler();
        if (sched) {
            sched->schedule([](float) {
                static int n = 0;
                ++n;
                auto* tc = Director::getInstance()->getTextureCache();
                std::string info = tc ? tc->getCachedTextureInfo() : std::string("(no cache)");
                fprintf(stderr, "WJJH_MEM: #%d textures=[%s]\n", n, info.c_str());
                // gh261: phys_footprint = Jetsam highwater 的同口径读数。gh260 实测
                //   一场切磋推到 1.38GB 被杀；有了这条曲线就能看到每一时刻离
                //   1.4GB 上限多远、是哪一步跳的（纹理/骨架/文字各占多少一目了然）。
                task_vm_info_data_t tvi;
                mach_msg_type_number_t tcnt = TASK_VM_INFO_COUNT;
                if (task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&tvi, &tcnt) == KERN_SUCCESS) {
                    fprintf(stderr, "WJJH_FOOT: #%d phys_footprint=%.0fMB\n",
                            n, (double)tvi.phys_footprint / 1048576.0);
                }
            }, this, 20.0f, false, "wjjh_mem_probe");
            wjjh_bootlog("WJJH_MEM: texture-cache probe scheduled (every 20s)");
        }
    }

    // 游戏资源在 bundle 的 Resources/ 下：src/ 与 res/ 由 main.lua 自己 addSearchPath
    auto engine = LuaEngine::getInstance();
    ScriptEngineManager::getInstance()->setScriptEngine(engine);

    // 官方模板同款：denshion/network/ui/studio/spine/audioengine 等模块在此注册，
    // 漏了会报 "SimpleAudioEngine is nil"
    lua_State* L = engine->getLuaStack()->getLuaState();
    lua_module_register(L);

    // gh217/A3: 注册 spine 3.8.75 真运行时桥（wjjh_spine38_*），必须在 Lua boot chunk 之前
    wjjh_spine38_register(L);
    wjjh_bootlog("WJJH_BOOT: spine38 real bridge registered");

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
    // gh265/gh271: 真机构建标志。★gh271 修正：CMakeLists 从未把 WJJH_DEVICE 定义成
    //   编译宏（它只是 configure 期变量）⇒ #if defined(WJJH_DEVICE) 恒假，
    //   frame-drive 在真机照跑=24 倍速乱打（gh270 实测）。
    //   改用 CI_DIAG 反义：CI_DIAG 只在模拟器 CI 构建定义（gh258 已验证该机制可靠）。
#if defined(WJJH_CI_DIAG)
    lua_pushboolean(L, 0);
#else
    lua_pushboolean(L, 1);
#endif
    lua_setglobal(L, "__WJJH_DEVICE");
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

-- ===== gh272: 死节点高危方法守卫（治「玩着玩着 SIGSEGV」）=====
-- 实测（老板 gh271，WJJH-2026-10-18-065421.ips）：打开 DecorativeBoxLayer 后下一帧，
--   某 Lua 定时回调对已释放节点调 getParent → lua_cocos2dx_Node_getParent 直接解引用
--   已释放内存 = SIGSEGV（原生层，Lua isnull 预筛只能自己人用，裸方法拦不住）。
--   iOS 上 UI 树混入已释放节点是常态（gh255 起多方实锤），Node 析构会把 tolua 注册
--   摘除 ⇒ tolua.isnull 能识别 ⇒ 把最常被裸调的 8 个方法包上 isnull 守卫：
--   死节点返回 nil/安全值（调用链下游若 nil 解引用，会被游戏既有的 xpcall 捕获成
--   Lua 错误，比 SIGSEGV 全局闪退好一个量级）。
--   开销：LuaJIT 方法转发 ~0.1μs/次，高频路径可忽略。与 gh153 getTag 守卫同模式。
if cc and cc.Node then
  local __isnull = (tolua and tolua.isnull) or nil
  -- gh275: 收窄到 getParent（.ips 实锤元凶）。gh272 的 8 方法版疑似与 gh273 的
  --   「背包不能点」有耦合（无日志无法定分水岭，先最小化行为面）。
  local __guarded = {
    getParent = function(self) return nil end,
  }
  for __name, __fallback in pairs(__guarded) do
    local __orig = rawget(cc.Node, __name) or cc.Node[__name]
    if type(__orig) == 'function' then
      cc.Node[__name] = function(self, ...)
        if __isnull and type(self) == 'userdata' and __isnull(self) then
          -- gh274: 命中守卫必须留痕——"网络错误请重试"这类软错误（老板 gh272 实测
          --   金先生）很可能就是死节点被防护网接住后的下游表现，日志点名后可精修。
          __wjjh_gi = (__wjjh_gi or 0) + 1
          if __wjjh_gi <= 60 then
            __wjjhlog('[WJJH_GUARD] ' .. __name .. ' on dead node #' .. __wjjh_gi
              .. ' self=' .. tostring(self) .. '\n'
              .. debug.traceback('[WJJH_GUARD-STACK]', 2))
          end
          return __fallback(self)
        end
        return __orig(self, ...)
      end
    end
  end
  __wjjhlog('WJJH_BOOT: dead-node method guards installed (8 methods)')
end


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
-- gh168/181: 链式桩的"万能返回体"——此前 __chain 未定义，链式桩方法全返回 nil，
-- 游戏里 `X:getInstance():removeAnimCache(...)` 这类写法就崩（MainLayer.lua:123）。
-- gh181: 实测"字段访问返回函数"仍会崩（LifeCycleSupport.lua:35 把字段当表索引：
-- `_lifeCycleEventSubject` 是 function 值 → attempt to index（发生在切磋战斗收尾
-- FightLayer:startMapFight）。现在每个字段/方法都返回**自返回的宽松表**：
-- 既能继续索引、又能被调用、又恒为真值。
local __wjjh_chain_mt
__wjjh_chain_mt = {
  __index = function(t, k)
    local v = setmetatable({}, __wjjh_chain_mt)
    rawset(t, k, v)
    return v
  end,
  __call = function(self, ...) return self end,
}
__chain = setmetatable({}, __wjjh_chain_mt)
-- gh183: 关键平衡——游戏里同一字段可能被当"函数"（type 检查 ✓）也可能被"当表索引"
-- （LifeCycleSupport:35 实测）。Lua 5.1 函数类型共享一张元表：给它加 __index，
-- 于是"函数被索引"不再报错、而是拿到宽松表；同时 type(x) 仍是 function ✓。
if debug and debug.setmetatable then
  local okFm = pcall(debug.setmetatable, function() end, {
    __index = function(f, k) return __chain end,
  })
  __wjjhlog('WJJH_BOOT: function-metatable indexable=' .. tostring(okFm))
end
-- gh191: 桩 peer 的"结构方法"白名单——LifeCycleSupport.resume 对树上每个节点
-- pairs(self:getChildren())（gh190 实锤堆栈：stub 节点 getChildren 命中 peer 自动
-- no-op 返回 nil → TableProxy:65 rawget(nil) 炸 → MainLayer push 链断 → 地图永不
-- 就绪）。cocos tolua 的 class_index_event 对 uservalue(peer) 用 lua_gettable：
-- peer 的 mt __index 先于原生命中且直接返回，所以必须让白名单先于自动 no-op。
local __wjjh_structMethods = {
  getChildren = function() return {} end,
  getChildrenCount = function() return 0 end,
}
local __wjjh_structOr = function(k, fallback)
  local m = __wjjh_structMethods[k]
  if m ~= nil then return m end
  return fallback
end
local __stubClass = function(name)
  if _G[name] then return _G[name] end
  local t = {}
  t.create = function(...)
    local node = cc.Node:create()
    if tolua and tolua.setpeer then
      local peer = {}
      setmetatable(peer, {__index = function(tt, k) local f = __wjjh_structOr(k, function() return nil end); rawset(tt, k, f); return f end})
      tolua.setpeer(node, peer)
      for k, v in pairs(t) do if k ~= 'create' then peer[k] = v end end
    end
    return node
  end
  setmetatable(t, {__index = function(tt, k)
    local f = function() return __chain end
    rawset(tt, k, f)
    return f
  end})
  _G[name] = t
  return t
end
for _, n in ipairs({'ExtRichText','ExtRichTextScroll','ExtPageView','YXShaderSprite','YXMotionStreak','YXEaseAction','YXHelper','encrypt','LogManager'}) do
  __stubClass(n)
end
-- ===== gh259: ExtPageView 真桩 =====
-- 旧桩 create 返回普通 cc.Node + 「未知方法一律返回 nil」peer，而游戏 ExtPageView.lua
-- (Extends.lua 加载)把 ccui.PageView 整个换成它 → MainTasksUI/RankingUI/GoodsInfo 等
-- 10 个界面的构建器造出的「PageView」是假节点：setName 被 peer 吞掉、getName() 走 peer
-- 自动 no-op 返回 nil → Helper:convertUIByParent 绑定时 'table index is nil'
-- (gh257 容错只跳过不绑定) → self.PageView_Tasks=nil → 任务页空白；返回时 onDisable
-- 报错 → 切层 aftAnim 不执行 → 触摸屏蔽层永不撤销 = 全局卡死（gh258 实测链路）。
-- 真桩：create 返回真 ccui.PageView（安装此刻 ccui.PageView 还是原生类）。
-- 3.15.1 的 PageView 继承 ListView，getItem/getItems/addPage/removeAllPages/
-- getInnerContainer/setCurrentPageIndex 全原生可用；游戏 ExtPageView.lua 在类表上
-- 定义的 3 个方法(getPageByIndex/getInnerContainerPosX/setScrollDurationWithNumber)
-- 于 create 时拷入 peer（其内部走原生 getItem/getInnerContainer，isNewPackage 默认
-- true=新分支，与 3.15.1 匹配）。3.15.1 没有魔改引擎的 playScrollPageAnim 与
-- setScrollDuration → peer 里用 scrollToPage 等价 / no-op，且本桩版本优先于类表拷贝。
-- peer 是净表（无自动 no-op 元表）：未知键自然落到原生方法，setName/getName 从此走原生。
local __wjjh_realPageView = ccui.PageView
ExtPageView = {}
local __pvProbe = __wjjh_realPageView:create()
local __natSetCurrentPageIndex = nil
pcall(function()
  if type(__pvProbe.setCurrentPageIndex) == 'function' then
    __natSetCurrentPageIndex = __pvProbe.setCurrentPageIndex
  end
end)
ExtPageView.create = function(...)
  local node = __wjjh_realPageView:create()
  if tolua and tolua.setpeer then
    local peer = {
      playScrollPageAnim = function(self, pageIndex)
        -- gh260: 越界夹紧(空页/越界直接忽略)——原生 scrollToItem 对越界的容错未知,
        --   原生异常会穿 tolua 变 abort(无 Lua 日志的闪退)。getItem 天然越界安全,这里对齐。
        local oki, items = pcall(function() return self:getItems() end)
        local n = 0
        if oki and type(items) == 'table' then n = #items end
        if n <= 0 or type(pageIndex) ~= 'number' then return end
        if pageIndex < 0 then pageIndex = 0 end
        if pageIndex > n - 1 then pageIndex = n - 1 end
        return self:scrollToPage(pageIndex)
      end,
      setCurrentPageIndex = function(self, index)
        local oki, items = pcall(function() return self:getItems() end)
        local n = 0
        if oki and type(items) == 'table' then n = #items end
        if n <= 0 then return end
        if type(index) ~= 'number' then index = 0 end
        if index < 0 then index = 0 end
        if index > n - 1 then index = n - 1 end
        if __natSetCurrentPageIndex then return __natSetCurrentPageIndex(self, index) end
        return nil
      end,
      setScrollDurationWithNumber = function(self, num) end,
      getPage = function(self, index)
        if self.getItem then return self:getItem(index) end
        return nil
      end,
    }
    for k, v in pairs(ExtPageView) do
      if k ~= 'create' and peer[k] == nil then peer[k] = v end
    end
    tolua.setpeer(node, peer)
  end
  return node
end
__wjjhlog('WJJH_BOOT: ExtPageView real stub installed (native PageView + clean peer)')
-- gh192: YXEaseAction 是安卓魔改引擎的缓动动作类(游戏 31 个文件 176 处调用,战斗/UI 动画全用)。
-- 官方 3.15.1 没有它 → 桩 create 原返回 cc.Node → runAction(cc.Node) = invalid arguments
-- (gh191 实锤 AnimFightLayer:1227 rolePopNumber 每回合炸一次 → 战斗卡在首回合)。
-- 修=create 直接透传第一个参数(真 action),放弃缓动只保流程;Easing 枚举常量(安卓引擎注册的
-- 全局数字)一并补齐,防别处 nil 引用。
-- ★gh193: gh192 的透传签名写错了——游戏是冒号调用 YXEaseAction:create(action, easing),
-- 第一个参数是 self(类表),gh192 返回的其实是 self → runAction(类表) 仍 invalid arguments
-- (gh192 实锤只剩 2 次报错但同点)。正确签名=function(_self, action, easing)。
YXEaseAction.create = function(_self, action, easing) return action end
for _, c in ipairs({'Sine_EaseIn','Sine_EaseInOut','Sine_EaseOut','Quad_EaseIn','Quad_EaseInOut','Quad_EaseOut','Cubic_EaseIn','Cubic_EaseInOut','Cubic_EaseOut','Quart_EaseIn','Quart_EaseInOut','Quart_EaseOut','Quint_EaseIn','Quint_EaseInOut','Quint_EaseOut','Expo_EaseIn','Expo_EaseInOut','Expo_EaseOut','Circ_EaseIn','Circ_EaseInOut','Circ_EaseOut','Back_EaseIn','Back_EaseInOut','Back_EaseOut','Bounce_EaseIn','Bounce_EaseInOut','Bounce_EaseOut','Elastic_EaseIn','Elastic_EaseInOut','Elastic_EaseOut'}) do
  if _G[c] == nil then _G[c] = 1 end
end
__wjjhlog('WJJH_BOOT: YXEaseAction passthrough installed')
local __stubClassChain = function(name)
  if _G[name] then return _G[name] end
  local t = {}
  t.create = function(...)
    local node = cc.Node:create()
    if tolua and tolua.setpeer then
      local peer = {}
      setmetatable(peer, {__index = function(tt, k) local f = __wjjh_structOr(k, function() return __chain end); rawset(tt, k, f); return f end})
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
-- gh185: 骨骼 peer 也加"未知方法自动 no-op"兜底（FightLayer:2138 调 playAnim 就是缺它）
local function __wjjh_peer_of(peerTbl)
  return setmetatable(peerTbl, {__index = function(tt, k)
    local f = __wjjh_structOr(k, function() return nil end)
    rawset(tt, k, f)
    return f
  end})
end
local __mkSkel = function(...)
  local sk = cc.Node:create()
  tolua.setpeer(sk, __wjjh_peer_of(__wjjh_skelPeerFull()))
  return sk
end
-- ===== gh217/A3: 骨骼工厂 v3（真 spine 3.8.75 优先，失败回退哑节点）=====
-- YXSkeletonAnimation/spine38 是安卓魔改引擎的 C++ 全局，官方 3.15.1 没有；
-- 官方自带 spine 3.5/3.6 解析不了游戏的 3.8 .skel（gh164 用 cocos sp.SkeletonAnimation
-- 实测崩：spAtlas_create→dispose）→ 一直用哑节点(__mkSkel)。gh217 把 spine-runtimes 3.8
-- 编进 App（third_party/spine-3.8.75），C 桥见 WJJHSpine38.cpp：createBinary 返回
-- (容器 cc.Node, spine 指针)。★真节点 peer 只挂显式 spine/YX 方法，不挂 catch-all ——
-- 免得遮住 cc.Node 原生方法（tolua 的 peer __index 优先于原生命中）。
-- 开关：__WJJH_SPINE38_REAL（gh221 起默认 true=开；读取失败自动回退哑节点，见下方说明）。
do
  local S = {}
  -- gh221: 真 spine 开关，默认 **开**。
  --   gh220 的结论"游戏 .skel 含 non-standard 附件 type 68"已被证伪：
  --   那是诊断脚本 parse_skel.py 的 bug（region 附件多读了一次 nonessential color int），
  --   用修正后的解析器 parse_skel2.py 复算：ep2/skeleton.skel 逐字节解析到 EOF 完全吻合，
  --   附件类型只有标准的 0(region)/2(mesh)，125 个附件在 skeleton.atlas 里全部命中，
  --   骨骼父索引/插槽骨索引也全部合法 → 文件是标准 spine 3.8.99。
  --   真正崩因：readSkeletonData 返回 NULL 时，SkeletonRenderer::initWithBinaryFile 里的
  --   CCASSERT 在 Release 是 no-op，NULL 被送进 new Skeleton(NULL) → SIGSEGV。
  --   gh221 已做两件事：① spine 源码加 NULL 保护；② createBinary 先"干跑"，
  --   读不出数据就直接回退哑节点（绝不进入会崩的构造路径）。
  --   要退回纯哑节点基线：设 __WJJH_SPINE38_REAL=false（或 rawset(_G,...) ）。
  if rawget(_G, '__WJJH_SPINE38_REAL') == nil then
    rawset(_G, '__WJJH_SPINE38_REAL', true)
  end
  S.real = (rawget(_G, '__WJJH_SPINE38_REAL') == true)
    and type(rawget(_G, 'wjjh_spine38_createBinary')) == 'function'
  S.probe = type(rawget(_G, 'wjjh_spine38_probe')) == 'function'
  S.normLoop = function(loop)
    if loop == nil then return false end
    if type(loop) == 'number' then return loop ~= 0 end
    return loop ~= false
  end
  S.peerOf = function(skelptr)
    return {
      setAnimation = function(self, ti, name, loop) return wjjh_spine38_setAnimation(skelptr, ti or 0, name, S.normLoop(loop)) end,
      addAnimation = function(self, ti, name, loop, delay) return wjjh_spine38_addAnimation(skelptr, ti or 0, name, S.normLoop(loop), delay or 0) end,
      setTimeScale = function(self, s) return wjjh_spine38_setTimeScale(skelptr, s or 1) end,
      setSpeedScale = function(self, s) return wjjh_spine38_setTimeScale(skelptr, s or 1) end,
      setTrackTime = function(self, ti, t) return wjjh_spine38_setTrackTime(skelptr, ti or 0, t or 0) end,
      getAnimDuration = function(self, name) return wjjh_spine38_getAnimDuration(skelptr, name) end,
      getAnimEvents = function(self, name) return wjjh_spine38_getAnimEvents(skelptr, name) end,
      getBoneSetupPosePosition = function(self, a, b) return wjjh_spine38_getBoneSetupPosePosition(skelptr, a, b) end,
      setSlotsToSetupPose = function(self) return wjjh_spine38_setSlotsToSetupPose(skelptr) end,
      setBonesToSetupPose = function(self) return wjjh_spine38_setBonesToSetupPose(skelptr) end,
      setToSetupPose = function(self) return wjjh_spine38_setToSetupPose(skelptr) end,
      updateWorldTransform = function(self) return wjjh_spine38_updateWorldTransform(skelptr) end,
      setAttachment = function(self, slot, name) return wjjh_spine38_setAttachment(skelptr, slot, name) end,
      -- YX 专有
      -- gh233: setSlotColor 实装（游戏用它给战斗角色染色，如 body/body_back 染黄）。
      --   cc.c4f(r,g,b,a) 返回普通 table {r=,g=,b=,a=}（Cocos2d.lua:288），这里取好分量再传给 C。
      setSlotColor = function(self, slot, c)
        local r, g, b, a = 1, 1, 1, 1
        if c ~= nil then
          r = c.r or 1; g = c.g or 1; b = c.b or 1; a = c.a or 1
        end
        return wjjh_spine38_setSlotColor(skelptr, slot, r, g, b, a)
      end,
      resetAnimState = function(self) end,
      setBackwards = function(self, b) end,
      setStartTime = function(self, t) end,
      setEndTime = function(self, t) end,
      setCompleteListener = function(self, cb) end,
      setEventCallback = function(self, cb) end,
      registerScriptHandler = function(self, cb) end,
      runActionWithName = function(self, name, action) if action then self:runAction(action) end end,
      -- gh262: playAnim 系——YXSkeletonAnimationEx.lua 把它们定义在 YX 类表上，
      --   而 userdata 实例的方法解析不过 Lua 类表 ⇒ 真节点上全是 nil。
      --   gh258 时代靠哑节点 catch-all 掩盖；gh217 真节点后 playAnim=nil（gh261 实测
      --   209 次报错）：initRoleAnim 先 setVisible(false) 再 playAnim(站桩)——报错中断
      --   ⇒ 身体永远隐藏=看不到小人；战斗每步 playRoleAnim 全炸=动画瘫痪、逻辑狂奔。
      --   playAnim 按 Ex 的同款语义实装（重置→复原姿态→setAnimation track0）。
      playAnim = function(self, name, loop)
        if type(name) == 'function' then name = name() end
        if type(name) ~= 'string' then return nil end
        self:resetAnimState()
        self:setSpeedScale(1)
        self:setBonesToSetupPose()
        self:setBackwards(false)
        return self:setAnimation(0, name, S.normLoop(loop))
      end,
      -- setStartTime/setEndTime 目前是 no-op（C 桥无帧窗接口），先保证调用链不断
      setAnimFrameFromTo = function(self, from, to)
        if from ~= nil then self:setStartTime(from / 30) end
        if to ~= nil then self:setEndTime(to / 30) end
      end,
      playAnimFromToWithDuration = function(self, name, from, to, duration)
        self:playAnim(name, false)
        self:setAnimFrameFromTo(from, to)
        local interval = (to or 0) - (from or 0)
        if interval < 0 then interval = 0 end
        if duration and duration > 0 then self:setSpeedScale(interval / (30 / duration)) end
      end,
      fadeIn = function(self) self:runActionWithName("fadeInAdnFadeOut", cc.FadeIn:create(0.3)) end,
      fadeOut = function(self) self:runActionWithName("fadeInAdnFadeOut", cc.FadeOut:create(0.3)) end,
    }
  end
  S.mkSpine = function(a, b, c, d)
    -- 兼容冒号调用(self, skel, atlas, scale) 与点调用(skel, atlas, scale)
    local skel, atlas, scale
    if type(a) == 'string' then skel, atlas, scale = a, b, c else skel, atlas, scale = b, c, d end
    if skel ~= nil and atlas ~= nil then
      if S.real then
        local ok, node, skelptr = pcall(wjjh_spine38_createBinary, skel, atlas, scale or 1)
        -- gh229: node 必须是 userdata 才能 setpeer。曾经在协程里被推到错状态，拿到的是 number，
        --   结果 setpeer 抛 "userdata expected" 并被上层 xpcall 吞掉 → 节点到不了游戏。
        --   这里加类型校验：不合格就回退哑节点，绝不把坏值交给 setpeer。
        if ok and type(node) == 'userdata' and skelptr ~= nil then
          tolua.setpeer(node, S.peerOf(skelptr))
          return node
        end
        __wjjhlog('WJJH_SPINE38: createBinary unusable (ok=' .. tostring(ok)
          .. ' nodetype=' .. type(node) .. ' skelptr=' .. tostring(skelptr) .. ') -> fallback dummy')
      elseif S.probe then
        -- gh221: 真 spine 关闭时也干跑一遍，只为把"这个资产能不能读"写进日志。
        -- 干跑只解析 SkeletonData、不构造 Skeleton，绝对安全。
        local ok, msg = pcall(wjjh_spine38_probe, skel, atlas)
        if ok and msg ~= nil then
          __wjjhlog('WJJH_SPINE38 PROBE: ' .. tostring(msg))
        end
      end
    end
    return __mkSkel()
  end
  -- gh242: 元数据模式创建 —— 与 mkSpine 同型，但走 wjjh_spine38_createMetaOnly
  --   （创建期间不加载图集纹理）。失败同样回退哑节点，绝不把坏值交给 setpeer。
  S.mkSpineMetaOnly = function(a, b, c, d)
    local skel, atlas, scale
    if type(a) == 'string' then skel, atlas, scale = a, b, c else skel, atlas, scale = b, c, d end
    if skel ~= nil and atlas ~= nil then
      local ok, node, skelptr = pcall(wjjh_spine38_createMetaOnly, skel, atlas, scale or 1)
      if ok and type(node) == 'userdata' and skelptr ~= nil then
        tolua.setpeer(node, S.peerOf(skelptr))
        return node
      end
      __wjjhlog('WJJH_SPINE38: createMetaOnly unusable (ok=' .. tostring(ok)
        .. ' nodetype=' .. type(node) .. ' skelptr=' .. tostring(skelptr) .. ') -> fallback dummy')
    end
    return __mkSkel()
  end
  local YX = {}
  YX.createWithFile = function(self, skel, atlas, scale) return S.mkSpine(skel, atlas, scale) end
  YX.createWithBinaryFile = function(self, skel, atlas, scale) return S.mkSpine(skel, atlas, scale) end
  YX.create = function(self, skel, atlas, scale) return S.mkSpine(skel, atlas, scale) end
  -- gh242: 元数据模式 —— 同一套接口，但**不加载图集纹理**。
  --   供游戏 AnimResManager.lua 的 initAnimResInfo() 在启动期读动画时长/事件/
  --   攻击距离用（这些只依赖 SkeletonData）。返回的对象与 createWithBinaryFile
  --   完全同型（node + skelptr），getAnimDuration/getAnimEvents/
  --   getBoneSetupPosePosition 等全部照常可用，只是不渲染。
  --   ⇒ 启动期不再载入 gongfu1 等 4 个图集（>1450MB），解决 88% Jetsam 被杀。
  YX.createMetadataOnly = function(self, skel, atlas, scale) return S.mkSpineMetaOnly(skel, atlas, scale) end
  setmetatable(YX, {__index = function(tt, k)
    local f = function() return __mkSkel() end
    rawset(tt, k, f)
    return f
  end})
  _G['YXSkeletonAnimation'] = YX

  spine38 = setmetatable({}, {__index = function(t, k)
    if k == 'NewSkeletonAnimation' then
      -- gh242: 必须把 createMetadataOnly 也挂在这张表上 —— 游戏 AnimResManager.lua
      --   走的正是 spine38.NewSkeletonAnimation:createMetadataOnly(...)，漏了会得到
      --   nil 并抛 "attempt to call a nil value"。
      return { createWithBinaryFile = S.mkSpine, createWithFile = S.mkSpine, create = S.mkSpine,
               createMetadataOnly = S.mkSpineMetaOnly }
    end
    return nil
  end})
  __wjjhlog('WJJH_BOOT: skeleton factory v3 (spine38-real=' .. tostring(S.real) .. ') installed')
end

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

-- ===== gh260: ExtRichTextScroll 真实现 =====
-- 旧"真实现"(gh179 时代)其实是文本黑洞：pushBackText=no-op、getRichText 返回哑对象。
--   后果：全部富文本区——主输出栏 PrintUI、Fight2Layer 战斗日志、RoleObserveLayer/RoleInfoLayer
--   NPC 描述（武馆管家描述空白实测）、MainTasksUI 挂机详情等 159 处——零报错但什么都不显示。
-- 本版实现：
--   实例 = 真 ccui.ScrollView（原生触摸滚动/jumpTo*/滚动条/惯性全可用）；
--   文本 = cc.Label 流式排版（每段一个 Label，超行宽则换行/整段硬折行）；
--   内容顶对齐：flow 节点承载负 y 子节点，flow.y=内容高 ⇒ 子节点落回 inner 正坐标；
--   内容超高自动 jumpToBottom（输出栏跟随）；setString/jumpToPercentVertical/getInnerContainer
--   等 API 全部覆盖。
-- 方法放类表：RichTextEx.lua 加载时会把类表 pushBackText 包装成「颜色码/\n 分段器」
--   （原实现存为 pushBackText_old 逐段回调）⇒ 类表 pushBackText 必须是「推单段」语义。
--   实例经 peer.__index=类表 解析方法；类表没列的原生方法由 tolua 回落到 ScrollView 本体，
--   常用者另在安装期从 probe 实例捕获直通（capture 失败也不致错）。
if ExtRichTextScroll and rawget(ExtRichTextScroll, '__wjjh_real') ~= true then
  local __cls = {}
  local __probe = ccui.ScrollView:create()
  local function nat(name)
    local ok, f = pcall(function() return __probe[name] end)
    if ok and type(f) == 'function' then return f end
    return nil
  end
  -- 原生直通（安装期捕获；缺了就靠 peer 未命中回落原生）
  for _, name in ipairs({'jumpToTop','jumpToBottom','scrollToBottom','jumpToPercentVertical',
      'setScrollBarEnabled','setBounceEnabled','setInertiaScrollEnabled','setTouchEnabled',
      'setInnerContainerSize','getInnerContainerSize','getInnerContainer',
      'addEventListener','addTouchEventListener','setContentSize','getContentSize',
      'addChild','removeFromParent','setAnchorPoint','setPosition','getPosition',
      'setLocalZOrder','setTag','setName','setVisible','setCascadeOpacity',
      'setScale','setSwallowTouches'}) do
    local f = nat(name)
    if f then __cls[name] = function(self, ...) return f(self, ...) end end
  end
  local __natJumpBottom = __cls.jumpToBottom
  local __natSetContentSize = __cls.setContentSize
  local __natDir = nat('setDirection')
  if __natDir then
    __cls.setDirection = function(self, dir)
      if dir == nil then dir = 1 end  -- kCCScrollViewDirectionVertical 在个别环境可能未定义,默认竖向
      return __natDir(self, dir)
    end
  end
  -- ---- 流式排版 ----
  local function st(self)
    if tolua and tolua.getpeer then
      local p = tolua.getpeer(self)
      if type(p) == 'table' then return p.st end
    end
    return nil
  end
  local function makeLabel(text, color, opacity, fontName, fontSize)
    local label
    pcall(function() label = cc.Label:createWithTTF(text, fontName, fontSize) end)
    if label == nil then
      pcall(function() label = cc.Label:createWithSystemFont(text, 'Arial', fontSize) end)
    end
    if label == nil then return nil end
    if color ~= nil then pcall(function() label:setTextColor(color) end) end
    if opacity ~= nil then pcall(function() label:setOpacity(opacity) end) end
    pcall(function() label:setAnchorPoint(cc.p(0, 1)) end)
    return label
  end
  local function labelSize(label)
    local ok, w, h = pcall(function()
      local sz = label:getContentSize()
      return sz.width, sz.height
    end)
    if not ok then return 0, 0 end
    return w, h
  end
  local function advanceLine(s, extra)
    local lh = s.lineH
    if lh <= 0 then lh = (s.lastSize or 42) * 1.2 end
    s.h = s.h + lh + (s.vspace or 0) + (extra or 0)
    s.x = 0
    s.lineH = 0
  end
  local function refit(self)
    local s = st(self)
    if not s or not s.flow then return end
    local vh = 0
    pcall(function() vh = self:getContentSize().height or 0 end)
    local innerH = s.h
    if vh and vh > innerH then innerH = vh end
    if innerH < 1 then innerH = 1 end
    s.flow:setPositionY(innerH)
    pcall(function() self:setInnerContainerSize(cc.size(math.max(s.W, 1), innerH)) end)
  end
  __cls.pushBackText = function(self, text, color, opacity, fontName, fontSize)
    local s = st(self)
    if not s or not s.flow or type(text) ~= 'string' or text == '' then return end
    if type(fontSize) ~= 'number' or fontSize <= 0 then fontSize = s.lastSize or 42 end
    s.lastSize = fontSize
    local label = makeLabel(text, color, opacity, fontName, fontSize)
    if label == nil then return end
    local w, h = labelSize(label)
    if w <= 0 then return end
    local wrapped = false
    if w > s.W then
      pcall(function()
        label:setLineBreakWithoutSpace(true)
        label:setDimensions(s.W, 0)
      end)
      w, h = labelSize(label)
      wrapped = true
    end
    if wrapped or (s.x > 0 and s.x + w > s.W) then
      advanceLine(s, 0)  -- 本行放不下,先结行再从头排
    end
    s.flow:addChild(label)
    pcall(function() label:setPosition(s.x, -s.h) end)
    if h > s.lineH then s.lineH = h end
    s.x = s.x + w
    refit(self)
  end
  __cls.pushBackNewLine = function(self, size)
    local s = st(self)
    if not s then return end
    advanceLine(s, size or 0)
    refit(self)
    -- 内容超高后跟随底部（输出栏语义）；描述类界面随后会自行 jumpToTop 覆盖
    local okv, sz = pcall(function() return self:getContentSize() end)
    if okv and sz ~= nil and sz.height and s.h > sz.height + 2 and __natJumpBottom then
      pcall(function() __natJumpBottom(self) end)
    end
  end
  __cls.setVerticalSpace = function(self, v)
    local s = st(self)
    if s and type(v) == 'number' then s.vspace = v end
  end
  __cls.setTextMaxHeight = function(self, mh)
    local s = st(self)
    if s then s.maxH = mh end  -- 记录不裁剪:游戏层到阈值自会重建(PrintUI 8888/MainTasksUI 4000)
  end
  __cls.getRichText = function(self)
    local s = st(self)
    if not s then return nil end
    if s.inner == nil then
      local inner = {}
      inner.setVerticalSpace = function(_, v)
        if type(v) == 'number' then s.vspace = v end
      end
      inner.getNewContentSizeHeight = function() return s.h end
      inner.formatText = function() end
      inner.removeAllElement = function()
        if s.flow then pcall(function() s.flow:removeAllChildren() end) end
        s.h, s.x, s.lineH = 0, 0, 0
        if s.owner then refit(s.owner) end
      end
      inner.pushBackText = function(_, text, c, o, f, sz)
        if s.owner then return __cls.pushBackText(s.owner, text, c, o, f, sz) end
      end
      inner.pushBackNewLine = function(_, n)
        if s.owner then return __cls.pushBackNewLine(s.owner, n) end
      end
      setmetatable(inner, {__index = function() return function() end end})
      s.inner = inner
    end
    return s.inner
  end
  __cls.setSize = function(self, size)
    local s = st(self)
    if not s then return end
    -- size 可能是 {width,height} 纯表,也可能是 getContentSize() 返回的 Size userdata
    local okw, w = pcall(function() return size.width end)
    local okh, h = pcall(function() return size.height end)
    if okw and okh and type(w) == 'number' and type(h) == 'number' then
      s.W = w
      if __natSetContentSize then pcall(function() __natSetContentSize(self, size) end) end
      refit(self)
    end
  end
  __cls.setString = function(self, str)
    if st(self) == nil then return end
    __cls.getRichText(self):removeAllElement()
    if type(str) == 'string' and str ~= '' then
      __cls.pushBackText(self, str, cc.c3b(255, 255, 255), 255, 'Font/default.ttf', 30)
      __cls.pushBackNewLine(self, 0)
    end
  end
  __cls.create = function(...)
    local scroll = ccui.ScrollView:create()
    if scroll == nil then return nil end
    local s = { W = 0, h = 0, x = 0, lineH = 0, vspace = 0, lastSize = nil, maxH = 0, owner = scroll }
    if not (tolua and tolua.setpeer) then
      __wjjhlog('WJJH_BOOT: ExtRichTextScroll real impl: no tolua.setpeer, degraded')
      return scroll
    end
    local peer = { st = s }
    setmetatable(peer, {__index = __cls})
    tolua.setpeer(scroll, peer)
    local flow = cc.Node:create()
    scroll:addChild(flow)  -- ScrollView.addChild 自动进 inner 容器
    s.flow = flow
    -- gh262: 默认关触摸——cocos ScrollView 默认吞触摸,真实现后战斗日志框
    --   (Image_print 底缘与按钮区重叠 12px)开始吞技能按钮的触摸。游戏在需要
    --   滚动的界面会显式 setTouchEnabled(true)（RoleInfoLayer:setViewPage 等）。
    pcall(function() scroll:setTouchEnabled(false) end)
    local okw, cw = pcall(function() return scroll:getContentSize().width end)
    if okw and type(cw) == 'number' and cw > 0 then s.W = cw else s.W = 600 end
    return scroll
  end
  rawset(__cls, '__wjjh_real', true)
  ExtRichTextScroll = __cls
  __wjjhlog('WJJH_BOOT: ExtRichTextScroll real impl installed (gh260, native ScrollView + label flow)')
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
  -- gh194: 战斗不推进诊断——gh193 战斗页停在「开始战斗」(开场文字动画的 endFunc 没执行
  -- → fight:start() 没被调 → 回合循环永不启动)。探针:开场链每一步 + 坏 delay 侦测。
  -- gh194 结果:ENDFUNC ✓ Fight.start ✓ 但画面仍停「开始战斗」→ 帧循环驱动待查(gh195)。
  ['app.views.layer.FightLayer.FightLayer'] = { { 'startMapFight' }, { 'afterSetPlayer' }, { 'beforeStartFightAmin' }, { 'playFightBeginAnim' }, { 'initRoleAnim' } },
  ['app.models.fight.Fight'] = { { 'start' }, { 'ready' }, { 'pause' }, { 'getLargestStartanimTime' }, { 'updateFrame', 'hot' }, { 'playFrame', 'hot' } },
  ['app.models.fight.FightRole'] = { { 'getStartAminNeedTime' }, { 'updateFrame' } },
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
  if name == 'app.models.fight.Fight' then
    -- gh196: 战斗帧循环提速。gh195 实锤:updateFrame 在跑但仅 ~2.5fps(设计 30fps)——
    -- cocos 渲染 tick 在 CI 模拟器只有 ~5-15fps,FightLayer:schedule(0.033) 的 elapsed
    -- 累积(每渲染帧+1/60)要 2 渲染帧才触发一次 → 战斗慢动作 30 倍,90s 窗口打不完一击。
    -- Game 主循环(15.8fps 实测)就是 newScene:schedule(fn,0) 同款——把 updateFrame 挂到
    -- 运行场景 interval=0,与主循环同权。原 33ms 调度保留(双驱动无害,state 守卫防战后误推)。
    -- ★yield-safe:回调内容=原 schedule 回调同款(updateFrame),等价迁移,不 pcall 不构造表。
    local ost = M.start
    if type(ost) == 'function' then
      M.start = function(self, ...)
        __wjjhlog('WJJH_FIGHT: Fight.start enter (frame-drive hook)')
        -- gh265: 真机构建跳过 frame-drive。该钩子为 CI 模拟器 5-15fps 渲染设计
        --   （每渲染帧推 12 逻辑帧补帧率）；真机 60fps 下 = 每秒 720 逻辑帧 = 24 倍速，
        --   战斗回合/指令节奏全被打飞（老板实测"自动乱打一通，毫无战斗逻辑"）。
        --   真机走游戏自己的 33ms 调度（≈30fps 逻辑帧，与安卓一致）。
        if __WJJH_DEVICE then
          __wjjhlog('WJJH_FIGHT: frame-drive skipped (device build)')
          return ost(self, ...)
        end
        if not self.__wjjh_frameHook then
          self.__wjjh_frameHook = true
          local sc = cc.Director:getInstance():getRunningScene()
          if sc and sc.schedule then
            -- gh197: 每渲染帧推 6 帧 ≈ 30fps 逻辑帧率(gh196 实测渲染 tick ~5fps)——
            -- gh198: 提到 12 帧(≈60fps 逻辑,2x 快进)——gh197 打了 20+ 击仍未分胜负。
            -- 战斗结束(state≠RUNNING)后 updateFrame 早退,空转开销可忽略。
            sc:schedule(function()
              -- gh209: 战斗结束后彻底停手——FIGHT_STATE_END 是**未定义全局(=nil)**(Definition.lua
              -- 只有 NONE0/IDLE1/READY2/START3/RUNNING4/PAUSE5/COMPLETE6),Fight 结束时 _state 被置
              -- nil(1799 行);且我们兜底逃跑后 state 仍=4。哨兵:state~=4 或已发过兜底 → 不推帧、
              -- 不发事件(gh208 实锤:战斗结束 30 秒后兜底逃跑仍在已销毁的战斗上发事件,污染状态)。
              if self.__wjjh_done or self._state ~= (_G.FIGHT_STATE_RUNNING or 4) then
                if not self.__wjjh_stopLogged then
                  self.__wjjh_stopLogged = true
                  __wjjhlog('WJJH_FIGHT: hook idle-stop state=' .. __ts(self._state))
                end
                return
              end
              for _ = 1, 12 do
                self:updateFrame()
              end
              -- gh207: CI 专测「打死」路径——开战首帧把对手血量压到 10,让随后正常一击必杀,
              -- 观察 roleDie→playWinAnim→fightEnd(WIN)→BaseMap FIGHT_FINISH 结算→回地图。
              -- ★仅 CI(__WJJH_AUTOSTART=false 的真机包不跑);正常伤害链不变,只改起点血量。
              if __WJJH_AUTOSTART and not self.__wjjh_nerfed then
                local foe = self:getRoleByTeamIdAndInTeamId(2, 1)
                if foe ~= nil and foe.addAttr ~= nil and foe.getAttr ~= nil then
                  local q = foe:getAttr("qi")
                  if q ~= nil and q > 10 then
                    foe:addAttr("qi", -(q - 10))
                    self.__wjjh_nerfed = true
                    __wjjhlog('WJJH_FIGHT: CI nerf foe qi ' .. __ts(q) .. ' -> 10 (KO path test)')
                  end
                end
              end
              -- gh200: 实证探针——gh199 里 auto-runaway 一次没触发(updateFrame 却有 1591 次),
              -- 不明。每 60 帧打一条 hook 心跳,直接看回调死活/frames 累计/auto 标志值。
              self.__wjjh_frames = (self.__wjjh_frames or 0) + 12
              self.__wjjh_hookTicks = (self.__wjjh_hookTicks or 0) + 1
              if self.__wjjh_frames % 60 == 0 then
                __wjjhlog('WJJH_FIGHT: tick frames=' .. __ts(self.__wjjh_frames) ..
                          ' ticks=' .. __ts(self.__wjjh_hookTicks) ..
                          ' auto=' .. __ts(__WJJH_AUTOSTART))
              end
              -- gh198: CI 自动逃跑——600 帧后触发并每 600 帧重试(逃跑有成功率),
              -- 刻意走「fightEnd→结算→回地图」链验证收尾,不用等打死。
              -- gh199: 改走 FightLayer:onRunaway() 正规 UI 路径(无参→btnType=nil→跳过
              -- 80% 失败分支;内部 runRole:runAway() 判定,失败会进 CD→下个 600 帧重试)。
              -- gh198 直调 Fight:runaway() 静默无效(绕过了 pause+事件+executeBuff 流程)。
              if __WJJH_AUTOSTART then
                -- gh204/205: 不再依赖 FightLayer 实例——gh203 实锤 fl._fight 读回 nil
                -- (userdata peer 字段不可靠),走了静默无效的 fallback。
                -- normalRunaway 核心三步直接在 Fight 模型侧执行。★顺序:runaway→事件→pause——
                -- pause 置 _isPaused 后 updateFrame 直接早退,_eventListenerFM:callFunctions()
                -- (在 updateFrame 尾部)永不执行=事件卡死(gh204 实锤:事件发了 4 次无人消费)。
                -- gh206: 逃跑推迟到 2600 帧当兜底——先让战斗自然打到 KO 验证「打死」路径
                -- (fightEnd WIN 分支与逃跑同一事件机制,BaseMap 的 FIGHT_FINISH 分支结算)。
                if self.__wjjh_frames >= (self.__wjjh_nextRunaway or 2600) then
                  self.__wjjh_nextRunaway = (self.__wjjh_nextRunaway or 2600) + 600
                  self.__wjjh_done = true
                  __wjjhlog('WJJH_FIGHT: runaway via fight events at frame ' .. self.__wjjh_frames)
                  self:runaway()
                  self:callEventListener(4)
                  self:pause()
                end
              end
            end, 0)
            __wjjhlog('WJJH_FIGHT: frame-drive hooked on running scene (interval=0)')
          else
            __wjjhlog('WJJH_FIGHT: no running scene, no hook')
          end
        end
        return ost(self, ...)
      end
    end
  end
  if name == 'app.views.layer.FightLayer.FightLayer' then
    -- gh201: 抓 FightLayer 实例——getLayer('FightLayer') 会 assert error(_layers 里注册名
    -- 不是这个短名,gh200 实锤 error:FightLayer)。startMapFight 时把 self 存全局。
    local osmf = M.startMapFight
    if type(osmf) == 'function' then
      M.startMapFight = function(self, ...)
        __WJJH_FIGHT_LAYER = self
        __wjjhlog('WJJH_FIGHT: FightLayer captured')
        return osmf(self, ...)
      end
    end
    -- gh202: onRunaway 前后探针——gh201 里 fl:onRunaway() 被调用但战斗未暂停未结束,
    -- 内部某步静默断(被 NodeEx:235 的 pcall 吞)。enter+done 两点定位。
    -- ★yield-safe:enter/done 日志+直接 return。
    local orn = M.onRunaway
    if type(orn) == 'function' then
      M.onRunaway = function(self, ...)
        __wjjhlog('WJJH_FIGHT: onRunaway enter fight=' .. __ts(self._fight) ..
                  ' state=' .. __ts(self._fight and self._fight._state))
        local r = orn(self, ...)
        __wjjhlog('WJJH_FIGHT: onRunaway done paused=' .. __ts(self._fight and self._fight:isPaused()))
        return r
      end
    end
    -- gh194: playFightBeginAnim 的 endFunc 是战斗回合循环(fight:start)的唯一启动点。
    -- 包住 endFunc,确认它有没有被开场文字动画的 CallFunc 调到。
    -- ★yield-safe:enter 日志+直接 return,不 pcall 不构造表(endFunc 包装仅闭包赋值)。
    local op = M.playFightBeginAnim
    if type(op) == 'function' then
      M.playFightBeginAnim = function(self, endFunc, ...)
        __wjjhlog('WJJH_FIGHT: playFightBeginAnim enter endFunc=' .. __ts(endFunc))
        if type(endFunc) == 'function' then
          local oe = endFunc
          endFunc = function(...)
            __wjjhlog('WJJH_FIGHT: playFightBeginAnim ENDFUNC CALLED -> fight:start next')
            return oe(...)
          end
        end
        return op(self, endFunc, ...)
      end
    end
  end
  if name == 'app.views.ui.FightUI.FightUI' then
    -- gh208: 胜利/失败面板自动点击(仅 CI)——BaseMap 的 FIGHT_FINISH 分支把结算+回图
    -- 绑在 Panel_fightEndTextArea 的 release 回调上(设计=等玩家点「退出战斗」,gh207
    -- 截图实锤胜利面板悬停)。CI 无点击:绑定时预约 ~2 秒(10 渲染帧)后自动触发一次。
    local orf = M.setFightEndTextAreaReleaseFunc
    if type(orf) == 'function' then
      M.setFightEndTextAreaReleaseFunc = function(self, releaseFunc, ...)
        __wjjhlog('WJJH_FIGHT: end panel release func bound')
        if __WJJH_AUTOSTART and type(releaseFunc) == 'function' then
          local sc = cc.Director:getInstance():getRunningScene()
          if sc and sc.schedule then
            local ticks = 0
            local fired = false
            sc:schedule(function()
              if fired then return end
              ticks = ticks + 1
              if ticks >= 10 then
                fired = true
                __wjjhlog('WJJH_FIGHT: auto-clicking end panel (CI)')
                local ok, err = pcall(releaseFunc)
                __wjjhlog('WJJH_FIGHT: end panel release ok=' .. tostring(ok) .. ' err=' .. __ts(err))
              end
            end, 0)
          end
        end
        return orf(self, releaseFunc, ...)
      end
    end
  end
  if name == 'app.extends.NodeEx' then
    -- gh194: 坏 delay 侦测——Node:delayFunc 对非数字 delay(nil 分支=立即执行;字符串/表=
    -- cc.DelayTime:create(坏值) 行为未定)。怀疑 getLargestStartanimTime 链返回坏值。
    local od = Node and Node.delayFunc
    if type(od) == 'function' then
      Node.delayFunc = function(self, delay, func, ...)
        if delay ~= nil and type(delay) ~= 'number' then
          __wjjhlog('WJJH_DELAY: bad delay type=' .. type(delay) .. ' val=' .. __ts(delay))
        end
        return od(self, delay, func, ...)
      end
      __wjjhlog('WJJH_MOD: Node.delayFunc bad-delay probe installed')
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
  -- gh194: 战斗诊断
  ['app.views.layer.FightLayer.FightLayer'] = true,
  ['app.models.fight.Fight'] = true,
  ['app.models.fight.FightRole'] = true,
  ['app.extends.NodeEx'] = true,
  -- gh208: 胜利面板自动点击(CI)——BaseMap 的 FIGHT_FINISH 分支把结算+回图绑在
  -- 胜利面板 release 回调上(等玩家点「退出战斗」),CI 需要自动触发。
  ['app.views.ui.FightUI.FightUI'] = true,
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
              -- gh191: gh190 的 nudge 写错了分支（放进了 layer==nil，永远不跑还索引 nil self）。
              -- 真正意图=层在但没房间：探测 _currMap 并主动催熟一次（setMap 只催一次）。
              if not __wjjh_mapNudged then
                __wjjh_mapNudged = true
                local mc = layer._currMap
                __wjjhlog('WJJH_GOAL: nudge try, _currMap=' .. __ts(mc) ..
                          ' entryRoom1=' .. __ts(mc and mc.entryRoom1))
                if mc ~= nil and layer.setMap ~= nil then
                  local okN, errN = pcall(function() layer:setMap(mc) end)
                  __wjjhlog('WJJH_GOAL: nudge setMap ok=' .. tostring(okN) .. ' err=' .. __ts(errN) ..
                            ' currRoom=' .. __ts(layer._currRoom))
                end
              end
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
        -- ===== gh215: 商店探针（CI only；真机包不编译此段）=====
        -- 走 Store:initStore（Store.lua:355）的同一条客户端接口
        --   HttpManagerEx:getMerchantStoreList —— 不依赖 UI 导航，不受商人位置影响。
        -- 断言三件事：①服务端返回 status=200/errcode=0 且 goodsList 非空；
        --   ②每件 goodsId 都能被 GoodsHelper:getGoodsResClass 解析
        --     （GoodsHelper.lua:34 查不到会 error() ⇒ 这是商店崩的唯一路径）；
        --   ③价格非 0 且量纲 sane。
        -- 节奏：autoRoleTick 每 0.5s 一跳（scheduleScriptFunc 0.5），故 navTicks≈2/s；
        --       取 navTicks 56..160 每 8 跳打一个，最多 4 个商户，约 40s 内跑完。
        if navTicks >= 56 and navTicks <= 160 and navTicks % 8 == 0
           and __wjjh_storeIdx == nil then
          __wjjh_storeIdx = 0
        end
        if navTicks >= 56 and navTicks <= 160 and navTicks % 8 == 0
           and __wjjh_storeIdx ~= nil and __wjjh_storeIdx < 4 then
          if __wjjh_storeIds == nil then
            local okM, conf = pcall(function()
              return require('script.store.storeConfig')['Sheet1']
            end)
            local ids = {}
            if okM and conf ~= nil then
              for k, v in pairs(conf) do
                if type(v) == 'table' then ids[#ids + 1] = tostring(k) end
              end
              table.sort(ids)
            end
            __wjjh_storeIds = ids
            __wjjhlog('WJJH_STORE: storeConfig loaded ok=' .. tostring(okM) ..
                      ' merchants=' .. #ids)
          end
          if #__wjjh_storeIds > 0 then
            __wjjh_storeIdx = __wjjh_storeIdx + 1
            local mid = __wjjh_storeIds[__wjjh_storeIdx]
            local okH, HM = pcall(require, 'app.models.Store.GoodsHelper')
            local okS, errS = pcall(function()
              if HttpManagerEx == nil or HttpManagerEx.getMerchantStoreList == nil then
                __wjjhlog('WJJH_STORE: HttpManagerEx nil, skip')
                return
              end
              HttpManagerEx:getMerchantStoreList(mid, false, 0, 0,
                function(status, errcode, errmsg, data, isEncrypted)
                  local okC, errC = pcall(function()
                    local n, pmin, pmax, unres = 0, nil, nil, 0
                    local sample, badg = {}, {}
                    if data ~= nil and data.goodsList ~= nil then
                      for _, g in ipairs(data.goodsList) do
                        n = n + 1
                        local gid = tostring(g.goodsId or g.goods_id or '?')
                        local pr = tonumber(g.price)
                        if pr ~= nil then
                          if pmin == nil or pr < pmin then pmin = pr end
                          if pmax == nil or pr > pmax then pmax = pr end
                        end
                        if n <= 4 then
                          sample[#sample + 1] = gid .. '@' .. tostring(pr)
                        end
                        if okH and HM ~= nil and HM.getGoodsResClass ~= nil then
                          local okR, r = pcall(HM.getGoodsResClass, HM, gid)
                          if not okR or r == nil then
                            unres = unres + 1
                            badg[#badg + 1] = gid
                          end
                        end
                      end
                    end
                    __wjjhlog('WJJH_STORE: m=' .. mid .. ' status=' .. tostring(status) ..
                              ' errcode=' .. tostring(errcode) .. ' enc=' .. tostring(isEncrypted) ..
                              ' goods=' .. n .. ' price=' .. tostring(pmin) .. '~' .. tostring(pmax) ..
                              ' unresolvable=' .. unres ..
                              ' sample=[' .. table.concat(sample, ', ') .. ']' ..
                              (#badg > 0 and (' bad=[' .. table.concat(badg, ', ') .. ']') or ''))
                  end)
                  if not okC then
                    __wjjhlog('WJJH_STORE: callback err=' .. __ts(errC))
                  end
                end, false)
            end)
            __wjjhlog('WJJH_STORE: probe#' .. __wjjh_storeIdx .. ' m=' .. mid ..
                      ' ok=' .. tostring(okS) .. ' err=' .. __ts(errS) ..
                      ' goodshelper=' .. tostring(okH))
          end
        end
        -- gh180: 目标驱动的探索——每到一间房：逐 NPC 算动作表（createMapRoleFuncList），
        -- 发现「切磋/挑战/动手/比试/攻击/战斗」直接触发（走 observe 层路径），否则继续走。
        if navTicks > 60 and navTicks <= 400 and navTicks % 4 == 0 and not __wjjh_fightFired then
          local okG, errG = pcall(function()
            local CL = package.loaded['app.views.layer.ControllLayer']
            local layer = CL and CL:getInstance() and CL:getInstance():getLayer('MapLayer')
            if layer == nil or layer._currRoom == nil then
              if __wjjh_goalWait == nil then
                __wjjh_goalWait = 0
              end
              __wjjh_goalWait = __wjjh_goalWait + 1
              if __wjjh_goalWait % 4 == 1 then
                local mc = _G.MainControllLayer
                local mcOK = (mc ~= nil)
                local mcRoom = 'n/a'
                if mcOK and mc.getLayer ~= nil then
                  local okL2, l2 = pcall(function() return mc:getLayer('MapLayer') end)
                  mcRoom = tostring(okL2) .. ':' .. tostring(l2) ..
                           ':' .. tostring(l2 and l2._currRoom)
                end
                __wjjhlog('WJJH_GOAL: waiting(t=' .. __wjjh_goalWait .. ') layer=' .. tostring(layer) ..
                          ' MainCL=' .. tostring(mcOK) .. ' mcGetLayer=' .. mcRoom)
              end
              return
            end
            local room = layer._currRoom
            local map = layer._currMap
            __walkVisited = __walkVisited or {}
            __walkVisited[room.id] = true
            if __wjjh_fightFired then return end
            -- 逐 NPC 找可触发的战斗动作
            local okModel, ROM = pcall(require, 'app.models.role.RoleObserveModel')
            local ids = map and map.getRoomRoleList and map:getRoomRoleList(room.id) or {}
            if okModel and ROM ~= nil and ROM.createMapRoleFuncList ~= nil then
              for i, rid in ipairs(ids or {}) do
                local role = map.getRole and map:getRole(rid)
                if role ~= nil and role.type == 'role' then
                  local nm = role.name or (role.getName and role:getName()) or role.id
                  local okL, fl = pcall(ROM.createMapRoleFuncList, ROM, role)
                  local names = {}
                  if okL and type(fl) == 'table' then
                    for j, v in ipairs(fl) do names[#names + 1] = tostring(v.btnName) end
                  end
                  __wjjhlog('WJJH_GOAL: at ' .. __ts(room.id) .. ' npc ' .. __ts(nm) ..
                            ' actions [' .. table.concat(names, ', ') .. '] ok=' .. tostring(okL))
                  -- gh184: pairs(nil) 侦察（TableProxy 覆盖了 pairs，喂 nil 时会自己报错并打断
                  -- 切磋流程；包一层记录调用点，找出是谁把 nil 传进来）。装在此处=游戏已加载完。
                  if __WJJH_AUTOSTART and not __wjjh_pairs_wrapped then
                    __wjjh_pairs_wrapped = true
                    local op = pairs
                    _G.pairs = function(tt, ...)
                      if tt == nil then
                        __wjjhlog('WJJH_PAIRS: pairs(nil) from ' ..
                                  tostring(debug.traceback('', 2)):gsub('%c', ' | '):sub(1, 500))
                        return function() return nil end
                      end
                      return op(tt, ...)
                    end
                    __wjjhlog('WJJH_PAIRS: wrapper installed')
                  end
                  if okL and type(fl) == 'table' then
                    for j, v in ipairs(fl) do
                      local an = tostring(v.btnName or '')
                      if an:find('切磋') or an:find('挑战') or an:find('动手')
                         or an:find('比试') or an:find('攻击') or an:find('战斗') then
                        __wjjh_fightFired = true
                        local okF, errF = pcall(v.btnFunc)
                        __wjjhlog('WJJH_GOAL: FIRED [' .. an .. '] vs ' .. __ts(nm) ..
                                  ' ok=' .. tostring(okF) .. ' err=' .. __ts(errF))
                        return
                      end
                    end
                  end
                end
              end
            end
            -- 没有可打的目标：继续走（优先未访问+可进入；否则退而求其次走任意可进入的未走出口）
            local lastDir, lastTo = nil, nil
            if type(room.link) == 'table' then
              for d, rid in pairs(room.link) do
                local attr = nil
                if map ~= nil and map.getRoomAttr ~= nil then attr = map:getRoomAttr(rid) end
                local ent = attr and attr.enterable
                if (ent == 1 or ent == true or ent == nil) and __walkVisited[rid] == nil then
                  lastDir, lastTo = d, rid; break
                end
              end
            end
            if lastTo == nil then
              __wjjhlog('WJJH_GOAL: no new room from ' .. __ts(room.id) .. ' -> hold')
              return
            end
            layer:entryRoom(room.id, lastTo, lastDir)
            __wjjhlog('WJJH_GOAL: walk ' .. __ts(room.id) .. ' -> ' .. __ts(lastTo))
          end)
          if not okG then
            __wjjhlog('WJJH_GOAL: step err=' .. __ts(errG))
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
    -- gh179: 战斗 UI 用的（FightUI.lua:63/65/104）
    setScrollBarEnabled = function(self, enable) return self end,
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

-- ===== gh265/gh271: 战斗骨架预热（治「第一次切磋迟钝很久」+ 保 4444 生效）=====
-- ★ gh271 推翻 gh265 的页预载方案：addImageAsync 走标准管线（RGBA8888）把 2107 页
--   塞进 TextureCache，战斗时 spine loader 直接命中这些 8888 纹理 ⇒ gh269 的
--   4444 强制 loader 被绕过（gh270 实测战斗内存反超 392MB、五场后 1450MB 被杀）。
-- 新方案 = 逐个「预热真实骨架」：createWith*File 走 spine loader（4444 强制✓），
--   SkeletonData 进桥内缓存✓，实例即抛（shared ownership，数据/纹理存活）。
--   首战只剩 Skeleton 实例构建（<1s）。每 0.6s 一个，避免标题界面一次卡 5 秒。
do
  local __warmQ = {
    { skel = 'Anim/gongfu/gongfu.json',            atlas = 'Anim/gongfu/gongfu.atlas' },
    { skel = 'Anim/specialEffect/specialEffect.skel', atlas = 'Anim/specialEffect/specialEffect.atlas' },
    { skel = 'Anim/enterVictory/enterVictory.skel', atlas = 'Anim/enterVictory/enterVictory.atlas' },
    { skel = 'Anim/FightEffect/fanghu.skel',       atlas = 'Anim/FightEffect/fanghu.atlas' },
  }
  local __warmIdx = 0
  local __sched = cc.Director:getInstance():getScheduler()
  if __sched then
    -- gh275: 3.15.1 Scheduler 绑定没有 unscheduleScriptFunc（gh271 实测 1314 次报错，
    --   每 0.6s 空转一次）。改标志位短路（空转开销≈nil 比较）。
    local __done = false
    __sched:scheduleScriptFunc(function()
      if __done then return end
      __warmIdx = __warmIdx + 1
      local it = __warmQ[__warmIdx]
      if not it then
        __done = true
        __wjjhlog('WJJH_PRELOAD: all skeletons warmed')
        return
      end
      local ok, err = pcall(function()
        if spine38 and spine38.NewSkeletonAnimation then
          spine38.NewSkeletonAnimation:createWithBinaryFile(it.skel, it.atlas, 1)
        end
      end)
      __wjjhlog('WJJH_PRELOAD: warm ' .. it.skel .. ' ok=' .. tostring(ok) .. ' ' .. tostring(err or ''))
    end, 0.6, false)
    __wjjhlog('WJJH_PRELOAD: skeleton warmup scheduled (4 items, 0.6s apart)')
  end
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
