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


def patch_animres_metadata_only():
    """gh242: 让 AnimResManager 的 initAnimResInfo 用「元数据模式」创建骨架。

    背景：AnimResManager.lua 在**模块加载时**就调用 initAnimResInfo()，遍历
    animRes 表（2138 条 / 4 个图集：gongfu1 / enterVictory / specialEffect /
    fanghu）为每项创建 SkeletonAnimation，只为读取动画时长、事件、攻击距离。
    这些只需要 SkeletonData，但 createWithBinaryFile 会把图集**全部页**载入并经
    TextureCache 常驻。gongfu1 单图集 1145 页 × 512² = 1145MB，四个合起来远超
    2GB 设备约 1.4GB 的单进程上限 ⇒ 载入到 88% 被 Jetsam 杀
    （JetsamEvent-2026-10-15-132645 / 192834，reason=per-process-limit）。

    修法：换成 createMetadataOnly（同名接口，创建期间不加载纹理）。元数据 API
    全部照常可用，画质零影响（进战斗时仍以正常模式加载）。
    只替换 `res.altlasPath` 这一处（全文件唯一），不动其它 createWithBinaryFile。
    """
    p = os.path.join(DEST, 'src', 'app', 'FightSystem', 'ResourceManager',
                     'AnimResManager.lua')
    if not os.path.exists(p):
        print('[overlay] gh242: WARN AnimResManager.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    old = b'spine38.NewSkeletonAnimation:createWithBinaryFile(res.altlasPath'
    new = b'spine38.NewSkeletonAnimation:createMetadataOnly(res.altlasPath'
    n = raw.count(old)
    if n == 1:
        open(p, 'wb').write(raw.replace(old, new))
        print('[overlay] gh242: AnimResManager -> createMetadataOnly (1 replacement) OK', flush=True)
    else:
        # 拿到 0 或 >1 都要显式暴露：0 多半说明脚本被加密/改版，>1 说明有意料之外的用法。
        print('[overlay] gh242: WARN expected exactly 1 match for createWithBinaryFile(res.altlasPath),'
              ' got %d -> file left unchanged' % n, flush=True)
        head = raw[:24]
        print('[overlay] gh242: file head hex=%s printable=%r'
              % (head.hex(), ''.join(chr(b) if 32 <= b < 127 else '.' for b in head)), flush=True)
        print('[overlay] gh242: file size=%d' % len(raw), flush=True)


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
    else:
        with zipfile.ZipFile(PATCH) as z:
            names = z.namelist()
            z.extractall(DEST)
        print('[overlay] applied %d files:' % len(names), flush=True)
        for n in names:
            p = os.path.join(DEST, n)
            print('   %-60s %d bytes %s' % (n, os.path.getsize(p),
                                            'OK' if os.path.exists(p) else 'MISSING'))

    # gh242: 必须跑在 game.zip 解压之后、CMake 打包之前。
    patch_animres_metadata_only()

    # gh243: spine 图集页 → PVRTC4（运行期纹理内存降到约 1/8）。
    #   必须在 res 已解压（fetch_private.py 已把 res_full 铺进 gamelua/）之后、
    #   CMake 打包之前执行。脚本自带校验与回退，失败等同于"没做优化"，不会破坏构建。
    try:
        import pvr_compress_atlas
        pvr_compress_atlas.main()
    except Exception as e:                                      # noqa: BLE001
        print('[overlay] gh243: pvr step skipped (%r)' % e, flush=True)


if __name__ == '__main__':
    main()
