// gh145/146: lua_module_register.cpp 以 C++ 方式引用 register_extension_module
// （原定义由 lua_cocos2dx_extension_auto 的注册宏生成，该绑定文件因引用已排除的
// AssetsManager 实现被整体排除）。提供 C++ 版空实现补符号（ld 提示需匹配 mangled 名）。
// gh148: 同理补 register_network_module —— 原定义在 lua_cocos2dx_network_manual.cpp，
// 该文件因引用已排除的 web_socket 绑定而排除。lua 侧网络能力走自研 WjjhXHR(cc.XMLHttpRequest)。
#include "lua.h"

void register_extension_module(lua_State* tolua_S)
{
    (void)tolua_S;
}

void register_network_module(lua_State* tolua_S)
{
    (void)tolua_S;
}
