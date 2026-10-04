#!/usr/bin/env python3
"""CI 步骤：从仓库内置的 tar 组装 Python 私服运行时（py_ios/）。

输入：仓库根目录 Python-iOS-support.tar.gz（python-apple-support 构建，含
      x86_64+arm64 模拟器切片；36MB 二进制直接入库，不再走网络下载——
      自有服务器 80 端口只放行 /game.zip，GitHub API 路由被污染，双输）。
输出：$CM_BUILD_DIR/py_ios/
      Python.framework/            ← 运行时 dylib（dlopen 加载，零链接期依赖）
      pyhome/lib/python3.14/       ← 纯 py 标准库（PYTHONHOME 指到 pyhome）
      pyhome/lib/python3.14/lib-dynload/  ← C 扩展（_socket/_ssl/zlib 等 67 个）
      server/                      ← 私服代码（stub_server.py 先行；真 jhserver 后续）
"""
import os
import sys
import tarfile
import shutil

BUILD_DIR = os.environ.get('CM_BUILD_DIR', os.getcwd())
OUT = os.path.join(BUILD_DIR, 'py_ios')
TGZ = os.path.join(BUILD_DIR, 'Python-iOS-support.tar.gz')

SIM = 'Python.xcframework/ios-arm64_x86_64-simulator'


def fail(msg):
    print('[py-ios] FATAL:', msg, flush=True)
    sys.exit(3)


def extract():
    if not os.path.exists(TGZ):
        fail('Python-iOS-support.tar.gz not found in repo checkout')
    dest = os.path.join(BUILD_DIR, 'Python-iOS-extract')
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    print('[py-ios] extracting %d bytes...' % os.path.getsize(TGZ), flush=True)
    with tarfile.open(TGZ, 'r:gz') as tf:
        tf.extractall(dest)
    src = os.path.join(dest, 'Python.xcframework')
    if not os.path.isdir(src):
        fail('Python.xcframework not found after extract')
    return src


def setup_py_ios(xcfw):
    """组装 OUT：framework + pyhome 标准布局 + server/"""
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT, exist_ok=True)

    # 1) Python.framework（dylib + Info.plist + Headers）→ OUT/Python.framework
    sim_fw = os.path.join(xcfw, 'ios-arm64_x86_64-simulator', 'Python.framework')
    if not os.path.isdir(sim_fw):
        fail('Python.framework not found: ' + sim_fw)
    shutil.copytree(sim_fw, os.path.join(OUT, 'Python.framework'))

    # 2) 纯 py 标准库 → OUT/pyhome/lib/python3.14/
    #    PYTHONHOME=pyhome 时 getpath 自动找 $prefix/lib/python3.14 ✓
    stdlib_src = os.path.join(xcfw, 'lib', 'python3.14')
    if not os.path.isdir(stdlib_src):
        fail('stdlib lib/python3.14 not found: ' + stdlib_src)
    shutil.copytree(stdlib_src, os.path.join(OUT, 'pyhome', 'lib', 'python3.14'))

    # 3) lib-dynload（C 扩展 .so）→ pyhome/lib/python3.14/lib-dynload/
    #    x86_64 变体（iphonesimulator 切片，模拟器 x86_64 用）
    dynload = os.path.join(xcfw, 'ios-arm64_x86_64-simulator',
                           'lib-x86_64', 'python3.14', 'lib-dynload')
    if not os.path.isdir(dynload):
        fail('lib-dynload not found: ' + dynload)
    shutil.copytree(dynload,
                    os.path.join(OUT, 'pyhome', 'lib', 'python3.14', 'lib-dynload'))

    # 4) 私服代码 → OUT/server/app/（平铺：fz_boot_server.py + fz_api/ + wjjh_aes.py）
    #    源 = 仓库 py_ios_server/jhserver/（assemble_ios_server.py 产物）+ stub_server.py 兜底
    server_dir = os.path.join(OUT, 'server')
    app_dir = os.path.join(server_dir, 'app')
    os.makedirs(app_dir, exist_ok=True)
    repo_srv = os.path.join(BUILD_DIR, 'py_ios_server', 'jhserver')
    stub = os.path.join(BUILD_DIR, 'py_ios_server', 'stub_server.py')
    if os.path.isdir(repo_srv):
        for name in os.listdir(repo_srv):
            s = os.path.join(repo_srv, name)
            d = os.path.join(app_dir, name)
            if os.path.isdir(s):
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
    else:
        fail('py_ios_server/jhserver not found (run assemble_ios_server.py first)')
    if os.path.exists(stub):
        shutil.copy2(stub, os.path.join(app_dir, 'stub_server.py'))


def main():
    xcfw = extract()
    setup_py_ios(xcfw)

    total = 0
    nfiles = 0
    for root, _dirs, files in os.walk(OUT):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
            nfiles += 1
    print('[py-ios] OUT contents:', sorted(os.listdir(OUT)), flush=True)
    print('[py-ios] pyhome/lib/python3.14/lib-dynload: %d files' %
          len(os.listdir(os.path.join(OUT, 'pyhome', 'lib', 'python3.14', 'lib-dynload'))))
    print('[py-ios] server/app:', sorted(os.listdir(os.path.join(OUT, 'server', 'app'))))
    print('[py-ios] total %d files, %.1f MB' % (nfiles, total / 1e6), flush=True)
    print('[py-ios] bootstrap complete.', flush=True)


if __name__ == '__main__':
    main()
