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
FT_ROOT = os.path.join(ENGINE, 'external', 'freetype2')
# deps 布局: include/<platform>/freetype2/freetype.h（iOS=include/ios）；另有扁平布局兜底
HDR_CANDIDATES = [
    os.path.join(FT_ROOT, 'include', 'ios', 'freetype2', 'freetype.h'),
    os.path.join(FT_ROOT, 'include', 'freetype', 'freetype.h'),
    os.path.join(FT_ROOT, 'include', 'freetype2', 'freetype.h'),
]

# 模块聚合源（freetype 官方"每模块一编译单元"清单；不存在的自动跳过）
# 注：2.5.5 的 CID 聚合文件叫 type1cid.c（不是 cid.c）；cache 模块也要带上。
MODULE_FILES = [
    'src/base/ftsystem.c', 'src/base/ftinit.c', 'src/base/ftdebug.c',
    'src/base/ftbase.c', 'src/base/ftbbox.c', 'src/base/ftglyph.c',
    'src/base/ftbitmap.c', 'src/base/ftstroke.c', 'src/base/ftsynth.c',
    'src/base/ftgasp.c', 'src/base/ftgxval.c', 'src/base/ftlcdfil.c',
    'src/base/ftmm.c', 'src/base/ftotval.c', 'src/base/ftpatent.c',
    'src/base/ftpfr.c', 'src/base/fttype1.c', 'src/base/ftwinfnt.c',
    'src/base/ftbdf.c', 'src/base/ftcid.c', 'src/base/fthash.c',
    'src/autofit/autofit.c', 'src/bdf/bdf.c', 'src/cache/ftcache.c',
    'src/cff/cff.c', 'src/cid/type1cid.c', 'src/pcf/pcf.c', 'src/pfr/pfr.c',
    'src/psaux/psaux.c', 'src/pshinter/pshinter.c', 'src/psnames/psnames.c',
    'src/raster/raster.c', 'src/sdf/sdf.c', 'src/sfnt/sfnt.c',
    'src/smooth/smooth.c', 'src/truetype/truetype.c', 'src/type1/type1.c',
    'src/type42/type42.c', 'src/winfonts/winfnt.c', 'src/gzip/ftgzip.c',
    'src/lzw/ftlzw.c', 'src/bzip2/ftbzip2.c',
]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, errors='replace', **kw)


def read_version():
    hdr = None
    for cand in HDR_CANDIDATES:
        if os.path.exists(cand):
            hdr = cand
            break
    if hdr is None:
        inc = os.path.join(FT_ROOT, 'include')
        try:
            print('[ft-src] 头文件候选都不存在, include 树: %r' % (
                {d: os.listdir(os.path.join(inc, d))[:6]
                 for d in os.listdir(inc)} if os.path.isdir(inc) else 'no include dir'))
        except Exception as e:
            print('[ft-src] 列 include 失败: %r' % e)
        return None, None, None
    print('[ft-src] 版本头:', hdr)
    txt = open(hdr, encoding='utf-8', errors='replace').read()
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

        # 关掉可选模块（zlib/bzip2/lzw）——2.5.5 的 ftoption.h 默认开 zlib，
        # 其 ftgzip.c 在 clang 16 下触发 -Wincompatible-pointer-types 硬错；
        # 我们的字体是明文 ttf，不需要压缩容器支持，也免掉 -lz 依赖。
        # ★路径随版本变：2.5.5 源码树是 include/config/，2.6+ 才是 include/freetype/config/
        opt = None
        for cand in (os.path.join(root, 'include', 'freetype', 'config', 'ftoption.h'),
                     os.path.join(root, 'include', 'config', 'ftoption.h')):
            if os.path.exists(cand):
                opt = cand
                break
        if opt is None:
            print('[ft-src] 找不到 ftoption.h（布局又变了?）列出 include/: %r'
                  % (os.listdir(os.path.join(root, 'include'))[:10]
                     if os.path.isdir(os.path.join(root, 'include')) else 'no include'))
        else:
            txt = open(opt, encoding='utf-8', errors='replace').read()
            for flag in ('FT_CONFIG_OPTION_USE_ZLIB', 'FT_CONFIG_OPTION_USE_BZIP2',
                         'FT_CONFIG_OPTION_USE_LZW'):
                txt = re.sub(r'^(\s*)#define\s+%s\s*$' % flag, r'\1/* disabled by wjjh */',
                             txt, flags=re.M)
            open(opt, 'w', encoding='utf-8', newline='\n').write(txt)
            print('[ft-src] 已关闭 USE_ZLIB/USE_BZIP2/USE_LZW @ %s' % opt)

        sdk = run(['xcrun', '-sdk', 'iphonesimulator', '-show-sdk-path']).stdout.strip()
        objs = []
        cfiles = [f for f in MODULE_FILES if os.path.exists(os.path.join(root, f))]
        print('[ft-src] 编译 %d 个模块编译单元' % len(cfiles))
        skipped = []
        for cf in cfiles:
            src = os.path.join(root, cf)
            obj = os.path.join(WORK, cf.replace('/', '_')[:-2] + '.o')
            r = run(['clang', '-c', src, '-o', obj,
                     '-isysroot', sdk, '-target', 'arm64-apple-ios13.0-simulator',
                     '-I', os.path.join(root, 'include'),
                     '-DFT2_BUILD_LIBRARY', '-O2', '-fno-objc-arc',
                     '-Wno-deprecated-declarations', '-Wno-unused-function',
                     '-Wno-incompatible-pointer-types', '-Wno-int-conversion'])
            if r.returncode != 0:
                # 单文件失败不再整车放弃：跳过并记录（末尾 nm 校验会把关）
                err = (r.stderr or '') + (r.stdout or '')
                first = err.strip().splitlines()[:2]
                print('[ft-src] compile %s FAILED, skip: %s' % (cf, ' / '.join(first)[:300]))
                skipped.append(cf)
                continue
            objs.append(obj)
        if skipped:
            print('[ft-src] 跳过 %d 个编译单元: %r' % (len(skipped), skipped))
        out = os.path.join(WORK, 'libfreetype_new.a')
        if os.path.exists(out):
            os.remove(out)
        r = run(['ar', 'crs', out] + objs)
        if r.returncode != 0:
            print('[ft-src] ar failed: %s' % (r.stderr or '')[-300:])
            return 0
        # 保险：核心全局符号必须全（缺了就直接当失败），模块类符号只做"告警级"统计
        # （2.5.5 里 module class 多为 static，不同版本 nm 可见性不一，不能当硬条件误伤）
        nm = run(['nm', out])
        syms = nm.stdout or ''
        need_global = ['_FT_Init_FreeType', '_FT_New_Memory_Face', '_FT_Load_Glyph',
                       '_FT_Set_Char_Size', '_FT_Done_FreeType',
                       '_FT_New_Face', '_FT_Get_Char_Index', '_FT_Open_Face']
        miss = [s for s in need_global if s not in syms]
        if miss:
            print('[ft-src] 新库缺全局符号 %r, 保留原库' % miss)
            return 0
        print('[ft-src] nm 全符号 %d 个; 关键名字出现情况: %r'
              % (len(syms.splitlines()),
                 {k: (k in syms) for k in ('tt_driver_class', 'cff_driver_class',
                                           'psnames_module_class', 'autofit_module',
                                           'ft_default_modules', 'sfnt_module_class')}))
        if len(objs) < 30:
            print('[ft-src] 成功编译的编译单元只有 %d 个（<30），可疑，保留原库' % len(objs))
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
