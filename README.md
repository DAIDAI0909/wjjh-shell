# wjjh-shell

iOS 壳工程构建骨架（通用 cocos2d-x 3.15.1 + 内嵌 Python 私服运行时）。

本仓库只包含我们自己编写的构建骨架与开源第三方件（python-apple-support、
cocos2d-x 头文件）。游戏资源、服务端代码与密钥相关源码**不在本仓库**，
构建时经 token 鉴权从配置的私有文件服务器拉取（见 `.github/workflows/ios-build.yml`
与 `scripts/fetch_private.py`，需配置仓库 Secret `WJJH_GAME_URL`）。
