# 常驻守卫（watchdog）

网页里的自动化全靠浏览器 `setInterval` —— 页面一被回收（浏览器休眠、ego lite 自升版本、标签被关），
定时器就没了。**看起来「还在挂机」，其实全停。** 这是实测踩到的最大坑：
2026-11-14 曾连续 2 小时 49 分钟以为在挂机，实际 10998 一个客户端都没有。

`wog_watchdog.py` 补上这一层：掉页自动重拉，并在内核层兜底合成与加点。

## 四件事

| 职责 | 做什么 | 周期 |
| --- | --- | --- |
| **A) 保活** | 查 10998 有没有 ESTABLISHED 客户端，没有就重拉常驻页（幂等：已有则复用） | 20s 巡检，掉页后 90s 冷却 |
| **B) 合成** | 直连 10998，按 `fusion.GroupID` 分组合成装备(6)/饰品(3)/宝玉(6) | 90s 起，无产出退避到 300s |
| **C) 加点** | 三系技能树 SP 自动加点（复刻 `reqLevelUpAsync(4,...)` recipe） | 120s 起，无加点退避到 1800s |
| **D) 版本哨兵** | ego lite 自升版本后本进程 attach 上下文过期，检测到就提示重启 | 600s 检查 |

## 为什么需要它（两个实测根因）

1. **掉页是常态，不是异常**。56 小时日志里 ALERT 95 次 —— 其中 89 次间隔精确是 **30 分 10 秒**
   （ego lite 从 0.6.2.7 自升 0.6.3.14 后，旧进程的 attach 上下文与新运行时不再匹配，页面被周期性回收）。
   95 次重拉 **exit=0 全成功**，但过程完全静默，只能靠事后看日志规律才发现。
2. **网页定时器会空转**。56 小时实测：合成理论调用 2238 次、有产出仅 234 次（**空转 89.5%**）；
   技能点理论调用 1678 次、有产出仅 13 次（**空转 99.2%**，SP 加满后必然如此）。
   每次空转都要开一次内核 WebSocket。

## 自适应退避

连续无产出就把周期翻倍，一旦有产出立刻回基准：

```
FUSE : 90 → 180 → 300 → 300 …            无效内核连接 960/天 → 约 288/天（-70%）
SKILL: 120 → 240 → 480 → 960 → 1800 …    无效内核连接 720/天 → 约 48/天（-93%）
```

命中 `FUSE_BUDGET=10` 上限说明还有积压，下一轮 **30 秒**后就续跑（`FUSE_DRAIN`），库存排空更快。

## 用法

```bash
python wog_watchdog.py              # 常驻（建议 pythonw.exe / nohup 无窗口）
python wog_watchdog.py --selftest   # 三路连通性自检（ctypes / netstat / 生效值）
python wog_watchdog.py --fuse-once  # 只跑一次合成
python wog_watchdog.py --skill-dry  # 只读展开加点计划
```

前置：游戏已开 `10998`（本仓库 `wog_kernel_tools.py` 或本地版「注入补丁」负责）；
`attach-ego-space.mjs` 与它同目录。

## 关键实现

- **跨平台、零第三方依赖**，只用 Python 标准库。
- **不弹黑框**：所有 `subprocess` 带 `CREATE_NO_WINDOW + STARTUPINFO(SW_HIDE)`。
  这是实战踩出来的 —— `pythonw.exe` 本身无控制台，每 20 秒起一个 `netstat`（控制台程序），
  Windows 会给每个控制台子进程新建一个窗口，表现为「一会弹一个 cmd」。
- **零子进程巡检**：改用 `ctypes` 调 `iphlpapi.dll!GetExtendedTcpTable` 纯内存枚举 TCP 表，
  连 `netstat` 都不起；`netstat` 分支只作为 ctypes 不可用时的兜底。
- **不写死本机路径**：`ego-browser` 位置自动发现（`EGO_BROWSER` 环境变量可覆盖），
  按 `~/.local/share/ego/active_version_dir/` 与 `%LOCALAPPDATA%\ego\active_version_dir\` 探测。
- **版本哨兵只提示不自动重启**：自杀式重启会让 watchdog 自己消失，必须由外部按提示重启。
- **日志 1MB 滚动**成 `.1`（只留一代）；心跳 15 分钟一条，带当前 `fuse every / skill every`，
  一眼看出退避到了哪一档。

## 边界

- 战斗过程在服务器结算，本脚本只覆盖「发起 / 入场 / 合成 / 加点」的数据操作层。
- 不修改游戏文件、不改存档，所有写入走游戏自身的 `reqFusionStagedAsync` / `reqLevelUpAsync`。
- 只连 `127.0.0.1:10998`，数据不出本机。
