# 阶段 D：人工游戏验收清单

本步骤必须在 Minecraft 客户端内人工完成。任务书规定：**此步骤通过前不得标记"端到端验证完成"。**

> 状态：**未执行**。`docs/V0_2_STATUS.md` 记录的 P0/P1 数据层与通行层校验均为软件内检查，
> 不能代替本清单。本文件中的方块集合标记为 `in_game_validated: false`。

## 0. 前置

- 原始蓝图：`terrain.litematic`（Region Position=(120,0,0)，Size=(-121,26,130)，DataVersion=4671）。
- 输出目录：`output/flat/`、`output/slope/`、`output/lakeside/`。
- 每个目录含：`full.litematic`、`changes.litematic`、`changes.json`、`compile.json`、
  `write_log.jsonl`、`nbt_preservation.json`、`report.md`、`preview_before.png`、
  `preview_after.png`、`preview_changes.png`。
- **坐标对齐**：本场景 `enclosing_min=(0,0,0)`，因此 local 坐标 == schematic 坐标。
  游戏内沿用原蓝图**同一 placement**（相同 origin、相同旋转/镜像，即无变换）即可对齐。

## 1. 放置方法

1. 在世界副本（不要用原存档）中，用 Litematica 加载原 `terrain.litematic`，记下其 placement。
2. 对每个选区，加载对应 `output/<case>/full.litematic`，使用**与原蓝图完全相同的 placement**
   （同一点作为原点，不旋转、不镜像）。
3. **不要用 `changes.litematic` 的 All 替换模式直接覆盖原世界**——它只是非空气改动投影，
   包围盒内未编辑位置是空气。拆除请参考 `changes.json` 中 `after == minecraft:air` 的条目，
   或直接使用 `full.litematic` 在世界副本中整体验收。

## 2. 三个选区（v0.2 重新导出后的数值）

| 选区 | 类型 | 场地 | 场地原点 (schematic) | 净变化 | 真实挖/填/替换 | 搜索估计填 | 入口过渡 |
|------|------|------|----------------------|--------|----------------|------------|----------|
| `output/flat/` | 平地 | site_03 | (9, 0, 24) | 315 | 35 / 134 / 146 | 7 | 1 |
| `output/slope/` | 缓坡 | site_07 | (95, 0, 15) | 368 | 29 / 188 / 151 | 124 | 1 |
| `output/lakeside/` | 湖岸 | site_01 | (89, 0, 15) | 344 | 29 / 158 / 157 | 96 | 1 |

以上数字来自 `output/<case>/compile.json` 的 `stats`；`estimated_*` 是 A* 搜索估计，
与去重后的真实值不是同一口径，不要互相替代。

## 3. 逐项检查（每个选区）

### 渲染与对齐
- [ ] `full.litematic` 在游戏内的包围盒与原 terrain 完全重合，无偏移。
- [ ] 新增方块位置与 `preview_changes.png` 的橙色掩码一致。

### 材料与方块
- [ ] 步道仅使用 `stone_path` 调色板（stone_bricks / cobblestone / gravel）与过渡半砖
      `stone_brick_slab`。
- [ ] 亭子仅使用白名单方块（oak_planks / oak_fence / lantern）。
- [ ] 无未知/模组方块；无白名单外方块。

### 方块朝向与形状
- [ ] 栅栏、灯笼朝向正确，无"悬空朝错方向"的方块。
- [ ] **新增**：过渡半砖的朝向/半位正确（本次新增 `minecraft:stone_brick_slab`，其
      游戏内表现是本轮唯一未经实测的方块；`walk_no_jump_v1` 的碰撞/台阶规则也据此声明）。

### 步道可走性
- [ ] 从入口锚点沿步道**全程无需跳跃**走到亭子入口（每段高差 ≤ 半格，或经半砖过渡）。
- [ ] 步道不涉水、不穿过树干、不悬空。
- [ ] **新增**：`compile.json` 的 `roads[].skipped` 列出的坐标处道路会局部变窄；
      请在游戏内确认这些位置可通行或可接受（那是树冠/未知方块所在列，系统按设计未改动它们）。

### 地基与支撑
- [ ] 亭子四角落柱下均有实地支撑，地基深度 ≤ 2。
- [ ] **新增**：`stats.fill` 包含"为悬空天然地面补的支撑柱"；请确认这些支撑柱在游戏内落到了
      实心地面上，没有悬空。
- [ ] 灯笼/灌木下方有支撑。

### 入口
- [ ] **新增**：亭子门口与步道之间 1 格门槛处的过渡半砖已放置（`stats.entry_transitions = 1`），
      可半格上、半格上地走进去，不需要跳。

### 树叶与装饰
- [ ] 灌木（oak_leaves）为静态资产，不依赖树苗生长。
- [ ] 装饰未占用道路或亭子入口。

### 保护区与原地形
- [ ] 既有大树、水域未被破坏（对照 `preview_before.png`）。
- [ ] 蜂巢方块实体所在体素（`output/<case>/../`. `inspect.json` 的 `entity_host_voxels`）
      的宿主方块未被改动。
- [ ] 拆除条目（`changes.json` 中 `after == minecraft:air`）与预期一致；本例应仅包含清理
      覆盖层（leaf_litter / short_grass）与必要的净空，不得包含树叶或树干。

### 数据层（无需游戏内确认，已自动校验）
- [x] 保存后重读，逐格比较方块与属性（`compare_to_expected`，408980 格）。
- [x] 类型敏感 NBT 比较：白名单之外无任何字段/类型变化（`nbt_preservation.json`）。
- [x] 源文件 `terrain.litematic` 哈希不变。

## 4. 通过标准

三个选区全部检查项通过，且游戏内无结构错误、无悬空、无越界写入，方可标记端到端验证完成。
在此之前，只能说"软件自动化检查通过"，**不得**声称 Minecraft 全链路或真实通行已验收。

## 5. 失败处理

任一检查失败：
1. 记录失败选区、坐标、截图。
2. 回到对应 `plan.json` 调整（换场地 / 换入口 / 改路宽 / 减数量）。
3. 重新 `export`（会从同一原始快照重新编译，不在旧结果上累积）。
4. 重新验收。

若失败点属于"方块集合在白名单版本下不成立"（例如 `stone_brick_slab` 的表现不符），
需要先更新 `assets/block_rules.json` 并重跑本清单，不要就地放宽校验。
