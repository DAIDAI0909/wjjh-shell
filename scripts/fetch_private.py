#!/usr/bin/env python3
"""CI 步骤：从私有文件服务器拉取受保护构建件（token 鉴权）。

需要环境变量 WJJH_GAME_URL（仓库 Secret），形如：
    http://<host>/game.zip?t=<CI_TOKEN>
拉取结果：
    gamelua/game.zip            游戏脚本包（CMake 配置期自解压）
    res_patch.zip               增量表补丁（overlay_res_patch.py 用）
    py_ios_server/              jhserver.zip 解压（jhserver/ + stub_server.py）
    shell/WjjhJM.mm|.h          密钥相关壳源码（不入公开仓）
"""
import os
import shutil
import sys
import urllib.request
import zipfile

GAME_URL = os.environ.get('WJJH_GAME_URL', '')


def fail(msg):
    print('[fetch-private] FATAL:', msg, flush=True)
    sys.exit(3)


def fetch(name, dest):
    token = GAME_URL.split('t=')[-1]
    base = GAME_URL.split('/game.zip')[0]
    u = '%s/fz/%s?t=%s' % (base, name, token)
    print('[fetch-private] %s -> %s' % (u, dest), flush=True)
    urllib.request.urlretrieve(u, dest)
    print('[fetch-private] %d bytes' % os.path.getsize(dest), flush=True)


def diag(tag):
    """脱敏诊断：只透出长度/关键片段是否存在/前缀，绝不打印完整 token"""
    print('[fetch-private][DIAG] %s: len=%d has_game_zip=%s has_t=%s head=%r' % (
        tag, len(GAME_URL), '/game.zip' in GAME_URL, 't=' in GAME_URL,
        GAME_URL[:28]), flush=True)


def main():
    diag('start')
    if '/game.zip?t=' not in GAME_URL:
        fail('WJJH_GAME_URL secret missing or malformed')

    os.makedirs('gamelua', exist_ok=True)
    fetch('game.zip', 'gamelua/game.zip')
    fetch('res_patch.zip', 'res_patch.zip')
    fetch('jhserver.zip', 'jhserver_private.zip')
    fetch('shell_secret.zip', 'shell_secret.zip')

    if os.path.exists('py_ios_server'):
        shutil.rmtree('py_ios_server')
    with zipfile.ZipFile('jhserver_private.zip') as z:
        z.extractall('py_ios_server')       # jhserver/ + stub_server.py
    print('[fetch-private] py_ios_server:', sorted(os.listdir('py_ios_server')), flush=True)

    with zipfile.ZipFile('shell_secret.zip') as z:
        for n in z.namelist():
            if n.endswith('/'):
                continue
            dest = n.split('shell_secret/')[-1]      # shell_secret/WjjhJM.mm -> WjjhJM.mm
            dest = os.path.join('shell', dest)
            with z.open(n) as fsrc, open(dest, 'wb') as fdst:
                fdst.write(fsrc.read())
            print('[fetch-private] ->', dest, flush=True)
    os.remove('shell_secret.zip')


if __name__ == '__main__':
    main()
