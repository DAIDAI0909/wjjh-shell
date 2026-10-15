#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gh243/gh244: 把 spine 图集的页 PNG 压成 PVRTC4(RGBA)，显著降低 iOS 运行期纹理内存。

背景（JetsamEvent 实证）：
  2GB A9 iPad 上 iOS 16 单进程上限 ≈1.4GB（rpages 撞 1450MB 即被 per-process-limit 杀）。
  gh242 用「元数据模式」解决了**启动期**加载 gongfu1（1144 页）的问题，
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

---- gh244 修正（gh243 的 run 243 被 60min job 上限砍掉，教训）----
  1. **gh243 的时间预算形同虚设**：`ThreadPoolExecutor` 一次性 submit 全部 3502 个任务，
     而 `with` 退出时会 `shutdown(wait=True)` —— 必须等所有任务跑完才返回，
     所以「超预算就 continue」根本拦不住，最终跑满 28 分钟被 CI 砍掉。
     ⇒ 改为**有界提交**：提交前逐个检查截止时间，到点就停止提交，
        最多只多等 `WORKERS` 个在跑的任务。
  2. **去掉每次调用的 xcrun 开销**：`xcrun -sdk iphoneos texturetool` 每次都起一个 xcrun
     进程做 SDK 解析，且并发时可能争抢缓存。改为 `xcrun -f texturetool` 解析出**绝对路径**，
     之后直接调用该二进制。
  3. **按内存收益排序**：3502 页里 Top18 图集占 95% 收益（gongfu1+gongfu 两册就占 2764 页）。
     按「图集总像素」降序提交 ⇒ 即使预算被砍，最重要的先转完。
  4. **加进度日志**：每 100 页打印速率与预估剩余，下次可据此校准预算。
  5. **双截止时间**：`t0 + 预算` 与 `仓库 checkout 时刻 + 45min` 取小值 ——
     因为 60min 是**整个 job** 的限额，而 fetch_private 可能吃掉 25~32 分钟。

安全设计（本脚本的核心原则：宁可不动，绝不改坏）：
  1. 只处理**正方形且边长为 2 的幂**的页（PVRTC 硬要求），其余原样保留 PNG；
  2. 转换后**逐个解析 .pvr 头部校验**：偏移 44 处须为 "PVR!"，且 `flags & 0xFF == 0x19`；
  3. 校验不过 ⇒ 删掉 .pvr、保留 PNG、**不改 .atlas**；
  4. 任何异常都被吞掉并计数，绝不让 CI 失败（最坏情况 = 与改动前完全一致）；
  5. 有**双截止时间**，超时立刻停止（只等已在跑的几个任务结束）。
"""
import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time

# ---- 配置 ----
BUILD_DIR = os.environ.get('CM_BUILD_DIR', os.getcwd())
RES_ROOT = os.path.join(BUILD_DIR, 'gamelua')          # fetch_private.py 把 res_full 解到这里
TIME_BUDGET_S = int(os.environ.get('WJJH_PVR_BUDGET_S', '900'))      # 自身预算：15 分钟
HARD_DEADLINE_S = int(os.environ.get('WJJH_PVR_HARD_S', '2700'))     # 自 checkout 起 45 分钟
WORKERS = int(os.environ.get('WJJH_PVR_WORKERS', '4'))
PROGRESS_EVERY = 100
PVRTC4BPP_RGBA = 0x19          # cocos: PVR2TexturePixelFormat::PVRTC4BPP_RGBA
PVR_MAGIC_OFFSET = 44          # cocos PVRv2TexHeader: pvrTag 在偏移 44
PVR_FLAG_OFFSET = 16           # flags 在偏移 16
PVR_TYPE_MASK = 0xFF


def _tool_rel_paths():
    """texturetool 在 Xcode.app 里可能出现的位置（历史上换过）。"""
    return (
        'Contents/Developer/Platforms/iPhoneOS.platform/Developer/usr/bin/texturetool',
        'Contents/Developer/usr/bin/texturetool',
        'Contents/Developer/Platforms/iPhoneSimulator.platform/Developer/usr/bin/texturetool',
    )


def find_texturetool_candidates():
    """列出候选 texturetool 调用方式，**按速度降序**。

    ★ gh245 关键背景：**Apple 在 Xcode 16 移除了 texturetool**（run 244 实测：
    `xcrun -f texturetool` 解析失败、`xcrun -sdk iphoneos texturetool` 也失败 rc=72）。
    但 GitHub 的 macOS runner 同时装了 **Xcode 15.0.1/15.1/15.2/15.3/15.4/16.1/16.2**，
    旧版很可能仍带 texturetool ⇒ 所以这里**主动扫描 /Applications/Xcode*.app**。

    返回 [(cmd_list, 说明), ...]，调用方用真实数据探针挑第一个可用的。
    """
    cands, seen = [], set()

    def add(cmd, how):
        key = tuple(cmd)
        if key not in seen:
            seen.add(key)
            cands.append((cmd, how))

    # 1) 当前 developer dir（最快，且若存在必是首选）
    try:
        r = subprocess.run(['xcrun', '-f', 'texturetool'], capture_output=True, timeout=60)
        p = (r.stdout or b'').decode('utf-8', 'replace').strip().splitlines()
        p = p[0].strip() if p else ''
        if r.returncode == 0 and p and os.path.exists(p):
            add([p], 'xcrun -f -> %s' % p)
    except Exception:                                           # noqa: BLE001
        pass

    # 2) ★ 扫描所有已安装的 Xcode（gh245 新增）
    for app in sorted(glob.glob('/Applications/Xcode*.app')):
        for rel in _tool_rel_paths():
            p = os.path.join(app, rel)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                add([p], 'xcode-scan %s' % p)

    # 3) Command Line Tools
    for rel in _tool_rel_paths():
        p = os.path.join('/Library/Developer/CommandLineTools',
                         rel.replace('Contents/Developer/', ''))
        if os.path.isfile(p) and os.access(p, os.X_OK):
            add([p], 'commandlinetools %s' % p)

    # 4) PATH 上直接有
    w = shutil.which('texturetool')
    if w:
        add([w], 'PATH %s' % w)

    # 5) xcrun 包装（保底；慢）
    for cmd in (['xcrun', '-sdk', 'iphoneos', 'texturetool'], ['xcrun', 'texturetool']):
        add(cmd, 'xcrun wrapper (slow)')
        break

    return cands


def describe_tool_search():
    """诊断用：把「装了哪些 Xcode、各自有没有 texturetool」打全，便于下次定位。

    还会列出当前 Xcode 里名字含 tex/pvr 的可执行文件 —— 万一 Apple 只是改名了。
    """
    apps = sorted(glob.glob('/Applications/Xcode*.app'))
    print('[pvr] Xcode apps installed: %d -> %s'
          % (len(apps), [os.path.basename(a) for a in apps]), flush=True)
    for app in apps:
        hit = [rel for rel in _tool_rel_paths() if os.path.isfile(os.path.join(app, rel))]
        print('[pvr]   %-28s texturetool=%s' % (os.path.basename(app), hit or 'NONE'), flush=True)
    try:
        r = subprocess.run(['xcode-select', '-p'], capture_output=True, timeout=30)
        print('[pvr] active developer dir: %s'
              % (r.stdout or b'').decode('utf-8', 'replace').strip(), flush=True)
    except Exception:                                           # noqa: BLE001
        pass
    # 当前 Xcode 的 bin 里有没有同类工具（改名/替代的可能）
    for base in ('/Applications/Xcode*.app/Contents/Developer/Platforms/iPhoneOS.platform'
                 '/Developer/usr/bin',
                 '/Applications/Xcode*.app/Contents/Developer/usr/bin'):
        for d in glob.glob(base):
            try:
                names = sorted(os.listdir(d))
            except Exception:                                   # noqa: BLE001
                continue
            like = [n for n in names if any(k in n.lower() for k in ('tex', 'pvr', 'compress'))]
            print('[pvr]   %s -> tex/pvr-like: %s' % (d.split('/Xcode')[1][:22], like or 'NONE'),
                  flush=True)


def probe_tool(cands, sample_png):
    """用**真实页**试转一次，挑出第一个真正能产出合法 PVRTC4BPP_RGBA 的调用方式。

    这比只看 `--help` 返回码可靠得多：解析出的绝对路径可能因环境缺失而编码失败。
    探针产出的 .pvr 会删掉（主循环会重做这一页）。
    """
    if not sample_png:
        return (cands[0] if cands else (None, 'no candidate'))
    for cmd, how in cands:
        ok, msg = convert_one(cmd, sample_png)
        try:
            pvr = sample_png[:-4] + '.pvr'
            if os.path.exists(pvr):
                os.remove(pvr)
        except Exception:                                       # noqa: BLE001
            pass
        if ok:
            return cmd, how
        print('[pvr] probe failed for %s: %s' % (how, msg), flush=True)
    return None, 'all candidates failed'


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
                                                    (r.stderr or b'')[:400].decode('utf-8', 'replace'))
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


def job_anchor():
    """估算本 job 的起点（checkout 时刻）——用于对齐 60min 总额。

    GitHub Actions 在 job 开头就 checkout，所以 .git/HEAD 的 mtime ≈ job 起点 + 20s。
    拿不到就返回 None（退化为只用自身预算）。
    """
    for rel in ('.git/HEAD', '.git/index', '.git'):
        p = os.path.join(BUILD_DIR, rel)
        try:
            if os.path.exists(p):
                return os.path.getmtime(p)
        except Exception:                                       # noqa: BLE001
            pass
    return None


def main():
    t0 = time.time()
    cands = find_texturetool_candidates()
    print('[pvr] texturetool candidates: %s' % ([c[1] for c in cands] or 'NONE'), flush=True)
    if not cands:
        print('[pvr] texturetool unavailable -> skip (keep all PNG)', flush=True)
        return
    if not os.path.isdir(RES_ROOT):
        print('[pvr] %s not found -> skip' % RES_ROOT, flush=True)
        return

    # ---- 截止时间：自身预算 与 (checkout+45min) 取小 ----
    # 实测校准（run 241/242）：fetch_private 稳定吃掉 31.4~31.7 分钟，
    # 之后 Configure 0.1 + Build 9.3 + Boot 1.2 + 回传/上传 0.4 ≈ 11 分钟。
    # 而整个 job 上限 60 分钟 ⇒ PVR 必须在「checkout+45min」前收手。
    now = time.time()
    deadline = t0 + TIME_BUDGET_S
    anchor = job_anchor()
    if anchor and (now - 7200) < anchor < now:      # anchor 须落在最近 2h 内才可信
        deadline = min(deadline, anchor + HARD_DEADLINE_S)
        print('[pvr] anchor=%s  hard=+%ds  self_budget=%ds  -> deadline in %.0fs'
              % (time.strftime('%H:%M:%S', time.localtime(anchor)), HARD_DEADLINE_S,
                 TIME_BUDGET_S, deadline - now), flush=True)
    else:
        print('[pvr] anchor unusable (%r) -> self budget only: deadline in %.0fs'
              % (anchor, deadline - now), flush=True)
    if deadline - now < 30:
        print('[pvr] WARN deadline only %.0fs away -> will convert very little'
              % (deadline - now), flush=True)

    atlases = []
    for dp, _dn, fn in os.walk(RES_ROOT):
        for f in fn:
            if f.endswith('.atlas'):
                atlases.append(os.path.join(dp, f))
    atlases.sort()
    print('[pvr] atlas files: %d' % len(atlases), flush=True)

    # ---- 收集待转页，按「图集总像素」降序排优先级 ----
    per_atlas = []            # (总像素, atlas_path, [(name,w,h), ...])
    skipped_nonpot = 0
    skipped_missing = 0
    for a in atlases:
        adir = os.path.dirname(a)
        pages, px = [], 0
        for name, w, h in parse_atlas(a):
            if not is_pot_square(w, h):
                skipped_nonpot += 1
                continue
            if not os.path.exists(os.path.join(adir, name)):
                skipped_missing += 1
                continue
            pages.append((name, w, h))
            px += w * h
        if pages:
            pages.sort(key=lambda t: -(t[1] * t[2]))       # 页内也按大小降序
            per_atlas.append((px, a, pages))
    per_atlas.sort(key=lambda t: -t[0])

    jobs = []                 # (atlas_path, name, png_path) —— 已按收益降序
    for _px, a, pages in per_atlas:
        adir = os.path.dirname(a)
        for name, _w, _h in pages:
            jobs.append((a, name, os.path.join(adir, name)))
    total_px = sum(p for p, _a, _pg in per_atlas)
    print('[pvr] candidates=%d  skipped_nonPOT=%d  skipped_missing=%d  eligible_px=%.0fM'
          % (len(jobs), skipped_nonpot, skipped_missing, total_px / 1e6), flush=True)
    print('[pvr] top atlases: %s'
          % ', '.join('%s(%d页)' % (os.path.relpath(a, RES_ROOT).replace(os.sep, '/'),
                                    len(pg)) for _p, a, pg in per_atlas[:5]), flush=True)

    # ---- 用真实页探针挑出可用的 texturetool（比看 --help 返回码可靠）----
    if not jobs:
        print('[pvr] no candidate pages -> skip', flush=True)
        return
    tool, how = probe_tool(cands, jobs[0][2])
    print('[pvr] texturetool chosen: %s (%s)' % (tool or 'NONE', how), flush=True)
    if not tool:
        # ★ 找不到就把环境打全（下次不用再猜）：装了哪些 Xcode、各自有没有 texturetool、
        #   当前 Xcode 的 bin 里有没有 tex/pvr 同类工具（万一只是改名）。
        describe_tool_search()
        print('[pvr] no working texturetool -> skip (keep all PNG)', flush=True)
        return
    print('[pvr] probe elapsed=%.1fs (单页含进程启动开销)' % (time.time() - t0), flush=True)

    # ---- 有界提交 + 截止时间硬约束（gh244 关键修正）----
    done = {}                 # png_path -> True/False
    converted = failed = timed_out = 0
    n = len(jobs)
    idx = 0
    lock = threading.Lock()

    def on_done(png, ok):
        nonlocal converted, failed
        done[png] = ok
        if ok:
            converted += 1
        else:
            failed += 1
        c = converted + failed
        if c % PROGRESS_EVERY == 0:
            el = time.time() - t0
            rate = c / el if el > 0 else 0
            left = (n - c) / rate if rate > 0 else 0
            print('[pvr] progress %d/%d  ok=%d fail=%d  %.1f pages/s  elapsed=%.0fs  eta=%.0fs'
                  % (c, n, converted, failed, rate, el, left), flush=True)

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        running = {}          # future -> png
        stop = False
        while idx < n or running:
            # 填满 worker 槽位（提交前检查截止时间）
            while not stop and idx < n and len(running) < WORKERS:
                if time.time() >= deadline:
                    stop = True
                    timed_out = n - idx
                    print('[pvr] DEADLINE hit -> stop submitting; %d pages left as PNG' % timed_out,
                          flush=True)
                    break
                _a, _nm, png = jobs[idx]
                idx += 1
                running[ex.submit(convert_one, tool, png)] = png
            if not running:
                break
            finished, _pending = cf.wait(list(running), return_when=cf.FIRST_COMPLETED, timeout=5)
            # ★ 必须把「已在跑」的任务结果全部收回来：否则它写出的 .pvr 无人登记，
            #   会变成孤儿文件（.atlas 未引用、对应 PNG 也没删）。gh244 修。
            for fut in finished:
                png = running.pop(fut)
                try:
                    ok, _msg = fut.result()
                except Exception:                               # noqa: BLE001
                    ok = False
                on_done(png, ok)
        # with 退出时 running 已空，不会像 gh243 那样等全量跑完

    print('[pvr] converted=%d failed=%d not_attempted=%d  elapsed=%.0fs'
          % (converted, failed, timed_out, time.time() - t0), flush=True)

    # ---- 兜底：清理「未被登记为成功」的孤儿 .pvr（防御性，避免留垃圾进包）----
    orphans = 0
    for _a, _nm, png in jobs:
        pvr = png[:-4] + '.pvr'
        if os.path.exists(pvr) and done.get(png) is not True:
            try:
                os.remove(pvr)
                orphans += 1
            except Exception:                                   # noqa: BLE001
                pass
    if orphans:
        print('[pvr] cleaned %d orphan .pvr' % orphans, flush=True)

    # ---- 改写 .atlas：仅把**转换成功**的页名 .png -> .pvr，并删掉对应 PNG ----
    rewritten = 0
    for a in atlases:
        adir = os.path.dirname(a)
        repl = {}
        for name, _w, _h in parse_atlas(a):
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
