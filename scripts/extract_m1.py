#!/usr/bin/env python3
"""Extract m1.o (first object member) from the patched libchipmunk.a
for otool inspection."""
import struct

data = open('cocos2d/external/chipmunk/prebuilt/ios/libchipmunk.a', 'rb').read()
p = 8
while p + 60 <= len(data):
    hdr = data[p:p+60]
    nf = hdr[:16].decode('ascii', 'replace')
    size = int(hdr[48:58].decode('ascii', 'replace').strip() or 0)
    content = data[p+60:p+60+size]
    bo = 0
    if nf.startswith('#1/'):
        bo = int(nf[3:].strip())
    name = content[:bo].rstrip(b'\x00').decode('ascii', 'replace')
    if name == 'm1.o':
        body = content[bo:]
        open('m1_check.o', 'wb').write(body)
        print('extracted', name, len(body))
        break
    p += 60 + size + (size & 1)
