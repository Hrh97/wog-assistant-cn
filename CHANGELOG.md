# 变更记录

## v0.4.0 · 常驻守卫（2026-11-16）

把「页面被回收 = 自动化全停」这一层补上，并把 56 小时日志审计出来的低效点修掉。

- **新增 `wog_watchdog.py`**（716 行）：跨平台、零依赖常驻守卫，四职责
  - A) 保活：20s 巡检 10998 有无 ESTABLISHED 客户端，掉了重拉常驻页（幂等复用，90s 冷却）
  - B) 合成：按 `fusion.GroupID` 分组合成，90s 起、无产出退避到 300s、命中预算 30s 续跑
  - C) 加点：三系 SP 自动加点，120s 起、无加点退避到 1800s
  - D) 版本哨兵：ego lite 自升版本后 attach 上下文过期，600s 检查并提示重启
- **新增 `attach-ego-space.mjs`**：把助手页挂进 ego lite 的 Agent Space 常驻（幂等，已有则复用、不关页）
- **新增 `WATCHDOG.md`**：四职责表、两个实测根因、退避曲线、关键实现、边界
- **CHANGELOG / README**：补条目

### 为什么加它（56 小时日志实证）

1. **掉页是常态**：ALERT 95 次，其中 89 次间隔精确 **30 分 10 秒**（ego lite 0.6.2.7→0.6.3.14 后旧进程 attach 上下文过期，页面被周期性回收）。95 次重拉 exit=0 全成功，但过程静默，只能靠事后看日志规律发现。曾出现「以为在挂机、实际 2 小时 49 分零连接」。
2. **网页定时器空转严重**：合成理论调用 2238 次、有产出 234 次（**89.5% 空转**）；技能点 1678 次、有产出 13 次（**99.2% 空转**）。每次空转都开一次内核 WebSocket。
3. **`pythonw.exe` 起控制台子进程会弹黑框**：Windows 给每个控制台子进程新建窗口，表现为「一会弹一个 cmd」。改用 `ctypes GetExtendedTcpTable` 纯内存枚举 + 所有 subprocess 加 `CREATE_NO_WINDOW`，零子进程。

### 退避效果

```
FUSE : 90 → 180 → 300 → 300 …            无效内核连接 960/天 → 约 288/天（-70%）
SKILL: 120 → 240 → 480 → 960 → 1800 …    无效内核连接 720/天 → 约 48/天（-93%）
```

### 验证

`py_compile` OK；`--selftest` 三路一致 `ctypes/netstat/EFFECTIVE=True`；退避序列推演符合预期；
日志轮转单测（1.1MB → 滚成 `.1`，当前文件继续写不再滚）；`ego_version()` 正确解析 `0.6.3.14`；
本机实跑 PID 37876，10998 保持 ESTABLISHED、自动化未中断；可发布版 `EGO` 走自动发现（`EGO_BROWSER` 可覆盖）、泄露审计无本机路径。

### 上游同步

上游 `yu-echo/wog-assistant-cn` 的 `main` 仍停在 `8ae18dd`（= v0.2.0 / 2026-10-22），无新提交、无新 tag。
本版本不覆盖任何上游代码。

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
