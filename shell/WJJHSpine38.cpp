// -*- coding: utf-8 -*-
// gh217/A3: spine 3.8.75 真运行时接入 —— C++ 桥。
//
// 背景：游戏原本走 AppDelegate.cpp 里的"哑节点工厂"(__mkSkel)，角色不播动画。
//   根因是官方 cocos2d-x 3.15.1 自带 spine 3.5/3.6，解析不了游戏的 3.8 .skel
//   （gh164 用 cocos 的 sp.SkeletonAnimation 直接崩：spAtlas_create→dispose）。
//   这里把 spine-runtimes 3.8 分支的运行时编进 App，提供真·spine38::SkeletonAnimation。
//
// 设计（与工程既有约定一致，不引入 tolua 类注册）：
//   * C 侧只 lua_register 一组 wjjh_spine38_* 函数；
//   * createBinary 返回 (容器 cc.Node, spine 指针 lightuserdata)：
//       - 容器 cc.Node 是 Lua 面对象（addChild/setPosition 等原生方法全可用）；
//       - 真 spine 节点作为容器的子节点，负责渲染；
//       - spine 指针用 lightuserdata 原样回传，Lua peer 闭包捕获后调方法时回传本文件。
//     这样 Lua 面永远是普通 cc.Node，spine 类型不进 tolua，零类型风险。
//   * 任何一步失败（文件缺失 / 解析失败 / 空 skeleton）都返回 nil，
//     Lua 侧回退到哑节点（绝不让 gh164 式崩溃重演）。
//
// 游戏驱动（SpineAnimationState.lua）每帧手调 setTrackTime(0, t) 驱动姿势，
//   并要求 getAnimDuration/getAnimEvents/setAnimation/setTimeScale —— 均已实现。

#include "WJJHSpine38.h"

#include "cocos2d.h"
#include "scripting/lua-bindings/manual/CCLuaEngine.h"

#include "spine38/SkeletonAnimation.h"
#include "spine38/spine.h"
#include "spine38/Atlas.h"
#include "spine38/AtlasAttachmentLoader.h"
#include "spine38/SkeletonBinary.h"
#include "spine38/SkeletonJson.h"
#include "spine38/SkeletonData.h"
#include "spine38/Slot.h"

extern "C" {
#include "lauxlib.h"
}

#include <string>
#include <set>
#include <map>
#include <cstdio>

USING_NS_CC;

// gh242: 定义在 third_party/spine-3.8.75/cocos2dx/spine-cocos2dx.cpp。
//   打开后 Cocos2dTextureLoader::load 直接返回，不创建 Texture2D。
namespace spine38 {
    bool wjjhSpine38SkipTextureLoad(bool skip);
}

namespace {

// Lua 侧传回的 spine 指针（lightuserdata）。
inline spine38::SkeletonAnimation* skel_of(lua_State* L, int idx) {
    void* p = lua_touserdata(L, idx);
    return reinterpret_cast<spine38::SkeletonAnimation*>(p);
}

inline const char* cstr_of(const spine38::String& s) {
    const char* b = s.buffer();
    return b ? b : "";
}

// ---- gh224 格式探测：区分 spine JSON 文本 与 spine 二进制 ----
// 背景（run 224 实测）：游戏资产是**混格式**的 ——
//   * 239 个 .skel（二进制 3.8.99），走 createWithBinaryFile；
//   * 另有 16 个 .json（其中 8 个**没有** .skel 兄弟文件，必须按 JSON 读），
//     版本是 3.0.16 / 2.1.08；游戏 Resource.lua 里 maskEffect/ep2 等就指向 .json。
// 而 Lua 侧 YXSkeletonAnimation:createWithFile 对两者都走同一个 C 入口。
// run 224 崩溃根因：把 maskEffect/skeleton.json（文本）当**二进制**读 → 解析出垃圾
//   （bones=9 slots=9 ik=123）→ Skeleton ctor 索引越界 → SIGSEGV。
// 修法：按**内容**判定格式（首个非空白字节是 '{' 即 JSON），分别派发。
bool wjjh_spine38_looks_json(const char* path) {
    cocos2d::Data d = FileUtils::getInstance()->getDataFromFile(path);
    if (d.isNull() || d.getSize() == 0) return false;
    const unsigned char* p = static_cast<const unsigned char*>(d.getBytes());
    const ssize_t n = static_cast<ssize_t>(d.getSize());
    for (ssize_t i = 0; i < n; ++i) {
        const unsigned char c = p[i];
        if (c == ' ' || c == '\t' || c == '\r' || c == '\n') continue;
        if (c == 0xEF || c == 0xBB || c == 0xBF) continue;  // UTF-8 BOM
        return c == '{';
    }
    return false;
}

// ---- gh221 干跑（dry run）----
// 只解析 SkeletonData，**不构造 Skeleton**（构造才是 run 220 SIGSEGV 的地方），因此绝对安全。
// 目的有二：
//   1) 诊断：readSkeletonData 到底返回 NULL 还是有效数据？失败原因（getError()）是什么？
//   2) 兜底门闸：createBinary 先干跑，失败就直接回退哑节点，绝不进入会崩的构造路径。
// 返回 true 表示"可以安全构造 Skeleton"，msg 里带完整诊断串。
bool wjjh_spine38_dry_run(const char* skel, const char* atlas, std::string& msg) {
    auto* fu = FileUtils::getInstance();
    const bool skelOk = fu->isFileExist(skel);
    const bool atlasOk = fu->isFileExist(atlas);

    msg = "skel=" + std::string(skel) + "(exists=" + (skelOk ? "1" : "0") + ")"
        + " atlas=" + std::string(atlas) + "(exists=" + (atlasOk ? "1" : "0") + ")";
    if (!skelOk || !atlasOk) {
        msg += " -> SKIP missing file";
        return false;
    }

    // 不带 textureLoader / 不建纹理：只解析 .atlas 文本，拿到 region 表。
    // 路径解析走 Cocos2dExtension::_readFile -> FileUtils，与真实加载完全一致。
    spine38::Atlas probeAtlas(spine38::String(atlas), NULL, false);
    msg += " atlasPages=" + std::to_string(probeAtlas.getPages().size());
    msg += " atlasRegions=" + std::to_string(probeAtlas.getRegions().size());

    spine38::AtlasAttachmentLoader loader(&probeAtlas);
    const bool isJson = wjjh_spine38_looks_json(skel);
    msg += isJson ? " fmt=json" : " fmt=bin";

    spine38::SkeletonData* data = NULL;
    std::string err;
    if (isJson) {
        spine38::SkeletonJson json(&loader);
        data = json.readSkeletonDataFile(spine38::String(skel));
        if (data == NULL) {
            const char* e = json.getError().buffer();
            err = (e && *e) ? e : "(empty)";
        }
    } else {
        spine38::SkeletonBinary binary(&loader);
        data = binary.readSkeletonDataFile(spine38::String(skel));
        if (data == NULL) {
            const char* e = binary.getError().buffer();
            err = (e && *e) ? e : "(empty)";
        }
    }

    if (data == NULL) {
        msg += " -> readSkeletonData=NULL err=" + err;
        return false;
    }

    msg += " -> OK bones=" + std::to_string(data->getBones().size());
    msg += " slots=" + std::to_string(data->getSlots().size());
    msg += " skins=" + std::to_string(data->getSkins().size());
    msg += " defaultSkin=" + std::string(data->getDefaultSkin() ? "1" : "0");
    msg += " anims=" + std::to_string(data->getAnimations().size());
    const char* v = data->getVersion().buffer();
    msg += " version=";
    msg += (v && *v) ? v : "?";
    // gh263: 附件类型构成——黄色方块问题的排查线索（mesh 解析坏=整条 mesh 化的人物
    //   渲染异常；若 region/mesh 数与 atlasRegions 严重不符=解析不完整）。
    {
        int nRegion = 0, nMesh = 0, nOther = 0;
        for (size_t si = 0; si < data->getSkins().size(); ++si) {
            spine38::Skin* skin = data->getSkins()[si];
            if (!skin) continue;
            auto entries = skin->getAttachments();
            while (entries.hasNext()) {
                spine38::Attachment* a = entries.next()._attachment;
                if (dynamic_cast<spine38::RegionAttachment*>(a)) ++nRegion;
                else if (dynamic_cast<spine38::MeshAttachment*>(a)) ++nMesh;
                else ++nOther;
            }
        }
        msg += " att(region=" + std::to_string(nRegion)
             + ",mesh=" + std::to_string(nMesh)
             + ",other=" + std::to_string(nOther) + ")";
    }
    delete data;
    return true;
}

// ---- gh263: SkeletonData 缓存 —— 治「点切磋要反应好几秒」----
// 每次创建都在重复两件重活：① dry-run 全量解析一遍（诊断用）② createWith*File 再全量
//   解析一遍；gongfu.json（488 骨/446 槽/5969 mesh 附件）单次解析即秒级，×2 路径 ×每场
//   4 个实例。同 (skel,atlas,scale) 的 SkeletonData 是只读的，缓存后：
//   * 命中 ⇒ 直接 createWithData(data, false)（不重读文件、不重建附件、不干跑）；
//   * 附件里的 Texture2D 引用随数据共享 ⇒ 每场战斗的骨架 CPU 内存也大幅下降。
//   只读性：Skeleton 实例只读 data；AnimationState 各自持有；皮肤/染色都在实例侧。
// ★ gh264 修复：此前读写各写了一个函数内 static map（=两个不同的 map，存 A 读 B，
//   gh263 实测 cache hit=0 从未命中）。统一放到命名空间作用域。
static std::map<std::string, spine38::SkeletonData*>& wjjh_spine38_skel_cache() {
    static std::map<std::string, spine38::SkeletonData*> s_cache;
    return s_cache;
}

// ---- createBinary(skel, atlas, scale) -> node(cc.Node), skelptr(lightuserdata) ----
// 失败只返回 nil（Lua 侧回退哑节点）。
int l_createBinary(lua_State* L) {
    const char* skel = luaL_checkstring(L, 1);
    const char* atlas = luaL_checkstring(L, 2);
    float scale = (float)luaL_optnumber(L, 3, 1.0);
    const std::string cacheKey = std::string(skel) + "|" + atlas + "|"
        + std::to_string(scale);

    // gh263: 缓存命中 = 跳过 dry-run 与文件解析，直接从共享 SkeletonData 构造。
    spine38::SkeletonData* cached = nullptr;
    {
        auto& cache = wjjh_spine38_skel_cache();
        auto it = cache.find(cacheKey);
        if (it != cache.end()) cached = it->second;
    }
    if (cached != nullptr) {
        fprintf(stderr, "WJJH_SPINE38: cache hit skel=%s\n", skel);
        spine38::SkeletonAnimation* sk = nullptr;
        try {
            sk = spine38::SkeletonAnimation::createWithData(cached, false);
        } catch (...) {
            sk = nullptr;
        }
        if (sk != nullptr && sk->getSkeleton() != nullptr) {
            sk->setTwoColorTint(false);  // gh263: 同下，two-color 着色器在 3.15.1 下不可靠
            Node* container = Node::create();
            if (container != nullptr) {
                container->addChild(sk);
                lua_State* mainL = LuaEngine::getInstance()->getLuaStack()->getLuaState();
                LuaEngine::getInstance()->getLuaStack()->pushObject(container, "cc.Node");
                if (mainL != L) lua_xmove(mainL, L, 1);
                lua_pushlightuserdata(L, reinterpret_cast<void*>(sk));
                return 2;
            }
        }
        fprintf(stderr, "WJJH_SPINE38: cache-hit create failed (skel=%s) -> fallback full path\n", skel);
    }

    // 先验存在性 + 干跑门闸：
    //   initWithBinaryFile 里 CCASSERT 在 Release(COCOS2D_DEBUG=0) 是 no-op，
    //   readSkeletonData 失败返回 NULL 时会被原样送进 new Skeleton(NULL) → SIGSEGV（run 220）。
    //   这里先干跑一遍，读不出来就直接回退哑节点。
    {
        std::string msg;
        if (!wjjh_spine38_dry_run(skel, atlas, msg)) {
            fprintf(stderr, "WJJH_SPINE38: dry-run failed (%s) -> fallback dummy\n", msg.c_str());
            lua_pushnil(L);
            return 1;
        }
        fprintf(stderr, "WJJH_SPINE38: dry-run ok (%s)\n", msg.c_str());
    }

    spine38::SkeletonAnimation* sk = nullptr;
    spine38::SkeletonData* parsed = nullptr;
    // gh224: 按内容派发 —— 游戏资产混格式（.skel 二进制 / .json 文本），
    //   文本 JSON 必须走 createWithJsonFile，否则会被二进制读取器解析成垃圾并崩。
    const bool isJson = wjjh_spine38_looks_json(skel);
    fprintf(stderr, "WJJH_SPINE38: dispatch skel=%s fmt=%s\n", skel, isJson ? "json" : "bin");
    try {
        if (isJson) {
            sk = spine38::SkeletonAnimation::createWithJsonFile(std::string(skel), std::string(atlas), scale);
        } else {
            sk = spine38::SkeletonAnimation::createWithBinaryFile(std::string(skel), std::string(atlas), scale);
        }
    } catch (...) {
        sk = nullptr;
    }
    if (sk == nullptr || sk->getSkeleton() == nullptr || sk->getSkeleton()->getData() == nullptr) {
        fprintf(stderr, "WJJH_SPINE38: create failed (skel=%s atlas=%s) -> fallback dummy\n", skel, atlas);
        lua_pushnil(L);
        return 1;
    }
    // gh263: 解析产物入缓存。骨架数据与图集纹理由此跨实例共享；
    //   实例的 owns 标志关掉（数据/图集生命周期归缓存管，进程内数量有限永不释放），
    //   否则首个实例析构会 delete 共享数据/图集 → 后续实例悬垂崩溃。
    parsed = sk->getSkeleton()->getData();
    wjjh_spine38_skel_cache()[cacheKey] = parsed;
    sk->setTwoColorTint(false);  // gh263: two-color 着色器在 cocos 3.15.1 下渲染不可靠
                                 //   （实测表现=人物/特效成纯色方块），关掉走单色路径。
    sk->setSharedOwnership(false, false);

    // 容器 + 真 spine 子节点
    Node* container = Node::create();
    if (container == nullptr) {
        lua_pushnil(L);
        return 1;
    }
    container->addChild(sk);

    // 推到 Lua：node(cc.Node) + skelptr(lightuserdata)
    //
    // gh229 关键修复：本函数经常在**协程**里被调用（游戏 third/coroutine 用真 Lua
    //   coroutine.create/resume），此时 L 是**协程的** lua_State；而
    //   LuaStack::pushObject 推到的是 LuaStack 自己的**主状态** _state。
    //   两者不一致 ⇒ 返回值错位：node 实际变成栈上残留的参数（scale），
    //   skelptr 才拿到 lightuserdata ⇒ 上层 tolua.setpeer(node,…) 报
    //   「Invalid argument #1 to setpeer: userdata expected」⇒ 游戏永远拿不到我们的节点
    //   （run 226–229 每次真 spine 创建都踩这个，之前被 xpcall 吞掉）。
    //   修法：仍然用 pushObject（保留它正确的 cocos 对象所有权处理），
    //   再用标准 C API lua_xmove 把刚压入的值从主状态搬到当前 L（协程间共享同一 global state）。
    lua_State* mainL = LuaEngine::getInstance()->getLuaStack()->getLuaState();
    LuaEngine::getInstance()->getLuaStack()->pushObject(container, "cc.Node");
    if (mainL != L) lua_xmove(mainL, L, 1);
    lua_pushlightuserdata(L, reinterpret_cast<void*>(sk));
    return 2;
}

// ---- gh242: createBinaryMetaOnly(skel, atlas, scale) ----
// 与 createBinary 完全相同，唯一区别：创建期间打开"跳过纹理加载"开关。
// 用途：游戏 AnimResManager.lua 的 initAnimResInfo() 在模块加载时遍历 animRes
//   表（2138 条 / 4 个图集）读取元数据（时长/事件/攻击距离），却在启动期把
//   4 个图集的**全部纹理**装载常驻。gongfu1 单图集 1145 页 × 512² 就 1145MB，
//   合计超过 2GB 设备 ~1.4GB 单进程上限 ⇒ 载入 88% 被 Jetsam 杀。
//   元数据 API（getAnimDuration/getAnimEvents/getBoneSetupPosePosition）只依赖
//   SkeletonData，不依赖纹理 ⇒ 本模式可安全替代，启动期纹理占用降为 0。
// 返回值语义与 createBinary 完全一致（node + skelptr），上层无需改动。
static int l_createBinaryMetaOnly(lua_State* L) {
    const char* skel = luaL_checkstring(L, 1);
    const char* atlas = luaL_checkstring(L, 2);
    float scale = (float)luaL_optnumber(L, 3, 1.0);

    // 同 createBinary：先干跑门闸（它本身就不建纹理），失败即回退哑节点。
    {
        std::string msg;
        if (!wjjh_spine38_dry_run(skel, atlas, msg)) {
            fprintf(stderr, "WJJH_SPINE38 [meta-only]: dry-run failed (%s) -> nil\n", msg.c_str());
            lua_pushnil(L);
            return 1;
        }
    }

    const bool isJson = wjjh_spine38_looks_json(skel);
    fprintf(stderr, "WJJH_SPINE38 [meta-only]: create skel=%s fmt=%s (no texture)\n",
            skel, isJson ? "json" : "bin");

    spine38::SkeletonAnimation* sk = nullptr;
    // ★ 打开开关：整段创建期间不加载任何图集纹理。
    spine38::wjjhSpine38SkipTextureLoad(true);
    try {
        if (isJson) {
            sk = spine38::SkeletonAnimation::createWithJsonFile(std::string(skel), std::string(atlas), scale);
        } else {
            sk = spine38::SkeletonAnimation::createWithBinaryFile(std::string(skel), std::string(atlas), scale);
        }
    } catch (...) {
        sk = nullptr;
    }
    // ★ 立即复位，绝不能影响后续正常创建。
    spine38::wjjhSpine38SkipTextureLoad(false);

    if (sk == nullptr || sk->getSkeleton() == nullptr || sk->getSkeleton()->getData() == nullptr) {
        fprintf(stderr, "WJJH_SPINE38 [meta-only]: create failed (skel=%s) -> nil\n", skel);
        lua_pushnil(L);
        return 1;
    }
    fprintf(stderr, "WJJH_SPINE38 [meta-only]: OK skel=%s\n", skel);

    Node* container = Node::create();
    if (container == nullptr) {
        lua_pushnil(L);
        return 1;
    }
    container->addChild(sk);

    // 与 l_createBinary 同一套 pushObject + lua_xmove（协程安全），保证返回值语义一致。
    lua_State* mainL = LuaEngine::getInstance()->getLuaStack()->getLuaState();
    LuaEngine::getInstance()->getLuaStack()->pushObject(container, "cc.Node");
    if (mainL != L) lua_xmove(mainL, L, 1);
    lua_pushlightuserdata(L, reinterpret_cast<void*>(sk));
    return 2;
}

// ---- setAnimation(skelptr, trackIndex, name, loop) ----
int l_setAnimation(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    if (!sk) return 0;
    int track = (int)luaL_checknumber(L, 2);
    const char* name = luaL_checkstring(L, 3);
    bool loop = lua_toboolean(L, 4) != 0;
    // gh231 取证：证明游戏确实在驱动**我们返回的**节点播动画（限 40 条，避免每帧刷屏）。
    static int s_animLogged = 0;
    if (s_animLogged < 40) {
        fprintf(stderr, "WJJH_SPINE38: setAnimation track=%d name=%s loop=%d\n", track, name, loop ? 1 : 0);
        ++s_animLogged;
    }
    sk->setAnimation(track, std::string(name), loop);
    return 0;
}

// ---- addAnimation(skelptr, trackIndex, name, loop, delay) ----
int l_addAnimation(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    if (!sk) return 0;
    int track = (int)luaL_checknumber(L, 2);
    const char* name = luaL_checkstring(L, 3);
    bool loop = lua_toboolean(L, 4) != 0;
    float delay = (float)luaL_optnumber(L, 5, 0.0);
    sk->addAnimation(track, std::string(name), loop, delay);
    return 0;
}

// ---- setTimeScale(skelptr, scale) ----
int l_setTimeScale(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    if (!sk) return 0;
    sk->setTimeScale((float)luaL_checknumber(L, 2));
    return 0;
}

// ---- setTrackTime(skelptr, trackIndex, time) —— 游戏手驱姿势的核心 ----
int l_setTrackTime(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    if (!sk || sk->getState() == nullptr || sk->getSkeleton() == nullptr) return 0;
    int track = (int)luaL_checknumber(L, 2);
    float t = (float)luaL_checknumber(L, 3);
    spine38::TrackEntry* e = sk->getState()->getCurrent((size_t)track);
    if (e) {
        e->setTrackTime(t);
        // 立即把该时间的姿势应用到骨架，避免等下一帧 scheduler 才刷新
        sk->getState()->apply(*sk->getSkeleton());
        sk->getSkeleton()->updateWorldTransform();
    }
    return 0;
}

// ---- getAnimDuration(skelptr, name) -> number ----
int l_getAnimDuration(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    const char* name = luaL_checkstring(L, 2);
    float d = 0.0f;
    if (sk) {
        spine38::Animation* a = sk->findAnimation(std::string(name));
        if (a) d = a->getDuration();
    }
    lua_pushnumber(L, (lua_Number)d);
    return 1;
}

// ---- getAnimEvents(skelptr, name) -> table of {name,time,intValue,floatValue,stringValue} ----
int l_getAnimEvents(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    const char* name = luaL_checkstring(L, 2);
    lua_newtable(L);
    int n = 0;
    if (sk) {
        spine38::Animation* a = sk->findAnimation(std::string(name));
        if (a) {
            spine38::Vector<spine38::Timeline*>& tls = a->getTimelines();
            for (size_t i = 0; i < tls.size(); ++i) {
                spine38::EventTimeline* et = dynamic_cast<spine38::EventTimeline*>(tls[i]);
                if (et == nullptr) continue;
                // getFrames() 返回 const Vector<float>&，而 Vector 只有非 const operator[]，
                // 拷一份出来索引（getAnimEvents 仅在初始化期调用，成本可忽略）。
                spine38::Vector<float> frames(et->getFrames());
                spine38::Vector<spine38::Event*>& events = et->getEvents();
                for (size_t j = 0; j < events.size(); ++j) {
                    spine38::Event* ev = events[j];
                    if (ev == nullptr) continue;
                    lua_newtable(L);
                    lua_pushstring(L, cstr_of(ev->getData().getName()));
                    lua_setfield(L, -2, "name");
                    lua_pushnumber(L, (lua_Number)((j < frames.size()) ? frames[j] : 0.0f));
                    lua_setfield(L, -2, "time");
                    lua_pushinteger(L, (lua_Integer)ev->getIntValue());
                    lua_setfield(L, -2, "intValue");
                    lua_pushnumber(L, (lua_Number)ev->getFloatValue());
                    lua_setfield(L, -2, "floatValue");
                    lua_pushstring(L, cstr_of(ev->getStringValue()));
                    lua_setfield(L, -2, "stringValue");
                    lua_rawseti(L, -2, ++n);
                }
            }
        }
    }
    return 1;
}

// ---- getBoneSetupPosePosition(skelptr, animName, boneName) -> {x,y} ----
// 游戏用法：getBoneSetupPosePosition(animName, "AttackPosition")，取 x 作攻击距离。
// animName 仅占位（setup pose 与动画无关），真正用的是 boneName（第 3 参）。
int l_getBoneSetupPosePosition(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    const char* boneName = lua_tostring(L, 3);
    if (boneName == nullptr) boneName = lua_tostring(L, 2); // 兜底：只有 2 参时按 boneName 处理
    float x = 0.0f, y = 0.0f;
    if (sk && sk->getSkeleton() && boneName) {
        spine38::Bone* b = sk->getSkeleton()->findBone(spine38::String(boneName));
        if (b) {
            x = b->getData().getX();
            y = b->getData().getY();
        }
    }
    lua_newtable(L);
    lua_pushnumber(L, (lua_Number)x);
    lua_setfield(L, -2, "x");
    lua_pushnumber(L, (lua_Number)y);
    lua_setfield(L, -2, "y");
    return 1;
}

// ---- YX 专有补充（真节点上也给到，避免 Lua 侧 "call nil"）----
int l_setSlotsToSetupPose(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    if (sk) sk->setSlotsToSetupPose();
    return 0;
}
int l_setBonesToSetupPose(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    if (sk) sk->setBonesToSetupPose();
    return 0;
}
int l_setToSetupPose(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    if (sk) sk->setToSetupPose();
    return 0;
}
int l_updateWorldTransform(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    if (sk) sk->updateWorldTransform();
    return 0;
}
int l_setAttachment(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    if (!sk) return 0;
    const char* slot = luaL_checkstring(L, 2);
    const char* att = luaL_checkstring(L, 3);
    sk->setAttachment(std::string(slot), std::string(att));
    return 0;
}

// ---- setSlotColor(skelptr, slotName, r, g, b, a) ----
// gh233: 游戏大量使用 YXSkeletonAnimation:setSlotColor(slotName, cc.c4f(r,g,b,a)) 给战斗
//   角色染色（把 "body"/"body_back" 染成黄色等表示状态）。此前这里是空实现 → 战斗角色
//   颜色不对。cc.c4f 返回的是普通 Lua table {r=,g=,b=,a=}（gamelua/src/cocos/cocos2d/
//   Cocos2d.lua:288），四个分量由 Lua 侧取好再传进来；spine 的 Color::set 会自动 clamp 到 0..1。
int l_setSlotColor(lua_State* L) {
    spine38::SkeletonAnimation* sk = skel_of(L, 1);
    if (!sk || !sk->getSkeleton()) return 0;
    const char* slotName = luaL_optstring(L, 2, "");
    const float r = (float)luaL_optnumber(L, 3, 1.0);
    const float g = (float)luaL_optnumber(L, 4, 1.0);
    const float b = (float)luaL_optnumber(L, 5, 1.0);
    const float a = (float)luaL_optnumber(L, 6, 1.0);
    spine38::Slot* slot = sk->getSkeleton()->findSlot(spine38::String(slotName));
    if (!slot) {
        static int s_miss = 0;
        if (s_miss < 10) {
            fprintf(stderr, "WJJH_SPINE38: setSlotColor slot not found: '%s'\n", slotName);
            ++s_miss;
        }
        return 0;
    }
    slot->getColor().set(r, g, b, a);
    return 0;
}

// ---- probe(skel, atlas) -> string|nil —— gh221 干跑诊断 ----
// 每个 (skel,atlas) 组合只跑一次（最多 24 组），避免每帧刷屏 / 拖慢。
int l_probe(lua_State* L) {
    const char* skel = luaL_optstring(L, 1, "");
    const char* atlas = luaL_optstring(L, 2, "");

    static std::set<std::string> probed;
    std::string key = std::string(skel) + "|" + atlas;
    if (probed.count(key) || probed.size() >= 24) {
        lua_pushnil(L);
        return 1;
    }
    probed.insert(key);

    std::string msg;
    const bool ok = wjjh_spine38_dry_run(skel, atlas, msg);
    fprintf(stderr, "WJJH_SPINE38 PROBE ok=%d %s\n", ok ? 1 : 0, msg.c_str());
    lua_pushstring(L, msg.c_str());
    return 1;
}

} // namespace

void wjjh_spine38_register(lua_State* L) {
    lua_register(L, "wjjh_spine38_createBinary", l_createBinary);
    lua_register(L, "wjjh_spine38_createMetaOnly", l_createBinaryMetaOnly);
    lua_register(L, "wjjh_spine38_probe", l_probe);
    lua_register(L, "wjjh_spine38_setAnimation", l_setAnimation);
    lua_register(L, "wjjh_spine38_addAnimation", l_addAnimation);
    lua_register(L, "wjjh_spine38_setTimeScale", l_setTimeScale);
    lua_register(L, "wjjh_spine38_setTrackTime", l_setTrackTime);
    lua_register(L, "wjjh_spine38_getAnimDuration", l_getAnimDuration);
    lua_register(L, "wjjh_spine38_getAnimEvents", l_getAnimEvents);
    lua_register(L, "wjjh_spine38_getBoneSetupPosePosition", l_getBoneSetupPosePosition);
    lua_register(L, "wjjh_spine38_setSlotsToSetupPose", l_setSlotsToSetupPose);
    lua_register(L, "wjjh_spine38_setBonesToSetupPose", l_setBonesToSetupPose);
    lua_register(L, "wjjh_spine38_setToSetupPose", l_setToSetupPose);
    lua_register(L, "wjjh_spine38_updateWorldTransform", l_updateWorldTransform);
    lua_register(L, "wjjh_spine38_setAttachment", l_setAttachment);
    lua_register(L, "wjjh_spine38_setSlotColor", l_setSlotColor);
}
