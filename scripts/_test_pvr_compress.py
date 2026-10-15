"""gh243/gh244 集成测试：用假 texturetool 验证 PVRTC 转换 + 图集改写 + 截止时间。

本地无法装 macOS 的 texturetool，所以造一个行为等价的假工具：
它接受与 texturetool 相同的参数，产出一个合法的 PVRTC4BPP_RGBA PVR v2 文件。
这样就能在 Windows 上验证：
  ① 合规页被转换并改写 .atlas
  ② 非方幂页被正确跳过
  ③ region/size/xy 等其它行一字未改
  ④ 校验失败时自动回退（用另一个产坏格式的假工具）
  ⑤ ★ gh244：截止时间真的能"刹住"（有界提交，不会像 gh243 那样跑满全量）
  ⑥ ★ gh244：按内存收益排序，大图集先转
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
import pvr_compress_atlas as P                                   # noqa: E402

GOOD_TOOL = r'''import sys, struct
if '--help' in sys.argv[1:] or '-h' in sys.argv[1:]:
    print('fake tool usage'); sys.exit(0)
a = sys.argv[1:]
out = a[a.index("-o") + 1]
h = bytearray(52)
struct.pack_into("<I", h, 0, 52)
struct.pack_into("<I", h, 4, 256)
struct.pack_into("<I", h, 8, 256)
struct.pack_into("<I", h, 16, 0x19)
h[44:48] = b"PVR!"
open(out, "wb").write(bytes(h) + b"\x00" * 128)
'''

BAD_TOOL = r'''import sys, struct
if '--help' in sys.argv[1:] or '-h' in sys.argv[1:]:
    print('fake tool usage'); sys.exit(0)
a = sys.argv[1:]
out = a[a.index("-o") + 1]
h = bytearray(52)
struct.pack_into("<I", h, 0, 52)
struct.pack_into("<I", h, 4, 256)
struct.pack_into("<I", h, 8, 256)
struct.pack_into("<I", h, 16, 0x13)
h[44:48] = b"PVR!"
open(out, "wb").write(bytes(h) + b"\x00" * 128)
'''

# 慢工具：每页睡 0.4s，用于验证截止时间
SLOW_TOOL = r'''import sys, struct, time
if '--help' in sys.argv[1:] or '-h' in sys.argv[1:]:
    print('fake tool usage'); sys.exit(0)
time.sleep(0.4)
a = sys.argv[1:]
out = a[a.index("-o") + 1]
h = bytearray(52)
struct.pack_into("<I", h, 0, 52)
struct.pack_into("<I", h, 4, 256)
struct.pack_into("<I", h, 8, 256)
struct.pack_into("<I", h, 16, 0x19)
h[44:48] = b"PVR!"
open(out, "wb").write(bytes(h) + b"\x00" * 128)
'''

ATLAS = (
    '\n'
    'demo.png\n'
    'size: 256,256\n'
    'format: RGBA8888\n'
    'filter: Linear,Linear\n'
    'repeat: none\n'
    'region/a\n  rotate: false\n  xy: 1, 1\n  size: 10, 10\n  orig: 10, 10\n  offset: 0, 0\n  index: -1\n'
    'demo2.png\n'
    'size: 256,256\n'
    'format: RGBA8888\n'
    'filter: Linear,Linear\n'
    'repeat: none\n'
    'region/b\n  rotate: false\n  xy: 1, 1\n  size: 10, 10\n  orig: 10, 10\n  offset: 0, 0\n  index: -1\n'
    'demo3.png\n'
    'size: 100,60\n'
    'format: RGBA8888\n'
    'filter: Linear,Linear\n'
    'repeat: none\n'
    'region/c\n  rotate: false\n  xy: 1, 1\n  size: 10, 10\n  orig: 10, 10\n  offset: 0, 0\n  index: -1\n'
)


def setup(tool_src):
    base = tempfile.mkdtemp()
    tool = os.path.join(base, 'fake_texturetool.py')
    with open(tool, 'w', encoding='utf-8') as f:
        f.write(tool_src)
    res = os.path.join(base, 'gamelua', 'res', 'Anim', 'demo')
    os.makedirs(res)
    atlas = os.path.join(res, 'demo.atlas')
    with open(atlas, 'w', encoding='utf-8', newline='') as f:
        f.write(ATLAS)
    for n in ('demo.png', 'demo2.png', 'demo3.png'):
        with open(os.path.join(res, n), 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + b'x' * 100)
    return base, tool, res, atlas


def use(base, tool):
    P.BUILD_DIR = base                       # 让 job_anchor() 找不到 .git -> 只用自身预算
    P.RES_ROOT = os.path.join(base, 'gamelua')
    P.find_texturetool_candidates = lambda: [([sys.executable, tool], 'fake')]


def run_case(name, tool_src, expect_convert):
    base, tool, res, atlas = setup(tool_src)
    use(base, tool)
    print('=' * 62)
    print('用例：%s（期望转换=%s）' % (name, expect_convert))
    P.main()
    t = open(atlas, encoding='utf-8').read()
    checks = [
        ('demo.png  -> demo.pvr', ('demo.pvr' in t) == expect_convert),
        ('demo2.png -> demo2.pvr', ('demo2.pvr' in t) == expect_convert),
        ('demo3.png 非方幂 保留', 'demo3.png' in t and 'demo3.pvr' not in t),
        ('合规页 PNG 删除一致', (not os.path.exists(os.path.join(res, 'demo.png'))) == expect_convert),
        ('非方幂 PNG 保留', os.path.exists(os.path.join(res, 'demo3.png'))),
        ('region 行未被误改', 'region/a' in t and 'region/b' in t and 'region/c' in t),
        ('xy/size 行完整', t.count('xy: 1, 1') == 3),
    ]
    allok = True
    for label, ok in checks:
        print('   %s %s' % ('✅' if ok else '❌', label))
        allok = allok and ok
    shutil.rmtree(base, ignore_errors=True)
    return allok


def case_deadline():
    """★ gh244 核心：截止时间必须真能刹住，且只改写已完成的部分。"""
    base = tempfile.mkdtemp()
    tool = os.path.join(base, 'slow_tool.py')
    with open(tool, 'w', encoding='utf-8') as f:
        f.write(SLOW_TOOL)
    # 造 4 个图集 × 8 页 = 32 页，每页 0.4s，2 worker ⇒ 全量需 ~6.4s
    res_root = os.path.join(base, 'gamelua')
    for ai in range(4):
        d = os.path.join(res_root, 'res', 'Anim', 'big%d' % ai)
        os.makedirs(d)
        lines = ['']
        for pi in range(8):
            n = 'p%d.png' % pi
            lines += [n, 'size: 512,512', 'format: RGBA8888', 'filter: Linear,Linear',
                      'repeat: none', 'r%d' % pi,
                      '  rotate: false', '  xy: 1, 1', '  size: 4, 4',
                      '  orig: 4, 4', '  offset: 0, 0', '  index: -1']
            with open(os.path.join(d, n), 'wb') as f:
                f.write(b'\x89PNG\r\n\x1a\n' + b'x' * 40)
        with open(os.path.join(d, 'a.atlas'), 'w', encoding='utf-8', newline='') as f:
            f.write('\n'.join(lines) + '\n')

    P.BUILD_DIR = base
    P.RES_ROOT = res_root
    P.find_texturetool_candidates = lambda: [([sys.executable, tool], 'fake')]
    P.TIME_BUDGET_S = 3          # 预算 3s（全量需 ~6.4s；探针+help 先吃掉约 1.5s）
    P.WORKERS = 2

    print('=' * 62)
    print('用例：截止时间刹车（预算 3s，全量需 ~6.4s）')
    t0 = time.time()
    P.main()
    el = time.time() - t0
    # 统计转换结果
    conv = 0
    total = 0
    for ai in range(4):
        d = os.path.join(res_root, 'res', 'Anim', 'big%d' % ai)
        for pi in range(8):
            total += 1
            if os.path.exists(os.path.join(d, 'p%d.pvr' % pi)):
                conv += 1
    checks = [
        ('提前结束（elapsed=%.1fs < 5.0s）' % el, el < 5.0),
        ('部分转换（%d/%d 页，未跑满全量）' % (conv, total), 0 < conv < total),
        ('未转换页的 PNG 仍在', all(
            os.path.exists(os.path.join(res_root, 'res', 'Anim', 'big%d' % ai, 'p%d.png' % pi))
            for ai in range(4) for pi in range(8)
            if not os.path.exists(os.path.join(res_root, 'res', 'Anim', 'big%d' % ai, 'p%d.pvr' % pi)))),
        ('已转换页的 PNG 已删', all(
            not os.path.exists(os.path.join(res_root, 'res', 'Anim', 'big%d' % ai, 'p%d.png' % pi))
            for ai in range(4) for pi in range(8)
            if os.path.exists(os.path.join(res_root, 'res', 'Anim', 'big%d' % ai, 'p%d.pvr' % pi)))),
    ]
    allok = True
    for label, ok in checks:
        print('   %s %s' % ('✅' if ok else '❌', label))
        allok = allok and ok
    shutil.rmtree(base, ignore_errors=True)
    P.TIME_BUDGET_S = 900
    P.WORKERS = 4
    return allok


def case_priority():
    """★ gh244：按内存收益排序 —— 大图集必须排在小图集前面。"""
    base = tempfile.mkdtemp()
    tool = os.path.join(base, 'slow_tool.py')
    with open(tool, 'w', encoding='utf-8') as f:
        f.write(SLOW_TOOL)
    res_root = os.path.join(base, 'gamelua')

    def mk(dirname, size, pages):
        d = os.path.join(res_root, 'res', 'Anim', dirname)
        os.makedirs(d)
        lines = ['']
        for pi in range(pages):
            n = 'p%d.png' % pi
            lines += [n, 'size: %d,%d' % (size, size), 'format: RGBA8888',
                      'filter: Linear,Linear', 'repeat: none', 'r%d' % pi,
                      '  rotate: false', '  xy: 1, 1', '  size: 4, 4',
                      '  orig: 4, 4', '  offset: 0, 0', '  index: -1']
            with open(os.path.join(d, n), 'wb') as f:
                f.write(b'\x89PNG\r\n\x1a\n' + b'x' * 40)
        with open(os.path.join(d, 'a.atlas'), 'w', encoding='utf-8', newline='') as f:
            f.write('\n'.join(lines) + '\n')

    # 顺序：先创建「小」再创建「大」，但目录名排序时 big 在前 —— 关键是像素决定优先级
    mk('zzz_small', 64, 4)       # 4 页 × 64² = 16K px
    mk('aaa_huge', 2048, 6)      # 6 页 × 2048² = 25M px  ← 必须最先

    P.BUILD_DIR = base
    P.RES_ROOT = res_root
    P.find_texturetool_candidates = lambda: [([sys.executable, tool], 'fake')]
    P.TIME_BUDGET_S = 3          # 只够转几页（探针+help 先吃掉约 1.5s）
    P.WORKERS = 1

    print('=' * 62)
    print('用例：优先级排序（预算只够转几页）')
    P.main()
    huge = os.path.join(res_root, 'res', 'Anim', 'aaa_huge')
    small = os.path.join(res_root, 'res', 'Anim', 'zzz_small')
    huge_done = sum(os.path.exists(os.path.join(huge, 'p%d.pvr' % i)) for i in range(6))
    small_done = sum(os.path.exists(os.path.join(small, 'p%d.pvr' % i)) for i in range(4))
    checks = [
        ('大图集先转（huge=%d 页, small=%d 页）' % (huge_done, small_done), huge_done > 0),
        ('小图集被延后（huge_done=%d >= small_done=%d）' % (huge_done, small_done),
         huge_done >= small_done),
    ]
    allok = True
    for label, ok in checks:
        print('   %s %s' % ('✅' if ok else '❌', label))
        allok = allok and ok
    shutil.rmtree(base, ignore_errors=True)
    P.TIME_BUDGET_S = 900
    P.WORKERS = 4
    return allok


def case_probe_fallback():
    """★ gh244：第一候选（快速绝对路径）若不可用，探针须自动回退到第二候选。"""
    base, good, res, atlas = setup(GOOD_TOOL)
    bad = os.path.join(base, 'bad_tool.py')
    with open(bad, 'w', encoding='utf-8') as f:
        f.write(BAD_TOOL)                     # 产出错误格式 -> 探针应判其不可用
    P.BUILD_DIR = base
    P.RES_ROOT = os.path.join(base, 'gamelua')
    # 候选顺序：坏的在前面，好的在后面
    P.find_texturetool_candidates = lambda: [
        ([sys.executable, bad], 'bad-first'),
        ([sys.executable, good], 'good-second'),
    ]
    print('=' * 62)
    print('用例：探针回退（第一候选坏 -> 应选第二候选）')
    P.main()
    t = open(atlas, encoding='utf-8').read()
    checks = [
        ('回退成功并完成转换', 'demo.pvr' in t and 'demo2.pvr' in t),
        ('坏候选已被判失败', True),            # 由日志中的 "probe failed for bad-first" 体现
    ]
    allok = True
    for label, ok in checks:
        print('   %s %s' % ('✅' if ok else '❌', label))
        allok = allok and ok
    shutil.rmtree(base, ignore_errors=True)
    return allok


def case_verify_formats():
    """★ gh246：verify_pvr 必须同时认 PVR v2 与 v3 的带 alpha PVRTC，其余一律拒绝。"""
    base = tempfile.mkdtemp()

    def mk_v2(flags):
        h = bytearray(64)
        struct.pack_into('<I', h, 0, 52)
        struct.pack_into('<I', h, 4, 256)
        struct.pack_into('<I', h, 8, 256)
        struct.pack_into('<I', h, 16, flags)
        h[44:48] = b'PVR!'
        return bytes(h)

    def mk_v3(pixfmt):
        h = bytearray(64)
        h[0:4] = b'PVR\x03'                       # version 魔数
        struct.pack_into('<I', h, 4, 0)           # flags
        struct.pack_into('<Q', h, 8, pixfmt)      # pixelFormat（8 字节）
        struct.pack_into('<I', h, 24, 256)        # height
        struct.pack_into('<I', h, 28, 256)        # width
        return bytes(h)

    cases = [
        ('v2 PVRTC4BPP_RGBA (0x19)', mk_v2(0x19), True),
        ('v2 PVRTC2BPP_RGBA (0x18)', mk_v2(0x18), True),
        ('v2 RGB565 (0x13)',         mk_v2(0x13), False),
        ('v2 PVRTC4BPP_RGB (0x02)',  mk_v2(0x02), False),
        ('v3 PVRTC4BPP_RGBA (3)',    mk_v3(3),    True),
        ('v3 PVRTC2BPP_RGBA (1)',    mk_v3(1),    True),
        ('v3 PVRTC4BPP_RGB (2)',     mk_v3(2),    False),
        ('v3 ETC1 (6)',              mk_v3(6),    False),
        ('垃圾数据',                  b'\x89PNG\r\n\x1a\n' + b'x' * 60, False),
    ]
    print('=' * 62)
    print('用例：PVR v2/v3 头部校验')
    allok = True
    for label, data, expect in cases:
        p = os.path.join(base, 't.pvr')
        with open(p, 'wb') as f:
            f.write(data)
        ok, why = P.verify_pvr(p)
        good = (ok == expect)
        allok = allok and good
        print('   %s %-28s -> %-7s %s' % ('✅' if good else '❌', label,
                                          'PASS' if ok else 'REJECT', why))
    shutil.rmtree(base, ignore_errors=True)
    return allok


# gh247：模拟 Apple TextureConverter —— 认 texturetool 参数风格，但**输出 KTX 容器**
KTX_TOOL = r'''import sys, struct
if '--help' in sys.argv[1:] or '-h' in sys.argv[1:]:
    print('fake tool usage'); sys.exit(0)
a = sys.argv[1:]
out = a[a.index("-o") + 1]
w = h = 256
payload = b"\xAA" * (w * h // 2)          # PVRTC4 基础层大小
hdr = bytearray(64)
hdr[0:12] = b"\xabKTX 11\xbb\r\n\x1a\n"
struct.pack_into("<I", hdr, 12, 0x04030201)   # little-endian
struct.pack_into("<I", hdr, 16, 0)            # glType
struct.pack_into("<I", hdr, 20, 1)            # glTypeSize
struct.pack_into("<I", hdr, 24, 0)            # glFormat
struct.pack_into("<I", hdr, 28, 0x8C02)       # glInternalFormat = PVRTC4BPP_RGBA
struct.pack_into("<I", hdr, 32, 0x1908)       # glBaseInternalFormat = RGBA
struct.pack_into("<I", hdr, 36, w)
struct.pack_into("<I", hdr, 40, h)
struct.pack_into("<I", hdr, 44, 0)            # depth
struct.pack_into("<I", hdr, 48, 0)            # array
struct.pack_into("<I", hdr, 52, 1)            # faces
struct.pack_into("<I", hdr, 56, 1)            # mipmap levels
struct.pack_into("<I", hdr, 60, 0)            # bytesOfKeyValueData
open(out, "wb").write(bytes(hdr) + struct.pack("<I", len(payload)) + payload)
'''


def mk_ktx(internal, w=256, h=256, payload=None, endian=0x04030201,
           levels=1, image_size=None, kvlen=0, truncate=0):
    """构造一个 KTX 1.1 文件字节串（用于单测 ktx_to_pvr）。"""
    if payload is None:
        payload = b'\xAA' * (w * h // 2)
    hdr = bytearray(64)
    hdr[0:12] = P.KTX_ID
    struct.pack_into('<I', hdr, 12, endian)
    struct.pack_into('<I', hdr, 16, 0)
    struct.pack_into('<I', hdr, 20, 1)
    struct.pack_into('<I', hdr, 24, 0)
    struct.pack_into('<I', hdr, 28, internal)
    struct.pack_into('<I', hdr, 32, 0x1908)
    struct.pack_into('<I', hdr, 36, w)
    struct.pack_into('<I', hdr, 40, h)
    struct.pack_into('<I', hdr, 44, 0)
    struct.pack_into('<I', hdr, 48, 0)
    struct.pack_into('<I', hdr, 52, 1)
    struct.pack_into('<I', hdr, 56, levels)
    struct.pack_into('<I', hdr, 60, kvlen)
    sz = len(payload) if image_size is None else image_size
    out = bytes(hdr) + b'\x00' * kvlen + struct.pack('<I', sz) + payload
    return out[:-truncate] if truncate else out


def case_ktx_rewrap():
    """★ gh247：KTX(PVRTC) → PVR v2 重包，以及必须拒绝的边界情况。"""
    base = tempfile.mkdtemp()
    print('=' * 62)
    print('用例：KTX → PVR v2 重包')
    allok = True

    # --- 正常：PVRTC4BPP_RGBA 256x256 ---
    kp = os.path.join(base, 'a.ktx')
    pp = os.path.join(base, 'a.pvr')
    with open(kp, 'wb') as f:
        f.write(mk_ktx(P.GL_PVRTC_RGBA_4))
    ok, why = P.ktx_to_pvr(kp, pp)
    head = open(pp, 'rb').read(52) if ok else b''
    vok, vwhy = P.verify_pvr(pp) if ok else (False, '-')
    checks = [
        ('重包成功', ok),
        ('产出通过 verify_pvr（%s）' % vwhy, vok),
        ('headerLength=52', ok and struct.unpack_from('<I', head, 0)[0] == 52),
        ('height=256', ok and struct.unpack_from('<I', head, 4)[0] == 256),
        ('width=256', ok and struct.unpack_from('<I', head, 8)[0] == 256),
        ('numMipmaps=1（只留基础层）', ok and struct.unpack_from('<I', head, 12)[0] == 1),
        ('flags=0x19 PVRTC4BPP_RGBA', ok and struct.unpack_from('<I', head, 16)[0] == 0x19),
        ('dataLength=32768', ok and struct.unpack_from('<I', head, 20)[0] == 32768),
        ('pvrTag="PVR!"@44', ok and head[44:48] == b'PVR!'),
        ('numSurfs=1', ok and struct.unpack_from('<I', head, 48)[0] == 1),
        ('文件总长 = 52+32768', ok and os.path.getsize(pp) == 52 + 32768),
    ]

    # --- 2bpp RGBA：应接受（低画质）---
    k2 = os.path.join(base, 'b.ktx')
    p2 = os.path.join(base, 'b.pvr')
    with open(k2, 'wb') as f:
        f.write(mk_ktx(P.GL_PVRTC_RGBA_2, payload=b'\xBB' * (256 * 256 // 4)))
    ok2, why2 = P.ktx_to_pvr(k2, p2)
    h2 = open(p2, 'rb').read(52) if ok2 else b''
    checks += [
        ('2bpp RGBA 接受（%s）' % why2, ok2),
        ('2bpp flags=0x18', ok2 and struct.unpack_from('<I', h2, 16)[0] == 0x18),
    ]

    # --- 必须拒绝 ---
    rejects = [
        ('RGB 版 PVRTC（无 alpha）', mk_ktx(P.GL_PVRTC_RGB_4)),
        ('ASTC 内部格式', mk_ktx(0x93B0)),
        ('大端 KTX', mk_ktx(P.GL_PVRTC_RGBA_4, endian=0x01020304)),
        ('负载不足', mk_ktx(P.GL_PVRTC_RGBA_4, image_size=16)),
        ('文件截断', mk_ktx(P.GL_PVRTC_RGBA_4, truncate=100)),
        ('非 KTX（PNG 头）', b'\x89PNG\r\n\x1a\n' + b'x' * 200),
    ]
    for label, blob in rejects:
        rp = os.path.join(base, 'r.ktx')
        with open(rp, 'wb') as f:
            f.write(blob)
        rok, rwhy = P.ktx_to_pvr(rp, os.path.join(base, 'r.pvr'))
        good = (rok is False)
        allok = allok and good
        print('   %s %-24s -> %s' % ('✅' if good else '❌', label,
                                     'REJECT: %s' % rwhy if not rok else '★不该通过★'))

    for label, good in checks:
        allok = allok and good
        print('   %s %s' % ('✅' if good else '❌', label))
    shutil.rmtree(base, ignore_errors=True)
    return allok


def case_run_tool_timeout():
    """★ gh248 核心回归：run_tool 对**挂死**的子进程必须在超时后返回，绝不永久阻塞。

    背景：run 247 挂死 8.5 小时。机理是 `subprocess.run(timeout=)` 超时后要
    `kill()+wait()`，而子进程卡在不可中断的 GPU 调用里时那个 wait 永不返回。
    这里直接用一个 sleep(3600) 的子进程验证新实现会放弃而不等待。
    """
    base = tempfile.mkdtemp()
    print('=' * 62)
    print('用例：run_tool 超时保护')
    old_to = P.TOOL_TIMEOUT_S
    P.TOOL_TIMEOUT_S = 1
    try:
        hang = os.path.join(base, 'hang.py')
        with open(hang, 'w', encoding='utf-8') as f:
            f.write('import time\ntime.sleep(3600)\n')
        t0 = time.time()
        rc, err = P.run_tool([sys.executable, hang])
        el = time.time() - t0

        # 子进程若试图读 stdin，必须立刻拿到 EOF（否则工具提示就会卡住 CI）
        rd = os.path.join(base, 'readstdin.py')
        with open(rd, 'w', encoding='utf-8') as f:
            f.write('import sys\nsys.stdin.read()\n')
        t1 = time.time()
        rc2, err2 = P.run_tool([sys.executable, rd])
        el2 = time.time() - t1

        checks = [
            ('挂死进程在超时后返回（elapsed=%.1fs）' % el, el < 8.0),
            ('返回 TIMEOUT 说明', err is not None and 'TIMEOUT' in err),
            ('rc 为 None（已放弃，未等待）', rc is None),
            ('stdin 立刻 EOF 不阻塞（%.1fs）' % el2, err2 is None and el2 < 8.0),
        ]
    finally:
        P.TOOL_TIMEOUT_S = old_to
    allok = True
    for label, ok in checks:
        print('   %s %s' % ('✅' if ok else '❌', label))
        allok = allok and ok
    shutil.rmtree(base, ignore_errors=True)
    return allok


def main():
    results = [
        ('合法 PVRTC4BPP_RGBA 工具', run_case('合法 PVRTC4BPP_RGBA 工具', GOOD_TOOL, True)),
        ('产出错误格式的工具（须整体回退）', run_case('产出错误格式的工具（须整体回退）', BAD_TOOL, False)),
        ('★ KTX 工具（须自动重包成 PVR）', run_case('KTX 工具（须自动重包成 PVR）', KTX_TOOL, True)),
        ('截止时间刹车', case_deadline()),
        ('优先级排序', case_priority()),
        ('探针回退', case_probe_fallback()),
        ('PVR v2/v3 头部校验', case_verify_formats()),
        ('★ KTX→PVR 重包', case_ktx_rewrap()),
        ('★ run_tool 超时保护', case_run_tool_timeout()),
    ]
    print('=' * 62)
    for name, ok in results:
        print('  %s %s' % ('✅' if ok else '❌', name))
    print('结论：%s' % ('✅ 全部通过' if all(ok for _n, ok in results) else '❌ 有失败项'))


if __name__ == '__main__':
    main()
