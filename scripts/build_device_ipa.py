#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gh211: 真机(iOS device)构建直通车。

触发:仓库根存在 DEVICE_BUILD_ON 标记文件时,模拟器侧 CMake configure 提前返回并挂一个
ALL 目标调用本脚本(见 CMakeLists 顶部)。本脚本完成:
  1) py_ios_bootstrap.py --device   (真机切片 Python.framework/pyhome/server)
  2) cmake configure build_device/  (PLATFORM=OS64 + WJJH_DEVICE=ON,与模拟器零冲突)
  3) cmake --build                  (引擎/壳/源编库全部 iphoneos 三元组)
  4) 打包 Payload/ → WJJH_dev_unsigned_gh<N>.ipa (未签名;由 Sideloadly/爱思/签名商重签)
  5) 回传:日志 POST /report,IPA POST /put(服务器流式落 /opt/wjjh_ci/pub/)

任何一步失败都尽力回传日志(CI 迭代定位);退出码仅日志可见(yml 的 `| tee` 会吞)。
"""
import http.client
import os
import shutil
import subprocess
import sys
import time

ROOT = os.environ.get('GITHUB_WORKSPACE') or os.getcwd()
BUILD = os.path.join(ROOT, 'build_device')
RUN_NO = os.environ.get('GITHUB_RUN_NUMBER', 'local')
LOG = os.path.join(ROOT, 'device_build_output.txt')

HOST = '134.175.113.201'
GAME_URL = os.environ.get('WJJH_GAME_URL', '')
TOKEN = GAME_URL.split('t=')[-1] if 't=' in GAME_URL else ''

_logf = None


def log(msg):
    global _logf
    line = '[device] %s' % msg
    print(line, flush=True)
    try:
        if _logf is None:
            _logf = open(LOG, 'a', encoding='utf-8', errors='replace')
        _logf.write(line + '\n')
        _logf.flush()
    except Exception:
        pass


def run(cmd, cwd=None, timeout=3600):
    log('RUN: %s' % ' '.join(cmd))
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=cwd, timeout=timeout,
                           capture_output=True, text=True, errors='replace')
        tail = (r.stdout or '') + (r.stderr or '')
        for ln in tail.splitlines()[-500:]:
            log('  | %s' % ln)
        log('RC=%d (%.0fs)' % (r.returncode, time.time() - t0))
        return r.returncode
    except subprocess.TimeoutExpired:
        log('!! TIMEOUT after %ds' % timeout)
        return 124


def upload(name, path):
    """流式 POST:/put 用于大文件(IPA),/report 用于日志。

    gh255: 加重试。run 254 实测 `OSError(51, 'Network is unreachable')` ——
    runner 出网偶发不可达,一次抖动就让 60 分钟的构建产物彻底丢失
    (那次既没进 Release,artifact 里也只有 495B 的诊断包)。
    重试只针对**连接层异常**(requests 意义上的"还没发出去"),文件每轮重新打开。
    """
    if not TOKEN:
        log('!! no token (WJJH_GAME_URL missing) — skip upload %s' % name)
        return False
    route = '/put' if name.endswith('.ipa') else '/report'
    size = os.path.getsize(path) if os.path.exists(path) else 0
    log('UPLOAD %s (%d bytes) -> %s' % (name, size, route))
    last = None
    for attempt in range(3):
        if attempt:
            wait = (5, 15, 30)[attempt]
            log('  upload retry #%d in %ds (last=%r)' % (attempt, wait, last))
            time.sleep(wait)
        try:
            conn = http.client.HTTPConnection(HOST, 80, timeout=1800)
            with open(path, 'rb') as f:
                conn.request('POST', '%s?name=%s&t=%s' % (route, name, TOKEN),
                             body=f, headers={'Content-Length': str(size)})
            resp = conn.getresponse()
            body = resp.read()[:200]
            log('  upload resp: %d %r' % (resp.status, body))
            conn.close()
            if resp.status == 200:
                return True
            last = 'HTTP %d' % resp.status
        except Exception as e:
            last = e
            log('  upload failed: %r' % e)
            try:
                conn.close()
            except Exception:
                pass
    log('!! upload gave up after 3 attempts: %r' % (last,))
    return False


def get_gh_token():
    """gh212: 取 runner 的自动令牌——Actions 不默认注入 GITHUB_TOKEN 环境变量,
    但 actions/checkout(persist-credentials 默认开)会把令牌写进本地 git config 的
    http.extraheader;从那里解出来。仓库已把 default_workflow_permissions 提为 write
    (classic 令牌改的仓库设置,不动 yml),该令牌具备 contents:write,可发 Release。"""
    tok = os.environ.get('GITHUB_TOKEN') or ''
    if tok:
        return tok
    try:
        r = subprocess.run(['git', 'config', '--get',
                            'http.https://github.com/.extraheader'],
                           capture_output=True, text=True, cwd=ROOT)
        v = (r.stdout or '').strip()
        import base64
        low = v.lower()
        if low.startswith('authorization: basic '):
            dec = base64.b64decode(v.split(' ', 2)[2]).decode()
            # x-access-token:TOKEN
            return dec.split(':', 1)[1]
    except Exception as e:
        log('token extract failed: %r' % e)
    return ''


def gh_request(host, method, path, tok, body=None, ctype=None, timeout=120):
    """gh255: 连接层加重试。

    run 254 实测:构建成功(IPA 已 ditto 出来)但 `gh_request('api.github.com',...)`
    抛 `OSError(51, 'Network is unreachable')` ⇒ release 里没有资产、artifact 里也没有
    IPA,整轮白跑。runner 出网偶发不可达,重试是最小代价的兜底。
    body 是文件时必须每轮 seek(0),否则第二轮发出去的是空体。
    """
    import json
    headers = {'Authorization': 'Bearer ' + tok, 'User-Agent': 'wjjh-device-ipa',
               'Accept': 'application/vnd.github+json'}
    if ctype:
        headers['Content-Type'] = ctype
    if body is not None and hasattr(body, 'read'):
        headers['Content-Length'] = str(os.path.getsize(body.name))
    last = None
    for attempt in range(3):
        if attempt:
            wait = (5, 15, 30)[attempt]
            log('  gh_request retry #%d in %ds (%s %s, last=%r)'
                % (attempt, wait, method, path.split('?')[0], last))
            time.sleep(wait)
        if body is not None and hasattr(body, 'seek'):
            try:
                body.seek(0)
            except Exception:
                pass
        conn = None
        try:
            conn = http.client.HTTPSConnection(host, timeout=timeout)
            conn.request(method, path, body=body, headers=headers)
            r = conn.getresponse()
            data = r.read()
            conn.close()
            conn = None
            try:
                js = json.loads(data)
            except Exception:
                js = None
            # 5xx 视为可重试；4xx 是确定性错误，直接返回
            if r.status >= 500:
                last = 'HTTP %d' % r.status
                continue
            return r.status, js
        except Exception as e:
            last = e
            log('  gh_request failed: %r' % e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    log('!! gh_request gave up after 3 attempts (%s %s): %r' % (method, path, last))
    return 0, None


def upload_release(ipa_path):
    """gh212: GitHub Release 快速回传(滚动 tag=device-ipa)——CI(美区数据中心)→GitHub
    是数据中心内通道(快);本地再经 Clash 取回。服务器 /put 通道美→中实测仅 ~33KB/s,
    382MB 要 3 小时,弃用。"""
    import json
    tok = get_gh_token()
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    if not tok or not repo:
        log('!! release upload skipped (token=%s repo=%s)' % (bool(tok), repo))
        return False
    tag = 'device-ipa'
    name = os.path.basename(ipa_path)
    rid = None
    st, js = gh_request('api.github.com', 'GET', '/repos/%s/releases/tags/%s' % (repo, tag), tok)
    if st == 200 and js:
        rid = js['id']
        log('release %s exists (id=%s); cleaning old assets' % (tag, rid))
        st2, ass = gh_request('api.github.com', 'GET',
                              '/repos/%s/releases/%d/assets' % (repo, rid), tok)
        if st2 == 200 and ass:
            for a in ass:
                gh_request('api.github.com', 'DELETE',
                           '/repos/%s/releases/assets/%d' % (repo, a['id']), tok)
                log('  deleted asset %s' % a['name'])
    else:
        body = json.dumps({'tag_name': tag, 'name': 'device IPA (rolling)',
                           'body': 'CI 真机构建未签名 IPA 滚动发布(gh212 起)'}).encode()
        st, js = gh_request('api.github.com', 'POST', '/repos/%s/releases' % repo, tok,
                            body=body, ctype='application/json')
        if st not in (200, 201) or not js:
            log('!! release create failed: %d %r' % (st, js))
            return False
        rid = js['id']
        log('release created (id=%s)' % rid)
    size = os.path.getsize(ipa_path)
    log('UPLOAD-RELEASE %s (%d bytes) -> uploads.github.com' % (name, size))
    t0 = time.time()
    try:
        with open(ipa_path, 'rb') as f:
            st, js = gh_request('uploads.github.com', 'POST',
                                '/repos/%s/releases/%d/assets?name=%s' % (repo, rid, name),
                                tok, body=f, ctype='application/octet-stream', timeout=2400)
        ok = st in (200, 201)
        log('  release asset resp: %d (%.0fs) %s' % (st, time.time() - t0,
                                                     'OK' if ok else repr(js)[:200]))
        return ok
    except Exception as e:
        log('  release upload failed: %r' % e)
        return False


def main():
    t0 = time.time()
    log('=== device build start; run=%s root=%s ===' % (RUN_NO, ROOT))
    rc = 0

    # 1) 真机 Python 运行时(pyhome/server 与模拟器同源,框架/扩展切 ios-arm64)
    if run(['python3', 'scripts/py_ios_bootstrap.py', '--device'], cwd=ROOT, timeout=900) != 0:
        log('!! bootstrap failed; continue anyway (build will surface missing pieces)')

    # 2) configure(独立 build 目录;deps 会在此刻下载解压到源码树 cocos2d/)
    cfg = ['cmake', '-B', BUILD, '-G', 'Ninja',
           '-DCMAKE_TOOLCHAIN_FILE=%s/ios-cmake/ios.toolchain.cmake' % ROOT,
           '-DPLATFORM=OS64',
           '-DDEPLOYMENT_TARGET=13.0',
           '-DENABLE_BITCODE=OFF',
           '-DCMAKE_BUILD_TYPE=Release',
           '-DWJJH_DEVICE=ON',
           '-DWJJH_ARM64=ON',
           '-DWJJH_LAUNCH_LOG=%s/launch_device.log' % ROOT]
    rc = run(cfg, cwd=ROOT, timeout=2400)

    # 3) build
    if rc == 0:
        rc = run(['cmake', '--build', BUILD, '--parallel', '3'], cwd=ROOT, timeout=4200)

    # 4) 打包(Payload/ → ipa;ditto 保留符号链接,zipfile 不保留)
    ipa = None
    if rc == 0:
        app = None
        for dp, dns, _fns in os.walk(BUILD):
            for d in dns:
                if d.endswith('.app'):
                    app = os.path.join(dp, d)
                    break
            if app:
                break
        if app is None:
            log('!! no .app found under ' + BUILD)
            rc = 1
        else:
            log('app bundle: %s' % app)
            # gh263: 原生 iPad 支持。此前 Info.plist 只有 iPhone 设备族 ⇒ iPad 上跑
            #   兼容模式（framesize 640x960），GPU 拉伸 2.4 倍 ⇒ 老板实测"整体界面模糊"
            #   + "开始界面的图不完整"（兼容模式非等比裁切）。打开 iPad 设备族后
            #   framesize=1536x2048，游戏 config.lua 的 SHOW_ALL(1080x1920) 原本就
            #   为 4:3 屏设计（display.lua 注释可证）⇒ 完整 UI + 原生清晰度。
            try:
                import plistlib
                _pl = os.path.join(app, 'Info.plist')
                with open(_pl, 'rb') as _f:
                    _info = plistlib.load(_f)
                _changed = []
                if list(_info.get('UIDeviceFamily', [])) != [1, 2]:
                    _info['UIDeviceFamily'] = [1, 2]
                    _changed.append('UIDeviceFamily=[1,2]')
                if not _info.get('UIRequiresFullScreen'):
                    _info['UIRequiresFullScreen'] = True
                    _changed.append('UIRequiresFullScreen')
                # iPad 也要声明方向（沿用 iPhone 的方向集合，竖屏为主）
                if 'UISupportedInterfaceOrientations' in _info \
                        and 'UISupportedInterfaceOrientations~ipad' not in _info:
                    _info['UISupportedInterfaceOrientations~ipad'] = \
                        _info['UISupportedInterfaceOrientations']
                    _changed.append('orientations~ipad')
                with open(_pl, 'wb') as _f:
                    plistlib.dump(_info, _f)
                log('gh263: Info.plist patched: %s' % ('; '.join(_changed) or 'no change'))
            except Exception as _e:
                log('!! gh263: Info.plist patch FAILED: %r (continue with original plist)' % _e)
            # gh237: iOS 16 要求任何被 dlopen 的目标（.so/.dylib）至少带 ad-hoc 签名，
            # 否则 Python 的 C 扩展加载失败（实测 binascii.cpython-314-iphoneos.so：
            # "mapped file has no cdhash, completely unsigned? Code has to be at least
            # ad-hoc signed."）→ 私服 import 链断 → 游戏卡在启动界面数十秒后崩溃。
            # Xcode 不会自动签 pyhome/ 与 server/ 下的 .so（它们是资源拷贝），
            # 爱思/Sideloadly 重签也不保证遍历到，故在此统一补 ad-hoc 签名
            # （codesign --sign - 无需证书，可在 CI 上执行）。
            _signed, _failed = 0, 0
            for _dp, _dns, _fns in os.walk(app):
                for _fn in _fns:
                    if _fn.endswith('.so') or _fn.endswith('.dylib'):
                        _p = os.path.join(_dp, _fn)
                        _r = subprocess.run(['codesign', '--force', '--sign', '-', _p],
                                            capture_output=True, text=True)
                        if _r.returncode == 0:
                            _signed += 1
                        else:
                            _failed += 1
                            log('!! adhoc sign FAILED: %s : %s' % (
                                _p, (_r.stderr or '').strip()[:200]))
            log('gh237: ad-hoc signed %d .so/.dylib files (failed=%d)' % (_signed, _failed))
            pk = os.path.join(ROOT, 'Payload')
            if os.path.exists(pk):
                shutil.rmtree(pk)
            os.makedirs(pk)
            shutil.copytree(app, os.path.join(pk, os.path.basename(app)), symlinks=True)
            ipa = os.path.join(ROOT, 'WJJH_dev_unsigned_gh%s.ipa' % RUN_NO)
            if os.path.exists(ipa):
                os.remove(ipa)
            rc2 = run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                       pk, ipa], cwd=ROOT, timeout=1200)
            if rc2 != 0 or not os.path.exists(ipa):
                log('!! ditto packaging failed')
                ipa = None
                rc = 1
            else:
                log('IPA: %s (%d bytes)' % (ipa, os.path.getsize(ipa)))

    # 5) 回传
    log('=== device build done rc=%d; total %.0fs ===' % (rc, time.time() - t0))
    if _logf:
        _logf.close()
    upload('gh%s_device_build.txt' % RUN_NO, LOG)
    if ipa and os.path.getsize(ipa) > 1000000:
        upload_release(ipa)
    sys.exit(0 if ipa else 1)


if __name__ == '__main__':
    main()
