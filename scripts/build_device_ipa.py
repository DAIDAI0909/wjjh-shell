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
        upload('WJJH_dev_unsigned_gh%s.ipa' % RUN_NO, ipa)
        upload('WJJH_device_unsigned_latest.ipa', ipa)
    sys.exit(0 if ipa else 1)


if __name__ == '__main__':
    main()
