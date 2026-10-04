#!/usr/bin/env python3
"""Make cocos 3rd-party prebuilt .a libs usable for the arm64 iOS SIMULATOR.

2017-2020 era prebuilts: arm64 members carry either device claims
(LC_VERSION_MIN_IPHONEOS / LC_BUILD_VERSION platform=2), malformed
LC_BUILD_VERSION (16-byte, bogus platform), or no platform LC at all —
Xcode 26's linker rejects all of them for an iOS-simulator build.

Unified fix — rebuild each member's LC area:
  1. keep every LC except platform declarations (0x23/0x24/0x25);
  2. append one standard 24-byte LC_BUILD_VERSION (platform=7 iOS-sim,
     minos=13.0, sdk=17.5);
  3. resize the LC area, bump ncmds/sizeofcmds;
  4. shift every file-offset field that pointed past the old LC area by the
     area-size delta (section offsets, symtab/stroff, dysymtab offsets,
     data-in-code offset).
Members whose LC area can't be located are kept untouched. The archive is
repacked with libtool (which also regenerates the symbol index). luajit is
skipped (built from source separately).
"""
import os
import struct
import subprocess
import sys

FAT = b'\xca\xfe\xba\xbe'
M64 = b'\xcf\xfa\xed\xfe'
M32 = b'\xce\xfa\xed\xfe'
LC_SEGMENT_64 = 0x19
LC_SYMTAB = 0x2
LC_DYSYMTAB = 0xB
LC_BUILD_VERSION = 0x25
LC_VERSION_MIN_MACOSX = 0x23
LC_VERSION_MIN_IPHONEOS = 0x24
LC_DATA_IN_CODE = 0x29
PLATFORM_IOS_SIMULATOR = 7

FAIL_LOG = None


def logp(msg):
    print(msg)
    if FAIL_LOG:
        with open(FAIL_LOG, 'a', encoding='utf-8') as f:
            f.write(msg + chr(10))


def find_lc_offset(body):
    """探测 LC 区起始：28（标准）或 32（2017 工具链非标准头）。
    校验：首条 LC 必为 LC_SEGMENT_64 且整条链走通、计数吻合。"""
    if body[:4] not in (M64, M32):
        return None
    ncmds, sizeofcmds = struct.unpack_from('<II', body, 16)
    best = None
    best_score = -1
    for off in (28, 32):
        end = off + sizeofcmds
        if end > len(body) or end + 8 > len(body):
            continue
        pp = off
        ok = True
        count = 0
        score = 0
        while pp + 8 <= end:
            cmd, cmdsize = struct.unpack_from('<II', body, pp)
            if cmdsize < 8 or pp + cmdsize > end:
                ok = False
                break
            if cmd == LC_SEGMENT_64:
                score += 2
            if cmd == LC_SYMTAB:
                score += 1
            pp += cmdsize
            count += 1
        if ok and count == ncmds and score > best_score:
            best_score = score
            best = off
    return best


def parse_lcs(body, lc_off):
    """返回 [(cmd, cmdsize, old_pp)] 与 sizeofcmds。"""
    ncmds, sizeofcmds = struct.unpack_from('<II', body, 16)
    lcs = []
    pp = lc_off
    end = lc_off + sizeofcmds
    while pp + 8 <= end:
        cmd, cmdsize = struct.unpack_from('<II', body, pp)
        if cmdsize < 8 or pp + cmdsize > end:
            return lcs, sizeofcmds, True
        lcs.append((cmd, cmdsize, pp))
        pp += cmdsize
    return lcs, sizeofcmds, False


def rebuild_member(body, lc_off):
    """重建 LC 区：剔除平台声明，追加标准 LC_BUILD_VERSION(platform=7)。
    返回新 body；LC 区无法定位/重建失败返回 None（调用方保留原件）。"""
    lcs, old_sc, truncated = parse_lcs(body, lc_off)
    if truncated:
        return None

    # 第一遍：收集所有需要 +delta 的旧文件偏移字段位置（在旧 LC 区内的字段）
    offset_fields = []
    for cmd, cs, pp in lcs:
        if cmd == LC_SEGMENT_64:
            nsects, = struct.unpack_from('<I', body, pp + 64)
            sec_base = pp + 72
            for s in range(nsects):
                offset_fields.append(sec_base + s * 80 + 48)  # section offset
                offset_fields.append(sec_base + s * 80 + 60)  # reloff
        elif cmd == LC_SYMTAB:
            offset_fields.append(pp + 16)  # symoff
            offset_fields.append(pp + 24)  # stroff
        elif cmd == LC_DYSYMTAB:
            for idx in range(6, 12):       # tocoff..locreloff
                offset_fields.append(pp + 16 + idx * 4)
        elif cmd == LC_DATA_IN_CODE:
            offset_fields.append(pp + 8)   # dataoff

    kept = [(cmd, cs, pp) for cmd, cs, pp in lcs
            if cmd not in (LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX, LC_VERSION_MIN_IPHONEOS)]
    new_sc = sum(cs for _, cs, _ in kept) + 24
    delta = new_sc - old_sc

    # 组装新 LC 区：按旧顺序保留 + 末尾追加平台声明
    new_lc = bytearray()
    for cmd, cs, pp in kept:
        new_lc += body[pp:pp+cs]
    new_lc += struct.pack('<IIIIII',
        LC_BUILD_VERSION, 24, PLATFORM_IOS_SIMULATOR,
        0x000D0000,   # minos 13.0
        0x00110500,   # sdk 17.5
        0)

    new = bytearray(body)
    # 偏移修正：先按旧布局判断，再统一 +delta
    for fo in offset_fields:
        v, = struct.unpack_from('<I', body, fo)
        if v != 0 and v >= lc_off + old_sc:
            struct.pack_into('<I', new, fo, v + delta)
    # 替换 LC 区
    new[lc_off:lc_off+old_sc] = new_lc
    # 头部计数
    struct.pack_into('<I', new, 16, len(kept) + 1)
    struct.pack_into('<I', new, 20, new_sc)
    return bytes(new)


def process_archive(path):
    with open(path, 'rb') as f:
        data = f.read()
    arm64 = None
    if data[:4] == FAT:
        n, = struct.unpack_from('>I', data, 4)
        for i in range(n):
            cpu, sub, off, size, al = struct.unpack_from('>IIIII', data, 8+i*20)
            if cpu == 0x0100000C:
                arm64 = data[off:off+size]
                break
        if arm64 is None:
            logp('[prebuilt-fix] %s: no arm64 slice, skip' % os.path.basename(path))
            return 1
    elif data[:8] == b'!<arch>\n':
        arm64 = data
    else:
        return 1

    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        p = 8
        members = []
        while p + 60 <= len(arm64):
            hdr = arm64[p:p+60]
            name_field = hdr[:16].decode('ascii', 'replace')
            size = int(hdr[48:58].decode('ascii', 'replace').strip() or 0)
            content = arm64[p+60:p+60+size]
            bo = 0
            if name_field.startswith('#1/'):
                bo = int(name_field[3:].strip())
            real_name = content[:bo].rstrip(b'\x00').decode('ascii', 'replace')
            # 符号索引表：内容是旧归档布局的偏移，留着会让 ld 按失效偏移乱读；
            # libtool 重新打包时会自动重建符号索引 —— 直接丢弃
            if real_name.startswith('__.SYMDEF') or real_name == '__.SYMDEF SORTED':
                p += 60 + size + (size & 1)
                continue
            body = content[bo:]
            members.append((real_name, body))
            p += 60 + size + (size & 1)

        rebuilt = 0
        skipped = 0
        for i, (real_name, body) in enumerate(members):
            lc_off = find_lc_offset(body)
            if lc_off is None:
                skipped += 1
                logp('[prebuilt-fix] keep (no LC area) ' + real_name)
                continue
            new_body = rebuild_member(body, lc_off)
            if new_body is None:
                skipped += 1
                logp('[prebuilt-fix] keep (rebuild failed) ' + real_name)
                continue
            members[i] = (real_name, new_body)
            rebuilt += 1
        logp('[prebuilt-fix] %s: %d members, rebuilt=%d skip=%d'
             % (os.path.basename(path), len(members), rebuilt, skipped))

        for i, (real_name, body) in enumerate(members):
            with open(os.path.join(tmp, 'm%d.o' % i), 'wb') as f:
                f.write(body)
        out_a = os.path.join(tmp, 'fixed.a')
        r = subprocess.run(['libtool', '-static', '-o', out_a] +
                           [os.path.join(tmp, 'm%d.o' % i) for i in range(len(members))],
                           capture_output=True, text=True)
        if r.returncode != 0:
            logp('[prebuilt-fix] FATAL libtool failed: ' + (r.stderr or '')[-300:])
            return 1
        with open(out_a, 'rb') as f:
            fixed = f.read()
        with open(path, 'wb') as f:
            f.write(fixed)
        logp('[prebuilt-fix] replaced %s (%d bytes)' % (os.path.basename(path), len(fixed)))
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    global FAIL_LOG
    root = sys.argv[1]
    FAIL_LOG = os.path.join(os.getcwd(), 'patch_failures.txt')
    if os.path.exists(FAIL_LOG):
        os.remove(FAIL_LOG)
    count = 0
    for dirpath, dirs, files in os.walk(root):
        norm = dirpath.replace(os.sep, '/')
        if '/prebuilt/ios' not in norm or '/luajit/' in norm:
            continue
        for fn in files:
            if not fn.endswith('.a'):
                continue
            path = os.path.join(dirpath, fn)
            if process_archive(path) == 0:
                count += 1
    logp('[prebuilt-fix] archives processed: %d' % count)
    return 0


if __name__ == '__main__':
    sys.exit(main())
