#!/usr/bin/env python3
"""gh138: libpng 源码现编(arm64 ios-simulator)。

2017 预编译 libpng 的成员组合会让现代 ld 断言崩溃(atoms 计数)，
且无单成员崩点可修。改为拉取 libpng 1.6.x 源码直接用 clang 编译为
arm64 模拟器静态库，替换 external/png/prebuilt/ios/libpng.a。

失败(网络等)不阻断构建:保留原库,继续。
"""
import os
import subprocess
import sys
import tarfile
import urllib.request

ENGINE = sys.argv[1]
DEST = os.path.join(ENGINE, 'external', 'png', 'prebuilt', 'ios', 'libpng.a')
WORK = '/tmp/wjjh_libpng'
URL = 'https://codeload.github.com/pnggroup/libpng/tar.gz/refs/tags/v1.6.43'


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, errors='replace', **kw)


def main():
    try:
        os.makedirs(WORK, exist_ok=True)
        tgz = os.path.join(WORK, 'libpng.tgz')
        if not os.path.exists(tgz) or os.path.getsize(tgz) < 100000:
            for attempt in range(3):
                try:
                    urllib.request.urlretrieve(URL, tgz)
                    break
                except Exception as e:
                    print('[png-src] download attempt %d failed: %r' % (attempt, e))
            else:
                print('[png-src] download failed, keep original libpng')
                return 0
        srcdir = os.path.join(WORK, 'src')
        if not os.path.exists(srcdir):
            os.makedirs(srcdir)
            with tarfile.open(tgz, 'r:gz') as tf:
                tf.extractall(srcdir)
        entries = os.listdir(srcdir)
        root = os.path.join(srcdir, entries[0])

        # pnglibconf.h 预生成配置
        pre = os.path.join(root, 'scripts', 'pnglibconf.h.prebuilt')
        conf = os.path.join(root, 'pnglibconf.h')
        if os.path.exists(pre) and not os.path.exists(conf):
            import shutil
            shutil.copyfile(pre, conf)

        sdk = run(['xcrun', '-sdk', 'iphonesimulator', '-show-sdk-path']).stdout.strip()
        cfiles = ['png.c', 'pngerror.c', 'pngget.c', 'pngmem.c', 'pngpread.c',
                  'pngread.c', 'pngrio.c', 'pngrtran.c', 'pngrutil.c', 'pngset.c',
                  'pngtrans.c', 'pngwio.c', 'pngwrite.c', 'pngwtran.c', 'pngwutil.c']
        objs = []
        for cf in cfiles:
            src = os.path.join(root, cf)
            obj = os.path.join(WORK, cf[:-2] + '.o')
            r = run(['clang', '-c', src, '-o', obj,
                     '-isysroot', sdk, '-target', 'arm64-apple-ios13.0-simulator',
                     '-I', root, '-O2', '-fno-objc-arc',
                     '-DPNG_ARM_NEON_OPT=0'])
            if r.returncode != 0:
                print('[png-src] compile %s failed: %s' % (cf, (r.stderr or '')[-400:]))
                print('[png-src] keep original libpng')
                return 0
            objs.append(obj)
        out = os.path.join(WORK, 'libpng_new.a')
        if os.path.exists(out):
            os.remove(out)
        r = run(['ar', 'crs', out] + objs)
        if r.returncode != 0:
            print('[png-src] ar failed: %s' % (r.stderr or '')[-300:])
            return 0
        import shutil
        shutil.copyfile(out, DEST)
        print('[png-src] libpng rebuilt from source -> %s (%d bytes)'
              % (DEST, os.path.getsize(out)))
        return 0
    except Exception as e:
        print('[png-src] unexpected error: %r (keep original libpng)' % e)
        return 0


if __name__ == '__main__':
    sys.exit(main())
