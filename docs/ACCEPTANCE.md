# 阶段 D：人工游戏验收清单

本步骤必须在 Minecraft 客户端内人工完成。任务书规定：**此步骤通过前不得标记"端到端验证完成"。**

## 0. 前置

- 原始蓝图：`terrain.litematic`（Region Position=(120,0,0)，Size=(-121,26,130)，DataVersion=4671）。
- 输出目录：`output/flat/`、`output/slope/`、`output/lakeside/`。
- 每个目录含：`full.litematic`、`changes.litematic`、`changes.json`、`removed.json`、`report.md`、`preview_before.png`、`preview_after.png`、`preview_changes.png`、`compare.png`。
- **坐标对齐**：本场景 `enclosing_min=(0,0,0)`，因此 local 坐标 == schematic 坐标。游戏内沿用原蓝图**同一 placement**（相同 origin、相同旋转/镜像，即无变换）即可对齐。

## 1. 放置方法

1. 在世界副本（不要用原存档）中，用 Litematica 加载原 `terrain.litematic`，记下其 placement。
2. 对每个选区，加载对应 `output/<case>/full.litematic`，使用**与原蓝图完全相同的 placement**（同一点作为原点，不旋转、不镜像）。
3. **不要用 `changes.litematic` 的 All 替换模式直接覆盖原世界**——它只是非空气改动投影，包围盒内未编辑位置是空气。拆除需参考 `removed.json` 或直接使用 `full.litematic` 在世界副本中整体验收。

## 2. 三个选区

| 选区 | 类型 | 场地 | 场地原点 (schematic) | 出口变化数 |
|------|------|------|----------------------|-----------|
| `output/flat/` | 平地 | site_03 | (9, 0, 24) | 268 |
| `output/slope/` | 缓坡 | site_07 | (95, 0, 15) | 322 |
| `output/lakeside/` | 湖岸 | site_01 | (89, 0, 15) | 322 |

## 3. 逐项检查（每个选区）

### 渲染与对齐
- [ ] `full.litematic` 在游戏内的包围盒与原 terrain 完全重合，无偏移。
- [ ] 新增方块位置与 `preview_changes.png` 的橙色掩码一致。

### 材料与方块
- [ ] 步道仅使用 `stone_path` 调色板（stone_bricks / cobblestone / gravel）。
- [ ] 亭子仅使用白名单方块（oak_planks / oak_fence / lantern）。
- [ ] 无未知/模组方块；无白名单外方块。

### 方块朝向
- [ ] 栅栏、灯笼、楼梯等朝向正确，无"悬空朝错方向"的方块。

### 步道可走性
- [ ] 从入口锚点沿步道可全程行走到亭子入口，相邻路面高差 ≤ 1。
- [ ] 步道不涉水、不穿过树干、不悬空。
- [ ] 路宽完整（3 格），不只中心线可走。

### 地基与支撑
- [ ] 亭子四角落柱下均有实地支撑，地基深度 ≤ 2。
- [ ] 步道悬空格下方已用调色板首方块填实。
- [ ] 灯笼/灌木下方有支撑。

### 树叶与装饰
- [ ] 灌木（oak_leaves）为静态资产，不依赖树苗生长。
- [ ] 装饰未占用道路或亭子入口。

### 保护区与原地形
- [ ] 既有大树、水域未被破坏（对照 `preview_before.png`）。
- [ ] 拆除清单 `removed.json` 为空（本例无拆除）；若有拆除，已按清单执行。

## 4. 通过标准

三个选区全部检查项通过，且游戏内无结构错误、无悬空、无越界写入，方可标记端到端验证完成。

## 5. 失败处理

任一检查失败：
1. 记录失败选区、坐标、截图。
2. 回到对应 `plan.json` 调整（换场地 / 换入口 / 改路宽 / 减数量）。
3. 重新 `export`（会从同一原始快照重新编译，不在旧结果上累积）。
4. 重新验收。
