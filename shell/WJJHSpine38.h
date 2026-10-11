// -*- coding: utf-8 -*-
// gh217/A3: spine 3.8.75 真运行时接入。
// 游戏原本走"哑节点工厂"(__mkSkel)，角色不播动画。这里提供真·spine38::SkeletonAnimation
// 节点的创建 + 游戏驱动所需的几个方法（含自定义 setTrackTime 手驱），并保留"失败回退哑节点"。
// 设计：C 侧只暴露一组 lua_register 函数，Lua 侧(shim)负责把节点包成 peer、挂方法，
//   与现有 YXSkeletonAnimation/spine38 的写法保持一致（不引入 tolua 类注册）。
#pragma once
#include <string>

struct lua_State;
namespace spine38 { class SkeletonAnimation; }

// 在 Lua 栈上注册 wjjh_spine38_* 一组 C 函数（createBinary / setAnimation /
// setTimeScale / setTrackTime / getAnimDuration / getAnimEvents /
// getBoneSetupPosePosition）。必须在 boot 的 Lua chunk 之前调用。
void wjjh_spine38_register(lua_State* L);
