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

# gh211: 真机构建(--device 或 WJJH_DEVICE=1)用 ios-arm64(设备)切片;缺省模拟器切片
DEVICE = ('--device' in sys.argv) or (os.environ.get('WJJH_DEVICE') == '1')
LEAF = 'ios-arm64' if DEVICE else 'ios-arm64_x86_64-simulator'


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
    fw = os.path.join(xcfw, LEAF, 'Python.framework')
    if not os.path.isdir(fw):
        fail('Python.framework not found: ' + fw)
    print('[py-ios] framework leaf: %s' % LEAF, flush=True)
    shutil.copytree(fw, os.path.join(OUT, 'Python.framework'))

    # 2) 纯 py 标准库 → OUT/pyhome/lib/python3.14/
    #    PYTHONHOME=pyhome 时 getpath 自动找 $prefix/lib/python3.14 ✓
    stdlib_src = os.path.join(xcfw, 'lib', 'python3.14')
    if not os.path.isdir(stdlib_src):
        fail('stdlib lib/python3.14 not found: ' + stdlib_src)
    shutil.copytree(stdlib_src, os.path.join(OUT, 'pyhome', 'lib', 'python3.14'))

    # 3) lib-dynload（C 扩展 .so）→ pyhome/lib/python3.14/lib-dynload/
    #    gh72 arm64 路线：优先 lib-arm64（arm64 模拟器原生），缺失才回退 lib-x86_64；
    #    gh211: 真机用 ios-arm64 切片下的 lib-arm64（iphoneos .so）
    if DEVICE:
        variants = ['lib-arm64']
    else:
        variants = ['lib-arm64', 'lib-x86_64']
    dynload = None
    for variant in variants:
        cand = os.path.join(xcfw, LEAF, variant, 'python3.14', 'lib-dynload')
        if os.path.isdir(cand):
            dynload = cand
            print('[py-ios] lib-dynload variant: %s (%d files)' %
                  (variant, len(os.listdir(cand))), flush=True)
            break
    if dynload is None:
        fail('lib-dynload not found in %s leaf (lib-arm64 missing)' % LEAF)
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

    # gh312: 单机授权支持（iOS）
    #  ① 服务端 /fz_license：无 license_required.flag（非单机包/模拟器）时 activated 直接为 True
    #     ⇒ 客户端即使走了授权闸也秒进；只有带 flag 的单机包才真正卡授权。
    #  ② 真机构建：写入 license_required.flag（开门）+ 把 _fz_device_code 换成 iOS 版
    #     （取不到 android_id 时用服务端可写目录里持久化的 UUID，每台唯一、覆盖安装不变）。
    _srv = os.path.join(app_dir, 'fz_boot_server.py')
    if os.path.exists(_srv):
        _t = open(_srv, 'r', encoding='utf-8', errors='replace').read()
        _old_act = "out = {'activated': act, 'device_code': dc, 'danji': _DANJI is True,"
        _new_act = "out = {'activated': (act or (not _DANJI)), 'device_code': dc, 'danji': _DANJI is True,"
        if _t.count(_old_act) == 1:
            _t = _t.replace(_old_act, _new_act)
            print('[py-ios] gh312: server /fz_license pass-through when not _DANJI OK')
        else:
            print('[py-ios] gh312: WARN /fz_license anchor count=%d (left as-is)' % _t.count(_old_act))
        if DEVICE:
            _old_dc = (
                "def _fz_device_code():\n"
                '    """设备码：android_id + 机型指纹 的 sha256 前 16 位，4x4 分组。无需任何权限。"""\n'
                "    aid, fp = '', ''\n"
                "    try:\n"
                "        import subprocess\n"
                "        aid = subprocess.run(['settings', 'get', 'secure', 'android_id'],\n"
                "                             capture_output=True, timeout=5).stdout.decode().strip()\n"
                "        fp = subprocess.run(['getprop', 'ro.build.fingerprint'],\n"
                "                            capture_output=True, timeout=5).stdout.decode().strip()\n"
                "    except Exception:\n"
                "        pass\n"
                "    d = hashlib.sha256((aid + '|' + fp).encode()).hexdigest()[:16].upper()\n"
                "    return '-'.join(d[i:i + 4] for i in range(0, 16, 4))"
            )
            _new_dc = (
                "def _fz_device_code():\n"
                '    """gh312(iOS)：android_id+机型指纹 sha256 前16位；取不到 android_id（iOS/非安卓）时\n'
                '    用服务端可写目录里持久化的 UUID 作为稳定设备码（每台唯一，覆盖安装/重启不变）。"""\n'
                "    aid, fp = '', ''\n"
                "    try:\n"
                "        import subprocess\n"
                "        aid = subprocess.run(['settings', 'get', 'secure', 'android_id'],\n"
                "                             capture_output=True, timeout=5).stdout.decode().strip()\n"
                "        fp = subprocess.run(['getprop', 'ro.build.fingerprint'],\n"
                "                            capture_output=True, timeout=5).stdout.decode().strip()\n"
                "    except Exception:\n"
                "        pass\n"
                "    if not aid:\n"
                "        try:\n"
                "            import uuid as _uuid\n"
                "            _f = os.path.join(HERE, 'device_id.txt')\n"
                "            if os.path.exists(_f):\n"
                "                aid = open(_f, 'r', encoding='ascii').read().strip()\n"
                "            if not aid:\n"
                "                aid = _uuid.uuid4().hex\n"
                "                open(_f, 'w', encoding='ascii').write(aid)\n"
                "        except Exception:\n"
                "            aid = 'ios-nodev'\n"
                "    d = hashlib.sha256((aid + '|' + fp).encode()).hexdigest()[:16].upper()\n"
                "    return '-'.join(d[i:i + 4] for i in range(0, 16, 4))"
            )
            if _t.count(_old_dc) == 1:
                _t = _t.replace(_old_dc, _new_dc)
                print('[py-ios] gh312: _fz_device_code -> iOS persisted UUID OK')
            else:
                print('[py-ios] gh312: WARN _fz_device_code anchor count=%d (left as-is)' % _t.count(_old_dc))
            with open(os.path.join(app_dir, 'license_required.flag'), 'w', encoding='ascii') as _lf:
                _lf.write('danji')
            print('[py-ios] gh312: license_required.flag written -> 授权闸 ON')
        open(_srv, 'w', encoding='utf-8').write(_t)


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
