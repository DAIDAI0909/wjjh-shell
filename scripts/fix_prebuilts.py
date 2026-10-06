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
import re
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
    返回新 body；LC 区无法定位/重建失败返回 None（调用方保留原件）。
    特例：部分 2017 成员的符号表数据长在 LC 区内部（libtool 报 "symbol table ...
    overlaps Mach-O headers"）——先把 [lo,hi) 整段搬迁到文件尾，再做常规 LC 重建；
    LC 区变长拼接后所有区内指针 +delta、搬迁段的指针 +delta，两段分开结算。"""
    lcs, old_sc, truncated = parse_lcs(body, lc_off)
    if truncated:
        return None

    symtab_span = None
    for cmd, cs, pp in lcs:
        if cmd == LC_SYMTAB:
            symoff, nsyms, stroff, strsize = struct.unpack_from('<IIII', body, pp + 8)
            lo = min(symoff, stroff)
            hi = max(symoff + nsyms * 16, stroff + strsize)
            if nsyms > 0 and strsize > 0 and lo != 0 and lo < lc_off + old_sc:
                symtab_span = (symoff, nsyms, stroff, strsize, lo, hi)
                break

    span_pad = 0
    span_len = 0
    new_lo_end = 0
    if symtab_span is not None:
        symoff, nsyms, stroff, strsize, lo, hi = symtab_span
        span = body[lo:hi]
        span_pad = (-len(span)) % 8
        span_len = len(span) + span_pad
        new = bytearray(body[:lo]) + bytearray(body[hi:])
        new_lo_end = len(new)
        new += span + bytes(span_pad)
        new_symoff = symoff - lo + new_lo_end if symoff >= lo else symoff
        new_stroff = stroff - lo + new_lo_end if stroff >= lo else stroff
        for cmd, cs, pp in lcs:
            if cmd == LC_SYMTAB:
                struct.pack_into('<IIII', new, pp + 8,
                                 new_symoff, nsyms, new_stroff, strsize)
            elif cmd == LC_DYSYMTAB:
                for idx in range(2, 18):
                    fo = pp + 16 + idx * 4
                    if fo + 4 <= pp + cs:
                        v, = struct.unpack_from('<I', body, fo)
                        if lo <= v < hi:
                            struct.pack_into('<I', new, fo, v - lo + new_lo_end)
        body = bytes(new)

    # 第一遍：收集 LC 区内所有文件偏移字段（数据段 section/reloff、symtab、dysymtab、data-in-code）
    offset_fields = []
    for cmd, cs, pp in lcs:
        if cmd == LC_SEGMENT_64:
            nsects, = struct.unpack_from('<I', body, pp + 64)
            sec_base = pp + 72
            for s in range(nsects):
                offset_fields.append(sec_base + s * 80 + 48)
                offset_fields.append(sec_base + s * 80 + 60)
        elif cmd == LC_SYMTAB:
            offset_fields.append(pp + 16)
            offset_fields.append(pp + 24)
        elif cmd == LC_DYSYMTAB:
            for idx in range(2, 18):
                fo = pp + 16 + idx * 4
                if fo + 4 <= pp + cs:
                    offset_fields.append(fo)
        elif cmd == LC_DATA_IN_CODE:
            offset_fields.append(pp + 8)

    kept = [(cmd, cs, pp) for cmd, cs, pp in lcs
            if cmd not in (LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX, LC_VERSION_MIN_IPHONEOS)]
    new_sc = sum(cs for _, cs, _ in kept) + 24
    delta = new_sc - old_sc

    new_lc = bytearray()
    for cmd, cs, pp in kept:
        new_lc += body[pp:pp+cs]
    new_lc += struct.pack('<IIIIII',
        LC_BUILD_VERSION, 24, PLATFORM_IOS_SIMULATOR,
        0x000D0000,
        0x00110500,
        0)

    new = bytearray(body)
    for fo in offset_fields:
        v, = struct.unpack_from('<I', body, fo)
        if v != 0 and v >= lc_off + old_sc:
            struct.pack_into('<I', new, fo, v + delta)
    new[lc_off:lc_off+old_sc] = new_lc
    struct.pack_into('<I', new, 16, len(kept) + 1)
    struct.pack_into('<I', new, 20, new_sc)

    # LC 区 +delta 拼接后：搬迁段的指针补上 delta（搬迁时写的是拼接前的位置）
    if symtab_span is not None:
        pp = lc_off
        end = lc_off + new_sc
        while pp + 8 <= end:
            cmd, cs = struct.unpack_from('<II', new, pp)
            if cmd == LC_SYMTAB:
                so, ns, to, ss = struct.unpack_from('<IIII', new, pp + 8)
                if so >= new_lo_end:
                    struct.pack_into('<IIII', new, pp + 8,
                                     so + delta, ns, to + delta, ss)
                break
            pp += cs
        span_base = new_lo_end + delta
        span_top = span_base + span_len
        pp = lc_off
        while pp + 8 <= end:
            cmd, cs = struct.unpack_from('<II', new, pp)
            if cmd == LC_DYSYMTAB:
                for idx in range(2, 18):
                    fo = pp + 16 + idx * 4
                    if fo + 4 <= pp + cs:
                        v, = struct.unpack_from('<I', new, fo)
                        if new_lo_end <= v < new_lo_end + span_len:
                            struct.pack_into('<I', new, fo, v + delta)
            pp += cs
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

        orig_bodies = {i: body for i, (real_name, body) in enumerate(members)}
        for i, (real_name, body) in enumerate(members):
            with open(os.path.join(tmp, 'm%d.o' % i), 'wb') as f:
                f.write(body)
        out_a = os.path.join(tmp, 'fixed.a')
        obj_paths = [os.path.join(tmp, 'm%d.o' % i) for i in range(len(members))]
        normalized = set()
        for attempt in range(6):
            r = subprocess.run(['libtool', '-static', '-o', out_a] + obj_paths,
                               capture_output=True, text=True)
            if r.returncode == 0:
                break
            err = (r.stderr or '') + (r.stdout or '')
            logp('[prebuilt-fix] libtool attempt %d failed: %s' % (attempt, err[-300:]))
            # 从报错里找失败成员号（mNN.o）
            m = re.search(r'(?:object:.*?|/)(m(\d+)\.o)\s+malformed', err)
            if not m:
                m = re.search(r'(m(\d+)\.o)\s*[^\n]*?(?:malformed|is not an object|unknown)', err)
            if not m:
                break
            idx = int(m.group(2))
            if idx >= len(obj_paths) or idx in normalized:
                break
            normalized.add(idx)
            # 用【原始成员】跑经典 ld -r 归一化（它对畸形头极其宽容），
            # 归一化产物再做一次 LC 平台重建，替换成员文件后重试 libtool
            orig = os.path.join(tmp, 'orig%d.o' % idx)
            with open(orig, 'wb') as f:
                f.write(orig_bodies[idx])
            norm = os.path.join(tmp, 'norm%d.o' % idx)
            r2 = subprocess.run(['ld', '-r', '-ld_classic', '-o', norm, orig],
                                capture_output=True, text=True)
            if r2.returncode != 0 or not os.path.exists(norm) or os.path.getsize(norm) == 0:
                logp('[prebuilt-fix] ld -r failed for %s: %s'
                     % (m.group(1), (r2.stderr or '')[-220:]))
                break
            with open(norm, 'rb') as f:
                nb = f.read()
            lc2 = find_lc_offset(nb)
            if lc2 is None:
                logp('[prebuilt-fix] normalized %s has no LC area, use as-is' % m.group(1))
            else:
                fixed2 = rebuild_member(nb, lc2)
                if fixed2 is not None:
                    nb = fixed2
            with open(obj_paths[idx], 'wb') as f:
                f.write(nb)
            logp('[prebuilt-fix] normalized+retagged %s via ld -r, retrying' % m.group(1))
        if r.returncode != 0:
            logp('[prebuilt-fix] FATAL libtool failed after retries: '
                 + ((r.stderr or '')[-300:]))
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
