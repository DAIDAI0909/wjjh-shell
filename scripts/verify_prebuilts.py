#!/usr/bin/env python3
"""Verify platform stamps of arm64 members in patched prebuilt archives."""
import os
import struct
import sys

M64 = b'\xcf\xfa\xed\xfe'
M32 = b'\xce\xfa\xed\xfe'


def find_lc_offset(body):
    if body[:4] not in (M64, M32):
        return None
    ncmds, sizeofcmds = struct.unpack_from('<II', body, 16)
    for off in (28, 32):
        end = off + sizeofcmds
        if end > len(body) or end + 8 > len(body):
            continue
        first_cmd, = struct.unpack_from('<I', body, off)
        if first_cmd != 0x19:
            continue
        pp = off
        ok = True
        count = 0
        while pp + 8 <= end:
            cmd, cmdsize = struct.unpack_from('<II', body, pp)
            if cmdsize < 8 or pp + cmdsize > end:
                ok = False
                break
            pp += cmdsize
            count += 1
        if ok and count == ncmds:
            return off
    return None


def scan(path):
    import hashlib
    with open(path, 'rb') as f:
        data = f.read()
    print('  md5:', hashlib.md5(data).hexdigest())
    arm64 = None
    if data[:4] == b'\xca\xfe\xba\xbe':
        n, = struct.unpack_from('>I', data, 4)
        for i in range(n):
            cpu, sub, off, size, al = struct.unpack_from('>IIIII', data, 8+i*20)
            if cpu == 0x0100000C:
                arm64 = data[off:off+size]
                break
    elif data[:8] == b'!<arch>\n':
        arm64 = data
    if arm64 is None:
        print('%s: no arm64' % os.path.basename(path))
        return
    print('%s: arm64 %d bytes' % (os.path.basename(path), len(arm64)))
    p = 8
    n_macho = 0
    stamps = {}
    nostamp = []
    while p + 60 <= len(arm64):
        hdr = arm64[p:p+60]
        nf = hdr[:16].decode('ascii', 'replace')
        size = int(hdr[48:58].decode('ascii', 'replace').strip() or 0)
        content = arm64[p+60:p+60+size]
        bo = 0
        if nf.startswith('#1/'):
            bo = int(nf[3:].strip())
        body = content[bo:]
        if body[:4] in (M64, M32):
            n_macho += 1
            lc = find_lc_offset(body)
            if lc is None:
                nostamp.append(nf.strip())
            else:
                ncmds, sizeofcmds = struct.unpack_from('<II', body, 16)
                end = min(lc + sizeofcmds, len(body))
                pp = lc
                plat = 'none'
                while pp + 8 <= end:
                    cmd, cmdsize = struct.unpack_from('<II', body, pp)
                    if cmdsize < 8:
                        break
                    if cmd == 0x25:
                        plat = 'build%d' % struct.unpack_from('<I', body, pp+8)[0]
                    if cmd in (0x23, 0x24):
                        plat = 'min' + ('MAC' if cmd == 0x23 else 'IOS')
                    pp += cmdsize
                stamps[plat] = stamps.get(plat, 0) + 1
        p += 60 + size + (size & 1)
    print('  macho members:', n_macho, '| stamps:', stamps)
    if nostamp:
        print('  NO-LC-OFFSET members:', nostamp[:5])


def main():
    for path in sys.argv[1:]:
        try:
            scan(path)
        except Exception as e:
            print(path, 'ERR', repr(e))


if __name__ == '__main__':
    main()
