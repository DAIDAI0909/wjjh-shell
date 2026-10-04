#!/usr/bin/env python3
"""CI 步骤：把仓库内置的 res_patch.zip 覆盖进 gamelua/（fb03 科举表等增量）。

game.zip（Release 资产）落后于 fangzhi_lua_dec 权威源码时，增量走本补丁包，
避免为 3 个文件整包重传 115MB。必须跑在 CMake configure 之前（CMake 若发现
gamelua/src 已存在会跳过自解压）。
"""
import os
import sys
import zipfile

BUILD_DIR = os.environ.get('CM_BUILD_DIR', os.getcwd())
PATCH = os.path.join(BUILD_DIR, 'res_patch.zip')
GAMEZIP = os.path.join(BUILD_DIR, 'gamelua', 'game.zip')
DEST = os.path.join(BUILD_DIR, 'gamelua')


def main():
    # gamelua/src 不存在 = game.zip 还没解（CMake 配置期才解）→ 先替它解掉
    if not os.path.isdir(os.path.join(DEST, 'src')):
        if not os.path.exists(GAMEZIP):
            print('[overlay] gamelua/game.zip not found, skip overlay')
            return
        print('[overlay] extracting game.zip first...', flush=True)
        with zipfile.ZipFile(GAMEZIP) as z:
            z.extractall(DEST)
    if not os.path.exists(PATCH):
        print('[overlay] res_patch.zip not found, skip')
        return
    with zipfile.ZipFile(PATCH) as z:
        names = z.namelist()
        z.extractall(DEST)
    print('[overlay] applied %d files:' % len(names), flush=True)
    for n in names:
        p = os.path.join(DEST, n)
        print('   %-60s %d bytes %s' % (n, os.path.getsize(p),
                                        'OK' if os.path.exists(p) else 'MISSING'))


if __name__ == '__main__':
    main()
