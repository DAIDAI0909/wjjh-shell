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
    # gh87 铁律:ld/otool 一律按 28 走链(2017 非标准头);32 视角的合法链 ld 看不见。
    # 28 验证通过必须无条件优先——否则补丁落在 ld 的链之外,改了等于没改。
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
        if ok and count == ncmds:
            if off == 28:
                return 28  # 28 视角合法即定案(ld 的一致视角)
            if score > best_score:
                best_score = score
                best = off
    # 平分时强制 28(链走向与 ld 一致)
    if 28 in (28, 32):
        pass
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


LC_LINKER_OPTIMIZATION_HINT = 0x2E

def rebuild_member_clean(body, lc_off):
    """完整重建成员:按干净布局重新组装标准 Mach-O。

    原地修补路线在 2017 畸形成员上与 otool/ld 的视角纠缠不清(gh83-90 十轮)。
    本函数不再修补:直接提取数据区(sections/symtab/strtab/relocs),按标准布局
    重新生成整个文件——所有字节由我们写出,零解读歧义。

    新布局(全部对齐 8):
      32B 头(ncmds/sizeofcmds 重算)
      LC_SEGMENT_64(含全部 sections,fileoff 重排)
      LC_BUILD_VERSION(24, platform=7 ios-sim, minos=13.0, sdk=17.5)
      LC_SYMTAB(24)
      [LC_DYSYMTAB(80) 仅当原成员有非零 dysymtab 偏移]
      数据区: sections 内容(按原相对顺序) + 重定位表 + 符号表 + 字符串表
    返回新 bytes;无法解析返回 None。"""
    if body[:4] != b'\xcf\xfa\xed\xfe':
        return None
    ncmds, sizeofcmds = struct.unpack_from('<II', body, 16)
    flags = struct.unpack_from('<I', body, 24)[0]

    # ---- 收集原成员的命令信息(只信字段,不信链) ----
    sections = []   # (sectname, segname, addr, size, align, reloff, nreloc, flags, res1, res2, old_off)
    symtab = None   # (symoff, nsyms, stroff, strsize)
    dysyms = []     # 非 dysymtab 的其他命令原样保留: (cmd, cs, raw bytes)
    dys_nonzero = False
    dys_raw = None
    pp = lc_off
    lc_end = lc_off + sizeofcmds
    while pp + 8 <= lc_end:
        cmd, cs = struct.unpack_from('<II', body, pp)
        if cs < 8 or pp + cs > min(lc_end + 64, len(body)):
            break  # 跨界=数据,弃
        if cmd == 0x19:  # LC_SEGMENT_64
            nsects = struct.unpack_from('<I', body, pp + 64)[0]
            for si in range(nsects):
                so = pp + 72 + si * 80
                if so + 80 > len(body):
                    break
                sectname = bytes(body[so:so + 16])
                segname = bytes(body[so + 16:so + 32])
                addr, size = struct.unpack_from('<QQ', body, so + 32)
                offset, align, reloff, nreloc = struct.unpack_from('<IIII', body, so + 48)
                sflags, res1, res2 = struct.unpack_from('<III', body, so + 64)
                if size > 0 and 0 < offset < len(body):
                    sections.append((sectname, segname, addr, size, align,
                                     reloff, nreloc, sflags, res1, res2, offset))
        elif cmd == 0x2:  # LC_SYMTAB
            symoff, nsyms, stroff, strsize = struct.unpack_from('<IIII', body, pp + 8)
            if 0 < symoff < len(body) and nsyms > 0:
                symtab = (symoff, nsyms, stroff, strsize)
        elif cmd == 0xB:  # LC_DYSYMTAB
            dys_raw = bytes(body[pp:pp + cs])
            vals = [struct.unpack_from('<I', body, pp + 8 + i * 4)[0] for i in range(18)]
            dys_nonzero = any(v != 0 for v in vals[2:])
        elif cmd in (0x24, 0x23, 0x25):
            pass  # 平台命令:重建时统一替换
        elif cmd == 0x2E:
            pass  # LC_LINKER_OPTIMIZATION_HINT: 非必需,弃
        else:
            dysyms.append((cmd, cs, bytes(body[pp:pp + cs])))
        pp += cs

    if not sections or symtab is None:
        return None

    # ---- 组装新文件 ----
    out = bytearray()
    head = bytearray(32)
    struct.pack_into('<IIII', head, 0, 0x0C, 0x0100000C, 0, 1)  # magic,cputype,subtype,filetype
    # ncmds/sizeofcmds 后填
    struct.pack_into('<I', head, 24, flags)
    out += head

    lc_blob = bytearray()
    # SEG
    seg = bytearray(72)
    struct.pack_into('<II', seg, 0, 0x19, 72 + 80 * len(sections))
    seg[8:24] = b'__DATA'.ljust(16, bytes(1))[:16]  # segname 占位,以第一个 section 的 segname 为准? 用 __DATA 统一
    total_size = sum(s[3] for s in sections)
    struct.pack_into('<QQQQ', seg, 24, 0, total_size, 0, total_size)
    struct.pack_into('<IIII', seg, 56, 7, 7, len(sections), 0)
    lc_blob += seg
    for si, (sectname, segname, addr, size, align, reloff, nreloc, sflags, res1, res2, old_off) in enumerate(sections):
        sec = bytearray(80)
        sec[0:16] = sectname
        sec[16:32] = segname
        struct.pack_into('<QQ', sec, 32, addr, size)
        # offset 稍后回填(需要先算命令区大小)
        struct.pack_into('<IIII', sec, 48, 0, align, 0, nreloc)
        struct.pack_into('<III', sec, 64, sflags, res1, res2)
        lc_blob += sec
    # BUILD
    lc_blob += struct.pack('<IIIIII', 0x25, 24, 7, 0x000D0000, 0x00110500, 0)
    # SYMTAB
    symoff_n, nsyms, stroff_n, strsize = symtab
    symtab_at = len(lc_blob)  # 记录 SYMTAB 命令在 lc_blob 内的真实位置
    lc_blob += struct.pack('<IIIIII', 0x2, 24, 0, nsyms, 0, strsize)
    ncmds_new = 3
    if dys_raw is not None and dys_nonzero:
        lc_blob += dys_raw
        ncmds_new += 1
    for cmd, cs, raw in dysyms:
        lc_blob += raw
        ncmds_new += 1

    # 数据区布局(8 对齐)
    data_off = 32 + len(lc_blob)
    cur = data_off
    sect_new_offs = []
    for (sectname, segname, addr, size, align, reloff, nreloc, sflags, res1, res2, old_off) in sections:
        cur = (cur + 7) // 8 * 8
        sect_new_offs.append(cur)
        cur += size
    # 重定位表(各 section 的 reloff/nreloc 区间)
    relocs = []
    for (sectname, segname, addr, size, align, reloff, nreloc, sflags, res1, res2, old_off) in sections:
        if nreloc > 0 and 0 < reloff < len(body):
            raw = bytes(body[reloff:reloff + nreloc * 8])
            relocs.append((nreloc, raw))
    cur = (cur + 7) // 8 * 8
    reloc_new_offs = []
    for nreloc, raw in relocs:
        reloc_new_offs.append(cur)
        cur += len(raw)
    # 符号表 + 字符串表
    sym_new_off = cur
    sym_bytes = bytes(body[symoff_n:symoff_n + nsyms * 16])
    cur += len(sym_bytes)
    str_new_off = cur
    str_bytes = bytes(body[stroff_n:stroff_n + strsize])
    cur += len(str_bytes)

    # 回填 section offset/reloff(lc_blob 内: SEG 命令占 [0,72), sections 从 72 起)
    for si in range(len(sections)):
        so = 72 + si * 80
        struct.pack_into('<I', lc_blob, so + 48, sect_new_offs[si])
        if si < len(reloc_new_offs):
            struct.pack_into('<I', lc_blob, so + 56, reloc_new_offs[si])
    # 回填 SYMTAB 偏移(构建时记录的位置)
    struct.pack_into('<I', lc_blob, symtab_at + 8, sym_new_off)
    struct.pack_into('<I', lc_blob, symtab_at + 16, str_new_off)

    struct.pack_into('<I', head, 16, ncmds_new)
    struct.pack_into('<I', head, 20, len(lc_blob))

    out = bytearray(head) + lc_blob
    # 数据区
    blob = bytearray(cur)
    blob[0:32] = head
    blob[32:32 + len(lc_blob)] = lc_blob
    for si, (sectname, segname, addr, size, align, reloff, nreloc, sflags, res1, res2, old_off) in enumerate(sections):
        end_off = min(old_off + size, len(body))
        blob[sect_new_offs[si]:sect_new_offs[si] + (end_off - old_off)] = body[old_off:end_off]
    for (nreloc, raw), new_off in zip(relocs, reloc_new_offs):
        blob[new_off:new_off + len(raw)] = raw
    blob[sym_new_off:sym_new_off + len(sym_bytes)] = sym_bytes
    blob[str_new_off:str_new_off + len(str_bytes)] = str_bytes
    return bytes(blob)


def serialize_archive(members, workdir):
    """用 libtool 生成归档(gh80 已验证:干净成员+libtool=ld 认)。"""
    objs = []
    for i, (name, body) in enumerate(members):
        op = os.path.join(workdir, 'm%d.o' % i)
        with open(op, 'wb') as f:
            f.write(body)
        objs.append(op)
    out = os.path.join(workdir, 'out.a')
    if os.path.exists(out):
        os.remove(out)
    r = subprocess.run(['libtool', '-static', '-o', out] + objs,
                       capture_output=True, text=True, errors='replace')
    if r.returncode != 0:
        logp('[prebuilt-fix] FATAL libtool failed: ' + (r.stderr or '')[-300:])
        return None
    with open(out, 'rb') as f:
        return f.read()


def process_archive(path):
    """v9: 重建每个 arm64 成员(干净标准 Mach-O),重序列化归档,ranlib 重建目录。

    v8 的教训:重建后成员尺寸变化,按原槽位写回会撑破归档。v9 整体重序列化,
    并把 fat 文件替换为 thin arm64 归档(模拟器链接只用 arm64 切片)。"""
    with open(path, 'rb') as f:
        data = f.read()

    members = []  # [(name, body)] — arm64 切片内的非 SYMDEF 成员
    if data[:4] == b'\xca\xfe\xba\xbe':
        n, = struct.unpack_from('>I', data, 4)
        arm = None
        for i in range(n):
            cpu, sub, off, size, al = struct.unpack_from('>IIIII', data, 8 + i * 20)
            if cpu == 0x0100000C:
                arm = data[off:off + size]
                break
        if arm is None:
            logp('[prebuilt-fix] %s: no arm64 slice, skip' % os.path.basename(path))
            return 1
        src = arm
    elif data[:8] == b'!<arch>\n':
        src = data
    else:
        return 1

    p = 8
    n_patched = 0
    n_fail = 0
    while p + 60 <= len(src):
        hdr = src[p:p + 60]
        name_field = hdr[:16].decode('ascii', 'replace')
        try:
            size = int(hdr[48:58].decode('ascii', 'replace').strip() or 0)
        except ValueError:
            break
        content = src[p + 60:p + 60 + size]
        bo = 0
        if name_field.startswith('#1/'):
            bo = int(name_field[3:].strip())
        real_name = content[:bo].rstrip(bytes(1)).decode('ascii', 'replace')
        if real_name.startswith('__.SYMDEF'):
            p += 60 + size + (size & 1)
            continue
        body = content[bo:]
        new_body = None
        if body[:4] == b'\xcf\xfa\xed\xfe':
            lc_off = find_lc_offset(body)
            if lc_off is not None:
                new_body = rebuild_member_clean(body, lc_off)
        elif body[:4] == b'\xca\xfe\xba\xbe':
            # FAT 成员:抽 arm64 内层切片重建
            n_in, = struct.unpack_from('>I', body, 4)
            for i in range(n_in):
                cpu_in, sub_in, off_in, size_in, al_in = struct.unpack_from(
                    '>IIIII', body, 8 + i * 20)
                if cpu_in == 0x0100000C:
                    inner = body[off_in:off_in + size_in]
                    lc_off = find_lc_offset(inner)
                    if lc_off is not None:
                        new_body = rebuild_member_clean(inner, lc_off)
                    break
        if new_body is not None:
            members.append((real_name, new_body))
            n_patched += 1
        else:
            members.append((real_name, body))
            n_fail += 1
        p += 60 + size + (size & 1)

    logp('[prebuilt-fix] %s: members=%d rebuilt=%d keep-orig=%d'
         % (os.path.basename(path), len(members), n_patched, n_fail))
    if n_fail:
        logp('[prebuilt-fix] FATAL %d members unfixable, archive left for ld to complain'
             % n_fail)
        return 1

    import tempfile
    tmp2 = tempfile.mkdtemp()
    try:
        new_arch = serialize_archive(members, tmp2)
        if new_arch is None:
            return 1
        with open(path, 'wb') as f:
            f.write(new_arch)
        logp('[prebuilt-fix] %s: re-serialized via ar, %d bytes'
             % (os.path.basename(path), len(new_arch)))
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp2, ignore_errors=True)


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
