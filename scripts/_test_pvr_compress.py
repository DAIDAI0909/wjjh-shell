"""gh243 集成测试：用假 texturetool 验证 PVRTC 转换 + 图集改写的完整流程。

本地无法装 macOS 的 texturetool，所以造一个行为等价的假工具：
它接受与 texturetool 相同的参数，产出一个合法的 PVRTC4BPP_RGBA PVR v2 文件。
这样就能在 Windows 上验证：
  ① 合规页被转换并改写 .atlas
  ② 非方幂页被正确跳过
  ③ region/size/xy 等其它行一字未改
  ④ 校验失败时自动回退（用另一个产坏格式的假工具）
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
import pvr_compress_atlas as P                                   # noqa: E402

GOOD_TOOL = r'''import sys, struct
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


def run_case(name, tool_src, expect_convert):
    base, tool, res, atlas = setup(tool_src)
    P.RES_ROOT = os.path.join(base, 'gamelua')
    P.find_texturetool = lambda: [sys.executable, tool]
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


def main():
    a = run_case('合法 PVRTC4BPP_RGBA 工具', GOOD_TOOL, True)
    b = run_case('产出错误格式的工具（须整体回退）', BAD_TOOL, False)
    print('=' * 62)
    print('结论：%s' % ('✅ 全部通过' if (a and b) else '❌ 有失败项'))


if __name__ == '__main__':
    main()
