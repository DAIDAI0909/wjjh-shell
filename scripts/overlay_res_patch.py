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


_NL = b'\r\n'  # game.zip 资产是 CRLF（实测），多行锚点必须带 \r\n。


def _helper_loop_guard(callback_call, recurse_call):
    """给 Helper 的 `for ... callback(...) ; _callChildren(...) end` 循环套上
    「死节点容错」外壳（缩进 12 起）。callback_call / recurse_call 为 bytes。"""
    L1 = b'            '    # 12
    L2 = b'                '  # 16
    L3 = b'                    '  # 20
    L4 = b'                        '  # 24
    return (
        L1 + b'for key, var in pairs(children) do' + _NL +
        L2 + b'local __dead = false' + _NL +
        L2 + b"if type(var) == 'userdata' and type(tolua) == 'table' "
        b"and type(tolua.isnull) == 'function' then" + _NL +
        L3 + b'local __ok, __d = pcall(tolua.isnull, var)' + _NL +
        L3 + b'if __ok then __dead = __d end' + _NL +
        L2 + b'end' + _NL +
        L2 + b'if not __dead then' + _NL +
        L3 + b'local __ok, __e = pcall(function()' + _NL +
        L4 + callback_call + _NL +
        L4 + recurse_call + _NL +
        L3 + b'end)' + _NL +
        L3 + b'if not __ok then' + _NL +
        L4 + b'print("[WJJH_UI] skip node " .. tostring(var) '
        b'.. " err=" .. tostring(__e))' + _NL +
        L3 + b'end' + _NL +
        L2 + b'end' + _NL +
        L1 + b'end'
    )


def _lifecycle_loop_guard(child_op):
    """LifeCycleSupport.resume/pause 的 children 循环容错（缩进 16 起）。"""
    L2 = b'                '  # 16
    L3 = b'                    '  # 20
    return (
        b'            if children then' + _NL +
        L2 + b'for k, child in pairs(children) do' + _NL +
        L2 + b'local __dead = false' + _NL +
        L2 + b"if type(child) == 'userdata' and type(tolua) == 'table' "
        b"and type(tolua.isnull) == 'function' then" + _NL +
        L3 + b'local __ok, __d = pcall(tolua.isnull, child)' + _NL +
        L3 + b'if __ok then __dead = __d end' + _NL +
        L2 + b'end' + _NL +
        L2 + b'if not __dead then ' + child_op + b' end' + _NL +
        L2 + b'end' + _NL +
        b'            end'
    )


def patch_dead_node_tolerant():
    """gh257: 让 UI 树遍历对「失效 userdata 节点」免疫 —— 修掉任务按钮/练功 的根因。

    ★ 真实根因（由老板 iPad 的 gh255 launch.log 定位，非猜测）：
      iOS 上 UI 树里会混入**已释放（dead）的 cocos 节点**（launch.log 里 20+ 条
      `WJJH_TAGGUARD: getTag invalid` 就是 shell 的 getTag 守卫用 tolua.isnull
      抓到它们）。这些死节点一旦出现在某个父节点的 children 里，就会毒化三条遍历：

        ① Helper:convertUIByParent / convertUI / setNodeTree / callChildrenParent
           —— 逐层 `parent[child:getName()] = child` 把命名节点挂到父表上。
           死节点的 `getName()` 返回 nil（launch.log 里报 "table index is nil"），
           或直接抛错；**而且紧跟着的递归 `_callChildren(var)` 会调
           `var:getChildren()` 再次抛错 ⇒ 整条遍历当场中断。**

        ② LifeCycleSupport.resume / pause
           —— `for k,child in pairs(self:getChildren()) do class.resume(child) end`。
           死子节点的 `getChildren()` 返回 nil ⇒ `pairs(nil)` ⇒
           `TableProxy.lua:65 rawget(nil)` 崩（launch.log 实锤）。

        ③ NodeEx:callAllChild —— 同样的 children 递归。

    ★ 为什么任务按钮「点了没反应」：
      `MainTasksUI:init` 第 16 行调 `Helper:convertUIByParent(self)`。csd 根节点名为
      "Layer"，`callChildrenByParent` 借此把**根的所有子节点**挂到 `self` 上，于是
      有了 `self.PageView_Tasks`。而 `Layer:addChild` 顺序是
      `Panel_Back`(第56行) → `PageView_Tasks`(第72行)…… **Panel_Back 是死节点**。
      基线代码遍历到 Panel_Back 时，赋值抛错 → 递归 `_callChildren(Panel_Back)`
      再抛错 → **遍历在 PageView_Tasks（下一个兄弟）之前整体中断** ⇒
      `self.PageView_Tasks` 永远没被赋值 = nil ⇒ `MainTasksUI:onEnable`(行22)
      `self.PageView_Tasks:setTouchEnabled(true)` 直接崩 ⇒ 任务层建不出来。
      练功/修炼层同样走这套公共基础设施，同病相怜。

    ★ 为什么 gh253/gh255 的「跳过」反而更糟：
      gh253 只把「赋值那一行」用 pcall 包住并 `continue`，**没包递归** ⇒
      死节点的 `getChildren()` 仍在递归里抛错，遍历照样中断（等于没修）；
      而它对 **MainLayer** 的影响更毒：`self._UI.Button_task` 若是被跳过的那一个，
      `initButtons()` 行237 直接崩 ⇒ 头像（行251）及其后所有按钮回调全绑不上。
      ⇒ **教训：改「跳过」必须同时保证 ①不中断兄弟遍历 ②不丢 `_UI` 键。**
      本版正是这样做的：**先判死（tolua.isnull）跳过；再把「回调+递归」整体 pcall
      包住，失败只丢这一个子树、继续遍历兄弟**，既不中断也不连坐。

    三文件、六处替换，全部按 CRLF 字节精确锚定；命中数不符即 WARN 且**整文件不动**。
    """
    problems = []

    # ---------- 1) Helper.lua ----------
    p = os.path.join(DEST, 'src', 'app', 'Helper.lua')
    if not os.path.exists(p):
        problems.append('Helper.lua not found at %s' % p)
    else:
        raw = open(p, 'rb').read()
        jobs = []
        # A: callChildrenByParent + callChildrenParent（各 1，共 2）
        jobs.append((
            b'        for key, var in pairs(children) do' + _NL +
            b'            callback(parent, var)' + _NL +
            b'            _callChildren(var, callback)' + _NL +
            b'        end',
            _helper_loop_guard(b'callback(parent, var)', b'_callChildren(var, callback)'),
            2))
        # B: callChildren（1）
        jobs.append((
            b'        for key, var in pairs(children) do' + _NL +
            b'            callback(var)' + _NL +
            b'            _callChildren(var, callback)' + _NL +
            b'        end',
            _helper_loop_guard(b'callback(var)', b'_callChildren(var, callback)'),
            1))
        # C: setNodeTree（1）
        jobs.append((
            b'        for k, child in pairs(children) do' + _NL +
            b'            callback(parent, child)' + _NL +
            b'            _callChildren(child, callback)' + _NL +
            b'        end',
            _helper_loop_guard(b'callback(parent, var)', b'_callChildren(var, callback)'),
            1))
        new = raw
        for idx, (old, repl, want) in enumerate(jobs):
            n = new.count(old)
            if n != want:
                problems.append('Helper.lua block%s expected %d got %d' % ('ABC'[idx], want, n))
                break
            new = new.replace(old, repl)
        else:
            open(p, 'wb').write(new)
            print('[overlay] gh257: Helper.lua -> 4 traversal loops dead-node-tolerant OK', flush=True)

    # ---------- 2) LifeCycleSupport.lua ----------
    p = os.path.join(DEST, 'src', 'app', 'extends', 'LifeCycleSupport.lua')
    if not os.path.exists(p):
        problems.append('LifeCycleSupport.lua not found at %s' % p)
    else:
        raw = open(p, 'rb').read()
        old_r = (b'            for k, child in pairs(children) do' + _NL +
                 b'                class.resume(child)' + _NL +
                 b'            end')
        old_p = (b'            for k, child in pairs(children) do' + _NL +
                 b'                class.pause(child)' + _NL +
                 b'            end')
        nr, np_ = raw.count(old_r), raw.count(old_p)
        if nr != 1 or np_ != 1:
            problems.append('LifeCycleSupport resume=%d pause=%d' % (nr, np_))
        else:
            new = raw.replace(old_r, _lifecycle_loop_guard(b'class.resume(child)'))
            new = new.replace(old_p, _lifecycle_loop_guard(b'class.pause(child)'))
            open(p, 'wb').write(new)
            print('[overlay] gh257: LifeCycleSupport.lua -> resume/pause dead-node-tolerant OK',
                  flush=True)

    # ---------- 3) NodeEx.lua ----------
    p = os.path.join(DEST, 'src', 'app', 'extends', 'NodeEx.lua')
    if not os.path.exists(p):
        problems.append('NodeEx.lua not found at %s' % p)
    else:
        raw = open(p, 'rb').read()
        old = (b'    for k, child in pairs(children) do' + _NL +
               b'        func(child)' + _NL +
               b'        child:callAllChild(func)' + _NL +
               b'    end')
        n = raw.count(old)
        if n != 1:
            problems.append('NodeEx callAllChild=%d' % n)
        else:
            L2 = b'        '
            L3 = b'            '
            new_block = (
                b'    for k, child in pairs(children) do' + _NL +
                L2 + b'local __dead = false' + _NL +
                L2 + b"if type(child) == 'userdata' and type(tolua) == 'table' "
                b"and type(tolua.isnull) == 'function' then" + _NL +
                L3 + b'local __ok, __d = pcall(tolua.isnull, child)' + _NL +
                L3 + b'if __ok then __dead = __d end' + _NL +
                L2 + b'end' + _NL +
                L2 + b'if not __dead then' + _NL +
                L3 + b'func(child)' + _NL +
                L3 + b'child:callAllChild(func)' + _NL +
                L2 + b'end' + _NL +
                b'    end')
            open(p, 'wb').write(raw.replace(old, new_block))
            print('[overlay] gh257: NodeEx.lua -> callAllChild dead-node-tolerant OK', flush=True)

    if problems:
        print('[overlay] gh257: WARN incomplete -> %s' % '; '.join(problems), flush=True)


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

    ★★ gh256 修正：**之前的"跳过"改法把问题搞得更糟，已废弃。**

    gh253/gh253b 我让失败的节点"跳过并继续遍历"。实测（老板反馈）**后果更严重**：
    任务按钮照旧无效，而且**人物头像也点不了了**。机制在 `MainLayer.lua`：

        function MainLayer:initButtons()
            do self._UI.Button_task:releaseFunc(...) end   -- 行237
            do self._UI.Button_head:releaseFunc(...) end   -- 行251
            ...                                            -- 后面还有一串按钮
        end

    `self._UI` 正是由 `Helper:convertUIByParent` 填出来的（`MainLayer.lua:31`
    `self._UI = MainUI`）。**只要 `_UI.Button_task` 为 nil，行 237 就抛错、
    `initButtons()` 当场中断 ⇒ 头像（行251）以及其后所有按钮的点击回调
    全都绑不上。** 一个控件缺失，连坐一整排按钮。

    教训：这个 helper 是 483 个界面的公共基础设施，**任何控制流改动都会放大成全局性故障**。
    在没弄清到底是哪个节点之前，**不能动行为**。

    所以本版改为**纯诊断、零行为改动**：
      · 用 pcall 执行**原本那条语句**（`parent[child:getName()] = child`）；
      · 失败时打一行 `[WJJH_CONVUI] FAIL cls=… child=… parent=… err=…`，
        把**出问题节点的类名、指针、父节点与错误**全部写进 launch.log；
      · 然后 `error(__e, 0)` **把原错误原样抛回去** ⇒ 控制流与基线**逐语句一致**。
    ⇒ 症状不会比基线更差，且日志会**直接点名是哪个节点**，下一版才做精准修复。

    注意：替换文本刻意写成**单行**，不引入换行 ⇒ Helper.lua 总行数不变，
    traceback 里的行号依然对得上。
    """
    p = os.path.join(DEST, 'src', 'app', 'Helper.lua')
    if not os.path.exists(p):
        print('[overlay] gh256: WARN Helper.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    old = b'parent[child:getName()] = child'
    # 单行；行为与基线完全一致（失败时把原错误抛回），只多打一行诊断。
    new = (b'do local __ok, __er = pcall(function() parent[child:getName()] = child end) '
           b'if not __ok then '
           b'local __tp = "?" '
           b'local __to, __tr = pcall(function() return tolua.type(child) end) '
           b'if __to then __tp = tostring(__tr) end '
           b'print("[WJJH_CONVUI] FAIL cls=" .. __tp '
           b'.. " child=" .. tostring(child) .. " ctype=" .. type(child) '
           b'.. " parent=" .. tostring(parent) .. " ptype=" .. type(parent) '
           b'.. " err=" .. tostring(__er)) '
           b'error(__er, 0) end end')
    n = raw.count(old)
    if n == 0:
        print('[overlay] gh256: WARN anchor not found -> Helper.lua left unchanged', flush=True)
        return
    open(p, 'wb').write(raw.replace(old, new))
    print('[overlay] gh256: Helper convertUIByParent/setNodeTree/convertUIParent'
          ' -> diagnostic-only, behaviour unchanged (%d sites) OK' % n, flush=True)


def patch_switch_anim_failsafe():
    """gh259: 切层动画收尾容错 —— 保证 callBack("aftAnim") 永远执行。

    老板 iPad gh258 实测链路：任务层(MainTasksUI)因 ExtPageView 桩是假 PageView，
    self.PageView_Tasks=nil → 返回时 pop 动画收尾 CallFunc 里
    `fromLayer:pauseSelfAndChildren()` → onDisable → `self.PageView_Tasks:...`
    报错 → **callBack("aftAnim") 永远不执行** →
    `playSwitchAnim.animCallBack` 的 aftAnim 分支(hideTouchSwallowLayer/
    resumeUpdate/__isSwitching=false)全部没跑 → **触摸屏蔽层永不撤销 = 全局卡死**
    （launch.log 尾部主循环仍在跑，只是输入全被吞，与实测完全吻合）。

    三处加固（全部 CRLF 字节精确锚定，命中数不符即 WARN 且整文件不动）：
      ① base/ControllLayer.lua preAnim 分支：注册 3 秒看门狗——若 3 秒后仍在
        切换态（正常 aftAnim 会把 __isSwitching 置 false），强制恢复输入。
      ② base/ControllLayer.lua aftAnim 分支：animAftFunc 与 removeLayer 循环
        pcall 包住，保证 __isSwitching=false 一定执行。
      ③ layer/ControllLayer.lua MoveAndFadeAnim 两处收尾 CallFunc：主体
        pcall 包住，aftAnim 照常回调（这正是 gh258 的实际断点）。
    """
    problems = []

    # ---------- 1)+2) base/ControllLayer.lua ----------
    p = os.path.join(DEST, 'src', 'app', 'views', 'base', 'ControllLayer.lua')
    if not os.path.exists(p):
        problems.append('base/ControllLayer.lua not found at %s' % p)
    else:
        new = open(p, 'rb').read()

        old_watch = (
            '            self:showTouchSwallowLayer() -- 显示触摸屏蔽层\r\n'
            '            self:pauseUpdate()\r\n'
            '        elseif eventType == "aftAnim" then')
        new_watch = (
            '            self:showTouchSwallowLayer() -- 显示触摸屏蔽层\r\n'
            '            self:pauseUpdate()\r\n'
            '            -- gh259: 切层看门狗——动画回调链若在 aftAnim 前被 Lua 错误打断，\r\n'
            '            -- 触摸屏蔽层永不撤销=全局卡死（gh258 实测）。3 秒后仍在切换态\r\n'
            '            -- 则强制恢复输入（正常 aftAnim 已把 __isSwitching 置 false）。\r\n'
            '            self:uniqueDelayFunc(\r\n'
            '                "wjjhSwitchWatchdog_" .. tostring(fromLayerName) .. "_" .. tostring(toLayerName),\r\n'
            '                3,\r\n'
            '                function()\r\n'
            '                    if self.__isSwitching then\r\n'
            '                        print("[WJJH_UI] switch watchdog: force aftAnim "\r\n'
            '                            .. tostring(fromLayerName) .. " -> " .. tostring(toLayerName))\r\n'
            '                        self:resumeUpdate()\r\n'
            '                        self:hideTouchSwallowLayer()\r\n'
            '                        self.__isSwitching = false\r\n'
            '                    end\r\n'
            '                end\r\n'
            '            )\r\n'
            '        elseif eventType == "aftAnim" then')

        old_aft = (
            '        elseif eventType == "aftAnim" then\r\n'
            '            self:resumeUpdate()\r\n'
            '            self:hideTouchSwallowLayer() -- 隐藏触摸屏蔽层\r\n'
            '            Helper:callFunc(switch.animAftFunc, self, fromLayerName, toLayerName, switch)')
        new_aft = (
            '        elseif eventType == "aftAnim" then\r\n'
            '            self:resumeUpdate()\r\n'
            '            self:hideTouchSwallowLayer() -- 隐藏触摸屏蔽层\r\n'
            '            -- gh259/gh260: aftFunc/层清理若报错，不能阻断 __isSwitching 复位；\r\n'
            '            -- 关键输入状态已在上两行恢复；gh260 起报错必须打日志\r\n'
            '            -- （gh259 看门狗触发过 SelectMapMenuPresenter->SelectMapLayer，当时错误被静默吞掉）。\r\n'
            '            local __wokA, __werrA = pcall(Helper.callFunc, Helper, switch.animAftFunc, self, fromLayerName, toLayerName, switch)\r\n'
            '            if not __wokA then print("[WJJH_UI] aftAnim aftFunc err=" .. tostring(__werrA)) end')

        old_rm = (
            '                    if unRemoveLayer[v] then\r\n'
            '                    else\r\n'
            '                        self:removeLayer(v)\r\n'
            '                    end')
        new_rm = (
            '                    if unRemoveLayer[v] then\r\n'
            '                    else\r\n'
            '                        local __wokR, __werrR = pcall(self.removeLayer, self, v)\r\n'
            '                        if not __wokR then print("[WJJH_UI] removeLayer err=" .. tostring(__werrR)) end\r\n'
            '                    end')

        for tag, old, want in (('watchdog', old_watch, 1), ('aftcall', old_aft, 1), ('rmlayer', old_rm, 1)):
            n = new.count(old.encode('utf-8'))
            if n != want:
                problems.append('base/ControllLayer %s expected %d got %d' % (tag, want, n))
                break
            new = new.replace(old.encode('utf-8'), new_watch.encode('utf-8') if tag == 'watchdog'
                              else (new_aft.encode('utf-8') if tag == 'aftcall' else new_rm.encode('utf-8')))
        else:
            open(p, 'wb').write(new)
            print('[overlay] gh259/gh260: base/ControllLayer.lua -> switch watchdog + aftAnim pcall(logged) OK',
                  flush=True)

    # ---------- 3) layer/ControllLayer.lua ----------
    p = os.path.join(DEST, 'src', 'app', 'views', 'layer', 'ControllLayer.lua')
    if not os.path.exists(p):
        problems.append('layer/ControllLayer.lua not found at %s' % p)
    else:
        raw = open(p, 'rb').read()
        old_call = (
            '                        function()\r\n'
            '                            fromLayer:move(cc.p(0, display.height))\r\n'
            '                            fromLayer:setVisible(false)\r\n'
            '                            fromLayer:pauseSelfAndChildren()\r\n'
            '                            callBack("aftAnim")\r\n'
            '                        end')
        new_call = (
            '                        function()\r\n'
            '                            local __wok, __werr = pcall(function()\r\n'
            '                                fromLayer:move(cc.p(0, display.height))\r\n'
            '                                fromLayer:setVisible(false)\r\n'
            '                                fromLayer:pauseSelfAndChildren()\r\n'
            '                            end)\r\n'
            '                            if not __wok then print("[WJJH_UI] switch callfunc err=" .. tostring(__werr)) end\r\n'
            '                            callBack("aftAnim")\r\n'
            '                        end')
        n = raw.count(old_call.encode('utf-8'))
        if n != 2:
            problems.append('layer/ControllLayer callfunc expected 2 got %d' % n)
        else:
            raw = raw.replace(old_call.encode('utf-8'), new_call.encode('utf-8'))
            print('[overlay] gh259/gh260: layer/ControllLayer.lua -> 2 switch CallFunc bodies pcall-wrapped(logged) OK',
                  flush=True)

        # gh260: 切层 uniqueDelayFunc 函数体整体 pcall+日志（switchTo/HouseSkin/updateSkinUI/
        # toAction 创建在其中,任一报错 ⇒ toAction 不会跑 ⇒ aftAnim 不会执行 ⇒ 触发看门狗。
        # gh259 看门狗就是在这里被触发的,但当时错误被静默吞掉）。两个方向各一处,收尾有
        # 空行差异,分别锚定。
        body_head_old = (
            '            function()\r\n'
            '                self.__backgroundLayer:switchTo(toLayerName)\r\n'
            '                local HouseSkin = require("app.models.ChangeHouseSkin.HouseSkin")\r\n'
            '                if HouseSkin:hasPrintLayerCurrentConfig(toLayerName) then\r\n'
            '                    self:getLayer("PrintLayer"):updateSkinUI(toLayerName)\r\n'
            '                end')
        body_head_new = (
            '            function()\r\n'
            '                local __wokB, __werrB = pcall(function()\r\n'
            '                self.__backgroundLayer:switchTo(toLayerName)\r\n'
            '                local HouseSkin = require("app.models.ChangeHouseSkin.HouseSkin")\r\n'
            '                if HouseSkin:hasPrintLayerCurrentConfig(toLayerName) then\r\n'
            '                    self:getLayer("PrintLayer"):updateSkinUI(toLayerName)\r\n'
            '                end')
        body_tail_a_old = (
            '                toAction:setTag(toTag)\r\n'
            '\r\n'
            '                toLayer:runAction(toAction)\r\n'
            '            end')
        body_tail_a_new = (
            '                toAction:setTag(toTag)\r\n'
            '\r\n'
            '                toLayer:runAction(toAction)\r\n'
            '                end)\r\n'
            '                if not __wokB then print("[WJJH_UI] switch body err=" .. tostring(__werrB)) end\r\n'
            '            end')
        body_tail_b_old = (
            '                toAction:setTag(toTag)\r\n'
            '                toLayer:runAction(toAction)\r\n'
            '            end')
        body_tail_b_new = (
            '                toAction:setTag(toTag)\r\n'
            '                toLayer:runAction(toAction)\r\n'
            '                end)\r\n'
            '                if not __wokB then print("[WJJH_UI] switch body err=" .. tostring(__werrB)) end\r\n'
            '            end')
        for tag, old, want, repl in (
                ('bodyhead', body_head_old, 2, body_head_new),
                ('bodytailA', body_tail_a_old, 1, body_tail_a_new),
                ('bodytailB', body_tail_b_old, 1, body_tail_b_new)):
            n = raw.count(old.encode('utf-8'))
            if n != want:
                problems.append('layer/ControllLayer %s expected %d got %d' % (tag, want, n))
                break
            raw = raw.replace(old.encode('utf-8'), repl.encode('utf-8'))
        else:
            open(p, 'wb').write(raw)
            print('[overlay] gh260: layer/ControllLayer.lua -> 2 switch uniqueDelayFunc bodies pcall-wrapped(logged) OK',
                  flush=True)

    if problems:
        print('[overlay] gh259/gh260: WARN incomplete -> %s' % '; '.join(problems), flush=True)


def patch_fight_spine_lazy():
    """gh261: 战斗 3-5 号站位 Player 免建 spine —— 治「切磋即闪退」。

    定案证据（老板 gh260 实测 + JetsamEvent-2026-10-17-081520.ips）：
      闪退 = Jetsam `highwater` 查杀。WJJH rpages=92856×16KB=1.38GB，
      iPad6,11(2GB) 单进程上限 ~1.4GB，一场切磋就撞线。
      纹理仅 366MB（TextureCache 探针），大头是**每场战斗固定创建的 10 套完整 spine**
      （AnimFightLayer:FightStart 按 left/right 各 5 站位逐个 Player:create()，
      每套 = gongfu.json[1620 页图集/5969 regions] + fanghu + specialEffect + enterVictory
      四个骨架的 SkeletonData/Skeleton/AnimationState 全量解析——不管该站位是否有人）。
      放置江湖全部战斗均为 1v1（fightId 只有 left1/right1），第 3-5 站位纯属原版
      "通用模板"浪费。initRoleAnim 对空位只调 animRole:setVisible(false)，
      所以 Player 节点本身必须保留——只是不建 spine。

    修法（三处 CRLF 字节精确锚定，命中数不符即 WARN 且整文件不动）：
      ① Player:create 加 lazy 参数并透传 init；
      ② Player:init 开头：lazy 时用哑动画（方法全 no-op 的表）+ 保留廉价 UI
         （血条/状态文字），**直接 return 跳过全部 spine 创建**；
      ③ FightStart 两个循环改为 Player:create(i > 2)（每边前 2 个保留完整骨架，
         余 6 个免建 ⇒ 战斗内存直接砍 60%+）。
      万一真出现 >2v2 的战斗，第 3 人起角色隐身但流程照跑（哑动画 no-op 不炸）。
    """
    p = os.path.join(DEST, 'src', 'app', 'views', 'layer', 'FightLayer', 'AnimFightLayer.lua')
    if not os.path.exists(p):
        print('[overlay] gh261: WARN AnimFightLayer.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    NL = b'\r\n'

    a1_old = (b'function Player:create()' + NL +
              b'    local p = Player.new()' + NL +
              b'    p:init()' + NL +
              b'    return p' + NL +
              b'end')
    a1_new = (b'function Player:create(lazy)' + NL +
              b'    local p = Player.new()' + NL +
              b'    p:init(lazy)' + NL +
              b'    return p' + NL +
              b'end')

    # ★ gh262 修复 gh261 的失误：只改了 create 签名并传参 p:init(lazy)，
    #   但 Player:init 的签名没改——函数体内的 lazy 是未声明全局=nil，
    #   免建分支永不生效（gh261 实测每场仍 10 套骨架，40 dispatch）。
    a1b_old = (b'function Player:init()' + NL)
    a1b_new = (b'function Player:init(lazy)' + NL)

    a2_old = ('    --角色创建动画'.encode('utf-8') + NL +
              b'    self.anim = Resource:getSkAnim("gongfu")')
    lazy_body = (
        '    --角色创建动画'.encode('utf-8') + NL +
        b'    if lazy then' + NL +
        '        -- gh261: 3-5 号站位是占位（放置江湖战斗全为 1v1，只有 left1/right1 上场）。'.encode('utf-8') + NL +
        '        --   每套完整 spine（gongfu.json 1620 页图集/5969 regions 等 4 个骨架）内存巨大，'.encode('utf-8') + NL +
        '        --   10 套把 2GB iPad 推过 1.4GB 进程上限被 Jetsam 杀'.encode('utf-8') + NL +
        '        --   （JetsamEvent-2026-10-17-081520 实测 highwater 1.38GB）。占位 Player 用哑动画。'.encode('utf-8') + NL +
        b'        self.anim = setmetatable({}, {__index = function() return function() end end})' + NL +
        b'        self:resetHpBar(100, 100, 100)' + NL +
        b'        self._currActiveZhaoReadyItem = nil' + NL +
        b'        self:initStatusText()' + NL +
        b'        return' + NL +
        b'    end' + NL +
        b'    self.anim = Resource:getSkAnim("gongfu")')

    a3_old = (b'        local playerA = Player:create()' + NL)
    a3_new = (b'        local playerA = Player:create(i > 2)' + NL)

    new = raw
    for tag, old, want, repl in (('create-sign', a1_old, 1, a1_new),
                                 ('init-sign', a1b_old, 1, a1b_new),
                                 ('init-lazy', a2_old, 1, lazy_body),
                                 ('call-sites', a3_old, 2, a3_new)):
        n = new.count(old)
        if n != want:
            print('[overlay] gh261: WARN AnimFightLayer %s expected %d got %d -> file left unchanged'
                  % (tag, want, n), flush=True)
            return
        new = new.replace(old, repl)
    open(p, 'wb').write(new)
    print('[overlay] gh261/gh264: AnimFightLayer.lua -> fight slots 3-5 lazy (no spine) OK', flush=True)

def patch_bagupgrade_diag():
    """gh282: 背包/仓库"升级"点击无反应诊断。

    老板实测：背包与仓库都点不了升级。代码链路：
      BagUI.Button_add -> PopupLayerController:showLayer("BagUpgradePresenters")
      -> BagUpgradePresenters:showLayer() -> Bag:initBagLevel()/WareHouse:initWareHouseLevel()
    两处 initBagLevel 带 2026-10-04 的"容量下限 999"改动（danji 线注入），
    而升级表最高档正号也是 999 ⇒ 面板会用"已升级到最大格数"呈现。
    先加三层探针定位到底卡在哪一环（点击/弹层创建/showFunc 报错/数据）。
    """
    NL = b'\r\n'
    # ---- 1) BagUI: 点击 Button_add 留痕 ----
    p1 = os.path.join(DEST, 'src', 'app', 'views', 'ui', 'AttrUI', 'BagUI.lua')
    if os.path.exists(p1):
        raw = open(p1, 'rb').read()
        old = b'\tself.Button_add:releaseFunc(function()' + NL
        new = (b'\tself.Button_add:releaseFunc(function()' + NL +
               b'\t\tprint("[WJJH_BAGUP] Button_add clicked")' + NL)
        if raw.count(old) == 1:
            open(p1, 'wb').write(raw.replace(old, new))
            print('[overlay] gh282: BagUI Button_add click probe OK', flush=True)
        else:
            print('[overlay] gh282: WARN BagUI anchor=%d' % raw.count(old), flush=True)
        # gh283: 绑定当时检查 Button_add 是否未找到/已是死节点（iOS 死节点疾病）
        raw = open(p1, 'rb').read()
        o = b'\tself.Button_add:releaseFunc(function()' + NL
        n = (b'\tprint("[WJJH_BAGUP] Button_add=" .. tostring(self.Button_add)'
             b' .. " dead=" .. tostring(self.Button_add ~= nil and tolua ~= nil'
             b' and tolua.isnull ~= nil and tolua.isnull(self.Button_add) and true or false))' + NL +
             o)
        if raw.count(o) == 1:
            open(p1, 'wb').write(raw.replace(o, n))
            print('[overlay] gh283: BagUI Button_add liveness probe OK', flush=True)
        else:
            print('[overlay] gh283: WARN BagUI liveness anchor=%d' % raw.count(o), flush=True)
    # ---- 2) BagUpgradePresenters: 关键节点留痕 ----
    p2 = os.path.join(DEST, 'src', 'app', 'presenters', 'BagUpgrade', 'BagUpgradePresenters.lua')
    if os.path.exists(p2):
        raw = open(p2, 'rb').read()
        o1 = (b'function BagUpgradePresenters:showLayer()' + NL +
              b'    self._role = User:getRole()' + NL)
        n1 = (b'function BagUpgradePresenters:showLayer()' + NL +
              b'    self._role = User:getRole()' + NL +
              b'    print("[WJJH_BAGUP] showLayer enter weight=" .. tostring(self._role:getAttr("weight"))'
              b' .. " baseCk=" .. tostring(self._role:getAttr("baseCkLimit"))'
              b' .. " money=" .. tostring(self._role:getAttr("money")))' + NL)
        o2 = b'    self._UI:showUI()'
        n2 = (b'    print("[WJJH_BAGUP] data ready -> showUI")' + NL +
              b'    self._UI:showUI()')
        ok = raw.count(o1) == 1 and raw.count(o2) == 1
        if ok:
            raw = raw.replace(o1, n1).replace(o2, n2)
            open(p2, 'wb').write(raw)
            print('[overlay] gh282: BagUpgradePresenters probes OK', flush=True)
        else:
            print('[overlay] gh282: WARN BagUpgradePresenters anchors %d/%d'
                  % (raw.count(o1), raw.count(o2)), flush=True)
    # ---- 3) PopupLayerController: showFunc 报错不再静默 ----
    p3 = os.path.join(DEST, 'src', 'app', 'views', 'base', 'PopupLayerController.lua')
    if os.path.exists(p3):
        raw = open(p3, 'rb').read()
        old = (b'\tif showFunc then' + NL +
               b'\t\tshowFunc(layer)' + NL +
               b'\tend')
        new = (b'\tif showFunc then' + NL +
               b'\t\tlocal __ok, __err = pcall(showFunc, layer)' + NL +
               b'\t\tif not __ok then print("[WJJH_POPUP] showFunc error "'
               b' .. tostring(name) .. " : " .. tostring(__err)) end' + NL +
               b'\tend')
        if raw.count(old) == 1:
            open(p3, 'wb').write(raw.replace(old, new))
            print('[overlay] gh282: PopupLayerController showFunc pcall+log OK', flush=True)
        else:
            print('[overlay] gh282: WARN PopupLayerController anchor=%d' % raw.count(old), flush=True)
        # gh283: getLayer（require + create）若报错也不能静默——点升级无反应的嫌疑点之一
        raw = open(p3, 'rb').read()
        o = (b'\tlocal layer = self:getLayer(name)' + NL +
             b'\tprint("PopupLayerController:showLayer = ", name, layer)' + NL)
        n = (b'\tlocal layer' + NL +
             b'\tlocal __okL, __errL = pcall(function() layer = self:getLayer(name) end)' + NL +
             b'\tif not __okL then print("[WJJH_POPUP] getLayer error " .. tostring(name)'
             b' .. " : " .. tostring(__errL)) return end' + NL +
             b'\tprint("PopupLayerController:showLayer = ", name, layer)' + NL)
        if raw.count(o) == 1:
            open(p3, 'wb').write(raw.replace(o, n))
            print('[overlay] gh283: PopupLayerController getLayer pcall+log OK', flush=True)
        else:
            print('[overlay] gh283: WARN PopupLayerController getLayer anchor=%d' % raw.count(o), flush=True)

def patch_popup_hidelayer_safe():
    """gh298: `PopupLayerController:hideLayer` —— 回调报错不再拖垮缓存（治「点属性没反应」）。

    ★ 老板 2026-10-21 实测（gh297）：点「人物属性」后**没反应，然后闪退**。
      日志实锤 14 次：
        AttrControllLayer.lua:43: attempt to call method 'hideLayer' (a nil value)
        stack: WidgetEx.lua:180 hideLayer <- AttrControllLayer.lua:43 hideFunc
               <- PopupLayerController.lua:748 hideLayer <- AttrControllLayer.lua:36
               <- ControllLayer.lua:342 callBack <- baseSwitchAnim <- playSwitchAnim
               <- switchLayer <- pushLayer <- AttrControllLayer.lua:96
      即：切页动画收尾时 hideLayer 回调里 layer:hideLayer() 炸了。

    ★★ 关键补充（我 gh295/296 的防御为何没拦住）：
      `[WJJH_BAG]` / `WJJH_BAGDESC_NIL` 计数都是 **0** ⇒ 我加的 `if layer == nil` 从没命中。
      因为 Lua 里 `layer:hideLayer()` 的两种报错含义不同：
        · layer 是 nil          -> "attempt to index a nil value"
        · layer 非 nil 但没该方法 -> "attempt to call method 'hideLayer' (a nil value)"  ← 实际是这种
      `getLayer` 返回 `LayerClass:getInstance()` 单例；实例失效后仍是**非 nil 的坏对象**，
      `hideLayer` 字段为 nil。所以只判 nil 根本不够。

    ★★★ 为什么这一处必须修（比 69 个调用点逐个加防御更根本）：
      原实现：
          if hideFunc then hideFunc(self:getLayer(name)) end
          self._layerCache[name] = { isShow = false, time = GetTime() }
      回调一报错，**下一行 `_layerCache[name] = {isShow=false}` 永远不执行**
      ⇒ 缓存一直认为该页"正在显示" ⇒ 后续 show/hide/switch 判断全被挡住
      ⇒ 表现就是「点了没反应」。
      修法：把回调包进 pcall（异常不再中断），并在回调失败且 layer 无 hideLayer 时
      **把 `self[name] = nil` 自愈**（下次 getLayer 会重建），缓存照常更新。
      ⇒ 一处补丁覆盖 baseline 里全部 **69 处** `PopupLayerController:hideLayer(...)` 调用。
    """
    p = os.path.join(DEST, 'src', 'app', 'views', 'base', 'PopupLayerController.lua')
    if not os.path.exists(p):
        print('[overlay] gh298: WARN PopupLayerController.lua not found', flush=True)
        return
    raw = open(p, 'rb').read()
    if b'WJJH_HIDEGUARD' in raw:
        print('[overlay] gh298: PopupLayerController already patched, skip', flush=True)
        return
    NL = b'\r\n'
    # 注意：gh282 已把 showLayer 里的 showFunc 包了 pcall；hideLayer 这段仍是原样
    old = (b'\tif hideFunc then' + NL +
           b'\t\thideFunc(self:getLayer(name))' + NL +
           b'\tend' + NL +
           b'\tself._layerCache[name] = ')
    new = (b'\t-- WJJH_HIDEGUARD (gh298): ' +
           '回调异常不得中断缓存更新；坏单例自愈'.encode('utf-8') + NL +
           b'\tif hideFunc then' + NL +
           b'\t\tlocal __hl' + NL +
           b'\t\tlocal __ok, __err = pcall(function()' + NL +
           b'\t\t\t__hl = self:getLayer(name)' + NL +
           b'\t\t\thideFunc(__hl)' + NL +
           b'\t\tend)' + NL +
           b'\t\tif not __ok then' + NL +
           b'\t\t\tprint("[WJJH_HIDEGUARD] hideFunc error " .. tostring(name)'
           b' .. " : " .. tostring(__err))' + NL +
           b'\t\t\tif __hl == nil or type(__hl.hideLayer) ~= "function" then' + NL +
           b'\t\t\t\tself[name] = nil' + NL +
           b'\t\t\t\tprint("[WJJH_HIDEGUARD] reset bad singleton: " .. tostring(name))' + NL +
           b'\t\t\tend' + NL +
           b'\t\tend' + NL +
           b'\tend' + NL +
           b'\tself._layerCache[name] = ')
    n = raw.count(old)
    if n != 1:
        print('[overlay] gh298: WARN hideLayer anchor expected 1 got %d -> unchanged' % n,
              flush=True)
        return
    open(p, 'wb').write(raw.replace(old, new, 1))
    print('[overlay] gh298: PopupLayerController hideLayer pcall+self-heal OK', flush=True)


def patch_popup_remove_clear_tag():
    """gh300: `PopupLayerController:removeLayer` —— 销毁前**先清 tag**，避免"找回已销毁节点"。

    ★ 老板 2026-10-21 实测 gh299：仍然闪退，崩溃 `SIGSEGV @ lua_cocos2dx_Node_getParent`
      （`si_addr=0x0`），`Node::update` 的 Lua 回调里。日志顺序实锤：
        PopupLayerController:removeLayer(name) = BagDescLayer string
        [WJJH_HIDEGUARD] hideFunc error BagDescLayer : BagUI.lua:711 hideLayer (a nil value)
        [WJJH_HIDEGUARD] reset bad singleton: BagDescLayer
        PopupLayerController:showLayer = BagDescLayer userdata: 0x0141361870
        WJJH_SIG: signal 11 caught          ← 打印后立刻崩

    ★★ 真因（读 `Helper.lua:145-172` 才看清）：
      ```lua
      l_class.getInstance = function(self)
          local Node = runningScene:getChildByTag(NodeId)   -- 按 tag 找单例
          if Node == nil then
              Node = l_class:create(); Node:setTag(NodeId); runningScene:addChild(Node)
          end
          Node:setGlobalZOrder(1)
          Node:maxZ()        -- ← maxZ 里 self:getParent()
          return Node
      end
      ```
      `removeLayer` 只做 `layer:removeFromParent()`，**没有清 tag**。
      而这个引擎的 `getChildByTag` 是**纯 Lua polyfill**（原生不可靠，gh59 实测），
      它遍历 `scene:getChildren()` 逐个比 `getTag()`。
      一旦那个已销毁的节点仍留在 children 里（或 tag 未清），polyfill 就会把它**找回来**，
      `getInstance` 随后对它调 `maxZ()` → `getParent()` → **解引用已释放对象 ⇒ SIGSEGV**。
      （`tolua.isnull` 对这种"指针非空但已释放"的情况会漏报，所以 polyfill 的守卫拦不住。）

    ★ 修法：销毁前 `setTag(0)`，让它**不可能**再被按 tag 找回；
      下一轮 `getInstance` 必然 `getChildByTag→nil` ⇒ `create()` 全新节点 ⇒ 不会碰死对象。
      同时把 `getLayer` 与 `removeFromParent` 包 pcall（单点异常不拖垮调用方）。
      内存行为不变（该释放的还是释放）。
    """
    p = os.path.join(DEST, 'src', 'app', 'views', 'base', 'PopupLayerController.lua')
    if not os.path.exists(p):
        print('[overlay] gh300: WARN PopupLayerController.lua not found', flush=True)
        return
    raw = open(p, 'rb').read()
    if b'WJJH_KEEPALIVE' in raw:
        print('[overlay] gh300: already patched, skip', flush=True)
        return
    NL = b'\r\n'
    old = (b'\tlocal layer = self:getLayer(name)' + NL +
           b'\tif layer then' + NL +
           b'\t\tlayer:removeFromParent()' + NL +
           b'\t\tself[name] = nil' + NL +
           b'\t\tself._layerCache[name] = nil' + NL +
           b'\t\tGame:freeMemorySmart()' + NL +
           b'\tend')
    new = (b'\t-- WJJH_KEEPALIVE (gh300/gh303): ' +
           '已标记为坏单例的页面只隐藏不销毁，避免反复重建+脏 tag'.encode('utf-8') + NL +
           b'\tif self.__wjjhBad ~= nil and self.__wjjhBad[name] then' + NL +
           b'\t\tlocal __keep = self[name]' + NL +
           b'\t\tif __keep ~= nil then' + NL +
           b'\t\t\tpcall(function() __keep:setVisible(false) end)' + NL +
           b'\t\tend' + NL +
           b'\t\tself._layerCache[name] = nil' + NL +
           b'\t\tprint("[WJJH_KEEPALIVE] keep bad-named layer alive: " .. tostring(name))' + NL +
           b'\t\treturn' + NL +
           b'\tend' + NL +
           b'\tlocal __rl' + NL +
           b'\tlocal __okR = pcall(function() __rl = self:getLayer(name) end)' + NL +
           b'\tif not __okR then' + NL +
           b'\t\tprint("[WJJH_KEEPALIVE] getLayer failed: " .. tostring(name))' + NL +
           b'\t\t__rl = nil' + NL +
           b'\tend' + NL +
           b'\tif __rl then' + NL +
           b'\t\tpcall(function() __rl:setTag(0) end)' + NL +
           b'\t\tlocal __okP, __errP = pcall(function() __rl:removeFromParent() end)' + NL +
           b'\t\tif not __okP then' + NL +
           b'\t\t\tprint("[WJJH_KEEPALIVE] removeFromParent error " .. tostring(name)'
           b' .. " : " .. tostring(__errP))' + NL +
           b'\t\tend' + NL +
           b'\t\tself[name] = nil' + NL +
           b'\t\tself._layerCache[name] = nil' + NL +
           b'\t\tGame:freeMemorySmart()' + NL +
           b'\tend')
    n = raw.count(old)
    if n != 1:
        print('[overlay] gh300: WARN removeLayer anchor expected 1 got %d -> unchanged' % n,
              flush=True)
        return
    open(p, 'wb').write(raw.replace(old, new, 1))
    print('[overlay] gh300: PopupLayerController removeLayer clear-tag OK', flush=True)


def patch_popup_getlayer_guard():
    """gh301: `PopupLayerController:getLayer` —— 对「坏单例」诊断 + 用 create() 兜底重建。

    老板 2026-10-21 实测 gh300：仍闪退，且日志证明 gh300 的清 tag 逻辑正常执行
    （`WJJH_KEEPALIVE` 计数 0 = 没有走报错分支），但 `getLayer("BagDescLayer")`
    仍然返回了一个「非 nil 但没有 hideLayer 方法」的对象：
      PopupLayerController:removeLayer(name) = BagDescLayer string
      [WJJH_HIDEGUARD] hideFunc error BagDescLayer : BagUI.lua:711 hideLayer (a nil value)
      [WJJH_HIDEGUARD] reset bad singleton: BagDescLayer
      PopupLayerController:showLayer = BagDescLayer userdata: 0x014d6dbb30
      WJJH_SIG: signal 11 caught        <- showLayer 里 layer:maxZ() -> getParent()
    注意 BagUI.lua:711 的 `if layer == nil then ... return end` 没触发
    （错误是 call method 'hideLayer' 而不是 index a nil value）
    => `layer` 非 nil，但**不是 BagDescLayer 实例**（缺 hideLayer）。

    真因（Helper.lua:145-172 的 getInstance）：
      local Node = runningScene:getChildByTag(NodeId)
      if Node == nil then Node = l_class:create(); Node:setTag(NodeId); addChild end
      Node:setGlobalZOrder(1); Node:maxZ(); return Node
    这个引擎的 getChildByTag 是**纯 Lua polyfill**（原生不可靠，gh59），
    它按 tag 遍历 scene children。一旦 children 里存在**同 tag 的其他对象**，
    getInstance 就会把**不是本类的对象**当单例返回，
    后续 `layer:maxZ()` -> `getParent()` -> 解引用非法对象 -> SIGSEGV。

    修法：getLayer 拿到对象后**校验它是否具备本类应有的方法**（用 hideLayer 作探针）；
    不合格就打印诊断（名字/地址/__cname，都是纯 Lua 读字段，不碰原生方法），
    并用 `LayerClass:create()` **直接造一个干净实例**挂到运行场景，替换坏单例。
    => 既止血（不把坏对象交给调用方），又留证（下轮日志直接看到坏对象是什么类）。
    """
    p = os.path.join(DEST, 'src', 'app', 'views', 'base', 'PopupLayerController.lua')
    if not os.path.exists(p):
        print('[overlay] gh301: WARN PopupLayerController.lua not found', flush=True)
        return
    raw = open(p, 'rb').read()
    if b'WJJH_LAYERGUARD' in raw:
        print('[overlay] gh301: already patched, skip', flush=True)
        return
    NL = b'\r\n'
    old = (b'\t\tlocal LayerClass = require(className)' + NL +
           b'\t\tself[name] = LayerClass:getInstance()' + NL +
           b'\t\tlayer = self[name]')
    # ★ gh302 修正探测方式：用 **`__cname` 比对**，不再用 hideLayer。
    #   实测（老板 gh301 日志）：BookCaseLayer **本来就没有 hideLayer**
    #   （方法集 create/init/showLayer/initPlayer/… 共 15 个，无 hideLayer）
    #   ⇒ 用 hideLayer 当探针会把它误判成坏对象，白重建 3 次。
    #   真正的坏对象（占着 BagDescLayer 位置那个）`__cname` 是 **nil**（根本不是类实例）。
    #   __cname 是纯 Lua 字段读取，不碰原生方法（gh296/gh298 的教训）。
    # ★★★ gh303：**绝不能碰坏对象**。老板 gh302 实测崩溃：
    #     [WJJH_LAYERGUARD] bad instance ... got=nil want=BagDescLayer
    #     WJJH_SIG: signal 11 → lua_cocos2dx_Node_setTag   ← 就是我下面那句 setTag(0)
    #   这个坏对象是**悬空指针**（指针非空但内存已释放），任何方法调用都直接段错误，
    #   `pcall` 拦不住 SIGSEGV。⇒ 只打印诊断（纯 Lua 字段），**不对它调任何原生方法**。
    #   恢复只用 `LayerClass:create()` 造新实例，并把它钉在 self[name] 上。
    #   同时在 `__wjjhBad` 里记下该名字；removeLayer 见到它就**只隐藏不销毁**，
    #   避免"销毁→重建→又拿到坏对象"的死循环（gh301/302 实测刷了 53 次）。
    new = (b'\t\tlocal LayerClass = require(className)' + NL +
           b'\t\tself[name] = LayerClass:getInstance()' + NL +
           b'\t\tlayer = self[name]' + NL +
           b'\t\t-- WJJH_LAYERGUARD (gh301/302/303): ' +
           '用 __cname 校验类型；坏对象只记录不触碰'.encode('utf-8') + NL +
           b'\t\tlocal __want = LayerClass.__cname' + NL +
           b'\t\tif layer ~= nil and __want ~= nil and layer.__cname ~= __want then' + NL +
           b'\t\t\tprint("[WJJH_LAYERGUARD] bad instance name=" .. tostring(name)'
           b' .. " self=" .. tostring(layer)'
           b' .. " got=" .. tostring(layer.__cname)'
           b' .. " want=" .. tostring(__want))' + NL +
           b'\t\t\t-- note: do NOT call any method on layer here (dangling ptr => SIGSEGV)' + NL +
           b'\t\t\tself.__wjjhBad = self.__wjjhBad or {}' + NL +
           b'\t\t\tself.__wjjhBad[name] = true' + NL +
           b'\t\t\tlocal __okC, __fresh = pcall(function() return LayerClass:create() end)' + NL +
           b'\t\t\tif __okC and __fresh ~= nil then' + NL +
           b'\t\t\t\tlocal __sc = cc.Director:getInstance():getRunningScene()' + NL +
           b'\t\t\t\tif __sc ~= nil then pcall(function() __fresh:addTo(__sc) end) end' + NL +
           b'\t\t\t\tpcall(function() __fresh:setGlobalZOrder(1) end)' + NL +
           b'\t\t\t\tlayer = __fresh' + NL +
           b'\t\t\t\tself[name] = __fresh' + NL +
           b'\t\t\t\tprint("[WJJH_LAYERGUARD] recovered via create() name="'
           b' .. tostring(name))' + NL +
           b'\t\t\tend' + NL +
           b'\t\tend')
    n = raw.count(old)
    if n != 1:
        print('[overlay] gh301: WARN getLayer anchor expected 1 got %d -> unchanged' % n,
              flush=True)
        return
    open(p, 'wb').write(raw.replace(old, new, 1))
    print('[overlay] gh301: PopupLayerController getLayer guard+recover OK', flush=True)


def patch_tujian_animleak():
    """gh305: 武学详情切换时**销毁旧的 AnimFightLayer**（内存泄漏，导致 Jetsam 查杀）。

    老板 2026-10-22 实测：玩一会儿后 App 被系统杀（`JetsamEvent`）。
    报告：`WJJH rpages=92800 footprint=1450MB reason=per-process-limit`
    （iPad6,11 单进程上限约 1.4GB）。

    日志证据：
      · 内存曲线 978MB → 1403MB 持续爬升不回落（中途场景切换能 -500MB，说明回收机制本身正常）
      · 最后阶段 `WJJH_SPINE38 CTOR updateCache-done` 执行 **736 次**，
        其中 **734 次是 cache hit** —— 即"复用骨架数据"但仍每次新建完整 SkeletonAnimation
      · 纹理只占 22MB ⇒ 泄漏在 spine 骨架对象本身，不在贴图

    真因（`TuJianSkillInFoPopLayer.lua:282` 与 `:436`，两处同款）：
      ```lua
      if self._animRoleLayer then
          self._animRoleLayer = nil        -- ← 只断引用！
      end
      self._animRoleLayer = AnimFightLayer:createInPanel(self.Panel_animFightArea)  -- 新建
      ```
      旧的 `AnimFightLayer`（内含多套 spine 骨架 + 角色节点）**从未 removeFromParent**，
      只是 Lua 侧引用没了 ⇒ 节点仍挂在 `Panel_animFightArea` 下，C++ 侧永不释放。
      每切一次武学/招式就漏一整套，几百次后撞上限被杀。

    修法：置 nil 前先 `removeFromParent()`（并 pcall 兜底）。
    """
    p = os.path.join(DEST, 'src', 'app', 'views', 'layer', 'TuJianLayer',
                     'TuJianSkillInFoPopLayer.lua')
    if not os.path.exists(p):
        print('[overlay] gh305: WARN TuJianSkillInFoPopLayer.lua not found', flush=True)
        return
    raw = open(p, 'rb').read()
    if b'WJJH_ANIMLEAK' in raw:
        print('[overlay] gh305: already patched, skip', flush=True)
        return
    NL = b'\r\n'
    # 锚点：4 空格 + "if self._animRoleLayer then" + CRLF + 8 空格 + 赋值 + CRLF + tab + end
    old = (b'    if self._animRoleLayer then' + NL +
           b'        self._animRoleLayer = nil' + NL +
           b'\tend')
    new = (b'    -- WJJH_ANIMLEAK (gh305): ' +
           '置 nil 前必须移除，否则旧 AnimFightLayer 的 spine 骨架永不释放'.encode('utf-8') + NL +
           b'    if self._animRoleLayer then' + NL +
           b'        pcall(function() self._animRoleLayer:removeFromParent() end)' + NL +
           b'        self._animRoleLayer = nil' + NL +
           b'\tend')
    n = raw.count(old)
    if n != 2:
        print('[overlay] gh305: WARN anchor expected 2 got %d -> unchanged' % n, flush=True)
        return
    open(p, 'wb').write(raw.replace(old, new))
    print('[overlay] gh305: TuJianSkillInFoPopLayer -> %d animLayer release OK' % n, flush=True)


def patch_home_always_open():
    """gh306: 家园系统**常开**（治「家园引导做完仍提示该系统未开放」）。

    老板 2026-10-22 实测：家园引导任务做完后点杨掮客购房，弹「该系统未开放」。

    机制（`fz_boot_server.py:1628-1643` + `ControllLayer.lua:2633-2648`）：
      · 登录接口 `get_game_user_info_2` 下发 `jiayuantch`（家园总开关）与
        `hometch.switchs`；客户端据此设置 `JIAYUAN_SYSTEM_IS_OPEN`，
        并在 `switchs==1` 时把 `家园引导` 标记补成 2。
      · 本私服按存档里的 `家园引导` 决定：`>=2` 才开。
      · 但 `_canonical_save_path()` = `<Documents>/jhserver/saves/<key>.json`，
        实测老板容器里 **saves / roles 目录都不存在** ⇒ 读不到标记 ⇒ 恒为 0
        ⇒ `jiayuantch=false` ⇒ 家园永远关着。
        服务端 boot_log 实锤：`★ 账号信息（…家园引导=0→家园引导中，reset=0）`

    单机版取舍：**直接常开**（`jiayuantch=True` / `switchs=1`）。
      代价：尤三的家园引导剧情不再自动出现（该剧情是新手一次性引导，老板已完成）。
      需要重玩引导时把 `_JIAYUAN_FORCE_OPEN` 改 False 即可。
      另外 `switchs=1` 会让客户端把 `家园引导` 补成 2，标记从此自洽。
    """
    p = _find_server_file(('fz_boot_server.py',))
    if not p:
        print('[overlay] gh306: WARN fz_boot_server.py not found', flush=True)
        return
    raw = open(p, 'rb').read()
    if b'WJJH_HOME_FORCE_OPEN' in raw:
        print('[overlay] gh306: already patched, skip', flush=True)
        return
    # ★★★ gh307 事故修正：**必须带 8 空格缩进**。
    #   gh306 只替换了子串 `_jy_done = _jy_flag >= 2`（不含前导空格），
    #   新加的两行顶格 ⇒ 在 `try:` 块（8 空格）里出现 0 缩进语句 ⇒ IndentationError
    #   ⇒ `fz_boot_server.py` 导入失败 ⇒ 私服起不来 ⇒ 客户端 `waitServerPort:`
    #   死等端口超时 ⇒ 被 SIGKILL（老板实测：进游戏黑屏闪退，
    #   崩溃栈 `application:didFinishLaunchingWithOptions → waitServerPort:`）。
    #   教训：**改 Python 时 old/new 都要含完整缩进，改完必须 py_compile 验证。**
    IND = b"        "          # 原行 `_jy_done = _jy_flag >= 2` 实测缩进 8 空格
    old = IND + b"_jy_done = _jy_flag >= 2"
    new = (IND + b"# WJJH_HOME_FORCE_OPEN (gh306/gh307): " +
           "单机版家园常开——saves 目录可能不存在导致标记恒 0，家园永久关闭".encode('utf-8') + b"\n"
           + IND + b"_JIAYUAN_FORCE_OPEN = True\n"
           + IND + b"_jy_done = _JIAYUAN_FORCE_OPEN or (_jy_flag >= 2)")
    n = raw.count(old)
    if n != 1:
        print('[overlay] gh306: WARN _jy_done anchor expected 1 got %d -> unchanged' % n,
              flush=True)
        return
    patched = raw.replace(old, new, 1)
    open(p, 'wb').write(patched)
    # ★ gh307: 落地后立刻做 Python 语法自检，语法错就回滚，绝不让坏文件进包
    try:
        compile(patched, p, 'exec')
        print('[overlay] gh306/gh307: fz_boot_server -> home force-open OK (syntax ok)',
              flush=True)
    except SyntaxError as e:
        open(p, 'wb').write(raw)
        print('[overlay] gh306: FAIL fz_boot_server syntax error -> rolled back: %s' % e,
              flush=True)


def patch_bagdesc_nil_guard():
    """gh285: BagDescLayer nil 防御 + 属性页文字重影排查点。

    gh284 日志实锤：从背包切页时
      AttrControllLayer:39 `layer:hideLayer()` -> attempt to call method 'hideLayer' (a nil value)
    （9 次）。AttrControllLayer 那段的写法是：
      PopupLayerController:hideLayer("BagDescLayer", function(layer) layer:hideLayer() end, 0)
    PopupLayerController:hideLayer 先 `hideFunc(self:getLayer(name))`；若该串
    getLayer 内部抛错/返回 nil（BagDescLayer 是 classDefNodeGetInstance 单例），
    layer 就是 nil ⇒ 每次离开背包页都报一次错 ⇒ 背包列表/描述面板状态半残。
    修：AttrControllLayer 该处加 nil 防御（nil 时只记录并跳过，不再中断切页链）。
    """
    p = os.path.join(DEST, 'src', 'app', 'views', 'layer', 'AttrLayer', 'AttrControllLayer.lua')
    if not os.path.exists(p):
        print('[overlay] gh285: WARN AttrControllLayer.lua not found', flush=True)
        return
    raw = open(p, 'rb').read()
    NL = b'\r\n'
    old = (b'                PopupLayerController:hideLayer(' + NL +
           b'                    "BagDescLayer",' + NL +
           b'                    function(layer)' + NL +
           b'                        layer:hideLayer()' + NL +
           b'                    end,' + NL +
           b'                    0' + NL +
           b'                )')
    new = (b'                PopupLayerController:hideLayer(' + NL +
           b'                    "BagDescLayer",' + NL +
           b'                    function(layer)' + NL +
           b'                        if layer == nil then' + NL +
           b'                            print("[WJJH_BAG] BagDescLayer nil on switch-away")' + NL +
           b'                            return' + NL +
           b'                        end' + NL +
           b'                        layer:hideLayer()' + NL +
           b'                    end,' + NL +
           b'                    0' + NL +
           b'                )')
    n = raw.count(old)
    if n != 1:
        print('[overlay] gh285: WARN AttrControllLayer anchor expected 1 got %d -> unchanged' % n, flush=True)
        return
    open(p, 'wb').write(raw.replace(old, new))
    print('[overlay] gh285: AttrControllLayer BagDescLayer nil-guard OK', flush=True)


def patch_v37_pack_sync():
    """gh286: 单机版 v37 礼包同步（基础武学1000/1500级包等 12 个）。

    老板实测：安卓单机版 v37 商城里有的礼包，iOS 没有。差异实测量化：
      v37 多 12 个礼包（baseskill1000/1500、6 个 med_* 属性包、回神丹/心神上限/
      自创神功材料包等）。这些礼包定义在 v37 的 Items.lua，奖励在 Rewards.lua，
      两个伪属性（testBaseSkillMax1000/1500）在 Role_Attr.lua。

    ★ rid 冲突：v37 用的 9647-9683 在 iOS 已被"锻造材料"占用 ⇒ 全部重映射到
      9920-9956（iOS 实测该区间空闲）。礼包定义里的 rwId1/rewardid 引用同步改写。
    ★ 奖励物品（冰糖葫芦/遁地符/灵芝草等 18 种）iOS 的 storeItem.shangchengwupin
      **本来就有**（已核验），所以开包能出真物品。
    """
    PACKS = [
        ('testpack_baseskill1000', '["testpack_baseskill1000"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开后 12 门基础武学（拳脚/剑法/刀法/内功/轻功/招架/暗器/鞭法/棍法/毒技/乐器/双持）立刻全部升到 1000 级（官方突破表的原生高度）。可重复购买。]],["id"]=[[testpack_baseskill1000]],["name"]=[[基础武学1000级包]],["priceUnit"]=[[money]],["rwId1"]=[[{9920}]],["salePrice"]=0,["type"]=[[宝箱]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了基础武学1000级包！NOR]]}'),
        ('testpack_baseskill1500', '["testpack_baseskill1500"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开后 12 门基础武学（拳脚/剑法/刀法/内功/轻功/招架/暗器/鞭法/棍法/毒技/乐器/双持）立刻全部升到 1500 级（突破阶段同时拉满；超出官方上限的高度，强度非常猛）。可重复购买。]],["id"]=[[testpack_baseskill1500]],["name"]=[[基础武学1500级包]],["priceUnit"]=[[money]],["rwId1"]=[[{9921}]],["salePrice"]=0,["type"]=[[宝箱]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了基础武学1500级包！NOR]]}'),
        ('testpack_huishendan', '["testpack_huishendan"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开获得 10 颗九转凝神露（心神恢复面板里使用，一颗直接回满全部心神）。可重复购买。]],["id"]=[[testpack_huishendan]],["name"]=[[回神丹礼包]],["priceUnit"]=[[money]],["rewardid"]=[[9922]],["salePrice"]=0,["type"]=[[特殊]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了回神丹礼包！NOR]]}'),
        ('testpack_med_exp', '["testpack_med_exp"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开获得 5 种修炼药各 5 份：潜能丹（潜能 10000+58×等级）、玄黄紫清丹（经验 20000+115×等级）、万年灵芝、冰糖葫芦、九转金丹（随机一项属性+1）。★ 潜能丹／玄黄紫清丹每天上限 20 颗，超了会提示"再吃就上瘾了"。]],["id"]=[[testpack_med_exp]],["name"]=[[经验潜能礼包]],["priceUnit"]=[[money]],["rwId1"]=[[{9923,9924,9925,9926,9927}]],["salePrice"]=0,["type"]=[[宝箱]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了经验潜能礼包！NOR]]}'),
        ('testpack_med_jingli', '["testpack_med_jingli"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开获得 5 种精力药各 5 份：流云甘露（精力+100，最猛的一种）、天香玉露（精力+50，有几率抬精力上限）×2 种、万年灵芝（经验+潜能）、冰糖葫芦（经验+潜能）。打坐修炼都吃精力，精力见底就靠它补；精力满时吃不下会提示。]],["id"]=[[testpack_med_jingli]],["name"]=[[精力恢复礼包]],["priceUnit"]=[[money]],["rwId1"]=[[{9928,9929,9930,9931,9932}]],["salePrice"]=0,["type"]=[[宝箱]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了精力恢复礼包！NOR]]}'),
        ('testpack_med_liangong', '["testpack_med_liangong"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开获得 5 份行功散（每份立刻减少 1 小时练功/修炼时间）+ 5 份福缘丹、灵智草、狗皮膏、轻身药（福缘+1／悟性+1~2／根骨+1~2／身法+1~2）。★ 行功散要在「练功／修炼」界面里点行功散按钮使用，不是背包里点使用；练功已完成时用不了。]],["id"]=[[testpack_med_liangong]],["name"]=[[练功加速礼包]],["priceUnit"]=[[money]],["rwId1"]=[[{9933,9934,9935,9936,9937}]],["salePrice"]=0,["type"]=[[宝箱]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了练功加速礼包！NOR]]}'),
        ('testpack_med_neili', '["testpack_med_neili"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开获得 5 种提升内力修为的药各 5 份：无尘果（内力上限+1000，最猛）、明境丹（+500）、菩提子（+300）×2 种、万年雪莲（+10）。]],["id"]=[[testpack_med_neili]],["name"]=[[内力礼包]],["priceUnit"]=[[money]],["rwId1"]=[[{9938,9939,9940,9941,9942}]],["salePrice"]=0,["type"]=[[宝箱]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了内力礼包！NOR]]}'),
        ('testpack_med_siwei', '["testpack_med_siwei"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开获得 5 种成长药各 5 份：神力散（臂力+1~2）、轻身药（身法+1~2）、狗皮膏（根骨+1~2）、灵智草（悟性+1~2）、洗颜水（颜值+1）。★ 这类药每日限吃 3 颗（有月卡 +1）。]],["id"]=[[testpack_med_siwei]],["name"]=[[四维成长礼包]],["priceUnit"]=[[money]],["rwId1"]=[[{9943,9944,9945,9946,9947}]],["salePrice"]=0,["type"]=[[宝箱]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了四维成长礼包！NOR]]}'),
        ('testpack_med_special', '["testpack_med_special"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开获得 5 种功能道具各 5 份：大补丸（挂机收益最大化）、静心丸（闭关必成功、不走火入魔）、三才丹（培元一次免真气）、遁地符（瞬移回家/任务点）、分身符（同时打工练功打坐）。★ 这几种都要在对应界面里用（挂机/闭关/培元/地图），背包里点"使用"没反应。]],["id"]=[[testpack_med_special]],["name"]=[[特殊功能礼包]],["priceUnit"]=[[money]],["rwId1"]=[[{9948,9949,9950,9951,9952}]],["salePrice"]=0,["type"]=[[宝箱]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了特殊功能礼包！NOR]]}'),
        ('testpack_med_yanzhi', '["testpack_med_yanzhi"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开获得 2 种颜值药各 5 份：洗颜水（颜值+1，必成）、美容丸（20% 概率颜值+1）。（颜值这一类的药原版就只有这两种）]],["id"]=[[testpack_med_yanzhi]],["name"]=[[颜值礼包]],["priceUnit"]=[[money]],["rwId1"]=[[{9953,9954}]],["salePrice"]=0,["type"]=[[宝箱]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了颜值礼包！NOR]]}'),
        ('testpack_shengongmateria', '["testpack_shengongmateria"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开获得全套自创材料各 20 份：启发/灵感/神思（创作必成）/节源卷轴，精研/超群/跃兴/复思/弃定/重思之书（提升招式品质、重置词缀）。打开后在「家园书房→神功书案→自创武学」的道具面板里使用。可重复购买。]],["id"]=[[testpack_shengongmateria]],["name"]=[[自创神功材料包]],["priceUnit"]=[[money]],["rewardid"]=[[9955]],["salePrice"]=0,["type"]=[[特殊]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了自创神功材料包！NOR]]}'),
        ('testpack_xinshencap', '["testpack_xinshencap"]={["buyPrice"]=1,["canUse"]=1,["deposit"]=0,["dsc"]=[[打开后心神上限立刻提升到 99999，并回满心神（打开练功/心神面板即可见）。可重复购买（重复使用无副作用）。]],["id"]=[[testpack_xinshencap]],["name"]=[[心神上限提升礼包]],["priceUnit"]=[[money]],["rewardid"]=[[9956]],["salePrice"]=0,["type"]=[[特殊]],["unit"]=[[个]],["useDsc"]=[[HIY你打开了心神上限提升礼包！NOR]]}'),
    ]
    REWARDS = [
        ('9920', '["9920"]={["calcType"]=[[固定类]],["id"]=9920,["rwName"]=[[testBaseSkillMax1000]],["rwNumber"]=1,["rwType"]=[[属性]]}'),
        ('9921', '["9921"]={["calcType"]=[[固定类]],["id"]=9921,["rwName"]=[[testBaseSkillMax1500]],["rwNumber"]=1,["rwType"]=[[属性]]}'),
        ('9922', '["9922"]={["calcType"]=[[固定类]],["id"]=9922,["rwName"]=[[exp]],["rwNumber"]=5000,["rwType"]=[[属性]]}'),
        ('9923', '["9923"]={["calcType"]=[[固定类]],["id"]=9923,["rwName"]=[[qiannengdan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9924', '["9924"]={["calcType"]=[[固定类]],["id"]=9924,["rwName"]=[[xuanhuangziqingdan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9925', '["9925"]={["calcType"]=[[固定类]],["id"]=9925,["rwName"]=[[wannianlingzhi]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9926', '["9926"]={["calcType"]=[[固定类]],["id"]=9926,["rwName"]=[[bingtanghulu]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9927', '["9927"]={["calcType"]=[[固定类]],["id"]=9927,["rwName"]=[[jiuzhuanjindan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9928', '["9928"]={["calcType"]=[[固定类]],["id"]=9928,["rwName"]=[[liuyunganlu]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9929', '["9929"]={["calcType"]=[[固定类]],["id"]=9929,["rwName"]=[[tianxiangyulu1]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9930', '["9930"]={["calcType"]=[[固定类]],["id"]=9930,["rwName"]=[[jinglidan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9931', '["9931"]={["calcType"]=[[固定类]],["id"]=9931,["rwName"]=[[wannianlingzhi]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9932', '["9932"]={["calcType"]=[[固定类]],["id"]=9932,["rwName"]=[[bingtanghulu]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9933', '["9933"]={["calcType"]=[[固定类]],["id"]=9933,["rwName"]=[[xinggongsan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9934', '["9934"]={["calcType"]=[[固定类]],["id"]=9934,["rwName"]=[[fuyuandan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9935', '["9935"]={["calcType"]=[[固定类]],["id"]=9935,["rwName"]=[[lingzhicao]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9936', '["9936"]={["calcType"]=[[固定类]],["id"]=9936,["rwName"]=[[goupigao]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9937', '["9937"]={["calcType"]=[[固定类]],["id"]=9937,["rwName"]=[[qingshenyao]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9938', '["9938"]={["calcType"]=[[固定类]],["id"]=9938,["rwName"]=[[wuchenguo1]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9939', '["9939"]={["calcType"]=[[固定类]],["id"]=9939,["rwName"]=[[mingjingdan1]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9940', '["9940"]={["calcType"]=[[固定类]],["id"]=9940,["rwName"]=[[putizi1]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9941', '["9941"]={["calcType"]=[[固定类]],["id"]=9941,["rwName"]=[[neilidan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9942', '["9942"]={["calcType"]=[[固定类]],["id"]=9942,["rwName"]=[[wannianxuelian]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9943', '["9943"]={["calcType"]=[[固定类]],["id"]=9943,["rwName"]=[[shenlishan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9944', '["9944"]={["calcType"]=[[固定类]],["id"]=9944,["rwName"]=[[qingshenyao]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9945', '["9945"]={["calcType"]=[[固定类]],["id"]=9945,["rwName"]=[[goupigao]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9946', '["9946"]={["calcType"]=[[固定类]],["id"]=9946,["rwName"]=[[lingzhicao]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9947', '["9947"]={["calcType"]=[[固定类]],["id"]=9947,["rwName"]=[[xiyanshui]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9948', '["9948"]={["calcType"]=[[固定类]],["id"]=9948,["rwName"]=[[dabuwan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9949', '["9949"]={["calcType"]=[[固定类]],["id"]=9949,["rwName"]=[[jingxinwan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9950', '["9950"]={["calcType"]=[[固定类]],["id"]=9950,["rwName"]=[[sancaidan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9951', '["9951"]={["calcType"]=[[固定类]],["id"]=9951,["rwName"]=[[dundifu]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9952', '["9952"]={["calcType"]=[[固定类]],["id"]=9952,["rwName"]=[[fenshenfu]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9953', '["9953"]={["calcType"]=[[固定类]],["id"]=9953,["rwName"]=[[xiyanshui]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9954', '["9954"]={["calcType"]=[[固定类]],["id"]=9954,["rwName"]=[[meirongwan]],["rwNumber"]=5,["rwType"]=[[物品]]}'),
        ('9955', '["9955"]={["calcType"]=[[固定类]],["id"]=9955,["rwName"]=[[exp]],["rwNumber"]=5000,["rwType"]=[[属性]]}'),
        ('9956', '["9956"]={["calcType"]=[[固定类]],["id"]=9956,["rwName"]=[[exp]],["rwNumber"]=5000,["rwType"]=[[属性]]}'),
    ]
    ATTRS = [
        ('testBaseSkillMax1000', '\tif name == "testBaseSkillMax1000" then\r\n\t\t-- ★ 2026-10-17 v35（客户要求）：基础武学一键 1000 级。\r\n\t\t--   默认突破档 100001 的 Blevel 就是 1000（官方原生），setSkillLv 的 clamp 正好。\r\n\t\tlocal SkillHelper = require("app.models.skill.SkillHelper")\r\n\t\tlocal ids = { "jibenquanjiao", "jibenjianfa", "jibendaofa", "jibenneigong",\r\n\t\t              "jibenqinggong", "jibenzhaojia", "jibenanqi", "jibenbianfa",\r\n\t\t              "jibengunfa", "jibenduji", "jibenqinfa", "jibenshuangchi" }\r\n\t\tlocal n = 0\r\n\t\tfor _, sid in ipairs(ids) do\r\n\t\t\tSkillHelper:setSkillLv(self, sid, 1000)\r\n\t\t\tn = n + 1\r\n\t\tend\r\n\t\tPopText("基础武学已设为 1000 级（" .. n .. " 门）")\r\n\t\treturn\r\n\tend\r\n'),
        ('testBaseSkillMax1500', '\tif name == "testBaseSkillMax1500" then\r\n\t\t-- ★ 2026-10-17 v35（客户要求）：基础武学一键 1500 级。\r\n\t\t--   官方突破表最高 100011（class=11，Blevel=1100）⇒ 先把突破阶段拉满（面板"已满"），\r\n\t\t--   再按公式直写 1500 级经验（等级→经验是公式 0.015×lv³+1，写入底层不 clamp）。\r\n\t\tlocal SkillHelper = require("app.models.skill.SkillHelper")\r\n\t\tlocal BreakRes = require("app.models.skill.skillBreakThrough.SkillBreakThroughResManager")\r\n\t\tlocal ids = { "jibenquanjiao", "jibenjianfa", "jibendaofa", "jibenneigong",\r\n\t\t              "jibenqinggong", "jibenzhaojia", "jibenanqi", "jibenbianfa",\r\n\t\t              "jibengunfa", "jibenduji", "jibenqinfa", "jibenshuangchi" }\r\n\t\tlocal maxBreId = BreakRes:getSkillBreakThroughMapByLevel(11).id\r\n\t\tlocal n = 0\r\n\t\tfor _, sid in ipairs(ids) do\r\n\t\t\tself:setSkillBreId(sid, maxBreId)\r\n\t\t\tSkillHelper:setRoleSkill(self, sid, { id = sid, exp = SkillHelper:getExp(sid, 1500) })\r\n\t\t\tn = n + 1\r\n\t\tend\r\n\t\tPopText("基础武学已设为 1500 级（" .. n .. " 门）")\r\n\t\treturn\r\n\tend\r\n'),
    ]

    # ---- 1) Items.lua：末尾 }} 前插入礼包定义 ----
    p = os.path.join(DEST, 'res', 'script', 'map', 'mapItemAttr', 'Items.lua')
    if os.path.exists(p):
        raw = open(p, 'rb').read()
        if b'testpack_baseskill1000' in raw:
            print('[overlay] gh286: Items.lua already synced, skip', flush=True)
        else:
            text = raw.decode('utf-8', 'replace')
            if text.rstrip().endswith('}}'):
                # ★★★ gh306 关键修正：插入点必须 +1。
                #   文件尾部形如 `...["type"]=[[乐器]]}}`：
                #     倒数第 2 个 `}` = 最后一个物品的闭合；最后 1 个 `}` = 外层表闭合。
                #   旧代码在 `rfind('}}')`（即倒数第 2 个 `}` 的位置）插入，
                #   等于把新礼包塞进**最后一个物品的表格内部** ⇒ 变成嵌套字段，
                #   `getOneItemByKey` 按顶层键查找自然找不到 ⇒
                #   老板实测「商城买自创神功材料包/特殊功能礼包弹无法找到该资源」。
                #   （日志实锤 `[WJJH_ITEM] missing testpack_shengongmateria mapItemAttr=no`）
                #   正确位置 = idx+1（越过物品闭合括号，落在外层表内、紧邻最后的 `}`）。
                idx = text.rstrip().rfind('}}') + 1
                ins = ',' + ','.join(defn for _k, defn in PACKS)
                text = text[:idx] + ins + text[idx:]
                open(p, 'wb').write(text.encode('utf-8'))
                print('[overlay] gh286/gh306: Items.lua -> %d packs injected OK (top-level)' % len(PACKS), flush=True)
            else:
                print('[overlay] gh286: WARN Items.lua tail unexpected', flush=True)
    # ---- 2) Rewards.lua ----
    p = os.path.join(DEST, 'res', 'script', 'map', 'mapItemAttr', 'Rewards.lua')
    if os.path.exists(p):
        raw = open(p, 'rb').read()
        if b'["9920"]' in raw:
            print('[overlay] gh286: Rewards.lua already synced, skip', flush=True)
        else:
            text = raw.decode('utf-8', 'replace')
            if text.rstrip().endswith('}}'):
                # gh306: 同 Items.lua —— +1，否则奖励也嵌套进最后一个条目内部
                idx = text.rstrip().rfind('}}') + 1
                ins = ',' + ','.join(defn for _k, defn in REWARDS)
                text = text[:idx] + ins + text[idx:]
                open(p, 'wb').write(text.encode('utf-8'))
                print('[overlay] gh286/gh306: Rewards.lua -> %d rewards injected OK (top-level)' % len(REWARDS), flush=True)
            else:
                print('[overlay] gh286: WARN Rewards.lua tail unexpected', flush=True)
    # ---- 3) Role_Attr.lua：500 级分支后插入两个新伪属性 ----
    p = os.path.join(DEST, 'src', 'app', 'models', 'role', 'Role_Attr.lua')
    if os.path.exists(p):
        raw = open(p, 'rb').read()
        if b'testBaseSkillMax1000' in raw:
            print('[overlay] gh286: Role_Attr already synced, skip', flush=True)
        else:
            text = raw.decode('utf-8', 'replace')
            crlf = chr(13) + chr(10)
            nl = crlf if crlf in text[:4000] else chr(10)
            anchor = ('\t\tPopText("基础武学已设为 500 级（" .. n .. " 门）")' + nl +
                      '\t\treturn' + nl + '\tend' + nl)
            if text.count(anchor) == 1:
                ins = ''.join(a for _k, a in ATTRS)
                if nl != crlf:
                    ins = ins.replace(crlf, nl)
                text = text.replace(anchor, anchor + ins)
                open(p, 'wb').write(text.encode('utf-8'))
                print('[overlay] gh286: Role_Attr.lua -> %d pseudo-attrs injected OK' % len(ATTRS), flush=True)
            else:
                print('[overlay] gh286: WARN Role_Attr anchor=%d' % text.count(anchor), flush=True)


def patch_server_test_packs():
    """gh287: 把 v37 新增的 12 个礼包加进私服货架清单（test_packs.json）。

    gh286 只把礼包定义注入客户端（Items/Rewards/Role_Attr），但商城货架由**私服的**
    test_packs.json 决定（fz_api/shop_store.py 读它 → 按 group 分栏）。
    gh286 实测：服务端应答仍是「限时 29」，新礼包没上架 ⇒ 商城里看不到。
    """
    import json as _json
    cands = [
        os.path.join(BUILD_DIR, 'py_ios_server', 'jhserver', 'test_packs.json'),
        os.path.join(BUILD_DIR, 'gamelua', 'test_packs.json'),
    ]
    p = next((c for c in cands if os.path.exists(c)), None)
    if p is None:
        import glob as _glob
        hits = _glob.glob(os.path.join(BUILD_DIR, '**', 'test_packs.json'), recursive=True)
        p = hits[0] if hits else None
    if p is None:
        print('[overlay] gh287: WARN test_packs.json not found under %s' % BUILD_DIR, flush=True)
        return
    NEW_ROWS_JSON = [
        '{"key": "testpack_med_liangong", "name": "练功加速礼包", "dsc": "打开获得 5 份行功散（每份立刻减少 1 小时练功/修炼时间）+ 5 份福缘丹、灵智草、狗皮膏、轻身药（福缘+1／悟性+1~2／根骨+1~2／身法+1~2）。★ 行功散要在「练功／修炼」界面里点行功散按钮使用，不是背包里点使用；练功已完成时用不了。", "price": 1, "group": "general"}',
        '{"key": "testpack_med_jingli", "name": "精力恢复礼包", "dsc": "打开获得 5 种精力药各 5 份：流云甘露（精力+100，最猛的一种）、天香玉露（精力+50，有几率抬精力上限）×2 种、万年灵芝（经验+潜能）、冰糖葫芦（经验+潜能）。打坐修炼都吃精力，精力见底就靠它补；精力满时吃不下会提示。", "price": 1, "group": "general"}',
        '{"key": "testpack_med_exp", "name": "经验潜能礼包", "dsc": "打开获得 5 种修炼药各 5 份：潜能丹（潜能 10000+58×等级）、玄黄紫清丹（经验 20000+115×等级）、万年灵芝、冰糖葫芦、九转金丹（随机一项属性+1）。★ 潜能丹／玄黄紫清丹每天上限 20 颗，超了会提示\\"再吃就上瘾了\\"。", "price": 1, "group": "general"}',
        '{"key": "testpack_med_neili", "name": "内力礼包", "dsc": "打开获得 5 种提升内力修为的药各 5 份：无尘果（内力上限+1000，最猛）、明境丹（+500）、菩提子（+300）×2 种、万年雪莲（+10）。", "price": 1, "group": "general"}',
        '{"key": "testpack_med_siwei", "name": "四维成长礼包", "dsc": "打开获得 5 种成长药各 5 份：神力散（臂力+1~2）、轻身药（身法+1~2）、狗皮膏（根骨+1~2）、灵智草（悟性+1~2）、洗颜水（颜值+1）。★ 这类药每日限吃 3 颗（有月卡 +1）。", "price": 1, "group": "general"}',
        '{"key": "testpack_med_yanzhi", "name": "颜值礼包", "dsc": "打开获得 2 种颜值药各 5 份：洗颜水（颜值+1，必成）、美容丸（20% 概率颜值+1）。（颜值这一类的药原版就只有这两种）", "price": 1, "group": "general"}',
        '{"key": "testpack_med_special", "name": "特殊功能礼包", "dsc": "打开获得 5 种功能道具各 5 份：大补丸（挂机收益最大化）、静心丸（闭关必成功、不走火入魔）、三才丹（培元一次免真气）、遁地符（瞬移回家/任务点）、分身符（同时打工练功打坐）。★ 这几种都要在对应界面里用（挂机/闭关/培元/地图），背包里点\\"使用\\"没反应。", "price": 1, "group": "general"}',
        '{"key": "testpack_shengongmateria", "name": "自创神功材料包", "dsc": "打开获得全套自创材料各 20 份：启发/灵感/神思（创作必成）/节源卷轴，精研/超群/跃兴/复思/弃定/重思之书（提升招式品质、重置词缀）。打开后在「家园书房→神功书案→自创武学」的道具面板里使用。可重复购买。", "price": 1, "group": "general"}',
        '{"key": "testpack_xinshencap", "name": "心神上限提升礼包", "dsc": "打开后心神上限立刻提升到 99999，并回满心神（打开练功/心神面板即可见）。可重复购买（重复使用无副作用）。", "price": 1, "group": "general"}',
        '{"key": "testpack_huishendan", "name": "回神丹礼包", "dsc": "打开获得 10 颗九转凝神露（心神恢复面板里使用，一颗直接回满全部心神）。可重复购买。", "price": 1, "group": "general"}',
        '{"key": "testpack_baseskill1000", "name": "基础武学1000级包", "dsc": "打开后 12 门基础武学（拳脚/剑法/刀法/内功/轻功/招架/暗器/鞭法/棍法/毒技/乐器/双持）立刻全部升到 1000 级（官方突破表的原生高度）。可重复购买。", "price": 1, "group": "general"}',
        '{"key": "testpack_baseskill1500", "name": "基础武学1500级包", "dsc": "打开后 12 门基础武学（拳脚/剑法/刀法/内功/轻功/招架/暗器/鞭法/棍法/毒技/乐器/双持）立刻全部升到 1500 级（突破阶段同时拉满；超出官方上限的高度，强度非常猛）。可重复购买。", "price": 1, "group": "general"}'
    ]
    try:
        rows = _json.load(open(p, encoding='utf-8'))
    except Exception as e:
        print('[overlay] gh287: WARN cannot parse test_packs.json (%r)' % e, flush=True)
        return
    have = {r.get('key') for r in rows if isinstance(r, dict)}
    added = 0
    for blob in NEW_ROWS_JSON:
        r = _json.loads(blob)
        if r['key'] not in have:
            rows.append(r)
            added += 1
    if added:
        _json.dump(rows, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('[overlay] gh287: %s -> +%d packs (total %d) OK' % (p, added, len(rows)), flush=True)


def _find_server_file(rel_parts):
    """在构建目录里找私服文件（兼容 CI 的 py_ios_server/jhserver 布局）。"""
    cands = [
        os.path.join(BUILD_DIR, 'py_ios_server', 'jhserver', *rel_parts),
        os.path.join(BUILD_DIR, 'py_ios_server', *rel_parts),
        os.path.join(BUILD_DIR, 'jhserver', *rel_parts),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    import glob as _glob
    hits = _glob.glob(os.path.join(BUILD_DIR, '**', *rel_parts), recursive=True)
    return hits[0] if hits else None


def patch_shop_zero_price():
    """gh295: 商城**四个页签全部零元购**（老板 2026-10-20 定）。

    老板原话：「我后来设定的就是商城物品都是零元购」——范围选的是
    「全部 0 元（含关卡卷），但『我的元宝』仍正常显示并可涨」。

    ★ 为什么只压**标价**、不动扣费函数：
      购买链 `shop_store._ep_buy_goods_3` 里
          price = info['buyPrice'] or price_of(key,'trader')
          total = price * num
          if have < total: fail('元宝不足')      # total=0 时 0<0 为假 ⇒ 不拦
          _spend_cur(ctx, YUANBAO, total)        # total=0 ⇒ -abs(0)=0 ⇒ 不扣
      所以把标价压成 0，**购买天然免费**；绝不能改 `_spend_cur`（那是全局扣费入口，
      黑市/门派贡献/名望/美貌等都在用，无差别清零会连锁破坏）。

    覆盖四处标价来源：
      A. test_packs.json 的 price  →「限时」「神功」「商城-资源礼包」三栏的礼包
      B. MALL_GOODS 元组第 3 位    →「商城」原版 11 件（洗颜水50…包月分身符1800）
      C. _VOL_PRICE 字面量         →「关卡」6 个卷（100/150/200）
      D. PRIVILEGE_GOODS['price']  →「商城」名士/雅士特权（各 10）
    """
    import json as _json
    import re as _re
    changed = []

    # ---------- A. test_packs.json：price 全 0 ----------
    p = _find_server_file(('test_packs.json',))
    if p:
        try:
            rows = _json.load(open(p, encoding='utf-8'))
            n = 0
            for r in rows:
                if isinstance(r, dict) and r.get('price') not in (0, None):
                    r['price'] = 0
                    n += 1
            if n:
                _json.dump(rows, open(p, 'w', encoding='utf-8'),
                           ensure_ascii=False, indent=1)
            changed.append('test_packs.json(%d)' % n)
        except Exception as e:                                  # noqa: BLE001
            print('[overlay] gh295: WARN test_packs.json %r' % e, flush=True)
    else:
        print('[overlay] gh295: WARN test_packs.json not found', flush=True)

    # ---------- B+D. shop_store.py ----------
    #   ★ 注意：关卡卷价在 shop_store 里**还有第二份** `VOLUME_PRICE`
    #     （`_extra_goods()` 实际读的是这份，不是 fz_boot_server 的 `_VOL_PRICE`）。
    #     只改一处会出现「货架显示 0 元、付款仍扣 100」的幻觉，故两份都改。
    sp = _find_server_file(('fz_api', 'shop_store.py'))
    if sp:
        t = open(sp, encoding='utf-8').read()
        # ★★★ gh296 关键修复：**`or 1` 把 price=0 又变回 1**。
        #   shop_store.py 载入 test_packs.json 的那行是：
        #       TEST_PACKS[_k] = (_row.get('name') or _k, int(_row.get('price') or 1))
        #   Python 里 `0 or 1 == 1`（0 是 falsy）⇒ 我 gh295 把 JSON 全改成 0 后，
        #   这里**又全部还原成 1**。老板实测「小部分零、大部分 1」正是这个：
        #     关卡卷(VOLUME_PRICE 硬编码) / 商城 11 件(MALL_GOODS) / 特权 2 件 = 0 ✓
        #     限时 41 + 神功 173 + 商城资源 14 = 1 ✗（全走 test_packs → or 1）
        #   修：改成 `0 if price is None else int(price)`，语义="缺字段才用 1"。
        _old_pk = ("TEST_PACKS[_k] = (_row.get('name') or _k, "
                   "int(_row.get('price') or 1))")
        _new_pk = ("# gh296: 0 是合法价格（零元购），不能用 `or 1` 兜底\n"
                   "                _p = _row.get('price')\n"
                   "                TEST_PACKS[_k] = (_row.get('name') or _k,\n"
                   "                                  1 if _p is None else int(_p))")
        if _old_pk in t:
            t = t.replace(_old_pk, _new_pk, 1)
            changed.append('or1-fix')
        elif "_p = _row.get('price')" in t:
            changed.append('or1-already')
        else:
            print('[overlay] gh296: WARN TEST_PACKS or1 anchor not found', flush=True)
        # B: MALL_GOODS 价格位（元组第 3 元素）
        mall_re = _re.compile(
            r"(\('[A-Za-z0-9_]+',\s*'[^']*',\s*)(\d+)(,\s*\d+,\s*'[^']*',)")
        i = t.find('MALL_GOODS = [')
        j = t.find('\n]', i)
        if i > 0 and j > i:
            seg = t[i:j]
            seg2, nb = mall_re.subn(lambda m: m.group(1) + '0' + m.group(3), seg)
            t = t[:i] + seg2 + t[j:]
            changed.append('MALL_GOODS(%d)' % nb) if nb else \
                print('[overlay] gh295: WARN MALL_GOODS matched 0', flush=True)
        else:
            print('[overlay] gh295: WARN MALL_GOODS block not found', flush=True)
        # C2: VOLUME_PRICE 形如 ('声震武林卷', 100, 'fb11;...') -> 价改 0
        m_v = _re.search(r"(VOLUME_PRICE\s*=\s*\{)(.*?)(\n\})", t, _re.S)
        if m_v:
            body, nv = _re.subn(r"(\('[^']*',\s*)(\d+)(,\s*')",
                                lambda m: m.group(1) + '0' + m.group(3),
                                m_v.group(2))
            t = t[:m_v.start()] + m_v.group(1) + body + m_v.group(3) + t[m_v.end():]
            changed.append('VOLUME_PRICE(%d)' % nv)
        else:
            print('[overlay] gh295: WARN VOLUME_PRICE anchor not found', flush=True)
        # D: PRIVILEGE_GOODS 内的 'price': N
        t, nd = _re.subn(r"('price':\s*)\d+", r"\g<1>0", t)
        if nd:
            changed.append('PRIVILEGE_GOODS(%d)' % nd)
        open(sp, 'w', encoding='utf-8').write(t)
    else:
        print('[overlay] gh295: WARN fz_api/shop_store.py not found', flush=True)

    # ---------- C. fz_boot_server.py：_VOL_PRICE ----------
    bp = _find_server_file(('fz_boot_server.py',))
    if bp:
        t = open(bp, encoding='utf-8').read()
        m = _re.search(r"(_VOL_PRICE\s*=\s*\{)(.*?)(\})", t, _re.S)
        if m:
            body = _re.sub(r":\s*\d+", ": 0", m.group(2))
            t = t[:m.start()] + m.group(1) + body + m.group(3) + t[m.end():]
            open(bp, 'w', encoding='utf-8').write(t)
            changed.append('_VOL_PRICE')
        else:
            print('[overlay] gh295: WARN _VOL_PRICE anchor not found', flush=True)
    else:
        print('[overlay] gh295: WARN fz_boot_server.py not found', flush=True)

    print('[overlay] gh295: 商城零元购 applied -> %s' %
          (', '.join(changed) if changed else 'NOTHING'), flush=True)


def patch_shenbing_unlock():
    """gh289: 神兵系统直接解锁（老板要求：单机版 v37 可直接解锁神兵）。

    iOS 现状（MainLayer.lua:603-614）：神兵按钮显示需满足任一条件——
      role:getInheritFlag("可进入苏州水底") == 1
      role:getFlag("开启神兵系统") == 1
      role:getExp() > CAN_SHENBING_EXP
    SHENBINGSYS 在 Definition.lua 里已是 true（点按钮能进 ShenBingLayer），
    只差"按钮显示"这一关。
    修：改成无条件显示（单机交付场景玩家要能直接使用）。
    """
    p = os.path.join(DEST, 'src', 'app', 'views', 'layer', 'MainLayer.lua')
    if not os.path.exists(p):
        print('[overlay] gh289: WARN MainLayer.lua not found', flush=True)
        return
    raw = open(p, 'rb').read()
    if ('gh289' in raw.decode('utf-8', 'replace')):
        print('[overlay] gh289: MainLayer already patched, skip', flush=True)
        return
    text = raw.decode('utf-8', 'replace')
    crlf = chr(13) + chr(10)
    nl = crlf if crlf in text[:4000] else chr(10)
    old = nl.join([
        '\t--判断神兵系统是否开启',
        '\tif role:getInheritFlag("可进入苏州水底") == 1 and not self.Curr_Button_State5 then',
        '\t\tself.Curr_Button_State5 = true',
        '\t\trole:setFlag("神兵","Y")',
        '\t\tself._UI.Button_shenbing:setVisible(true)',
        '\telse',
        '\t\tif (role:getFlag("开启神兵系统") == 1 or role:getExp() > CAN_SHENBING_EXP) and not self.Curr_Button_State5 then',
        '\t\t\tself.Curr_Button_State5 = true',
        '\t\t\trole:setFlag("神兵","Y")',
        '\t\t\tself._UI.Button_shenbing:setVisible(true)',
        '\t\tend',
        '\tend',
    ])
    new = nl.join([
        '\t-- gh289: 神兵系统直接解锁（单机版）',
        '\tif not self.Curr_Button_State5 then',
        '\t\tself.Curr_Button_State5 = true',
        '\t\trole:setFlag("神兵","Y")',
        '\t\tself._UI.Button_shenbing:setVisible(true)',
        '\tend',
    ])
    n = text.count(old)
    if n != 1:
        print('[overlay] gh289: WARN MainLayer shenbing anchor expected 1 got %d -> unchanged' % n,
              flush=True)
        return
    open(p, 'wb').write(text.replace(old, new).encode('utf-8'))
    print('[overlay] gh289: MainLayer.lua -> shenbing always unlocked OK', flush=True)



def patch_bagui_desc_nil_guard():
    """gh296: BagUI 里 4 处 `PopupLayerController:hideLayer("BagDescLayer", ...)`
    的 layer nil 防御（gh285 只修了 AttrControllLayer 那一处）。

    老板 gh295 实测日志实锤 10 次：
      [string "app/views/ui/AttrUI/BagUI.lua"]:711:
        attempt to call method 'hideLayer' (a nil value)
      stack: WidgetEx.lua:180 hideLayer <- BagUI.lua:711 hideFunc
             <- PopupLayerController.lua:748 hideLayer <- BagUI.lua:710 clickOneItem
    即：点背包物品时 `PopupLayerController:hideLayer` 取到的 layer 是 nil，
    回调里直接 `layer:hideLayer()` 就炸。每点一次报一次，背包状态半残。

    做法：把所有 `function(layer) layer:hideLayer() end` 的**单行/多行**写法
    统一改成带 nil 判断（nil 只记日志并 return，不中断后续链）。
    """
    p = os.path.join(DEST, 'src', 'app', 'views', 'ui', 'AttrUI', 'BagUI.lua')
    if not os.path.exists(p):
        print('[overlay] gh296: WARN BagUI.lua not found', flush=True)
        return
    raw = open(p, 'rb').read()
    if b'WJJH_BAGDESC_NIL' in raw:
        print('[overlay] gh296: BagUI already patched, skip', flush=True)
        return
    NL = b'\r\n'
    guard = (b'if layer == nil then ' +
             b'print("[WJJH_BAGDESC_NIL] BagUI hideLayer(nil) skipped") return end ')
    # 形如 `function ( layer )` / `function(layer)` 后面紧跟 layer:hideLayer()
    variants = [
        (b'function ( layer )' + NL + b'\t\t\t\t\tlayer:hideLayer()',
         b'function ( layer )' + NL + b'\t\t\t\t\t' + guard + b'layer:hideLayer()'),
        (b'function(layer)' + NL + b'\t\t\tlayer:hideLayer()',
         b'function(layer)' + NL + b'\t\t\t' + guard + b'layer:hideLayer()'),
        (b'function(layer)' + NL + b'\t\t\t\t\tlayer:hideLayer()',
         b'function(layer)' + NL + b'\t\t\t\t\t' + guard + b'layer:hideLayer()'),
    ]
    new, total = raw, 0
    for old, repl in variants:
        n = new.count(old)
        if n:
            new = new.replace(old, repl)
            total += n
    if total == 0:
        print('[overlay] gh296: WARN BagUI hideLayer anchor matched 0 -> unchanged',
              flush=True)
        return
    open(p, 'wb').write(new)
    print('[overlay] gh296: BagUI.lua -> %d hideLayer(nil) guards OK' % total,
          flush=True)


def patch_shenbing_suidi_flag():
    """gh293: 私服「回退路径」补神兵两个成对 flag（神兵直开真正生效处）。

    单机版 v37 神兵直开需要两个成对 flag：
      开始神兵任务 = 2（免做唐门之乱剧情，锻造/强化/领取）
      可进入苏州水底 = 1（主界面神兵按钮/地图锻造·熔炉·加工·修理/家具神兵台）

    关键：`_overlay_persisted_attrs` 里虽有这两个保底，但它只在 `roles/full.json`
    存在（--role ours 模板路径）时调用；**单机版走的是「回退路径」**（--danji 净化掉
    full.json，直接回客户端上传的存档），回退路径里只注入了 m_volume，**没注入神兵
    flag** ⇒ 老板实测神兵按钮在但点进去不能打造。
    补上：在回退路径的 m_volume 注入处，同步把两个 flag 写进 _inherit_flags。
    """
    cands = [
        os.path.join(BUILD_DIR, 'py_ios_server', 'jhserver', 'fz_boot_server.py'),
        os.path.join(BUILD_DIR, 'py_ios_server', 'fz_boot_server.py'),
    ]
    p = next((c for c in cands if os.path.exists(c)), None)
    if p is None:
        print('[overlay] gh293: WARN fz_boot_server.py not found', flush=True)
        return
    t = open(p, encoding='utf-8').read()
    if '可进入苏州水底标记保底置 1' in t:
        print('[overlay] gh293: already patched, skip', flush=True)
        return
    anchor = "log('★ 关卡全解锁（回退路径）：m_volume volume_1..8 = true')"
    if t.count(anchor) != 2:
        print('[overlay] gh293: WARN anchor count=%d (expected 2)' % t.count(anchor), flush=True)
        return
    block = anchor + """
            log('★ 神兵直开（回退路径）：开始神兵任务=2 + 可进入苏州水底=1')"""
    # 在每处 m_volume 注入后，把 flag 也写进去。回退路径两处分支（list / dict）都要。
    # 这里用更稳的方式：直接在 anchor 后追加一句打印，flag 真正写入交给下面单独锚点。
    open(p, 'w', encoding='utf-8').write(t.replace(anchor, block))
    print('[overlay] gh293: fz_boot_server.py -> fallback-path shenbing flag log OK', flush=True)


def patch_shenbing_flags_fallback():
    """gh293b: 回退路径真正写入两个神兵 flag（list 与 dict 两分支）。"""
    cands = [
        os.path.join(BUILD_DIR, 'py_ios_server', 'jhserver', 'fz_boot_server.py'),
        os.path.join(BUILD_DIR, 'py_ios_server', 'fz_boot_server.py'),
    ]
    p = next((c for c in cands if os.path.exists(c)), None)
    if p is None:
        print('[overlay] gh293b: WARN fz_boot_server.py not found', flush=True)
        return
    t = open(p, encoding='utf-8').read()
    if '_set_shenbing_open' in t:
        print('[overlay] gh293b: already patched, skip', flush=True)
        return
    # 在回退路径 json.loads 之后、m_volume 之前，注入一个公共辅助函数调用。
    # 锚点：两处 m_volume 注入前的 elif/if 结构。用最稳的单一锚点：注入一个 helper 定义。
    anchor = "def next_dataver():"
    if t.count(anchor) != 1:
        print('[overlay] gh293b: WARN next_dataver anchor=%d' % t.count(anchor), flush=True)
        return
    helper = '''def _set_shenbing_open(role):
    """神兵直开两个成对 flag 保底（单机版回退路径专用）。"""
    _iflags = role.get('_inherit_flags')
    if not isinstance(_iflags, dict):
        _iflags = {}
        role['_inherit_flags'] = _iflags
    try:
        _sbq = int(_iflags.get('开始神兵任务') or 0)
    except (TypeError, ValueError):
        _sbq = 0
    if _sbq < 2:
        _iflags['开始神兵任务'] = 2
    try:
        _syd = int(_iflags.get('可进入苏州水底') or 0)
    except (TypeError, ValueError):
        _syd = 0
    if _syd < 1:
        _iflags['可进入苏州水底'] = 1
    return role


'''
    t = t.replace(anchor, helper + anchor, 1)
    # 在两处回退路径分支里调用 helper
    t = t.replace(
        "            _j[0]['m_volume'] = {'volume_%d' % i: True for i in range(1, 9)}",
        "            _j[0] = _set_shenbing_open(_j[0])\n            _j[0]['m_volume'] = {'volume_%d' % i: True for i in range(1, 9)}",
        1)
    t = t.replace(
        "            _j['m_volume'] = {'volume_%d' % i: True for i in range(1, 9)}",
        "            _j = _set_shenbing_open(_j)\n            _j['m_volume'] = {'volume_%d' % i: True for i in range(1, 9)}",
        1)
    open(p, 'w', encoding='utf-8').write(t)
    print('[overlay] gh293b: fz_boot_server.py -> fallback shenbing flags injected OK', flush=True)


def patch_shenbing_repair_guard():
    """gh294: 神兵直开保底——补在**客户端** Role:repairUserData（v37 单机版原位）。

    ★★★ 为什么 gh293（只改服务端）不生效（老板实测"神兵还需做任务"）：
      v37 单机版 Role.lua:801-807 原注释写得很明白——
        「正常登录**不用服务端下发的角色档，标志只在本地存档里**；服务端下载路径
          另有同款保底」
      ⇒ 单机版玩家走「登录读本地存档 → repairUserData 修复」这条链，
        gh293 只补了私服下发路径，本地存档里的 flag 仍是 1/0 ⇒ 按钮在但打造不了。

    这里按 v37 原文（逐字对齐，四块）注入 repairUserData 开头：
      1. 「开始神兵任务」< 2 → 2   （免做唐门之乱剧情；神兵主界面/玄兵古洞NPC/家具）
      2. 「可进入苏州水底」< 1 → 1 （主界面神兵按钮/地图锻造·熔炉·加工·修理/家具神兵台）
      3. bagShenBingNumLimit < 99 → 99（v33 双上限保底；出厂 1 ⇒ 客户见"1/1"）
      4. 「开启神功系统」< 1 → 1 + shenBingNumLimit < 99 → 99（v36 神功系统直开）
    """
    p = os.path.join(DEST, 'src', 'app', 'models', 'role', 'Role.lua')
    if not os.path.exists(p):
        print('[overlay] gh294: WARN Role.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    if b'WJJH_SHENBING_GUARD' in raw:
        print('[overlay] gh294: Role.lua already patched, skip', flush=True)
        return

    NL = b'\r\n'
    # 锚点：v37 与 CI 基线里该函数唯一，且下一行固定是 --@desc 注释
    anchor = (b'function Role:repairUserData()' + NL +
              b'\t--@desc ' + '修改角色状态存储数据结构'.encode('utf-8'))

    guard = b''.join([
        b'function Role:repairUserData()' + NL,
        b'\t-- WJJH_SHENBING_GUARD (gh294): ' +
        '神兵/神功直开保底——逐字对齐单机版 v37（Role.lua:801-833）'.encode('utf-8') + NL,
        b'\tif self:getInheritFlag("' + '开始神兵任务'.encode('utf-8') + b'") < 2 then' + NL,
        b'\t\tself:setInheritFlag("' + '开始神兵任务'.encode('utf-8') + b'", 2)' + NL,
        b'\tend' + NL,
        b'\tif self:getInheritFlag("' + '可进入苏州水底'.encode('utf-8') + b'") < 1 then' + NL,
        b'\t\tself:setInheritFlag("' + '可进入苏州水底'.encode('utf-8') + b'", 1)' + NL,
        b'\tend' + NL,
        b'\tif (self:getAttr("bagShenBingNumLimit") or 0) < 99 then' + NL,
        b'\t\tself:setAttr("bagShenBingNumLimit", 99)' + NL,
        b'\tend' + NL,
        b'\tif self:getInheritFlag("' + '开启神功系统'.encode('utf-8') + b'") < 1 then' + NL,
        b'\t\tself:setInheritFlag("' + '开启神功系统'.encode('utf-8') + b'", 1)' + NL,
        b'\tend' + NL,
        b'\tif (self:getAttr("shenBingNumLimit") or 0) < 99 then' + NL,
        b'\t\tself:setAttr("shenBingNumLimit", 99)' + NL,
        b'\tend' + NL,
        b'\t--@desc ' + '修改角色状态存储数据结构'.encode('utf-8'),
    ])

    n = raw.count(anchor)
    if n != 1:
        print('[overlay] gh294: WARN Role.lua repairUserData anchor expected 1 got %d '
              '-> unchanged' % n, flush=True)
        return
    open(p, 'wb').write(raw.replace(anchor, guard, 1))
    print('[overlay] gh294: Role.lua -> repairUserData shenbing/shengong guard OK',
          flush=True)


def patch_additem_diag():
    """gh279: Role_Item:1482 无法找到该资源 诊断——商城商品 getOneItemByKey 落空。"""
    p = os.path.join(DEST, 'src', 'app', 'models', 'role', 'Role_Item.lua')
    if not os.path.exists(p):
        print('[overlay] gh279: WARN Role_Item.lua not found', flush=True)
        return
    raw = open(p, 'rb').read()
    NL = b'\r\n'
    popmsg = '无法找到该资源，请尝试重启游戏。'.encode('utf-8')
    old = (b'\tlocal itemAttr = self:getOneItemByKey(itemId)' + NL +
           b'\tif itemAttr == nil then' + NL +
           b'\t\tPopText("' + popmsg + b'")' + NL +
           b'\t\treturn false' + NL +
           b'\tend')
    diag = (
        b'\t\t-- gh279: diagnose which table lacks the definition\n' +
        b'\t\tpcall(function()\n' +
        b'\t\t\tlocal __mi = require("script.map.mapItemAttr")\n' +
        b'\t\t\tlocal __si = require("script.store.storeItem")\n' +
        b'\t\t\tlocal function __has(tbl, k)\n' +
        b'\t\t\t\tif type(tbl) ~= "table" then return "noTable" end\n' +
        b'\t\t\t\tfor _, sheet in pairs(tbl) do\n' +
        b'\t\t\t\t\tif type(sheet) == "table" and sheet[k] ~= nil then return "YES" end\n' +
        b'\t\t\t\tend\n' +
        b'\t\t\t\treturn "no"\n' +
        b'\t\t\tend\n' +
        b'\t\t\tprint("[WJJH_ITEM] missing " .. tostring(itemId)\n' +
        b'\t\t\t\t.. " mapItemAttr=" .. __has(__mi, itemId)\n' +
        b'\t\t\t\t.. " storeItem=" .. tostring(type(__si) == "table" and __si.shangchengwupin and __si.shangchengwupin[itemId] ~= nil))\n' +
        b'\t\tend)\n')
    new = (b'\tlocal itemAttr = self:getOneItemByKey(itemId)' + NL +
           b'\tif itemAttr == nil then' + NL +
           diag +
           b'\t\tPopText("' + popmsg + b'")' + NL +
           b'\t\treturn false' + NL +
           b'\tend')
    n = raw.count(old)
    if n != 1:
        print('[overlay] gh279: WARN Role_Item anchor expected 1 got %d -> unchanged' % n, flush=True)
        return
    open(p, 'wb').write(raw.replace(old, new))
    print('[overlay] gh279: Role_Item.lua -> addItem missing-item diagnostics OK', flush=True)


def patch_bagui_defensive():
    """gh276: 背包打不开（BagUI itemAttr nil）防御。"""
    p = os.path.join(DEST, 'src', 'app', 'views', 'ui', 'AttrUI', 'BagUI.lua')
    if not os.path.exists(p):
        print('[overlay] gh276: WARN BagUI.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    NL = b'\r\n'
    a1_old = (b'\t\tlocal itemAttr = role:getOneItemByKey(v)\r\n' +
              b'\t\titemInfo.text = itemAttr.name')
    a1_new = (b'\t\tlocal itemAttr = role:getOneItemByKey(v)\r\n' +
              b'\t\tif itemAttr == nil then\r\n' +
              b'\t\tprint("[WJJH_BAG] container item no attr: " .. tostring(v))\r\n' +
              b'\t\titemAttr = { name = tostring(v), canFold = ITEM_STATE_TRUE, type = "\xe5\xae\xb9\xe5\x99\xa8" }\r\n' +
              b'\t\tend\r\n' +
              b'\t\titemInfo.text = itemAttr.name')
    a2_old = (b'\t\t\tlocal itemAttr = role:getOneItemByKey(item.itemId)\r\n' +
              b'\t\t\tlocal itemInfo = {}')
    a2_new = (b'\t\t\tlocal itemAttr = role:getOneItemByKey(item.itemId)\r\n' +
              b'\t\t\tif itemAttr == nil then\r\n' +
              b'\t\t\tprint("[WJJH_BAG] item no attr: " .. tostring(item.itemId) .. " - skipped")\r\n' +
              b'\t\t\telse\r\n' +
              b'\t\t\tlocal itemInfo = {}')
    a3_old = (b'\t\t\ttable.insert(bagItemsInfo,itemInfo)\r\n' +
              b'\t\tend')
    a3_new = (b'\t\t\ttable.insert(bagItemsInfo,itemInfo)\r\n' +
              b'\t\t\tend\r\n' +
              b'\t\tend')
    new = raw
    ok = True
    for tag, old, want, repl in (('container', a1_old, 1, a1_new),
                                 ('itemhead', a2_old, 1, a2_new),
                                 ('itemtail', a3_old, 1, a3_new)):
        n = new.count(old)
        if n != want:
            print('[overlay] gh276: WARN BagUI %s expected %d got %d -> file left unchanged'
                  % (tag, want, n), flush=True)
            ok = False
            break
        new = new.replace(old, repl)
    if ok:
        open(p, 'wb').write(new)
        print('[overlay] gh276: BagUI.lua -> itemAttr nil guards (log + fallback) OK', flush=True)


def patch_ipad_resolution():
    """gh264: 启用游戏自带的 iPad 4:3 适配（config.lua 被注释的 FIXED_WIDTH 分支）。

    gh263 已把 App 打开原生 iPad（framesize=1536x2048，日志实锤生效），但
    CC_DESIGN_RESOLUTION.callback 仍无条件返回 SHOW_ALL ⇒ 1080x1920 设计在 4:3 屏上
    两侧各留 192px 黑边（老板此前在兼容模式见过 50px 边，原生后边更宽＝"开始图不完整"）。
    config.lua 的注释代码本就写明了 iPad(1536*2048) 走 FIXED_WIDTH（开发者意图），
    本补丁按其意图启用：ratio<=1.34 ⇒ FIXED_WIDTH（全宽、上下按设计裁切、无黑边）。
    """
    p = os.path.join(DEST, 'src', 'config.lua')
    if not os.path.exists(p):
        print('[overlay] gh264: WARN config.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    NL = b'\r\n'
    old = (b'    callback = function(framesize)' + NL +
           b'        return {autoscale = "SHOW_ALL"}' + NL +
           b'        -- local ratio = framesize.width / framesize.height' + NL +
           b'        -- if ratio <= 1.34 then' + NL +
           b'        -- iPad 768*1024(1536*2048) is 4:3 screen' + NL +
           b'        -- return {autoscale = "SHOW_ALL"}--"FIXED_WIDTH"}' + NL +
           b'        -- end' + NL +
           b'    end')
    new = (b'    callback = function(framesize)' + NL +
           '        -- gh264: 启用开发者注释中的 iPad 4:3 适配（原生 1536x2048 上 SHOW_ALL'.encode('utf-8') + NL +
           '        --   两侧留黑边；ratio<=1.34（4:3 屏）走 EXACT_FIT 全屏。'.encode('utf-8') + NL +
           b'        local ratio = framesize.width / framesize.height' + NL +
           b'        if ratio <= 1.34 then' + NL +
           b'            return {autoscale = "EXACT_FIT"}' + NL +
           b'        end' + NL +
           b'        return {autoscale = "SHOW_ALL"}' + NL +
           b'    end')
    n = raw.count(old)
    if n != 1:
        print('[overlay] gh264: WARN config.lua callback expected 1 got %d -> file left unchanged' % n,
              flush=True)
        return
    open(p, 'wb').write(raw.replace(old, new))
    print('[overlay] gh264: config.lua -> iPad FIXED_WIDTH callback enabled OK', flush=True)


def patch_menu_resolution():
    """gh265: MenuLayer:init 每次都把设计分辨率重置回 1080x1920 SHOW_ALL。

    gh264 在 config.lua 启用的 FIXED_WIDTH 开机生效后，进标题界面又被这里覆盖回
    SHOW_ALL ⇒ 原生 iPad 上全程两侧各 192px 黑边、所有 UI 内缩（老板"开始界面图
    残缺/不完整"的真正来源——config.lua 的补丁被这处覆盖了）。
    按屏比适配：4:3 屏（ratio<=1.34）走 FIXED_WIDTH（全宽 1440x1920），
    常规 16:9 手机维持原 SHOW_ALL 行为不变。
    """
    p = os.path.join(DEST, 'src', 'app', 'views', 'layer', 'MenuLayer', 'MenuLayer.lua')
    if not os.path.exists(p):
        print('[overlay] gh265: WARN MenuLayer.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    NL = b'\r\n'
    old = (
        '    if (channelId == "oppo" or channelId == "xiaomi") and Game:isCheckNewPackage() == NEED_CHECK_AND_IS_OPEN then'.encode('utf-8') + NL +
        b'        cc.Director:getInstance():getOpenGLView():setDesignResolutionSize(1080, 1920, cc.ResolutionPolicy.EXACT_FIT)' + NL +
        b'    else' + NL +
        b'        cc.Director:getInstance():getOpenGLView():setDesignResolutionSize(1080, 1920, cc.ResolutionPolicy.SHOW_ALL)' + NL +
        b'    end')
    new = (
        '    -- gh265/gh269: 按屏比适配——4:3 屏（原生 iPad 1536x2048）走 EXACT_FIT 全屏，'.encode('utf-8') + NL +
        '    --   （SHOW_ALL 两侧各留 192px 黑边；FIXED_WIDTH 会把 1080 宽 UI 列挤到左侧、'.encode('utf-8') + NL +
        '    --   右侧留 ~360 死区——gh268 实测）。1080x1920 全屏拉伸，与 oppo/小米渠道方案一致。'.encode('utf-8') + NL +
        '    local __fsize = cc.Director:getInstance():getOpenGLView():getFrameSize()'.encode('utf-8') + NL +
        '    local __ratio = __fsize.width / __fsize.height'.encode('utf-8') + NL +
        '    if __ratio <= 1.34 then'.encode('utf-8') + NL +
        '        cc.Director:getInstance():getOpenGLView():setDesignResolutionSize(1080, 1920, cc.ResolutionPolicy.EXACT_FIT)'.encode('utf-8') + NL +
        '    elseif (channelId == "oppo" or channelId == "xiaomi") and Game:isCheckNewPackage() == NEED_CHECK_AND_IS_OPEN then'.encode('utf-8') + NL +
        b'        cc.Director:getInstance():getOpenGLView():setDesignResolutionSize(1080, 1920, cc.ResolutionPolicy.EXACT_FIT)' + NL +
        b'    else' + NL +
        b'        cc.Director:getInstance():getOpenGLView():setDesignResolutionSize(1080, 1920, cc.ResolutionPolicy.SHOW_ALL)' + NL +
        b'    end')
    n = raw.count(old)
    if n != 1:
        print('[overlay] gh265: WARN MenuLayer resolution block expected 1 got %d -> file left unchanged' % n,
              flush=True)
        return
    open(p, 'wb').write(raw.replace(old, new))
    print('[overlay] gh265: MenuLayer.lua -> aspect-adaptive design resolution OK', flush=True)


def patch_ios_license_gate():
    """gh312: iOS 启动也走单机授权闸。

    客户端 MainScene.lua 的 startGame() 里 iOS 分支原本走 startGameForIOS()（无授权闸）；
    改为走带闸的 startGameForAndroid()（其内部：轮询 /fz_license → 显示设备码/放行进/
    拉黑停用）。是否真卡授权由服务端 license_required.flag 决定：带 flag 的单机包才卡；
    无 flag（模拟器/开发包）服务端 /fz_license 直接返回 activated=true ⇒ 秒进，无副作用。
    字节级锚定（CRLF+tab），命中数不符即 WARN 且整文件不动。
    """
    p = os.path.join(DEST, 'src', 'app', 'views', 'MainScene.lua')
    if not os.path.exists(p):
        print('[overlay] gh312: WARN MainScene.lua not found at %s' % p, flush=True)
        return
    raw = open(p, 'rb').read()
    old = b'elseif device.platform == "ios" then\r\n\t\tself:startGameForIOS()'
    new = b'elseif device.platform == "ios" then\r\n\t\tself:startGameForAndroid()'
    n = raw.count(old)
    if n == 1:
        open(p, 'wb').write(raw.replace(old, new))
        print('[overlay] gh312: MainScene.lua -> iOS 启动改走授权闸 OK', flush=True)
    else:
        print('[overlay] gh312: WARN expected 1 match for ios-dispatch, got %d -> left unchanged' % n,
              flush=True)


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

    # gh273: 版本同步增量——把安卓 danji/开发线（v155b）的内容更新带入 iOS 单机版：
    #   A 组 src 修复+玩法（技能栏 6->8 招全套、Skill 空手修复、Loading 加速、Menu 动画）
    #   + res 32 个内容表（经典五门武学表/梦境/副本/商店）。已排除 M4 桥（MyApp）、
    #   跳标题页（Game.lua）、诊断开关（Definition）、MainLayer（M4 内容混杂，二批甄别）。
    #   全部文件已按 CI 基线约定转 CRLF；gh265 等既有锚点在同步后的文件中已验证仍命中
    #   （MenuLayer 分辨率块原样存在）。排解压顺序：sync 在 res_patch.zip 之后覆盖。
    SYNC = os.path.join(BUILD_DIR, 'res_patch_sync.zip')
    if os.path.exists(SYNC):
        with zipfile.ZipFile(SYNC) as z:
            names = z.namelist()
            z.extractall(DEST)
        print('[overlay] gh273: version-sync applied %d files:' % len(names), flush=True)
        for n in names:
            p = os.path.join(DEST, n)
            print('   %-60s %d bytes %s' % (n, os.path.getsize(p),
                                            'OK' if os.path.exists(p) else 'MISSING'))
    else:
        print('[overlay] gh273: res_patch_sync.zip not found, skip version sync', flush=True)

    # gh313: 单机安卓版 v46 内容同步（老板指定源 Z:\我叫江湖-单机特别版v46.apk）。
    #   差异已实测：src 40 个 / res 28 个不同，0 增 0 删（iOS 基线本就极接近 v46）。
    #   本包只含 v46 的新版文件（独孤九剑那条按老板指示跳过）。
    #   ★ 解压位置刻意放在所有 patch_* 之前 ⇒ 其中带 iOS 补丁的 4 个文件
    #     （MainScene/BagDescLayer/ControllLayer/BagUI）会在此之后被补丁重新打上，
    #     等价于「v46 内容 + 重放 iOS 补丁」。锚点已在 v46 文件里逐条验证命中。
    V46 = os.path.join(BUILD_DIR, 'res_patch_v46.zip')
    if os.path.exists(V46):
        with zipfile.ZipFile(V46) as z:
            names = z.namelist()
            z.extractall(DEST)
        print('[overlay] gh313: v46 sync applied %d files:' % len(names), flush=True)
        miss = [n for n in names if not os.path.exists(os.path.join(DEST, n))]
        for n in names:
            print('   %-64s %d bytes' % (n, os.path.getsize(os.path.join(DEST, n))
                                         if os.path.exists(os.path.join(DEST, n)) else -1))
        if miss:
            print('[overlay] gh313: WARN %d files missing after extract: %s'
                  % (len(miss), miss[:5]), flush=True)
    else:
        print('[overlay] gh313: res_patch_v46.zip not found, skip v46 sync', flush=True)

    # gh316: v46 新增资源补缺（38 件，9.4MB）——v46 lua 引用而 iOS 包里没有的真缺口：
    #   10 张 specialEffect 特效图(321-330)、Skin_7 wenxian 换肤动画 5 件、
    #   20 张新面具/头像框/商店图、Layer 2 新图(demo.jpg/hengtiao2.png)。
    #   已按 iOS 目录映射落位（Anim/specialEffect、Anim/UIAnim/skin/Skin_7、Image/UI/...）。
    #   ★ 必须在 pvr_compress_atlas 之前 ⇒ 新图集页同样走 RGBA4444 降采样管线（内存纪律）。
    V46A = os.path.join(BUILD_DIR, 'res_patch_v46_assets.zip')
    if os.path.exists(V46A):
        with zipfile.ZipFile(V46A) as z:
            names = z.namelist()
            z.extractall(DEST)
        miss = [n for n in names if not os.path.exists(os.path.join(DEST, n))]
        print('[overlay] gh316: v46 asset gaps applied %d files (miss=%d)' % (len(names), len(miss)),
              flush=True)
        if miss:
            print('[overlay] gh316: WARN missing: %s' % miss[:5], flush=True)
    else:
        print('[overlay] gh316: res_patch_v46_assets.zip not found, skip asset gaps', flush=True)

    # gh242: 必须跑在 game.zip 解压之后、CMake 打包之前。
    patch_animres_metadata_only()
    # gh257: UI 树遍历对「失效 userdata 节点」免疫（Helper/LifeCycleSupport/NodeEx）。
    #   由老板 iPad 的 gh255 launch.log 定位到真实根因（Panel_Back 等死节点毒化遍历），
    #   修法 = tolua.isnull 预筛 + pcall 包住「回调+递归」，跳过死节点但不中断兄弟遍历。
    #   ★ 取代 gh256 的纯诊断版（诊断版只包赋值不包递归，遍历仍会中断）。
    patch_dead_node_tolerant()
    # gh259: 切层动画收尾容错——保证 aftAnim 必执行（否则触摸屏蔽层永不撤销=全局卡死）。
    #   gh258 实测链路：任务层 PageView 坏节点 → onDisable 报错 → 收尾 CallFunc 中断。
    patch_switch_anim_failsafe()
    # gh261: 战斗 3-5 号站位免建 spine（治切磋 Jetsam 闪退——10 套骨架推爆 1.4GB 上限）。
    patch_fight_spine_lazy()
    # gh264: 启用 config.lua 的 iPad 4:3 适配（原生分辨率下去黑边）。
    patch_ipad_resolution()
    # gh265: MenuLayer 会把设计分辨率重置回 SHOW_ALL（黑边回归）——按屏比适配。
    patch_menu_resolution()
    # gh276: BagUI itemAttr nil 防御（背包打不开的根因——存档物品/容器在表里查无定义）。
    patch_bagui_defensive()
    # gh279: Role_Item addItem 缺定义诊断（商城"无法找到该资源"点名）。
    patch_additem_diag()
    # gh282: 背包/仓库升级点击无反应诊断（三层探针）。
    patch_bagupgrade_diag()
    # gh285: BagDescLayer nil 防御（离开背包页 9 次报错）。
    patch_bagdesc_nil_guard()
    # gh291/gh289: 神兵按钮「无条件显示」——**gh294 起停用**。
    #   ★ 原因：老板实测 gh293 后仍是「神兵还需做任务」，而 v37 单机版 MainLayer
    #   的神兵按钮条件与基线**逐字相同**（可进入苏州水底==1 / 开启神兵系统==1 /
    #   exp>CAN_SHENBING_EXP），根本没有"无条件显示"这回事。
    #   真正解锁靠 gh294 在客户端 repairUserData 把 flag 抬到已开启值 ——
    #   flag 一旦=1，按钮按原版逻辑自然出现，无需再篡改显示条件。
    #   函数定义保留，可一行恢复。
    # patch_shenbing_unlock()
    # gh292: 恢复单机版 v37 的 12 个礼包（已确认来自 v37 的 jhserver/test_packs.json，
    #   不是开发版）：baseskill1000/1500、7 个 med_*、xinshencap、shengongmateria、huishendan。
    patch_v37_pack_sync()
    patch_server_test_packs()
    # gh293: 私服回退路径补神兵两个成对 flag（单机版实际走的路径）。
    patch_shenbing_suidi_flag()
    patch_shenbing_flags_fallback()
    # gh294: 神兵/神功直开保底补在**客户端** Role:repairUserData。
    #   gh293 只改服务端不生效（老板实测"神兵还需做任务"）：单机版登录读**本地存档**，
    #   v37 原注释明确"正常登录不用服务端下发的角色档，标志只在本地存档里"。
    #   逐字对齐 v37：开始神兵任务=2 / 可进入苏州水底=1 / 双上限 99 / 开启神功系统=1。
    patch_shenbing_repair_guard()
    # gh295: 商城四个页签**全部零元购**（老板 2026-10-20 定：
    #   "商城物品都是零元购"，范围=含关卡卷全 0，元宝仍显示可涨）。
    #   只压标价不动扣费函数：total=0 ⇒ 不拦不扣，购买天然免费。
    #   覆盖 test_packs.json / MALL_GOODS / PRIVILEGE_GOODS / _VOL_PRICE 四处。
    patch_shop_zero_price()
    # gh296: BagUI 里 4 处 hideLayer("BagDescLayer") 的 layer nil 防御
    #   （gh285 只修了 AttrControllLayer 一处；老板日志实锤 BagUI:711 报错 10 次）。
    patch_bagui_desc_nil_guard()
    # gh298: PopupLayerController:hideLayer —— 回调报错不再拖垮 _layerCache
    #   （治「点人物属性没反应」：回调炸了缓存就不更新，后续切页全被挡）。
    #   单点覆盖 baseline 全部 69 处调用点；坏单例自动重建。
    patch_popup_hidelayer_safe()
    # gh300: removeLayer 销毁前先 setTag(0)——防止 getChildByTag 把已销毁节点找回来
    #   （getInstance 会对它调 maxZ() -> getParent() -> SIGSEGV，gh299 实测必崩）。
    patch_popup_remove_clear_tag()
    # gh301: getLayer 校验 + 坏单例用 create() 兜底重建
    #   （gh300 清 tag 后仍返回非 nil 但缺 hideLayer 的对象 ⇒ 必崩）
    patch_popup_getlayer_guard()
    # gh305: 武学详情切页时销毁旧 AnimFightLayer（spine 骨架泄漏 → Jetsam）
    patch_tujian_animleak()
    # gh306: 家园常开（服务端读不到存档 ⇒ 标记恒 0 ⇒ 家园永久关闭）
    patch_home_always_open()
    # gh312: iOS 启动也走单机授权闸（设备码→面板放行→进游戏/拉黑→停用）
    patch_ios_license_gate()
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
