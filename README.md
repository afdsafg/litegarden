# litegarden

Litematic 地形美化工具。读取 Java Edition 的 `terrain.litematic`，做场地分析，由 Agent 生成 `plan.json`，
确定性地编译成补丁并导出新的 litematic。

## 状态

- [x] 验收 A：零改动往返（坐标变换、原始 NBT 保留、多 Region 拒绝）
- [x] 验收 B：确定性施工（operations 层）
- [x] 验收 C：场地分析 + Agent 输入包
- [ ] 验收 D：输出与人工游戏验收（**P6 待执行**，见 `docs/ACCEPTANCE.md`）
- [x] v0.2 增量 P0：写权限守卫、净变化归并、NBT 类型保真与输入门禁
- [x] v0.2 增量 P1：`walk_no_jump_v1` 通行 profile、入口、台阶过渡、真实挖填
- [ ] v0.2 P2–P5（Viewer 工作台、交互修订、Playwright 取证、Agent 硬自检）——本轮范围外

本轮实现范围、已验证项与**未实现项**详见 `docs/V0_2_STATUS.md`。

## 安装与测试

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .[dev]
.\.venv\Scripts\python -m pytest
```

## CLI

```powershell
python -m litegarden inspect terrain.litematic --out work/demo
python -m litegarden compile terrain.litematic --plan plan.json --dry-run --out work/demo
python -m litegarden export terrain.litematic --plan plan.json --out output/demo
python -m litegarden pack terrain.litematic --config examples/config.json --out work/pack
```

`--config config.json`（只读）可声明：`protected_zones`、`editable_zone`、`task_authorized`、
`unknown_zones`、`budget`（`max_blocks/max_cut/max_fill/max_replace`）、`anchors`、
`editable_data_versions`、`path_weights`。所有盒子使用半开区间 `[min, max_exclusive)`。

`--assets assets` 中的 `block_rules.json` 提供 `allowed_new_blocks`（可放置方块白名单）与
`editable_data_versions`（可编辑 data version 白名单）；两者都不受 plan 影响。

## 输出

- `compile.json`：写策略与哈希、写审计（`events` 数、守卫统计）、道路/入口报告、
  `stats`（真实 `cut/fill/replace` 与搜索估计 `estimated_cut/estimated_fill`）、问题与诊断。
- `write_log.jsonl`：逐次写入的阶段前后状态与 op 来源。
- `nbt_preservation.json`：本次允许变化的 NBT 路径。
- `error.json`：被拒绝时写入结构化错误（含 `code / op_id / pos_local / rule_id / expected / actual`）。

非法写入会**原子拒绝**整个候选：源文件哈希不变，也不会产生新的 `full.litematic`。

## 约定

见 `AGENTS.md`、`MC_Litematic_MVP_工程任务书.md` 与 `docs/V0_2_STATUS.md`。
坐标约定与原始数据保留是最高优先级。
