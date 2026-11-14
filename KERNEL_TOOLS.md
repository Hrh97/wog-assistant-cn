# 内核自动化工具（wog_kernel_tools.py）

跨平台、零依赖、只用 Python 标准库的小工具，直连 `ws://127.0.0.1:10998`（游戏 Puerts V8 inspector）发 `Runtime.evaluate`。它和网页里的自动化是**两套独立通道**：网页那套靠浏览器 `setInterval`，关掉页面就停；这个工具不依赖浏览器，适合「只想要稳定的内核级合成 / 加点、不想挂着一个网页」的场景。

> 数据来源和写入都在你自己的电脑本机，不经过外网。前置：游戏已开 `10998`（本地版 `start.py` 的「⚡ 注入补丁」或页面连接负责）。

## 为什么需要它

排查「背包/仓库爆满却没合成」「有技能点却没加」时发现两个游戏内核层面的坑：

1. **合成**：游戏内置「自动融合 section」开着时会把 `autoRegisterFusion()` 的 `minRating` 顶到 2，所有 `rating=1`（T1）材料被排除；而游戏自身又没真正消化 T1 —— 两边都不管，T1 宝玉/装备长期堆积。另外合成是按 `fusion.GroupID` 分组，每组硬性要 6 件装备 / 3 件饰品 / 6 颗宝玉，同组不够就本来合不了（那些是等掉落的零头，不是 bug）。
2. **技能点（SP）**：页面技能树里的「自动加点」只改本地 `localStorage` 规划，真正写游戏的 `reqLevelUpAsync(4, ...)` 绑在手动按钮上，没接自动循环；「训练所自动加点」是另一套花金币的、与 SP 无关。

本工具直接走更底层但同一套内核 API 绕开这两点，并且掉页也照常工作。

## 三个命令

```bash
# 只读体检：连通性、我的战力、三系 SP、战场累计次数、非堆叠物品数
python wog_kernel_tools.py check

# 按 fusion.GroupID 分组合成（装备 6 / 饰品 3 / 宝玉 6 件一组）
python wog_kernel_tools.py fuse --dry            # 只报告每组件数 / 可合组，不写游戏
python wog_kernel_tools.py fuse --budget 8       # 真提交，单次最多 8 组

# 三系技能树 SP 自动加点（多轮贪心，加点解锁前置链后自动补后续点）
python wog_kernel_tools.py skill --dry --budget 40        # 只读，展开加点计划
python wog_kernel_tools.py skill --once --budget 6         # 真加点，渐进不超过 6 点
```

`skill` 默认只读，想真动手必须显式加 `--once`（不加就是 dry，避免误把 SP 一次打满）；`fuse` 默认也是 dry，去 `--dry` 即真提交。`--budget` 控制单次最多做的组数/点数。

## 实现要点

- **`fuse`**：`collectAutoRegisterCandidates('Fusion', isTarget, /*includeStorage*/true, /*targetOverride*/{rating:0,level:0}, /*minRating*/1)` 取候选 → 按 `resolveFusionTableByItem().GroupID` 分组 → 找 `members.length >= fusion.MaterialRatingCnt` 的组 → `clearFusionStaging()` → `lockFusionTable()` → `fillFusionSlots()` → `isFusionStagingFull()` → `await reqFusionStagedAsync()` → 循环（一组凑齐多份可连合多轮），`budget` 封顶、`readyGroups=0` 即停。每轮先清全局 staging，避免和页面/游戏内置自动融合抢 `_fusionTid`/`_fusionSlots`。
- **`skill`**：复刻 `wog-companion.js` 的 `syncAllocationsToLiveGame` recipe —— `nn.db.skill.group.getGroupTables(gid)` → 下一级表 → `ServiceTree.canOpenSkillRow(nt)` → 材料从表内 `LevelUpItemID`/`LevelUpItemCnt` 读（不硬编码）→ `await treeS.reqLevelUpAsync(4, gid, toTid, mats)` → `getSkillGroupReachedLevel` 复核等级真涨。三系 group 自发现 `8{C}{NN}010`（C=1/2/3, NN=01..35），SP 材料 tid 8000/8100/8200 从表读。判据全以内核为准（前置 / 角色等级 / SP 材料够不够），凑不齐或被 `OpenLevel` 挡住自动跳过。
- **WS 客户端**：`base64` + `socket` + `struct` 手写 WebSocket 帧（RFC 6455 客户端掩码），`Runtime.evaluate` 带 `awaitPromise: true` 等异步内核调用返回。连接在函数内开、内关。

## 只读判据 / 期望输出

- `check` 返回 `ok:true` 即 10998 通；`sp` 三系为 0 表示 SP 已用完；`dungeon` 是三项战场累计 `play` 次数。
- `fuse --dry` 每行 `GroupID:已有/需求`，例如 `51000:12/6` 表示这一组能连合 2 组；`全 readyGroups=0` 表示当前确实没有能合的组，等挂机掉落即可，无需人工干预。
- `skill --dry` 列出展开的加点计划；`used=0` 表示当下无可加点（SP 用尽 / 被等级或前置挡）。

## 边界

- **战斗过程在服务器结算**，本工具自动化的是「发起 / 入场 / 合成 / 加点」这些数据操作层，不替你操作战斗画面。
- 工具不修改游戏文件、不改存档、不依赖内部对象 ID；所有写入都走游戏自身的 `reqFusionStagedAsync` / `reqLevelUpAsync` 接口。
- 跨平台（Windows / Linux / macOS），Python 3.9+，无第三方包。
