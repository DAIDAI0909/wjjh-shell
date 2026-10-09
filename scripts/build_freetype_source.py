#!/usr/bin/env python3
"""gh158: freetype 源码现编 (arm64 ios-simulator)。

背景：2017 预编译 libfreetype.a 的 arm64 切片在「真正加载字体」时崩：
  FT_Add_Default_Modules -> FT_Add_Module -> _platform_strcmp(垃圾指针)
即模块类(class)数据指针是坏的（旧库畸形 + 我们归一化重建的副作用）。
此前 res/Font 一直缺席（gh157 才补进包），FreeType 从没被真正调用过，所以从没暴露。

做法：从引擎头 include/freetype/freetype.h 读 FREETYPE_MAJOR/MINOR/PATCH，
拉同版本源码（保证与引擎头 ABI 一致），clang 编 arm64 模拟器静态库，
替换 external/freetype2/prebuilt/ios/libfreetype.a。
失败（网络/编译）不阻断构建：保留原库继续。
"""
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ENGINE = sys.argv[1]
DEST = os.path.join(ENGINE, 'external', 'freetype2', 'prebuilt', 'ios', 'libfreetype.a')
WORK = '/tmp/wjjh_freetype'
HDR = os.path.join(ENGINE, 'external', 'freetype2', 'include', 'freetype', 'freetype.h')

# 模块聚合源（freetype 官方"每模块一编译单元"清单；不存在的自动跳过）
MODULE_FILES = [
    'src/base/ftsystem.c', 'src/base/ftinit.c', 'src/base/ftdebug.c',
    'src/base/ftbase.c', 'src/base/ftbbox.c', 'src/base/ftglyph.c',
    'src/base/ftbitmap.c', 'src/base/ftstroke.c', 'src/base/ftsynth.c',
    'src/base/ftgasp.c', 'src/base/ftgxval.c', 'src/base/ftlcdfil.c',
    'src/base/ftmm.c', 'src/base/ftotval.c', 'src/base/ftpatent.c',
    'src/base/ftpfr.c', 'src/base/fttype1.c', 'src/base/ftwinfnt.c',
    'src/base/ftbdf.c', 'src/base/ftcid.c', 'src/base/fthash.c',
    'src/autofit/autofit.c', 'src/bdf/bdf.c', 'src/cff/cff.c', 'src/cid/cid.c',
    'src/pcf/pcf.c', 'src/pfr/pfr.c', 'src/psaux/psaux.c',
    'src/pshinter/pshinter.c', 'src/psnames/psnames.c', 'src/raster/raster.c',
    'src/sdf/sdf.c', 'src/sfnt/sfnt.c', 'src/smooth/smooth.c',
    'src/truetype/truetype.c', 'src/type1/type1.c', 'src/type42/type42.c',
    'src/winfonts/winfnt.c', 'src/gzip/ftgzip.c', 'src/lzw/ftlzw.c',
    'src/bzip2/ftbzip2.c',
]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, errors='replace', **kw)


def read_version():
    txt = open(HDR, encoding='utf-8', errors='replace').read()
    def get(name):
        m = re.search(r'#define\s+%s\s+(\d+)' % name, txt)
        return int(m.group(1)) if m else None
    maj, mi, pa = get('FREETYPE_MAJOR'), get('FREETYPE_MINOR'), get('FREETYPE_PATCH')
    return maj, mi, pa


def main():
    try:
        maj, mi, pa = read_version()
        if not maj:
            print('[ft-src] 读不到 freetype 版本头, 保留原库')
            return 0
        tag = 'VER-%d-%d-%d' % (maj, mi, pa)
        url = 'https://codeload.github.com/freetype/freetype/tar.gz/refs/tags/%s' % tag
        print('[ft-src] 引擎头版本 %d.%d.%d -> %s' % (maj, mi, pa, url))

        os.makedirs(WORK, exist_ok=True)
        tgz = os.path.join(WORK, 'freetype.tgz')
        if not os.path.exists(tgz) or os.path.getsize(tgz) < 100000:
            for attempt in range(3):
                try:
                    urllib.request.urlretrieve(url, tgz)
                    break
                except Exception as e:
                    print('[ft-src] download attempt %d failed: %r' % (attempt, e))
            else:
                print('[ft-src] download failed, keep original freetype')
                return 0
        srcdir = os.path.join(WORK, 'src')
        if not os.path.exists(srcdir):
            os.makedirs(srcdir)
            with tarfile.open(tgz, 'r:gz') as tf:
                tf.extractall(srcdir)
        root = os.path.join(srcdir, os.listdir(srcdir)[0])
        print('[ft-src] 源码树:', root)

        sdk = run(['xcrun', '-sdk', 'iphonesimulator', '-show-sdk-path']).stdout.strip()
        objs = []
        cfiles = [f for f in MODULE_FILES if os.path.exists(os.path.join(root, f))]
        print('[ft-src] 编译 %d 个模块编译单元' % len(cfiles))
        for cf in cfiles:
            src = os.path.join(root, cf)
            obj = os.path.join(WORK, cf.replace('/', '_')[:-2] + '.o')
            r = run(['clang', '-c', src, '-o', obj,
                     '-isysroot', sdk, '-target', 'arm64-apple-ios13.0-simulator',
                     '-I', os.path.join(root, 'include'),
                     '-DFT2_BUILD_LIBRARY', '-O2', '-fno-objc-arc',
                     '-Wno-deprecated-declarations', '-Wno-unused-function'])
            if r.returncode != 0:
                print('[ft-src] compile %s failed: %s' % (cf, (r.stderr or '')[-500:]))
                print('[ft-src] keep original freetype')
                return 0
            objs.append(obj)
        out = os.path.join(WORK, 'libfreetype_new.a')
        if os.path.exists(out):
            os.remove(out)
        r = run(['ar', 'crs', out] + objs)
        if r.returncode != 0:
            print('[ft-src] ar failed: %s' % (r.stderr or '')[-300:])
            return 0
        shutil.copyfile(out, DEST)
        print('[ft-src] freetype rebuilt from source -> %s (%d bytes, %d objs)'
              % (DEST, os.path.getsize(out), len(objs)))
        return 0
    except Exception as e:
        print('[ft-src] unexpected error: %r (keep original freetype)' % e)
        return 0


if __name__ == '__main__':
    sys.exit(main())
