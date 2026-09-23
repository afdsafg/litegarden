# litegarden

Litematic 地形美化工具（MVP）。读取 Java Edition 的 `terrain.litematic`，做场地分析，由 Agent 生成 `plan.json`，确定性地编译成补丁并导出新的 litematic。

## 状态

- [x] 验收 A：零改动往返（坐标变换、原始 NBT 保留、多 Region 拒绝）
- [ ] 验收 B：确定性施工（operations 层）
- [ ] 验收 C：场地分析 + Agent
- [ ] 验收 D：输出与人工游戏验收

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
```

## 约定

见 `AGENTS.md` 与 `MC_Litematic_MVP_工程任务书.md`。坐标约定与原始数据保留是最高优先级。
