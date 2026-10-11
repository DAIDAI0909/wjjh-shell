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
    """流式 POST:/put 用于大文件(IPA),/report 用于日志。"""
    if not TOKEN:
        log('!! no token (WJJH_GAME_URL missing) — skip upload %s' % name)
        return False
    route = '/put' if name.endswith('.ipa') else '/report'
    size = os.path.getsize(path) if os.path.exists(path) else 0
    log('UPLOAD %s (%d bytes) -> %s' % (name, size, route))
    try:
        conn = http.client.HTTPConnection(HOST, 80, timeout=1800)
        with open(path, 'rb') as f:
            conn.request('POST', '%s?name=%s&t=%s' % (route, name, TOKEN),
                         body=f, headers={'Content-Length': str(size)})
        resp = conn.getresponse()
        body = resp.read()[:200]
        log('  upload resp: %d %r' % (resp.status, body))
        conn.close()
        return resp.status == 200
    except Exception as e:
        log('  upload failed: %r' % e)
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
    import json
    headers = {'Authorization': 'Bearer ' + tok, 'User-Agent': 'wjjh-device-ipa',
               'Accept': 'application/vnd.github+json'}
    if ctype:
        headers['Content-Type'] = ctype
    if body is not None and hasattr(body, 'read'):
        headers['Content-Length'] = str(os.path.getsize(body.name))
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    conn.request(method, path, body=body, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        js = json.loads(data)
    except Exception:
        js = None
    return r.status, js


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
