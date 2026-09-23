# Litematic 地形美化 Agent：MVP 工程任务书

核查日期：2026-09-23。
状态：工程设计与公开源码静态审阅；不是已实现的软件，也未完成 Minecraft 客户端联调。

## 1. 技术决策

新建轻量 Python 工程，不 fork 整个游戏模组或在线机器人项目。

- 核心：litemapy 读写方块、nbtlib 保留原始 NBT、NumPy 地形分析、Pillow 生成分析图、Pydantic 校验计划、pytest 测试。
- Agent：先采用文件协议，让具有文件、终端和图片读取能力的现有 Agent 调用 CLI；不先实现专用 Agent 框架、API 服务或 MCP 服务。
- minecraft-litematica：可作为开发期的小型资产生成器；生成后经人工验收，放入自己的资产目录。不经过它的 Scene 导入/导出链路保存原地形。
- Litematica-GPT、agentic-minecraft：参考计划/编译/校验的职责划分，不搬入 Java/Node.js 运行环境。
- GDPC：参考坐标与高度图设计，不依赖其 HTTP 世界读取接口；不用已弃用的 lookup.py 充当版本通用的方块注册表。

## 2. 第一版范围

输入：Java Edition 的 terrain.litematic、自然语言 brief.txt，可选 config.json 中的保护区、可编辑区及入口位置。

第一组真实样例以约 64×64 的自然场地为目标，选区保留足够地上空间与地下支撑层。这是工程范围建议，不是性能承诺。

支持一个 Region，支持合法的负 Size 与非零 Position；多 Region 输入明确报错，不静默丢弃或合并。

支持：顺地形步道、小型模板构筑物、沿路景观灯、灌木/花坛等静态资产。Agent 选择风格、候选地点和连接关系；程序计算坐标与施工细节。

不支持：自动建设整座城镇、任意大型建筑生成、跨 Minecraft 版本转换、任意模组方块编辑、复杂红石改造、自动登录服务器、自动粘贴、完整物理模拟、自动判定所有既有建筑边界。

原有未知方块保持只读；新增方块仅来自目标版本验证过的资产白名单。复杂实体、方块实体、待执行更新等数据不做语义修改；不能证明安全保留的数据布局应报错停止，而不是悄悄丢弃。

## 3. 数据流

terrain.litematic
  -> 原始 NBT 快照 + litemapy 解码
  -> 只读 SceneSnapshot
  -> 高度/水域/障碍/候选场地分析
  -> analysis.json + 带坐标分析图 + 资产目录
  -> Agent 输出 plan.json
  -> schema / 约束检查
  -> 确定性编译得到 PatchSet
  -> dry-run / 冲突 / 支撑 / 可达 / 预算检查
  -> 从原始快照应用补丁
  -> full.litematic + changes.litematic + changes.json + 报告

每次重编译从同一个只读快照开始，不能在上一次候选结果上累积运行全部计划。

## 4. 建议目录

```text
litegarden/
  pyproject.toml
  AGENTS.md
  src/litegarden/
    __main__.py         # inspect / compile / export
    io.py               # NBT快照、litemapy适配、保存与重读
    scene.py            # 坐标转换、SceneSnapshot、PatchSet
    terrain.py          # 高度、水域、坡度、障碍、候选地块
    render.py           # 俯视、高度、改动掩码图
    schema.py           # Plan与操作白名单
    compiler.py         # 解析引用、顺序编译、资源预算
    validate.py         # 碰撞、保护区、支撑、通行、状态校验
    report.py           # 材料、改动、警告、使用说明
    operations/
      path.py           # 顺地形寻路与铺路
      stamp.py          # 经验证的资产放置
      scatter.py        # 有约束的绿化/沿路布置
  assets/
    catalog.json
    palettes.json
    block_rules.json   # 仅包含已验证目标版本和方块
    prefabs/           # 灯柱、小亭、灌木/花坛
  tests/
    fixtures/
    test_roundtrip.py
    test_coordinates.py
    test_preservation.py
    test_operations.py
    test_constraints.py
    test_export.py
  examples/
    brief.txt
    config.json
    plan.json
```

## 5. I/O 与坐标：最高优先级

### 5.1 坐标约定

- p_region：litemapy Region 自身坐标，允许负数。
- p_schematic = region.Position + p_region。
- p_local = p_schematic - enclosing_min。
- Agent 只接触 p_local；导出时通过逆变换写回原 Region。

`region.block_positions()` 返回的是 Region 自身坐标，不能再把它当成从零开始的数组下标。

不要用 `min_schem_x/y/z + region_local_coordinate` 代替 `region.x/y/z + region_local_coordinate`；在负 Size 的区域中，这会产生多余偏移。

MVP 不提供任意旋转/镜像场地。输出保持输入的 Region.Position、Size 和 schematic 坐标语义，游戏内沿用原蓝图同一 placement 的位置与变换。世界绝对坐标不是从 Region.Position 直接推断的。

### 5.2 原始数据保留

维护两份表示：原始 NBT 树用于保存，litemapy 对象用于操作方块。

推荐保存方式：复制原始 NBT 树，在被编辑的 Region 中替换重新编码的 BlockStatePalette / BlockStates；仅更新明确列入白名单的元数据字段。Position、Size、MinecraftDataVersion 保持不变。对原有实体和其他 NBT 字段做语义/类型对比，保证非授权字段未改变。

如源数据包含未知版本或未支持的 NBT 结构，停止而非猜测。若原始导出文件本来没有保存容器内容，本工具无法恢复这些缺失数据。

不用简单新建空 Schematic 后只拷贝非空气方块来代表“保留原地形”。

### 5.3 空气语义

PatchSet 中：没有某坐标 = 不修改；after 为 minecraft:air = 明确拆除。这两者严格分离。

一条内部改动至少记录：region_id、pos_local、before、after、op_id。提交时 before 必须与原始基线匹配。内部编译器可以维护阶段工作视图，但最终输出按原始基线归并每个坐标的净变化。

## 6. SceneSnapshot 与地形摘要

原始体素数据留在程序中，不把每个方块转成对象列表发送给 Agent。

SceneSnapshot 至少包含：
- 文件摘要、MinecraftDataVersion、坐标转换、Region 信息。
- 通过 litemapy 访问的基线方块状态；可选 palette 索引数组缓存。
- editable_mask、protected_mask、known_mask。

TerrainAnalysis 至少区分：
- surface_height：可见顶部，不直接等价于可施工地面。
- ground_height：按已验证自然地面规则识别的地表。
- water_mask、obstacle_mask、slope_map、headroom。
- site_candidates：带真实 footprint、支撑/挖填估计的候选场地。
- anchors：候选入口、资产入口、连接点。

树冠、屋顶、水面和地面不能混用。不能识别的区块保守标记，既有建筑优先由保护区明确限定，不声称自动完整理解。

俯视图与高度图必须带方向/坐标刻度，且与 JSON 坐标完全一致。分析图不是 Minecraft 材质渲染图，不用其颜色近似结果声称真实美观效果。

## 7. Agent 协议

Agent 输入：brief.txt、analysis.json、分析图、资产/材质白名单、操作说明。

Agent 输出：plan.json。约束来自只读 config.json，Agent 无权放宽保护区或修改预算。

示例契约（拟实现，不是上游仓库已有格式）：

```json
{
  "schema_version": "0.1",
  "scene_id": "input-content-hash",
  "seed": 42,
  "style_id": "rustic",
  "operations": [
    {"id":"pavilion_1","op":"place_asset","asset_id":"pavilion_small","site_id":"site_03","variant":"north"},
    {"id":"path_1","op":"connect_path","from":"entry_01","to":"pavilion_1.entry","width":3,"palette_id":"stone_path"},
    {"id":"lights_1","op":"decorate_path","path_id":"path_1","asset_id":"lamp_small","spacing":8},
    {"id":"shrubs_1","op":"scatter_assets","zone_id":"plant_02","asset_id":"shrub_small","count":12}
  ]
}
```

所有 asset/site/anchor/zone/variant 引用必须存在。schema 拒绝未知操作、额外字段、非整数格坐标、超范围参数、危险代码或任意路径输入。

修改建议最多进行有限次数重试；编译失败时返回带 op_id 的结构化错误。禁止无限生成—修复循环。固定 plan、输入、资产版本和随机种子时，编译结果必须确定。

## 8. 操作实现

### connect_path

Agent 只决定连接点和道路风格。程序在高度/障碍网格上求路径，代价包含长度、坡度、转弯与挖填量。约束检查覆盖完整路宽，不只检查中心线。

第一版建议仅允许相邻路面高度差最多 1 格，不涉水、不跨越未知区域，不自动修桥。净空、台阶、填方支撑和两端入口连接均检查。不满足约束时返回无解，不能偷偷挖山。

### place_asset

只使用校验过的小型资产。每个资产登记 footprint、占用体素、必须为空的体素、支撑点、入口、允许地基深度和合法朝向变体。

不能只粘贴资产中的非空气体素：即使没有体素碰撞，也可能把门口和亭内空间留在泥土中。必须同时验证 required_empty 和入口通行空间。

第一版优先使用预验收的朝向变体，暂不实现任意方块类型的通用旋转。

### decorate_path / scatter_assets

使用同一资产放置器。先占位道路和主构筑物，再生成装饰。执行道路/入口避让、地面适配、支撑和数量检查。树木优先用静态资产，不依赖树苗生长；树叶等状态由已验证资产提供。

不把景观灯数量等同于完整的刷怪安全/光照仿真结论。

## 9. 输出与游戏使用

```text
output/
  full.litematic        # 原地形 + 全部通过检查的修改
  changes.litematic     # 净变化中 after 非空气的方块
  changes.json         # 完整新增/替换/拆除，含 before/after
  plan.json
  report.json
  report.md
  preview_before.png
  preview_after.png
  preview_changes.png
```

`changes.litematic` 必须明确标记为非空气改动投影，不是包含删除语义的通用补丁格式。未编辑的位置在它的包围盒中仍可能表现为空气；不能用 All 替换模式直接覆盖原世界。

完整结果含明确拆除后的空气。忽略空气粘贴不会完成拆除，必须参考 changes.json 或在世界副本中使用完整蓝图验收。第一版不自动执行任何世界粘贴操作。

源文件永不覆盖；保存用临时文件+原子替换。重新读入导出文件，与预期最终状态逐格对比。导出后的方块与 NBT 类型保持正确，比二进制压缩文件逐字节一致更有意义。

## 10. CLI 契约（待实现）

```text
python -m litegarden inspect terrain.litematic --out work/demo
# 现有 Agent 读取 work/demo 和 brief.txt，写 plan.json
python -m litegarden compile terrain.litematic --plan plan.json --dry-run --out work/demo
python -m litegarden export terrain.litematic --plan plan.json --out output/demo
```

export 必须再次编译与校验，不信任旧的 dry-run 状态。校验失败不产生 full.litematic；错误报告写入独立目录。

第一版不做上传网页、多用户账号、数据库、消息队列、Minecraft 服务端或自建 LLM 框架。文件协议跑通后才加 provider adapter 或网页。

## 11. 开发与验收顺序

### A. 零改动往返

构造含角点标记、非零 Position、负 Size、多个 block state 的测试文件。零改动输出逐格一致，输入文件 hash 不变，版本/坐标/原始 NBT 的非授权字段一致。多 Region 明确拒绝。此阶段不接 Agent。

### B. 确定性施工

手写固定 plan，完成步道、单个资产、沿路灯、绿化。加入狭窄走廊、斜坡、入口占用、模板内部被地形填塞、无支撑等失败样例。任何超预算/越界/保护区写入都阻止输出。

### C. 场地分析 + Agent

生成分析包与候选地点；Agent 只能选择已有引用。验证非法 JSON、未知方块、虚构 site、无解路线、API/Agent 中断等情况下仍保留原输入且不导出无效结果。

### D. 输出与人工游戏验收

至少覆盖平地、缓坡、湖岸三类实际选区。导出 full、changes、拆除清单与对比图，在世界副本中以相同 placement 对齐验收。测试渲染、材料、方块朝向、步道可走、地基、树叶/装饰支撑。此步骤通过前不得标记“端到端验证完成”。

## 12. 源码核查索引

以下均为审阅时的公开分支路径，开发前应固定实际提交 SHA；此任务书没有把分支名当成已锁定依赖。

1. litemapy：`SmylerMC/litemapy`，master。`litemapy/schematic.py`、`litemapy/minecraft.py`。
   - https://github.com/SmylerMC/litemapy
   - https://raw.githubusercontent.com/SmylerMC/litemapy/master/litemapy/schematic.py
   - https://litemapy.readthedocs.io/en/latest/litematics.html
2. minecraft-litematica：`Bissbert/minecraft-litematica`，main。`import_/litematic.py` 中每个 Region 分别归零；`ImportedRegionComponent` offset 默认零；单 Region 导出按占用包围盒归零。不能原样用作无损地形往返。
   - https://raw.githubusercontent.com/Bissbert/minecraft-litematica/main/litematica/import_/litematic.py
   - https://raw.githubusercontent.com/Bissbert/minecraft-litematica/main/litematica/core/component.py
   - https://raw.githubusercontent.com/Bissbert/minecraft-litematica/main/litematica/export/litematic.py
3. AIbuilder：`kaoyu555666777/AIbuilder`，1.20.6 分支。`BuilderMode.java` 调用 `AIClient` 与 `SchematicHelper`；读取的是提示生成结果，而不是现有地形上下文。审阅分支的导出器写死其目标 MinecraftDataVersion。
   - https://raw.githubusercontent.com/kaoyu555666777/AIbuilder/1.20.6/src/client/java/cn/kaoyu666/aibuilder/client/mode/BuilderMode.java
   - https://raw.githubusercontent.com/kaoyu555666777/AIbuilder/1.20.6/src/client/java/cn/kaoyu666/aibuilder/client/schematic/SchematicHelper.java
4. Litematica-GPT：`lukeclaw/litematica-gpt`，main。`HighEffortAIWorkflow.java` → `OpenAISchematicDSLParser.java` → `OpenAISchematicBuilder.java`。部件合并有避免空气覆盖已生成方块的逻辑，不是通用地形删除补丁语义。
   - https://raw.githubusercontent.com/lukeclaw/litematica-gpt/main/src/main/java/fi/dy/masa/litematica/schematic/ai/HighEffortAIWorkflow.java
   - https://raw.githubusercontent.com/lukeclaw/litematica-gpt/main/src/main/java/fi/dy/masa/litematica/schematic/ai/OpenAISchematicDSLParser.java
5. GDPC：`avdstaaij/gdpc`，master。`world_slice.py` 经 HTTP 获取区块与高度图；`lookup.py` 已声明弃用。
   - https://raw.githubusercontent.com/avdstaaij/gdpc/master/src/gdpc/world_slice.py
   - https://raw.githubusercontent.com/avdstaaij/gdpc/master/src/gdpc/lookup.py
6. agentic-minecraft：`cryptogakusei/agentic-minecraft`，main。蓝图编译为游戏命令；审阅的 `city-planner.ts` 网格布局使用固定 y，不能直接当成通用坡地道路算法。
   - https://raw.githubusercontent.com/cryptogakusei/agentic-minecraft/main/src/builder/compiler.ts
   - https://raw.githubusercontent.com/cryptogakusei/agentic-minecraft/main/src/planner/city-planner.ts
   - https://raw.githubusercontent.com/cryptogakusei/agentic-minecraft/main/src/critic/aesthetic-critic.ts
7. Litematica 官方粘贴说明：空气替换行为、实体/方块实体保存边界。
   - https://github.com/maruohon/litematica/wiki/Schematic-Pasting

复用前单独记录依赖、资产与复制源码的许可证；不要把“公开可读”直接等同于可以无条件再发布。
