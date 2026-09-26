// ===========================================================================
// web/review_bridge.js — /review-view 的自动化控制接口
// ===========================================================================
//
// 用途
// ----
// 用 Playwright/CDP 打开 **与工作台同一构建、同一适配层** 的 review-view.html，
// 通过页面内接口摆相机、切切片、等渲染完成，再对元素截图。这里没有任何"猜坐标"或
// 模拟连续拖动：所有视图都由相机/切片协议显式设置。
//
// 关键契约
// --------
//   load(sceneUrl)             加载 RenderScene，返回 {receipt, diagnostics}
//   setCamera(camera)          显式相机（pitch/yaw/camera_pos，上游语义）
//   setSlice({min,max}|null)   切片（crop 局部 Y，含端点）
//   whenIdle(sceneHash)        资源就绪 + 场景建立 + 该 build 至少提交一帧
//   screenshotReady()          截图前的"可取证"检查：whenIdle → renderFrame →
//                              readPixels 摘要 + 颜色分布（用于区分"空选区"与"空白帧"）
//   视图预设                     viewTop() / viewOblique(a|b) / viewCutaway(minY)
//   roundTripPickCheck()       投影像素 ↔ 体素拾取的一致性自检（矩阵/射线/DDA）
//
// 明确不做的事
// ------------
//   * 不用 sleep/networkidle 假装渲染就绪；
//   * 不在 WebGL 不可用时给出一张空白图当"通过"：会返回 status=RENDER_UNAVAILABLE；
//   * 不写蓝图、不写文件：截图由外部工具完成。
// ===========================================================================

import { createViewerAdapter, validateRenderScene, RENDER_STATUS } from "./viewer_adapter.js";

const viewport = document.getElementById("review-viewport");

const adapter = createViewerAdapter(viewport, {
  interactionMode: "navigate",
  resetViewOnLoad: true,
});

const state = {
  sceneUrl: null,
  payload: null,
  receipt: null,
  sceneHash: null,
  digestAlgo: null,
};

function fail(code, message, detail) {
  const err = new Error(message);
  err.code = code;
  if (detail !== undefined) err.detail = detail;
  return err;
}

async function fetchJson(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw fail("SCENE_LOAD_FAILED", `HTTP ${res.status} ${res.statusText}（${url}）`);
  return await res.json();
}

async function load(sceneUrl, options = {}) {
  if (!sceneUrl) throw fail("BAD_ARGUMENT", "load(sceneUrl) 需要 URL");
  state.sceneUrl = sceneUrl;
  const payload = await fetchJson(sceneUrl);
  const check = validateRenderScene(payload);
  if (!check.ok) throw fail("INVALID_RENDER_SCENE", "RenderScene 预检失败", check.errors);
  state.payload = payload;
  const receipt = await adapter.loadScene(payload);
  state.receipt = receipt;
  state.sceneHash = receipt.scene_hash;
  return { receipt, diagnostics: adapter.getDiagnostics(), warnings: check.warnings };
}

function currentSceneHash() {
  const info = adapter.getSceneInfo();
  return info ? info.scene_hash : null;
}

/** 场景尺寸（crop 局部，渲染器方块坐标从 0 开始）。 */
function cropSize() {
  const info = adapter.getSceneInfo();
  return info ? info.size : [1, 1, 1];
}

function setCamera(camera) {
  adapter.setCamera(camera);
}

function getCamera() {
  return adapter.getCamera();
}

async function setSlice(slice) {
  await adapter.setSlice(slice);
  return adapter.getDiagnostics().slice_range;
}

function whenIdle(sceneHash) {
  return adapter.whenIdle(sceneHash || currentSceneHash());
}

// --- 视图预设（deterministic evidence views） ------------------------------
//
// 相机语义：pitch/yaw 是弧度，camera_pos 是**参与 view matrix 的平移量**。
// 把结构中心摆到原点：camera_pos = -(size/2)。
// 顶视：pitch = +π/2（clamp 上界）时 eye 的 -Z 指向世界 -Y，即自上而下看。

function centerCameraOffset() {
  const [sx, sy, sz] = cropSize();
  return [-sx / 2, -sy / 2, -sz / 2];
}

async function viewTop() {
  setCamera({ pitch: Math.PI / 2, yaw: 0, camera_pos: centerCameraOffset() });
  return await whenIdle();
}

async function viewOblique(which = "a") {
  const presets = {
    a: { pitch: 0.8, yaw: 0.5 },
    b: { pitch: 0.55, yaw: 2.4 },
  };
  const preset = presets[which] || presets.a;
  setCamera({ pitch: preset.pitch, yaw: preset.yaw, camera_pos: centerCameraOffset() });
  return await whenIdle();
}

async function viewCutaway(minY, which = "a") {
  await setSlice({ min: minY, max: Math.max(minY, cropSize()[1] - 1) });
  return await viewOblique(which);
}

async function setSliceFull() {
  await setSlice(null);
  return adapter.getDiagnostics().slice_range;
}

// --- 像素取证 -------------------------------------------------------------

function fnv1a64(bytes) {
  let hash = 0xcbf29ce484222325n;
  const prime = 0x100000001b3n;
  const mask = 0xffffffffffffffffn;
  for (let i = 0; i < bytes.length; i++) {
    hash ^= BigInt(bytes[i]);
    hash = (hash * prime) & mask;
  }
  return hash.toString(16).padStart(16, "0");
}

async function digestBytes(bytes) {
  const subtle = typeof crypto !== "undefined" ? crypto.subtle : null;
  if (subtle && typeof subtle.digest === "function") {
    try {
      const buf = await subtle.digest("SHA-256", bytes);
      const hex = Array.from(new Uint8Array(buf))
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");
      return { algo: "sha256", hex };
    } catch (err) {
      return { algo: "fnv1a64", hex: fnv1a64(bytes) };
    }
  }
  return { algo: "fnv1a64", hex: fnv1a64(bytes) };
}

/**
 * 帧内容统计。**不是**用方差判断"有没有东西"：这里只给出颜色桶分布，
 * 用来发现"整帧一个颜色"的异常；空选区是合法状态，靠 receipt.empty / status 判定。
 */
function frameStats(pixels) {
  if (!pixels) return null;
  const { data } = pixels;
  const buckets = new Map();
  let opaque = 0;
  for (let i = 0; i < data.length; i += 4) {
    const a = data[i + 3];
    if (a === 0) continue;
    opaque++;
    const key = ((data[i] >> 4) << 8) | ((data[i + 1] >> 4) << 4) | (data[i + 2] >> 4);
    buckets.set(key, (buckets.get(key) || 0) + 1);
  }
  let dominant = 0;
  let dominantKey = null;
  for (const [key, count] of buckets) {
    if (count > dominant) {
      dominant = count;
      dominantKey = key;
    }
  }
  return {
    width: pixels.width,
    height: pixels.height,
    pixels: pixels.width * pixels.height,
    opaque_pixels: opaque,
    distinct_color_buckets: buckets.size,
    dominant_bucket: dominantKey,
    dominant_ratio: opaque ? Number((dominant / opaque).toFixed(4)) : 0,
    read_errors: pixels.errors,
    note: "颜色桶 = 每通道取高 4 位；dominant_ratio≈1 说明整帧几乎一个颜色（配合 status/empty 判断是否合法）",
  };
}

async function readFrame() {
  const frame = adapter.readFramePixels();
  if (!frame) return null;
  const digest = await digestBytes(frame.pixels);
  return {
    digest: digest.hex,
    digest_algo: digest.algo,
    stats: frameStats(frame),
  };
}

function elementRect() {
  const rect = viewport.getBoundingClientRect();
  return {
    x: rect.left + window.scrollX,
    y: rect.top + window.scrollY,
    width: rect.width,
    height: rect.height,
    device_pixel_ratio: window.devicePixelRatio,
    note: "CSS 像素；截图时用这个矩形做 clip（元素截图亦可）",
  };
}

/**
 * 截图就绪：whenIdle(receipt) → renderFrame() → readPixels 摘要。
 * 返回的 `safe_to_capture` 只有在**渲染就绪或合法空场景**时才为 true。
 */
async function screenshotReady(options = {}) {
  const expected = options.expectedSceneHash || currentSceneHash();
  if (!expected) throw fail("NO_SCENE_LOADED", "screenshotReady 之前必须 load(sceneUrl)");
  const receipt = await adapter.whenIdle(expected);
  await adapter.renderFrame();
  const frame = await readFrame();
  const diagnostics = adapter.getDiagnostics();
  const empty = receipt.empty === true || receipt.status === RENDER_STATUS.EMPTY;
  const unavailable = receipt.status === RENDER_STATUS.UNAVAILABLE;
  const contextLost = diagnostics.context_lost === true;
  const safe = !unavailable && !contextLost;
  return {
    scene_hash: receipt.scene_hash,
    file_sha256: receipt.file_sha256,
    renderer_build_hash: receipt.renderer_build_hash,
    resource_hash: receipt.resource_hash,
    frames_rendered: receipt.frames_rendered,
    status: receipt.status,
    empty,
    context_lost: contextLost,
    pixel_digest: frame ? frame.digest : null,
    pixel_digest_algo: frame ? frame.digest_algo : null,
    frame_stats: frame ? frame.stats : null,
    safe_to_capture: safe,
    blank_frame_suspected: !empty && !!frame && frame.stats.opaque_pixels > 0 && frame.stats.distinct_color_buckets <= 1,
    element_rect: elementRect(),
    viewport: diagnostics.viewport,
    camera: diagnostics.camera,
    slice_range: diagnostics.slice_range,
    accepted_voxel_count: diagnostics.accepted_voxel_count,
    clipped_voxel_count: diagnostics.clipped_voxel_count,
    note: "safe_to_capture=false 时不要生成证据图：本构建不会用空白图冒充成功",
  };
}

// --- 一致性自检 -----------------------------------------------------------

/**
 * 上游 render() 与本适配层的镜像帧是否逐像素一致。
 * 镜像帧额外画了"隐藏层线框"，所以比对时临时关掉该图层。
 */
async function parityCheck() {
  const display = adapter.getDisplayOptions();
  const restore = { hidden_layer_outlines: display.hidden_layer_outlines, grid: display.grid };
  const upstreamRender = globalThis.render;
  if (typeof upstreamRender !== "function") {
    return { ok: false, reason: "上游 render() 不存在（viewer-lite.js 未加载）" };
  }
  try {
    adapter.setDisplayOption("hidden_layer_outlines", false);
    adapter.setDisplayOption("grid", true);
    // 1) 直接用上游 render() 画一帧（它内部构造 view matrix 并调用 drawStructure + drawGrid）
    upstreamRender();
    const a = await readFrame();
    // 2) 用适配层的镜像帧（同一相机、同一图层）
    await adapter.renderFrame();
    const b = await readFrame();
    return {
      ok: true,
      match: a && b && a.digest === b.digest,
      digest_upstream_render: a ? a.digest : null,
      digest_adapter_frame: b ? b.digest : null,
      stats_upstream: a ? a.stats : null,
      stats_adapter: b ? b.stats : null,
      note: "两侧都读同一个 canvas 的 readPixels；相机与图层状态相同。不一致说明镜像帧与上游 render() 有语义差异",
    };
  } finally {
    adapter.setDisplayOption("hidden_layer_outlines", restore.hidden_layer_outlines);
    adapter.setDisplayOption("grid", restore.grid);
  }
}

/**
 * 投影 ↔ 拾取一致性：对采样体素投影中心 → 在 1px 容差内用同样的屏幕点做 DDA 拾取。
 * 命中同一个体素 = 一致；命中更靠近相机的体素 = 遮挡（合理）；命中更远的体素 = 不一致。
 */
async function roundTripPickCheck(sampleCount = 40) {
  const payload = state.payload;
  if (!payload) return { ok: false, reason: "尚未 load()" };
  const cam = adapter.getCamera();
  const view = cam.view_matrix;
  const { mat4, vec3 } = globalThis.glMatrix || {};
  if (!mat4 || !view) return { ok: false, reason: "缺少 glMatrix / view_matrix" };
  // eye = -R^T · t（view = R·T(t)，R 正交）
  const rotation = mat4.fromValues(
    view[0], view[1], view[2], 0,
    view[4], view[5], view[6], 0,
    view[8], view[9], view[10], 0,
    0, 0, 0, 1,
  );
  mat4.transpose(rotation, rotation);
  const t = vec3.fromValues(view[12], view[13], view[14]);
  const eyeOffset = vec3.transformMat4(vec3.create(), t, rotation);
  const eye = [-eyeOffset[0], -eyeOffset[1], -eyeOffset[2]];

  const [sx, sy, sz] = payload.size;
  const origin = payload.crop_origin_local;
  const step = Math.max(1, Math.floor(payload.idx.length / sampleCount));
  const results = { checked: 0, consistent: 0, occluded_plausible: 0, inconsistent: [], eye_render: eye };
  for (let k = 0; k < payload.idx.length; k += step) {
    const i = payload.idx[k];
    const layer = sy * sz;
    const x = Math.floor(i / layer);
    const rem = i - x * layer;
    const y = Math.floor(rem / sz);
    const z = rem - y * sz;
    const posLocal = [x + origin[0], y + origin[1], z + origin[2]];
    const screen = adapter.projectToScreen(posLocal);
    if (!screen || !screen.visible) continue;
    if (screen.x < 1 || screen.y < 1 || screen.x > screen.width - 1 || screen.y > screen.height - 1) continue;
    const picked = adapter.pickVoxelAt(screen.x, screen.y);
    results.checked++;
    if (!picked) {
      results.inconsistent.push({ pos_local: posLocal, screen, picked: null, why: "投影可见但拾取为空" });
      continue;
    }
    if (picked.pos_local[0] === posLocal[0] && picked.pos_local[1] === posLocal[1] && picked.pos_local[2] === posLocal[2]) {
      results.consistent++;
      continue;
    }
    const dist = (v) => Math.hypot(v[0] - eye[0], v[1] - eye[1], v[2] - eye[2]);
    const closer = dist(picked.pos_local) < dist(posLocal);
    if (closer) results.occluded_plausible++;
    else results.inconsistent.push({ pos_local: posLocal, screen, picked: picked.pos_local, why: "拾取到了更远的体素" });
  }
  results.ok = results.checked > 0;
  results.note =
    "consistent = 命中同一体素；occluded_plausible = 命中更近的体素（遮挡合理）；inconsistent 必须为空才算通过";
  return results;
}

/** 单点查询，便于人工核对：屏幕上某点的体素 + 显示状态。 */
function probeScreenPoint(x, y) {
  const hit = adapter.pickVoxelAt(x, y);
  if (!hit) return { hit: null };
  return { hit, voxel: adapter.probeVoxel(hit.pos_local) };
}

// --- 页面接口 -------------------------------------------------------------

const api = {
  version: "0.1",
  adapter,
  status() {
    return adapter.getStatus();
  },
  load,
  setCamera,
  getCamera,
  setSlice,
  setSliceFull,
  whenIdle,
  renderFrame: () => adapter.renderFrame(),
  getDiagnostics: () => adapter.getDiagnostics(),
  getSceneInfo: () => adapter.getSceneInfo(),
  setSelection: (box) => adapter.setSelection(box),
  setDiff: (diff) => adapter.setDiff(diff),
  highlightIssue: (issue) => adapter.highlightIssue(issue),
  probeVoxel: (pos) => adapter.probeVoxel(pos),
  pickVoxelAt: (x, y) => adapter.pickVoxelAt(x, y),
  projectToScreen: (pos) => adapter.projectToScreen(pos),
  probeScreenPoint,
  screenshotReady,
  parityCheck,
  roundTripPickCheck,
  elementRect,
  viewTop,
  viewOblique,
  viewCutaway,
  async loadFromQuery() {
    const params = new URLSearchParams(location.search);
    const scene = params.get("scene") || "../work/sample_render_scene.json";
    const result = await load(scene);
    const slice = params.get("slice");
    if (slice && /^\d+:\d+$/.test(slice)) {
      const [a, b] = slice.split(":").map(Number);
      await setSlice({ min: Math.min(a, b), max: Math.max(a, b) });
    }
    const view = params.get("view");
    if (view === "top") await viewTop();
    else if (view === "oblique_b") await viewOblique("b");
    return result;
  },
};

globalThis.LiteGardenReview = api;

// 页面打开后**自动加载**：没有用户操作也要能确定性产出图像。
// ?scene= 指定场景（默认 ../work/sample_render_scene.json）。
document.documentElement.dataset.reviewReady = "pending";
api
  .loadFromQuery()
  .then(() => {
    document.documentElement.dataset.reviewReady = "ready";
    document.documentElement.dataset.sceneHash = currentSceneHash() || "";
  })
  .catch((err) => {
    document.documentElement.dataset.reviewReady = "failed";
    document.documentElement.dataset.reviewError = `${err && err.code ? err.code : "ERROR"}: ${err && err.message ? err.message : err}`;
    console.error("[review_bridge] load failed", err);
  });
