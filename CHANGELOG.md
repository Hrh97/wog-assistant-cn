# 变更记录

## v0.3.0 · 内核自动化工具（2026-11-14）

在 fork 基线 `8ae18dd`（上游 v0.2.0）之上，第二个功能版本。

- **新增 `wog_kernel_tools.py`**：跨平台、零依赖、只用 Python 标准库的内核自动化工具，直连本机 `ws://127.0.0.1:10998`（Puerts V8 inspector）发 `Runtime.evaluate`。三个命令：
  - `check` 只读体检：连通性 / 我的战力 / 三系 SP / 战场累计次数 / 非堆叠物品数
  - `fuse` 按 `fusion.GroupID` 分组合成装备（6）/饰品（3）/宝玉（6），`--dry` 只读、`--budget N` 真提交
  - `skill` 三系技能树 SP 自动加点（复刻 `wog-companion.js` 的 `reqLevelUpAsync(4,...)` recipe，多轮贪心解锁前置链自动补点），默认只读、`--once` 才真写
  - 手写 WebSocket 客户端（RFC 6455 掩码帧），`awaitPromise:true` 等异步内核调用；连接函数内开内关
- **新增 `KERNEL_TOOLS.md`**：工具原理、两个内核坑的实证、实现要点、只读判据、边界
- **README**：新增「内核自动化工具」一节与目录条目
- 验证：三命令在真实游戏内核上实跑通过（`check` 读回 CP/SP/战场次数、`fuse --dry` 正常列各组件数、`skill` dry/commit 语义分离）；`py_compile` 通过

### 为什么加它（实测根因）

1. 游戏内置「自动融合 section」开着时把 `autoRegisterFusion()` 的 `minRating` 顶到 2，排除所有 T1 材料；游戏自身又没真消化 T1 → 低阶宝玉/装备长期堆仓库。工具自己 `collectAutoRegisterCandidates(...minRating=1)` 并按 GroupID 分组绕开。
2. 页面技能树「自动加点」只改本地 `localStorage` 规划，真正写游戏的 `reqLevelUpAsync(4,...)` 绑手动按钮、没接自动循环（训练所自动加点是另一套花金币的，与 SP 无关）。工具直接走内核 API。
3. 网页自动化全靠浏览器 `setInterval`，掉页即停；内核工具不依赖浏览器，是网页通道之外的独立兜底。

### 上游同步

上游 `yu-echo/wog-assistant-cn` 的 `main` 仍停在 `8ae18dd`（= v0.2.0 / 2026-10-22），无新提交。`git merge upstream/main` = `Already up to date.`；本版本不覆盖任何上游代码。

## v0.2.0-fork.1（2026-11-13）

- `js/wog-companion.js`：修复背包/仓库爆满却 `fused:0`、装备/首饰不自动合成（详见 `AUTO_FUSION_FIX.md`）。上游 v0.2.0 之后未再改动该文件。
- 基线 `8ae18dd` = 上游 v0.2.0（合成闪退修复）。
