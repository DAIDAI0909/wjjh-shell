cmake_minimum_required(VERSION 3.24)
project(WJJH CXX C OBJC OBJCXX)

# ---- arm64 模拟器原生路线(gh72,老板拍板 A 线)----
# yml 写死 -DPLATFORM=SIMULATOR64 且无 workflow scope 改不了 -> 在这里强设架构覆盖：
# 工具链在 configure 早期已按 PLATFORM 选了 x86_64，此处 FORCE 后各 target 生成期改用 arm64。
# 好处：甩掉 Rosetta；LuaJIT arm64 强制 GC64 布局；SIGSEGV(UAF 类堆损坏)大概率随 Rosetta 消失。
option(WJJH_ARM64 "Force arm64 iOS-simulator slice (Rosetta-free)" ON)
if(WJJH_ARM64)
    set(CMAKE_OSX_ARCHITECTURES "arm64" CACHE STRING "" FORCE)
    message(STATUS "== WJJH_ARM64=ON: forcing arm64 simulator build ==")
endif()

# CI 诊断日志路径：app 的 stderr/stdout 会 freopen 到这个文件（由各 CI 的 yaml 传入）
set(WJJH_LAUNCH_LOG "" CACHE STRING "CI launch log path for app stderr redirect")

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(ENGINE ${CMAKE_SOURCE_DIR}/cocos2d)

# ---- 下载 cocos 官方第三方预编译包（luajit/poly2tri/全部 .a 都在里面）----
if(NOT EXISTS ${ENGINE}/external/luajit/include/lua.h)
    message(STATUS "== downloading cocos2d-x v3-deps-130 ==")
    set(DEPS_URL "https://github.com/cocos2d/cocos2d-x-3rd-party-libs-bin/archive/v3-deps-172.zip")
    file(DOWNLOAD ${DEPS_URL} ${CMAKE_BINARY_DIR}/deps.zip SHOW_PROGRESS TIMEOUT 300 STATUS dl_st)
    list(GET dl_st 0 dl_code)
    if(NOT dl_code EQUAL 0)
        message(FATAL_ERROR "deps download failed: ${dl_st}")
    endif()
    file(ARCHIVE_EXTRACT INPUT ${CMAKE_BINARY_DIR}/deps.zip DESTINATION ${CMAKE_BINARY_DIR}/deps)
    file(GLOB DEP_TOP ${CMAKE_BINARY_DIR}/deps/*)
    list(LENGTH DEP_TOP dep_top_n)
    if(NOT dep_top_n EQUAL 1)
        message(FATAL_ERROR "deps zip 布局异常: ${DEP_TOP}")
    endif()
    file(GLOB DEP_ITEMS ${DEP_TOP}/*)
    foreach(item ${DEP_ITEMS})
        file(COPY ${item} DESTINATION ${ENGINE}/external)
    endforeach()
    message(STATUS "== v3-deps 归位完成 ==")

    # v3-deps-172 的 unzip 缺 cocos 的 unzOpenBuffer/unz*64 API —— 用 130 版源码固定覆盖
    file(COPY ${CMAKE_SOURCE_DIR}/unzip_130/ DESTINATION ${ENGINE}/external/unzip)
    # 172 的新版 flatbuffers 头与 cocos 3.15 生成头不兼容 —— 同样固定 130 版
    file(COPY ${CMAKE_SOURCE_DIR}/flatbuffers_130/ DESTINATION ${ENGINE}/external/flatbuffers)
    # 172 的新版 LuaJIT 头没有 cocos 用的 luaL_reg —— 固定 130 版头文件（lib 用我们源码现编的）
    file(COPY ${CMAKE_SOURCE_DIR}/luajit_inc_130/ DESTINATION ${ENGINE}/external/lua/luajit/include)

    # ---- gh139: libpng 源码现编前置(在 fix_prebuilts 之前,让探针也看到新库) ----
    if(WJJH_ARM64)
        find_program(PY3B_EXE python3)
        if(PY3B_EXE)
            execute_process(COMMAND ${PY3B_EXE} ${CMAKE_SOURCE_DIR}/scripts/build_png_source.py ${ENGINE}
                            RESULT_VARIABLE png_r OUTPUT_VARIABLE png_out ERROR_VARIABLE png_out
                            TIMEOUT 420)
            message(STATUS "png-source-build:\n${png_out}")
        endif()
    endif()

    # ---- 预编译库平台修补：arm64 模拟器路线需要（切片来自 fat 库的 arm64，平台声明是
    #      2017 真机构建——Xcode 26 链接器拒收，统一重建为 iOS-simulator LC_BUILD_VERSION）----
    if(WJJH_ARM64)
        find_program(PY3_EXE python3)
        if(PY3_EXE)
            execute_process(COMMAND ${PY3_EXE} ${CMAKE_SOURCE_DIR}/scripts/fix_prebuilts.py ${ENGINE}
                            RESULT_VARIABLE fp_r OUTPUT_VARIABLE fp_out ERROR_VARIABLE fp_out
                            TIMEOUT 300)
            if(NOT fp_r EQUAL 0)
                message(FATAL_ERROR "fix_prebuilts.py failed (r=${fp_r}):\n${fp_out}")
            else()
                message(STATUS "prebuilt platform patch:\n${fp_out}")
            endif()
        endif()
    endif()

    # ---- 从源码构建 LuaJIT 2.1.0-beta3 替换 2017 预编译库 ----
    # 预编译库的 lj_vm.o 带 macOS 平台标记（且对象头非标准），新链接器拒绝链进 iOS 模拟器；
    # 现代 clang 按源码现编则平台标记天然正确。API 同为 Lua 5.1，与 tolua/绑定完全兼容。
    set(LJ_A ${ENGINE}/external/lua/luajit/prebuilt/ios/libluajit.a)
    find_program(MAKE_EXE make)
    if(MAKE_EXE AND EXISTS ${LJ_A} AND NOT EXISTS ${CMAKE_BINARY_DIR}/luajit/libluajit.a)
        set(LJ_DIR ${CMAKE_BINARY_DIR}/luajit-src)
        file(REMOVE_RECURSE ${LJ_DIR})
        set(ljclone_r 128)
        foreach(attempt 1 2 3)
            execute_process(COMMAND git clone --depth 1 -b v2.1 https://github.com/LuaJIT/LuaJIT.git ${LJ_DIR}
                            RESULT_VARIABLE ljclone_r OUTPUT_QUIET ERROR_VARIABLE ljclone_err TIMEOUT 300)
            if(ljclone_r EQUAL 0)
                break()
            endif()
            message(WARNING "luajit clone attempt ${attempt} failed (r=${ljclone_r}): ${ljclone_err}")
            execute_process(COMMAND ${CMAKE_COMMAND} -E sleep 5)
            file(REMOVE_RECURSE ${LJ_DIR})
        endforeach()
        if(NOT ljclone_r EQUAL 0)
            message(FATAL_ERROR "luajit clone failed after retries")
        endif()
        execute_process(COMMAND xcrun -sdk iphonesimulator -show-sdk-path
                        OUTPUT_VARIABLE SIM_SDK OUTPUT_STRIP_TRAILING_WHITESPACE)
        if(WJJH_ARM64)
            set(LJ_TARGET arm64)
            set(LJ_TARCH "arm64-apple-ios13.0-simulator")
            # LuaJIT arm64 强制 GC64 布局(lj_arch.h),显式声明保两侧一致
            set(LJ_EXTRA_FLAGS "-DLUAJIT_ENABLE_GC64")
        else()
            set(LJ_TARGET x86_64)
            set(LJ_TARCH "x86_64-apple-ios13.0-simulator")
            set(LJ_EXTRA_FLAGS "-DLUAJIT_ENABLE_GC64 -DLUAJIT_USE_SYSMALLOC")
        endif()
        set(LJ_TFLAGS "-target ${LJ_TARCH} -isysroot ${SIM_SDK}")
        execute_process(COMMAND ${MAKE_EXE} TARGET=${LJ_TARGET} TARGET_OS=iOS
                        "TARGET_FLAGS=${LJ_TFLAGS} ${LJ_EXTRA_FLAGS}"
                        "TARGET_LDFLAGS=${LJ_TFLAGS}"
                        # gh69 实锤:Lua 堆对象被写坏(lj_BC_JFUNCV 调垃圾地址) -> GC64 根修;
                        # 两侧都要一致：目标侧靠上面的 -D，buildvm（主机编译）靠 HOST_CC 的 -D，
                        # 否则 lj_libdef.h 与源码不一致（x86_64 时代踩过的坑）
                        "HOST_CC=clang -DLUAJIT_DISABLE_JIT ${LJ_EXTRA_FLAGS}"
                        "MACOSX_DEPLOYMENT_TARGET=13.0"
                        WORKING_DIRECTORY ${LJ_DIR}
                        RESULT_VARIABLE ljmk_r
                        OUTPUT_VARIABLE ljmk_out ERROR_VARIABLE ljmk_out
                        TIMEOUT 600)
        if(NOT ljmk_r EQUAL 0 OR NOT EXISTS ${LJ_DIR}/src/libluajit.a)
            string(LENGTH "${ljmk_out}" _len)
            math(EXPR _start "${_len} - 1500")
            if(_start LESS 0)
                set(_start 0)
            endif()
            string(SUBSTRING "${ljmk_out}" ${_start} 1500 _tail)
            message(FATAL_ERROR "luajit source build failed (r=${ljmk_r}) in ${LJ_DIR}:\n${_tail}")
        endif()
        file(COPY_FILE ${LJ_DIR}/src/libluajit.a ${LJ_A})
        message(STATUS "built libluajit.a from source (v2.1.0-beta3, ${LJ_TARGET} ios-sim GC64) -> ${LJ_A}")
    endif()
endif()

# ---- 引擎源码补丁：cocos2d-x 3.15 对新 SDK 的 iconv 类型修正 ----
set(FONT_H ${ENGINE}/cocos/2d/CCFontAtlas.h)
if(EXISTS ${FONT_H})
    file(READ ${FONT_H} _ftxt)
    set(_ftxt_orig "${_ftxt}")
    string(REPLACE "void* _iconv;" "iconv_t _iconv;" _ftxt "${_ftxt}")
    string(REPLACE "#ifndef" "#include <iconv.h>\n#ifndef" _ftxt "${_ftxt}")
    if(NOT _ftxt STREQUAL _ftxt_orig)
        file(WRITE ${FONT_H} "${_ftxt}")
        message(STATUS "patched CCFontAtlas.h: iconv_t + include <iconv.h>")
    endif()
endif()

# ---- 引擎源码补丁：apple 音频头缺 <functional> ----
set(AUDIO_H ${ENGINE}/cocos/audio/apple/AudioCache.h)
if(EXISTS ${AUDIO_H})
    file(READ ${AUDIO_H} _atxt)
    if(NOT _atxt MATCHES "include <functional>")
        file(WRITE ${AUDIO_H} "#include <functional>\n${_atxt}")
        message(STATUS "patched AudioCache.h: include <functional>")
    endif()
endif()

# ---- 引擎源码补丁：CCFileUtils removeDirectory 的 system() 在 iOS 不可用 ----
set(FUT_CPP ${ENGINE}/cocos/platform/CCFileUtils.cpp)
if(EXISTS ${FUT_CPP})
    file(READ ${FUT_CPP} _ftxt2)
    if(_ftxt2 MATCHES "#if !defined\\(CC_TARGET_OS_TVOS\\)")
        string(REPLACE "#if !defined(CC_TARGET_OS_TVOS)"
                       "#if 0 // patched: system() unavailable on iOS" _ftxt2 "${_ftxt2}")
        file(WRITE ${FUT_CPP} "${_ftxt2}")
        message(STATUS "patched CCFileUtils.cpp: removeDirectory guard")
    endif()
endif()

# ---- 引擎源码补丁：CCDevice-apple.h 缺 UIKit 导入（原工程靠 pch）----
set(DEVH ${ENGINE}/cocos/platform/apple/CCDevice-apple.h)
if(EXISTS ${DEVH})
    file(READ ${DEVH} _dtxt)
    if(NOT _dtxt MATCHES "import <UIKit/UIKit.h>")
        file(WRITE ${DEVH} "#import <UIKit/UIKit.h>\n${_dtxt}")
        message(STATUS "patched CCDevice-apple.h: import UIKit")
    endif()
endif()

# ---- 引擎源码补丁：apple 音频 .mm 缺 UIKit 导入（原工程靠 pch）----
foreach(amm ${ENGINE}/cocos/audio/apple/AudioEngine-inl.mm
            ${ENGINE}/cocos/audio/apple/AudioPlayer.mm
            ${ENGINE}/cocos/audio/apple/AudioCache.mm)
    if(EXISTS ${amm})
        file(READ ${amm} _mtxt)
        if(NOT _mtxt MATCHES "import <UIKit/UIKit.h>")
            file(WRITE ${amm} "#import <UIKit/UIKit.h>\n${_mtxt}")
            message(STATUS "patched ${amm}: import UIKit")
        endif()
    endif()
endforeach()

# ---- 游戏脚本树：不进 git（隔离），CI 构建时拉 gamelua/game.zip（src+res），本地构建直接用本地文件 ----
if(NOT EXISTS ${CMAKE_SOURCE_DIR}/gamelua/src)
    if(EXISTS ${CMAKE_SOURCE_DIR}/gamelua/game.zip)
        file(ARCHIVE_EXTRACT INPUT ${CMAKE_SOURCE_DIR}/gamelua/game.zip DESTINATION ${CMAKE_SOURCE_DIR}/gamelua)
        message(STATUS "extracted gamelua/game.zip -> gamelua/{src,res}")
    else()
        message(FATAL_ERROR "gamelua/src 不存在且没有 gamelua/game.zip —— 游戏脚本包未就绪")
    endif()
endif()

# ---- 游戏资源打进 bundle（src/ 与 res/ 在 Resources/ 下）----
set(GAME_RES_FILES "")
file(GLOB_RECURSE LUA_FILES ${CMAKE_SOURCE_DIR}/gamelua/*)
list(FILTER LUA_FILES EXCLUDE REGEX ".*\\.git.*")
foreach(f ${LUA_FILES})
    file(RELATIVE_PATH rel ${CMAKE_SOURCE_DIR}/gamelua ${f})
    get_filename_component(reldir "${rel}" DIRECTORY)
    set_source_files_properties(${f} PROPERTIES MACOSX_PACKAGE_LOCATION "Resources/${reldir}")
    list(APPEND GAME_RES_FILES ${f})
endforeach()

# ---- cocos2d-x 3.15 core sources ----
file(GLOB_RECURSE COCOS_SRC
    ${ENGINE}/cocos/*.cpp
    ${ENGINE}/cocos/*.c
    ${ENGINE}/cocos/*.mm
)
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/prebuilt/.*")
list(FILTER COCOS_SRC EXCLUDE REGEX
    ".*/platform/android/.*|.*/platform/win32/.*|.*/platform/winrt/.*|.*/platform/wp8/.*|.*/platform/linux/.*|.*/platform/desktop/.*|.*/platform/mac/.*|.*/platform/win8.1-universal/.*")
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/audio/android/.*|.*/audio/openal/.*|.*/audio/win32/.*|.*/audio/winrt/.*|.*/audio/linux/.*|.*/audio/mac/.*")
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/network/CCDownloader-android\\.cpp|.*/platform/android/jni/.*")
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/scripting/js-bindings/.*|.*/scripting/lua-bindings/.*")
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/UIEditBox/Mac/.*|.*/UIEditBox/Win32/.*|.*/UIEditBox/Android/.*|.*/UIEditBox/linux/.*")
list(FILTER COCOS_SRC EXCLUDE REGEX
    ".*/ui/UIWebView\\.cpp|.*/ui/UIWebViewImpl-tizen\\.cpp|.*/ui/UIEditBox/UIEditBoxImpl-tizen\\.cpp|.*/ui/UIVideoPlayer-tizen\\.cpp")
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/platform/tizen/.*|.*/audio/tizen/.*")
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/physics3d/.*|.*/physics/chipmunk/.*")
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/vr/.*")
# gh135: 斩 curl 引用(CCDownloader-curl 用 curl; iOS 有 apple 版走 NSURLSession)
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/network/CCDownloader-curl\\.cpp")
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/navmesh/.*")
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/network/HttpClient\\.cpp")
list(FILTER COCOS_SRC EXCLUDE REGEX ".*/editor-support/dragonbones/.*|.*/editor-support/script/.*")

file(GLOB_RECURSE COCOS_EXT_SRC
    ${ENGINE}/cocos/editor-support/spine/*.cpp
)

# ---- extensions/ 源码（lua_cocos2dx_extension_auto 引用其符号，不编则链接期 undefined）----
file(GLOB EX_SRC
    ${ENGINE}/extensions/*.cpp
    ${ENGINE}/extensions/assets-manager/*.cpp
    ${ENGINE}/extensions/GUI/CCControlExtension/*.cpp
    ${ENGINE}/extensions/GUI/CCScrollView/*.cpp
    ${ENGINE}/extensions/physics-nodes/*.cpp
)
file(GLOB_RECURSE EX_PARTICLE_SRC ${ENGINE}/extensions/Particle3D/*.cpp)
list(APPEND EX_SRC ${EX_PARTICLE_SRC})
# gh135: assets-manager(AssetsManagerEx)用 curl -> 排除(斩 curl 引用)
list(FILTER EX_SRC EXCLUDE REGEX ".*/assets-manager/.*")

# ---- external 单文件/源码库（v3-deps 带来的，无预编译 .a）----
file(GLOB_RECURSE UTIL_SRC
    ${ENGINE}/external/xxhash/*.c
    ${ENGINE}/external/unzip/*.c
    ${ENGINE}/external/unzip/*.cpp
    ${ENGINE}/external/md5/*.c
    ${ENGINE}/external/edtaa3func/*.cpp
    ${ENGINE}/external/ConvertUTF/*.c
    ${ENGINE}/external/ConvertUTF/*.cpp
    ${ENGINE}/external/clipper/*.cpp
    ${ENGINE}/external/tinyxml2/*.cpp
    ${ENGINE}/external/poly2tri/*.cc
    ${ENGINE}/external/poly2tri/*.cpp
    ${ENGINE}/external/recast/fastlz/*.c
    ${ENGINE}/external/recast/fastlz/*.cpp
)

# ---- ObjC 实现文件（.m 不在 .cpp/.mm glob 里，漏了就 OBJC_CLASS_$ undefined）----
file(GLOB OBJC_SRC
    ${ENGINE}/cocos/audio/ios/*.m
    ${ENGINE}/cocos/platform/ios/*.m
    ${ENGINE}/cocos/network/*.m
)

# ---- lua bindings + tolua + luasocket + xxtea ----
file(GLOB_RECURSE LUA_BIND_SRC
    ${ENGINE}/cocos/scripting/lua-bindings/manual/*.cpp
    ${ENGINE}/cocos/scripting/lua-bindings/manual/*.mm
    ${ENGINE}/cocos/scripting/lua-bindings/manual/*.c
    ${ENGINE}/cocos/scripting/lua-bindings/auto/*.cpp
    ${ENGINE}/external/lua/tolua/*.c
    ${ENGINE}/external/lua/luasocket/*.c
    ${ENGINE}/external/xxtea/*.cpp
)
list(FILTER LUA_BIND_SRC EXCLUDE REGEX ".*/lua_cocos2dx_physics3d_auto\\.cpp|.*/lua_cocos2dx_navmesh_auto\\.cpp")
list(FILTER LUA_BIND_SRC EXCLUDE REGEX ".*/manual/platform/android/.*|.*/manual/physics3d/.*|.*/manual/navmesh/.*|.*/manual/video/.*")
list(FILTER LUA_BIND_SRC EXCLUDE REGEX ".*/luasocket/wsocket\\.c")
# gh144: extension 绑定引用了已排除的 AssetsManager 等实现(undefined) - 排掉这两个绑定文件
list(FILTER LUA_BIND_SRC EXCLUDE REGEX ".*/lua_cocos2dx_extension_auto\\.cpp")
list(FILTER LUA_BIND_SRC EXCLUDE REGEX ".*/lua_cocos2dx_extension_manual\\.cpp")

file(GLOB SHELL_SRC
    ${CMAKE_SOURCE_DIR}/shell/*.mm
    ${CMAKE_SOURCE_DIR}/shell/*.cpp
    ${CMAKE_SOURCE_DIR}/shell/*.m
)

add_executable(WJJH MACOSX_BUNDLE
    ${COCOS_SRC} ${COCOS_EXT_SRC} ${EX_SRC} ${UTIL_SRC} ${OBJC_SRC} ${LUA_BIND_SRC} ${SHELL_SRC} ${GAME_RES_FILES}
)

# ---- cocos 2017 代码是 MRC：所有引擎 .mm/.m 关 ARC ----
foreach(f ${COCOS_SRC} ${COCOS_EXT_SRC} ${OBJC_SRC} ${LUA_BIND_SRC})
    if(f MATCHES "\\.mm$" OR f MATCHES "\\.m$")
        set_source_files_properties(${f} PROPERTIES COMPILE_OPTIONS "-fno-objc-arc")
    endif()
endforeach()

set_target_properties(WJJH PROPERTIES
    MACOSX_BUNDLE_GUI_IDENTIFIER "com.wjjh.ios"
    MACOSX_BUNDLE_BUNDLE_NAME "WJJH"
    MACOSX_BUNDLE_INFO_PLIST ${CMAKE_SOURCE_DIR}/shell/Info.plist.in
    XCODE_ATTRIBUTE_CODE_SIGNING_ALLOWED "NO"
    XCODE_ATTRIBUTE_CODE_SIGN_IDENTITY ""
    XCODE_ATTRIBUTE_TARGETED_DEVICE_FAMILY "1,2"
    XCODE_ATTRIBUTE_IPHONEOS_DEPLOYMENT_TARGET "13.0"
)

target_include_directories(WJJH PRIVATE
    ${CMAKE_SOURCE_DIR}/shell
    ${ENGINE}
    ${ENGINE}/cocos
    ${ENGINE}/cocos/platform
    ${ENGINE}/cocos/platform/ios
    ${ENGINE}/cocos/editor-support
    ${ENGINE}/extensions
    ${ENGINE}/extensions/GUI/CCControlExtension
    ${ENGINE}/cocos/scripting/lua-bindings/manual
    ${ENGINE}/cocos/scripting/lua-bindings/auto
    ${ENGINE}/cocos/scripting/lua-bindings/external
    ${ENGINE}/external
    ${ENGINE}/external/lua/luajit/include
    ${ENGINE}/external/glfw3/include
    ${ENGINE}/external/chipmunk/include
    ${ENGINE}/external/curl/include
    ${ENGINE}/external/png/include
    ${ENGINE}/external/jpeg/include
    ${ENGINE}/external/tiff/include
    ${ENGINE}/external/webp/include
    ${ENGINE}/external/freetype2/include
    ${ENGINE}/external/xxhash
    ${ENGINE}/external/unzip
    ${ENGINE}/external/convertUTF
    ${ENGINE}/external/edtaa3func
)

# ---- 穷举 external 头文件目录（两级），省得一个个追 ----
file(GLOB EXT_DIRS ${ENGINE}/external/*)
foreach(d ${EXT_DIRS})
    if(IS_DIRECTORY ${d})
        target_include_directories(WJJH PRIVATE ${d})
        file(GLOB EXT_SUB ${d}/*)
        foreach(s ${EXT_SUB})
            # msinttypes 是 MSVC 专用垫片，会遮住系统 stdint.h —— 跳过
            if(IS_DIRECTORY ${s} AND NOT s MATCHES "msinttypes")
                target_include_directories(WJJH PRIVATE ${s})
            endif()
        endforeach()
    endif()
endforeach()
file(GLOB LB_EXT_DIRS ${ENGINE}/cocos/scripting/lua-bindings/external/*)
foreach(d ${LB_EXT_DIRS})
    if(IS_DIRECTORY ${d})
        target_include_directories(WJJH PRIVATE ${d})
    endif()
endforeach()

# ---- 平台化头文件目录（freetype2 等第三方按 include/ios 分层）----
file(GLOB INC_IOS ${ENGINE}/external/*/include/ios)
foreach(d ${INC_IOS})
    target_include_directories(WJJH PRIVATE ${d})
    file(GLOB SUB ${d}/*)
    foreach(s ${SUB})
        if(IS_DIRECTORY ${s})
            target_include_directories(WJJH PRIVATE ${s})
        endif()
    endforeach()
endforeach()

target_compile_definitions(WJJH PRIVATE
    USE_FILE32API
    CC_ENABLE_CHIPMUNK_INTEGRATION=1
    CC_LUA_ENGINE_ENABLED=1
    CC_CONSTRUCTOR_ACCESS=public
    COCOS2D_DEBUG=0
    CC_USE_3D_PHYSICS=0
    CC_USE_NAVMESH=0
    CC_USE_WEBP=0
)

if(WJJH_LAUNCH_LOG)
    target_compile_definitions(WJJH PRIVATE WJJH_LAUNCH_LOG_PATH="${WJJH_LAUNCH_LOG}")
elseif(DEFINED ENV{GITHUB_ACTIONS})
    # GitHub Actions runner 的固定工作区路径
    target_compile_definitions(WJJH PRIVATE WJJH_LAUNCH_LOG_PATH="/Users/runner/work/wjjh-ios/wjjh-ios/launch.log")
endif()

# ---- third-party prebuilt static libs（cocos 官方包，含模拟器架构）----
file(GLOB EXTLIBS ${ENGINE}/external/*/prebuilt/ios/*.a)
# gh140: 白名单——只链"已证明健康"的库（30+ 轮修复后 rc=0 的集合）。
# 不健康库（curl/websockets/uv/js_static/crypto/ssl/box2d/bullet*）从链接剔除:
# 若无引用=彻底无害;若被引用,链接会在 undefined 里点名引用者,再对症处理。
set(EXTLIBS_KEEP "")
foreach(_lb ${EXTLIBS})
    get_filename_component(_lbn ${_lb} NAME)
    if(_lbn STREQUAL "libchipmunk.a" OR _lbn STREQUAL "libwebp.a"
       OR _lbn STREQUAL "libpng.a" OR _lbn STREQUAL "libjpeg.a"
       OR _lbn STREQUAL "libtiff.a" OR _lbn STREQUAL "libluajit.a"
       OR _lbn STREQUAL "libfreetype.a")
        list(APPEND EXTLIBS_KEEP ${_lb})
    endif()
endforeach()
list(LENGTH EXTLIBS_KEEP _nkeep)
message(STATUS "gh140 whitelist: keeping ${_nkeep} of ${EXTLIBS} external libs")
set(EXTLIBS ${EXTLIBS_KEEP})
# bullet/Box2D 的 arm64 切片是真机构建（2017 库）：3D 物理已关、2D 物理游戏零引用
#（physics-nodes 用 chipmunk，不链 libbox2d），arm64 切片还有符号表/重定位表互相
# 重叠的畸形（gh80 实测，libtool/ld 都救不回）—— 一并排除
list(FILTER EXTLIBS EXCLUDE REGEX ".*/external/bullet/.*")
list(FILTER EXTLIBS EXCLUDE REGEX ".*/external/Box2D/.*")
# gh143: freetype 现已修复健康(fix_prebuilts 全链 + rc=0 + ld-r 44/44 成功),恢复链接。
# (gh81 时代的排除在 gh140 白名单后又把它误伤,曾致 19 个 FT_ 符号 undefined)
# luajit 在 external/lua/luajit/ 两层深，上面的单层 glob 扫不到；只链 luajit 不链 lua（防重名符号）
list(APPEND EXTLIBS ${ENGINE}/external/lua/luajit/prebuilt/ios/libluajit.a)
target_link_libraries(WJJH PRIVATE ${EXTLIBS})

# 2017 年的预编译库存档成员未按 8 字节对齐，Xcode 26 新链接器直接报错；
# 经典链接器（ld-classic）十三年来一直接受它们 —— x86_64 路线必须用它
target_link_options(WJJH PRIVATE "-Wl,-ld_classic")
# gh139: -why_load 探针(ld 打印每个被拉成员原因;崩溃前最后一行=真凶成员)
target_link_options(WJJH PRIVATE "-Wl,-why_load")

target_link_libraries(WJJH PRIVATE
    "-framework OpenGLES" "-framework OpenAL" "-framework UIKit"
    "-framework Foundation" "-framework CoreGraphics" "-framework CoreText"
    "-framework QuartzCore" "-framework Metal" "-framework AudioToolbox"
    "-framework AVFoundation" "-framework MediaPlayer" "-framework GameController"
    "-framework Security" "-framework SystemConfiguration" "-framework CFNetwork"
    "-framework CoreMotion"
    z iconv sqlite3
)

# ---- Python 私服框架（python-apple-support 的 iOS 构建）----
set(PY_IOS_DIR ${CMAKE_SOURCE_DIR}/py_ios)
set(PY_FW ${PY_IOS_DIR}/Python.framework)

if(EXISTS ${PY_FW})
    message(STATUS "Python.framework found: ${PY_FW}")
    # 纯 dlopen 方案：不 include、不链接（零编译期依赖，加载失败只丢私服不崩 App）
    # 把 framework 拷进 .app bundle 的 Frameworks/ 目录
    add_custom_command(TARGET WJJH POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_directory
            ${PY_FW} $<TARGET_BUNDLE_DIR:WJJH>/Frameworks/Python.framework
        COMMENT "Embedding Python.framework into WJJH.app"
    )
    # 把 pyhome（stdlib+dynload）和 server/ 也拷进 .app
    if(EXISTS ${PY_IOS_DIR}/pyhome)
        add_custom_command(TARGET WJJH POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_directory
                ${PY_IOS_DIR}/pyhome $<TARGET_BUNDLE_DIR:WJJH>/pyhome
            COMMAND ${CMAKE_COMMAND} -E copy_directory
                ${PY_IOS_DIR}/server $<TARGET_BUNDLE_DIR:WJJH>/server
            COMMENT "Embedding Python pyhome + server into WJJH.app"
        )
    endif()
else()
    message(STATUS "Python.framework not found at ${PY_FW} — private server disabled")
endif()

# ---- 模拟器安装诊断(gh121):安装失败"Failed to get FD to bundle executable",
#      在构建期把 bundle 内容/主可执行文件类型打到 build 日志 ----
add_custom_command(TARGET WJJH POST_BUILD
    COMMAND sh -c "echo '== WJJH bundle diag =='; ls -la '$<TARGET_BUNDLE_DIR:WJJH>' | head -20; echo '-- binary:'; file '$<TARGET_FILE:WJJH>'; otool -hv '$<TARGET_FILE:WJJH>'; echo '-- build-version:'; otool -l '$<TARGET_FILE:WJJH>' | grep -A5 LC_BUILD_VERSION || true; echo '-- frameworks:'; ls '$<TARGET_BUNDLE_DIR:WJJH>/Frameworks' 2>/dev/null | head -5 || true"
    COMMENT "WJJH bundle/binary diagnostics"
    VERBATIM)
