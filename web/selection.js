// ===========================================================================
// web/selection.js — 俯视矩形选择（明确的顶视投影）+ 授权 Y 范围控件
// ===========================================================================
//
// 为什么要有俯视图
// ----------------
// 3D 视角下框选的 x/z 取决于相机朝向，难以复核。本模块提供一张**明确的顶视投影**
// （p_render 的 x 向右、z 向下；画面范围 = 当前渲染 crop），在其上拖选 x/z 矩形，
// 是确定性、可复核的基础选择方式；3D 视口里的"水平工作平面框选"与"3D 拾取"是补充。
//
// 坐标系与单位（与 viewer_adapter 一致）
// ------------------------------------
//   * 俯视图的每个像素格 = 一个 **crop 局部**体素格（p_render，整数格，1 格 = 1 方块）；
//   * 对外的选区一律是 **project_local** 的半开区间 [min, max_exclusive)；
//   * Y 范围来自右侧输入框（授权范围），**与渲染切片无关**：切片只是观察方式，
//     永远不会改写这些输入框，也不会被这些输入框改写。
//
// 分工
// ----
//   本模块只产生选区数据 + 画好俯视图；把选区交给适配层（setSelection）与诊断由 app.js 完成。
// ===========================================================================

const ACCENT = "#ffd166";
const CLAMP_STEP_CHOICES = [1, 2, 4, 8, 16, 32, 64, 128];
const MAX_COLUMNS = 4000000; // 超过则不做逐列高度图（只画占位）

function isInt(n) {
  return typeof n === "number" && Number.isInteger(n);
}

function clampInt(v, lo, hi) {
  return v < lo ? lo : v > hi ? hi : v;
}

/**
 * @param {object} options
 *   canvas       俯视图 canvas（必需）
 *   yMin, yMax   两个 <input type=number>（project_local Y，含端点）
 *   yFullButton  按钮：把 Y 范围设为完整场景高度（可选）
 *   yClearButton 按钮：清空选区（可选）
 *   feedback     用于放校验文本的元素（可选）
 *   onChange(selection|null)  选区变化回调（必需）
 *   onHover(cellInfo|null)    悬停回调（可选）
 *   reducedMotion             是否禁用动画（可选，默认读取 matchMedia）
 */
export function createSelectionController(options) {
  const canvas = options.canvas;
  if (!canvas || typeof canvas.getContext !== "function") {
    throw new Error("createSelectionController 需要 canvas 元素");
  }
  const ctx = canvas.getContext("2d");
  const yMinInput = options.yMin || null;
  const yMaxInput = options.yMax || null;
  const yFullButton = options.yFullButton || null;
  const yClearButton = options.yClearButton || null;
  const feedback = options.feedback || null;
  const onChange = typeof options.onChange === "function" ? options.onChange : () => {};
  const onHover = typeof options.onHover === "function" ? options.onHover : () => {};
  const reducedMotion =
    typeof options.reducedMotion === "boolean"
      ? options.reducedMotion
      : typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

  /** 场景信息（crop 尺寸、origin、完整场景 bounds、体素数组） */
  let scene = null;
  /** 每个调色板条目的显示色（来自适配层的贴图平均色；可为 null） */
  let swatches = null;
  /** 当前选区（project_local 半开区间）。null = 未选择 */
  let selection = null;
  /** 选区来源，用于展示与排查：'top_view' | 'plane' | 'pick' | 'external' */
  let source = null;
  /** 拖拽状态 */
  let drag = null;
  let hoverCell = null;
  let cursor = null; // 键盘光标（crop 局部 x,z）
  let anchor = null; // 键盘锚点
  let enabled = true;
  let disposed = false;
  /** 底图缓存：场景/尺寸不变时只画一次 */
  const mapLayer = document.createElement("canvas");
  let mapKey = "";
  /** 动画（选区框变化时的短插值） */
  let animation = null;
  let rafHandle = null;

  // --- 高度图（每列最高非空气方块） ---------------------------------------
  let height = null; // Int16Array(sx*sz)，-1 = 空列
  let heightState = null; // Int32Array(sx*sz)，该列最高体素的调色板下标
  let heightCount = null; // Int32Array(sx*sz)，该列非空气体素数

  function buildHeightMap() {
    if (!scene) return;
    const [sx, sy, sz] = scene.size;
    const n = sx * sz;
    if (n > MAX_COLUMNS) {
      height = null;
      heightState = null;
      heightCount = null;
      return;
    }
    height = new Int16Array(n).fill(-1);
    heightState = new Int32Array(n);
    heightCount = new Int32Array(n);
    const { idx, state } = scene;
    const layer = sy * sz;
    for (let k = 0; k < idx.length; k++) {
      const i = idx[k];
      const x = Math.floor(i / layer);
      const rem = i - x * layer;
      const y = Math.floor(rem / sz);
      const z = rem - y * sz;
      const col = z * sx + x;
      heightCount[col]++;
      if (y > height[col]) {
        height[col] = y;
        heightState[col] = state[k];
      }
    }
  }

  // --- 布局：把 crop 的 (sx, sz) 等比放进 canvas ---------------------------

  function layout() {
    const rect = canvas.getBoundingClientRect();
    const cssW = Math.max(1, Math.round(rect.width));
    const cssH = Math.max(1, Math.round(rect.height));
    const dpr = Math.min(3, Math.max(1, window.devicePixelRatio || 1));
    const pw = Math.max(1, Math.round(cssW * dpr));
    const ph = Math.max(1, Math.round(cssH * dpr));
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
      mapLayer.width = pw;
      mapLayer.height = ph;
      mapKey = "";
    }
    const pad = 10 * dpr;
    const boxW = pw - pad * 2;
    const boxH = ph - pad * 2;
    const [sx, sz] = scene ? scene.size.map((v, i) => (i === 1 ? 1 : v)) : [1, 1];
    const scale = Math.min(boxW / Math.max(1, sx), boxH / Math.max(1, sz));
    const cellW = Math.max(0.5, scale);
    const cellH = Math.max(0.5, scale);
    const areaW = cellW * Math.max(1, sx);
    const areaH = cellH * Math.max(1, sz);
    const originX = pad + Math.max(0, (boxW - areaW) / 2);
    const originY = pad + Math.max(0, (boxH - areaH) / 2);
    return { dpr, pw, ph, cellW, cellH, originX, originY, areaW, areaH, sx, sz };
  }

  /** 屏幕像素（canvas 设备像素）→ crop 局部列；返回 null 表示在地图之外。 */
  function cellAtDevice(px, py, lay) {
    if (!scene) return null;
    const x = Math.floor((px - lay.originX) / lay.cellW);
    const z = Math.floor((py - lay.originY) / lay.cellH);
    if (x < 0 || z < 0 || x >= lay.sx || z >= lay.sz) return null;
    return { x, z };
  }

  function deviceFromEvent(ev, lay) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / Math.max(1, rect.width);
    const scaleY = canvas.height / Math.max(1, rect.height);
    return { px: (ev.clientX - rect.left) * scaleX, py: (ev.clientY - rect.top) * scaleY };
  }

  // --- 绘制 ---------------------------------------------------------------

  /** 该列在俯视图里的颜色：优先用调色板条目的贴图平均色，否则按高度做灰阶。 */
  function colorForColumn(col) {
    if (!height) return "#3a444e";
    if (y < 0) return null; // 空列
    const stateIndex = heightState[col];
    const swatch = swatches && swatches[stateIndex] ? swatches[stateIndex] : null;
    if (swatch && swatch.color) return swatch.color;
    // 兜底：按高度做灰阶（仍然是真实数据，只是没有贴图色）
    const sy = scene ? Math.max(1, scene.size[1] - 1) : 1;
    const t = clampInt(y, 0, sy) / sy;
    const g = Math.round(56 + t * 150);
    return `rgb(${g},${g + 4},${g + 8})`;
  }

  function buildMapLayer(lay) {
    const key = [
      scene ? scene.scene_hash : "none",
      scene ? scene.crop_origin_local.join(",") : "",
      scene ? scene.size.join(",") : "",
      lay.pw,
      lay.ph,
      swatches ? swatches.map((s) => (s && s.color) || "-").join("|") : "",
    ].join("#");
    if (key === mapKey) return;
    mapKey = key;
    const mctx = mapLayer.getContext("2d");
    mctx.setTransform(1, 0, 0, 1, 0, 0);
    mctx.clearRect(0, 0, mapLayer.width, mapLayer.height);
    mctx.fillStyle = "#0d1013";
    mctx.fillRect(0, 0, mapLayer.width, mapLayer.height);
    if (!scene) return;

    const [sx, sy, sz] = scene.size;
    // 1) 地图区域底色
    mctx.fillStyle = "#14181c";
    mctx.fillRect(lay.originX, lay.originY, lay.areaW, lay.areaH);

    // 2) 逐"有方块的列"上色（空列保持底色）：直接按列画，数量上界 = 体素数
    if (height) {
      for (let z = 0; z < sz; z++) {
        for (let x = 0; x < sx; x++) {
          const col = z * sx + x;
          const color = colorForColumn(col);
          if (!color) continue;
          mctx.fillStyle = color;
          mctx.globalAlpha = 0.92;
          mctx.fillRect(lay.originX + x * lay.cellW, lay.originY + z * lay.cellH, Math.ceil(lay.cellW), Math.ceil(lay.cellH));
        }
      }
      mctx.globalAlpha = 1;
    } else {
      mctx.fillStyle = "#1b2026";
      mctx.fillRect(lay.originX, lay.originY, lay.areaW, lay.areaH);
      mctx.fillStyle = "#74838f";
      mctx.font = `${Math.round(11 * lay.dpr)}px ui-monospace, Consolas, monospace`;
      mctx.fillText("crop 过大：俯视图不做逐列高度图（只显示范围）", lay.originX + 8, lay.originY + 18 * lay.dpr);
    }

    // 3) 网格：步长按像素密度自适应，保证网格线至少 6px 间距
    let step = CLAMP_STEP_CHOICES[CLAMP_STEP_CHOICES.length - 1];
    for (const candidate of CLAMP_STEP_CHOICES) {
      if (candidate * lay.cellW >= 6 * lay.dpr) {
        step = candidate;
        break;
      }
    }
    mctx.strokeStyle = "rgba(255,255,255,0.10)";
    mctx.lineWidth = Math.max(1, Math.round(lay.dpr * 0.6));
    for (let x = step; x < sx; x += step) {
      const px = lay.originX + x * lay.cellW;
      mctx.beginPath();
      mctx.moveTo(px, lay.originY);
      mctx.lineTo(px, lay.originY + lay.areaH);
      mctx.stroke();
    }
    for (let z = step; z < sz; z += step) {
      const py = lay.originY + z * lay.cellH;
      mctx.beginPath();
      mctx.moveTo(lay.originX, py);
      mctx.lineTo(lay.originX + lay.areaW, py);
      mctx.stroke();
    }
    // 4) crop 边框 + 原点角标
    mctx.strokeStyle = "rgba(169,182,194,0.55)";
    mctx.lineWidth = Math.max(1, Math.round(lay.dpr));
    mctx.strokeRect(lay.originX + 0.5, lay.originY + 0.5, lay.areaW - 1, lay.areaH - 1);
    mctx.fillStyle = "rgba(232,238,244,0.75)";
    mctx.font = `${Math.round(10 * lay.dpr)}px ui-monospace, Consolas, monospace`;
    mctx.fillText("x→", lay.originX + 4 * lay.dpr, lay.originY - 3 * lay.dpr);
    mctx.fillText("z↓", lay.originX - 2 * lay.dpr, lay.originY + 11 * lay.dpr);
  }

  function render() {
    if (disposed) return;
    const lay = layout();
    buildMapLayer(lay);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(mapLayer, 0, 0);
    ctx.setTransform(lay.dpr, 0, 0, lay.dpr, 0, 0);
    const cw = lay.pw / lay.dpr;
    const ch = lay.ph / lay.dpr;
    void cw;
    void ch;

    if (!scene) {
      ctx.fillStyle = "#74838f";
      ctx.font = "11px ui-monospace, Consolas, monospace";
      ctx.fillText("尚未加载场景", 12, 22);
      return;
    }

    // 选区（动画中的过渡框或最终框，单位 = crop 局部格）
    const rect = animation ? animation.current : selectionToRenderRect();
    if (rect) {
      const x0 = lay.originX + rect.minX * lay.cellW;
      const y0 = lay.originY + rect.minZ * lay.cellH;
      const w = Math.max(lay.cellW, (rect.maxX - rect.minX) * lay.cellW);
      const h = Math.max(lay.cellH, (rect.maxZ - rect.minZ) * lay.cellH);
      ctx.fillStyle = "rgba(255,209,102,0.16)";
      ctx.fillRect(x0, y0, w, h);
      ctx.strokeStyle = ACCENT;
      ctx.lineWidth = 2;
      ctx.strokeRect(x0 + 1, y0 + 1, w - 2, h - 2);
      // 角标：四个角各画一小段，强调这是"可编辑范围的角点"
      ctx.lineWidth = 3;
      const tick = Math.max(5, Math.min(12, Math.min(w, h) / 3));
      for (const [cx, cy, dx, dy] of [
        [x0, y0, 1, 1],
        [x0 + w, y0, -1, 1],
        [x0, y0 + h, 1, -1],
        [x0 + w, y0 + h, -1, -1],
      ]) {
        ctx.beginPath();
        ctx.moveTo(cx + dx * tick, cy);
        ctx.lineTo(cx, cy);
        ctx.lineTo(cx, cy + dy * tick);
        ctx.stroke();
      }
    }

    // 拖动中的橡皮筋
    if (drag) {
      const minX = Math.min(drag.a.x, drag.b.x);
      const maxX = Math.max(drag.a.x, drag.b.x) + 1;
      const minZ = Math.min(drag.a.z, drag.b.z);
      const maxZ = Math.max(drag.a.z, drag.b.z) + 1;
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = "rgba(255,209,102,0.9)";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(
        lay.originX + minX * lay.cellW,
        lay.originY + minZ * lay.cellH,
        (maxX - minX) * lay.cellW,
        (maxZ - minZ) * lay.cellH,
      );
      ctx.setLineDash([]);
    }

    // 悬停格 + 键盘光标
    const cell = hoverCell || cursor;
    if (cell) {
      ctx.strokeStyle = hoverCell ? "rgba(120,190,255,0.95)" : "rgba(255,255,255,0.7)";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(
        lay.originX + cell.x * lay.cellW + 0.5,
        lay.originY + cell.z * lay.cellH + 0.5,
        Math.max(1, lay.cellW - 1),
        Math.max(1, lay.cellH - 1),
      );
    }
  }

  // --- 选区数据 -----------------------------------------------------------

  /** 从 Y 输入框读出授权 Y 范围（project_local，含端点），并夹到完整场景高度。 */
  function currentYRange() {
    const bounds = scene ? scene.full_scene_bounds : { min: [0, 0, 0], max_exclusive: [1, 1, 1] };
    const lo = bounds.min[1];
    const hi = bounds.max_exclusive[1] - 1;
    const rawMin = yMinInput ? Number(yMinInput.value) : lo;
    const rawMax = yMaxInput ? Number(yMaxInput.value) : hi;
    const minSafe = Number.isFinite(rawMin) ? rawMin : lo;
    const maxSafe = Number.isFinite(rawMax) ? rawMax : hi;
    const clampedMin = clampInt(Math.round(minSafe), lo, Math.max(lo, hi));
    const clampedMax = clampInt(Math.round(maxSafe), lo, Math.max(lo, hi));
    return {
      min: Math.min(clampedMin, clampedMax),
      max: Math.max(clampedMin, clampedMax),
      boundsLo: lo,
      boundsHi: hi,
      // 输入被夹住时要在界面上说清楚（不静默改数）
      rawOutOfBounds: clampedMin !== Math.round(minSafe) || clampedMax !== Math.round(maxSafe),
    };
  }
  /** 选区 → crop 局部矩形（用于俯视图绘制）。 */
  function selectionToRenderRect() {
    if (!selection || !scene) return null;
    const o = scene.crop_origin_local;
    const [sx, sy, sz] = scene.size;
    const minX = clampInt(selection.min[0] - o[0], 0, sx);
    const maxX = clampInt(selection.max_exclusive[0] - o[0], 0, sx);
    const minZ = clampInt(selection.min[2] - o[2], 0, sz);
    const maxZ = clampInt(selection.max_exclusive[2] - o[2], 0, sz);
    if (maxX <= minX || maxZ <= minZ) return null;
    void sy;
    return { minX, maxX, minZ, maxZ };
  }

  function selectionFromRenderRect(rect, yRange, why) {
    const o = scene.crop_origin_local;
    const min = [rect.minX + o[0], yRange.min, rect.minZ + o[2]];
    const maxExclusive = [rect.maxX + o[0], yRange.max + 1, rect.maxZ + o[2]];
    return makeSelection(min, maxExclusive, why);
  }

  function makeSelection(min, maxExclusive, why) {
    const size = [maxExclusive[0] - min[0], maxExclusive[1] - min[1], maxExclusive[2] - min[2]];
    const voxelCount = size[0] * size[1] * size[2];
    return {
      min,
      max_exclusive: maxExclusive,
      size,
      coordinate_space: "project_local",
      voxel_count: voxelCount,
      non_air_count: countNonAir(min, maxExclusive),
      source: why,
      note: "预计授权范围：最终以服务端 WriteGuard ∩ 可编辑 ∩ 非保护区 为准；本前端不做授权判定",
    };
  }

  /** 选区内的非空气体素数（基于**当前渲染场景**的 crop 数据，不是全场景）。 */
  function countNonAir(min, maxExclusive) {
    if (!scene || !scene.idx) return null;
    const o = scene.crop_origin_local;
    const [sx, sy, sz] = scene.size;
    const lo = [min[0] - o[0], min[1] - o[1], min[2] - o[2]];
    const hi = [maxExclusive[0] - o[0], maxExclusive[1] - o[1], maxExclusive[2] - o[2]];
    // 与 crop 无交集时无法从渲染数据推断
    if (hi[0] <= 0 || hi[1] <= 0 || hi[2] <= 0 || lo[0] >= sx || lo[1] >= sy || lo[2] >= sz) return null;
    let count = 0;
    const layer = sy * sz;
    for (let k = 0; k < scene.idx.length; k++) {
      const i = scene.idx[k];
      const x = Math.floor(i / layer);
      const rem = i - x * layer;
      const y = Math.floor(rem / sz);
      const z = rem - y * sz;
      if (x >= lo[0] && x < hi[0] && y >= lo[1] && y < hi[1] && z >= lo[2] && z < hi[2]) count++;
    }
    return count;
  }

  function emitChange() {
    if (feedback) {
      const yRange = currentYRange();
      const messages = [];
      if (selection) {
        messages.push(
          `project_local 半开区间 [${selection.min.join(", ")}] → [${selection.max_exclusive.join(", ")}]，尺寸 ${selection.size.join("×")}`,
        );
        if (selection.extends_outside_crop) messages.push("选区超出当前渲染 crop：超出的部分只能在后端校验（视口看不到）");
        const sy = scene ? scene.size[1] : 0;
        const sliceHidden = selection.min[1] < scene?.crop_origin_local[1] || selection.max_exclusive[1] > scene?.crop_origin_local[1] + sy;
        if (sliceHidden) messages.push("选区 Y 范围超出现有 crop 的 Y 范围：渲染切片不会因此改变");
      }
      if (yRange.rawOutOfBounds) messages.push(`Y 范围已夹到完整场景高度 [${yRange.boundsLo}, ${yRange.boundsHi}]`);
      feedback.textContent = messages.join("　|　");
    }
    onChange(selection);
  }

  function applySelection(next, why) {
    const previous = selection;
    selection = next;
    source = why;
    if (selection) {
      // 记录是否超出 crop（显示与校验分开说明）
      const rect = selectionToRenderRect();
      const full =
        rect &&
        rect.minX === selection.min[0] - scene.crop_origin_local[0] &&
        rect.maxX === selection.max_exclusive[0] - scene.crop_origin_local[0] &&
        rect.minZ === selection.min[2] - scene.crop_origin_local[2] &&
        rect.maxZ === selection.max_exclusive[2] - scene.crop_origin_local[2];
      const [sx, sy, sz] = scene.size;
      const yInside =
        selection.min[1] >= scene.crop_origin_local[1] &&
        selection.max_exclusive[1] <= scene.crop_origin_local[1] + sy;
      selection.extends_outside_crop = !(full && yInside);
      selection.crop_size = [sx, sy, sz];
    }
    if (!reducedMotion && previous && selection) {
      // 短插值：让框的移动被"看见"，而不是瞬移（reduced-motion 下直接跳）
      animateFrom(previous, selection);
    }
    render();
    emitChange();
  }

  function animateFrom(from, to) {
    const a = selectionToRenderRectOf(from);
    const b = selectionToRenderRectOf(to);
    if (!a || !b) return;
    animation = { start: performance.now(), current: { ...a }, from: a, to: b, duration: 160 };
    if (rafHandle === null) rafHandle = requestAnimationFrame(stepAnimation);
  }

  /** 任意选区 → crop 局部矩形（只做裁剪，不判断完整性）。 */
  function selectionToRenderRectOf(sel) {
    if (!sel || !scene) return null;
    const o = scene.crop_origin_local;
    const [sx, , sz] = scene.size;
    return {
      minX: clampInt(sel.min[0] - o[0], 0, sx),
      maxX: clampInt(sel.max_exclusive[0] - o[0], 0, sx),
      minZ: clampInt(sel.min[2] - o[2], 0, sz),
      maxZ: clampInt(sel.max_exclusive[2] - o[2], 0, sz),
    };
  }

  function stepAnimation() {
    rafHandle = null;
    if (!animation) return;
    const t = Math.min(1, (performance.now() - animation.start) / animation.duration);
    const ease = 1 - Math.pow(1 - t, 3); // ease-out cubic
    animation.current = {
      minX: animation.from.minX + (animation.to.minX - animation.from.minX) * ease,
      maxX: animation.from.maxX + (animation.to.maxX - animation.from.maxX) * ease,
      minZ: animation.from.minZ + (animation.to.minZ - animation.from.minZ) * ease,
      maxZ: animation.from.maxZ + (animation.to.maxZ - animation.from.maxZ) * ease,
    };
    render();
    if (t < 1) rafHandle = requestAnimationFrame(stepAnimation);
    else animation = null;
  }

  // --- 交互 ---------------------------------------------------------------

  function onPointerDown(ev) {
    if (!enabled || !scene || ev.button !== 0) return;
    const lay = layout();
    const { px, py } = deviceFromEvent(ev, lay);
    const cell = cellAtDevice(px, py, lay);
    if (!cell) return;
    ev.preventDefault();
    canvas.focus({ preventScroll: true });
    drag = { a: cell, b: cell, additive: ev.shiftKey, pointerId: ev.pointerId };
    anchor = cell;
    cursor = cell;
    canvas.setPointerCapture?.(ev.pointerId);
    render();
  }

  function onPointerMove(ev) {
    if (!enabled || !scene) return;
    const lay = layout();
    const { px, py } = deviceFromEvent(ev, lay);
    const cell = cellAtDevice(px, py, lay);
    if (drag) {
      if (cell) {
        drag.b = cell;
        cursor = cell;
        render();
      }
      return;
    }
    const changed = !!cell !== !!hoverCell || (cell && hoverCell && (cell.x !== hoverCell.x || cell.z !== hoverCell.z));
    if (changed) {
      hoverCell = cell;
      if (cell) {
        const col = cell.z * lay.sx + cell.x;
        const y = height ? height[col] : -1;
        const stateIndex = heightState && y >= 0 ? heightState[col] : null;
        const entry = stateIndex != null && scene.palette[stateIndex] ? scene.palette[stateIndex] : null;
        onHover({
          cell,
          pos_local: [cell.x + scene.crop_origin_local[0], y >= 0 ? y + scene.crop_origin_local[1] : null, cell.z + scene.crop_origin_local[2]],
          top_y_render: y,
          column_voxels: heightCount ? heightCount[col] : null,
          top_state: entry ? { name: entry.name, props: entry.props || {} } : null,
          note: "俯视单元格读数是显示数据（该列最高非空气方块）",
        });
      } else {
        onHover(null);
      }
      render();
    }
  }

  function onPointerUp() {
    if (!drag) return;
    const finished = drag;
    drag = null;
    canvas.releasePointerCapture?.(finished.pointerId);
    const minX = Math.min(finished.a.x, finished.b.x);
    const maxX = Math.max(finished.a.x, finished.b.x) + 1;
    const minZ = Math.min(finished.a.z, finished.b.z);
    const maxZ = Math.max(finished.a.z, finished.b.z) + 1;
    let rect = { minX, maxX, minZ, maxZ };
    if (finished.additive) rect = unionRenderRect(selectionToRenderRect(), rect);
    if (!rect) {
      render();
      return;
    }
    const yRange = currentYRange();
    const next = selectionFromRenderRect(rect, yRange, "top_view");
    commitYInputs(yRange);
    applySelection(next, "top_view");
  }

  function unionRenderRect(a, b) {
    if (!a) return b;
    if (!b) return a;
    return {
      minX: Math.min(a.minX, b.minX),
      maxX: Math.max(a.maxX, b.maxX),
      minZ: Math.min(a.minZ, b.minZ),
      maxZ: Math.max(a.maxZ, b.maxZ),
    };
  }

  function onPointerLeave() {
    if (drag) return;
    if (hoverCell) {
      hoverCell = null;
      onHover(null);
      render();
    }
  }

  function onKeyDown(ev) {
    if (!enabled || !scene) return;
    const stepKeys = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    const delta = stepKeys[ev.key];
    if (ev.key === "Escape") {
      ev.preventDefault();
      clear();
      return;
    }
    if (!delta) return;
    ev.preventDefault();
    const [sx, , sz] = scene.size;
    if (!cursor) cursor = { x: Math.floor(sx / 2), z: Math.floor(sz / 2) };
    const rect = selectionToRenderRect();
    if (ev.shiftKey) {
      // Shift + 方向键：以锚点为准调整右下角（矩形伸缩）
      if (!anchor) anchor = cursor;
      cursor = { x: clampInt(cursor.x + delta[0], 0, sx - 1), z: clampInt(cursor.z + delta[1], 0, sz - 1) };
      const minX = Math.min(anchor.x, cursor.x);
      const maxX = Math.max(anchor.x, cursor.x) + 1;
      const minZ = Math.min(anchor.z, cursor.z);
      const maxZ = Math.max(anchor.z, cursor.z) + 1;
      const yRange = currentYRange();
      applySelection(selectionFromRenderRect({ minX, maxX, minZ, maxZ }, yRange, "top_view"), "top_view");
      return;
    }
    // 方向键：整体平移选区（或建立 1×1 选区）
    if (!rect) {
      cursor = { x: clampInt(cursor.x + delta[0], 0, sx - 1), z: clampInt(cursor.z + delta[1], 0, sz - 1) };
      anchor = cursor;
      const yRange = currentYRange();
      applySelection(selectionFromRenderRect({ minX: cursor.x, maxX: cursor.x + 1, minZ: cursor.z, maxZ: cursor.z + 1 }, yRange, "top_view"), "top_view");
      return;
    }
    const w = rect.maxX - rect.minX;
    const h = rect.maxZ - rect.minZ;
    const minX = clampInt(rect.minX + delta[0], 0, sx - w);
    const minZ = clampInt(rect.minZ + delta[1], 0, sz - h);
    cursor = { x: clampInt(cursor.x + delta[0], 0, sx - 1), z: clampInt(cursor.z + delta[1], 0, sz - 1) };
    anchor = cursor;
    const yRange = currentYRange();
    applySelection(selectionFromRenderRect({ minX, maxX: minX + w, minZ, maxZ: minZ + h }, yRange, "top_view"), "top_view");
  }

  /** 把输入框里被夹住的值写回，避免"显示 5 实际 4"的静默不一致。 */
  function commitYInputs(yRange) {
    if (yMinInput) yMinInput.value = String(yRange.min);
    if (yMaxInput) yMaxInput.value = String(yRange.max);
  }

  function onYInputChanged() {
    if (!scene) return;
    const yRange = currentYRange();
    commitYInputs(yRange);
    if (!selection) {
      if (feedback) feedback.textContent = `Y 范围 = [${yRange.min}, ${yRange.max}]（含端点，project_local）；先在俯视图拖选 x/z 区域`;
      return;
    }
    applySelection(makeSelection(selection.min, [selection.max_exclusive[0], yRange.max + 1, selection.max_exclusive[2]], source || "top_view"), source || "top_view");
  }

  function setFullHeight() {
    if (!scene) return;
    const lo = scene.full_scene_bounds.min[1];
    const hi = scene.full_scene_bounds.max_exclusive[1] - 1;
    setYRange({ min: lo, max: hi });
  }

  function setYRange(range) {
    const bounds = scene ? scene.full_scene_bounds : { min: [0, 0, 0], max_exclusive: [1, 1, 1] };
    const lo = bounds.min[1];
    const hi = bounds.max_exclusive[1] - 1;
    const rawMin = Number(range.min);
    const rawMax = Number(range.max);
    const min = clampInt(Math.round(Number.isFinite(rawMin) ? rawMin : lo), lo, Math.max(lo, hi));
    const max = clampInt(Math.round(Number.isFinite(rawMax) ? rawMax : hi), lo, Math.max(lo, hi));
    if (yMinInput) yMinInput.value = String(Math.min(min, max));
    if (yMaxInput) yMaxInput.value = String(Math.max(min, max));
    if (selection) onYInputChanged();
    else if (feedback) feedback.textContent = `Y 范围 = [${Math.min(min, max)}, ${Math.max(min, max)}]（含端点，project_local）`;
  }

  function clear() {
    selection = null;
    animation = null;
    cursor = null;
    anchor = null;
    render();
    emitChange();
  }

  function setSelection(box, why = "external") {
    if (!scene) return;
    if (!box) {
      clear();
      return;
    }
    if (!isInt(box.min[0]) || !isInt(box.min[1]) || !isInt(box.min[2])) return;
    const next = makeSelection([box.min[0], box.min[1], box.min[2]], [box.max_exclusive[0], box.max_exclusive[1], box.max_exclusive[2]], why);
    // 同步 Y 输入框：选区是唯一事实，控件只是它的显示
    if (yMinInput) yMinInput.value = String(next.min[1]);
    if (yMaxInput) yMaxInput.value = String(next.max_exclusive[1] - 1);
    applySelection(next, why);
  }

  // --- 绑定 ---------------------------------------------------------------

  canvas.setAttribute("tabindex", "0");
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointercancel", onPointerUp);
  canvas.addEventListener("pointerleave", onPointerLeave);
  canvas.addEventListener("keydown", onKeyDown);
  if (yMinInput) yMinInput.addEventListener("change", onYInputChanged);
  if (yMaxInput) yMaxInput.addEventListener("change", onYInputChanged);
  if (yFullButton) yFullButton.addEventListener("click", setFullHeight);
  if (yClearButton) yClearButton.addEventListener("click", clear);
  const resizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(() => render()) : null;
  if (resizeObserver) resizeObserver.observe(canvas);

  return {
    /** 新的场景：重建高度图与底图缓存，并清空选区（选区属于场景）。 */
    setScene(sceneInfo, paletteSwatches) {
      scene = sceneInfo;
      swatches = paletteSwatches || null;
      selection = null;
      animation = null;
      cursor = null;
      anchor = null;
      hoverCell = null;
      mapKey = "";
      buildHeightMap();
      if (scene && yMinInput && yMaxInput && !yMinInput.value && !yMaxInput.value) {
        const lo = scene.full_scene_bounds.min[1];
        const hi = scene.full_scene_bounds.max_exclusive[1] - 1;
        yMinInput.value = String(lo);
        yMaxInput.value = String(hi);
      }
      render();
      emitChange();
    },

    /** 当前授权 Y 范围（project_local，含端点；已夹到完整场景高度）。 */
    getYRange() {
      const range = currentYRange();
      return { min: range.min, max: range.max, boundsLo: range.boundsLo, boundsHi: range.boundsHi, unit: "project_local Y（含端点）" };
    },
    setSelection,
    getSelection() {
      return selection;
    },
    setYRange,
    setFullHeight,
    clear,
    render,
    setEnabled(value) {
      enabled = !!value;
      canvas.setAttribute("aria-disabled", enabled ? "false" : "true");
    },
    getCellAt(clientX, clientY) {
      const lay = layout();
      const rect = canvas.getBoundingClientRect();
      const px = (clientX - rect.left) * (canvas.width / Math.max(1, rect.width));
      const py = (clientY - rect.top) * (canvas.height / Math.max(1, rect.height));
      return cellAtDevice(px, py, lay);
    },
    dispose() {
      disposed = true;
      if (rafHandle !== null) cancelAnimationFrame(rafHandle);
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointercancel", onPointerUp);
      canvas.removeEventListener("pointerleave", onPointerLeave);
      canvas.removeEventListener("keydown", onKeyDown);
      if (yMinInput) yMinInput.removeEventListener("change", onYInputChanged);
      if (yMaxInput) yMaxInput.removeEventListener("change", onYInputChanged);
      if (yFullButton) yFullButton.removeEventListener("click", setFullHeight);
      if (yClearButton) yClearButton.removeEventListener("click", clear);
      if (resizeObserver) resizeObserver.disconnect();
    },
  };
}
