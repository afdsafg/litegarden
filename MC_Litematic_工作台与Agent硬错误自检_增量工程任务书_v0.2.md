# Litematic AI 设计工作台：局部重设计与 Agent 硬错误自检
## v0.2 增量工程任务书

编制日期：2026-09-23。  
前置文档：`MC_Litematic_MVP_工程任务书.md`。  
指定 Viewer：`albertchen857/Litematica-viewer`。  
实施方式：在现有 LiteGarden/Python 工程上增量开发，不推倒重写已经通过测试的 I/O、坐标、操作和 CLI。

**文档性质与证据边界**

这是待实施的工程任务书，不是已经完成的功能报告。现有工程进度依据用户提供的实现核查，未取得其私有源码、测试日志及真实世界副本，不能把“用户报告完成”改写成“本次独立验证完成”。本次已经读取原任务书，并静态审阅指定 Viewer 的公开 `main` 分支关键文件；未运行该 Viewer 或进行 Minecraft 客户端联调。公开分支可能变化，本次未取得可靠的提交 SHA，开工必须记录实际 SHA 和资源哈希，不得以 `main` 当锁定版本。

本文中的新增模块名、HTTP 路由、CLI、JSON 字段及 `LiteGardenViewer` 接口均为**待实现契约**，不是上游现成 API。开发时先对照实际工程目录定位，再按职责映射，不为追求同名文件而大规模搬迁旧代码。

---

## 0. 本轮定案

系统从“文件式生成工具”升级为本地工作台：

```text
打开项目 → 查看当前方案 → 框选区域 → 输入重设计要求
→ 冻结当前版本与修改权限 → Agent 生成局部计划
→ 确定性编译和硬约束校验 → 序列化并重读候选蓝图
→ 同一 Viewer 渲染 → Agent 检查可验证的实现错误
→ 有界修复 → 用户比较 Before / After / Diff
→ 接受或拒绝 → 生成新修订 → 导出
```

本轮核心是三个闭环：

1. **数据正确性闭环**：保护区、可编辑区、净补丁、NBT 保留和通行约束真实执行。
2. **交互闭环**：用户选择局部，其他区域不变，候选不直接覆盖当前方案，可以拒绝、撤销和导出。
3. **硬错误自检闭环**：Agent 使用真实渲染证据和确定性查询发现并定位错误；不自动做审美打分。

**明确不建设 Aesthetic Critic。** 用户可以提出“更自然”“换日式风格”等设计要求，但自动 review 的职责是发现错位、缺失、冲突、断路、入口堵塞、非法越界等可验证实现错误，不能因为“看起来不够好看”触发自动重做。

本轮默认方案：**现有 Python 核心 + 本地 HTTP 服务 + 浏览器工作台 + 指定 Viewer 的 JSrender 派生适配层 + Playwright 截图驱动**。不先搬入完整 Tk 桌面应用，也不增加 Mineflayer、Minecraft 服务端或自动粘贴。

---

## 1. 与上一版的关系及范围

### 1.1 必须保留的能力

保留单 Region、合法负 Size、非零 Position、原始 NBT 双表示、空气/拆除语义、四类施工操作、确定性编译、源文件不覆盖、原子输出及重读逐格校验。原有 `inspect / compile / export / pack` 的可用行为和测试应持续通过。

旧任务书“不做网页”的限制由本轮工作台需求覆盖；其他数据保真和施工安全约束不因增加网页而降低。

### 1.2 本轮必须支持

| 能力 | v0.2 交付要求 |
|---|---|
| 项目打开 | 导入地形，或打开已经持久化的工作台项目 |
| 真实方块可视化 | 复用指定 Viewer 的渲染代码，显示状态属性，报告未支持内容 |
| 局部选择 | 俯视矩形 + Y 范围；3D 选点确定角点；始终显示最终长方体范围 |
| 局部重设计 | 编辑区强限制，周边上下文只读，已知生成对象可以安全替换 |
| 对比 | 当前已接受版本 / 候选版本 / 净变化切换，锁定相机 |
| 校验 | 保护、补丁、NBT、道路、入口、支撑、边界连续性 |
| Agent 自检 | 使用截图、图层、方块查询、结构化校验报告，有限修复 |
| 历史 | 候选与正式修订分离，最近一次提交的 Undo / Redo |
| 导出 | 只导出已接受且再次校验通过的修订，保留原有完整输出 |

### 1.3 不纳入本轮

不做任意形状笔刷、自动识别任意输入建筑的完整语义边界、多用户协作、任意历史提交的选择性撤销、完整红石/流体/重力模拟、自动跨版本转换、自动世界粘贴、审美评分和不受限自由方块生成。

Viewer 上游支持更多格式或多 Region，并不意味着本项目扩大输入契约。本轮工程入口仍只接受支持版本的单 Region `.litematic`。

---

## 2. 按现有核查报告补齐缺口

以下是开发优先级，不是对私有代码重新审计后的结论。

| 核查项 | 本轮处理 | 优先级与门槛 |
|---|---|---|
| #1/#2 editable/protected 掩码及强制 | 统一到不可绕过的 `WriteGuard`，逐次写入和最终提交双重检查 | P0；通过前不得启用局部重设计的正式提交 |
| #3 净变化归并 | 建立只读事务基线、阶段工作视图和最终净补丁，保留完整写入来源 | P0；通过前不得叠加局部修订 |
| #8 NBT 类型/语义比较 | 保存前后验证所有非白名单字段；检查实体数据与宿主方块一致性 | P0；不能只依赖深拷贝 |
| #6 入口通行空间 | 独立入口走廊和连接检查，并在全部装饰完成后复检 | P1；候选接受前必须通过 |
| #5 道路净空/台阶 | 用明确的通行 profile 检查全路宽、碰撞、阶梯和两端连接 | P1；不能只以 max_step=1 代替 |
| #4 挖填代价 | A* 增加非负挖填代价，编译后另算去重后的真实挖填量 | P1；代价不能代替硬预算 |
| #7 多 Region 拒绝 | 先检查 raw NBT `Regions` 类型与计数，再调用解码；已有等价检查则补测试即可 | 不重复造轮子，不要求另写二进制 NBT 扫描器 |
| #9 有限重试 | 原 CLI 失败即返回属于有界行为，并非本身不安全；新自动 review 循环明确限制尝试次数 | 接 Agent 时必做 |
| #10 游戏内验收 | 保留为独立阶段，使用世界副本和同一 placement | 未完成就不得标记全链路已通过 |

用户核查中“地形摘要已完成”与“净空校验未执行”并不矛盾：**算出了 headroom，不等于编译、接受和导出时都真正检查了 headroom。** 验收应检查调用路径与反例，不只检查字段是否存在。

---

## 3. 指定 Viewer 的源码核查与复用边界

### 3.1 实际结构

公开 README 和架构文档显示，该项目是 Python 桌面应用；主界面与 3D 窗口分开，3D 通过 pywebview 子进程承载 Deepslate 网页。[S1][S2][S8]

| 上游路径 | 已核查接口/行为 | 本项目处理 |
|---|---|---|
| `script/lv/render.py` | `build_payload()` 输出 size、palette、idx、state；`write_payload()` 写固定 `payload.js`；`open_viewer()` 起子进程 | 参考 payload 数据布局，重写为只读场景适配器；不沿用固定共享文件 |
| `script/JSrender/src/bridge.js` | `lvBuildStructure()` 将稀疏方块交给 Deepslate；Y 切层；HUD；资源初始化 | 改为可重复加载、可等待完成、可回报错误的桥接层 |
| `script/JSrender/src/viewer.js` | `createRenderCanvas()`、`setStructure()`、`render()`；全局相机变量、鼠标和键盘控制 | 封装为单实例 ViewerAdapter；补相机协议、生命周期、布局响应 |
| `script/JSrender/src/deepslate-helpers.js` | 建立方块模型、纹理图集与 flags | 复用资源装载逻辑，增加资源覆盖诊断 |
| `script/JSrender/viewer.html` | 引用 Deepslate 0.10.1、gl-matrix 3.4.3，加载 assets、atlas 和桥接脚本 | 改为本地工作台页面和本地锁定依赖 |
| `script/lv/formats.py` | 多格式统一模型和重新构造式保存；部分实体转换失败会跳过 | **不接管 LiteGarden 读写和导出** |
| `tools/selftest.py` | roundtrip 的通过条件重点比较材料统计总数 | 仅作上游自检参考，不代替本工程逐格、状态、坐标和 NBT 验证 |

这些行为分别见源码 [S3]—[S9] 及 [S11]。未在已审阅的入口和桥接文件中发现现成的局部重设计、净补丁 Diff、稳定截图协议或 Agent review API；本轮按新增功能实现，不假定上游已具备。

### 3.2 必须处理的集成风险

**共享 payload 风险。** 上游写入固定路径 `script/JSrender/payload.js`。同时打开原始、候选或不同项目时，存在互相覆盖的数据交付风险。本项目改用由 scene/revision/candidate hash 标识的不可变 JSON 资源，不向 vendor 目录写运行时数据。[S3]

**静默忽略。** `lvBuildStructure()` 对不存在的调色板项直接 `continue`。本项目必须报 `INVALID_RENDER_PALETTE_INDEX`，不能悄悄少画方块。空场景应作为合法情况显示，不能把“选区已全部挖空”与“加载失败”混为一谈。[S4]

**资源与状态覆盖。** helper 中的属性元数据接口返回 null，资源来自静态 assets/atlas。本轮必须验证实际允许资产的状态组合；不能因为传入了 props 就宣称所有版本方块都准确渲染。[S6]

**布局与生命周期。** 当前 canvas 用 window 尺寸；相机和 renderer 是全局变量；切层会重新创建 renderer。工作台需要按视口元素调整大小，管理键盘焦点，防止反复切换累积监听器或 GPU 资源。是否有可用释放 API须按锁定的 Deepslate 源码确认，不臆造上游方法。[S5]

**文本插入。** HUD/错误使用 innerHTML。场景名、模型输出和文件元数据都视为不可信文本；改为 textContent 或严格转义，不允许蓝图字符串成为执行代码。[S4]

**资源离线化。** 上游页面通过 CDN 引用两项 JS 依赖。本项目打包固定版本到本地，资源取 hash，离线截图不依赖 CDN。保留上游 MIT 代码声明，并分别核查复制的上游派生代码、方块模型和纹理来源，不能自动把根目录许可证套用到所有资产。[S7][S10]

### 3.3 版本锁定要求

开工产出 `third_party/viewer/UPSTREAM.md` 与 `viewer.lock.json`，记录：仓库 URL、实际 commit SHA、复制文件清单、各文件 hash、Deepslate/gl-matrix 版本、资源包 hash、已验证 MinecraftDataVersion 集合、许可证和本地修改。

初次集成优先复现已锁定组合；升级渲染库是独立变更，必须重跑参考场景测试，不以“升级到最新版”代替排错。

---

## 4. 总体架构与唯一数据源

```text
                         浏览器工作台
             选区 / 指令 / 对比 / 错误定位 / 接受 / Undo
                                  │
                     本地 API（单项目写入串行）
                                  │
         ┌────────────────────────┼────────────────────────┐
         ▼                        ▼                        ▼
   ProjectStore             RedesignService           ReviewService
  基线/修订/HEAD           冻结任务与局部约束        硬校验/截图/证据裁决
         │                        │                        │
         └───────────────┬────────┴────────────────────────┘
                         ▼
          现有 compiler + WriteGuard + NetPatchBuilder
                         │
                 安全序列化 → 重新读取
                         │
                CandidateSnapshot（不可变）
                         │
                   RenderSceneAdapter
                         │
              同一 JSrender / ViewerAdapter
                   ├─ 用户交互浏览
                   └─ Playwright 自动取证
```

**Python 的不可变快照、修订和补丁是唯一事实来源。** 前端场景只是显示用投影；Viewer 的材质统计、体素数组、相机或隐藏图层不能修改真正的场景。Agent 只提交受 schema 约束的计划和结构化 review，不直接写 NBT、配置、HEAD 或任意源码。

保持现有 CLI 调用核心服务；新增 HTTP 层同样调用服务，不能复制一套独立编译逻辑。FastAPI 可同时提供 API 与静态文件，本地浏览器访问同源资源。[S13]

---

## 5. P0：权限掩码与不可绕过的 WriteGuard

### 5.1 不再把 ground_height=-1 当唯一 known_mask

必须区分三件事：

| 字段/查询 | 语义 |
|---|---|
| `known_voxel(p)` | 该体素在输入范围内且方块状态成功读取；已知空气也是 known |
| `ground_valid(x,z)` | 这一列是否识别出可用地面；与“方块是否存在/已知”不同 |
| `edit_supported(p, action)` | 方块及动作是否属于当前规则可安全处理的范围 |

例如，一列全部是空气可能没有地面，但体素仍是已知的；一个未知模组方块的 ID 可以读到，但其碰撞/修改语义未必受支持。不能把“未知地面”“未知方块规则”“文件范围外”混为一类。

`editable_mask / protected_mask / known_mask` 必须有明确的三维查询接口和序列化诊断。实现可用稀疏区域/谓词以节省内存，不强制分配多个全尺寸 bool 数组，但语义必须与显式掩码等价。

### 5.2 写权限公式

对任务中每个体素 p：

```text
can_write(p, action) =
    in_input_bounds(p)
    AND known_voxel(p)
    AND global_editable(p)
    AND task_authorized(p)
    AND NOT protected(p)
    AND edit_supported(p, action)
    AND entity_dependency_safe(p, action)
```

保护优先：重叠时 protected 永远覆盖 editable。任何条件未知即拒绝。没有显式局部任务的旧 CLI 编译可按只读配置定义全局 editable，但新增网页局部任务必须显式携带授权选区。

`task_authorized` 来源是用户确认后由后端冻结的选择，不是 Agent 在 plan.json 中自行声明的 bounds。Context Halo 完全只读；本轮默认没有外扩 Blend Band。

### 5.3 强制落点

所有实际修改——铺路、清理净空、地基、装饰、旧生成对象回退、修复操作——必须经过同一个写入口。操作的提案阶段可以检查候选位置，但不得靠各 op 自觉检查权限。

至少设置三道门：

- 编译过程中每一次真实写入前检查；非法写入即停止该候选，不能先写后恢复来规避。
- 得到最终净补丁后重新逐坐标检查，并检查预算和 before。
- 接受/导出前根据被冻结任务和当前规则重新验证；规则版本变化则旧候选失效。

可视化中“显示红色保护框”不构成保护实现。API 构造、CLI 绕过前端、Agent 人工改 plan 的同类写入必须同样被拒绝。

错误至少包含：`code / op_id / pos_local / rule_id / expected / actual`。不允许简单把越界或保护区变更从补丁中裁掉，因为裁掉部分可能破坏道路或资产结构；应原子拒绝整个候选。

---

## 6. P0：事务基线与净变化归并

### 6.1 区分两个基线

- `B0`：最初导入的不可变场景，用于恢复项目和最终“累计改了什么”的导出。
- `Br`：发起本次重设计时当前已接受修订的不可变场景，是**本次事务的 before 基线**。

同一次任务的每个生成/修复尝试都从同一个 Br 开始。不能从上一失败候选继续叠加计划，也不能把本次 before 错误地绑定到 B0。

### 6.2 两类记录

内部 `WriteEvent` 记录阶段工作视图的变化：`stage_before / stage_after / op_id / target_id / sequence`。最终 `NetChange` 记录：`before=Br[p] / after=最终工作视图[p] / contributors`。

不要再依赖“后写覆盖先写”的字典直接充当最终净补丁。允许有意的顺序覆盖必须由明确的操作依赖或同一目标替换规则批准；无意的道路/装饰争用应报冲突。净归并解决 before 正确性，不自动证明所有覆盖都合理。

### 6.3 归并算法契约

以下为伪代码，需映射到实际 BlockState/PatchSet 类型：

```python
# base：本次任务被冻结的 Br；working：本次尝试私有工作视图

def write(p, after, op_id, expected_stage_before):
    current = working.get(p, base[p])
    require_exact_state(current, expected_stage_before)
    if current == after:
        return
    guard.check_write(p, current, after, op_id)
    conflict_policy.check(p, op_id, current, after)
    write_log.append((p, current, after, op_id))
    working[p] = after


def finalize():
    result = []
    for p in sorted(touched_positions):
        original = base[p]
        final = working.get(p, original)
        if original != final:
            result.append(NetChange(
                pos=p, before=original, after=final,
                contributors=ordered_writers(p)
            ))
    validate_before_matches_base(result, base)
    guard.check_net_patch(result)
    return result
```

状态相等按规范化方块 ID 和完整属性键值比较，不能只比较 palette index 或方块名称。规范化属性键顺序用于比较，不擅自删除属性或把不认识的状态当默认值。

### 6.4 必测例

```text
Br: dirt
op1: dirt → stone
op2: stone → moss_block
最终只有 dirt → moss_block，保留 op1/op2 来源。

Br: dirt
op1: dirt → stone
op2: stone → dirt
最终无净变化，但内部写事件和权限检查不能丢。

Br: air
op1: air → stone
op2: stone → air
最终无净变化，不得因为净变化为零允许越权施工。
```

累计导出 `changes.json` 从 B0 与当前已接受场景计算；某次 revision 的 `patch.json` 从父修订计算。这两种补丁必须有不同的 `base_revision_id / base_scene_hash`，不能相互直接应用。

---

## 7. P0：NBT 类型保留与数据依赖保护

### 7.1 类型敏感语义比较

对导出后重新读取的 NBT 与原始 NBT 比较，只有精确列入白名单的路径允许变化，例如被修改 Region 的 palette/BlockStates，以及经批准更新的统计元数据。不能放过整个 Metadata 或整个 Regions 子树。

比较要求：

- Compound 的键集合和每项类型/值一致；键排列不作为语义差异。
- List 的元素 tag 类型、顺序、长度及每个值一致，包含空 List 的元素类型。
- Byte/Short/Int/Long 等类型不可因为数值相同而互换。
- ByteArray/IntArray/LongArray 的类型、长度、内容一致。
- 浮点按原 tag 类型保留；对 NaN、正负零等边界，使用明确的位级或等价保真策略，不用普通 JSON 转换抹平差异。
- Entities、TileEntities、待执行更新及原有扩展字段不得被删除、重排或重建成失去类型的信息。

产生 `NBT_TYPE_CHANGED / NBT_VALUE_CHANGED / NBT_FIELD_MISSING / NBT_UNAUTHORIZED_FIELD_CHANGE`，附 tag path。补充一个“方块没变、实体数值没变，但 Short 被改成 Int”的失败样例。

### 7.2 只保存 NBT 还不够

保留箱子的 TileEntity、却把箱子方块换成石头，同样是不合格的结果。对保留数据关联的宿主方块、位置和必要支撑/邻接关系建立保护约束；对未知实体依赖无法安全判断时，阻止相关区域的破坏性编辑。

本轮不自动迁移实体、不改容器内容、不修订待执行更新。相关区域按保守只读处理；不能为了让新设计通过而删除这些 NBT。

截图不能证明 NBT 类型保留；该项始终由数据层负责。

---

## 8. P1：道路、台阶、净空、入口与挖填

### 8.1 明确通行 profile

保留 `max_step=1` 作为几何路线约束，但不能把“一格高差”直接宣布为“无需跳跃可连续步行”。

本轮采用已验证方块范围内的 `walk_no_jump_v1`：完整方块、指定朝向楼梯及指定上下半台阶拥有明确的碰撞和支撑规则。道路存在高差时，要生成合法过渡并检查沿线可步行；不支持的形状返回 unsupported，不能按整立方体猜测后通过。

允许另设明确命名的跳跃 profile，但不作为默认，也不能把它的结果显示成无需跳跃通行。玩家外形与步行能力参数是待结合目标游戏版本验证的规则，不在未实测时宣称完整复现原版运动。

### 8.2 全施工过程及最终复检

至少检查：全路宽范围、占用体积、头部空间、台阶朝向、下方支撑、起终点接口、路径连续性、净空清理写入权限和挖填预算。所有灯柱、灌木放完后再检查一次，防止装饰堵路。

净空不足时可以生成“清理请求”，但只有允许清理的已知方块且整个清理体积都可写时才执行。保护区内的树叶/障碍也不能自动删。

### 8.3 入口验证

每个资产定义 entrance 的位置、朝向、宽度、通行体积和内外连接锚点。验证不仅是 required_empty，还必须有“外部路线 → 入口 → 内部落脚区”的合法通路。

入口和路径的检查在阶段工作视图与最终候选上执行；不能只查原始地形。无可接入道路的独立资产必须在资产契约里明确允许，不能把“没有道路”一律视为所有对象的硬错误。

### 8.4 挖填代价

A* 代价可以使用：

```text
cost = w_len*length + w_slope*slope + w_turn*turn
     + w_cut*estimated_cut + w_fill*estimated_fill
```

权重非负；权重与预算来自只读配置。存在转弯代价时，搜索状态需包含进入方向，不能只以同一 x,z 合并所有方向状态后声称得到同一代价模型下最优路径。

挖填估计覆盖全路宽，标明是搜索估计。最终真实 cut/fill 按净修改体素去重统计，并对高度变化、挖深、填高和方块数执行硬约束。若本轮设置“不挖山/只清表”，超范围路线直接无解，代价再低也不能放行。

---

## 9. 工作台交互规格

### 9.1 单页面、单主视口

```text
顶栏：项目名 | 当前修订 | 保存状态 | 导出

左侧：图层、保护区、资产实例、历史
中央：同一 3D Viewer；当前/候选/Diff 切换；Y 切片
右侧：选区范围、任务指令、生成状态、硬错误列表
底部：Before/After 状态、候选接受/拒绝、Undo/Redo
```

MVP 默认用一个 renderer 切换场景，避免直接复制上游全局变量式 viewer 成多个互相污染的实例。分屏以后再做；本轮仍要支持相机锁定和快速切换对比。

### 9.2 用户流程

选择当前修订 → 框选 → 在右侧核对三维 bounds → 输入要求 → 点击生成。系统先提示部分相交的已知对象、边界道路和只读保护区，再冻结任务。

生成结果进入 Candidate，不改当前方案。用户可切换前后、定位错误、拒绝、重新生成或接受。接受后才更新 HEAD；接受必须由用户触发，Agent 只有提出候选和修复的权限。

### 9.3 选择与输入不能互相抢操作

明确区分导航模式与选择模式。输入框获得焦点时禁用 WASD/空格移动；拖动框选不能同时旋转相机。撤销快捷键仅在工作台合适焦点下生效，不截断输入框自身文字撤销。

所有选择都显示 x/y/z 区间、尺寸、坐标系和预计授权范围。渲染切片只是观察方式，不自动改变选区的 Y 范围。

### 9.4 比较语义

本次 Before 默认指 Br，不是 B0；另提供“最初地形”只读查看。

Diff 来自后端精确状态净补丁：新增、删除、替换、纯状态变更。删除对象用原状态 ghost/边界叠加显示，不往实际蓝图加入标记块。相机、切片和 crop 需在前后比较中保持一致。

---

## 10. 统一坐标与选择协议

### 10.1 继续使用原工程坐标

```text
p_schematic = Region.Position + p_region
p_local = p_schematic - enclosing_min
```

不改变原来已验证的负 Size 处理。局部任务、掩码、API、问题坐标、资产注册表统一用项目 `p_local`。

工作台 bounds 使用整数半开区间：`[min, max_exclusive)`。例：min=[10,5,20]、max_exclusive=[20,15,30] 对应 10×10×10，而不是 11×11×11。必须有 schema_version；旧配置的端点语义由显式迁移器转换，不能静默改变已有测试含义。

### 10.2 渲染坐标额外带 origin

只渲染局部 crop 时：

```text
p_render = p_local - crop_origin_local
p_local = p_render + crop_origin_local
```

相机位置、网格、问题标记、选区框都经过同一转换。不要给 Before/After 各自按非空气方块重新定原点；采用同一个 scene bounds 或两者共同的 crop。

上游 payload 的线性下标为 `i = x*sy*sz + y*sz + z`。[S3][S4] 这是本地显示数组的布局，不是要求修改 Litematic 原文件的位打包顺序。

### 10.3 两种 MVP 选择

**俯视矩形。** 在明确的顶视投影上拖选 x,z，再用高度控件设定 yMin/yMax。确定性、容易验收，作为基础功能和 3D 拾取失败时的降级方式。

**3D 两角点。** 在当前可见几何中点取方块，转换为网格坐标作为角点，再编辑高度范围。对楼梯、栏杆等非满方块，必须在 UI 标明选中的是“该体素格”，不能把格子选择代理当作真实碰撞结论。

实现拾取时必须从实际 view/projection 矩阵构造射线并做网格相交；上游 cameraPos 是参与 view matrix 的平移量，不能直接当世界相机位置使用。[S5] CSS 像素、canvas 像素和 devicePixelRatio 的变换须测试。选区不允许仅凭截图由模型猜坐标。

---

## 11. 局部重设计：只改该改的内容

### 11.1 三个范围，本轮仅两个有权限含义

- `selection`：用户授权的可编辑三维范围。
- `context_halo`：包围 selection 的只读上下文，初始建议水平外扩 12 格，属于可配置工程初值，不是实测最优值。
- `global_context`：项目风格、已有对象摘要和道路关系，可远超 crop，但只读。

Halo 从第一版局部重设计就必须存在。边界道路锚点及外部支撑不能等后续版本再补，否则第一版就可能割断道路。

本轮不默认启用外部 Blend Band。后续若增加，必须显示扩大的授权范围并由用户确认，再与 global_editable 取交集；保护区仍优先。绝不把“自动衔接”解释成可以悄悄修改选区外。

### 11.2 两类安全编辑模式

**模式 A：修订当前方案 `revise_current`。** 以 Br 为输入，在允许范围内新增/调整操作；已有对象默认保留。模型不能自行把整块区域清空。

**模式 B：替换已知生成对象 `replace_generated`。** 用户选中的对象有可验证的生成来源，系统先安全撤回这些对象的施工，再生成替代设计。四类施工 DSL 保持不变；对象撤回是后端事务准备步骤，不向 Agent 开放任意“删除所有方块”操作。

### 11.3 为什么需要来源记录

单靠当前 `.litematic`，不能可靠分辨一块石头是原山体还是 AI 的地基。本轮每次生成资产/道路必须登记：

```text
object_id、kind、creation_revision、operation_ids、asset_version
occupied_voxels / required_empty / support / entry / footprint
write_set、必要 read_dependencies、边界连接
施工前 substrate 状态、施工后的 owned 状态、依赖其他对象
```

替换前校验所有旧 owned 状态仍匹配记录，相关写入完全位于授权区，且没有未处理的后续对象依赖。随后通过同一 WriteGuard，把旧生成物及其施工地基恢复到**该次施工前的 substrate**，而不是无条件恢复成 B0 或空气。

如果旧对象写入被后续修订覆盖、共同地基/道路有依赖、来源不完整，则返回 `TARGET_OWNERSHIP_CONFLICT` 或 `TARGET_DEPENDENCY_CONFLICT`，要求扩大用户明确授权的目标集合或保留该对象。不能以 last-writer-wins 硬撤回。

已有项目迁入时，只从可验证旧日志重建来源；缺失就标记 imported/unowned。不能把导入建筑自动标成“本系统生成”。对无来源原建筑，本轮默认保护或仅允许不破坏它的增量设计；任意拆建属于后续显式授权功能。

### 11.4 部分相交对象

选区切到已知亭子一半时，只允许：用户确认扩大选区至完整对象，或保留对象。禁止后台自动扩大；暂不支持强制拆半栋。

对象完整性检查同时覆盖占用体素、施工写入、必要入口和支撑，不能只检查装饰性包围盒。

### 11.5 边界契约

提取选区与外部现有道路交点的接口：位置、方向、宽度、落脚高度、允许衔接 profile、关联对象 ID。可以改变内部走线，但既有外部连接必须保留，除非用户在新的明确任务中允许取消。

来源已知的连接优先使用注册表；输入地形中无法确定的道路由用户设置锚点或标记为待确认，不声称自动识别任意道路。

最终检查选区外状态逐格不变，并检查边界接口及其必要外部依赖。纯视觉“边缘看起来接上了”不能代替这些条件。

---

## 12. 项目、任务、候选与修订

### 12.1 最小持久化结构

```text
projects/<project_id>/
  project.json                     # 项目设置、schema、目标游戏版本
  source/
    original.litematic              # B0；不可覆盖
    source_manifest.json
  config/
    rules.<config_revision>.json    # 版本化只读约束
  revisions/
    r000/manifest.json
    r001/manifest.json
    r001/patch.json                 # 相对父修订
    r001/objects.json               # 生成来源/接口注册表
  tasks/<task_id>/
    request.json                   # 冻结 base、选区、指令、权限
    context/                       # 场地摘要、边界、模型输入
    attempts/a001/
      plan.json
      write_log.jsonl
      patch.json
      candidate.litematic          # 数据校验通过后才允许生成
      validation.json
      review/                      # 图像、manifest、模型发现、核实结果
      candidate.json               # 各项 hash、状态、可接受性
  HEAD.json                        # 指向已完整落盘的正式 revision
  exports/<export_id>/
  logs/
```

工作目录与 vendor/static 目录严格分离。运行时不写用户输入路径、第三方源码路径或共享 payload 文件。候选路径由后端分配，Agent 不能指定任意服务器路径。

### 12.2 冻结的任务请求

示例仅用于协议说明：

```json
{
  "schema_version": "0.2",
  "task_id": "task_004",
  "project_id": "demo",
  "base_revision_id": "r003",
  "base_scene_hash": "<server-computed>",
  "head_generation": 7,
  "config_revision": "cfg002",
  "config_hash": "<server-computed>",
  "selection": {
    "space": "project_local",
    "min": [10, 5, 20],
    "max_exclusive": [42, 37, 52]
  },
  "context_halo_xz": 12,
  "mode": "replace_generated",
  "target_ids": ["pavilion_003"],
  "instruction": "重新设计这个休息亭及其入口，保留外围道路。",
  "seed": 42
}
```

bounds、config、target_ids 的最终授权由后端确认；上例不意味着 Agent 能随意赋值。所有整数参数禁止把 bool、浮点或字符串隐式转成整数。

### 12.3 状态机

```text
CREATED → CONTEXT_READY → WAITING_AGENT / PLANNING
→ PLAN_READY → COMPILING → DATA_VALIDATING
→ CANDIDATE_SERIALIZED → READBACK_VALIDATING
→ RENDERING → AGENT_REVIEW → VERIFYING_FINDINGS
→ READY_FOR_USER → ACCEPTED / REJECTED
```

旁路状态：`FAILED / CANCELED / STALE / NEEDS_MANUAL_REVIEW`。这些状态不能被渲染成功或模型一句“通过”自动改成 ACCEPTED。

每个修复尝试有独立 ID，失败产物不可覆盖前一个尝试。长任务由独立工作进程执行，HTTP 只报告真实状态；客户端轮询任务进度即可，不先引入 Redis/Celery 等外部队列。

### 12.4 接受与原子提交

接受时持有项目写锁并验证：

```text
请求的 base_revision == 当前 HEAD.revision
请求的 head_generation == 当前 HEAD.generation
当前配置 hash == 冻结配置 hash
候选 file/scene/patch/evidence hash 全部匹配
所有硬校验通过；自动审查证据有效；候选未取消/过期
```

满足后，把完整 revision 写到临时目录，校验后原子提升，再原子替换 HEAD。顺序保证 HEAD 从不指向未完整落盘的 revision。崩溃恢复以 HEAD 为准，孤立完整候选可以保留但不能自动成为当前方案。

使用单写入服务和项目级锁；禁止两个服务进程同时绕过锁修改同一项目。`head_generation` 每次接受/Undo/Redo 都递增，避免用户撤销回同一 revision ID 后，旧任务误以为从未发生过状态变化。

MVP 对陈旧候选一律报 STALE 并重新基于当前版本生成；不实现自动合并不相交修改，更不自动合并相交修改。

### 12.5 Undo / Redo 的准确承诺

只保证撤销最近一次已接受修订及其线性重做。Undo 不是直接把任意旧补丁反向套到当前场景；可让 HEAD 指向经过验证的父修订，当前文件不被破坏。

撤销较早的一次修改、同时保留所有后续修改，属于带依赖的选择性撤销，本轮不承诺。撤销后产生新的正式分支时，旧 redo 链仍可保留只读记录，但不再作为直接 redo。

---

## 13. RenderScene 协议与 ViewerAdapter

### 13.1 从重读结果构建渲染输入

Candidate 的正式 review 必须渲染**已经序列化并重读的候选文件**，不能只显示编译器内存对象。这能覆盖一部分序列化/坐标/状态交付错误。

Python 将完整方块状态转成显示专用 palette；必要时把空气归为显示用索引 0，但保留原始 ID/属性对应关系，不能修改源 NBT 的 palette。空气类方块可不建 mesh，精确状态仍留在后端数据与 diff 中。

示例：

```json
{
  "schema_version": "0.2",
  "scene_id": "candidate_c004",
  "scene_hash": "<semantic-hash>",
  "file_sha256": "<serialized-file-hash>",
  "coordinate_space": "project_local",
  "crop_origin_local": [8, 0, 8],
  "size": [48, 48, 48],
  "full_scene_bounds": {"min": [0, 0, 0], "max_exclusive": [64, 64, 64]},
  "minecraft_data_version": "<copied-from-input>",
  "palette": [
    {"name": "minecraft:air", "props": {}},
    {"name": "minecraft:stone", "props": {}}
  ],
  "idx": [0, 1],
  "state": [1, 1],
  "resource_hash": "<pinned-assets-hash>",
  "counts": {"non_air_voxels": 2}
}
```

`minecraft_data_version` 在实际 schema 中为原始整数；上面的占位符不是合法生产值。所有数组长度一致，idx 唯一、整数且位于体积范围，state 在 palette 范围内。未知字段和越界数据不得静默忽略。

区分体素语义 hash 与压缩文件 SHA256：时间戳或压缩封装可能不同，编译确定性主要核对规范化的场景/补丁语义；截图仍绑定此次确切候选文件 hash。规范化算法、排除的允许变动字段和版本必须明确，不允许泛化地忽略 NBT 差异。

### 13.2 数据交付

提供 `/api/.../render-scene` 返回 `application/json`，使用不可变 candidate ID/scene hash 标识。可用 ETag，但不得让旧缓存冒充新 revision。加载请求带序号，迟到的旧响应不得覆盖已经请求的新候选。

资源与 API 同源；页面初始加载不执行由蓝图生成的 JS。不沿用上游全局 `window.LV_PAYLOAD` 文件覆盖机制。

### 13.3 新增接口契约

```typescript
interface LiteGardenViewer {
  loadScene(scene: RenderScene): Promise<RenderReceipt>;
  setCamera(camera: CameraState): void;
  getCamera(): CameraState;
  setSlice(slice: SliceState): Promise<void>;
  setSelection(selection: SelectionBox | null): void;
  setDiff(diff: DiffOverlay | null): void;
  highlightIssue(issue: IssueOverlay | null): void;
  probeVoxel(posLocal: Vec3i): VoxelDisplayInfo;
  getDiagnostics(): RenderDiagnostics;
  whenIdle(expectedSceneHash: string): Promise<RenderReceipt>;
  renderFrame(): Promise<void>;
  dispose(): void;
}
```

这是本工程需要实现的适配器 API，不表示 Deepslate 或上游已经提供同名方法。`probeVoxel` 是本地显示数据查询；最终确认仍走后端权威查询。

CameraState 使用明确的 view/projection 矩阵或经过校准的相机参数，附 viewport 宽高与 DPR；不能在不同坐标系中混用 eye/target 和上游负向平移变量。

### 13.4 RenderDiagnostics

至少包括：

```text
scene_hash / file_sha256 / renderer_build_hash / resource_hash
crop_bounds / slice_range / hidden_layers / viewport / camera
input_non_air_count / accepted_voxel_count / clipped_voxel_count
invalid_palette_indices / missing_block_definitions / missing_models
unresolved_variants / missing_textures / unsupported_block_entities
webgl_errors / context_lost / last_frame_sequence / status
```

`accepted_voxel_count` 是提交给场景模型的方块数，不是屏幕可见像素数或 mesh 面数。遮挡、透明度和面剔除会改变可见结果，不能拿三者直接对比。

缺资源用专门的诊断代理/标记显示，并列出坐标与状态；代理只存在于视图中，不得导出为替代方块。模型缺失属于 renderer 问题，不能指示 Agent 删除原本正确的方块来“修复画面”。

### 13.5 大小、相机与加载完成

增加 ResizeObserver 或等价布局处理，按工作台视口大小设置 canvas 和 DPR。绑定输入时检查焦点。切场景/切片只保留受管理的一组事件处理和渲染循环。

`whenIdle` 必须在资源加载完成、当前场景建立成功且至少完成一帧指定场景渲染后兑现，返回匹配 scene_hash 的 receipt。错误、取消、超时、WebGL context loss 应显式失败。固定 sleep 或 networkidle 不等价于渲染就绪。

---

## 14. 同一 Viewer 的自动截图与证据包

### 14.1 采用方式

用 Playwright 的独立浏览器 page 打开与用户工作台**相同构建与相同资源**的 `/review-view` 路由。通过页面执行接口控制 Viewer，再对视口元素截图；Playwright 官方提供页面 JS 求值与元素/页面截图能力。[S12]

不要让 Agent 依靠屏幕坐标模拟连续拖动来摆相机。自动化通过明确的 camera/slice/crop 协议设置，并等待渲染 receipt。

独立 page 不抢用户正在操作的相机。可先跑有界面的浏览器；无头模式需验证本机 WebGL 可用。没有可用 WebGL 时报告 `RENDER_UNAVAILABLE`，不能生成一张空白图当自检通过。

### 14.2 最小取证视图

每个候选至少保存当前 Before/After 的一致视角：全范围顶视、两个非共线斜视角、与修改高度相关的切层图。对每个新/重设计资产入口及道路边界接口，另外生成近景或剖面证据。

不是固定“拍满六张就算通过”；视图清单由修改对象和硬约束决定。道路净空可能被屋顶遮挡，需要切层；NBT 永远不靠截图验证。

每组比较用相同 crop、相机和 slice。正式 review 不继承用户随手隐藏的对象或旧层范围；取证前恢复明示的可见性状态，并写入 manifest。

### 14.3 文件结构

```text
review/
  manifest.json
  render_receipts.json
  coverage.json
  hard_validation.json
  model_findings.json
  verified_findings.json
  summary.json
  images/
    before_top.png
    after_top.png
    before_oblique_a.png
    after_oblique_a.png
    after_oblique_b.png
    after_cutaway.png
    entry_<id>.png
    boundary_<id>.png
```

manifest 绑定项目、基线、候选、计划、配置、资产、renderer、资源、截图和浏览器版本的 hash/标识，以及坐标、相机、切片、隐藏状态。每张图有稳定 image_id；不能从其他候选借一张图充数。

coverage 说明哪些区域/接口已检查、哪些可见、哪些被遮挡、哪些方块/行为不支持。**截图审查是取证和补充检查，不是对所有不可见体素的完整证明。**

### 14.4 防“看错版本”

开始和结束截图时分别核对 scene_hash 与 frame receipt；候选变化则整组作废重拍。截图成功但模型数据或资源加载失败，仍判取证失败。空图检测不能只看方差：合法的空选区应由输入统计与显式 empty 状态辨别。

可对专用校准夹具检查不对称角点标记的位置、截图坐标和方块查询一致性，但不向用户场景写入校准块。

---

## 15. Agent 硬错误自检：职责、分类与证实流程

### 15.1 三层检查，职责不互换

| 层 | 检查对象 | 结论权限 |
|---|---|---|
| A 确定性数据/施工校验 | 权限、基线、净补丁、NBT、碰撞、入口、支撑、边界、预算 | 发现已定义规则违例即阻止接受；模型不能覆盖 |
| B 渲染一致性与工具健康 | 输入是否送达、坐标/状态/切片、模型纹理覆盖、WebGL、版本一致 | 区分“设计错误”和“显示错误”；失败不能宣称视觉复核完成 |
| C Agent 视觉复核 | 从真实图像发现漏检的明显错位、断连、被堵、结构缺失等线索 | 输出可定位的疑点，调用查询核实；不是像素裁判和审美裁判 |

Agent 的 review 输入包括 A/B 报告、截图 manifest、必要局部场地摘要、对象/入口/边界契约。它不得仅看生成 plan，然后自行宣称“实现正确”。

### 15.2 第一批硬错误目录

| code | 应检查的规则 | 主要证据 |
|---|---|---|
| `WRITE_OUTSIDE_SELECTION` | 修改超出授权区 | 净补丁 + WriteGuard |
| `WRITE_PROTECTED` | 写入受保护体素/对象 | 掩码 + 写日志 |
| `BEFORE_MISMATCH` | 补丁 before 不等于本次基线 | Br + patch |
| `NBT_UNAUTHORIZED_CHANGE` | 非授权 NBT 值/类型改变 | 类型敏感比较 |
| `BLOCK_ENTITY_HOST_CHANGED` | 保留数据与宿主状态不兼容 | NBT + 最终方块 |
| `ASSET_COLLISION` | 非授权体积重叠 | 资产体积 + 最终工作视图 |
| `ASSET_REQUIRED_EMPTY_BLOCKED` | 资产契约要求为空的区域被占用 | 体素查询 |
| `ENTRY_BLOCKED` | 外部通路到入口/内部落脚区断开 | 通行 profile + 几何证据 |
| `PATH_HEADROOM_BLOCKED` | 道路头部空间不足 | 路段体积 + 碰撞规则 |
| `PATH_STEP_INVALID` | 高差或楼梯/台阶方向不能形成规定通路 | 路面状态 + 通行规则 |
| `PATH_DISCONNECTED` | 应连接的节点在最终场景不连通 | 可通行图 + 接口契约 |
| `BOUNDARY_ANCHOR_BROKEN` | 局部改动破坏已有外部接口 | Br/候选接口比较 |
| `SUPPORT_RULE_VIOLATION` | 经登记的地基、附着点或支撑规则失效 | 资产/方块规则 |
| `ASSET_STATE_MISMATCH` | 实际模板方向/状态偏离冻结契约 | 模板预期 + 最终数据 |
| `RENDER_PAYLOAD_MISMATCH` | Viewer 数据与候选场景不一致 | 场景摘要 + 浏览器回报 |
| `RENDER_RESOURCE_MISSING` | 方块定义/模型/纹理不受支持 | 资源探测 |
| `REVIEW_STALE_EVIDENCE` | 证据不属于当前候选 | hash 链 |

不要把“任何没落地的方块”定义为结构错误：屋檐、悬挑等是否合理应按具体资产/附着规则，不能套一个所有连通分量都必须落地的粗糙条件。也不能把未知游戏物理规则猜成通过。

### 15.3 Agent 禁止输出的自动修复理由

“不够自然”“颜色不协调”“建筑不够宏伟”“路太直”“景观密度太低”不属于本轮硬错误。用户可以另发设计指令，但自动 reviewer 不得以这些理由拒绝、扩大修改范围或反复重建。

外观异常只有在能对应具体契约时才可能成为硬错误，例如“要求的 north 朝向被写成 west”或“契约要求两端连通而中间断了”。

### 15.4 模型发现协议

```json
{
  "schema_version": "0.2",
  "candidate_id": "c004",
  "scene_hash": "<must-match>",
  "review_kind": "hard_error_only",
  "verdict": "suspected_issue",
  "findings": [
    {
      "id": "finding_01",
      "code": "ENTRY_BLOCKED",
      "op_id": "pavilion_1",
      "object_id": "pavilion_003",
      "bounds_local": {"min": [20, 8, 30], "max_exclusive": [23, 11, 33]},
      "evidence_image_ids": ["entry_pavilion_003"],
      "observation": "入口走廊的下部似乎存在占位方块。",
      "requested_check": "validate_entry_clearance",
      "repair_hint": "检查入口通行体积；只在授权区域内修改。"
    }
  ]
}
```

可用模型 verdict 为 `no_issue_observed / suspected_issue / insufficient_evidence / render_issue`。`no_issue_observed` 不是“已证明无错”。模型输出不得直接标记 `confirmed=true` 后跳过后端核实。

### 15.5 核实工具

提供只读、受范围和数量限制的工具/CLI/API：

```text
inspect_voxels(candidate, bounds)
inspect_object(candidate, object_id)
inspect_patch(candidate, bounds)
check_entry(candidate, entry_id)
check_path(candidate, path_id)
get_render_diagnostics(candidate)
request_evidence(candidate, camera_or_issue_preset)
```

工具返回真实数据和坐标，不接受任意 shell、Python 或 JS 代码。截图缺失方块的疑点应先查数据：数据存在而资源缺失，归为显示问题；数据不存在且与契约不符，才转为生成缺陷。

核实后状态为 `confirmed / refuted / unresolved / unsupported`。未解疑点保留并交人工，不允许为了自动通过把它丢弃。

### 15.6 接受门槛

正常自动复核链路至少满足：A 硬规则通过；B 取证有效；C 已执行且无未处理的 confirmed/unresolved 硬问题；未支持内容不影响本次修改及其必要依赖。

若修改区域存在无法正确渲染/验证的方块，默认 `NEEDS_MANUAL_REVIEW`，不自动接受。MVP 不提供“模型强行通过”按钮；可以保留最后一个已接受版本正常导出。未来的人工例外机制必须单独设计并显式标注“未完成自动验证”。

---

## 16. 有界生成—核实—修复

新增 orchestrator 只组织现有工具和 Agent 接入，不自行修改安全策略。配置示例：

```json
{
  "max_plan_attempts": 3,
  "max_review_rounds_per_attempt": 1,
  "max_evidence_requests_per_attempt": 8,
  "max_same_error_repeats": 2,
  "max_provider_retries_per_call": 1,
  "max_total_runtime_seconds": 600,
  "max_changed_blocks": "<existing-project-limit>"
}
```

这些是建议初始预算，不是运行性能承诺；实际 schema 使用具体整数。`max_plan_attempts=3` 指**首次生成加最多两次修复**，不再另外追加无限“最后一次”。

控制规则：

- 所有尝试仍绑定同一 Br、selection、target_ids、config、资产版本和固定种子策略。
- Agent 只能调整计划，不能放宽掩码、增加预算、改校验器、换目标版本或修改已有资产文件。
- 编译/几何问题可以反馈给 Agent 修计划；NBT 异常、before 异常、数据 hash 不一致等核心正确性故障先停止并报工程错误，不让 Agent“改需求避开”。
- 缺纹理、CDN/本地资源失效、截图失败、WebGL 不可用属于渲染/环境问题，不能靠删建筑修复。
- 相同错误指纹连续重复达到上限时停止；总工具调用、总时长与供应商重试都有上限。
- 用户取消后不再提交结果；并发调用晚返回也不能推进任务。
- 达到上限而未通过时保留完整诊断，当前场景仍为 Br，不生成可接受修订。

现有文件式 Agent 模式可以继续使用。没有配置真实 Agent runner 时显示 `WAITING_AGENT`，允许导出任务包并提交 plan/review 文件，但**不得把 mock 或等待状态标成自主 review 完成**。交付必须同时区分回放测试与至少一次实际 Agent 的完整运行记录。

---

## 17. 服务、CLI 与 Agent 接入契约

### 17.1 新增服务模块，不复制既有核心

```text
src/litegarden/
  io.py / scene.py / terrain.py / compiler.py / validate.py   # 保留并补缺口
  constraints.py         # MaskSet、WriteGuard、规则来源与 hash
  net_patch.py           # 阶段写日志、净变化、来源/冲突策略
  nbt_compare.py         # 类型敏感 NBT 比较与允许路径
  traversal.py           # 已验证通行 profile、入口、边界连接
  project_store.py       # 不可变修订、HEAD、锁、原子提交、恢复
  objects.py             # 对象来源、substrate、依赖与安全替换
  redesign.py            # 范围、Halo、任务基线、预处理、局部计划
  render_scene.py        # 已重读场景 → 只读 RenderScene
  review/
    capture.py           # Playwright 取证
    hard_checks.py       # 统一硬规则调度
    evidence.py          # hash、receipt、覆盖范围
    findings.py          # 模型发现校验与确定性核实
    runner.py            # 有界 review/repair 状态机
  agent_adapter.py       # 已有 Agent/文件协议/回放；明确能力和超时
  server/
    app.py               # 本地服务、静态页面
    routes.py            # 薄 API 层
    jobs.py              # 本地工作进程、真实状态、取消
web/
  index.html
  review-view.html
  app.js                 # UI 状态和 API 调用
  viewer_adapter.js
  selection.js
  diff_overlay.js
  review_bridge.js
  styles.css
  vendor/litematica-viewer/   # 固定上游派生代码与声明
  resources/                 # 本地模型、纹理、锁定 JS 库
third_party/viewer/
  UPSTREAM.md
  viewer.lock.json
  patches/
tests/
  unit/ integration/ browser/ fixtures/ e2e/
```

新模块可合并到现有对应文件，重要的是职责和测试隔离。上游全局脚本不直接改名成 ES module 就算迁移成功；先处理隐式全局变量与初始化依赖，再封装生命周期。

前端先用简单 JS/ES modules 和 CSS 即可；没有现成框架就不为这一轮引入多套 UI 框架。已存在成熟前端则适配现有构建，不强制重写。

### 17.2 HTTP 最小路由

下表路径为拟实现接口。revision/candidate/config 等权限相关值由服务端查询和验证，不信任客户端自报的“已验证”。

| 方法与路径 | 请求/返回要点 |
|---|---|
| `POST /api/projects` | 文件导入；返回 project_id、r000、场景摘要、规则版本 |
| `GET /api/projects/{p}` | 当前 HEAD、head_generation、规则、已登记对象 |
| `GET /api/projects/{p}/revisions/{r}/render-scene` | 完整或经验证 crop 的只读显示数据 |
| `POST /api/projects/{p}/selections/validate` | bounds、基线；返回规范选区、相交对象、保护重叠、边界接口 |
| `POST /api/projects/{p}/redesign-tasks` | 用户需求和已确认选择；返回 task_id 与冻结请求 |
| `GET /api/tasks/{t}` | 状态、当前 attempt、错误/进度，不伪造百分比 |
| `POST /api/tasks/{t}/cancel` | 撤销运行意图；晚到结果不得提交 |
| `POST /api/tasks/{t}/attempts` | Agent 提交符合协议的 plan；服务端分配 attempt/candidate |
| `GET /api/candidates/{c}/render-scene` | 候选显示输入；不可变 hash |
| `GET /api/candidates/{c}/diff` | 相对本次 Br 的精确净变化 |
| `POST /api/candidates/{c}/inspect` | 受限只读体素/对象/通行查询 |
| `POST /api/candidates/{c}/review` | 触发有界取证/核实；返回 job/status |
| `POST /api/candidates/{c}/accept` | 用户动作；携带 expected_head/generation/config；CAS 提交 |
| `POST /api/candidates/{c}/reject` | 只丢弃候选资格，不影响 Br |
| `POST /api/projects/{p}/undo`、`/redo` | 线性历史，校验 expected_head/generation |
| `POST /api/projects/{p}/exports` | 导出指定已接受修订；返回已完成产物或导出任务 ID |

接口层至少区分输入错误、范围/规则违例、409 陈旧/冲突、依赖/资源未支持及内部错误。返回结构化错误，不把 traceback、绝对私有路径或密钥传给浏览器。

### 17.3 CLI 扩展

以下是**目标命令**，完成实现前不宣称可直接运行：

```bash
# 保持旧 inspect/compile/export/pack 行为
python -m litegarden project init terrain.litematic --out projects/demo
python -m litegarden serve --project projects/demo --host 127.0.0.1

# 文件式 Agent 和自动化测试仍可用
python -m litegarden redesign prepare --project projects/demo --request request.json
python -m litegarden redesign compile --project projects/demo --task TASK_ID --plan plan.json
python -m litegarden review --project projects/demo --candidate CANDIDATE_ID
python -m litegarden project accept --project projects/demo --candidate CANDIDATE_ID --expected-head REV_ID
python -m litegarden project export --project projects/demo --revision REV_ID --out export/demo
```

CLI 与网页经过同一服务检查。`accept` 的正式权限只给用户控制侧，不能作为运行时 Agent 的工具。CLI 在受信任管理员环境下可用，并不意味着随意给模型宿主机写权限也能形成安全隔离。

### 17.4 真实 Agent 接入的验收

适配器至少有 `generate_plan(context, budget, cancel)` 与 `review_evidence(evidence, readonly_tools, budget, cancel)` 两个职责。可以由同一 Agent 在分离上下文中完成，不能把它们做成“生成完就回一句通过”的空调用。

保留文件式 runner 兼容现有流程；新增自动 runner 时实际调用一种已配置的 Agent 服务即可，不同时集成多个供应商。只有输入包而没有返回计划、取证、review、核实及有界反馈，不算自动闭环完成。

---

## 18. 输出、接受和最终导出

### 18.1 候选阶段

数据校验通过后可在 attempt 隔离目录生成 `candidate.litematic`，用于重读和渲染。它是待审查中间产物，不更新 `current`，不作为 `full.litematic` 提供正式导出。

编译、权限、净补丁或 NBT 验证失败时，不创建可导入的候选文件；可以保存错误报告和原场景错误标记。视觉审查失败时已生成的 candidate 可以保留取证，但状态必须不可接受。

### 18.2 导出阶段

```text
exports/<export_id>/
  full.litematic
  changes.litematic
  changes.json
  plan.json                     # 最后一次任务的计划；明示不是完整历史重放脚本
  revision_manifest.json
  revision_history.json          # 关联父修订及每次计划/补丁
  report.json
  report.md
  preview_before.png
  preview_after.png
  preview_changes.png
  review_summary.json
  validation_manifest.json
```

最终导出基于明确的已接受 revision，不默认取“最后一个生成的候选”。重放/物化该修订、核对源文件 hash、所有修订链 before/after、规则与 NBT，再生成并重读输出。旧的单次 `export --plan` 仍须重新编译；新项目导出不能把多个局部 plan 当一个全局 plan 重跑，也不能调用模型随机再设计一次。

输出 `changes.json` 是 B0 → 已接受结果的累计净变化，并记录基线。`changes.litematic` 仍只是 after 非空气的改动投影，不具备完整删除语义。原任务书对空气和 All 粘贴风险的说明继续保留。[U1]

所有 export 进入新目录；校验失败不出现新的 `full.litematic`。旧的成功导出文件可继续存在，但 UI 必须按 export_id、revision_id 和状态显示，不能让失败操作误展示旧文件为刚生成成功。

### 18.3 最终报告必须说清楚

报告分别列出：数据硬约束状态、渲染状态、Agent review 是否实际执行、发现及核实结果、未支持范围、是否完成游戏内人工验收。不要用一个无条件的“全部通过”隐藏这些差异。

---

## 19. 性能、资源与本地安全边界

### 19.1 性能范围

第一批仍以原任务书约 64×64 场地和小型资产为目标；记录具体 Y 高度、体素数、非空气数、修改数、资源版本和测试机器。对更大场景先测量，不宣称支持任意体积。

上游 `BLOCK_WARN_LIMIT=400_000` 只是提醒阈值，不是吞吐保证。[S3] 先保证全量数据校验；局部取证可以渲染 selection+halo 的 crop，但必须标注 origin 和覆盖范围，不得用裁剪隐藏风险。

测量 parse/analysis/compile/readback/render/capture 时间、峰值内存、场景切换和关闭后的资源回收。截图采用受控视角和明确加载完成协议。跨 GPU 的截图不要求逐像素完全相同；用坐标标记、已知状态样例、结构数据和容差受控的图像检查共同验收。

若性能不足，优先减少同时持有的 renderer、缓存固定资源、只重建当前显示场景；不得把内部方块删掉再冒充完整导出数据。

### 19.2 最小防护

服务默认绑定 loopback，不监听公网。浏览器操作与 Agent 工具分离权限：Agent 不可调用接受、规则修改、导出任意路径或删除项目接口。所有写请求验证本地会话/来源；不能认为 127.0.0.1 就自动免于跨站请求。

限制文件大小、解压后体积、NBT 深度/列表长度、尺寸乘积、任务数、体素查询量、模型调用次数、截图请求数和总时长。防止巨型压缩结构拖垮进程。

文件名、蓝图描述、用户指令和模型文本不进入 eval、exec、shell 拼接、innerHTML 或未过滤模板。自动化 page.evaluate 仅执行开发者固定的可信脚本，参数通过结构化对象传入，不能执行 Agent 提供的 JavaScript。[S12]

源文件与规则只读、输出目录隔离、每次使用前校验 hash；生产运行时 Agent 仅获得限定工具/工作目录。不能同时授予 Agent 任意宿主机源码和配置写权限，又声称只有 plan 协议能约束它。

外部模型调用需要用户配置；清楚说明将发送截图、任务文字及局部摘要，不默认发送完整蓝图或无关项目文件。密钥留在后端，不放前端静态包、plan 或日志。

---

## 20. 分阶段开发任务与阻断门槛

| 阶段 | 修改重点 | 可演示产物 | 必须满足的门槛 |
|---|---|---|---|
| P0-A 现状冻结 | 定位私有代码、运行现有测试、收集真实样例、锁定 Viewer SHA/资产 | baseline 报告、映射表、依赖锁 | 原测试结果可复现；本次代码位置不凭任务书猜测 |
| P0-B 安全补齐 | WriteGuard、净归并、NBT 比较、raw NBT 输入门禁 | 正/反例测试、修复报告 | 非法写入/基线/NBT 变更全部阻断 |
| P1 通行补齐 | entry、headroom、台阶、真实挖填、最终复检 | 固定 plan 的可走路径与失败夹具 | 已知通行 profile 内规则完整执行 |
| P2 Viewer 集成 | 锁定资源、RenderScene、桥接、相机、切片、诊断、只读浏览 | 同源网页准确显示原场景和固定候选 | 负 Size/offset/crop 校准通过，缺资源不静默 |
| P3 交互修订 | 选区、Halo、对象来源、局部准备、Candidate/Revision、Undo | **先用手写 plan** 演示选区替换、拒绝、接受、撤销 | 选区外不变；陈旧候选不能覆盖当前；没有 Agent 也可测试 |
| P4 自动取证 | 复用 Viewer 的 Playwright 驱动、manifest、覆盖报告 | 同一候选多视角、切层、问题定位 | 截图与文件 hash 一致，旧图/空白失败被拦截 |
| P5 Agent 硬自检 | 真实 runner、问题协议、只读查询、有界修复、取消 | 人为注入缺陷→发现/核实/修复或安全失败 | 审美理由不触发修复；核心故障不靠模型绕过 |
| P6 游戏内验收 | 世界副本、placement、真实行走/状态/数据确认 | 平地、缓坡、湖岸验收记录 | 未完成时不能关闭该阶段 |

允许并行做只读 Viewer 技术验证，但 P0/P1 未完成之前，不开放正式“接受新方案”。不能为了优先看到 UI 效果，把已知缺口延期到发布之后。

建议每阶段形成可独立回滚的提交，不把修复保护区、换文件格式库和开发整个 UI 混成一个提交。

---

## 21. 验收测试矩阵

下列为新增验收要求，**不是本次已执行测试数**。每个失败夹具都检查：源文件未变、HEAD 未变、未生成可接受修订、错误可定位。

### A. 权限与掩码

| ID | 用例 | 预期 |
|---|---|---|
| A01 | 道路只越过一格保护区；资产本体没越界但地基越界 | 全候选拒绝；返回真实写入位置 |
| A02 | 地表在选区内，清净空写入高于选区 ymax | 拒绝，Y 范围不能被忽略 |
| A03 | 先写保护区再恢复原样，最终净变化为零 | 仍在首次真实写入处拒绝 |
| A04 | 已知空气列 ground=-1；未知方块 ID 能读取 | 前者不误当未知体素；后者不误当安全可编辑 |
| A05 | 用 CLI/API 绕过 UI 或篡改 plan 的约束字段 | 与 UI 同样拒绝 |
| A06 | 保护区与 editable 重叠；候选生成后保护配置变更 | 保护优先；旧候选失效 |

### B. 净补丁与来源

| ID | 用例 | 预期 |
|---|---|---|
| B01 | dirt→stone→moss，多个 op 写同一点 | 最终 before=dirt，after=moss，来源完整 |
| B02 | dirt→stone→dirt；air→stone→air | 净补丁无该点，内部日志与预算仍有记录 |
| B03 | 后 op 的 stage_before 错误 | 立即拒绝，不靠最终覆盖掩盖 |
| B04 | same ID 不同 facing/waterlogged 属性 | 识别为真实状态变化 |
| B05 | 第二次局部修订应用到已接受方案 | before 对应 Br，而不是 B0 |
| B06 | 同一输入/配置/资产/计划/seed 运行两次 | 规范化补丁与场景一致；不要求压缩时间戳一致 |

### C. NBT 与输入

| ID | 用例 | 预期 |
|---|---|---|
| C01 | 实体数据数值相同但 Short 变 Int | 类型比较失败 |
| C02 | 删扩展字段、改 List 类型/顺序、改变数组 | 非白名单路径失败 |
| C03 | 箱子 NBT 保留，但宿主方块被替换 | 宿主依赖校验失败 |
| C04 | 负 Size、非零 Position、已知不同属性状态 | 坐标和属性原样保真 |
| C05 | 多 Region、错误 Regions 类型或不支持布局 | 解码/施工前明确拒绝 |
| C06 | 游戏版本不在编辑白名单，但浏览资源存在 | 不能以“能画出来”绕过编辑版本门槛 |

### D. 通行、支撑与边界

| ID | 用例 | 预期 |
|---|---|---|
| D01 | 中心线通过但路边碰撞，头部空间不足 | 拒绝；指出侧边/上方具体体素 |
| D02 | 连续一格台差但没有合法步行过渡 | no-jump profile 不得通过 |
| D03 | 亭内 required_empty 全空，门外却被灯柱堵住 | 入口最终复检失败 |
| D04 | 铺路时通过，scatter 后堵路 | 最终候选检查失败 |
| D05 | 选区边界道路看似相邻但高程/宽度不匹配 | 接口校验失败 |
| D06 | A* 低成本路线需要超预算挖填 | 硬预算仍拒绝；估计和实际量分别报告 |

### E. 局部替换与历史

| ID | 用例 | 预期 |
|---|---|---|
| E01 | 用手写 plan 重设计 16×16 局部 | 授权区外所有方块状态不变 |
| E02 | 框到半个已知资产 | 保留或要求用户扩大；不后台扩大 |
| E03 | 替换已知亭子并移除旧地基 | 安全恢复记录的 substrate，再建设新对象 |
| E04 | 旧生成区域被后续对象引用/覆盖 | 依赖冲突，不能直接 inverse patch |
| E05 | 输入建筑没有生成来源 | 默认保护/增量设计，不清空后当成功重设计 |
| E06 | 候选拒绝、取消、接受、最近一次 Undo/Redo | 生命周期和 HEAD 正确；每步 generation 递增 |
| E07 | 用户已接受其他方案，旧候选晚返回 | STALE，不能覆盖；Undo 回同 ID 也不绕过 generation |
| E08 | 提交途中崩溃或输出磁盘错误 | 原 HEAD 可恢复；不引用半成品修订 |

### F. Viewer 与坐标

| ID | 用例 | 预期 |
|---|---|---|
| F01 | 不对称角点标记 + 负 Size + offset + crop | 后端/显示/点选坐标一致，无镜像或平移 |
| F02 | CSS 缩放、DPR 变化、窗口收缩 | 拾取与选框仍准确 |
| F03 | 输入框打 WASD/空格，切换导航/选择 | 不误动相机、不误框选 |
| F04 | invalid palette index、缺模型、缺纹理 | 显式失败或诊断代理，不静默少块 |
| F05 | 合法空场景与资源失败空白画面 | 正确区分 |
| F06 | 旧 scene 加载晚返回、新旧 candidate 快速切换 | 旧数据不覆盖新场景 |
| F07 | 多次切场景、切层、关闭重开 | 资源与监听器不无界累积 |
| F08 | 删除/状态变更 Diff 与后端 changes 对账 | 数量/坐标一致，ghost 不写入蓝图 |

### G. Agent review 与取证

| ID | 用例 | 预期 |
|---|---|---|
| G01 | 注入入口堵塞、断路、资产错位等受支持缺陷 | 数据检查或视觉疑点+核实捕获；有坐标和规则证据 |
| G02 | 遮挡或渲染缺模型造成“看不见” | 先查数据，不能删正确方块来修画面 |
| G03 | 提供“颜色不协调/不自然”的模型评价 | 不纳入硬错误、不自动触发重建 |
| G04 | 截图 hash 属于旧 candidate；slice 残留隐藏关键区域 | 证据失效，不能标视觉复核完成 |
| G05 | 模型给出虚构 image_id/object_id/越界坐标 | schema/引用检查失败 |
| G06 | WebGL 不可用、page 崩溃、资源超时 | RENDER_UNAVAILABLE/FAILED，保留原方案 |
| G07 | 模型连续生成同错误或供应商连续超时 | 在配置预算内停止，当前方案不变 |
| G08 | 编译/NBT 失败但模型声称“通过” | 数据门禁保持失败 |
| G09 | 只接 mock 或只产输入包，无真实 review 调用 | 标记为测试/等待，不宣称自主闭环完成 |

### H. 导出与游戏内验收

| ID | 用例 | 预期 |
|---|---|---|
| H01 | 导出已接受 revision，而非最新候选 | 文件、manifest、累计净补丁对应正确 revision |
| H02 | 失败导出旁存在旧成功 full.litematic | UI 不误报旧文件为本次成功 |
| H03 | 保存后重读，比较方块、属性、Position/Size、NBT | 全量符合允许变更，不仅总数相同 |
| H04 | 平地、缓坡、湖岸三类世界副本使用相同 placement | 位置、状态、入口、道路和拆除行为人工确认 |
| H05 | 边界/台阶/半砖/装饰支撑在目标版本客户端观察 | 与声明 profile 一致；差异登记并补夹具 |

---

## 22. 交付清单与完成定义

编码 Agent 应交付：可运行源码和依赖锁、上游复制清单和许可证记录、现有测试回归结果、新增反例夹具、API/CLI 使用说明、任务/候选/修订样例、一组真实 Agent 运行与 review 证据、未支持能力清单，以及游戏内验收记录或明确的待验收状态。

以下全部成立才可称为 v0.2 软件闭环完成：

- 用户可在真实 Viewer 中选择区域并发起局部任务，不靠手工输入全部坐标。
- 保护区和选区外强制不可写；净补丁 before 与 Br 一致；NBT 类型与数据依赖保持安全。
- 候选不直接覆盖当前版本；接受、拒绝、最近一次撤销及陈旧任务检测可靠。
- 真实候选文件被重读后交给同一 Viewer，截图与 hash/坐标可追溯。
- Agent 实际完成硬错误 review，疑点经工具核实，有界修复；没有审美评分旁路。
- 源文件永不覆盖，最终导出可重新读取并对账。

游戏内阶段 D 仍单独报告：在世界副本确认之前，最多称“软件自动化闭环通过”，不能声称 Minecraft 全链路和真实通行已最终验收。

---

## 23. 给 coding agent 的执行摘要

> 在现有工程增量开发，先运行并记录已有测试，确认用户报告的缺口。不要重写已验证的 Litematic I/O。第一批修复 WriteGuard、显式掩码语义、按事务基线归并净变化、NBT 类型语义与宿主依赖检查；第二批补道路/入口净空、阶梯与挖填。锁定 albertchen857/Litematica-viewer 的实际提交，仅复用其 JSrender 和必要资源，避免上游 formats.py 接管导出。新建本地浏览器工作台，替换共享 payload.js 为不可变 RenderScene，补相机、坐标选择、Diff、诊断和渲染完成协议。先用手写 plan 跑通当前版本 Br → 局部候选 → 接受/拒绝/Undo，保持 Halo 只读且选区外不变。然后用同一 Viewer 的 Playwright 自动化生成绑定候选 hash 的证据包，接入真实 Agent 做硬错误自检和受限查询；最多首次生成加两次修复，核心数据故障停止，审美评价不触发重做。所有阶段按测试矩阵留证据，未完成游戏内验收就明确标记待验收。不得用 mock、总方块数相同或“截图看着正常”代替实际正确性证明。

---

## 24. 来源与复核索引

引用仅支持上游能力/现状和工具能力；本文其余架构、字段、预算、测试和门槛是本项目的新设计要求，不是声称上游已经实现。

**[U1] 前置任务书与用户进度核查**

- 本对话已有文件：`MC_Litematic_MVP_工程任务书.md`，2026-09-23。
- 本轮用户提供的“对照工程任务书的实现核查”。其中完成情况没有经过本次独立运行核验。

**[S1] 指定仓库 README**

https://github.com/albertchen857/Litematica-viewer

核对：桌面应用定位、运行入口、单独 3D 窗口、目录布局。

**[S2] 架构与渲染交付**

https://raw.githubusercontent.com/albertchen857/Litematica-viewer/main/docs/ARCHITECTURE.md

核对：`lv` 分层、pywebview 子进程、payload → bridge → Deepslate 链路。文档中的性能自述不作为本项目已实测结论。

**[S3] Python 渲染桥**

https://raw.githubusercontent.com/albertchen857/Litematica-viewer/main/script/lv/render.py

核对：`build_payload / write_payload / open_viewer`、数组布局、固定 payload 路径与提醒阈值。

**[S4] JS 桥接**

https://raw.githubusercontent.com/albertchen857/Litematica-viewer/main/script/JSrender/src/bridge.js

核对：`lvBuildStructure / lvBuildSliders / lvStart`、稀疏索引还原、无效 palette 跳过、HUD 和资源就绪。

**[S5] 相机与画布**

https://raw.githubusercontent.com/albertchen857/Litematica-viewer/main/script/JSrender/src/viewer.js

核对：`createRenderCanvas / setStructure / render`、全局状态、视图矩阵、输入和画布布局。

**[S6] 渲染资源帮助函数**

https://raw.githubusercontent.com/albertchen857/Litematica-viewer/main/script/JSrender/src/deepslate-helpers.js

核对：`loadDeepslateResources`、模型与纹理资源、属性元数据接口。

**[S7] Viewer 页面与依赖**

https://raw.githubusercontent.com/albertchen857/Litematica-viewer/main/script/JSrender/viewer.html

核对：Deepslate 0.10.1、gl-matrix 3.4.3 的页面引用及加载顺序。这是所读页面引用，不是对最新版本的判断。

**[S8] 桌面 WebView 子进程**

https://raw.githubusercontent.com/albertchen857/Litematica-viewer/main/script/webview_host.py

核对：独立窗口、pywebview 和本地 HTTP 承载方式。

**[S9] 上游格式读写**

https://raw.githubusercontent.com/albertchen857/Litematica-viewer/main/script/lv/formats.py

核对：`_load_litematic / _save_litematic`、统一模型、重新构造式写出及实体异常处理。本文不将其作为本项目的无损保存链路。

**[S10] 上游根目录许可证**

https://raw.githubusercontent.com/albertchen857/Litematica-viewer/main/LICENSE

核对：MIT 代码许可声明；素材和上游派生代码来源仍应单独登记核查。

**[S11] 上游自检脚本**

https://raw.githubusercontent.com/albertchen857/Litematica-viewer/main/tools/selftest.py

核对：roundtrip 的 `same_total` 检查，不替代本项目逐格/状态/类型验收。

**[S12] Playwright 官方文档**

https://playwright.dev/python/docs/screenshots

https://playwright.dev/python/docs/evaluating

核对：页面/元素截图和在页面上下文求值。渲染就绪、证据 hash 与诊断协议由本项目新增。

**[S13] FastAPI 官方文档**

https://fastapi.tiangolo.com/tutorial/static-files/

核对：在同一应用中提供静态文件。项目 API、鉴权、任务生命周期和本地存储由本项目实现。

所有公开代码路径的读取日期均为 2026-09-23，指向当时可访问的 main；开工后必须把以上索引补充为实际 commit permalink 和本地文件 hash，再作为可重复构建依据。
