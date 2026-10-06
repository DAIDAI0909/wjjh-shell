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

    2017 畸形成员通病：section contents / 符号表等数据与 LC 区尾部重叠
    （libtool 报 "overlaps Mach-O headers"）。解法 v5：
      1. 宽容走链（不受名义 sizeofcmds 限制）；
      2. 数据起点 = 落在走链区内的最早数据引用（section offset/symoff/stroff...）；
      3. LC 区收缩到数据起点：真命令保留；空间不够时丢弃"全零 LC_DYSYMTAB"
         （无间接符号表时该命令可安全省略，ld 接受无 DYSYMTAB 的可重定位对象）；
      4. 数据原地不动，其后所有数据偏移统一 +delta（delta 常为负=数据前移）。"""
    file_end = len(body)

    # ---- 1. 宽容走链 ----
    cmds = []
    pp = lc_off
    while pp + 8 <= file_end:
        cmd, cs = struct.unpack_from('<II', body, pp)
        if cs < 8 or pp + cs > file_end:
            break
        cmds.append((cmd, cs, pp))
        pp += cs
    walked_end = pp

    # ---- 2. 收集数据引用与数据区间 ----
    ranges = []
    refs = []
    dys = []  # (cmd, cs, pp, 全零?) — LC_DYSYMTAB 可丢弃候选
    for cmd, cs, cpp in cmds:
        if cmd == LC_SEGMENT_64:
            nsects, = struct.unpack_from('<I', body, cpp + 64)
            sec_base = cpp + 72
            for s in range(nsects):
                so = sec_base + s * 80
                offset = struct.unpack_from('<I', body, so + 48)[0]
                size = struct.unpack_from('<I', body, so + 40)[0]
                if size > 0 and offset > 0:
                    ranges.append((offset, min(offset + size, file_end)))
                refs.append(so + 48)
                refs.append(so + 60)
        elif cmd == LC_SYMTAB:
            symoff, nsyms, stroff, strsize = struct.unpack_from('<IIII', body, cpp + 8)
            if nsyms > 0 and symoff > 0:
                ranges.append((symoff, min(symoff + nsyms * 16, file_end)))
            if strsize > 0 and stroff > 0:
                ranges.append((stroff, min(stroff + strsize, file_end)))
            refs.append(cpp + 8)   # symoff
            refs.append(cpp + 16)  # stroff
        elif cmd == LC_DYSYMTAB:
            for idx in (2, 4, 6, 8, 10):  # tocoff/modtaboff/extrefsymoff/indirectsymoff/extreloff
                fo = cpp + 8 + idx * 4
                if fo + 4 <= cpp + cs:
                    refs.append(fo)
            ioff, nind = struct.unpack_from('<II', body, cpp + 8 + 8 * 4)  # indirectsymoff/nindirectsyms
            if nind > 0 and ioff > 0:
                ranges.append((ioff, min(ioff + nind * 4, file_end)))
            dys.append((cmd, cs, cpp))  # 一律登记:空间不够时丢弃(DYSYMTAB 对 .o 可省)
        elif cmd == LC_DATA_IN_CODE:
            doff, dsz = struct.unpack_from('<II', body, cpp + 8)
            if dsz > 0 and doff > 0:
                ranges.append((doff, min(doff + dsz, file_end)))
            refs.append(cpp + 8)

    # ---- 3. 数据起点与 LC 区收缩 ----
    inside = [s for (s, e) in ranges if lc_off <= s <= walked_end]
    if not inside:
        return None
    true_end = min(inside)
    space = true_end - lc_off

    # 真命令 = 完整落在 [lc_off, true_end) 内的非平台命令
    kept = []
    dropped_dys = []
    pp = lc_off
    while pp + 8 <= true_end:
        cmd, cs = struct.unpack_from('<II', body, pp)
        if cs < 8:
            return None
        if pp + cs > true_end:
            break  # 跨界命令=数据
        if cmd in (LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX, LC_VERSION_MIN_IPHONEOS):
            pp += cs
            continue
        # 本成员的 LC_DYSYMTAB 若全零，先记账（空间不够时丢弃）
        dys_here = [d for d in dys if d[2] == pp]
        if dys_here:
            dropped_dys.append((cmd, cs, pp, True))  # 可丢
        else:
            kept.append((cmd, cs, pp))
        pp += cs

    used = sum(cs for _, cs, _ in kept)
    # 空间不够：丢 DYSYMTAB（大者先丢,对 .o 可省略）
    if used + 24 > space:
        for cmd, cs, cpp in sorted(dys, key=lambda d: -d[1]):
            if cpp + cs <= true_end and any(p2 == cpp for _, _, p2 in kept):
                kept = [(c, s2, p2) for c, s2, p2 in kept if p2 != cpp]
                used = sum(s2 for _, s2, _ in kept)
                if used + 24 <= space:
                    break

    new_sc = used + 24
    delta = new_sc - space
    if used + 24 > space:
        logp('[prebuilt-fix] member at %d: LC area cannot shrink below data start '
             '(need %d have %d), skip' % (lc_off, used + 24, space))
        return None

    new = bytearray(body)
    for fo in refs:
        v, = struct.unpack_from('<I', body, fo)
        if v != 0 and v >= true_end:
            struct.pack_into('<I', new, fo, v + delta)
    # kept 命令字节从 delta 修正后的 new 取,否则 kept 内的字段偏移(如 section offset)被还原
    new_lc = bytearray()
    for cmd, cs, cpp in kept:
        new_lc += new[cpp:cpp+cs]
    new_lc += struct.pack('<IIIIII',
        LC_BUILD_VERSION, 24, PLATFORM_IOS_SIMULATOR,
        0x000D0000,
        0x00110500,
        0)
    new[lc_off:true_end] = new_lc
    struct.pack_into('<I', new, 16, len(kept) + 1)
    struct.pack_into('<I', new, 20, new_sc)
    return bytes(new)


def patch_macho_lcs(body, lc_off):
    """原地改写单个 Mach-O 的平台声明。返回是否改动。"""
    patched = False
    sizeofcmds = struct.unpack_from('<I', body, 20)[0]
    pp = lc_off
    end = lc_off + sizeofcmds
    while pp + 8 <= end:
        cmd, cs = struct.unpack_from('<II', body, pp)
        if cs < 8 or pp + cs > end:
            break
        if cmd in (LC_VERSION_MIN_IPHONEOS, LC_VERSION_MIN_MACOSX):
            struct.pack_into('<IIIIII', body, pp,
                             LC_BUILD_VERSION, 24,
                             PLATFORM_IOS_SIMULATOR,
                             0x000D0000, 0x00110500, 0)
            patched = True
        elif cmd == LC_BUILD_VERSION:
            plat = struct.unpack_from('<I', body, pp + 8)[0]
            if plat != PLATFORM_IOS_SIMULATOR:
                struct.pack_into('<I', body, pp + 8, PLATFORM_IOS_SIMULATOR)
                patched = True
        pp += cs
    return patched


def process_archive(path):
    """v7: 原地改写平台声明，不做任何重打包。

    2017 预编译 arm64 切片的问题只有一个：平台标签（真机/错平台/缺失）。
    LC_VERSION_MIN_IPHONEOS/MACOSX 与 LC_BUILD_VERSION 同为 24 字节——原地改写
    （cmd 0x24/0x23→0x25，platform=7 iOS-sim，minos=13.0，sdk=17.5）即可，
    文件尺寸零变化、零偏移重排、libtool 完全不参与。
    好处：2017 成员普遍存在的符号表/重定位表重叠等畸形原样保留——
    ld -ld_classic 对这些完全宽容（x86_64 线二十轮验证），只有 libtool 的
    严格校验会拒收。原先重打包路线反而把宽容的 ld 挡在 libtool 门外。"""
    with open(path, 'rb') as f:
        data = bytearray(f.read())
    n_patched = 0
    n_noplat = 0

    def patch_slice(buf):
        nonlocal n_patched, n_noplat
        p = 8
        while p + 60 <= len(buf):
            hdr = buf[p:p + 60]
            name_field = hdr[:16].decode('ascii', 'replace')
            try:
                size = int(hdr[48:58].decode('ascii', 'replace').strip() or 0)
            except ValueError:
                break
            content = buf[p + 60:p + 60 + size]
            bo = 0
            if name_field.startswith('#1/'):
                bo = int(name_field[3:].strip())
            real_name = content[:bo].rstrip(bytes(1)).decode('ascii', 'replace')
            if real_name.startswith('__.SYMDEF'):
                p += 60 + size + (size & 1)
                continue
            body = content[bo:]
            if body[:4] == FAT:
                # 成员本身是 FAT(armv7+arm64 合体,2017 库常见):钻进去改 arm64 切片
                n_in, = struct.unpack_from('>I', body, 4)
                any_patched = False
                for i in range(n_in):
                    cpu_in, sub_in, off_in, size_in, al_in = struct.unpack_from(
                        '>IIIII', body, 8 + i * 20)
                    if cpu_in != 0x0100000C:
                        continue
                    inner = bytearray(body[off_in:off_in + size_in])
                    lc_off2 = find_lc_offset(inner)
                    if lc_off2 is None:
                        continue
                    got = patch_macho_lcs(inner, lc_off2)
                    if got:
                        body[off_in:off_in + size_in] = inner
                        any_patched = True
                if any_patched:
                    n_patched += 1
                    buf[p + 60 + bo:p + 60 + size] = body
                continue
            lc_off = find_lc_offset(body)
            if lc_off is not None:
                if patch_macho_lcs(body, lc_off):
                    n_patched += 1
                    buf[p + 60 + bo:p + 60 + size] = body
            p += 60 + size + (size & 1)

    if data[:4] == FAT:
        n, = struct.unpack_from('>I', data, 4)
        arm64_idx = None
        for i in range(n):
            cpu, sub, off, size, al = struct.unpack_from('>IIIII', data, 8 + i * 20)
            if cpu == 0x0100000C:
                arm64_idx = i
                break
        if arm64_idx is None:
            logp('[prebuilt-fix] %s: no arm64 slice, skip' % os.path.basename(path))
            return 1
        off, size = struct.unpack_from('>II', data, 8 + arm64_idx * 20 + 8)
        slice_buf = bytearray(data[off:off + size])
        patch_slice(slice_buf)
        data[off:off + size] = slice_buf
    elif data[:8] == b'!<arch>\n':
        patch_slice(data)
    else:
        return 1

    with open(path, 'wb') as f:
        f.write(data)
    logp('[prebuilt-fix] %s: in-place patched=%d no-platform=%d'
         % (os.path.basename(path), n_patched, n_noplat))
    return 0


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
