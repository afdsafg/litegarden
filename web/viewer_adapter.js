// ===========================================================================
// web/viewer_adapter.js — LiteGardenViewer 适配层（Deepslate 0.10.1 / 上游 JSrender 派生）
// ===========================================================================
//
// 使命
// ----
// 把「锁定版本的上游渲染器」封装成**单实例、可回报、可诊断**的视图对象：
// 一个 canvas、一组受管理的监听器、一个渲染循环、一份可解释的 RenderReceipt。
//
// 上游事实（已逐字节核对 third_party/viewer/upstream/*，不要凭印象改）
// -----------------------------------------------------------------------
// * 上游是全局脚本风格，本文件通过 globalThis 访问它导出的全局：
//     deepslate / glMatrix / loadDeepslateResources / deepslateResources /
//     setStructure / render / webglContext / deepslateRenderer /
//     cameraPitch / cameraYaw / cameraPos
// * `new deepslate.StructureRenderer(gl, structure, resources, {chunkSize: 8})`
//   没有 dispose()。所以：**一个场景只构造一次渲染器**；切片变化调用
//   `renderer.setStructure(newStructure)`（重建几何缓冲，不重建着色器与贴图）。
// * `renderer.setViewport(x, y, w, h)` 设置 GL viewport 并**重新计算投影矩阵**；
//   `getPerspective()` 用的是 `gl.canvas.clientWidth / clientHeight`，所以
//   `canvas.width/height`（设备像素）与 CSS 尺寸必须分开设置，且宽度不能为 0。
// * `renderer.projMatrix` 是公开字段（投影矩阵），可直接用于射线拾取。
// * `Structure.addBlock(pos, name, props)` 越界**抛错**；`Structure` 的方块坐标
//   就是 crop 局部整数格。
// * `resources.getBlockProperties/getDefaultBlockProperties` 恒返回 null（上游如此），
//   因此渲染结果只由 blockstate/model/texture 决定。
// * 渲染器内部：`getBlockDefinition(name)` 返回 undefined 时该方块**静默不画**；
//   模型缺失时 `getBuffers` 抛错并被渲染器 try/catch 吞进 console.error。
//   两种都不允许在本项目里静默：见下面的资源探测与显示代理。
//
// 坐标系与单位（全文统一）
// ------------------------
//   p_local     = 项目坐标（project_local），选区/诊断/后端一律用它；可为负。
//   p_render    = p_local - crop_origin_local，裁剪盒内从 0 开始的整数格。
//   p_schematic = p_local + enclosing_min（本文件不涉及，后端负责）。
//   linear index（与上游 payload 一致）: i = x*size_y*size_z + y*size_z + z
//   渲染器、相机、网格、选区框、Diff 叠加、问题标记全部在 p_render 空间作图；
//   对外的 API 一律收/发 p_local，转换只发生在本文件的 toRender()/toLocal()。
//   camera_pos 是**参与 view matrix 的平移量**（crop 局部单位），不是 eye 位置。
//
// 本文件不做的事
// --------------
// * 不写蓝图：Diff 叠加、显示代理、选区框只存在于视图。
// * 不用 sleep 假装渲染完成：whenIdle 必须等到「资源就绪 + 当前场景建立 + 该
//   build 至少提交过一帧（gl.finish() 与 gl.getError() 都检查过）」。
// * 不把空白画面当成功：WebGL 不可用 → status = RENDER_UNAVAILABLE 并 reject。
// ===========================================================================
import { normalizeDiff, buildDiffBoxes, drawDiffBoxes } from "./diff_overlay.js";


const UPSTREAM = globalThis;

/** 上游与运行时的构建标识，参与 renderer_build_hash，也用于取证 manifest。 */
export const VIEWER_BUILD = Object.freeze({
  upstream_repo: "albertchen857/Litematica-viewer",
  upstream_commit: "65f41744eb372c8ffa40e23462fd13cb6168133f",
  deepslate: "0.10.1",
  gl_matrix: "3.4.3",
  chunk_size: 8,
  derived: ["upstream/deepslate-helpers.js", "upstream/viewer-lite.js"],
});

/** 视图状态机。空场景（合法）与加载失败必须能分开看。 */
export const RENDER_STATUS = Object.freeze({
  IDLE: "IDLE",
  LOADING: "RENDER_LOADING",
  READY: "READY",
  EMPTY: "RENDER_EMPTY",
  UNAVAILABLE: "RENDER_UNAVAILABLE",
  ERROR: "RENDER_ERROR",
  CONTEXT_LOST: "RENDER_CONTEXT_LOST",
  DISPOSED: "DISPOSED",
});

/** 显式错误码。whenIdle / setSlice / loadScene 的 reject 只带这些码。 */
export const ERROR_CODES = Object.freeze({
  BAD_ARGUMENT: "BAD_ARGUMENT",
  INVALID_SCENE: "INVALID_RENDER_SCENE",
  RENDER_UNAVAILABLE: "RENDER_UNAVAILABLE",
  RENDER_FAILED: "RENDER_FAILED",
  RENDER_TIMEOUT: "RENDER_TIMEOUT",
  RENDER_SUPERSEDED: "RENDER_SUPERSEDED",
  RENDER_CONTEXT_LOST: "RENDER_CONTEXT_LOST",
  RENDER_DISPOSED: "RENDER_DISPOSED",
  RENDER_EMPTY: "RENDER_EMPTY",
  SCENE_HASH_MISMATCH: "SCENE_HASH_MISMATCH",
  NO_SCENE: "NO_SCENE_LOADED",
});

/** 显示代理：用于「调色板下标越界 / 方块定义缺失」的体素，只存在于视图。 */
export const DISPLAY_PROXY_BLOCK = "minecraft:cake";

/** 空气家族（RenderScene 的 idx 里不应出现；出现了就计数并跳过）。 */
const AIR_NAMES = new Set(["minecraft:air", "minecraft:cave_air", "minecraft:void_air"]);

const DIAGNOSTIC_SAMPLE_LIMIT = 50;

// --- 相机 / 交互常量（与上游 createRenderCanvas 的量纲一致） -----------------
const CAMERA = Object.freeze({
  default_pitch: 0.8,
  default_yaw: 0.5,
  rotate_per_px: 1 / 200,   // 上游 pan(): direction / 200
  translate_per_px: 1 / 500, // 上游 move(): offset / 500
  dolly_per_wheel_px: 1 / 200, // 上游 move3d([0,0,-deltaY/200])
  key_step: 0.2,             // 上游 moveDist
});

const GL_ERROR_NAMES = Object.freeze({
  0x0500: "INVALID_ENUM",
  0x0501: "INVALID_VALUE",
  0x0502: "INVALID_OPERATION",
  0x0505: "OUT_OF_MEMORY",
  0x0506: "INVALID_FRAMEBUFFER_OPERATION",
  0x9242: "CONTEXT_LOST_WEBGL",
});

// ===========================================================================
// 小工具
// ===========================================================================

function fail(code, message, detail) {
  const err = new Error(message);
  err.code = code;
  if (detail !== undefined) err.detail = detail;
  return err;
}

function isInt(n) {
  return typeof n === "number" && Number.isInteger(n);
}

function isTriple(v) {
  return Array.isArray(v) && v.length === 3 && v.every(isInt);
}

function cloneTriple(v) {
  return [v[0], v[1], v[2]];
}

function clampInt(v, lo, hi) {
  return v < lo ? lo : v > hi ? hi : v;
}

function normalizeModelId(id) {
  const s = String(id);
  return s.includes(":") ? s : "minecraft:" + s;
}

/**
 * RenderScene 预检（前端自己的守卫，不替代后端校验）。
 * 只做「结构/类型/自洽性」检查：schema_version、坐标系、crop 与 bounds、palette、
 * idx/state 长度与取值范围、counts、empty 标志。
 * p_local 语义：crop_origin_local 为 project_local，idx 为 crop 局部线性下标。
 */
export function validateRenderScene(scene) {
  const errors = [];
  const warnings = [];
  const req = (cond, msg) => {
    if (!cond) errors.push(msg);
  };

  if (!scene || typeof scene !== "object" || Array.isArray(scene)) {
    return { ok: false, errors: ["RenderScene 必须是 JSON 对象"], warnings };
  }
  req(scene.schema_version === "0.2", `schema_version 必须是 "0.2"，实际 ${JSON.stringify(scene.schema_version)}`);
  req(typeof scene.scene_id === "string" && scene.scene_id.length > 0, "scene_id 缺失或不是非空字符串");
  req(typeof scene.scene_hash === "string" && scene.scene_hash.length > 0, "scene_hash 缺失或不是非空字符串");
  req(scene.coordinate_space === "project_local", `coordinate_space 必须是 "project_local"，实际 ${JSON.stringify(scene.coordinate_space)}`);
  req(isTriple(scene.crop_origin_local), "crop_origin_local 必须是 3 个整数（project_local）");
  req(isTriple(scene.size) && scene.size.every((n) => n > 0), "size 必须是 3 个正整数（crop 尺寸）");
  req(isInt(scene.minecraft_data_version), "minecraft_data_version 必须是整数");
  if (scene.file_sha256 != null) {
    req(typeof scene.file_sha256 === "string", "file_sha256 必须是字符串或省略");
  } else {
    warnings.push("payload 未带 file_sha256：本次渲染无法绑定候选文件字节");
  }
  if (scene.resource_hash != null) {
    req(typeof scene.resource_hash === "string", "resource_hash 必须是字符串或省略");
  } else {
    warnings.push("payload 未带 resource_hash：无法声明本次渲染使用的资源集");
  }

  const palette = scene.palette;
  if (!Array.isArray(palette) || palette.length === 0) {
    errors.push("palette 必须是非空数组");
  } else {
    palette.forEach((entry, i) => {
      if (!entry || typeof entry !== "object") {
        errors.push(`palette[${i}] 不是对象`);
        return;
      }
      if (typeof entry.name !== "string" || entry.name.length === 0) errors.push(`palette[${i}].name 缺失`);
      const props = entry.props;
      if (props != null && (typeof props !== "object" || Array.isArray(props))) errors.push(`palette[${i}].props 必须是对象`);
      else if (props) {
        for (const [k, v] of Object.entries(props)) {
          if (typeof v !== "string") errors.push(`palette[${i}].props.${k} 必须是字符串（BlockState 属性值）`);
        }
      }
    });
  }

  const idx = scene.idx;
  const state = scene.state;
  if (!Array.isArray(idx) || !Array.isArray(state)) {
    errors.push("idx / state 必须是数组");
  } else {
    if (idx.length !== state.length) errors.push(`idx(${idx.length}) 与 state(${state.length}) 长度不一致`);
    if (isTriple(scene.size)) {
      const volume = scene.size[0] * scene.size[1] * scene.size[2];
      let prev = -1;
      for (let k = 0; k < idx.length; k++) {
        const i = idx[k];
        if (!isInt(i) || i < 0 || i >= volume) {
          errors.push(`idx[${k}]=${i} 超出 crop 体积 [0,${volume})`);
          break;
        }
        if (i <= prev) {
          errors.push(`idx 必须严格升序且唯一：idx[${k}]=${i} 不大于前一个 ${prev}`);
          break;
        }
        prev = i;
      }
    }
    if (Array.isArray(palette) && palette.length) {
      for (let k = 0; k < state.length; k++) {
        const s = state[k];
        if (!isInt(s) || s < 0 || s >= palette.length) {
          warnings.push(`state[${k}]=${s} 越界（palette 长度 ${palette.length}）：该体素将用显示代理渲染并计入诊断`);
          break;
        }
      }
    }
  }

  const counts = scene.counts;
  if (!counts || typeof counts !== "object") {
    errors.push("counts 缺失");
  } else {
    if (!isInt(counts.non_air_voxels)) errors.push("counts.non_air_voxels 必须是整数");
    else if (Array.isArray(idx) && counts.non_air_voxels !== idx.length) {
      errors.push(`counts.non_air_voxels=${counts.non_air_voxels} 与 idx.length=${idx.length} 不一致`);
    }
    if (counts.voxels != null && !isInt(counts.voxels)) errors.push("counts.voxels 必须是整数");
  }

  const bounds = scene.full_scene_bounds;
  if (!bounds || typeof bounds !== "object") {
    errors.push("full_scene_bounds 缺失");
  } else {
    if (!isTriple(bounds.min)) errors.push("full_scene_bounds.min 必须是 3 个整数");
    if (!isTriple(bounds.max_exclusive)) errors.push("full_scene_bounds.max_exclusive 必须是 3 个整数");
    if (isTriple(bounds.min) && isTriple(bounds.max_exclusive) && isTriple(scene.crop_origin_local) && isTriple(scene.size)) {
      for (let a = 0; a < 3; a++) {
        if (bounds.max_exclusive[a] < bounds.min[a]) {
          errors.push(`full_scene_bounds 第 ${a} 轴 max_exclusive < min`);
          break;
        }
        const lo = scene.crop_origin_local[a];
        const hi = lo + scene.size[a];
        if (lo < bounds.min[a] || hi > bounds.max_exclusive[a]) {
          errors.push(`crop 第 ${a} 轴 [${lo},${hi}) 超出 full_scene_bounds [${bounds.min[a]},${bounds.max_exclusive[a]})`);
          break;
        }
      }
    }
  }

  const isEmpty = Array.isArray(idx) && idx.length === 0;
  if (scene.empty === true && !isEmpty) errors.push("empty=true 但 idx 非空");
  if (isEmpty && scene.empty !== true) warnings.push("idx 为空但未带 empty:true（按空选区处理）");
  // 空选区是合法状态，不是错误：调用方按 status=RENDER_EMPTY 区分「空」与「加载失败」

  return { ok: errors.length === 0, errors, warnings };
}

// ===========================================================================
// 适配层
// ===========================================================================

/**
 * @param {HTMLElement} container 中央视口元素（适配层在其中创建 canvas 与叠加层）
 * @param {object} [opts]
 *   opts.atlasUrl         贴图图集 URL（默认相对页面的 vendor 路径）
 *   opts.idleTimeoutMs    whenIdle 超时（默认 8000）
 *   opts.resetViewOnLoad  首次建立场景时是否把相机摆到默认斜视角（默认 true）
 *   opts.interactionMode  'navigate' | 'select' | 'pick'（默认 navigate）
 *   opts.transparent      背景透明（默认 false）
 */
export function createViewerAdapter(container, opts = {}) {
  if (!container || typeof container.appendChild !== "function") {
    throw fail(ERROR_CODES.BAD_ARGUMENT, "createViewerAdapter 需要一个容器元素");
  }

  const settings = {
    atlasUrl: opts.atlasUrl || "vendor/litematica-viewer/resource/atlas.png",
    idleTimeoutMs: Number.isFinite(opts.idleTimeoutMs) ? opts.idleTimeoutMs : 8000,
    resetViewOnLoad: opts.resetViewOnLoad !== false,
    interactionMode: opts.interactionMode || "navigate",
    transparent: opts.transparent === true,
  };

  const { mat4, vec3 } = UPSTREAM.glMatrix || {};
  const mat4Ok = !!(mat4 && vec3);

  // --- DOM -----------------------------------------------------------------
  const canvas = document.createElement("canvas");
  canvas.className = "viewer-canvas";
  canvas.setAttribute("tabindex", "0");
  canvas.setAttribute("aria-label", "三维体素视口（WebGL，可用鼠标与 WASD 导航）");
  container.appendChild(canvas);

  const overlay = document.createElement("canvas");
  overlay.className = "viewer-overlay";
  overlay.setAttribute("aria-hidden", "true");
  container.appendChild(overlay);
  const ctx2d = overlay.getContext("2d");

  const gl =
    canvas.getContext("webgl", {
      antialias: true,
      alpha: settings.transparent,
      depth: true,
      preserveDrawingBuffer: true, // 取证：截图/readPixels 需要确定性的绘制缓冲
      powerPreference: "default",
    }) || canvas.getContext("experimental-webgl");

  // viewer-lite.js 的全局 webglContext 就是渲染器的 gl：必须在任何 setStructure() 之前写好。
  if (gl) UPSTREAM.webglContext = gl;


  // --- 状态 ----------------------------------------------------------------
  const state = {
    status: RENDER_STATUS.IDLE,
    unavailable_reason: null,
    scene: null,
    /** 场景身份：scene_hash + crop + palette，用于 loadScene 幂等判断 */
    identity: null,
    buildId: 0,
    /** buildId -> 该 build 已提交帧数 */
    frames: new Map(),
    frameSequence: 0,
    lastFrame: null,
    buildHash: null,
    buildHashAlgo: null,
    rendererBuilds: 0,
    rendererReused: 0,
    contextLost: false,
    contextLostCount: 0,
    restoredCount: 0,
    glErrors: [],
    glErrorCount: 0,
    viewportDirty: true,
    css: { width: 0, height: 0 },
    dpr: 1,
    slice: null, // {min,max} crop 局部，含端点；null = 全范围
    sliceRequested: null,
    selection: null, // {min,max_exclusive} p_local
    selectionRender: null, // 裁剪到 crop 后的 p_render 盒（显示用）
    diff: null,
    diffBoxes: null,
    issue: null,
    hover: null,
    counters: null,
    resourceProbes: new Map(),
    swatches: null,
    observedDigests: null,
    notes: [],
    callSeq: 0,
    buildSeq: 0,
    disposed: false,
    display: { grid: true, hidden_layer_outlines: true, overlay_boxes: true, frustum_fade: true },
    plane_y: 0, // 框选工作平面（crop 局部 Y）
    drag: null,
    probeCache: null, // {linearToState: Map, hasVoxel(i): bool}
  };

  const listeners = new Map(); // event -> Set<fn>
  const waiters = new Set();
  let frameHandle = null;
  /** 当前按下的移动键（keydown/keyup 维护；输入控件获得焦点时不参与） */
  const pressedKeys = new Set();
  /** 适配层句柄：事件监听器与对象方法互相引用时用它，避免 this 绑定问题 */
  let api = null;
  let resourcesPromise = null;
  let lastPickEvent = 0;

  function emit(event, payload) {
    const set = listeners.get(event);
    if (!set) return;
    for (const fn of set) {
      try {
        fn(payload);
      } catch (err) {
        console.error("[viewer_adapter] listener error", err);
      }
    }
  }

  function note(msg) {
    if (!state.notes.includes(msg)) state.notes.push(msg);
  }

  // =========================================================================
  // 资源
  // =========================================================================

  function ensureResources() {
    if (resourcesPromise) return resourcesPromise;
    resourcesPromise = new Promise((resolve, reject) => {
      if (!mat4Ok) {
        reject(fail(ERROR_CODES.RENDER_UNAVAILABLE, "glMatrix 未加载：锁定版本的 vendor 脚本缺失或加载失败"));
        return;
      }
      if (typeof UPSTREAM.loadDeepslateResources !== "function") {
        reject(fail(ERROR_CODES.RENDER_UNAVAILABLE, "loadDeepslateResources 不存在：vendor/litematica-viewer/upstream 派生脚本未加载"));
        return;
      }
      if (typeof UPSTREAM.deepslate === "undefined") {
        reject(fail(ERROR_CODES.RENDER_UNAVAILABLE, "deepslate 未加载：vendor/litematica-viewer/resource/vendor/deepslate-0.10.1.js 缺失或加载失败"));
        return;
      }
      const img = new Image();
      img.decoding = "sync";
      img.onload = () => {
        try {
          // 上游函数：建立 blockDefinitions / blockModels / TextureAtlas 并赋给全局 deepslateResources
          const resources = UPSTREAM.loadDeepslateResources(img);
          if (!resources) throw new Error("loadDeepslateResources 返回空");
          state.atlas = { width: img.naturalWidth, height: img.naturalHeight };
          resolve(resources);
        } catch (err) {
          reject(fail(ERROR_CODES.RENDER_UNAVAILABLE, `贴图图集/资源建立失败：${err && err.message ? err.message : err}`));
        }
      };
      img.onerror = () => {
        reject(fail(ERROR_CODES.RENDER_UNAVAILABLE, `贴图图集加载失败：${settings.atlasUrl}`));
      };
      img.src = settings.atlasUrl;
    });
    return resourcesPromise;
  }

  function resources() {
    return UPSTREAM.deepslateResources || null;
  }

  /** 贴图 id 表（与 loadDeepslateResources 建 idMap 用的是同一份资源表）。 */
  let textureKeySet;
  function assetsTextureKeys() {
    if (textureKeySet !== undefined) return textureKeySet;
    try {
      // resource/assets.js 顶层 `const assets = ...`：全局词法绑定，模块里可直接引用名字。
      const a = typeof assets !== "undefined" ? assets : null;
      textureKeySet = a && a.textures ? new Set(Object.keys(a.textures)) : null;
    } catch (err) {
      textureKeySet = null;
    }
    return textureKeySet;
  }

  /**
   * 资源探测（每个调色板条目只做一次，结果缓存）。
   * 判定链与渲染器实际行为一致：getModelVariants → getBlockModel → 顶面贴图。
   * 返回 {status:'ok'|'missing_definition'|'unresolved_variant'|'missing_model'|'missing_texture'|'probe_error',
   *       missing_models:[], missing_textures:[], error?:string}
   */
  function probeEntry(name, props) {
    const key = name + "|" + JSON.stringify(props || {});
    const cached = state.resourceProbes.get(key);
    if (cached) return cached;
    const res = resources();
    const out = { status: "ok", missing_models: [], missing_textures: [] };
    if (!res) {
      out.status = "probe_error";
      out.error = "deepslateResources 尚未建立";
      state.resourceProbes.set(key, out);
      return out;
    }
    try {
      const def = res.getBlockDefinition(name);
      if (def === undefined || def === null) {
        out.status = "missing_definition";
      } else if (typeof def.getModelVariants !== "function") {
        out.status = "probe_error";
        out.error = "BlockDefinition 无 getModelVariants（版本不符）";
      } else {
        const variants = def.getModelVariants(props || {});
        if (!variants || variants.length === 0) {
          out.status = "unresolved_variant";
        } else {
          const table = assetsTextureKeys();
          for (const v of variants) {
            const modelId = normalizeModelId(v.model);
            const model = res.getBlockModel(modelId);
            if (!model) {
              out.missing_models.push(modelId);
              continue;
            }
            for (const tex of missingTexturesOf(model, res, table)) {
              if (!out.missing_textures.includes(tex)) out.missing_textures.push(tex);
            }
          }
          if (out.missing_models.length) out.status = "missing_model";
          else if (out.missing_textures.length) out.status = "missing_texture";
        }
      }
    } catch (err) {
      out.status = "probe_error";
      out.error = err && err.message ? err.message : String(err);
    }
    state.resourceProbes.set(key, out);
    return out;
  }

  /**
   * 模型引用的贴图是否在图集里。
   * 说明：Deepslate 0.10.1 的 TextureAtlas.getTextureUV() 对未知 id **不返回 undefined**，
   * 而是回落到 [0,0,part,part]（已核对源码），所以单靠它无法判定缺失。
   * 这里：优先用与 idMap 同源的 assets.textures 键表判定；键表不可用时才退回
   * 「getTextureUV 返回 undefined」这一判定（题目假设的行为）。
   */
  function missingTexturesOf(model, res, table) {
    const out = [];
    const refs = new Set();
    const elements = model.elements || [];
    for (const el of elements) {
      const faces = el && el.faces ? el.faces : {};
      for (const face of Object.values(faces)) {
        if (face && typeof face.texture === "string") refs.add(face.texture);
      }
    }
    for (const ref of refs) {
      let id;
      try {
        const resolved = typeof model.getTexture === "function" ? model.getTexture(ref) : ref;
        id = resolved ? String(resolved) : String(ref);
      } catch (err) {
        id = String(ref);
      }
      const key = id.replace(/^minecraft:/, "");
      if (table) {
        if (!table.has(key)) out.push(id);
      } else if (typeof res.getTextureUV === "function" && typeof res.getTextureUV(id) === "undefined") {
        out.push(id);
      }
    }
    return out;
  }

  // =========================================================================
  // 场景身份、线性下标、探测（p_local <-> p_render）
  // =========================================================================

  function toRender(pLocal) {
    const o = state.scene ? state.scene.crop_origin_local : [0, 0, 0];
    return [pLocal[0] - o[0], pLocal[1] - o[1], pLocal[2] - o[2]];
  }

  function toLocal(pRender) {
    const o = state.scene ? state.scene.crop_origin_local : [0, 0, 0];
    return [pRender[0] + o[0], pRender[1] + o[1], pRender[2] + o[2]];
  }

  function linearIndex(pRender) {
    const [sx, sy, sz] = state.scene.size;
    return pRender[0] * sy * sz + pRender[1] * sz + pRender[2];
  }

  function renderFromLinear(i) {
    const [sx, sy, sz] = state.scene.size;
    const layer = sy * sz;
    const x = Math.floor(i / layer);
    const rem = i - x * layer;
    const y = Math.floor(rem / sz);
    const z = rem - y * sz;
    return [x, y, z];
  }

  function buildProbeCache(scene) {
    const map = new Map();
    for (let k = 0; k < scene.idx.length; k++) map.set(scene.idx[k], scene.state[k]);
    state.probeCache = { linearToState: map };
  }

  function currentSliceRange() {
    const sy = state.scene ? state.scene.size[1] : 0;
    const req = state.slice || state.sliceRequested;
    if (!req) return { min: 0, max: Math.max(0, sy - 1), explicit: false };
    return {
      min: clampInt(req.min, 0, sy - 1),
      max: clampInt(req.max, 0, sy - 1),
      explicit: true,
    };
  }

  function isSubmittedRender(pRender) {
    if (!state.scene || !state.probeCache) return false;
    const [x, y, z] = pRender;
    const [sx, sy, sz] = state.scene.size;
    if (x < 0 || y < 0 || z < 0 || x >= sx || y >= sy || z >= sz) return false;
    const i = linearIndex(pRender);
    if (!state.probeCache.linearToState.has(i)) return false;
    const range = currentSliceRange();
    return y >= range.min && y <= range.max;
  }

  // =========================================================================
  // 视口尺寸（CSS 像素 × DPR）与投影
  // =========================================================================

  function applyViewport() {
    const rect = container.getBoundingClientRect();
    const width = Math.max(1, Math.round(rect.width));
    const height = Math.max(1, Math.round(rect.height));
    const dpr = Math.min(3, Math.max(1, UPSTREAM.devicePixelRatio || 1));
    state.css = { width, height };
    state.dpr = dpr;

    const pw = Math.max(1, Math.round(width * dpr));
    const ph = Math.max(1, Math.round(height * dpr));
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw; // 设备像素
      canvas.height = ph;
    }
    if (overlay.width !== pw || overlay.height !== ph) {
      overlay.width = pw;
      overlay.height = ph;
    }
    // CSS 尺寸与设备像素分开设置：Deepslate 的 getPerspective() 读 clientWidth/clientHeight
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    overlay.style.width = width + "px";
    overlay.style.height = height + "px";

    const renderer = UPSTREAM.deepslateRenderer;
    if (renderer && !state.contextLost) {
      // setViewport 设置 GL viewport 并重算投影矩阵（设备像素）
      renderer.setViewport(0, 0, pw, ph);
    }
    state.viewportDirty = false;
    emit("viewport", { width, height, dpr, canvas_width: pw, canvas_height: ph });
  }

  /** view matrix：与上游 render() 逐行同义（本项目自己画帧以便逐层控制可见性）。 */
  function computeViewMatrix() {
    const view = mat4.create();
    mat4.rotateX(view, view, UPSTREAM.cameraPitch);
    mat4.rotateY(view, view, UPSTREAM.cameraYaw);
    mat4.translate(view, view, UPSTREAM.cameraPos);
    return view;
  }

  function viewProjection() {
    const renderer = UPSTREAM.deepslateRenderer;
    if (!renderer) return null;
    const proj = renderer.projMatrix;
    if (!proj) return null;
    return mat4.multiply(mat4.create(), proj, computeViewMatrix());
  }

  // =========================================================================
  // 相机
  // =========================================================================

  function normalizeCamera(pitch, yaw) {
    // 与上游 render() 完全一致的归一化（JS 的 % 保留符号，幂等）
    return { yaw: yaw % (Math.PI * 2), pitch: Math.max(-Math.PI / 2, Math.min(Math.PI / 2, pitch)) };
  }

  function defaultCamera(size) {
    return {
      pitch: CAMERA.default_pitch,
      yaw: CAMERA.default_yaw,
      camera_pos: [-size[0] / 2, -size[1] / 2, -size[2] / 2],
    };
  }

  function setCameraInternal(camera) {
    const { pitch, yaw } = normalizeCamera(Number(camera.pitch), Number(camera.yaw));
    const p = camera.camera_pos;
    UPSTREAM.cameraPitch = pitch;
    UPSTREAM.cameraYaw = yaw;
    if (Array.isArray(p) && p.length === 3) vec3.set(UPSTREAM.cameraPos, Number(p[0]), Number(p[1]), Number(p[2]));
    emit("camera", getCamera());
  }

  /**
   * 相机初始化：
   *   reset=true（首次建立渲染器且 opts.resetViewOnLoad）→ 摆到上游 reset_view 的角度
   *   reset=false → 保留当前相机，只保证 pitch/yaw 有限、cameraPos 已分配
   * 无论哪条路径都走 setCameraInternal，保证写进上游全局的值与 getCamera() 报告的完全一致。
   */
  function ensureCameraInitialized(size, reset) {
    if (!UPSTREAM.cameraPos) UPSTREAM.cameraPos = vec3.create();
    if (reset || !Number.isFinite(UPSTREAM.cameraPitch) || !Number.isFinite(UPSTREAM.cameraYaw)) {
      setCameraInternal(defaultCamera(size));
      return;
    }
    setCameraInternal({
      pitch: UPSTREAM.cameraPitch,
      yaw: UPSTREAM.cameraYaw,
      camera_pos: [UPSTREAM.cameraPos[0], UPSTREAM.cameraPos[1], UPSTREAM.cameraPos[2]],
    });
  }


  function getCamera() {
    const pos = UPSTREAM.cameraPos ? [UPSTREAM.cameraPos[0], UPSTREAM.cameraPos[1], UPSTREAM.cameraPos[2]] : [0, 0, 0];
    const view = mat4Ok ? Array.from(computeViewMatrix()) : null;
    return {
      pitch: Number(UPSTREAM.cameraPitch) || 0,
      yaw: Number(UPSTREAM.cameraYaw) || 0,
      camera_pos: pos,
      viewport: { width: state.css.width, height: state.css.height, dpr: state.dpr },
      view_matrix: view,
      note: "camera_pos is the upstream view translation, not an eye position",
      coordinate_space: "crop_local（p_render；渲染器方块坐标从 0 开始）",
      units: "体素格（1 单位 = 1 方块）",
    };
  }

  /**
   * 把某个 p_local 点摆到画面中心（问题定位用）。
   * view = rotateX(pitch)·rotateY(yaw)·translate(camera_pos)，点 p_render 映射到
   * R·p_render + camera_pos；要让它落在原点前方 d 处：
   * camera_pos = -(R·p_render) + (0,0,-d)。
   */
  function focusOnPoint(pLocal, options = {}) {
    if (!state.scene || !mat4Ok) return;
    const pRender = toRender(pLocal);
    const distance = Number.isFinite(options.distance) ? options.distance : Math.max(6, state.scene.size[0] * 0.9);
    const rot = mat4.create();
    mat4.rotateX(rot, rot, UPSTREAM.cameraPitch);
    mat4.rotateY(rot, rot, UPSTREAM.cameraYaw);
    const rotated = vec3.transformMat4(vec3.create(), vec3.fromValues(pRender[0], pRender[1], pRender[2]), rot);
    const camera = {
      pitch: UPSTREAM.cameraPitch,
      yaw: UPSTREAM.cameraYaw,
      camera_pos: [-rotated[0], -rotated[1], -rotated[2] - distance],
    };
    if (Number.isFinite(options.pitch)) camera.pitch = options.pitch;
    if (Number.isFinite(options.yaw)) camera.yaw = options.yaw;
    setCameraInternal(camera);
    scheduleFrame();
  }

  // =========================================================================
  // 几何构建（切片 → Structure）与诊断计数
  // =========================================================================

  function emptyCounters(inputNonAir) {
    return {
      input_non_air: inputNonAir,
      accepted_real: 0,
      accepted_proxy: 0,
      clipped: 0,
      skipped_air: 0,
      out_of_range: 0,
      invalid_palette: 0,
      missing_definition: 0,
      missing_model: 0,
      missing_texture: 0,
      unresolved_variant: 0,
      probe_error: 0,
      addblock_error: 0,
      samples: {
        invalid_palette: [],
        out_of_range: [],
        skipped_air: [],
        missing_definition: [],
        missing_model: [],
        missing_texture: [],
        unresolved_variant: [],
        addblock_error: [],
      },
      proxy_states: [],
    };
  }

  function pushSample(counters, kind, entry) {
    const list = counters.samples[kind];
    if (list && list.length < DIAGNOSTIC_SAMPLE_LIMIT) list.push(entry);
  }

  /**
   * 依据当前切片建立 deepslate.Structure（**渲染坐标**：p_render）。
   * 不在调色板内 / 资源缺失的体素用 DISPLAY_PROXY_BLOCK 渲染并计入诊断；
   * 被切片裁掉的体素计入 clipped（正常行为，不是错误）。
   */
  function buildStructure(counters) {
    const scene = state.scene;
    const [sx, sy, sz] = scene.size;
    const range = currentSliceRange();
    const structure = new UPSTREAM.deepslate.Structure([sx, sy, sz]);
    const hidden = [];
    for (let y = 0; y < sy; y++) if (y < range.min || y > range.max) hidden.push(y);

    const volume = sx * sy * sz;
    for (let k = 0; k < scene.idx.length; k++) {
      const i = scene.idx[k];
      const stateIndex = scene.state[k];
      if (!isInt(i) || i < 0 || i >= volume) {
        counters.out_of_range++;
        pushSample(counters, "out_of_range", { linear_index: i, array_index: k });
        continue;
      }
      const pRender = renderFromLinear(i);
      if (pRender[1] < range.min || pRender[1] > range.max) {
        counters.clipped++;
        continue;
      }
      const pLocal = toLocal(pRender);
      const entry = scene.palette[stateIndex];
      if (!entry) {
        // 上游桥接会 `continue` 静默跳过；本项目必须显式计数并显示代理。
        counters.invalid_palette++;
        pushSample(counters, "invalid_palette", {
          linear_index: i,
          state_index: stateIndex,
          palette_size: scene.palette.length,
          pos_render: pRender,
          pos_local: pLocal,
        });
        addProxy(structure, pRender, counters, "invalid_palette_index");
        continue;
      }
      const name = entry.name;
      const props = entry.props && typeof entry.props === "object" ? entry.props : {};
      if (AIR_NAMES.has(name)) {
        // idx 只应包含非空气体素；出现空气说明 payload 与协议不符，计数但不可当方块提交。
        counters.skipped_air++;
        pushSample(counters, "skipped_air", { linear_index: i, state_index: stateIndex, pos_local: pLocal });
        continue;
      }
      const probe = probeEntry(name, props);
      if (probe.status === "ok") {
        addBlock(structure, pRender, name, props, counters, pLocal);
      } else {
        counters[probe.status]++;
        pushSample(counters, probe.status, {
          name,
          props,
          palette_index: stateIndex,
          pos_render: pRender,
          pos_local: pLocal,
          missing_models: probe.missing_models,
          missing_textures: probe.missing_textures,
          error: probe.error,
        });
        addProxy(structure, pRender, counters, probe.status);
      }
    }
    return { structure, range, hidden };
  }

  function addBlock(structure, pRender, name, props, counters, pLocal) {
    try {
      if (props && Object.keys(props).length) structure.addBlock(pRender, name, props);
      else structure.addBlock(pRender, name);
      counters.accepted_real++;
    } catch (err) {
      counters.addblock_error++;
      pushSample(counters, "addblock_error", {
        name,
        pos_render: pRender,
        pos_local: pLocal,
        error: err && err.message ? err.message : String(err),
      });
    }
  }

  function addProxy(structure, pRender, counters, reason) {
    try {
      structure.addBlock(pRender, DISPLAY_PROXY_BLOCK);
      counters.accepted_proxy++;
      if (!counters.proxy_states.includes(reason)) counters.proxy_states.push(reason);
    } catch (err) {
      counters.addblock_error++;
      pushSample(counters, "addblock_error", {
        name: DISPLAY_PROXY_BLOCK,
        pos_render: pRender,
        error: err && err.message ? err.message : String(err),
      });
    }
  }

  // =========================================================================
  // build hash（绑定「本次交给渲染器的几何 + 场景 + 资源」）
  // =========================================================================

  function fnv1a64(bytes) {
    // 退化路径：crypto.subtle 在非安全上下文不可用（例如用局域网 IP 打开页面）。
    let hash = 0xcbf29ce484222325n;
    const prime = 0x100000001b3n;
    const mask = 0xffffffffffffffffn;
    for (let i = 0; i < bytes.length; i++) {
      hash ^= BigInt(bytes[i]);
      hash = (hash * prime) & mask;
    }
    return { algo: "fnv1a64", hex: hash.toString(16).padStart(16, "0") };
  }

  async function digestBytes(bytes) {
    const subtle = UPSTREAM.crypto && UPSTREAM.crypto.subtle;
    if (subtle && typeof subtle.digest === "function") {
      try {
        const buf = await subtle.digest("SHA-256", bytes);
        const hex = Array.from(new Uint8Array(buf))
          .map((b) => b.toString(16).padStart(2, "0"))
          .join("");
        return { algo: "sha256", hex };
      } catch (err) {
        return fnv1a64(bytes);
      }
    }
    return fnv1a64(bytes);
  }

  function concatBytes(parts) {
    let len = 0;
    for (const p of parts) len += p.length;
    const out = new Uint8Array(len);
    let off = 0;
    for (const p of parts) {
      out.set(p, off);
      off += p.length;
    }
    return out;
  }

  async function computeBuildHash(scene, range, counters) {
    const enc = new TextEncoder();
    const header = JSON.stringify({
      v: 1,
      build: VIEWER_BUILD,
      scene_id: scene.scene_id,
      scene_hash: scene.scene_hash,
      file_sha256: scene.file_sha256 ?? null,
      resource_hash: scene.resource_hash ?? null,
      crop_origin_local: scene.crop_origin_local,
      size: scene.size,
      slice: [range.min, range.max],
      proxy: DISPLAY_PROXY_BLOCK,
      accepted_real: counters.accepted_real,
      accepted_proxy: counters.accepted_proxy,
      clipped: counters.clipped,
      proxy_reasons: counters.proxy_states,
    });
    const idx = new Int32Array(scene.idx);
    const st = new Int32Array(scene.state);
    const { algo, hex } = await digestBytes(
      concatBytes([enc.encode(header), new Uint8Array(idx.buffer), new Uint8Array(st.buffer)]),
    );
    return { algo, hex };
  }

  // =========================================================================
  // 帧与提交
  // =========================================================================

  function scheduleFrame() {
    if (state.disposed) return;
    if (frameHandle === null) frameHandle = UPSTREAM.requestAnimationFrame(frameTick);
  }

  function drainGlErrors() {
    if (!gl || state.contextLost) return;
    let guard = 0;
    for (;;) {
      const code = gl.getError();
      if (code === gl.NO_ERROR || code === 0) break;
      state.glErrorCount++;
      if (state.glErrors.length < 32) {
        state.glErrors.push({
          code,
          name: GL_ERROR_NAMES[code] || `0x${code.toString(16)}`,
          sequence: state.frameSequence,
          frame_build_id: state.buildId,
        });
      }
      if (++guard > 8) break;
    }
  }

  function drawFrame() {
    const renderer = UPSTREAM.deepslateRenderer;
    const view = computeViewMatrix();
    renderer.drawStructure(view);
    if (state.display.grid) renderer.drawGrid(view);
    // 上游额外能力：把被切片/hidden 的方块画成线框，避免"少画"被误解成"不存在"
    if (state.display.hidden_layer_outlines && typeof renderer.drawInvisibleBlocks === "function") {
      renderer.drawInvisibleBlocks(view);
    }
  }

  function frameTick() {
    frameHandle = null;
    if (state.disposed) return;
    if (state.contextLost) return;
    if (!UPSTREAM.deepslateRenderer || !state.scene) return;

    if (state.viewportDirty) applyViewport();
    if (state.css.width < 1 || state.css.height < 1) {
      note("视口尺寸为 0：等待布局，不提交帧（whenIdle 会超时而不是假装成功）");
      scheduleFrame();
      return;
    }

    let ok = true;
    try {
      drawFrame();
    } catch (err) {
      ok = false;
      state.status = RENDER_STATUS.ERROR;
      recordFailure(ERROR_CODES.RENDER_FAILED, `绘制失败：${err && err.message ? err.message : err}`);
    }
    if (ok) {
      // 有等待者（或显式 renderFrame 请求）时这一帧要"可取证"：gl.finish() 等 GPU 真正完成。
      const commit = waiters.size > 0;
      if (commit) {
        try {
          gl.finish();
        } catch (err) {
          /* 上下文丢失时 finish 可能抛错，由 contextlost 路径负责 */
        }
      }
      drainGlErrors(); // 每帧都检查（whenIdle 的兑现条件之一）
      state.frameSequence++;
      state.frames.set(state.buildId, (state.frames.get(state.buildId) || 0) + 1);
      state.lastFrame = {
        sequence: state.frameSequence,
        build_id: state.buildId,
        scene_hash: state.scene.scene_hash,
        finished: commit,
        at: Date.now(),
      };
      drawOverlay();
      settleWaiters();
      emit("frame", state.lastFrame);
    }
    if (pressedKeys.size > 0) scheduleFrame();
  }

  /**
   * 等待者两类：
   *   idle  — 要「当前场景（expected_scene_hash）当前 build 已提交过帧」；
   *   frame — renderFrame() 用，要「帧序号推进到 sequence_target」。
   * 场景被替换、上下文丢失、WebGL 不可用、绘制失败都会显式 reject（不是静默忽略）。
   */
  function settleWaiters() {
    if (waiters.size === 0) return;
    const currentHash = state.scene ? state.scene.scene_hash : null;
    const frames = state.frames.get(state.buildId) || 0;
    for (const waiter of Array.from(waiters)) {
      const done = (fn) => {
        waiters.delete(waiter);
        clearTimeout(waiter.timer);
        fn();
      };
      if (waiter.kind === "frame") {
        if (state.frameSequence >= waiter.sequence_target) done(() => waiter.resolve());
        continue;
      }
      if (waiter.expected_scene_hash !== currentHash) {
        done(() =>
          waiter.reject(
            fail(ERROR_CODES.RENDER_SUPERSEDED, `场景已切换到 ${currentHash}，等待的 ${waiter.expected_scene_hash} 已被取代`),
          ),
        );
        continue;
      }
      if (state.status === RENDER_STATUS.UNAVAILABLE) {
        done(() => waiter.reject(fail(ERROR_CODES.RENDER_UNAVAILABLE, state.unavailable_reason || "WebGL 不可用")));
        continue;
      }
      if (state.contextLost) {
        done(() => waiter.reject(fail(ERROR_CODES.RENDER_CONTEXT_LOST, "WebGL 上下文丢失")));
        continue;
      }
      if (state.status === RENDER_STATUS.ERROR) {
        done(() => waiter.reject(fail(ERROR_CODES.RENDER_FAILED, state.failure_message || "渲染失败")));
        continue;
      }
      if (frames >= 1) done(() => waiter.resolve(receipt()));
    }
  }

  function recordFailure(code, message) {
    state.failure_message = message;
    state.failure_code = code;
    state.notes.push(message);
  }

  function rejectAllWaiters(code, message) {
    for (const waiter of Array.from(waiters)) {
      waiters.delete(waiter);
      clearTimeout(waiter.timer);
      waiter.reject(fail(code, message));
    }
  }

  function receipt() {
    return {
      scene_hash: state.scene ? state.scene.scene_hash : null,
      file_sha256: state.scene ? state.scene.file_sha256 ?? null : null,
      renderer_build_hash: state.buildHash,
      resource_hash: state.scene ? state.scene.resource_hash ?? null : null,
      frames_rendered: state.frames.get(state.buildId) || 0,
      status: state.status,
      // 便于取证核对（不属于契约字段，但同源）：
      renderer_build_hash_algo: state.buildHashAlgo,
      build_id: state.buildId,
      scene_id: state.scene ? state.scene.scene_id : null,
      empty: state.status === RENDER_STATUS.EMPTY,
      last_frame_sequence: state.frameSequence,
    };
  }

  // =========================================================================
  // 叠加层（2D canvas）：选区 / Diff / 问题标记 / 悬停探测 / 工作平面
  // =========================================================================

  function projectPoint(pRender, mvp) {
    // mvp = proj * view，全部是列主序 mat4；输出 CSS 像素坐标 + NDC 深度
    const m = mvp;
    const x = pRender[0];
    const y = pRender[1];
    const z = pRender[2];
    const cx = m[0] * x + m[4] * y + m[8] * z + m[12];
    const cy = m[1] * x + m[5] * y + m[9] * z + m[13];
    const cz = m[2] * x + m[6] * y + m[10] * z + m[14];
    const cw = m[3] * x + m[7] * y + m[11] * z + m[15];
    if (cw <= 0.00001) return { x: 0, y: 0, z: 2, visible: false };
    const ndcX = cx / cw;
    const ndcY = cy / cw;
    const ndcZ = cz / cw;
    return {
      x: (ndcX * 0.5 + 0.5) * state.css.width,
      y: (1 - (ndcY * 0.5 + 0.5)) * state.css.height,
      z: ndcZ,
      visible: ndcZ >= -1 && ndcZ <= 1,
    };
  }

  function boxEdges(min, max) {
    const [x0, y0, z0] = min;
    const [x1, y1, z1] = max;
    const c = [
      [x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1],
      [x0, y1, z0], [x1, y1, z0], [x1, y1, z1], [x0, y1, z1],
    ];
    return [
      [c[0], c[1]], [c[1], c[2]], [c[2], c[3]], [c[3], c[0]],
      [c[4], c[5]], [c[5], c[6]], [c[6], c[7]], [c[7], c[4]],
      [c[0], c[4]], [c[1], c[5]], [c[2], c[6]], [c[3], c[7]],
    ];
  }

  function strokeEdges(ctx, mvp, min, max, style) {
    const edges = boxEdges(min, max);
    let drawn = 0;
    for (const [a, b] of edges) {
      const pa = projectPoint(a, mvp);
      const pb = projectPoint(b, mvp);
      if (!pa.visible && !pb.visible) continue;
      const depth = Math.min(pa.z, pb.z);
      const alpha = state.display.frustum_fade ? Math.max(0.25, Math.min(1, 1.15 - Math.max(-1, depth) * 0.45)) : 1;
      ctx.globalAlpha = alpha * (style.alpha ?? 1);
      ctx.beginPath();
      ctx.moveTo(pa.x, pa.y);
      ctx.lineTo(pb.x, pb.y);
      ctx.stroke();
      drawn++;
    }
    ctx.globalAlpha = 1;
    return drawn;
  }

  function drawOverlay() {
    if (!ctx2d || state.disposed) return;
    const mvp = viewProjection();
    ctx2d.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
    ctx2d.clearRect(0, 0, state.css.width, state.css.height);
    if (!mvp) return;
    ctx2d.lineWidth = 1.25;
    ctx2d.lineJoin = "round";

    // 1) Diff 叠加（只在视图里，不写蓝图）
    if (state.diffBoxes && state.display.overlay_boxes) {
      drawDiffBoxes(ctx2d, {
        boxes: state.diffBoxes.boxes,
        visible: state.diffVisible || null,
        project: (p) => projectPoint(p, mvp),
        fade: state.display.frustum_fade,
      });
    }

    // 2) 选区（显示裁剪到 crop 的部分；权威范围在 UI 文本里）
    if (state.selectionRender && state.display.overlay_boxes) {
      ctx2d.setLineDash([]);
      ctx2d.strokeStyle = "#ffd166";
      ctx2d.lineWidth = 1.5;
      strokeEdges(ctx2d, mvp, state.selectionRender.min, state.selectionRender.max_exclusive, {});
      // 底面对角十字，帮助判断内外面
      const s = state.selectionRender;
      const mid = [
        (s.min[0] + s.max_exclusive[0]) / 2,
        s.min[1],
        (s.min[2] + s.max_exclusive[2]) / 2,
      ];
      const corners = [
        [s.min[0], s.min[1], s.min[2]],
        [s.max_exclusive[0], s.min[1], s.max_exclusive[2]],
      ];
      ctx2d.strokeStyle = "rgba(255,209,102,0.45)";
      for (const c of corners) {
        const pc = projectPoint(c, mvp);
        const pm = projectPoint(mid, mvp);
        if (!pc.visible || !pm.visible) continue;
        ctx2d.beginPath();
        ctx2d.moveTo(pc.x, pc.y);
        ctx2d.lineTo(pm.x, pm.y);
        ctx2d.stroke();
      }
    }

    // 3) 工作平面预览（框选拖动中）
    if (state.drag && state.drag.kind === "plane" && state.drag.preview) {
      const p = state.drag.preview;
      ctx2d.setLineDash([5, 4]);
      ctx2d.strokeStyle = "#ffd166";
      ctx2d.lineWidth = 1.25;
      const y = state.drag.plane_y;
      const prismMin = [p.min[0], y, p.min[2]];
      const prismMax = [p.max[0], y + 1, p.max[2]];
      strokeEdges(ctx2d, mvp, prismMin, prismMax, { alpha: 0.85 });
      ctx2d.setLineDash([]);
    }

    // 4) 悬停 / 拾取高亮
    if (state.hover && state.hover.pos_render && state.display.overlay_boxes) {
      const h = state.hover;
      ctx2d.strokeStyle = state.interactionMode === "navigate" ? "rgba(120,190,255,0.7)" : "#4fb3ff";
      ctx2d.lineWidth = 1.5;
      ctx2d.setLineDash([]);
      ctx2d.globalAlpha = 0.9;
      strokeEdges(ctx2d, mvp, h.pos_render, [h.pos_render[0] + 1, h.pos_render[1] + 1, h.pos_render[2] + 1], {});
      ctx2d.globalAlpha = 1;
    }

    // 5) 问题标记：从体素顶面竖直拉起一根针 + 顶部小方块
    if (state.issue && state.issue.pos_local) {
      const pRender = toRender(state.issue.pos_local);
      const base = [pRender[0] + 0.5, pRender[1] + 1, pRender[2] + 0.5];
      const tip = [base[0], base[1] + 2.4, base[2]];
      const pb = projectPoint(base, mvp);
      const pt = projectPoint(tip, mvp);
      if (pb.visible || pt.visible) {
        ctx2d.strokeStyle = "#f06a5f";
        ctx2d.setLineDash([]);
        ctx2d.lineWidth = 1.5;
        ctx2d.beginPath();
        ctx2d.moveTo(pb.x, pb.y);
        ctx2d.lineTo(pt.x, pt.y);
        ctx2d.stroke();
        ctx2d.fillStyle = "#f06a5f";
        ctx2d.beginPath();
        ctx2d.arc(pt.x, pt.y, 3.5, 0, Math.PI * 2);
        ctx2d.fill();
        const label = String(state.issue.code || "ISSUE");
        ctx2d.font = "600 11px ui-monospace, Consolas, monospace";
        const w = ctx2d.measureText(label).width + 10;
        ctx2d.fillStyle = "rgba(12,14,17,0.85)";
        ctx2d.fillRect(pt.x + 6, pt.y - 16, w, 16);
        ctx2d.strokeStyle = "#f06a5f";
        ctx2d.lineWidth = 1;
        ctx2d.strokeRect(pt.x + 6.5, pt.y - 15.5, w - 1, 15);
        ctx2d.fillStyle = "#ffd9d5";
        ctx2d.fillText(label, pt.x + 11, pt.y - 4);
      }
    }
  }

  // =========================================================================
  // loadScene
  // =========================================================================

  function sceneIdentity(scene) {
    return [
      scene.scene_hash,
      scene.crop_origin_local.join(","),
      scene.size.join(","),
      scene.palette.length,
      scene.palette.map((p) => p.name + "|" + Object.keys(p.props || {}).sort().join("&")).join(";"),
      scene.resource_hash ?? "",
      scene.file_sha256 ?? "",
    ].join("#");
  }


  api = {
    // --- 只读元信息（UI 用） ------------------------------------------------
    get element() {
      return canvas;
    },
    get overlayElement() {
      return overlay;
    },

    getSceneInfo() {
      if (!state.scene) return null;
      const s = state.scene;
      const range = currentSliceRange();
      return {
        scene_id: s.scene_id,
        scene_hash: s.scene_hash,
        file_sha256: s.file_sha256 ?? null,
        resource_hash: s.resource_hash ?? null,
        coordinate_space: s.coordinate_space,
        crop_origin_local: cloneTriple(s.crop_origin_local),
        size: cloneTriple(s.size),
        crop_bounds_local: {
          min: cloneTriple(s.crop_origin_local),
          max_exclusive: [
            s.crop_origin_local[0] + s.size[0],
            s.crop_origin_local[1] + s.size[1],
            s.crop_origin_local[2] + s.size[2],
          ],
        },
        full_scene_bounds: {
          min: cloneTriple(s.full_scene_bounds.min),
          max_exclusive: cloneTriple(s.full_scene_bounds.max_exclusive),
        },
        minecraft_data_version: s.minecraft_data_version,
        palette: s.palette.map((p) => ({ name: p.name, props: { ...(p.props || {}) } })),
        counts: { ...s.counts },
        empty: s.empty === true || s.idx.length === 0,
        slice_range: { ...range, axis: "y", space: "crop_local" },
        status: state.status,
      };
    },

    getStatus() {
      return state.status;
    },

    // --- 契约方法 ----------------------------------------------------------
    /**
     * 加载并渲染一个 RenderScene（幂等：同一场景身份不重建）。
     * 解析：> 资源就绪 → 建几何（复用渲染器）→ 提交一帧 → RenderReceipt。
     */
    async loadScene(scene) {
      const seq = ++state.callSeq;
      if (state.disposed) throw fail(ERROR_CODES.RENDER_DISPOSED, "适配层已 dispose");
      const check = validateRenderScene(scene);
      if (!check.ok) {
        state.status = RENDER_STATUS.ERROR;
        recordFailure(ERROR_CODES.INVALID_SCENE, "RenderScene 预检失败：" + check.errors.join("；"));
        throw fail(ERROR_CODES.INVALID_SCENE, "RenderScene 预检失败", check.errors);
      }
      if (!gl) {
        state.status = RENDER_STATUS.UNAVAILABLE;
        state.unavailable_reason = "canvas.getContext('webgl') 返回空：本机/本浏览器没有可用的 WebGL 上下文";
        throw fail(ERROR_CODES.RENDER_UNAVAILABLE, state.unavailable_reason);
      }
      if (state.contextLost) {
        state.status = RENDER_STATUS.CONTEXT_LOST;
        throw fail(ERROR_CODES.RENDER_CONTEXT_LOST, "WebGL 上下文已丢失，需重新加载页面");
      }
      if (check.warnings.length) for (const w of check.warnings) note(w);

      // 幂等：同场景身份直接返回现有 receipt，不重建、不重新加载资源
      const identity = sceneIdentity(scene);
      if (state.identity === identity && state.scene && state.frames.has(state.buildId)) {
        if (state.status !== RENDER_STATUS.CONTEXT_LOST) {
          return receipt();
        }
      }

      state.status = RENDER_STATUS.LOADING;
      rejectAllWaiters(ERROR_CODES.RENDER_SUPERSEDED, "新的 loadScene 取代了等待中的旧请求");
      emit("status", { status: state.status, scene_hash: scene.scene_hash });

      let res;
      try {
        res = await ensureResources();
      } catch (err) {
        if (seq !== state.callSeq) throw fail(ERROR_CODES.RENDER_SUPERSEDED, "loadScene 已被更新的调用取代");
        state.status = RENDER_STATUS.UNAVAILABLE;
        state.unavailable_reason = err && err.message ? err.message : String(err);
        throw err;
      }
      if (seq !== state.callSeq) throw fail(ERROR_CODES.RENDER_SUPERSEDED, "loadScene 已被更新的调用取代");
      if (state.disposed) throw fail(ERROR_CODES.RENDER_DISPOSED, "适配层已 dispose");
      void res;

      // 接受场景
      state.scene = scene;
      state.identity = identity;
      state.selectionRender = null;
      state.hover = null;
      state.issue = null;
      state.diff = null;
      state.diffBoxes = null;
      state.resourceProbes.clear();
      state.swatches = null;
      state.probeCache = null;
      state.frames.clear();
      state.notes.length = 0;
      for (const w of check.warnings) note(w);
      buildProbeCache(scene);
      if (!state.sliceRequested) state.slice = null;

      // 几何构建
      const counters = emptyCounters(scene.idx.length);
      state.counters = counters;
      const { structure, range, hidden } = buildStructure(counters);
      state.hidden = hidden;
      state.range = range;
      state.plane_y = clampInt(state.plane_y, range.min, Math.max(range.min, range.max));

      // 渲染器：只在需要时构造一次
      ensureCameraInitialized(scene.size, settings.resetViewOnLoad && state.rendererBuilds === 0);
      applyViewport();
      if (!UPSTREAM.deepslateRenderer) {
        state.rendererBuilds++;
        UPSTREAM.setStructure(structure, settings.resetViewOnLoad);
      } else {
        state.rendererReused++;
        UPSTREAM.deepslateRenderer.setStructure(structure);
      }
      state.buildId++;
      state.frames.set(state.buildId, 0);
      if (UPSTREAM.deepslateRenderer && !state.contextLost) {
        UPSTREAM.deepslateRenderer.setViewport(0, 0, canvas.width, canvas.height);
      }

      const { algo, hex } = await computeBuildHash(scene, range, counters);
      state.buildHash = hex;
      state.buildHashAlgo = algo;

      state.status = counters.input_non_air === 0 ? RENDER_STATUS.EMPTY : RENDER_STATUS.READY;
      computeDisplaySwatches();
      analyzeObservedResources();
      void observeResourceDigests();
      emit("status", { status: state.status, scene_hash: scene.scene_hash });
      emit("scene", api.getSceneInfo());

      // 兑现条件：至少提交一帧
      scheduleFrame();
      return await api.whenIdle(scene.scene_hash);
    },

    setCamera(camera) {
      if (state.disposed) return;
      if (!camera || typeof camera !== "object") return;
      setCameraInternal(camera);
      if (scene_ready()) applyViewport();
      scheduleFrame();
    },

    getCamera() {
      return getCamera();
    },

    focusOnPoint(pLocal, options) {
      focusOnPoint(pLocal, options);
    },

    /** 切片（crop 局部 Y，含端点）；null = 全范围。解析于该 build 提交首帧之后。 */
    async setSlice(slice) {
      if (state.disposed) throw fail(ERROR_CODES.RENDER_DISPOSED, "适配层已 dispose");
      if (slice !== null && !(slice && isInt(slice.min) && isInt(slice.max))) {
        throw fail(ERROR_CODES.BAD_ARGUMENT, "setSlice 需要 {min,max} 整数（crop 局部 Y，含端点）或 null");
      }
      state.sliceRequested = slice ? { min: slice.min, max: slice.max } : null;
      if (!state.scene || !UPSTREAM.deepslateRenderer) {
        state.slice = state.sliceRequested;
        return;
      }
      const previous = state.slice;
      state.slice = state.sliceRequested ? { min: slice.min, max: slice.max } : null;
      // 切片变化 = 重建几何（复用同一个渲染器）
      const counters = emptyCounters(state.scene.idx.length);
      const { structure, range, hidden } = buildStructure(counters);
      state.counters = counters;
      state.hidden = hidden;
      state.range = range;
      state.plane_y = clampInt(state.plane_y, range.min, Math.max(range.min, range.max));
      try {
        UPSTREAM.deepslateRenderer.setStructure(structure);
      } catch (err) {
        if (previous) state.slice = previous;
        state.status = RENDER_STATUS.ERROR;
        recordFailure(ERROR_CODES.RENDER_FAILED, `切片重建失败：${err && err.message ? err.message : err}`);
        throw fail(ERROR_CODES.RENDER_FAILED, state.failure_message);
      }
      state.rendererReused++;
      state.buildId++;
      state.frames.set(state.buildId, 0);
      const { hex, algo } = await computeBuildHash(state.scene, range, counters);
      state.buildHash = hex;
      state.buildHashAlgo = algo;
      state.status = counters.input_non_air === 0 ? RENDER_STATUS.EMPTY : RENDER_STATUS.READY;
      emit("slice", { slice: state.slice, range });
      scheduleFrame();
      // 等这一 build 的首帧提交，保证后续截图/取证对得上新切片
      await api.whenIdle(state.scene.scene_hash);
    },

    /** 选区（p_local，半开区间 [min, max_exclusive)）。仅显示用，不写蓝图。 */
    setSelection(box) {
      if (!state.scene) {
        state.selection = null;
        state.selectionRender = null;
        scheduleFrame();
        return;
      }
      if (!box) {
        state.selection = null;
        state.selectionRender = null;
        drawOverlay();
        scheduleFrame();
        return;
      }
      const min = cloneTriple(box.min);
      const maxEx = cloneTriple(box.max_exclusive);
      state.selection = { min, max_exclusive: maxEx, coordinate_space: "project_local" };
      const rMin = toRender(min);
      const rMax = toRender(maxEx);
      const [sx, sy, sz] = state.scene.size;
      const cMin = [clampInt(rMin[0], 0, sx), clampInt(rMin[1], 0, sy), clampInt(rMin[2], 0, sz)];
      const cMax = [clampInt(rMax[0], 0, sx), clampInt(rMax[1], 0, sy), clampInt(rMax[2], 0, sz)];
      const inside = cMax[0] > cMin[0] && cMax[1] > cMin[1] && cMax[2] > cMin[2];
      state.selectionRender = inside
        ? {
            min: cMin,
            // 选区是半开区间 [min, max_exclusive)：体素格正好落在 min..max_exclusive 之间
            max_exclusive: cMax,
            clipped: cMin[0] !== rMin[0] || cMax[0] !== rMax[0] || cMin[1] !== rMin[1] || cMax[1] !== rMax[1] || cMin[2] !== rMin[2] || cMax[2] !== rMax[2],
          }
        : null;
      drawOverlay();
      scheduleFrame();
    },

    /** Diff 叠加（后端净变化；纯显示，绝不写回蓝图，也不加标记方块）。 */
    setDiff(diff) {
      if (!diff) {
        state.diff = null;
        state.diffBoxes = null;
        drawOverlay();
        scheduleFrame();
        return;
      }
      const normalized = normalizeDiff(diff);
      state.diff = normalized;
      if (!normalized.ok) {
        state.diffBoxes = null;
        note("Diff 载荷非法：" + normalized.errors.join("；"));
      } else if (state.scene) {
        state.diffBoxes = buildDiffBoxes(normalized, {
          size: state.scene.size,
          crop_origin_local: state.scene.crop_origin_local,
        });
      }
      drawOverlay();
      scheduleFrame();
    },

    setDiffVisibility(categories) {
      state.diffVisible = categories && categories.length ? new Set(categories) : null;
      drawOverlay();
      scheduleFrame();
    },

    /** 问题标记（p_local）。 */
    highlightIssue(issue) {
      if (!issue || !issue.pos_local) {
        state.issue = null;
      } else {
        state.issue = { code: issue.code || "ISSUE", pos_local: cloneTriple(issue.pos_local), detail: issue };
      }
      drawOverlay();
      scheduleFrame();
    },

    /** 本地显示数据查询（权威查询在后端）。 */
    probeVoxel(posLocal) {
      const info = {
        query: cloneTriple(posLocal),
        coordinate_space: "project_local",
        in_full_bounds: false,
        in_crop: false,
        pos_render: null,
        linear_index: null,
        palette_index: null,
        display_state: null,
        render_state: null,
        renderable: false,
        hidden_by_slice: false,
        state_source: "no_scene",
        note: "本地显示查询：不代表后端权威体素查询，也不代表真实碰撞",
      };
      if (!state.scene) return info;
      const scene = state.scene;
      const b = scene.full_scene_bounds;
      info.in_full_bounds =
        posLocal[0] >= b.min[0] && posLocal[0] < b.max_exclusive[0] &&
        posLocal[1] >= b.min[1] && posLocal[1] < b.max_exclusive[1] &&
        posLocal[2] >= b.min[2] && posLocal[2] < b.max_exclusive[2];
      const pRender = toRender(posLocal);
      info.pos_render = pRender;
      const [sx, sy, sz] = scene.size;
      info.in_crop = pRender[0] >= 0 && pRender[0] < sx && pRender[1] >= 0 && pRender[1] < sy && pRender[2] >= 0 && pRender[2] < sz;
      if (!info.in_crop) {
        info.state_source = "outside_crop";
        return info;
      }
      const i = linearIndex(pRender);
      info.linear_index = i;
      const stateIndex = state.probeCache.linearToState.get(i);
      if (stateIndex === undefined) {
        info.state_source = "air";
        info.display_state = { name: "minecraft:air", props: {} };
        return info;
      }
      info.palette_index = stateIndex;
      const entry = scene.palette[stateIndex];
      const range = currentSliceRange();
      info.hidden_by_slice = pRender[1] < range.min || pRender[1] > range.max;
      if (!entry) {
        info.state_source = "invalid_palette_index";
        info.render_state = { name: DISPLAY_PROXY_BLOCK, props: {}, proxy: true, proxy_reason: "invalid_palette_index" };
        info.renderable = !info.hidden_by_slice;
        return info;
      }
      info.display_state = { name: entry.name, props: { ...(entry.props || {}) } };
      const probe = probeEntry(entry.name, entry.props || {});
      if (probe.status === "ok") {
        info.state_source = "scene";
        info.render_state = { name: entry.name, props: { ...(entry.props || {}) }, proxy: false };
      } else {
        info.state_source = "display_proxy";
        info.render_state = {
          name: DISPLAY_PROXY_BLOCK,
          props: {},
          proxy: true,
          proxy_reason: probe.status,
          missing_models: probe.missing_models,
          missing_textures: probe.missing_textures,
        };
      }
      info.renderable = !info.hidden_by_slice;
      return info;
    },

    /**
     * 屏幕坐标 → 体素（射线 + 网格相交，Amanatides & Woo DDA）。
     * 只用真实矩阵：inv(projMatrix × viewMatrix) 反投影出射线，再在 crop 体素网格上步进，
     * 命中「当前 build 已提交的体素」（含显示代理）。cssX/cssY 是相对 canvas 的 CSS 像素。
     */
    pickVoxelAt(cssX, cssY) {
      const ray = rayFromScreen(cssX, cssY);
      if (!ray) return null;
      return marchRay(ray, { requireSubmitted: true });
    },

    /** 屏幕坐标 → 水平工作平面上的点（用于框选矩形，确定且不依赖相机朝向）。 */
    pickPlaneAt(cssX, cssY, planeY) {
      const ray = rayFromScreen(cssX, cssY);
      if (!ray) return null;
      const y = Number.isFinite(planeY) ? planeY : state.plane_y;
      const dy = ray.direction[1];
      if (Math.abs(dy) < 1e-6) return null;
      const t = (y - ray.origin[1]) / dy;
      if (t <= 0) return null;
      const point = [
        ray.origin[0] + ray.direction[0] * t,
        ray.origin[1] + ray.direction[1] * t,
        ray.origin[2] + ray.direction[2] * t,
      ];
      if (!state.scene) return null;
      const [sx, sy, sz] = state.scene.size;
      if (point[0] < 0 || point[0] > sx || point[2] < 0 || point[2] > sz) return null;
      return {
        pos_render: [point[0], point[1], point[2]],
        pos_local: toLocal(point),
        cell: [Math.min(sx - 1, Math.max(0, Math.floor(point[0]))), clampInt(Math.floor(y), 0, sy - 1), Math.min(sz - 1, Math.max(0, Math.floor(point[2])))],
      };
    },

    /** 观测到的资源摘要（前端无法复算后端的 resource_hash 算法，只报观测值）。 */
    async getResourceDigests() {
      return observeResourceDigests();
    },

    getDiagnostics() {
      return buildDiagnostics();
    },

    /**
     * 兑现条件（全部满足才 resolve）：
     *   ① 资源加载完成（deepslateResources 已建立）；
     *   ② 当前场景 = expectedSceneHash，且几何已建立；
     *   ③ 该 build 至少提交过一帧，且这一帧走过 gl.finish() 与 gl.getError()。
     * 显式 reject：hash 不匹配 / 无场景 / WebGL 不可用 / 上下文丢失 / 绘制失败 /
     * 场景被取代 / 超时 / 已 dispose。
     */
    whenIdle(expectedSceneHash) {
      return new Promise((resolve, reject) => {
        if (state.disposed) {
          reject(fail(ERROR_CODES.RENDER_DISPOSED, "适配层已 dispose"));
          return;
        }
        if (typeof expectedSceneHash !== "string" || !expectedSceneHash) {
          reject(fail(ERROR_CODES.BAD_ARGUMENT, "whenIdle 需要 expectedSceneHash 字符串"));
          return;
        }
        if (!gl || state.status === RENDER_STATUS.UNAVAILABLE) {
          reject(fail(ERROR_CODES.RENDER_UNAVAILABLE, state.unavailable_reason || "WebGL 不可用"));
          return;
        }
        if (state.contextLost) {
          reject(fail(ERROR_CODES.RENDER_CONTEXT_LOST, "WebGL 上下文丢失"));
          return;
        }
        if (!state.scene) {
          reject(fail(ERROR_CODES.NO_SCENE, "whenIdle 在 loadScene 成功之前被调用"));
          return;
        }
        if (state.scene.scene_hash !== expectedSceneHash) {
          reject(
            fail(
              ERROR_CODES.SCENE_HASH_MISMATCH,
              `期望渲染 ${expectedSceneHash}，当前场景是 ${state.scene.scene_hash}`,
            ),
          );
          return;
        }
        if (!UPSTREAM.deepslateRenderer) {
          reject(fail(ERROR_CODES.RENDER_FAILED, "渲染器未建立（几何构建失败）"));
          return;
        }
        const waiter = {
          kind: "idle",
          expected_scene_hash: expectedSceneHash,
          resolve,
          reject,
          timer: null,
        };
        waiter.timer = setTimeout(() => {
          waiters.delete(waiter);
          reject(
            fail(
              ERROR_CODES.RENDER_TIMEOUT,
              `whenIdle 超时（${settings.idleTimeoutMs}ms）：未能在期限内提交 scene_hash=${expectedSceneHash} 的帧`,
            ),
          );
        }, settings.idleTimeoutMs);
        waiters.add(waiter);
        scheduleFrame();
        // 已经提交过帧的情况立即兑现（否则等上面调度的这一帧）
        if ((state.frames.get(state.buildId) || 0) >= 1) settleWaiters();
      });
    },

    /** 强制渲染并同步到 GPU 一帧（取证/截图前调用）。 */
    renderFrame() {
      return new Promise((resolve, reject) => {
        if (state.disposed) {
          reject(fail(ERROR_CODES.RENDER_DISPOSED, "适配层已 dispose"));
          return;
        }
        if (state.contextLost) {
          reject(fail(ERROR_CODES.RENDER_CONTEXT_LOST, "WebGL 上下文丢失"));
          return;
        }
        if (!state.scene || !UPSTREAM.deepslateRenderer) {
          reject(fail(ERROR_CODES.NO_SCENE, "没有可渲染的场景"));
          return;
        }
        const waiter = {
          kind: "frame",
          sequence_target: state.frameSequence + 1,
          resolve: () => resolve(),
          reject,
          timer: null,
        };
        waiter.timer = setTimeout(() => {
          waiters.delete(waiter);
          reject(fail(ERROR_CODES.RENDER_TIMEOUT, "renderFrame 超时：未能推进帧序号"));
        }, settings.idleTimeoutMs);
        waiters.add(waiter);
        scheduleFrame();
      });
    },

    // --- 交互 / 显示（扩展方法，非契约字段） ------------------------------
    setInteractionMode(mode) {
      if (!["navigate", "select", "pick"].includes(mode)) return;
      state.interactionMode = mode;
      emit("mode", { mode });
      drawOverlay();
    },
    getInteractionMode() {
      return state.interactionMode;
    },
    setWorkPlaneY(y) {
      const [sy] = state.scene ? [state.scene.size[1]] : [0];
      state.plane_y = clampInt(Math.round(y), 0, Math.max(0, sy - 1));
      emit("plane", { y: state.plane_y });
    },
    getWorkPlaneY() {
      return state.plane_y;
    },
    setDisplayOption(name, value) {
      if (!(name in state.display)) return;
      state.display[name] = !!value;
      drawOverlay();
      scheduleFrame();
    },
    getDisplayOptions() {
      return { ...state.display };
    },
    on(event, fn) {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event).add(fn);
      return () => listeners.get(event)?.delete(fn);
    },
    getDisplaySwatches() {
      return state.swatches;
    },

    /**
     * p_local → CSS 像素坐标（用真实的 projMatrix × view matrix）。
     * 取证用：可以和 pickVoxelAt() 做一致性自检（见 review_bridge.roundTripPickCheck）。
     */
    projectToScreen(posLocal) {
      const mvp = viewProjection();
      if (!mvp || !state.scene) return null;
      const p = projectPoint(toRender(posLocal), mvp);
      return {
        x: p.x,
        y: p.y,
        depth: p.z,
        visible: p.visible,
        width: state.css.width,
        height: state.css.height,
        coordinate_space: "css_px（相对 canvas 左上角）",
      };
    },

    /** 读取当前帧像素并返回摘要（取证用；需要 preserveDrawingBuffer）。 */
    readFramePixels() {
      if (!gl || state.contextLost || state.css.width < 1) return null;
      const w = canvas.width;
      const h = canvas.height;
      const pixels = new Uint8Array(w * h * 4);
      gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      const drain = [];
      let code = gl.getError();
      let guard = 0;
      while (code !== gl.NO_ERROR && guard++ < 8) {
        drain.push(GL_ERROR_NAMES[code] || `0x${code.toString(16)}`);
        code = gl.getError();
      }
      return { width: w, height: h, pixels, errors: drain };
    },

    dispose() {
      if (state.disposed) return;
      state.disposed = true;
      state.status = RENDER_STATUS.DISPOSED;
      rejectAllWaiters(ERROR_CODES.RENDER_DISPOSED, "适配层已 dispose");
      if (frameHandle !== null) {
        UPSTREAM.cancelAnimationFrame(frameHandle);
        frameHandle = null;
      }
      removeInputListeners();
      if (resizeObserver) resizeObserver.disconnect();
      UPSTREAM.removeEventListener("resize", onWindowResize);
      removeDprListener();
      listeners.clear();
      // 上游渲染器没有 dispose：显式释放上下文是唯一可用的释放手段。
      try {
        const lose = gl && gl.getExtension("WEBGL_lose_context");
        if (lose) lose.loseContext();
      } catch (err) {
        /* 忽略：上下文可能已丢失 */
      }
      canvas.remove();
      overlay.remove();
    },
  };

  return api;

  // =========================================================================
  // 内部辅助（函数声明会提升，可在上面的对象字面量里引用）

  function scene_ready() {
    return !!state.scene && !!UPSTREAM.deepslateRenderer;
  }

  // --- 资源观测 -------------------------------------------------------------

  async function observeResourceDigests() {
    if (state.observedDigests) return state.observedDigests;
    const files = [
      { key: "assets_js", url: "vendor/litematica-viewer/resource/assets.js" },
      { key: "opaque_js", url: "vendor/litematica-viewer/resource/opaque.js" },
      { key: "atlas_png", url: "vendor/litematica-viewer/resource/atlas.png" },
    ];
    const out = { algo: null, files: {}, note: "前端无法复算后端 resource_hash 的算法：这里只记录观测到的字节摘要" };
    for (const f of files) {
      try {
        const res = await UPSTREAM.fetch(f.url, { cache: "force-cache" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const buf = await res.arrayBuffer();
        const { algo, hex } = await digestBytes(new Uint8Array(buf));
        out.algo = out.algo || algo;
        out.files[f.key] = { sha256_like: hex, algo, bytes: buf.byteLength, url: f.url };
      } catch (err) {
        out.files[f.key] = { error: err && err.message ? err.message : String(err), url: f.url };
      }
    }
    state.observedDigests = out;
    return out;
  }

  // --- 资源分析（写进诊断） -------------------------------------------------

  function analyzeObservedResources() {
    const scene = state.scene;
    if (!scene) return;
    const defs = [];
    const models = [];
    const textures = [];
    const variants = [];
    const seen = new Set();
    for (let s = 0; s < scene.palette.length; s++) {
      const entry = scene.palette[s];
      if (!entry || AIR_NAMES.has(entry.name)) continue;
      const key = entry.name + "|" + JSON.stringify(entry.props || {});
      if (seen.has(key)) continue;
      seen.add(key);
      const probe = probeEntry(entry.name, entry.props || {});
      if (probe.status === "missing_definition") defs.push({ palette_index: s, name: entry.name, props: entry.props || {} });
      else if (probe.status === "unresolved_variant") variants.push({ palette_index: s, name: entry.name, props: entry.props || {} });
      else if (probe.status === "missing_model") models.push({ palette_index: s, name: entry.name, props: entry.props || {}, missing_models: probe.missing_models });
      else if (probe.status === "missing_texture") textures.push({ palette_index: s, name: entry.name, props: entry.props || {}, missing_textures: probe.missing_textures });
    }
    state.resource_probe_summary = { definitions: defs, models, textures, variants };
  }

  // --- 顶视图取色：模型 up 面贴图在图集里的平均色 -----------------------------

  function computeDisplaySwatches() {
    const scene = state.scene;
    const res = resources();
    if (!scene || !res || typeof res.getTextureAtlas !== "function") {
      state.swatches = null;
      return;
    }
    let atlas = null;
    try {
      atlas = res.getTextureAtlas();
    } catch (err) {
      atlas = null;
    }
    if (!atlas || !atlas.data || !atlas.width) {
      state.swatches = null;
      return;
    }
    const table = assetsTextureKeys();
    const rawTextures = (() => {
      try {
        const a = typeof assets !== "undefined" ? assets : null;
        return a && a.textures ? a.textures : null;
      } catch (err) {
        return null;
      }
    })();
    const out = [];
    for (const entry of scene.palette) {
      out.push(topColorOfEntry(entry, res, atlas, rawTextures));
    }
    state.swatches = out;
  }

  function topColorOfEntry(entry, res, atlas, rawTextures) {
    const none = { color: null, source: "none", note: "无可用贴图，顶视图使用中性灰" };
    if (!entry || AIR_NAMES.has(entry.name)) return none;
    const probe = probeEntry(entry.name, entry.props || {});
    if (probe.status !== "ok") return { color: null, source: "proxy", note: "该条目使用显示代理" };
    let def;
    try {
      def = res.getBlockDefinition(entry.name);
    } catch (err) {
      return none;
    }
    if (!def || typeof def.getModelVariants !== "function") return none;
    let variants;
    try {
      variants = def.getModelVariants(entry.props || {});
    } catch (err) {
      return none;
    }
    if (!variants || !variants.length) return none;
    let model;
    try {
      model = res.getBlockModel(normalizeModelId(variants[0].model));
    } catch (err) {
      model = null;
    }
    if (!model || !Array.isArray(model.elements) || !model.elements.length) return none;
    // 取最高元素的 up 面（没有 up 面就取第一个面）
    let best = null;
    let bestTop = -Infinity;
    for (const el of model.elements) {
      const top = Array.isArray(el.to) ? el.to[1] : -1;
      const face = el.faces && (el.faces.up || el.faces.north || el.faces.east || Object.values(el.faces)[0]);
      if (face && top >= bestTop) {
        bestTop = top;
        best = face;
      }
    }
    if (!best || typeof best.texture !== "string") return none;
    let id;
    try {
      id = String(model.getTexture(best.texture));
    } catch (err) {
      id = best.texture;
    }
    const key = id.replace(/^minecraft:/, "");
    const rect = rawTextures ? rawTextures[key] : null;
    if (!rect) {
      // 退回：用归一化 UV × 图集尺寸（getTextureUV 从像素矩形推出来）
      try {
        const uv = res.getTextureUV(id);
        if (!uv) return none;
        const [u0, v0, u1, v1] = uv.map((n) => n * atlas.width);
        return { color: averageAtlasRect(atlas, u0, v0, u1 - u0, v1 - v0), source: "texture_uv", texture: id };
      } catch (err) {
        return none;
      }
    }
    const [u, v, du, dv] = rect;
    return { color: averageAtlasRect(atlas, u, v, du, dv), source: "texture", texture: id };
  }

  function averageAtlasRect(atlas, x0, y0, w, h) {
    const x = Math.max(0, Math.round(x0));
    const y = Math.max(0, Math.round(y0));
    const ww = Math.max(1, Math.round(w));
    const hh = Math.max(1, Math.round(h));
    let r = 0;
    let g = 0;
    let b = 0;
    let n = 0;
    const data = atlas.data;
    for (let j = 0; j < hh; j++) {
      const yy = y + j;
      if (yy >= atlas.height) continue;
      for (let i = 0; i < ww; i++) {
        const xx = x + i;
        if (xx >= atlas.width) continue;
        const o = (yy * atlas.width + xx) * 4;
        const alpha = data[o + 3];
        if (alpha < 8) continue;
        r += data[o];
        g += data[o + 1];
        b += data[o + 2];
        a += alpha;
        n++;
      }
    }
    if (!n) return null;
    return "#" + [r / n, g / n, b / n].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
  }

  // --- 射线 ----------------------------------------------------------------

  function rayFromScreen(cssX, cssY) {
    if (!mat4Ok || !state.scene) return null;
    const mvp = viewProjection();
    if (!mvp) return null;
    const inv = mat4.invert(mat4.create(), mvp);
    if (!inv) return null;
    if (state.css.width < 1 || state.css.height < 1) return null;
    const ndcX = (cssX / state.css.width) * 2 - 1;
    const ndcY = 1 - (cssY / state.css.height) * 2;
    const near = unproject(inv, [ndcX, ndcY, -1]);
    const far = unproject(inv, [ndcX, ndcY, 1]);
    if (!near || !far) return null;
    const dir = vec3.normalize(vec3.create(), vec3.subtract(vec3.create(), far, near));
    return { origin: near, direction: [dir[0], dir[1], dir[2]] };
  }

  function unproject(inv, ndc) {
    const x = ndc[0];
    const y = ndc[1];
    const z = ndc[2];
    const w = inv[3] * x + inv[7] * y + inv[11] * z + inv[15];
    if (Math.abs(w) < 1e-9) return null;
    return [
      (inv[0] * x + inv[4] * y + inv[8] * z + inv[12]) / w,
      (inv[1] * x + inv[5] * y + inv[9] * z + inv[13]) / w,
      (inv[2] * x + inv[6] * y + inv[10] * z + inv[14]) / w,
    ];
  }

  /** Amanatides & Woo 体素步进；crop 网格 [0,size)。 */
  function marchRay(ray, options = {}) {
    const [sx, sy, sz] = state.scene.size;
    const o = ray.origin;
    const d = ray.direction;
    let tMin = 0;
    let tMax = Infinity;
    for (let a = 0; a < 3; a++) {
      const size = [sx, sy, sz][a];
      if (Math.abs(d[a]) < 1e-9) {
        if (o[a] < 0 || o[a] > size) return null;
      } else {
        let t1 = (0 - o[a]) / d[a];
        let t2 = (size - o[a]) / d[a];
        if (t1 > t2) {
          const tmp = t1;
          t1 = t2;
          t2 = tmp;
        }
        tMin = Math.max(tMin, t1);
        tMax = Math.min(tMax, t2);
        if (tMin > tMax) return null;
      }
    }
    const start = [
      clampInt(Math.floor(o[0] + d[0] * (tMin + 1e-4)), 0, sx - 1),
      clampInt(Math.floor(o[1] + d[1] * (tMin + 1e-4)), 0, sy - 1),
      clampInt(Math.floor(o[2] + d[2] * (tMin + 1e-4)), 0, sz - 1),
    ];
    const step = [d[0] > 0 ? 1 : d[0] < 0 ? -1 : 0, d[1] > 0 ? 1 : d[1] < 0 ? -1 : 0, d[2] > 0 ? 1 : d[2] < 0 ? -1 : 0];
    const tDelta = [1, 2].map((_, i) => (Math.abs(d[i]) < 1e-9 ? Infinity : Math.abs(1 / d[i])));
    const tMaxArr = [0, 1, 2].map((i) => {
      if (Math.abs(d[i]) < 1e-9) return Infinity;
      const next = start[i] + (step[i] > 0 ? 1 : 0);
      return tMin + (next - (o[i] + d[i] * tMin)) / d[i];
    });
    const cur = [start[0], start[1], start[2]];
    let lastAxis = -1;
    const maxSteps = 3 * (sx + sy + sz) + 16;
    for (let n = 0; n < maxSteps; n++) {
      if (cur[0] < 0 || cur[1] < 0 || cur[2] < 0 || cur[0] >= sx || cur[1] >= sy || cur[2] >= sz) break;
      const hit = options.requireSubmitted ? isSubmittedRender(cur) : true;
      if (hit) {
        return {
          pos_render: [cur[0], cur[1], cur[2]],
          pos_local: toLocal(cur),
          face: lastAxis < 0 ? null : ["x", "y", "z"][lastAxis] + (step[lastAxis] > 0 ? "-" : "+"),
          t: tMin,
          t: tMin,
        };
      }
      if (tMaxArr[0] <= tMaxArr[1] && tMaxArr[0] <= tMaxArr[2]) {
        cur[0] += step[0];
        tMin = tMaxArr[0];
        tMaxArr[0] += tDelta[0];
        lastAxis = 0;
      } else if (tMaxArr[1] <= tMaxArr[2]) {
        cur[1] += step[1];
        tMin = tMaxArr[1];
        tMaxArr[1] += tDelta[1];
        lastAxis = 1;
      } else {
        cur[2] += step[2];
        tMin = tMaxArr[2];
        tMaxArr[2] += tDelta[2];
        lastAxis = 2;
      }
    }
    return null;
  }

  // --- 输入（受管理、可解除；不接触 document 级上游监听器） -------------------

  function isEditableTarget(el) {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (el.isContentEditable === true) return true;
    const attr = typeof el.getAttribute === "function" ? el.getAttribute("contenteditable") : null;
    return attr === "" || attr === "true" || attr === "plaintext-only";
  }

  function shouldIgnoreKeyEvent(ev) {
    if (isEditableTarget(ev.target) || isEditableTarget(document.activeElement)) return true;
    const ae = document.activeElement;
    if (ae && (ae.tagName === "BUTTON" || ae.tagName === "A") && (ev.code === "Space" || ev.code === "Enter")) return true;
    return false;
  }

  function moveDirectionFromKey(code) {
    const step = CAMERA.key_step;
    switch (code) {
      case "KeyW":
      case "ArrowUp":
        return [0, 0, step];
      case "KeyS":
      case "ArrowDown":
        return [0, 0, -step];
      case "KeyA":
      case "ArrowLeft":
        return [step, 0, 0];
      case "KeyD":
      case "ArrowRight":
        return [-step, 0, 0];
      case "ShiftLeft":
      case "ShiftRight":
        return [0, step, 0];
      case "Space":
        return [0, -step, 0];
      default:
        return null;
    }
  }

  function move3d(offset, relativeVertical, sensitivity = 1) {
    // 与上游 createRenderCanvas 内同名函数同义
    const v = vec3.fromValues(offset[0] * sensitivity, offset[1] * sensitivity, offset[2] * sensitivity);
    if (relativeVertical) vec3.rotateX(v, v, [0, 0, 0], -UPSTREAM.cameraPitch * sensitivity);
    vec3.rotateY(v, v, [0, 0, 0], -UPSTREAM.cameraYaw * sensitivity);
    vec3.add(UPSTREAM.cameraPos, UPSTREAM.cameraPos, v);
    emit("camera", getCamera());
  }

  function translateScreen(dx, dy, sensitivity = 1) {
    const off = vec3.fromValues(dx * CAMERA.translate_per_px * sensitivity, -dy * CAMERA.translate_per_px * sensitivity, 0);
    vec3.rotateX(off, off, [0, 0, 0], -UPSTREAM.cameraPitch);
    vec3.rotateY(off, off, [0, 0, 0], -UPSTREAM.cameraYaw);
    vec3.add(UPSTREAM.cameraPos, UPSTREAM.cameraPos, off);
    emit("camera", getCamera());
  }

  function rotateCamera(dx, dy) {
    // 上游 pan() 的符号约定：向右拖 yaw 增大
    UPSTREAM.cameraYaw += dx * CAMERA.rotate_per_px;
    UPSTREAM.cameraPitch += dy * CAMERA.rotate_per_px;
    const { pitch, yaw } = normalizeCamera(UPSTREAM.cameraPitch, UPSTREAM.cameraYaw);
    UPSTREAM.cameraPitch = pitch;
    UPSTREAM.cameraYaw = yaw;
    emit("camera", getCamera());
  }

  function localPointer(ev) {
    const rect = canvas.getBoundingClientRect();
    return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
  }

  function onPointerDown(ev) {
    if (!scene_ready()) return;
    canvas.focus({ preventScroll: true });
    const p = localPointer(ev);
    const mode = state.interactionMode;
    if (ev.button === 0 && (mode === "select" || mode === "pick")) {
      ev.preventDefault();
      if (mode === "pick") {
        const hit = api.pickVoxelAt(p.x, p.y);
        if (hit) {
          const info = api.probeVoxel(hit.pos_local);
          emit("pick", { hit, info });
        } else {
          emit("pick", { hit: null, info: null });
        }
        return;
      }
      // 框选：在水平工作平面上取点，不旋转相机
      const cell = api.pickPlaneAt(p.x, p.y, state.plane_y);
      if (!cell) return;
      state.drag = {
        kind: "plane",
        pointerId: ev.pointerId,
        start: cell.cell,
        current: cell.cell,
        plane_y: state.plane_y,
        preview: null,
        additive: ev.shiftKey,
      };
      updatePlanePreview();
      canvas.setPointerCapture?.(ev.pointerId);
      return;
    }
    if (ev.button === 1 || ev.button === 2 || (ev.button === 0 && mode === "navigate")) {
      ev.preventDefault();
      const translate = ev.button !== 0 || ev.shiftKey;
      state.drag = { kind: translate ? "translate" : "rotate", pointerId: ev.pointerId, last: [ev.clientX, ev.clientY] };
      canvas.setPointerCapture?.(ev.pointerId);
    }
  }

  function onPointerMove(ev) {
    if (!scene_ready()) return;
    const p = localPointer(ev);
    const drag = state.drag;
    if (drag) {
      if (drag.kind === "rotate") {
        rotateCamera(ev.clientX - drag.last[0], ev.clientY - drag.last[1]);
        drag.last = [ev.clientX, ev.clientY];
      } else if (drag.kind === "translate") {
        translateScreen(ev.clientX - drag.last[0], ev.clientY - drag.last[1]);
        drag.last = [ev.clientX, ev.clientY];
      } else if (drag.kind === "plane") {
        const cell = api.pickPlaneAt(p.x, p.y, drag.plane_y);
        if (cell) {
          drag.current = cell.cell;
          updatePlanePreview();
        }
        return;
      }
      scheduleFrame();
      return;
    }
    // 悬停探测（节流到 ~20Hz，避免每帧做 DDA）
    const now = Date.now();
    if (now - lastPickEvent > 50) {
      lastPickEvent = now;
      const hit = api.pickVoxelAt(p.x, p.y);
      const prev = state.hover;
      if (!!hit !== !!prev || (hit && prev && (hit.pos_local[0] !== prev.pos_local[0] || hit.pos_local[1] !== prev.pos_local[1] || hit.pos_local[2] !== prev.pos_local[2]))) {
        if (hit) {
          const info = api.probeVoxel(hit.pos_local);
          state.hover = { pos_local: hit.pos_local, pos_render: hit.pos_render, face: hit.face, info };
        } else {
          state.hover = null;
        }
        emit("hover", state.hover);
        drawOverlay();
      }
    }
  }

  function updatePlanePreview() {
    const drag = state.drag;
    if (!drag || drag.kind !== "plane") return;
    const a = drag.start;
    const b = drag.current;
    const minX = Math.min(a[0], b[0]);
    const maxX = Math.max(a[0], b[0]) + 1;
    const minZ = Math.min(a[2], b[2]);
    const maxZ = Math.max(a[2], b[2]) + 1;
    drag.preview = { min: [minX, drag.plane_y, minZ], max: [maxX, drag.plane_y + 1, maxZ] };
    emit("marquee", { preview: drag.preview, plane_y: drag.plane_y });
    drawOverlay();
  }

  function onPointerUp(ev) {
    const drag = state.drag;
    if (!drag) return;
    state.drag = null;
    canvas.releasePointerCapture?.(drag.pointerId);
    if (drag.kind === "plane") {
      const a = drag.start;
      const b = drag.current;
      const minX = Math.min(a[0], b[0]);
      const maxX = Math.max(a[0], b[0]) + 1;
      const minZ = Math.min(a[2], b[2]);
      const maxZ = Math.max(a[2], b[2]) + 1;
      // 输出 p_render 的 x/z 矩形；Y 范围由调用方（授权 Y 范围控件）决定，切片不影响它
      const rectRender = { min: [minX, drag.plane_y, minZ], max_exclusive: [maxX, drag.plane_y + 1, maxZ] };
      emit("marquee_commit", {
        rect_render: rectRender,
        rect_local: { min: toLocal(rectRender.min), max_exclusive: toLocal(rectRender.max_exclusive) },
        plane_y: drag.plane_y,
        additive: !!drag.additive,
      });
      drawOverlay();
    }
  }

  function onWheel(ev) {
    if (!scene_ready()) return;
    ev.preventDefault();
    move3d([0, 0, -ev.deltaY * CAMERA.dolly_per_wheel_px], true);
    scheduleFrame();
  }

  function onKeyDown(ev) {
    // 关键顺序：先判断焦点是否在输入控件里，再决定要不要 preventDefault。
    if (shouldIgnoreKeyEvent(ev)) return;
    const dir = moveDirectionFromKey(ev.code);
    if (dir) {
      ev.preventDefault();
      pressedKeys.add(ev.code);
      scheduleFrame();
    }
  }

  function onKeyUp(ev) {
    pressedKeys.delete(ev.code);
  }

  function onWindowBlur() {
    pressedKeys.clear();
  }

  function onContextLost(ev) {
    ev.preventDefault?.();
    state.contextLost = true;
    state.contextLostCount++;
    state.status = RENDER_STATUS.CONTEXT_LOST;
    rejectAllWaiters(ERROR_CODES.RENDER_CONTEXT_LOST, "WebGL 上下文丢失（webglcontextlost）");
    emit("status", { status: state.status });
  }

  function onContextRestored() {
    // 上下文回来了，但上游渲染器持有的 GL 对象已失效；重建渲染器（renderer_builds +1）
    state.restoredCount++;
    state.contextLost = false;
    state.viewportDirty = true;
    if (!state.scene) {
      state.status = RENDER_STATUS.IDLE;
      return;
    }
    try {
      const counters = emptyCounters(state.scene.idx.length);
      const { structure, range, hidden } = buildStructure(counters);
      state.counters = counters;
      state.hidden = hidden;
      state.range = range;
      UPSTREAM.deepslateRenderer = null;
      state.rendererBuilds++;
      UPSTREAM.setStructure(structure, false);
      applyViewport();
      state.buildId++;
      state.frames.set(state.buildId, 0);
      state.status = counters.input_non_air === 0 ? RENDER_STATUS.EMPTY : RENDER_STATUS.READY;
      note("WebGL 上下文恢复：渲染器已重建，renderer_builds 计数增加");
      scheduleFrame();
    } catch (err) {
      state.status = RENDER_STATUS.UNAVAILABLE;
      state.unavailable_reason = `上下文恢复后重建失败：${err && err.message ? err.message : err}`;
      emit("status", { status: state.status });
    }
  }

  function onPointerLeave() {
    if (state.hover) {
      state.hover = null;
      emit("hover", null);
      drawOverlay();
    }
  }

  function onContextMenu(ev) {
    ev.preventDefault();
  }

  function addInputListeners() {
    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", onPointerUp);
    canvas.addEventListener("pointerleave", onPointerLeave);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("contextmenu", onContextMenu);
    canvas.addEventListener("webglcontextlost", onContextLost, false);
    canvas.addEventListener("webglcontextrestored", onContextRestored, false);
    UPSTREAM.addEventListener("keydown", onKeyDown);
    UPSTREAM.addEventListener("keyup", onKeyUp);
    UPSTREAM.addEventListener("blur", onWindowBlur);
  }

  function removeInputListeners() {
    canvas.removeEventListener("pointerdown", onPointerDown);
    canvas.removeEventListener("pointermove", onPointerMove);
    canvas.removeEventListener("pointerup", onPointerUp);
    canvas.removeEventListener("pointercancel", onPointerUp);
    canvas.removeEventListener("pointerleave", onPointerLeave);
    canvas.removeEventListener("wheel", onWheel);
    canvas.removeEventListener("contextmenu", onContextMenu);
    canvas.removeEventListener("webglcontextlost", onContextLost);
    canvas.removeEventListener("webglcontextrestored", onContextRestored);
    UPSTREAM.removeEventListener("keydown", onKeyDown);
    UPSTREAM.removeEventListener("keyup", onKeyUp);
    UPSTREAM.removeEventListener("blur", onWindowBlur);
  }

  // --- 尺寸监听 -------------------------------------------------------------

  let resizeObserver = null;
  let dprQuery = null;
  let dprQueryListener = null;

  function onWindowResize() {
    state.viewportDirty = true;
    applyViewport();
    scheduleFrame();
  }

  function registerDprListener() {
    removeDprListener();
    if (typeof UPSTREAM.matchMedia !== "function") return;
    const dpr = Math.min(3, Math.max(1, UPSTREAM.devicePixelRatio || 1));
    try {
      dprQuery = UPSTREAM.matchMedia(`(resolution: ${dpr}dppx)`);
      dprQueryListener = () => {
        state.viewportDirty = true;
        applyViewport();
        scheduleFrame();
        registerDprListener();
      };
      dprQuery.addEventListener?.("change", dprQueryListener);
    } catch (err) {
      dprQuery = null;
    }
  }

  function removeDprListener() {
    if (dprQuery && dprQueryListener) dprQuery.removeEventListener?.("change", dprQueryListener);
    dprQuery = null;
    dprQueryListener = null;
  }

  // --- 诊断 -----------------------------------------------------------------

  function buildDiagnostics() {
    const scene = state.scene;
    const counters = state.counters;
    const range = state.scene ? currentSliceRange() : null;
    const slice = range
      ? {
          axis: "y",
          space: "crop_local",
          min: range.min,
          max: range.max,
          explicit: range.explicit,
          min_local: scene ? scene.crop_origin_local[1] + range.min : null,
          max_local: scene ? scene.crop_origin_local[1] + range.max : null,
          note: "切片只影响显示；不改变选区的 Y 范围，也不改变蓝图",
        }
      : null;
    const samples = counters ? counters.samples : null;
    const mkSample = (kind, extra) => {
      const list = samples && samples[kind] ? samples[kind] : [];
      return { count: counters ? counters[kind] || 0 : 0, limit: DIAGNOSTIC_SAMPLE_LIMIT, truncated: (counters ? counters[kind] || 0 : 0) > list.length, entries: list, ...(extra || {}) };
    };
    const rs = state.resource_probe_summary || { definitions: [], models: [], textures: [], variants: [] };
    return {
      schema_version: "0.1",
      status: state.disposed ? RENDER_STATUS.DISPOSED : state.status,
      scene_id: scene ? scene.scene_id : null,
      scene_hash: scene ? scene.scene_hash : null,
      file_sha256: scene ? scene.file_sha256 ?? null : null,
      renderer_build_hash: state.buildHash,
      renderer_build_hash_algo: state.buildHashAlgo,
      renderer_build: VIEWER_BUILD,
      resource_hash: scene ? scene.resource_hash ?? null : null,
      resource_hash_source: "render_scene.resource_hash（本构建未复算后端算法）",
      resource_hash_verified: false,
      resource_sha256_observed: state.observedDigests || null,
      coordinate_space: "project_local",
      crop_bounds: scene
        ? {
            origin_local: cloneTriple(scene.crop_origin_local),
            size: cloneTriple(scene.size),
            min_local: cloneTriple(scene.crop_origin_local),
            max_exclusive_local: [
              scene.crop_origin_local[0] + scene.size[0],
              scene.crop_origin_local[1] + scene.size[1],
              scene.crop_origin_local[2] + scene.size[2],
            ],
          }
        : null,
      full_scene_bounds: scene
        ? { min: cloneTriple(scene.full_scene_bounds.min), max_exclusive: cloneTriple(scene.full_scene_bounds.max_exclusive) }
        : null,
      slice_range: slice,
      hidden_layers: state.hidden ? state.hidden.slice() : [],
      hidden_layer_count: state.hidden ? state.hidden.length : 0,
      viewport: {
        width: state.css.width,
        height: state.css.height,
        dpr: state.dpr,
        canvas_width: canvas.width,
        canvas_height: canvas.height,
        note: "canvas.width/height 是设备像素；CSS 尺寸另设；projMatrix 由 clientWidth/clientHeight 决定",
      },
      camera: getCamera(),
      input_non_air_count: counters ? counters.input_non_air : scene ? scene.idx.length : 0,
      accepted_voxel_count: counters ? counters.accepted_real + counters.accepted_proxy : 0,
      accepted_voxel_count_note: "提交给场景模型（deepslate.Structure）的方块数，不是可见像素数或面数",
      accepted_real_count: counters ? counters.accepted_real : 0,
      accepted_proxy_count: counters ? counters.accepted_proxy : 0,
      proxy_block: DISPLAY_PROXY_BLOCK,
      proxy_reasons: counters ? counters.proxy_states.slice() : [],
      clipped_voxel_count: counters ? counters.clipped : 0,
      clipped_voxel_count_note: "被切片裁掉的方块数：这是正常的观察行为，不是错误",
      skipped_air_count: counters ? counters.skipped_air : 0,
      out_of_range_index_count: counters ? counters.out_of_range : 0,
      invalid_palette_indices: mkSample("invalid_palette", {
        note: "state[i] 越界或 palette[state[i]] 缺失：用显示代理渲染并记录，绝不静默跳过",
      }),
      missing_block_definitions: mkSample("missing_definition", { distinct: rs.definitions }),
      missing_models: mkSample("missing_model", { distinct: rs.models }),
      unresolved_variants: mkSample("unresolved_variant", { distinct: rs.variants }),
      missing_textures: mkSample("missing_texture", { distinct: rs.textures }),
      addblock_errors: mkSample("addblock_error"),
      unsupported_block_entities: [],
      unsupported_block_entities_note: "实体数据不在 RenderScene 协议内（渲染不携带 block entity/NBT）；NBT 保留由后端负责，截图不能证明 NBT",
      webgl_errors: state.glErrors.slice(),
      webgl_error_count: state.glErrorCount,
      context_lost: state.contextLost,
      context_lost_count: state.contextLostCount,
      context_restored_count: state.restoredCount,
      last_frame_sequence: state.frameSequence,
      last_frame: state.lastFrame,
      frames_for_current_build: state.frames.get(state.buildId) || 0,
      renderer_builds: state.rendererBuilds,
      renderer_reused_count: state.rendererReused,
      renderer_builds_note: "StructureRenderer 的构造次数；切片变化只调用 setStructure()，不增加该计数",
      renderer_reused_for_setStructure: true,
      display: { ...state.display, plane_y: state.plane_y },
      diff_overlay: state.diff
        ? {
            active: !!state.diffBoxes,
            ok: state.diff.ok,
            errors: state.diff.errors,
            counts: state.diff.counts,
            drawn_boxes: state.diffBoxes ? state.diffBoxes.drawn : 0,
            aggregated: state.diffBoxes ? state.diffBoxes.aggregated : false,
            truncated: state.diffBoxes ? state.diffBoxes.truncated : false,
            outside_crop: state.diffBoxes ? state.diffBoxes.outside_crop : 0,
            note: "仅视图叠加：删除用虚线 ghost，不往蓝图写入任何标记方块",
          }
        : { active: false },
      selection: state.selection
        ? {
            bounds_local: { min: state.selection.min, max_exclusive: state.selection.max_exclusive },
            displayed: !!state.selectionRender,
            clipped_by_crop: state.selectionRender ? state.selectionRender.clipped : true,
          }
        : null,
      hover: state.hover
        ? { pos_local: state.hover.pos_local, pos_render: state.hover.pos_render, face: state.hover.face, state_source: state.hover.info.state_source }
        : null,
      issue: state.issue ? { code: state.issue.code, pos_local: state.issue.pos_local } : null,
      interaction_mode: state.interactionMode,
      notes: state.notes.slice(),
      crop_vs_full_estimate: scene && counters ? estimateAlignment(scene, counters) : null,
    };
  }

  function estimateAlignment(scene, counters) {
    // 粗略提示：如果完整场景远超 crop，观察可能误导；不改变任何决策。
    const crop = scene.size[0] * scene.size[1] * scene.size[2];
    const full = scene.full_scene_bounds.max_exclusive.reduce((a, b, i) => a * (b - scene.full_scene_bounds.min[i]), 1);
    return { crop_voxels: crop, full_scene_voxels: full, crop_ratio: full > 0 ? Number((crop / full).toFixed(4)) : null };
  }

  // --- 启动 ---------------------------------------------------------------

  state.interactionMode = settings.interactionMode;
  if (!gl) {
    state.status = RENDER_STATUS.UNAVAILABLE;
    state.unavailable_reason =
      "canvas.getContext('webgl') 返回空：本机/本浏览器没有可用的 WebGL 上下文（不会用空白图冒充成功）";
    state.notes.push(state.unavailable_reason);
  } else {
    applyViewport();
    addInputListeners();
    if (typeof UPSTREAM.ResizeObserver === "function") {
      resizeObserver = new UPSTREAM.ResizeObserver(() => {
        state.viewportDirty = true;
        applyViewport();
        scheduleFrame();
      });
      resizeObserver.observe(container);
    }
    UPSTREAM.addEventListener("resize", onWindowResize);
    registerDprListener();
    if (typeof UPSTREAM.devicePixelRatio === "undefined") {
      note("window.devicePixelRatio 不可用：按 1 处理");
    }
  }
}
