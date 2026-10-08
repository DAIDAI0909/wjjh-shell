// gh145: lua_module_register.cpp 引用 register_extension_module（由 lua_cocos2dx_extension_auto
// 的 LUA_EXTENSION_REGISTER 宏生成）。该绑定文件因引用已排除的 AssetsManager 实现被整体排除，
// 这里提供空实现补符号。extension 的 lua 类（cc.AssetsManager 等）在 lua 层表现为 nil，
// 游戏主流程不使用（ccui.* 系由 UI 绑定提供，不受影响）。
#include "lua.h"

extern "C" void register_extension_module(lua_State* tolua_S)
{
    (void)tolua_S;
}
