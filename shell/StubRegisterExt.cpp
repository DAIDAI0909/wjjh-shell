// gh145/146: lua_module_register.cpp 以 C++ 方式引用 register_extension_module
// （原定义由 lua_cocos2dx_extension_auto 的注册宏生成，该绑定文件因引用已排除的
// AssetsManager 实现被整体排除）。提供 C++ 版空实现补符号（ld 提示需匹配 mangled 名）。
#include "lua.h"

void register_extension_module(lua_State* tolua_S)
{
    (void)tolua_S;
}
