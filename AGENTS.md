# AGENTS.md — litegarden 工程约定

本工程实现《MC_Litematic_MVP_工程任务书》。以下为开发约定：

## 坐标约定（最高优先级）
- `p_region`：litemapy Region 自身坐标，允许负数。
- `p_schematic = region.Position + p_region`。
- `p_local = p_schematic - enclosing_min`（enclosing_min 为全场景 schematic 坐标最小值）。
- Agent/Plan 只接触 `p_local`；导出时逆变换写回原 Region。
- `region.block_positions()` 返回 Region 自身坐标，不能当从零开始的数组下标。
- 禁止用 `min_schem + region_local` 代替 `region.x/y/z + region_local`（负 Size 区域会产生多余偏移）。
- MVP 不提供旋转/镜像；输出保持输入的 Region.Position、Size 与 schematic 坐标语义。

## 原始数据保留
- 维护两份表示：原始 NBT 树用于保存，litemapy 对象用于操作方块。
- 保存方式：复制原始 NBT 树，在被编辑 Region 中替换重新编码的 BlockStatePalette/BlockStates；仅更新白名单元数据字段。Position、Size、MinecraftDataVersion 不变。
- 未知版本/未支持 NBT 结构 → 停止而非猜测。
- 禁止"新建空 Schematic 只拷贝非空气方块"来代表保留原地形。

## 空气语义
- PatchSet 中：无某坐标 = 不修改；after 为 `minecraft:air` = 明确拆除。严格分离。
- 每条改动记录：region_id、pos_local、before、after、op_id。提交时 before 必须匹配原始基线。
- 编译器可维护阶段工作视图，最终输出按原始基线归并每个坐标的净变化。

## 多 Region
- 支持一个 Region；多 Region 输入明确报错，不静默丢弃或合并。

## 验收顺序
A. 零改动往返 → B. 确定性施工 → C. 场地分析 + Agent → D. 输出与人工游戏验收。
