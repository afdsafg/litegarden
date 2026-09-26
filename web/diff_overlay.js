// ===========================================================================
// web/diff_overlay.js — 净变化的叠加显示（纯数据分类 + 2D 投影绘制）
// ===========================================================================
//
// 职责边界
// --------
// * 本文件**不接触 WebGL**、不接触蓝图：它把后端给的净变化数据分类成四类几何盒，
//   并把它们画到适配层的 2D 叠加 canvas 上（投影函数由调用方提供）。
// * 删除用 ghost（虚线 + 底面标记）显示，**绝不往蓝图写入标记方块**。
//
// 坐标与单位
// ----------
//   payload.net_changes[].pos_local 是 project_local 整数格（半开区间 [min, max_exclusive) 语义）
//   本文件输出的 boxes 用 **crop 局部**（p_render）整数格 + 单位立方体 [min, min+1]；
//   转换 p_render = p_local - crop_origin_local 由 buildDiffBoxes() 统一完成。
//
// 输入载荷（后端 GET /api/candidates/{c}/diff 的形状；字段名与契约一致）
// ---------------------------------------------------------------------
// {
//   "schema_version": "0.2",
//   "scene_hash": "<候选场景语义 hash>",
//   "base_revision": "r001",
//   "counts": {"added": 12, "removed": 3, "replaced": 5, "state_only": 2},   // 可选
//   "net_changes": [
//     {"pos_local": [12, 4, 30],
//      "before": {"name": "minecraft:air", "props": {}},   // 或 null
//      "after":  {"name": "minecraft:stone", "props": {}},
//      "category": "added"}                                 // 可选，缺失时按 before/after 推断
//   ]
// }
// 净变化是**按原始基线归并后的每坐标净结果**（不是写日志）：同一坐标不会出现两条。
// ===========================================================================

/** 四类净变化。文案与颜色是本工作台的固定语义，不要拿颜色单独承载信息。 */
export const CATEGORIES = Object.freeze({
  ADDED: "added",
  REMOVED: "removed",
  REPLACED: "replaced",
  STATE_ONLY: "state_only",
});

export const CATEGORY_ORDER = Object.freeze([CATEGORIES.ADDED, CATEGORIES.REMOVED, CATEGORIES.REPLACED, CATEGORIES.STATE_ONLY]);

/**
 * 每类的视觉样式：颜色 + 线型（线型让"删除"在黑白打印/色盲下也能区分）。
 * dash 为空数组表示实线。
 */
export const CATEGORY_STYLE = Object.freeze({
  [CATEGORIES.ADDED]: { color: "#5ad1a8", dash: [], label: "新增", pattern: "实线", ghost: false },
  [CATEGORIES.REMOVED]: { color: "#e2726e", dash: [4, 3], label: "删除（ghost）", pattern: "虚线", ghost: true },
  [CATEGORIES.REPLACED]: { color: "#e0b45e", dash: [7, 3, 2, 3], label: "替换", pattern: "点划线", ghost: false },
  [CATEGORIES.STATE_ONLY]: { color: "#9b8cf0", dash: [2, 4], label: "纯状态变化", pattern: "细虚线", ghost: false },
});

const AIR_NAMES = new Set(["minecraft:air", "minecraft:cave_air", "minecraft:void_air"]);

function isInt(n) {
  return typeof n === "number" && Number.isInteger(n);
}

function isTriple(v) {
  return Array.isArray(v) && v.length === 3 && v.every(isInt);
}

function isAirState(state) {
  if (!state) return true;
  if (typeof state !== "object") return true;
  return AIR_NAMES.has(String(state.name));
}

function stateKey(state) {
  if (!state || typeof state !== "object") return "";
  const props = state.props && typeof state.props === "object" ? state.props : {};
  return (
    String(state.name || "") +
    "|" +
    Object.keys(props)
      .sort()
      .map((k) => `${k}=${props[k]}`)
      .join(",")
  );
}

/** 依据 before/after 推断类别（后端未给 category 时的确定性回退）。 */
export function classifyChange(before, after) {
  const bAir = isAirState(before);
  const aAir = isAirState(after);
  if (bAir && aAir) return { category: null, reason: "before/after 都是空气：不是净变化" };
  if (bAir && !aAir) return { category: CATEGORIES.ADDED, reason: "空气 → 方块" };
  if (!bAir && aAir) return { category: CATEGORIES.REMOVED, reason: "方块 → 空气（拆除）" };
  const sameName = String(before.name) === String(after.name);
  if (!sameName) return { category: CATEGORIES.REPLACED, reason: "方块名不同" };
  if (stateKey(before) !== stateKey(after)) return { category: CATEGORIES.STATE_ONLY, reason: "方块名相同、BlockState 属性不同" };
  return { category: null, reason: "before 与 after 完全一致：不是净变化" };
}

/**
 * 校验并归一化净变化载荷。错误与警告都返回，不抛错、不静默丢数据。
 */
export function normalizeDiff(payload) {
  const errors = [];
  const warnings = [];
  const categories = {
    [CATEGORIES.ADDED]: [],
    [CATEGORIES.REMOVED]: [],
    [CATEGORIES.REPLACED]: [],
    [CATEGORIES.STATE_ONLY]: [],
  };
  const out = {
    ok: false,
    errors,
    warnings,
    categories,
    counts: {
      [CATEGORIES.ADDED]: 0,
      [CATEGORIES.REMOVED]: 0,
      [CATEGORIES.REPLACED]: 0,
      [CATEGORIES.STATE_ONLY]: 0,
    },
    declared_counts: null,
    counts_mismatch: false,
    scene_hash: null,
    base_revision: null,
    coordinate_space: "project_local",
    total: 0,
    ignored: 0,
  };

  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    errors.push("Diff 载荷必须是 JSON 对象");
    return out;
  }
  out.scene_hash = typeof payload.scene_hash === "string" ? payload.scene_hash : null;
  out.base_revision = typeof payload.base_revision === "string" ? payload.base_revision : null;
  if (payload.counts && typeof payload.counts === "object") out.declared_counts = { ...payload.counts };

  const changes = payload.net_changes ?? payload.changes;
  if (!Array.isArray(changes)) {
    errors.push("net_changes 必须是数组（后端精确净变化）");
    return out;
  }

  for (let i = 0; i < changes.length; i++) {
    const change = changes[i];
    if (!change || typeof change !== "object") {
      errors.push(`net_changes[${i}] 不是对象`);
      continue;
    }
    if (!isTriple(change.pos_local)) {
      errors.push(`net_changes[${i}].pos_local 必须是 3 个整数（project_local）`);
      continue;
    }
    const before = change.before ?? null;
    const after = change.after ?? null;
    for (const [key, value] of [
      ["before", before],
      ["after", after],
    ]) {
      if (value != null && (typeof value !== "object" || typeof value.name !== "string")) {
        errors.push(`net_changes[${i}].${key} 必须是 null 或 {name, props}`);
      }
    }
    const derived = classifyChange(before, after);
    const declared = change.category;
    let category = declared;
    if (declared != null && !CATEGORY_ORDER.includes(declared)) {
      warnings.push(`net_changes[${i}].category=${JSON.stringify(declared)} 不是已知类别，按 before/after 推断`);
      category = derived.category;
    }
    if (category == null) category = derived.category;
    if (category == null) {
      out.ignored++;
      warnings.push(`net_changes[${i}] ${derived.reason}`);
      continue;
    }
    const entry = {
      index: i,
      pos_local: [change.pos_local[0], change.pos_local[1], change.pos_local[2]],
      before: before ? { name: String(before.name), props: before.props && typeof before.props === "object" ? { ...before.props } : {} } : null,
      after: after ? { name: String(after.name), props: after.props && typeof after.props === "object" ? { ...after.props } : {} } : null,
      category,
      category_source: declared === category ? "payload" : "derived",
      op_id: typeof change.op_id === "string" ? change.op_id : null,
      reason: typeof change.reason === "string" ? change.reason : derived.reason,
    };
    categories[category].push(entry);
    out.counts[category]++;
    out.total++;
  }

  if (out.declared_counts) {
    for (const key of CATEGORY_ORDER) {
      const declared = out.declared_counts[key];
      if (isInt(declared) && declared !== out.counts[key]) {
        out.counts_mismatch = true;
        warnings.push(`counts.${key}=${declared} 与实际解析出的 ${out.counts[key]} 不一致（以实际数据为准）`);
      }
    }
  }

  out.ok = errors.length === 0;
  return out;
}

/**
 * 把归一化后的净变化转成可绘制的盒子（crop 局部整数格）。
 * * 先按 (x,z) 列把 y 方向相邻的同类体素合并成一段（run），显著减少盒子数；
 * * 超过 maxBoxes 时降级为「按列」再降级为「每类一个包围盒」，并如实报告聚合方式。
 */
export function buildDiffBoxes(normalized, options = {}) {
  const size = options.size;
  const origin = options.crop_origin_local || [0, 0, 0];
  const maxBoxes = Number.isFinite(options.maxBoxes) ? options.maxBoxes : 1200;
  const result = {
    boxes: [],
    drawn: 0,
    aggregated: false,
    aggregation: "none",
    truncated: false,
    outside_crop: 0,
    totals: { ...normalized.counts },
    coordinate_space: "crop_local（p_render）",
    voxel_unit: "1 单位 = 1 方块",
  };
  if (!size || !normalized || !normalized.ok) return result;
  const [sx, sy, sz] = size;

  const outside = [];
  // 逐类逐列收集 y 值
  const perCategory = new Map();
  for (const category of CATEGORY_ORDER) {
    perCategory.set(category, new Map()); // key = "x,z" -> {x,z,ys:Set,count}
  }

  for (const category of CATEGORY_ORDER) {
    for (const change of normalized.categories[category]) {
      const px = change.pos_local[0] - origin[0];
      const py = change.pos_local[1] - origin[1];
      const pz = change.pos_local[2] - origin[2];
      if (px < 0 || py < 0 || pz < 0 || px >= sx || py >= sy || pz >= sz) {
        result.outside_crop++;
        if (outside.length < 50) outside.push({ category, pos_local: change.pos_local, pos_render: [px, py, pz] });
        continue;
      }
      const key = `${px},${pz}`;
      const bucket = perCategory.get(category);
      let column = bucket.get(key);
      if (!column) {
        column = { x: px, z: pz, ys: [], count: 0 };
        bucket.set(key, column);
      }
      column.ys.push(py);
      column.count++;
    }
  }
  result.outside_crop_samples = outside;

  // 第一级：按列合并 y 连续段
  const runs = [];
  let sumRuns = 0;
  for (const category of CATEGORY_ORDER) {
    for (const column of perCategory.get(category).values()) {
      const ys = column.ys.slice().sort((a, b) => a - b);
      let start = ys[0];
      let prev = ys[0];
      let count = 1;
      for (let i = 1; i < ys.length; i++) {
        if (ys[i] === prev + 1) {
          prev = ys[i];
          count++;
          continue;
        }
        runs.push({ category, min: [column.x, start, column.z], max_exclusive: [column.x + 1, prev + 1, column.z + 1], voxels: count });
        sumRuns += count;
        start = ys[i];
        prev = ys[i];
        count = 1;
      }
      runs.push({ category, min: [column.x, start, column.z], max_exclusive: [column.x + 1, prev + 1, column.z + 1], voxels: count });
      sumRuns += count;
    }
  }
  result.merged_run_voxels = sumRuns;

  if (runs.length <= maxBoxes) {
    result.boxes = runs;
    result.drawn = runs.length;
    result.aggregation = "y_runs";
    return result;
  }

  // 第二级：每列一个盒子（跨该列所有 y）
  const columnBoxes = [];
  for (const category of CATEGORY_ORDER) {
    for (const column of perCategory.get(category).values()) {
      const ys = column.ys.slice().sort((a, b) => a - b);
      columnBoxes.push({
        category,
        min: [column.x, ys[0], column.z],
        max_exclusive: [column.x + 1, ys[ys.length - 1] + 1, column.z + 1],
        voxels: column.count,
        aggregated: true,
      });
    }
  }
  if (columnBoxes.length <= maxBoxes) {
    result.boxes = columnBoxes;
    result.drawn = columnBoxes.length;
    result.aggregated = true;
    result.aggregation = "by_column";
    result.truncated = false;
    return result;
  }

  // 第三级：每类一个包围盒
  const aabbs = [];
  for (const category of CATEGORY_ORDER) {
    const list = normalized.categories[category];
    if (!list.length) continue;
    let minX = Infinity;
    let minY = Infinity;
    let minZ = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    let maxZ = -Infinity;
    for (const change of list) {
      const px = change.pos_local[0] - origin[0];
      const py = change.pos_local[1] - origin[1];
      const pz = change.pos_local[2] - origin[2];
      if (px < 0 || py < 0 || pz < 0 || px >= sx || py >= sy || pz >= sz) continue;
      minX = Math.min(minX, px);
      minY = Math.min(minY, py);
      minZ = Math.min(minZ, pz);
      maxX = Math.max(maxX, px);
      maxY = Math.max(maxY, py);
      maxZ = Math.max(maxZ, pz);
    }
    if (minX !== Infinity) {
      aabbs.push({ category, min: [minX, minY, minZ], max_exclusive: [maxX + 1, maxY + 1, maxZ + 1], voxels: list.length, aggregated: true });
    }
  }
  result.boxes = aabbs;
  result.drawn = aabbs.length;
  result.aggregated = true;
  result.aggregation = "aabb_per_category";
  result.truncated = true;
  return result;
}

/**
 * 把盒子画到 2D 叠加 canvas。`project(pRender)` 由适配层提供，
 * 返回 {x, y, z(NDC 深度), visible}；本函数不做任何矩阵运算。
 */
export function drawDiffBoxes(ctx, options) {
  const boxes = options.boxes || [];
  const project = options.project;
  const fade = options.fade !== false;
  const visibleSet = options.visible || null;
  let segments = 0;
  if (!project || !boxes.length) return { segments };

  // 先画 ghost（删除）再画其它，避免被压住
  const ordered = boxes
    .slice()
    .sort((a, b) => (CATEGORY_STYLE[a.category]?.ghost ? -1 : 0) - (CATEGORY_STYLE[b.category]?.ghost ? -1 : 0));

  for (const box of ordered) {
    if (visibleSet && !visibleSet.has(box.category)) continue;
    const style = CATEGORY_STYLE[box.category] || CATEGORY_STYLE[CATEGORIES.ADDED];
    const edges = boxEdges(box.min, box.max_exclusive);
    ctx.strokeStyle = style.color;
    ctx.setLineDash(style.dash);
    ctx.lineWidth = box.aggregated ? 1.75 : 1.25;
    for (const [a, b] of edges) {
      const pa = project(a);
      const pb = project(b);
      if (!pa.visible && !pb.visible) continue;
      const depth = Math.min(pa.z, pb.z);
      const alpha = fade ? Math.max(0.22, Math.min(1, 1.1 - Math.max(-1, depth) * 0.42)) : 1;
      ctx.globalAlpha = style.ghost ? alpha * 0.85 : alpha;
      ctx.beginPath();
      ctx.moveTo(pa.x, pa.y);
      ctx.lineTo(pb.x, pb.y);
      ctx.stroke();
      segments++;
    }
    if (style.ghost) {
      // ghost 语义：底面加两条对角线，表示"这里曾经有方块，现在没有"
      const y = box.min[1];
      const c0 = [box.min[0], y, box.min[2]];
      const c1 = [box.max_exclusive[0], y, box.max_exclusive[2]];
      const c2 = [box.max_exclusive[0], y, box.min[2]];
      const c3 = [box.min[0], y, box.max_exclusive[2]];
      ctx.globalAlpha = 0.5;
      ctx.setLineDash([2, 3]);
      for (const [a, b] of [
        [c0, c1],
        [c2, c3],
      ]) {
        const pa = project(a);
        const pb = project(b);
        if (!pa.visible || !pb.visible) continue;
        ctx.beginPath();
        ctx.moveTo(pa.x, pa.y);
        ctx.lineTo(pb.x, pb.y);
        ctx.stroke();
        segments++;
      }
    }
  }
  ctx.globalAlpha = 1;
  ctx.setLineDash([]);
  return { segments };
}

/** 图例数据（DOM 用 textContent 渲染；颜色只作为辅助，文案与线型才是主信息）。 */
export function describeDiff(normalized, boxResult) {
  const rows = CATEGORY_ORDER.map((category) => ({
    category,
    label: CATEGORY_STYLE[category].label,
    pattern: CATEGORY_STYLE[category].pattern,
    color: CATEGORY_STYLE[category].color,
    count: normalized && normalized.counts ? normalized.counts[category] : 0,
  }));
  return {
    rows,
    total: normalized ? normalized.total : 0,
    ignored: normalized ? normalized.ignored : 0,
    errors: normalized ? normalized.errors : [],
    warnings: normalized ? normalized.warnings : [],
    scene_hash: normalized ? normalized.scene_hash : null,
    base_revision: normalized ? normalized.base_revision : null,
    counts_mismatch: normalized ? normalized.counts_mismatch : false,
    drawn_boxes: boxResult ? boxResult.drawn : 0,
    aggregation: boxResult ? boxResult.aggregation : "none",
    aggregated: boxResult ? boxResult.aggregated : false,
    outside_crop: boxResult ? boxResult.outside_crop : 0,
    note: "叠加只存在于视图：删除显示为 ghost，不向蓝图写入任何标记方块；叠加层不参与深度测试",
  };
}

function boxEdges(min, max) {
  const [x0, y0, z0] = min;
  const [x1, y1, z1] = max;
  const c = [
    [x0, y0, z0],
    [x1, y0, z0],
    [x1, y0, z1],
    [x0, y0, z1],
    [x0, y1, z0],
    [x1, y1, z0],
    [x1, y1, z1],
    [x0, y1, z1],
  ];
  return [
    [c[0], c[1]], [c[1], c[2]], [c[2], c[3]], [c[3], c[0]],
    [c[4], c[5]], [c[5], c[6]], [c[6], c[7]], [c[7], c[4]],
    [c[0], c[4]], [c[1], c[5]], [c[2], c[6]], [c[3], c[7]],
  ];
}
