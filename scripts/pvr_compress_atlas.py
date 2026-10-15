#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gh243: 把 spine 图集的页 PNG 压成 PVRTC4(RGBA)，显著降低 iOS 运行期纹理内存。

背景（JetsamEvent 实证）：
  2GB A9 iPad 上 iOS 16 单进程上限 ≈1.4GB（rpages 撞 1450MB 即被 per-process-limit 杀）。
  gh242 用「元数据模式」解决了**启动期**加载 gongfu1（1145 页）的问题，
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

安全设计（本脚本的核心原则：宁可不动，绝不改坏）：
  1. 只处理**正方形且边长为 2 的幂**的页（PVRTC 硬要求），其余原样保留 PNG；
  2. 转换后**逐个解析 .pvr 头部校验**：偏移 44 处须为 "PVR!"，且 `flags & 0xFF == 0x19`；
  3. 校验不过 ⇒ 删掉 .pvr、保留 PNG、**不改 .atlas**；
  4. 任何异常都被吞掉并计数，绝不让 CI 失败（最坏情况 = 与改动前完全一致）；
  5. 有**时间预算**，超时就停止转换（CI job 上限 60min，本步必须留足余量）。
"""
import concurrent.futures as cf
import os
import re
import struct
import subprocess
import sys
import time

# ---- 配置 ----
BUILD_DIR = os.environ.get('CM_BUILD_DIR', os.getcwd())
RES_ROOT = os.path.join(BUILD_DIR, 'gamelua')          # fetch_private.py 把 res_full 解到这里
TIME_BUDGET_S = int(os.environ.get('WJJH_PVR_BUDGET_S', '600'))   # 默认 10 分钟
WORKERS = int(os.environ.get('WJJH_PVR_WORKERS', '4'))
PVRTC4BPP_RGBA = 0x19          # cocos: PVR2TexturePixelFormat::PVRTC4BPP_RGBA
PVR_MAGIC_OFFSET = 44          # cocos PVRv2TexHeader: pvrTag 在偏移 44
PVR_FLAG_OFFSET = 16           # flags 在偏移 16
PVR_TYPE_MASK = 0xFF


def find_texturetool():
    """定位 texturetool：优先 xcrun，其次 Xcode 内的固定路径。"""
    for cmd in (['xcrun', '-sdk', 'iphoneos', 'texturetool'],
                ['xcrun', 'texturetool']):
        try:
            r = subprocess.run(cmd + ['--help'], capture_output=True, timeout=30)
            if r.returncode == 0 or b'texturetool' in (r.stdout + r.stderr).lower():
                return cmd
        except Exception:                                       # noqa: BLE001
            pass
    for p in ('/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform'
              '/Developer/usr/bin/texturetool',):
        if os.path.exists(p):
            return [p]
    return None


def is_pot_square(w, h):
    pot = lambda x: x > 0 and (x & (x - 1)) == 0
    return pot(w) and pot(h) and w == h


def verify_pvr(path):
    """校验产出的 .pvr 是否为 cocos 能吃的 PVRTC4BPP_RGBA（带 alpha）。

    返回 True 才允许替换；否则一律回退 PNG。
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(52)
        if len(head) < 52:
            return False, 'short header'
        if head[PVR_MAGIC_OFFSET:PVR_MAGIC_OFFSET + 4] != b'PVR!':
            return False, 'bad magic'
        flags = struct.unpack_from('<I', head, PVR_FLAG_OFFSET)[0]
        fmt = flags & PVR_TYPE_MASK
        if fmt != PVRTC4BPP_RGBA:
            return False, 'format=0x%02X (need 0x19 PVRTC4BPP_RGBA)' % fmt
        return True, 'ok'
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


def convert_one(tool, png_path):
    """转单页 → 返回 (ok, msg)。产出 <png>.pvr 并用 verify_pvr 校验。"""
    pvr = png_path[:-4] + '.pvr'
    try:
        if os.path.exists(pvr):
            os.remove(pvr)
        r = subprocess.run(tool + ['-f', 'PVR', '-e', 'PVRTC', '--bits-per-pixel-4',
                                   '-o', pvr, png_path],
                           capture_output=True, timeout=120)
        if not os.path.exists(pvr):
            return False, 'no output (rc=%d) %s' % (r.returncode,
                                                    (r.stderr or b'')[:80].decode('utf-8', 'replace'))
        ok, why = verify_pvr(pvr)
        if not ok:
            os.remove(pvr)
            return False, why
        return True, 'ok'
    except Exception as e:                                      # noqa: BLE001
        try:
            if os.path.exists(pvr):
                os.remove(pvr)
        except Exception:                                       # noqa: BLE001
            pass
        return False, 'exc %r' % e


def main():
    t0 = time.time()
    tool = find_texturetool()
    print('[pvr] texturetool: %s' % (tool or 'NOT FOUND'), flush=True)
    if not tool:
        print('[pvr] texturetool unavailable -> skip (keep all PNG)', flush=True)
        return
    if not os.path.isdir(RES_ROOT):
        print('[pvr] %s not found -> skip' % RES_ROOT, flush=True)
        return

    atlases = []
    for dp, _dn, fn in os.walk(RES_ROOT):
        for f in fn:
            if f.endswith('.atlas'):
                atlases.append(os.path.join(dp, f))
    atlases.sort()
    print('[pvr] atlas files: %d' % len(atlases), flush=True)

    # 收集待转页（去重：多 atlas 可能共享同页？实际不会，但保险）
    jobs = []            # (atlas_path, page_name, png_path)
    skipped_nonpot = 0
    skipped_missing = 0
    for a in atlases:
        adir = os.path.dirname(a)
        for name, w, h in parse_atlas(a):
            if not is_pot_square(w, h):
                skipped_nonpot += 1
                continue
            png = os.path.join(adir, name)
            if not os.path.exists(png):
                skipped_missing += 1
                continue
            jobs.append((a, name, png))
    print('[pvr] candidates=%d  skipped_nonPOT=%d  skipped_missing=%d'
          % (len(jobs), skipped_nonpot, skipped_missing), flush=True)

    # 并行转换（带时间预算）
    done = {}            # png_path -> True/False
    converted = 0
    failed = 0
    timed_out = 0
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(convert_one, tool, png): png for _a, _n, png in jobs}
        for fut in cf.as_completed(futs):
            png = futs[fut]
            if time.time() - t0 > TIME_BUDGET_S:
                timed_out += 1
                try:
                    fut.cancel()
                except Exception:                               # noqa: BLE001
                    pass
                continue
            try:
                ok, _msg = fut.result()
            except Exception:                                   # noqa: BLE001
                ok = False
            done[png] = ok
            if ok:
                converted += 1
            else:
                failed += 1
    print('[pvr] converted=%d failed=%d timed_out=%d  elapsed=%.0fs'
          % (converted, failed, timed_out, time.time() - t0), flush=True)

    # 改写 .atlas：仅把**转换成功**的页名 .png -> .pvr，并删掉对应 PNG
    rewritten = 0
    for a in atlases:
        adir = os.path.dirname(a)
        pages = parse_atlas(a)
        repl = {}
        for name, _w, _h in pages:
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
