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
#include "spine38/SkeletonData.h"

extern "C" {
#include "lauxlib.h"
}

#include <string>
#include <set>

USING_NS_CC;

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

// ---- gh221 干跑（dry run）----
// 只解析 SkeletonData，**不构造 Skeleton**（构造才是 run 220 SIGSEGV 的地方），因此绝对安全。
// 目的有二：
//   1) 诊断：readSkeletonData 到底返回 NULL 还是有效数据？失败原因（binary.getError()）是什么？
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
    spine38::SkeletonBinary binary(&loader);
    spine38::SkeletonData* data = binary.readSkeletonDataFile(spine38::String(skel));

    if (data == NULL) {
        const char* err = binary.getError().buffer();
        msg += " -> readSkeletonData=NULL err=";
        msg += (err && *err) ? err : "(empty)";
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
    delete data;
    return true;
}

// ---- createBinary(skel, atlas, scale) -> node(cc.Node), skelptr(lightuserdata) ----
// 失败只返回 nil（Lua 侧回退哑节点）。
int l_createBinary(lua_State* L) {
    const char* skel = luaL_checkstring(L, 1);
    const char* atlas = luaL_checkstring(L, 2);
    float scale = (float)luaL_optnumber(L, 3, 1.0);

    // 先验存在性 + 干跑门闸：
    //   initWithBinaryFile 里 CCASSERT 在 Release(COCOS2D_DEBUG=0) 是 no-op，
    //   readSkeletonData 失败返回 NULL 时会被原样送进 new Skeleton(NULL) → SIGSEGV（run 220）。
    //   这里先干跑一遍，读不出来就直接回退哑节点。
    {
        std::string msg;
        if (!wjjh_spine38_dry_run(skel, atlas, msg)) {
            CCLOG("WJJH_SPINE38: dry-run failed (%s) -> fallback dummy", msg.c_str());
            lua_pushnil(L);
            return 1;
        }
        CCLOG("WJJH_SPINE38: dry-run ok (%s)", msg.c_str());
    }

    spine38::SkeletonAnimation* sk = nullptr;
    try {
        sk = spine38::SkeletonAnimation::createWithBinaryFile(std::string(skel), std::string(atlas), scale);
    } catch (...) {
        sk = nullptr;
    }
    if (sk == nullptr || sk->getSkeleton() == nullptr || sk->getSkeleton()->getData() == nullptr) {
        CCLOG("WJJH_SPINE38: create failed (skel=%s atlas=%s) -> fallback dummy", skel, atlas);
        lua_pushnil(L);
        return 1;
    }

    // 容器 + 真 spine 子节点
    Node* container = Node::create();
    if (container == nullptr) {
        lua_pushnil(L);
        return 1;
    }
    container->addChild(sk);

    // 推到 Lua：node(cc.Node) + skelptr(lightuserdata)
    LuaEngine::getInstance()->getLuaStack()->pushObject(container, "cc.Node");
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
    CCLOG("WJJH_SPINE38 PROBE ok=%d %s", ok ? 1 : 0, msg.c_str());
    lua_pushstring(L, msg.c_str());
    return 1;
}

} // namespace

void wjjh_spine38_register(lua_State* L) {
    lua_register(L, "wjjh_spine38_createBinary", l_createBinary);
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
}
