#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gh243/gh244: 把 spine 图集的页 PNG 压成 PVRTC4(RGBA)，显著降低 iOS 运行期纹理内存。

背景（JetsamEvent 实证）：
  2GB A9 iPad 上 iOS 16 单进程上限 ≈1.4GB（rpages 撞 1450MB 即被 per-process-limit 杀）。
  gh242 用「元数据模式」解决了**启动期**加载 gongfu1（1144 页）的问题，
  但**运行期**仍会加载 `Anim/gongfu`（1620 页 × 256² = RGBA4444 下 202MB）等图集，
  叠加主界面 UI/立绘后 internal 峰值达 928MB，再次撞上限。

  PVRTC 4bpp 在 iOS GPU 上可直接采样，**不需要解压**：
      RGBA8888(4B/px) → RGBA4444(2B/px) → PVRTC4(0.5B/px)
      例：gongfu 405MB → 202MB → 51MB。

cocos2d-x 3.15.1 侧已确认可行（读源码实证）：
  * `CCImage.cpp` 有 `isPvr()` / `initWithPVRv2Data()` / `initWithPVRv3Data()`，格式自动识别；
  * `Texture2D::initWithImage` 对 `image->isCompressed()` 走**直通上传**（不做 RGBA 转换）；
  * `v2_pixel_formathash` **只映射带 alpha 的两种**：
        PVRTC2BPP_RGBA → PVRTC2A
        PVRTC4BPP_RGBA → PVRTC4A
    ⇒ 所以我们**必须**产出 PVRTC4BPP_RGBA（`flags & 0xFF == 0x19`），
      否则 cocos 会以 "Unsupported PVR Pixel Format" 拒绝加载 ⇒ 贴图变白/丢图。

---- gh244 修正（gh243 的 run 243 被 60min job 上限砍掉，教训）----
  1. **gh243 的时间预算形同虚设**：`ThreadPoolExecutor` 一次性 submit 全部 3502 个任务，
     而 `with` 退出时会 `shutdown(wait=True)` —— 必须等所有任务跑完才返回，
     所以「超预算就 continue」根本拦不住，最终跑满 28 分钟被 CI 砍掉。
     ⇒ 改为**有界提交**：提交前逐个检查截止时间，到点就停止提交，
        最多只多等 `WORKERS` 个在跑的任务。
  2. **去掉每次调用的 xcrun 开销**：`xcrun -sdk iphoneos texturetool` 每次都起一个 xcrun
     进程做 SDK 解析，且并发时可能争抢缓存。改为 `xcrun -f texturetool` 解析出**绝对路径**，
     之后直接调用该二进制。
  3. **按内存收益排序**：3502 页里 Top18 图集占 95% 收益（gongfu1+gongfu 两册就占 2764 页）。
     按「图集总像素」降序提交 ⇒ 即使预算被砍，最重要的先转完。
  4. **加进度日志**：每 100 页打印速率与预估剩余，下次可据此校准预算。
  5. **双截止时间**：`t0 + 预算` 与 `仓库 checkout 时刻 + 45min` 取小值 ——
     因为 60min 是**整个 job** 的限额，而 fetch_private 可能吃掉 25~32 分钟。

安全设计（本脚本的核心原则：宁可不动，绝不改坏）：
  1. 只处理**正方形且边长为 2 的幂**的页（PVRTC 硬要求），其余原样保留 PNG；
  2. 转换后**逐个解析 .pvr 头部校验**：偏移 44 处须为 "PVR!"，且 `flags & 0xFF == 0x19`；
  3. 校验不过 ⇒ 删掉 .pvr、保留 PNG、**不改 .atlas**；
  4. 任何异常都被吞掉并计数，绝不让 CI 失败（最坏情况 = 与改动前完全一致）；
  5. 有**双截止时间**，超时立刻停止（只等已在跑的几个任务结束）。
"""
import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time

# ---- 配置 ----
BUILD_DIR = os.environ.get('CM_BUILD_DIR', os.getcwd())
RES_ROOT = os.path.join(BUILD_DIR, 'gamelua')          # fetch_private.py 把 res_full 解到这里
TIME_BUDGET_S = int(os.environ.get('WJJH_PVR_BUDGET_S', '900'))      # 自身预算：15 分钟
HARD_DEADLINE_S = int(os.environ.get('WJJH_PVR_HARD_S', '2700'))     # 自 checkout 起 45 分钟
WORKERS = int(os.environ.get('WJJH_PVR_WORKERS', '4'))
PROGRESS_EVERY = 100
PVRTC4BPP_RGBA = 0x19          # cocos: PVR2TexturePixelFormat::PVRTC4BPP_RGBA
PVRTC2BPP_RGBA = 0x18          # cocos: PVR2TexturePixelFormat::PVRTC2BPP_RGBA
PVR_MAGIC_OFFSET = 44          # cocos PVRv2TexHeader: pvrTag 在偏移 44
PVR_FLAG_OFFSET = 16           # flags 在偏移 16
PVR_TYPE_MASK = 0xFF

# ---- gh247: KTX 容器（Apple `TextureConverter` 的实际输出格式）----
KTX_ID = b'\xabKTX 11\xbb\r\n\x1a\n'
KTX2_ID = b'\xabKTX 20\xbb\r\n\x1a\n'
# GL 枚举（Imagination PVRTC）
GL_PVRTC_RGB_4 = 0x8C00
GL_PVRTC_RGB_2 = 0x8C01
GL_PVRTC_RGBA_4 = 0x8C02
GL_PVRTC_RGBA_2 = 0x8C03
# GL internalFormat -> (PVR2 flags, bpp)  —— 只收**带 alpha** 的
KTX_PVRTC_MAP = {
    GL_PVRTC_RGBA_4: (PVRTC4BPP_RGBA, 4),
    GL_PVRTC_RGBA_2: (PVRTC2BPP_RGBA, 2),
}
KTX_NO_ALPHA = {GL_PVRTC_RGB_4, GL_PVRTC_RGB_2}


def _tool_rel_paths():
    """纹理编码工具在 Xcode.app 里可能出现的位置。

    * `texturetool`  —— 老工具，Xcode 16 起已移除（run 244/245 实测 15 个 Xcode 全无）
    * `TextureConverter` —— Apple 的官方替代（Xcode 13 发行说明推荐），在 `Developer/usr/bin`
    """
    out = []
    for name in ('texturetool', 'TextureConverter'):
        out += [
            'Contents/Developer/Platforms/iPhoneOS.platform/Developer/usr/bin/%s' % name,
            'Contents/Developer/usr/bin/%s' % name,
            'Contents/Developer/Platforms/iPhoneSimulator.platform/Developer/usr/bin/%s' % name,
        ]
    return tuple(out)


def find_texturetool_candidates():
    """列出候选纹理编码工具（**绝对路径优先，快**），返回 [(cmd_list, 说明), ...]。

    ★ 背景链：
      - gh244：Xcode 16 已无 `texturetool` ⇒ `xcrun -f` 与 xcrun 包装都失败。
      - gh245：扫遍 runner 上 **15 个** Xcode（15.0.1~16.2）的 3 个历史路径，
        `texturetool` 全部 `NONE`；但**发现 Xcode 16.1 的 `Developer/usr/bin` 里有
        `TextureConverter`** —— 正是 Apple 官方替代品。
      - gh246：因此这里同时收录 `TextureConverter`，参数风格交给探针实测。
    """
    cands, seen = [], set()

    def add(cmd, how):
        key = tuple(cmd)
        if key not in seen:
            seen.add(key)
            cands.append((cmd, how))

    # 1) 扫描所有已安装的 Xcode（texturetool 与 TextureConverter 一起找）
    for app in sorted(glob.glob('/Applications/Xcode*.app')):
        for rel in _tool_rel_paths():
            p = os.path.join(app, rel)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                add([p], 'xcode %s/%s' % (os.path.basename(app), os.path.basename(p)))

    # 2) 当前 developer dir 解析（若存在）
    for name in ('texturetool', 'TextureConverter'):
        try:
            r = subprocess.run(['xcrun', '-f', name], capture_output=True, timeout=60)
            p = (r.stdout or b'').decode('utf-8', 'replace').strip().splitlines()
            p = p[0].strip() if p else ''
            if r.returncode == 0 and p and os.path.exists(p):
                add([p], 'xcrun -f %s -> %s' % (name, p))
        except Exception:                                       # noqa: BLE001
            pass

    # 3) Command Line Tools
    for rel in _tool_rel_paths():
        p = os.path.join('/Library/Developer/CommandLineTools',
                         rel.replace('Contents/Developer/', ''))
        if os.path.isfile(p) and os.access(p, os.X_OK):
            add([p], 'clt %s' % p)

    # 4) PATH 上直接有
    for name in ('texturetool', 'TextureConverter'):
        w = shutil.which(name)
        if w:
            add([w], 'PATH %s' % w)

    # 5) xcrun 包装（保底；慢）
    add(['xcrun', '-sdk', 'iphoneos', 'TextureConverter'], 'xcrun wrapper TextureConverter')
    add(['xcrun', '-sdk', 'iphoneos', 'texturetool'], 'xcrun wrapper texturetool')
    return cands


def discover_help(cands):
    """所有候选都失败时，把每个候选的 `--help` 打出来（截断 2000 字符）。

    目的：一次把工具的真实用法带回本地，**避免再靠猜参数浪费一轮 43 分钟构建**。
    """
    for cmd, how in cands:
        for helpflag in ('--help', '-h'):
            try:
                r = subprocess.run(list(cmd) + [helpflag], capture_output=True, timeout=60)
                out = ((r.stdout or b'') + (r.stderr or b'')).decode('utf-8', 'replace').strip()
                if out:
                    print('[pvr] --- help for %s (%s) ---' % (how, helpflag), flush=True)
                    print(out[:2000], flush=True)
                    break
            except Exception as e:                              # noqa: BLE001
                print('[pvr] help failed for %s: %r' % (how, e), flush=True)
                break


def describe_tool_search():
    """诊断用：把「装了哪些 Xcode、各自有没有编码工具」打全，便于下次定位。"""
    apps = sorted(glob.glob('/Applications/Xcode*.app'))
    print('[pvr] Xcode apps installed: %d -> %s'
          % (len(apps), [os.path.basename(a) for a in apps]), flush=True)
    for app in apps:
        hit = [os.path.basename(rel) for rel in _tool_rel_paths()
               if os.path.isfile(os.path.join(app, rel))]
        print('[pvr]   %-28s tools=%s' % (os.path.basename(app), sorted(set(hit)) or 'NONE'),
              flush=True)
    try:
        r = subprocess.run(['xcode-select', '-p'], capture_output=True, timeout=30)
        print('[pvr] active developer dir: %s'
              % (r.stdout or b'').decode('utf-8', 'replace').strip(), flush=True)
    except Exception:                                           # noqa: BLE001
        pass
    for d in sorted(glob.glob('/Applications/Xcode*.app/Contents/Developer/usr/bin')):
        try:
            names = sorted(os.listdir(d))
        except Exception:                                       # noqa: BLE001
            continue
        like = [n for n in names if any(k in n.lower() for k in ('tex', 'pvr', 'compress'))]
        if like:
            print('[pvr]   %s -> %s' % (os.path.basename(os.path.dirname(os.path.dirname(d))), like),
                  flush=True)


def probe_tool(cands, sample_png):
    """用**真实页**试转一次，挑出第一个能产出合法 PVRTC 的「工具 × 参数风格」。

    ★ 关键安全点（gh246 新增）：**在临时目录里对副本操作**。
    因为要试多种参数风格，其中「位置参数颠倒」那种万一被工具理解成
    「把输出写到第 1 个参数」，就会**覆盖掉真实的 PNG**。用副本 ⇒ 零风险。

    返回 (tool_cmd, argfn, label) 或 (None, None, 原因)。
    """
    if not sample_png:
        if cands:
            _c, how = cands[0]
            return cands[0][0], arg_modes()[0][1], how
        return None, None, 'no candidate'
    tmp = tempfile.mkdtemp(prefix='pvrprobe_')
    try:
        probe_png = os.path.join(tmp, 'probe.png')
        shutil.copyfile(sample_png, probe_png)
        for cmd, how in cands:
            for amname, argfn in arg_modes():
                ok, msg = convert_one(cmd, argfn, probe_png)
                if ok:
                    print('[pvr] probe OK: %s | %s | %s' % (how, amname, msg), flush=True)
                    return cmd, argfn, '%s | %s' % (how, amname)
                # 只对「工具确实存在」的候选打印详细失败，避免噪音
                if os.path.exists(cmd[0]) or cmd[0] in ('xcrun',):
                    print('[pvr] probe failed: %s | %s -> %s' % (how, amname, msg[:200]), flush=True)
        return None, None, 'all combos failed'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def is_pot_square(w, h):
    pot = lambda x: x > 0 and (x & (x - 1)) == 0
    return pot(w) and pot(h) and w == h


def verify_pvr(path):
    """校验产出的 .pvr 是否为 cocos 能吃的 **带 alpha 的 PVRTC**。

    ★ gh246：必须同时认 **PVR v2 与 v3** 两种容器 —— 读 cocos 源码确认：
      * v2（`PVRv2TexHeader`，52B）：魔数 `"PVR!"` 在**偏移 44**；格式在**偏移 16**，
        `flags & 0xFF`：`0x19`=PVRTC4BPP_RGBA、`0x18`=PVRTC2BPP_RGBA。
        v2 的 `v2_pixel_formathash` **只映射这两种**（无 RGB 变体）。
      * v3（`PVRv3TexHeader`，52B）：魔数 `"PVR\\x03"` 在**偏移 0**；格式在**偏移 8**（8 字节），
        `3`=PVRTC4BPP_RGBA、`1`=PVRTC2BPP_RGBA。v3 映射表覆盖全部 4 种 PVRTC。
      * `Image::isPvr()` 的判别：v2 看 `pvrTag=="PVR!"`，v3 看 `version==0x50565203`（BE 读法）。

    返回 True 才允许替换；否则一律回退 PNG。
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(64)
        if len(head) < 52:
            return False, 'short header'

        # --- PVR v2 ---
        if head[PVR_MAGIC_OFFSET:PVR_MAGIC_OFFSET + 4] == b'PVR!':
            flags = struct.unpack_from('<I', head, PVR_FLAG_OFFSET)[0]
            fmt = flags & PVR_TYPE_MASK
            if fmt == 0x19:
                return True, 'v2 PVRTC4BPP_RGBA'
            if fmt == 0x18:
                return True, 'v2 PVRTC2BPP_RGBA (low quality)'
            return False, 'v2 format=0x%02X (need 0x19 PVRTC4BPP_RGBA)' % fmt

        # --- PVR v3 ---
        if head[0:4] == b'PVR\x03':
            fmt = struct.unpack_from('<Q', head, 8)[0]
            if fmt == 3:
                return True, 'v3 PVRTC4BPP_RGBA'
            if fmt == 1:
                return True, 'v3 PVRTC2BPP_RGBA (low quality)'
            return False, 'v3 pixelFormat=%d (need 3 PVRTC4BPP_RGBA)' % fmt

        return False, 'not a PVR file (head=%r)' % head[:8]
    except Exception as e:                                      # noqa: BLE001
        return False, 'verify exception %r' % e


def parse_atlas(path):
    """解析 .atlas，返回 [(页名, w, h)]（只取页头）。"""
    pages = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
    except Exception:                                           # noqa: BLE001
        return pages
    cur = None
    for ln in lines:
        s = ln.strip()
        if s and not ln.startswith(' ') and s.endswith('.png'):
            cur = s
        elif cur and s.startswith('size:'):
            m = re.findall(r'\d+', s)
            if len(m) >= 2:
                pages.append((cur, int(m[0]), int(m[1])))
            cur = None
    return pages


def arg_modes():
    """候选命令行参数风格 —— 不同工具/版本参数不同，靠探针实测挑。

    Apple 把 texturetool 换成了 `TextureConverter`（Xcode 13 发行说明：
    "use Texture Converter on the command line to compress textures to all Metal
    compressed texture formats"），但它的参数语法网上查不到 ⇒ 这里一次列全，
    用**真实页**逐个试，能产出合法 PVR 的才算数。

    ★ 安全约束：**只收录「输出路径走显式参数」的风格**。
    绝不试 `[输入, 输出]` 这种位置参数 —— 万一工具把第 1 个参数当输出，
    就会**覆盖掉真实的 PNG**，那是不可逆的破坏。
    """
    return [
        ('texturetool-style', lambda o, i: ['-f', 'PVR', '-e', 'PVRTC',
                                            '--bits-per-pixel-4', '-o', o, i]),
        ('tc-format-4bpp',    lambda o, i: ['--format', 'PVRTC_4BPP', '--output', o, i]),
        ('tc-format-pvr',     lambda o, i: ['--format', 'PVR', '--output', o, i]),
        ('tc-short-o',        lambda o, i: ['-o', o, i]),
        ('tc-f-4bpp',         lambda o, i: ['-f', 'PVRTC_4BPP', '-o', o, i]),
    ]


def ktx_to_pvr(ktx_path, pvr_path):
    """把 `TextureConverter` 产出的 **KTX(PVRTC)** 重新包成 cocos 能吃的 **PVR v2**。

    ★ gh247 的核心发现：`TextureConverter` 认得 texturetool 的参数风格
    （`-f PVR -e PVRTC --bits-per-pixel-4 -o OUT IN`），也确实做了 PVRTC 压缩，
    但**输出容器是 KTX**（实测文件头 `\\xabKTX 11\\xbb`）。而 cocos 的 KTX 路径
    （`initWithATITCData`）**只映射 ATC 格式**，根本不认 KTX 里的 PVRTC ⇒ 会被拒。
    解法：**压缩数据原样搬运，只换容器头**（画质与 Apple 编码器完全一致）。

    只取**基础层**（mipmap 0），PVR 头写 `numMipmaps=1` ⇒ 内存最小。

    返回 (ok, msg)。
    """
    try:
        with open(ktx_path, 'rb') as f:
            blob = f.read()
    except Exception as e:                                      # noqa: BLE001
        return False, 'ktx read fail %r' % e
    if len(blob) < 68:
        return False, 'ktx too short (%d)' % len(blob)
    if blob[:12] != KTX_ID:
        if blob[:12] == KTX2_ID:
            return False, 'KTX 2.0 container (not supported)'
        return False, 'not KTX (head=%r)' % blob[:12]
    if struct.unpack_from('<I', blob, 12)[0] != 0x04030201:
        return False, 'KTX big-endian not supported'

    gl_internal = struct.unpack_from('<I', blob, 28)[0]
    w, h, _depth, _arr, _faces, levels, kvlen = struct.unpack_from('<7I', blob, 36)

    fmt = KTX_PVRTC_MAP.get(gl_internal)
    if fmt is None:
        if gl_internal in KTX_NO_ALPHA:
            return False, 'KTX internal=0x%04X is PVRTC WITHOUT alpha (sprites need alpha)' % gl_internal
        return False, 'KTX internal=0x%04X not PVRTC_RGBA (need 0x8C02)' % gl_internal
    flag, bpp = fmt

    off = 64 + kvlen
    if off + 4 > len(blob):
        return False, 'ktx truncated at imageSize'
    image_size = struct.unpack_from('<I', blob, off)[0]
    off += 4
    need = w * h // (8 // bpp)          # 4bpp -> w*h/2 ; 2bpp -> w*h/4
    if image_size < need or off + need > len(blob):
        return False, ('ktx payload too small (imageSize=%d need=%d avail=%d)'
                       % (image_size, need, len(blob) - off))
    data = blob[off:off + need]

    # ---- PVR v2 头（52B, little-endian；cocos 用 CC_SWAP_INT32_LITTLE_TO_HOST，小端即原样）----
    hdr = bytearray(52)
    struct.pack_into('<I', hdr, 0, 52)      # headerLength
    struct.pack_into('<I', hdr, 4, h)       # height
    struct.pack_into('<I', hdr, 8, w)       # width
    struct.pack_into('<I', hdr, 12, 1)      # numMipmaps = 1（只留基础层）
    struct.pack_into('<I', hdr, 16, flag)   # flags（0x19/0x18）
    struct.pack_into('<I', hdr, 20, need)   # dataLength
    struct.pack_into('<I', hdr, 24, bpp)    # bpp
    hdr[44:48] = b'PVR!'                    # pvrTag（cocos memcmp 4 字节）
    struct.pack_into('<I', hdr, 48, 1)      # numSurfs
    with open(pvr_path, 'wb') as f:
        f.write(bytes(hdr))
        f.write(data)

    ok, why = verify_pvr(pvr_path)
    if not ok:
        try:
            os.remove(pvr_path)
        except Exception:                                       # noqa: BLE001
            pass
        return False, 'rewrap produced invalid pvr: %s' % why
    return True, 'ktx %dx%d int=0x%04X lv=%d -> %s' % (w, h, gl_internal, levels, why)


def convert_one(tool, argfn, png_path):
    """转单页 → 返回 (ok, msg)。产出 <png>.pvr 并用 verify_pvr 校验。

    gh247：若工具产出的是 KTX（TextureConverter 的行为），就地重包成 PVR。
    """
    pvr = png_path[:-4] + '.pvr'
    try:
        if os.path.exists(pvr):
            os.remove(pvr)
        argv = list(tool) + list(argfn(pvr, png_path))
        r = subprocess.run(argv, capture_output=True, timeout=120)
        if not os.path.exists(pvr):
            return False, 'no output (rc=%d) %s' % (r.returncode,
                                                    (r.stderr or b'')[:400].decode('utf-8', 'replace'))
        ok, why = verify_pvr(pvr)
        if ok:
            return True, why
        # 不是合法 PVR —— 看是不是 KTX 容器（TextureConverter 的输出）
        try:
            with open(pvr, 'rb') as f:
                head = f.read(12)
        except Exception:                                       # noqa: BLE001
            head = b''
        if head in (KTX_ID, KTX2_ID) or head[:7] == b'\xabKTX 20':
            ok2, why2 = ktx_to_pvr(pvr, pvr)     # 原地重包（blob 已全读入内存，安全）
            if ok2:
                return True, why2
            try:
                os.remove(pvr)
            except Exception:                                   # noqa: BLE001
                pass
            return False, why2
        os.remove(pvr)
        return False, why
    except Exception as e:                                      # noqa: BLE001
        try:
            if os.path.exists(pvr):
                os.remove(pvr)
        except Exception:                                       # noqa: BLE001
            pass
        return False, 'exc %r' % e


def job_anchor():
    """估算本 job 的起点（checkout 时刻）——用于对齐 60min 总额。

    GitHub Actions 在 job 开头就 checkout，所以 .git/HEAD 的 mtime ≈ job 起点 + 20s。
    拿不到就返回 None（退化为只用自身预算）。
    """
    for rel in ('.git/HEAD', '.git/index', '.git'):
        p = os.path.join(BUILD_DIR, rel)
        try:
            if os.path.exists(p):
                return os.path.getmtime(p)
        except Exception:                                       # noqa: BLE001
            pass
    return None


def main():
    t0 = time.time()
    cands = find_texturetool_candidates()
    print('[pvr] texturetool candidates: %s' % ([c[1] for c in cands] or 'NONE'), flush=True)
    if not cands:
        print('[pvr] texturetool unavailable -> skip (keep all PNG)', flush=True)
        return
    if not os.path.isdir(RES_ROOT):
        print('[pvr] %s not found -> skip' % RES_ROOT, flush=True)
        return

    # ---- 截止时间：自身预算 与 (checkout+45min) 取小 ----
    # 实测校准（run 241/242）：fetch_private 稳定吃掉 31.4~31.7 分钟，
    # 之后 Configure 0.1 + Build 9.3 + Boot 1.2 + 回传/上传 0.4 ≈ 11 分钟。
    # 而整个 job 上限 60 分钟 ⇒ PVR 必须在「checkout+45min」前收手。
    now = time.time()
    deadline = t0 + TIME_BUDGET_S
    anchor = job_anchor()
    if anchor and (now - 7200) < anchor < now:      # anchor 须落在最近 2h 内才可信
        deadline = min(deadline, anchor + HARD_DEADLINE_S)
        print('[pvr] anchor=%s  hard=+%ds  self_budget=%ds  -> deadline in %.0fs'
              % (time.strftime('%H:%M:%S', time.localtime(anchor)), HARD_DEADLINE_S,
                 TIME_BUDGET_S, deadline - now), flush=True)
    else:
        print('[pvr] anchor unusable (%r) -> self budget only: deadline in %.0fs'
              % (anchor, deadline - now), flush=True)
    if deadline - now < 30:
        print('[pvr] WARN deadline only %.0fs away -> will convert very little'
              % (deadline - now), flush=True)

    atlases = []
    for dp, _dn, fn in os.walk(RES_ROOT):
        for f in fn:
            if f.endswith('.atlas'):
                atlases.append(os.path.join(dp, f))
    atlases.sort()
    print('[pvr] atlas files: %d' % len(atlases), flush=True)

    # ---- 收集待转页，按「图集总像素」降序排优先级 ----
    per_atlas = []            # (总像素, atlas_path, [(name,w,h), ...])
    skipped_nonpot = 0
    skipped_missing = 0
    for a in atlases:
        adir = os.path.dirname(a)
        pages, px = [], 0
        for name, w, h in parse_atlas(a):
            if not is_pot_square(w, h):
                skipped_nonpot += 1
                continue
            if not os.path.exists(os.path.join(adir, name)):
                skipped_missing += 1
                continue
            pages.append((name, w, h))
            px += w * h
        if pages:
            pages.sort(key=lambda t: -(t[1] * t[2]))       # 页内也按大小降序
            per_atlas.append((px, a, pages))
    per_atlas.sort(key=lambda t: -t[0])

    jobs = []                 # (atlas_path, name, png_path) —— 已按收益降序
    for _px, a, pages in per_atlas:
        adir = os.path.dirname(a)
        for name, _w, _h in pages:
            jobs.append((a, name, os.path.join(adir, name)))
    total_px = sum(p for p, _a, _pg in per_atlas)
    print('[pvr] candidates=%d  skipped_nonPOT=%d  skipped_missing=%d  eligible_px=%.0fM'
          % (len(jobs), skipped_nonpot, skipped_missing, total_px / 1e6), flush=True)
    print('[pvr] top atlases: %s'
          % ', '.join('%s(%d页)' % (os.path.relpath(a, RES_ROOT).replace(os.sep, '/'),
                                    len(pg)) for _p, a, pg in per_atlas[:5]), flush=True)

    # ---- 用真实页探针挑出可用的 texturetool（比看 --help 返回码可靠）----
    if not jobs:
        print('[pvr] no candidate pages -> skip', flush=True)
        return
    tool, argfn, how = probe_tool(cands, jobs[0][2])
    print('[pvr] encoder chosen: %s (%s)' % (tool or 'NONE', how), flush=True)
    if not tool:
        # ★ 找不到就把环境打全 + 把候选工具的 --help 原文带回本地
        #   （gh246：避免再靠猜参数浪费一轮 43 分钟构建）
        describe_tool_search()
        discover_help(cands)
        print('[pvr] no working encoder -> skip (keep all PNG)', flush=True)
        return
    print('[pvr] probe elapsed=%.1fs (单页含进程启动开销)' % (time.time() - t0), flush=True)

    # ---- 有界提交 + 截止时间硬约束（gh244 关键修正）----
    done = {}                 # png_path -> True/False
    converted = failed = timed_out = 0
    n = len(jobs)
    idx = 0
    lock = threading.Lock()

    def on_done(png, ok):
        nonlocal converted, failed
        done[png] = ok
        if ok:
            converted += 1
        else:
            failed += 1
        c = converted + failed
        if c % PROGRESS_EVERY == 0:
            el = time.time() - t0
            rate = c / el if el > 0 else 0
            left = (n - c) / rate if rate > 0 else 0
            print('[pvr] progress %d/%d  ok=%d fail=%d  %.1f pages/s  elapsed=%.0fs  eta=%.0fs'
                  % (c, n, converted, failed, rate, el, left), flush=True)

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        running = {}          # future -> png
        stop = False
        while idx < n or running:
            # 填满 worker 槽位（提交前检查截止时间）
            while not stop and idx < n and len(running) < WORKERS:
                if time.time() >= deadline:
                    stop = True
                    timed_out = n - idx
                    print('[pvr] DEADLINE hit -> stop submitting; %d pages left as PNG' % timed_out,
                          flush=True)
                    break
                _a, _nm, png = jobs[idx]
                idx += 1
                running[ex.submit(convert_one, tool, argfn, png)] = png
            if not running:
                break
            finished, _pending = cf.wait(list(running), return_when=cf.FIRST_COMPLETED, timeout=5)
            # ★ 必须把「已在跑」的任务结果全部收回来：否则它写出的 .pvr 无人登记，
            #   会变成孤儿文件（.atlas 未引用、对应 PNG 也没删）。gh244 修。
            for fut in finished:
                png = running.pop(fut)
                try:
                    ok, _msg = fut.result()
                except Exception:                               # noqa: BLE001
                    ok = False
                on_done(png, ok)
        # with 退出时 running 已空，不会像 gh243 那样等全量跑完

    print('[pvr] converted=%d failed=%d not_attempted=%d  elapsed=%.0fs'
          % (converted, failed, timed_out, time.time() - t0), flush=True)

    # ---- 兜底：清理「未被登记为成功」的孤儿 .pvr（防御性，避免留垃圾进包）----
    orphans = 0
    for _a, _nm, png in jobs:
        pvr = png[:-4] + '.pvr'
        if os.path.exists(pvr) and done.get(png) is not True:
            try:
                os.remove(pvr)
                orphans += 1
            except Exception:                                   # noqa: BLE001
                pass
    if orphans:
        print('[pvr] cleaned %d orphan .pvr' % orphans, flush=True)

    # ---- 改写 .atlas：仅把**转换成功**的页名 .png -> .pvr，并删掉对应 PNG ----
    rewritten = 0
    for a in atlases:
        adir = os.path.dirname(a)
        repl = {}
        for name, _w, _h in parse_atlas(a):
            png = os.path.join(adir, name)
            if done.get(png) is True and os.path.exists(png[:-4] + '.pvr'):
                repl[name] = name[:-4] + '.pvr'
        if not repl:
            continue
        try:
            with open(a, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.read().splitlines(True)
            out = []
            for ln in lines:
                s = ln.strip()
                # 只替换**页头行**（不缩进、以 .png 结尾且整行就是页名）
                if s in repl and not ln.startswith(' ') and ln.strip() == s:
                    out.append(ln.replace(s, repl[s]))
                else:
                    out.append(ln)
            with open(a, 'w', encoding='utf-8', newline='') as f:
                f.write(''.join(out))
            rewritten += 1
        except Exception as e:                                  # noqa: BLE001
            print('[pvr] WARN rewrite failed %s: %r' % (a, e), flush=True)
            continue
        # 删除已替换的 PNG（省包体；PVR 已就位）
        for name in repl:
            try:
                os.remove(os.path.join(adir, name))
            except Exception:                                   # noqa: BLE001
                pass
    print('[pvr] atlases rewritten=%d  total_elapsed=%.0fs' % (rewritten, time.time() - t0), flush=True)
    print('[pvr] DONE', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:                                      # noqa: BLE001
        # 绝不因本步让 CI 失败：失败就等同于"没做优化"，游戏照旧可跑。
        print('[pvr] FATAL (ignored, keeping PNG): %r' % e, flush=True)
        sys.exit(0)
