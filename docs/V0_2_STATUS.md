# litegarden v0.2 增量实现状态（P0 + P1）

本文件对应《MC_Litematic_工作台与Agent硬错误自检_增量工程任务书_v0.2.md》的 **P0 与 P1** 两阶段。
结论按证据分栏陈述，不使用无条件的"全部通过"。

## 1. 本轮已实现（有测试或命令输出为证）

### P0-B 写权限与掩码（矩阵 A）

- `src/litegarden/constraints.py`
  - `MaskSet`：半开区间盒并集，语义等价于全尺寸布尔数组，但不分配数组（`test_maskset_is_sparse_but_equivalent_to_an_explicit_array`）。
  - `known_voxel / ground_valid / edit_supported` 三者分离：已知空气仍是 known；无地面 ≠ 未知体素；未知方块可读但不可安全编辑（A04）。
  - `WriteGuard.check_write`：越界、保护区、未授权选区、未知体素、**data version 门禁**、实体宿主依赖、方块规则，逐条拒绝；错误结构固定含
    `code / op_id / pos_local / rule_id / expected / actual`。
  - **保护优先**：protected 与 editable 重叠时 protected 决定（A06）。
  - `policy_hash`：保护区/可编辑区/版本白名单/方块白名单任一变化都会改变哈希，用于让旧候选失效（A06）。
  - 半开区间 `[min, max_exclusive)`；整数解析拒绝 bool / float / 字符串。
- **三道门**：
  1. 编译期每次真实写入前（`WorkingWorld.write` → `guard.check_write`）；
  2. 归并出净补丁后逐坐标重查，并核对每条 `before`（`guard.check_net_patch`）；
  3. 导出写盘前用当前规则再次重查（`__main__._cmd_export`）。
- **原子拒绝**：非法写入立即抛错，整个候选被拒；**不会**把越界项从补丁里裁掉后继续。被拒时源文件哈希不变、不产生 `full.litematic`、只留下结构化错误报告（A01/A02/A03/A05）。

### P0-B 净变化归并（矩阵 B）

- `src/litegarden/net_patch.py`
  - `WorkingWorld` 是编译期唯一可变状态；操作层只**提案** `BlockChange`，由同一个写入口落盘。
  - `WriteEvent`（阶段前后状态、op_id、顺序号）与 `NetChange`（`before = Br[p]`、`after = 最终工作视图[p]`、`contributors`）分离。
  - 冲突策略：不同 op 覆盖同一坐标而无显式 target/dependency 时抛 `WRITE_CONFLICT`（不再 last-writer-wins）。
  - 阶段 `before` 不符立即抛 `BEFORE_MISMATCH`，不靠"最终覆盖"掩盖（B03）。
  - 零净变化不豁免权限：先裁决权限再判断无变化（A03、B02）。
  - 属性级状态比较：同 id、不同 `facing`/`waterlogged` 视为真实变化（B04）。
  - `scene_semantic_hash`：与文件字节、压缩、时间戳无关的场景语义哈希，用于 B05/B06。

### P0-B NBT 类型保真与输入门禁（矩阵 C）

- `src/litegarden/nbt_compare.py`：类型敏感比较（Short↔Int、Float↔Double 位级、List 元素类型/顺序/长度、ByteArray/IntArray/LongArray），白名单路径之外的差异一律报
  `NBT_TYPE_CHANGED / NBT_VALUE_CHANGED / NBT_FIELD_MISSING / NBT_UNAUTHORIZED_FIELD_CHANGE`，并附 tag path。
- `src/litegarden/io.py`
  - `entity_host_positions` / `tile_entity_records`：`TileEntities` 的 x/y/z 按**地图坐标**解析（已用真实文件校准），宿主体素按保守只读处理。
  - `ensure_export_preserved`：重读导出后逐字段比对，并检查方块实体宿主；失败抛 `NbtPreservationError` 或 `BLOCK_ENTITY_HOST_CHANGED`。
  - raw NBT 输入门禁：多 Region / `Regions` 类型错误 / 缺失 → 解码前拒绝（C05）。
  - `block_state_from_string`：`after` 携带属性时正确构造 `BlockState`（此前会直接失败）。
  - data version 门禁：由 `assets/block_rules.json` 的 `editable_data_versions` 声明；声明后不在白名单的版本一律拒绝编辑，**不因"能读能渲染"放行**（C06）。
- 导出改为"先写临时文件 → 重读校验 → 原子替换"，失败不会留下新的 `full.litematic`。

### P1 通行、入口与挖填（矩阵 D）

- `src/litegarden/traversal.py`：`walk_no_jump_v1` profile。整方块/楼梯/上下半砖白名单驱动；未列出形状一律 `unsupported`；`None`（未知体素）按阻塞处理；1 格高差必须有合法半砖过渡才算可通行。
- `src/litegarden/operations/path.py`
  - A* 代价含 `length + slope + turn + cut_estimate + fill_estimate`，状态含**进入方向**；挖填估计覆盖全路宽。
  - 铺装按横截面取齐到**最高可建造地面**，从不削平实心地形；低处用支撑块回填到自身地面，超过 `max_fill_depth` 则跳过并报告。
  - 已知非碰撞覆盖层（`leaf_litter`、`short_grass` 等）作为"清理请求"清除；未知方块与树冠**从不覆盖**，改成跳过 + 按坐标报告。
  - 每个坐标只由最先覆盖它的横截面铺装一次，避免相邻截面互相压盖。
  - 1 格高差自动生成半砖过渡；palette 未声明 `transition` 时直接拒绝该路线。
  - 悬空地形（天然突出地面）向下补支撑柱，有界深度。
  - 路面末端进入建筑投影（亭子地板下方）的格子不再铺装，该处改由入口契约负责。
  - 铺装与实际地形相接处若出现 1 格高差，同样在半砖可放的位置生成边界过渡。
- `src/litegarden/compiler.py`：**所有装饰完成之后**在最终候选上复检——道路表面按路径顺序取"实际铺装格，未铺装格取现有地形可行走表面"（不再遇到第一个空缺就停止），其余铺装格逐格检查，入口用 `check_entry`。
  - 落在**编译器刻意未改动列**（`roads[].skipped` 有记录）上的残留通行问题记为 `manual_review` 诊断并附坐标（任务书 §15.6：无法完整验证时默认 NEEDS_MANUAL_REVIEW，绝不静默放行）；入口契约问题与编译器写过的列一律为硬错误。
- 真实挖填（净修改体素去重）与搜索估计**分别报告**（`stats.cut/fill/replace` 与 `stats.estimated_cut/estimated_fill`），硬预算独立于代价估计。

## 2. 明确未实现（本轮范围外，任务书 P2–P6）

| 项 | 状态 |
|---|---|
| P2 Viewer 集成、RenderScene、相机/切片/诊断 | 未开始 |
| P3 交互式局部重设计（选区 UI、Halo、对象来源注册表、Candidate/Revision、Undo/Redo） | 未开始 |
| P4 Playwright 取证包、截图 manifest/coverage | 未开始 |
| P5 Agent 硬错误自检 runner、有界修复、只读查询工具 | 未开始 |
| P6 游戏内人工验收 | **未完成** |
| Aesthetic Critic | **不建设**（按任务书要求） |

因此本轮**不能**声称"v0.2 软件闭环完成"，也**不能**声称 Minecraft 全链路或真实通行已最终验收。

## 3. 需要用户知晓的实现取舍与边界（不是已验证结论）

1. **切片底层政策**：输入区域最小 y 上的方块，其下方体素在文件之外，无法证明也无法否证。这类支撑问题按 `unverifiable` 诊断记录，不作为硬错误，也**从不**被当作"已确认有支撑"。
2. **方块集合的版本适格性待 P6 确认**：`assets/block_rules.json` 记录 `in_game_validated: false`，`minecraft_data_version: 3953` 仅表示白名单撰写时的参考版本；`editable_data_versions: [4671]` 是本工程的声明，不是实测结论。`minecraft:stone_brick_slab` 为本次新增的过渡方块，同样待游戏内确认。
3. **道路局部变窄**：树冠或未知方块占据路宽时，该格被跳过并在 `roads[].skipped` 中按坐标报告，道路局部变窄而不是被削平或穿树。
4. **连续双台阶**：同一方向连续两格台阶需要楼梯过渡，本轮未实现，直接判为路线不可用（`route unsupported`）。三条真实路线均是孤立单台阶，未触发。
5. **入口门槛**：门内地面比门外高 1 格时，系统在门外较低格上方放半砖过渡（`stats.entry_transitions`）。palette 未声明 `transition` 时记录警告，并按 `check_entry` 的入口契约（|高差| ≤ 1）判定。
6. **无规则的旧式调用保持宽松**：不提供 `block_rules.json` 时不做方块白名单与版本门禁（`rules_source: none`, `version_gate: inactive`），编译器会把该状态写进报告，**不伪装成已验证**。
7. **`changes.litematic` 仍只是 after 非空气投影**，不具备删除语义，不能用 All-replace 方式粘贴。

## 4. 复现方式

```powershell
# 全部测试
python -m pytest -q -p no:cacheprovider

# 真实地形三例（源文件从不被覆盖）
python -m litegarden inspect terrain.litematic --out work/inspect_v02
python -m litegarden compile terrain.litematic --plan examples/cases/flat_plan.json `
    --config examples/cases/flat_config.json --assets assets --out work/flat --dry-run
python -m litegarden export terrain.litematic --plan examples/cases/flat_plan.json `
    --config examples/cases/flat_config.json --assets assets --out output/flat
```

`output/<case>/compile.json` 记录写策略、写审计、道路与入口报告、以及 `estimated_*` 与真实挖填；
`write_log.jsonl` 是逐次写入的阶段日志；`nbt_preservation.json` 记录本次允许变化的 NBT 路径。

游戏内验收清单见 `docs/ACCEPTANCE.md`（**P6 仍待执行**）。


## 5. 独立审查发现与修复（P0 安全边界）

本轮结束前做了一次只读对抗性代码审查，发现并修复了以下**真实缺陷**（修复后有回归测试）：

| 发现 | 修复 |
|---|---|
| `--config` 写错/不存在时静默当成"没有规则"，保护、可编辑区、版本门禁同时失效 | 指明了但打不开的配置直接报错；`--assets` 下缺 `block_rules.json` 时 `compile`/`export` 拒绝运行，并留下 `POLICY_INPUT_INVALID` 的结构化错误 |
| 原子提升与 `changes.litematic` 落盘未再校验目标路径，`--out` 指向输入目录时会覆盖源文件 | 两条落盘都先断言目标不等于输入路径，否则拒绝（退出码 2） |
| 重读校验失败时可能残留 `full.litematic` / `.tmp` | 所有可能失败的产物都在提升之前生成；失败路径统一清理临时文件 |
| 生产路径上宿主方块校验恒不通过（`apply_patchset` 原地改了快照，比较双方变成同一份） | 校验改用导入后的**未打补丁**场景副本 |
| 2D `bbox` 形式可省略 Y 范围，等于静默授权所有高度 | `bbox` 形式必须显式给出 `min_y` / `max_y_exclusive` |
| `policy_hash` 把"未声明"与"空集合"折叠成同一策略 | 摘要区分 `None` 与空集合 |
| 工作视图语义哈希缓存不随写入失效 | 每次写入使缓存失效 |
| 缺少 `export` 端到端门禁的回归测试 | 新增 `tests/test_export_gates.py`：保护区内写入被拒、配置路径错误被拒、缺规则被拒、拒绝覆盖输入、成功导出后重读与 NBT 校验仍成立 |

审查同时确认成立的部分：操作层只提案不写；拒绝后调用方场景保持干净；净变化的 `before` 严格取自基线且零净变化不豁免权限；NBT 白名单无法覆盖 `Position`/`Size`/`MinecraftDataVersion`（region_id 含 `.` 时是 fail-closed 的假失败）；保护优先于可编辑区。
