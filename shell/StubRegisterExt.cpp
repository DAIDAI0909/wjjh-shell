// gh148/152: lua_module_register.cpp 以 C++ 方式引用 register_network_module ——
// 原定义在 lua_cocos2dx_network_manual.cpp，该文件因引用已排除的 web_socket 绑定
// (tolua_web_socket_open) 而整体排除。这里给 C++ 版空实现补符号。
// lua 侧网络能力走自研 WjjhXHR(cc.XMLHttpRequest) + WjjhJM。
//
// 注：register_extension_module 曾在此补桩(gh145/146)，gh152 恢复 extension 绑定后
// 已由 lua_cocos2dx_extension_manual.cpp 提供真定义，桩移除（否则 duplicate symbol）。
#include "lua.h"

void register_network_module(lua_State* tolua_S)
{
    (void)tolua_S;
}
