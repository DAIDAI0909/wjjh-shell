#!/usr/bin/env python3
"""CI 步骤：把仓库内置的 res_patch.zip 覆盖进 gamelua/（fb03 科举表等增量）。

game.zip（Release 资产）落后于 fangzhi_lua_dec 权威源码时，增量走本补丁包，
避免为 3 个文件整包重传 115MB。必须跑在 CMake configure 之前（CMake 若发现
gamelua/src 已存在会跳过自解压）。
"""
import os
import sys
import zipfile

BUILD_DIR = os.environ.get('CM_BUILD_DIR', os.getcwd())
PATCH = os.path.join(BUILD_DIR, 'res_patch.zip')
GAMEZIP = os.path.join(BUILD_DIR, 'gamelua', 'game.zip')
DEST = os.path.join(BUILD_DIR, 'gamelua')


def patch_animres_metadata_only():
    """gh242: 让 AnimResManager 的 initAnimResInfo 用「元数据模式」创建骨架。

    背景：AnimResManager.lua 在**模块加载时**就调用 initAnimResInfo()，遍历
    animRes 表（2138 条 / 4 个图集：gongfu1 / enterVictory / specialEffect /
    fanghu）为每项创建 SkeletonAnimation，只为读取动画时长、事件、攻击距离。
    这些只需要 SkeletonData，但 createWithBinaryFile 会把图集**全部页**载入并经
    TextureCache 常驻。gongfu1 单图集 1145 页 × 512² = 1145MB，四个合起来远超
    2GB 设备约 1.4GB 的单进程上限 ⇒ 载入到 88% 被 Jetsam 杀
    （JetsamEvent-2026-10-15-132645 / 192834，reason=per-process-limit）。

    修法：换成 createMetadataOnly（同名接口，创建期间不加载纹理）。元数据 API
    全部照常可用，画质零影响（进战斗时仍以正常模式加载）。
    只替换 `res.altlasPath` 这一处（全文件唯一），不动其它 createWithBinaryFile。
    """
    p = os.path.join(DEST, 'src', 'app', 'FightSystem', 'ResourceManager',
                     'AnimResManager.lua')
    if not os.path.exists(p):
        print('[overlay] gh242: WARN AnimResManager.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    old = b'spine38.NewSkeletonAnimation:createWithBinaryFile(res.altlasPath'
    new = b'spine38.NewSkeletonAnimation:createMetadataOnly(res.altlasPath'
    n = raw.count(old)
    if n == 1:
        open(p, 'wb').write(raw.replace(old, new))
        print('[overlay] gh242: AnimResManager -> createMetadataOnly (1 replacement) OK', flush=True)
    else:
        # 拿到 0 或 >1 都要显式暴露：0 多半说明脚本被加密/改版，>1 说明有意料之外的用法。
        print('[overlay] gh242: WARN expected exactly 1 match for createWithBinaryFile(res.altlasPath),'
              ' got %d -> file left unchanged' % n, flush=True)
        head = raw[:24]
        print('[overlay] gh242: file head hex=%s printable=%r'
              % (head.hex(), ''.join(chr(b) if 32 <= b < 127 else '.' for b in head)), flush=True)
        print('[overlay] gh242: file size=%d' % len(raw), flush=True)


def patch_skip_title_page():
    """gh249: 单机版**保留标题页**，不再自动进入游戏。

    背景（老板实测）：进游戏后停在"正在开始游戏"界面，**还没点「开始游戏」就自动
    进了主界面**。代码里写得很清楚 —— `app/models/game/Game.lua`：
        -- ★ 三世界"跳过标题页"（开发版 v57 恢复）。
        --   单机版出包前注释掉下一行（单机版保留标题页，2026-10-04 约定）。
        menuLayer:StartGame()
    这是**开发版遗留**：建好 MenuLayer 后立刻调 StartGame()，等价于替玩家点了按钮。
    按 2026-10-04 的约定，单机版必须注释掉它。

    注释后的流程（这才是"重新开始建角色"该走的路）：
        标题页等玩家点「开始游戏」
          → GameStart:start_game() → Account:createAccount() → errcode=0
          → __normal_start_game() → 本地无存档
          → __download_and_start_game() → 私服也无存档
          → __show_create_role()   ← 创建角色界面
    """
    p = os.path.join(DEST, 'src', 'app', 'models', 'game', 'Game.lua')
    if not os.path.exists(p):
        print('[overlay] gh249: WARN Game.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    old = b'menuLayer:StartGame()'
    # 注意：文件按 bytes 读写，注释必须是 ASCII（bytes 字面量不能含中文）。
    new = (b'-- gh249: offline build keeps the title page (2026-10-04 rule);\n'
           b'-- do not auto-start, wait for the player to tap "start game".\n'
           b'                                -- menuLayer:StartGame()')
    n = raw.count(old)
    if n == 1:
        open(p, 'wb').write(raw.replace(old, new))
        print('[overlay] gh249: Game.lua -> menuLayer:StartGame() 已注释（保留标题页）OK', flush=True)
    else:
        print('[overlay] gh249: WARN expected exactly 1 match for menuLayer:StartGame(),'
              ' got %d -> file left unchanged' % n, flush=True)
        head = raw[:24]
        print('[overlay] gh249: file head hex=%s printable=%r'
              % (head.hex(), ''.join(chr(b) if 32 <= b < 127 else '.' for b in head)), flush=True)
        print('[overlay] gh249: file size=%d' % len(raw), flush=True)


def patch_offline_button_reverts():
    """gh250: 还原 MainLayer 里 4 处「开发版 v57/v61」改动（单机版出包前必做）。

    全库搜「单机版出包前 / 出单机包前」共 **5 处 TODO**，gh249 只做了 `Game.lua` 那一处，
    剩下 4 处都在 `app/views/layer/MainLayer.lua`：

      ① 第 250-255 行  任务按钮被改名成「经营」并隐藏原图 ← 注释掉
            self._UI.Image_task:setVisible(false)
            local Text_jingying = ccui.Text:create("经营", ...)
            ... self._UI.Button_task:addChild(Text_jingying)
      ② 第 273-292 行  江湖按钮改成"弹窗选世界"           ← 换回原版直进副本
            弹窗第一项「武动仙途」走 `luaj.callStaticMethod("com/zwyouto/main/Util","openMud")`
            —— 那是**安卓 Java 类**，iOS 上必然失败 ⇒ 玩家看到"进入武动仙途失败"
      ③ 第 299-304 行  师门按钮被改名成「科举」           ← 注释掉
      ④ 第 310-334 行  师门按钮改成"弹窗二选一"            ← 换回原师门直进行为

    ★ 这是**老板"找不到任务按钮"的直接原因**：任务按钮上叠了一个绿色「经营」字，
      原「任务」图被 setVisible(false) 隐藏 ⇒ 玩家找不到任务入口。

    实现用**锚点行手术**（不依赖多行长字符串匹配）：先按 strip() 内容定位唯一锚点行，
    再按行号范围改写；找不到或锚点不唯一就**大声告警并整文件不动**。
    """
    p = os.path.join(DEST, 'src', 'app', 'views', 'layer', 'MainLayer.lua')
    if not os.path.exists(p):
        print('[overlay] gh250: WARN MainLayer.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    text = raw.decode('utf-8', 'replace')
    if b'\r\n' in raw:
        print('[overlay] gh250: WARN file uses CRLF -> left unchanged', flush=True)
        return
    lines = text.split('\n')

    def find_unique(pred, desc):
        hits = [i for i, l in enumerate(lines) if pred(l)]
        if len(hits) != 1:
            print('[overlay] gh250: WARN anchor %s matched %d times -> abort'
                  % (desc, len(hits)), flush=True)
            return None
        return hits[0]

    def strip_is(s):
        return lambda l: l.strip() == s

    # ---- 锚点定位 ----
    a_task = find_unique(strip_is('self._UI.Image_task:setVisible(false)'), 'task-rename')
    a_task_end = find_unique(strip_is('self._UI.Button_task:addChild(Text_jingying)'), 'task-rename-end')
    a_shimen = find_unique(strip_is('self._UI.Image_shimen:setVisible(false)'), 'shimen-rename')
    a_shimen_end = find_unique(strip_is('self._UI.Button_shimen:addChild(Text_keju)'), 'shimen-rename-end')
    # 两个弹窗都靠「锚点行 + 紧随的标题行」共同确认
    jh = [i for i, l in enumerate(lines)
          if l.strip() == 'ButtonPopLayer:createCustomInRunningScene('
          and i + 1 < len(lines) and '要前往哪个世界' in lines[i + 1]]
    sm = [i for i, l in enumerate(lines)
          if l.strip() == 'ButtonPopLayer:createCustomInRunningScene('
          and i + 1 < len(lines) and '要做什么' in lines[i + 1]]
    if any(x is None for x in (a_task, a_task_end, a_shimen, a_shimen_end)) \
            or len(jh) != 1 or len(sm) != 1:
        print('[overlay] gh250: WARN anchors not all unique -> file left unchanged', flush=True)
        return
    a_jh, a_sm = jh[0], sm[0]

    def bare_paren_after(start):
        """锚点之后第一个「整行就是一个右括号」的行 = createCustomInRunningScene 的收尾。"""
        for i in range(start + 1, len(lines)):
            if lines[i].strip() == ')':
                return i
        return None

    e_jh = bare_paren_after(a_jh)
    e_sm = bare_paren_after(a_sm)
    if e_jh is None or e_sm is None:
        print('[overlay] gh250: WARN popup closing paren not found -> file left unchanged', flush=True)
        return

    # ---- 取师门事务原文（④ 要保留它的行为）----
    try:
        f_start = next(i for i in range(a_sm, e_sm) if lines[i].strip() == '"师门事务",')
        body_start = f_start + 2                       # 跳过 function()
        sect_body = lines[body_start:e_sm - 1]         # 去掉内层 function 的 end
        if 'hasFamily' not in '\n'.join(sect_body):
            raise ValueError('sect body not recognised')
    except Exception as e:                              # noqa: BLE001
        print('[overlay] gh250: WARN cannot extract sect body (%r) -> file left unchanged' % e,
              flush=True)
        return

    def comment_out(i_from, i_to, marker):
        indent = lines[i_from][:len(lines[i_from]) - len(lines[i_from].lstrip())]
        out = ['%s-- %s' % (indent, marker)]
        for i in range(i_from, i_to + 1):
            ln = lines[i]
            if not ln.strip():
                out.append(ln)
                continue
            ind = ln[:len(ln) - len(ln.lstrip())]
            out.append('%s-- %s' % (ind, ln.lstrip()))       # 保留缩进在注释符之前
        return out

    DIRECT_JH = [
        '\t\t\t-- gh250: offline build enters the dungeon directly'
        ' (dev v57 world picker removed)',
        '\t\t\tlocal role = User:getRole()',
        '\t\t\tlocal RoleTaskControllor ='
        ' require("app.views.layer.RoleLayer.RoleTaskControllor")',
        '\t\t\tif RoleTaskControllor:clickMapLayer(role, EMPTY_FUNC) == false then',
        '\t\t\t\treturn',
        '\t\t\tend',
        '\t\t\tMainControllLayer:pushLayer("SelectMapMenuPresenter")',
    ]
    DIRECT_SM = ['\t\t\t-- gh250: offline build goes straight to the sect'
                 ' (dev v61 exam picker removed)'] + sect_body

    # ---- 从后往前改，避免行号漂移 ----
    ops = sorted([(a_jh, e_jh, DIRECT_JH), (a_sm, e_sm, DIRECT_SM)], key=lambda t: -t[0])
    for s, e, repl in ops:
        lines[s:e + 1] = repl
    # ①③ 的注释化（此时行号可能已变，重新按锚点定位更稳）
    for anchor, end_anchor, marker in (
            ('self._UI.Image_task:setVisible(false)',
             'self._UI.Button_task:addChild(Text_jingying)',
             'gh250: offline build keeps the original task button (dev v57 rename removed)'),
            ('self._UI.Image_shimen:setVisible(false)',
             'self._UI.Button_shimen:addChild(Text_keju)',
             'gh250: offline build keeps the original sect button (dev v57 rename removed)')):
        i0 = next((i for i, l in enumerate(lines) if l.strip() == anchor), None)
        i1 = next((i for i, l in enumerate(lines) if l.strip() == end_anchor), None)
        if i0 is None or i1 is None or i1 < i0:
            print('[overlay] gh250: WARN re-anchor failed for %s -> skipped' % marker, flush=True)
            continue
        lines[i0:i1 + 1] = comment_out(i0, i1, marker)

    out = '\n'.join(lines).encode('utf-8')
    # 安全检查：不许出现语法上断掉的引用
    problems = []
    if out.count(b'createCustomInRunningScene') >= text.count('createCustomInRunningScene'):
        # 两个弹窗应该各少一次调用
        if text.count('createCustomInRunningScene') - out.decode('utf-8', 'replace').count(
                'createCustomInRunningScene') != 2:
            problems.append('popup call count did not drop by exactly 2')
    for name in ('Text_jingying', 'Text_keju'):
        body_ = out.decode('utf-8', 'replace')
        live = [l for l in body_.split('\n')
                if name in l and not l.lstrip().startswith('--')]
        if live:
            problems.append('%s still has a live reference' % name)
    if problems:
        print('[overlay] gh250: WARN sanity check failed (%s) -> file left unchanged'
              % '; '.join(problems), flush=True)
        return
    open(p, 'wb').write(out)
    print('[overlay] gh250: MainLayer.lua -> 4 dev-v57/v61 reverts applied'
          ' (task/sect renamed back, jianghu/sect popups -> direct) OK', flush=True)


def patch_helper_nil_name():
    """gh253: 修掉「任务按钮点了没反应」的**根因** —— Helper:convertUIByParent 崩溃。

    老板实测症状：进主界面后点「任务」毫无反应（连点 5 次都一样）。launch.log 里
    每次点击都留下一条 Lua 错误，被 WidgetEx 的 xpcall 捕获后只打印、不弹窗，
    所以界面上"看起来什么都没发生"：

        table index is nil
        stack traceback:
            [C]: in function '__newindex'
            [string "app/Helper.lua"]:227: in function 'callback'
            [string "app/Helper.lua"]:217: in function '_callChildren'
            [string "app/Helper.lua"]:218: in function 'callChildrenByParent'
            [string "app/Helper.lua"]:225: in function 'convertUIByParent'
            [string "app/views/ui/TaskUI/MainTasksUI.lua"]:16: in function 'init'
            [string "app/views/ui/TaskUI/MainTasksUI.lua"]:7:  in function 'create'
            [string "app/presenters/Tasks/MainTaskPresenter.lua"]:23: in function '__init'
            [string "app/presenters/Tasks/MainTaskPresenter.lua"]:17: in function 'create'
            [string "app/views/base/ControllLayer.lua"]:187: in function 'getLayer'
            [string "app/views/layer/ControllLayer.lua"]:2354: in function 'MoveAndFadeAnim'
            [string "app/views/layer/MainLayer.lua"]:240: in function 'func'

    整条链与 MainLayer.lua:235-241 的「任务系统」按钮**完全对上**：

        self._UI.Button_task:releaseFunc(function()
            Audio:playEffect("daAnNiu")
            MainControllLayer:pushLayer("MainTaskPresenter")   -- 行 240，崩溃点
        end)

    ⇒ 点任务按钮 → pushLayer("MainTaskPresenter") → MainTaskPresenter:create()
      → MainTasksUI:init() → Helper:convertUIByParent(self) 抛错
      ⇒ 任务层永远建不出来 = 按钮"没反应"。

    崩点就是 Helper.lua:227 的 `parent[child:getName()] = child`：赋值经 tolua 的
    C __newindex 落库时报 "table index is nil"。**同一份代码在 Android 官方包里
    一字不差**（逐字节比对过），res 布局 41 个节点也全部 setName，所以这不是
    "我们改坏了某个资源"，而是这条递归遍历对某个节点的假设不成立。

    修法（不改动正常路径的语义，只在**出错时**降级）：
      · **失效子节点直接跳过**：`child == nil` 或 `tolua.isnull(child)` 为真时记
        `[WJJH_CONVUI] DEAD-CHILD` 并 continue。依据：本机日志里有 14 条
        `WJJH_TAGGUARD: child invalid`，说明 children 列表里确实会混进**失效 userdata**
        （shell 注释里 gh152 崩溃也证实过：Release 下 `tolua_tousertype` 不做有效性检查，
        对失效对象调 native 方法就是 UB）。往存活对象里塞一个失效子节点也是有病的，必须跳过。
      · 取名字与赋值各包一层 pcall；任何一步失败都**跳过该节点继续遍历**，
        其余具名控件照常挂到 self 上 ⇒ 任务界面能正常建出来、能打开；
      · 出错时打一行 [WJJH_CONVUI] 日志（DEAD-CHILD / GETNAME-ERR / NILNAME / ASSIGNFAIL），
        把 child/parent 的 tostring、type、name 与 err 写进 launch.log
        ⇒ 若仍异常，一轮 CI 就能定位到具体节点。

    `convertUI` / `convertUIParent` 里有**一模一样**的语句（同源缺陷），一并替换，
    这三个 helper 的所有调用方都受益。

    注意：替换文本刻意写成**单行**，不引入换行 ⇒ Helper.lua 总行数不变，
    将来 traceback 里的行号依然对得上。
    """
    p = os.path.join(DEST, 'src', 'app', 'Helper.lua')
    if not os.path.exists(p):
        print('[overlay] gh253: WARN Helper.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    old = b'parent[child:getName()] = child'
    new = (b'do local __dead = (child == nil) '
           b'if not __dead and type(tolua) == "table" and type(tolua.isnull) == "function" then '
           b'local __oki, __r = pcall(tolua.isnull, child) if __oki and __r then __dead = true end end '
           b'if __dead then print("[WJJH_CONVUI] DEAD-CHILD skipped child=" .. tostring(child) '
           b'.. " type=" .. type(child) .. " parent=" .. tostring(parent)) else '
           b'local __okn, __n = pcall(function() return child:getName() end) '
           b'if not __okn then print("[WJJH_CONVUI] GETNAME-ERR child=" .. tostring(child) '
           b'.. " type=" .. type(child) .. " err=" .. tostring(__n)) '
           b'elseif __n == nil then print("[WJJH_CONVUI] NILNAME child=" .. tostring(child) '
           b'.. " type=" .. type(child) .. " parent=" .. tostring(parent)) '
           b'else local __oka, __e = pcall(function() parent[__n] = child end) '
           b'if not __oka then print("[WJJH_CONVUI] ASSIGNFAIL name=" .. tostring(__n) '
           b'.. " parent=" .. tostring(parent) .. " child=" .. tostring(child) '
           b'.. " err=" .. tostring(__e)) end end end end')
    n = raw.count(old)
    if n == 0:
        print('[overlay] gh253: WARN anchor not found -> Helper.lua left unchanged', flush=True)
        return
    open(p, 'wb').write(raw.replace(old, new))
    print('[overlay] gh253: Helper convertUIByParent/convertUI/convertUIParent'
          ' -> nil-name safe + diagnostic (%d sites) OK' % n, flush=True)


def main():
    # gamelua/src 不存在 = game.zip 还没解（CMake 配置期才解）→ 先替它解掉
    if not os.path.isdir(os.path.join(DEST, 'src')):
        if not os.path.exists(GAMEZIP):
            print('[overlay] gamelua/game.zip not found, skip overlay')
            return
        print('[overlay] extracting game.zip first...', flush=True)
        with zipfile.ZipFile(GAMEZIP) as z:
            z.extractall(DEST)
    if not os.path.exists(PATCH):
        print('[overlay] res_patch.zip not found, skip')
    else:
        with zipfile.ZipFile(PATCH) as z:
            names = z.namelist()
            z.extractall(DEST)
        print('[overlay] applied %d files:' % len(names), flush=True)
        for n in names:
            p = os.path.join(DEST, n)
            print('   %-60s %d bytes %s' % (n, os.path.getsize(p),
                                            'OK' if os.path.exists(p) else 'MISSING'))

    # gh242: 必须跑在 game.zip 解压之后、CMake 打包之前。
    patch_animres_metadata_only()
    # gh253: Helper 的三个 convertUI* helper 加 nil-name 保护 + [WJJH_CONVUI] 诊断。
    #   修「任务按钮点了没反应」（Helper.lua:227 抛 table index is nil，被 xpcall 吞掉）。
    patch_helper_nil_name()
    # ★ gh251 说明：原 gh249/gh250 不再调用。
    #   那两轮我按**本地开发树** ink_dec/fangzhi_lua_dec 分析，以为 game.zip 里带
    #   开发版 v57 遗留（按钮改名『经营』『科举』、跳过标题页）。实测 CI 实际用的
    #   /opt/wjjh_ci/game.zip 是**已还原的干净版**（MainLayer 1079 行、不含
    #   Text_jingying/Text_keju、Game.lua 也没有 menuLayer:StartGame()），
    #   所以那两个补丁的锚点根本不存在，CI 里只会打印 WARN 后原样保留。
    #   留着函数以便日后基线变化时复用，但不再默认执行。
    # patch_skip_title_page()
    # patch_offline_button_reverts()

    # gh243: spine 图集页 → PVRTC4（运行期纹理内存降到约 1/8）。
    #   必须在 res 已解压（fetch_private.py 已把 res_full 铺进 gamelua/）之后、
    #   CMake 打包之前执行。脚本自带校验与回退，失败等同于"没做优化"，不会破坏构建。
    try:
        import pvr_compress_atlas
        pvr_compress_atlas.main()
    except Exception as e:                                      # noqa: BLE001
        print('[overlay] gh243: pvr step skipped (%r)' % e, flush=True)


if __name__ == '__main__':
    main()
