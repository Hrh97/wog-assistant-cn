# 自动合成（低阶清理）修复说明

Fork 自 `yu-echo/wog-assistant-cn`。本 fork 只改 `js/wog-companion.js`，修复「背包/仓库爆满却 `fused: 0`，装备/首饰一直不自动合成」的 bug。

## 症状

页面常驻、WebSocket 桥 `connected`、所有自动化开关全开、`evaluatorMetrics` 零报错，但 `write:jewel-fusion` 每次返回 `fused: 0 / deposited: 0`。同时游戏里背包和仓库都已满、提示背包满。即引擎一直在跑，合成命令却每次空手而归。

## 根因（三条叠加）

用 CDP `Runtime.evaluate` 直读游戏内核，逐 `contentType × rating` 模拟 `autoRegisterFusion()` 登记：

| contentType | 含义 | `autoRegisterFusion()` | `isFusionStagingFull()` |
|---|---|---|---|
| 1 | 装备（占爆仓大头） | registered 6 | true ✅ 能合 |
| 2 | 首饰 | registered 3 | true ✅ 能合 |
| 20 | 宝玉 | registered 1 | false ❌ |

内核融合表和自动融合分区配置（gear/ac/jewel）全部正常，问题在页面 `js/wog-companion.js`：

1. **`fuse` 命令写死 `setFusionContentType(20)`**：只处理宝玉，且 `allowed_tiers` 默认只合 T1 宝玉；宝玉凑不齐 6 个就 `break`。装备/首饰（爆仓大头）根本没被合成引擎处理。
2. **`lowFuseReady` 计数口径错**：按「阶 + 等级」分组后 `Math.floor(cnt/6)`，大批物品按等级一分散，每组都不足 6，`lowFuseReady` 恒为 0 → `fuseLow` 永不触发。
3. **`fuseLow` 写死 `sameLevelOnly: true`** + `runFusionPass` 手工 `lockFusionTable/fillFusionSlots`：要求「同阶且同等级」6 件，混等级时 `isFusionStagingFull()` 为 false → `break` → `fused: 0`。

## 修复

`!sameLevelOnly` 分支的 `runFusionPass` 改为走纯内核路径，去掉脆弱的 `collectAutoRegisterCandidates(...).filter(ratingType === rating)` 预筛和 `raw.length` 门槛：

```text
clearFusionStaging()
  → autoRegisterFusion()            // 内核按 GroupID + baseLimitLevel 自行分组
  → isFusionStagingFull()
  → reqFusionStagedAsync()
```

配套两处：

- `lowFuseReady` 改成按阶计数（`lowByRating[RatingType] += 1`，再 `Math.floor(total/6)`），不再按等级拆散；
- `fuseLow` 派发时 `sameLevelOnly` 改为 `false`。

`sameLevelOnly` 分支（T3 同阶同级合成）保留原来的手工分组行为，不受影响。

## 验证

- 绕过页面直连内核复刻该路径：`fused 14`（装备 11 + 首饰 3），物品 `180 → 119`，背包/仓库立刻腾空 —— 证明内核通道与材料都没问题，是页面逻辑卡死。
- 重载页面应用修复后：仓库占用从 `84/84` 降到 `67/84`，背包 `82 → 40`，桥 `connected`、自动加点与六个日常任务照常运行、`evaluatorErrors 0`、低阶清理开关「🟢」。
- `node --check js/wog-companion.js` 通过。

## 判断「自动合成到底动没动」

以后看 `write.runtimeResult.jewels.fused` 是否 > 0，别只看 `evaluatorErrors === 0`；后者只说明桥没报错，不代表合成成功。条件不满足（没有可合材料、没有可领邮件、没有累计钻石）时静默跳过属正常。
