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

    2017 畸形成员通病：sizeofcmds 虚胖（真命令后面跟的是 section contents /
    符号表等数据，伪 LC）。解法 v4：
      1. 宽容走链（不受名义 sizeofcmds 限制，走到文件尾或不合理条目）；
      2. 收集所有数据引用（section/symtab/strtab/indirect/data-in-code 偏移）；
      3. 真正的 LC 区终点 = 落在走链区内的最早数据引用（数据起点）；
      4. LC 区收缩到数据起点（数据原地不动），其后所有数据偏移统一 +delta。"""
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
    for cmd, cs, cpp in cmds:
        if cmd == LC_SEGMENT_64:
            nsects, = struct.unpack_from('<I', body, cpp + 64)
            sec_base = cpp + 72
            for s in range(nsects):
                so = sec_base + s * 80
                offset = struct.unpack_from('<I', body, so + 48)[0]
                size = struct.unpack_from('<I', body, so + 40)[0]  # size 在 offset 前面(align 在 52)
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
            refs.append(cpp + 16)
            refs.append(cpp + 24)
        elif cmd == LC_DYSYMTAB:
            for idx in range(2, 18):
                fo = cpp + 16 + idx * 4
                if fo + 4 <= cpp + cs:
                    refs.append(fo)
            ioff, nind = struct.unpack_from('<II', body, cpp + 16 + 8 * 8)
            if nind > 0 and ioff > 0:
                ranges.append((ioff, min(ioff + nind * 4, file_end)))
        elif cmd == LC_DATA_IN_CODE:
            doff, dsz = struct.unpack_from('<II', body, cpp + 8)
            if dsz > 0 and doff > 0:
                ranges.append((doff, min(doff + dsz, file_end)))
            refs.append(cpp + 8)

    # ---- 3. 数据起点 = 走链区内的最早数据引用 ----
    inside = [s for (s, e) in ranges if lc_off <= s <= walked_end]  # <=:走链常恰好停在数据起点
    if inside:
        true_end = min(inside)
        kept = [(cmd, cs, cpp) for cmd, cs, cpp in cmds
                if cpp + cs <= true_end
                and cmd not in (LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX,
                                LC_VERSION_MIN_IPHONEOS)]
        old_true_sc = true_end - lc_off
    else:
        nominal_sc = struct.unpack_from('<I', body, 20)[0]  # sizeofcmds 在 Mach-O 头绝对偏移 20
        true_end = min(walked_end, lc_off + nominal_sc)
        kept = [(cmd, cs, cpp) for cmd, cs, cpp in cmds
                if cpp + cs <= true_end
                and cmd not in (LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX,
                                LC_VERSION_MIN_IPHONEOS)]
        old_true_sc = true_end - lc_off

    if not kept:
        return None

    new_sc = sum(cs for _, cs, _ in kept) + 24
    delta = new_sc - old_true_sc

    new_lc = bytearray()
    for cmd, cs, cpp in kept:
        new_lc += body[cpp:cpp+cs]
    new_lc += struct.pack('<IIIIII',
        LC_BUILD_VERSION, 24, PLATFORM_IOS_SIMULATOR,
        0x000D0000,
        0x00110500,
        0)

    new = bytearray(body)
    for fo in refs:
        v, = struct.unpack_from('<I', body, fo)
        if v != 0 and v >= true_end:
            struct.pack_into('<I', new, fo, v + delta)
    new[lc_off:true_end] = new_lc
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
            all_m = re.findall(r'm(\d+)\.o\s+malformed', err) or re.findall(r'm(\d+)\.o', err)
            if not all_m:
                logp('[prebuilt-fix] no member id in libtool error, abort retries')
                break
            idx = int(all_m[-1])   # libtool 每次报一个 offender，取最后一次提及
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
                logp('[prebuilt-fix] ld -r failed for m%d.o: %s'
                     % (idx, (r2.stderr or '')[-220:]))
                break
            with open(norm, 'rb') as f:
                nb = f.read()
            lc2 = find_lc_offset(nb)
            if lc2 is None:
                logp('[prebuilt-fix] normalized m%d.o has no LC area, use as-is' % idx)
            else:
                fixed2 = rebuild_member(nb, lc2)
                if fixed2 is not None:
                    nb = fixed2
            with open(obj_paths[idx], 'wb') as f:
                f.write(nb)
            logp('[prebuilt-fix] normalized+retagged m%d.o via ld -r, retrying' % idx)
        if r.returncode != 0:
            logp('[prebuilt-fix] FATAL libtool failed after retries: '
                 + ((r.stderr or '')[-300:]))
            # 诊断:失败成员(重建产物)与原始成员的 LC 布局对照
            all_m2 = re.findall(r'm(\d+)\.o', (r.stderr or '') + (r.stdout or ''))
            if all_m2:
                di = int(all_m2[-1])
                for tag, f in (('rebuilt', obj_paths[di]), ('original', None)):
                    src_f = f
                    if src_f is None:
                        src_f = os.path.join(tmp, 'orig%d.o' % di)
                        if not os.path.exists(src_f):
                            with open(src_f, 'wb') as f:
                                f.write(orig_bodies[di])
                    rr = subprocess.run(['otool', '-l', src_f],
                                        capture_output=True, text=True)
                    logp('[prebuilt-fix] %s m%d.o otool -l (trimmed):' % (tag, di))
                    for ln in rr.stdout.split(chr(10)):
                        if any(k in ln for k in ('cmd ', 'cmdsize', 'offset', 'symoff',
                                                 'stroff', 'nsects', 'size', 'ncmds')):
                            logp('  ' + ln.strip())
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
