// ===========================================================================
// web/app.js — 工作台 UI 状态与数据加载
// ===========================================================================
//
// 职责
// ----
// 只做三件事：① 读写真实数据源（RenderScene / 净变化 / 面板夹具）；② 把状态写进 DOM
// （**全部用 textContent / createTextNode**，绝不 innerHTML 拼数据字符串）；
// ③ 把用户操作转成适配层调用。渲染与诊断本身在 viewer_adapter.js。
//
// 数据来源（后端路由尚未实现，因此本构建全部走 URL 参数）
// -----------------------------------------------------
//   ?scene=<url>      当前修订（默认 ../work/sample_render_scene.json）
//   ?candidate=<url>  候选场景（对应 GET /api/candidates/{c}/render-scene）
//   ?diff=<url>       净变化（对应 GET /api/candidates/{c}/diff）
//   ?panels=<url>     左侧列表与硬错误（对应 GET /api/projects/{p} 的片段）
//   ?demo=1           加载 web/fixtures/demo_*.json，并在界面上标注"演示数据"
//   ?mode=navigate|select|pick   初始交互模式
//   ?slice=min:max    初始渲染切片（crop 局部 Y，含端点）
//
// 场景切换竞态：每次加载请求带自增序号，迟到的旧响应直接丢弃（不会覆盖新场景）。
// ===========================================================================

import {
  createViewerAdapter,
  validateRenderScene,
  RENDER_STATUS,
  DISPLAY_PROXY_BLOCK,
} from "./viewer_adapter.js";
import { createSelectionController } from "./selection.js";
import { CATEGORY_ORDER, CATEGORY_STYLE, describeDiff, normalizeDiff, buildDiffBoxes } from "./diff_overlay.js";

const $ = (id) => document.getElementById(id);

const el = {
  factProject: $("fact-project"),
  factRevision: $("fact-revision"),
  factSceneHash: $("fact-scene-hash"),
  factFileHash: $("fact-file-hash"),

  viewport: $("viewport"),
  notice: $("viewport-notice"),
  noticeTitle: $("viewport-notice-title"),
  noticeDetail: $("viewport-notice-detail"),
  hud: $("viewport-hud"),

  modeSwitch: $("mode-switch"),
  sceneSwitch: $("scene-switch"),
  chipModeHint: $("chip-mode-hint"),
  btnResetView: $("btn-reset-view"),

  sliceMin: $("slice-min"),
  sliceMax: $("slice-max"),
  sliceReadout: $("slice-readout"),
  btnSliceFull: $("btn-slice-full"),

  statusState: $("status-state"),
  statusText: $("status-text"),

  layerGrid: $("layer-grid"),
  layerHiddenOutlines: $("layer-hidden-outlines"),
  layerOverlay: $("layer-overlay"),
  layerFade: $("layer-fade"),

  listProtected: $("list-protected"),
  countProtected: $("count-protected"),
  hintProtected: $("hint-protected"),
  listAssets: $("list-assets"),
  countAssets: $("count-assets"),
  hintAssets: $("hint-assets"),
  listHistory: $("list-history"),
  countHistory: $("count-history"),
  hintHistory: $("hint-history"),

  selX: $("sel-x"),
  selY: $("sel-y"),
  selZ: $("sel-z"),
  selSize: $("sel-size"),
  selVoxels: $("sel-voxels"),
  selNonair: $("sel-nonair"),
  selSource: $("sel-source"),
  selAuth: $("sel-auth"),
  yMin: $("y-min"),
  yMax: $("y-max"),
  yFeedback: $("y-feedback"),
  btnYFull: $("btn-y-full"),
  btnSelClear: $("btn-sel-clear"),
  topView: $("top-view"),
  mapHover: $("map-hover"),

  pickA: $("pick-a"),
  pickB: $("pick-b"),
  pickFace: $("pick-face"),
  pickBlock: $("pick-block"),
  planeY: $("plane-y"),

  diffLegend: $("diff-legend"),
  diffNote: $("diff-note"),

  instruction: $("instruction"),
  instructionHint: $("instruction-hint"),
  requestPreview: $("request-preview"),
  btnGenerate: $("btn-generate"),

  jobState: $("job-state"),
  jobCandidate: $("job-candidate"),
  jobNote: $("job-note"),

  issueList: $("issue-list"),
  countIssues: $("count-issues"),

  btnRefreshDiag: $("btn-refresh-diag"),
  btnCopyDiag: $("btn-copy-diag"),
  diagJson: $("diagnostics-json"),

  baPos: $("ba-pos"),
  baBefore: $("ba-before"),
  baAfter: $("ba-after"),
  baCategory: $("ba-category"),

  btnExport: $("btn-export"),
  btnAccept: $("btn-accept"),
  btnReject: $("btn-reject"),
  btnUndo: $("btn-undo"),
  btnRedo: $("btn-redo"),

  toast: $("toast"),
  srStatus: $("screenreader-status"),
};

const params = new URLSearchParams(location.search);
const urls = {
  scene: params.get("scene") || "../work/sample_render_scene.json",
  candidate: params.get("candidate"),
  diff: params.get("diff"),
  panels: params.get("panels"),
};
const demoMode = params.get("demo") === "1";
if (demoMode) {
  urls.panels = urls.panels || "fixtures/demo_panels.json";
  urls.diff = urls.diff || "fixtures/demo_diff.json";
}

const state = {
  project: demoMode ? "演示数据（demo）" : params.get("project") || "未连接",
  revision: params.get("revision") || (demoMode ? "r001（演示）" : "—"),
  sceneUrl: urls.scene,
  view: "current",
  loadSeq: 0,
  receipt: null,
  sceneInfo: null,
  /** 原始 RenderScene 载荷（含 idx/state，供选择器做高度图与统计） */
  scenePayload: null,
  diagnostics: null,
  /** 归一化后的净变化（diff_overlay.normalizeDiff 的返回） */
  diff: null,
  diffPayload: null,
  pickA: null,
  pickB: null,
  hoverInfo: null,
  fixture: { protected_areas: [], assets: [], history: [], issues: [] },
  fixtureLoaded: false,
  slice: { min: 0, max: 0 },
  localIssues: [],
};

// ===========================================================================
// 通用小工具
// ===========================================================================

function setText(node, text) {
  if (node) node.textContent = text == null ? "" : String(text);
}

function clear(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

function shortHash(hash, head = 10) {
  if (typeof hash !== "string" || !hash) return "—";
  return hash.length <= head + 4 ? hash : `${hash.slice(0, head)}…${hash.slice(-4)}`;
}

function fmtTriple(v) {
  return Array.isArray(v) ? `[${v.join(", ")}]` : "—";
}

function fmtState(s) {
  if (!s) return "空气";
  const props = s.props && Object.keys(s.props).length ? ` ${JSON.stringify(s.props)}` : "";
  return `${s.name}${props}`;
}

function isEditableTarget(node) {
  if (!node || node.nodeType !== 1) return false;
  const tag = node.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  return node.isContentEditable === true;
}

let toastTimer = null;
function toast(message, tone = "info") {
  setText(el.toast, message);
  el.toast.dataset.tone = tone;
  el.toast.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.toast.hidden = true;
  }, 5000);
  setText(el.srStatus, message);
}

function setStatus(status, text) {
  el.statusState.dataset.status = status;
  setText(el.statusState, statusLabel(status));
  setText(el.statusText, text);
}

function statusLabel(status) {
  switch (status) {
    case RENDER_STATUS.READY:
      return "渲染就绪";
    case RENDER_STATUS.EMPTY:
      return "空选区";
    case RENDER_STATUS.LOADING:
      return "加载中";
    case RENDER_STATUS.UNAVAILABLE:
      return "渲染不可用";
    case RENDER_STATUS.ERROR:
      return "错误";
    case RENDER_STATUS.CONTEXT_LOST:
      return "上下文丢失";
    case RENDER_STATUS.DISPOSED:
      return "已释放";
    default:
      return "就绪";
  }
}

function showNotice(title, detail, tone = "error") {
  setText(el.noticeTitle, title);
  setText(el.noticeDetail, detail || "");
  el.notice.dataset.tone = tone;
  el.notice.hidden = false;
}

function hideNotice() {
  el.notice.hidden = true;
}

// ===========================================================================
// 适配层与选择器
// ===========================================================================

const adapter = createViewerAdapter(el.viewport, { interactionMode: params.get("mode") || "navigate" });

const selection = createSelectionController({
  canvas: el.topView,
  yMin: el.yMin,
  yMax: el.yMax,
  yFullButton: el.btnYFull,
  yClearButton: el.btnSelClear,
  feedback: el.yFeedback,
  onChange: (sel) => {
    renderSelectionReadout(sel);
    adapter.setSelection(sel ? { min: sel.min, max_exclusive: sel.max_exclusive } : null);
    updateRequestPreview();
  },
  onHover: (info) => {
    if (!info) {
      setText(el.mapHover, "把鼠标移到俯视图上查看该列最高非空气方块。");
      return;
    }
    setText(
      el.mapHover,
      `列 (x=${info.cell.x}, z=${info.cell.z}) 局部 → 该列最高 y=${info.top_y_render}；共 ${info.column_voxels} 个非空气体素；顶方块 ${info.top_state ? info.top_state.name : "无"}`,
    );
  },
});

// ===========================================================================
// 状态栏 / HUD / 面板渲染
// ===========================================================================

function refreshHud() {
  const cam = adapter.getCamera();
  const vp = cam.viewport;
  const parts = [
    `视口 ${vp.width}×${vp.height} @${vp.dpr}x`,
    `pitch ${cam.pitch.toFixed(3)}`,
    `yaw ${cam.yaw.toFixed(3)}`,
    `camera_pos ${cam.camera_pos.map((v) => v.toFixed(1)).join(", ")}`,
  ];
  setText(el.hud, parts.join(" · "));
  el.hud.hidden = false;
}

function renderSelectionReadout(sel) {
  if (!sel) {
    setText(el.selX, "—");
    setText(el.selY, "—");
    setText(el.selZ, "—");
    setText(el.selSize, "—");
    setText(el.selVoxels, "—");
    setText(el.selNonair, "—");
    setText(el.selSource, "—");
    setText(el.selAuth, "预计授权范围：未选择 → 没有可编辑范围。");
    return;
  }
  setText(el.selX, `[${sel.min[0]}, ${sel.max_exclusive[0]})`);
  setText(el.selY, `[${sel.min[1]}, ${sel.max_exclusive[1]})`);
  setText(el.selZ, `[${sel.min[2]}, ${sel.max_exclusive[2]})`);
  setText(el.selSize, `${sel.size.join(" × ")}（体素格）`);
  setText(el.selVoxels, `${sel.voxel_count}（授权体素数上界）`);
  setText(el.selNonair, sel.non_air_count == null ? "超出 crop，无法从渲染数据统计" : `${sel.non_air_count}（当前 crop 内）`);
  setText(el.selSource, `${sel.source}（${sel.coordinate_space}）`);
  const bits = ["预计授权范围：与可编辑区、保护区取交集后由服务端 WriteGuard 判定。"];
  if (sel.extends_outside_crop) bits.push("选区超出当前渲染 crop：超出的部分在视口里看不到，但后端仍会校验。");
  setText(el.selAuth, bits.join(" "));
  el.selVoxels.parentElement?.parentElement?.classList.add("readout--flash");
  setTimeout(() => el.selVoxels.parentElement?.parentElement?.classList.remove("readout--flash"), 340);
}

function renderDiffLegend(diff, boxes) {
  clear(el.diffLegend);
  if (!diff) {
    setText(el.diffNote, "未加载净变化数据（后端 /api/candidates/{c}/diff）。可用 ?diff= 指定一个 JSON 文件。");
    return;
  }
  const desc = describeDiff(diff, boxes);
  for (const row of desc.rows) {
    const li = document.createElement("li");
    const key = document.createElement("span");
    key.className = "legend__key";
    const swatch = document.createElement("span");
    swatch.className = "legend__swatch";
    swatch.style.background = row.color;
    swatch.style.borderStyle = row.pattern === "实线" ? "solid" : "dashed";
    const label = document.createElement("span");
    label.textContent = `${row.label}（${row.pattern}）`;
    key.appendChild(swatch);
    key.appendChild(label);
    const count = document.createElement("span");
    count.className = "legend__count";
    count.textContent = String(row.count);
    li.appendChild(key);
    li.appendChild(count);
    el.diffLegend.appendChild(li);
  }
  const notes = [
    `共 ${desc.total} 条净变化；画了 ${desc.drawn_boxes} 个盒子（合并方式 ${desc.aggregation}）。`,
    "叠加只在视图里：删除显示为虚线 ghost，不向蓝图写入任何标记方块；叠加层不参与深度测试。",
  ];
  if (desc.aggregated) notes.push("体素过多或分布过散：叠加按列/包围盒聚合，不能拿它数体素。");
  if (desc.outside_crop) notes.push(`有 ${desc.outside_crop} 条变化落在当前 crop 之外（视口里看不到）。`);
  if (desc.counts_mismatch) notes.push("载荷自带 counts 与实际解析不一致：以实际数据为准（已列入硬错误）。");
  if (desc.base_revision) notes.push(`基线修订：${desc.base_revision}；本次 Before 默认指 Br，不是最初地形。`);
  if (desc.errors.length) notes.push(`载荷错误：${desc.errors.join("；")}`);
  if (desc.warnings.length) notes.push(`载荷提示：${desc.warnings.slice(0, 3).join("；")}`);
  setText(el.diffNote, notes.join(" "));
}

function renderListItems(container, rows, options = {}) {
  clear(container);
  if (!rows.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = options.emptyText || "空";
    container.appendChild(li);
    return;
  }
  rows.forEach((row, index) => {
    const li = document.createElement("li");
    li.style.setProperty("--i", String(Math.min(index, 12)));
    const title = document.createElement("b");
    title.textContent = row.title;
    const sub = document.createElement("span");
    sub.textContent = row.subtitle;
    li.appendChild(title);
    li.appendChild(sub);
    if (row.extra) {
      const extra = document.createElement("span");
      extra.textContent = row.extra;
      li.appendChild(extra);
    }
    container.appendChild(li);
  });
}

function renderPanels() {
  const fx = state.fixture;
  setText(el.countProtected, String(fx.protected_areas.length));
  renderListItems(
    el.listProtected,
    fx.protected_areas.map((area) => ({
      title: area.label || area.id || "未命名保护区",
      subtitle: area.bounds ? `bounds ${fmtTriple(area.bounds.min)} → ${fmtTriple(area.bounds.max_exclusive)}（project_local，半开区间）` : "bounds 缺失",
      extra: area.read_only === false ? "read_only=false（注意：保护区默认只读）" : `来源：${area.source || "未标注"}`,
    })),
    { emptyText: "无保护区数据" },
  );
  setText(
    el.hintProtected,
    fx.protected_areas.length
      ? `${demoMode ? "演示数据：" : ""}显示红色保护框不构成保护实现：真正的写入拒绝在后端 WriteGuard。`
      : "未提供保护区数据（后端 /api/projects/{p} 未实现）。",
  );

  setText(el.countAssets, String(fx.assets.length));
  renderListItems(
    el.listAssets,
    fx.assets.map((asset) => ({
      title: `${asset.object_id || "未命名对象"}${asset.kind ? ` · ${asset.kind}` : ""}`,
      subtitle: asset.bounds ? `占用 ${fmtTriple(asset.bounds.min)} → ${fmtTriple(asset.bounds.max_exclusive)}` : "bounds 缺失",
      extra: `来源修订：${asset.creation_revision || "未登记"}；asset_version：${asset.asset_version || "—"}`,
    })),
    { emptyText: "无资产实例" },
  );
  setText(el.hintAssets, fx.assets.length ? `${demoMode ? "演示数据：" : ""}来源不完整的对象不得当作本系统生成。` : "未提供资产注册表（后端 objects/ 未接入）。");

  setText(el.countHistory, String(fx.history.length));
  renderListItems(
    el.listHistory,
    fx.history.map((rev) => ({
      title: `${rev.revision_id || "未命名修订"}${rev.kind ? ` · ${rev.kind}` : ""}`,
      subtitle: rev.label || "无说明",
      extra: rev.scene_url ? `scene：${rev.scene_url}` : "无绑定场景 URL",
    })),
    { emptyText: "无历史记录" },
  );
  setText(el.hintHistory, fx.history.length ? `${demoMode ? "演示数据：" : ""}历史是线性列表，撤销/重做需要后端校验 expected_head。` : "未提供修订列表；本构建不伪造 revision。");
}

// ===========================================================================
// 硬错误列表（渲染诊断 + 夹具问题）
// ===========================================================================

function collectIssues(diag) {
  const issues = [];
  const sample = (block, code, describe, limit = 4) => {
    if (!block || !block.count) return;
    for (const entry of (block.entries || []).slice(0, limit)) {
      issues.push({
        code,
        message: describe(entry),
        pos_local: entry.pos_local || null,
        severity: "error",
      });
    }
    if (block.count > limit) {
      issues.push({ code, message: `${describe({})} 等共 ${block.count} 处（诊断里只保留前 ${block.limit} 条样本）`, pos_local: null, severity: "error" });
    }
  };
  sample(diag.missing_block_definitions, "RENDER_RESOURCE_MISSING", (e) => `方块定义缺失：${e.name || ""}${e.props && Object.keys(e.props).length ? ` ${JSON.stringify(e.props)}` : ""}（该体素用显示代理 ${diag.proxy_block} 渲染，绝不静默跳过）`);
  sample(diag.missing_models, "RENDER_RESOURCE_MISSING", (e) => `模型缺失：${e.name || ""} → ${(e.missing_models || []).join(", ")}`);
  sample(diag.missing_textures, "RENDER_RESOURCE_MISSING", (e) => `贴图缺失：${e.name || ""} → ${(e.missing_textures || []).join(", ")}`);
  sample(diag.unresolved_variants, "RENDER_RESOURCE_MISSING", (e) => `BlockState 变体无法解析：${e.name || ""} ${e.props ? JSON.stringify(e.props) : ""}`);
  sample(diag.invalid_palette_indices, "RENDER_PAYLOAD_MISMATCH", (e) => `调色板下标越界：state=${e.state_index}（palette 长度 ${e.palette_size}）→ 显示代理`);
  sample(diag.addblock_errors, "RENDER_ERROR", (e) => `addBlock 失败：${e.name || ""} ${e.error || ""}`);
  for (const err of diag.webgl_errors || []) {
    issues.push({ code: "RENDER_WEBGL_ERROR", message: `WebGL 错误 ${err.name}（第 ${err.sequence} 帧）`, pos_local: null, severity: "error" });
  }
  if (diag.diff_overlay && diag.diff_overlay.errors && diag.diff_overlay.errors.length) {
    for (const message of diag.diff_overlay.errors.slice(0, 4)) {
      issues.push({ code: "RENDER_PAYLOAD_MISMATCH", message: `Diff 载荷：${message}`, pos_local: null, severity: "error" });
    }
  }
  for (const note of diag.notes || []) {
    issues.push({ code: "RENDER_NOTE", message: note, pos_local: null, severity: "info" });
  }
  return issues;
}

function renderIssues() {
  const diag = state.diagnostics;
  const rows = [];
  if (diag) {
    for (const issue of collectIssues(diag)) rows.push(issue);
  }
  for (const issue of state.fixture.issues) {
    rows.push({
      code: issue.code || "ISSUE",
      message: `${issue.message || issue.rule_id || "（无说明）"}${issue.expected ? ` 期望 ${issue.expected}` : ""}${issue.actual ? ` 实际 ${issue.actual}` : ""}`,
      pos_local: issue.pos_local || null,
      severity: issue.severity || "error",
      fixture: true,
    });
  }
  clear(el.issueList);
  setText(el.countIssues, String(rows.length));
  if (!rows.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "当前渲染诊断与夹具都没有硬错误。";
    el.issueList.appendChild(li);
    return;
  }
  rows.slice(0, 60).forEach((issue, index) => {
    const li = document.createElement("li");
    li.style.setProperty("--i", String(Math.min(index, 12)));
    const head = document.createElement("div");
    head.className = "issue__head";
    const code = document.createElement("b");
    code.textContent = issue.code;
    head.appendChild(code);
    if (issue.pos_local) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "issue__locate";
      btn.textContent = "定位";
      btn.addEventListener("click", () => locateIssue(issue));
      head.appendChild(btn);
    } else {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = issue.severity === "info" ? "提示" : "无坐标";
      head.appendChild(chip);
    }
    li.appendChild(head);
    const message = document.createElement("span");
    message.textContent = issue.message;
    li.appendChild(message);
    if (issue.pos_local) {
      const pos = document.createElement("span");
      pos.className = "mono";
      pos.textContent = `pos_local ${fmtTriple(issue.pos_local)}${issue.fixture ? "（夹具数据）" : ""}`;
      li.appendChild(pos);
    }
    el.issueList.appendChild(li);
  });
}

function locateIssue(issue) {
  if (!issue.pos_local) return;
  adapter.highlightIssue(issue);
  adapter.focusOnPoint(issue.pos_local, {});
  const probe = adapter.probeVoxel(issue.pos_local);
  updateBeforeAfter(issue.pos_local, probe, issue);
  toast(`已定位 ${issue.code} @ ${fmtTriple(issue.pos_local)}（camera 已对准该点）`);
}

function updateBeforeAfter(posLocal, probe, issue) {
  setText(el.baPos, `${fmtTriple(posLocal)}（project_local）`);
  const display = probe && probe.display_state ? fmtState(probe.display_state) : "crop 之外 / 无数据";
  setText(el.baBefore, display);
  const change = findDiffAt(posLocal);
  if (issue) {
    setText(el.baAfter, "—（硬错误定位，不是净变化）");
    setText(el.baCategory, issue.code);
    return;
  }
  if (state.diff && state.diff.ok) {
    if (change) {
      setText(el.baAfter, fmtState(change.after));
      setText(el.baCategory, CATEGORY_STYLE[change.category] ? `${CATEGORY_STYLE[change.category].label}` : change.category);
    } else {
      setText(el.baAfter, "（本次净变化里没有这个坐标）");
      setText(el.baCategory, "无变化");
    }
  } else {
    setText(el.baAfter, "未加载候选/净变化");
    setText(el.baCategory, "—");
  }
  if (probe && probe.render_state && probe.render_state.proxy) {
    setText(el.baBefore, `${display} → 显示代理 ${DISPLAY_PROXY_BLOCK}（${probe.render_state.proxy_reason}）`);
  }
}

function findDiffAt(posLocal) {
  if (!state.diff || !state.diff.ok) return null;
  for (const category of CATEGORY_ORDER) {
    for (const change of state.diff.categories[category]) {
      if (change.pos_local[0] === posLocal[0] && change.pos_local[1] === posLocal[1] && change.pos_local[2] === posLocal[2]) return change;
    }
  }
  return null;
}

// ===========================================================================
// 场景加载
// ===========================================================================

async function fetchJson(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}（${url}）`);
  return await res.json();
}

async function loadSceneUrl(url, { label = "当前" } = {}) {
  const seq = ++state.loadSeq;
  if (!url) {
    showNotice(
      `没有${label}场景数据`,
      `本构建需要 URL 参数指向一个 RenderScene JSON（例如 ?scene=../work/sample_render_scene.json）。` +
        `后端路由 GET /api/candidates/{c}/render-scene 尚未实现，前端不伪造场景。`,
      "empty",
    );
    setStatus("NO_DATA", `${label}场景未提供`);
    return;
  }
  setStatus(RENDER_STATUS.LOADING, `正在加载${label}场景：${url}`);
  showNotice("正在加载…", url, "ok");

  let payload;
  try {
    payload = await fetchJson(url);
  } catch (err) {
    if (seq !== state.loadSeq) return; // 迟到的旧响应：丢弃
    const message = err && err.message ? err.message : String(err);
    showNotice("场景加载失败", `${message}\n这是**加载失败**，不是空选区：请确认静态服务从仓库根目录启动（python -m http.server 8123），且 URL 相对 web/ 正确。`, "error");
    setStatus(RENDER_STATUS.ERROR, `SCENE_LOAD_FAILED：${message}`);
    addLocalIssue("RENDER_PAYLOAD_MISMATCH", `场景加载失败：${message}`, null);
    return;
  }
  if (seq !== state.loadSeq) return;

  const check = validateRenderScene(payload);
  if (!check.ok) {
    showNotice("RenderScene 预检失败", check.errors.join("\n"), "error");
    setStatus(RENDER_STATUS.ERROR, `INVALID_RENDER_SCENE：${check.errors[0]}`);
    addLocalIssue("RENDER_PAYLOAD_MISMATCH", `预检失败：${check.errors.join("；")}`, null);
    return;
  }
  for (const warning of check.warnings) addLocalIssue("RENDER_PAYLOAD_MISMATCH", warning, null);

  let receipt;
  try {
    receipt = await adapter.loadScene(payload);
  } catch (err) {
    if (seq !== state.loadSeq) return;
    const code = err && err.code ? err.code : "RENDER_FAILED";
    const message = err && err.message ? err.message : String(err);
    if (code === "RENDER_UNAVAILABLE") {
      showNotice(
        "渲染不可用：RENDER_UNAVAILABLE",
        `${message}\n本次构建没有可用的 WebGL 上下文，因此**不会**产生任何"渲染成功"的结论：blank 画面不等于通过。`,
        "error",
      );
      setStatus(RENDER_STATUS.UNAVAILABLE, `RENDER_UNAVAILABLE：${message}`);
    } else if (code === "RENDER_SUPERSEDED") {
      // 竞态：新的加载已取代旧请求，静默丢弃即可
      return;
    } else {
      showNotice(`加载被拒绝：${code}`, message, "error");
      setStatus(RENDER_STATUS.ERROR, `${code}：${message}`);
    }
    addLocalIssue(code, message, null);
    return;
  }
  if (seq !== state.loadSeq) return;

  state.receipt = receipt;
  state.sceneInfo = adapter.getSceneInfo();
  hideNotice();
  applySceneToUi(payload, receipt);
}

function addLocalIssue(code, message, posLocal) {
  state.localIssues = state.localIssues || [];
  state.localIssues.push({ code, message, pos_local: posLocal || null, severity: "error" });
  renderIssues();
}

function applySceneToUi(payload, receipt) {
  const info = state.sceneInfo;
  setText(el.factSceneHash, shortHash(info.scene_hash, 16));
  el.factSceneHash.title = info.scene_hash || "";
  setText(el.factFileHash, shortHash(info.file_sha256, 16));
  el.factFileHash.title = info.file_sha256 || "payload 未带 file_sha256";
  setText(el.factProject, state.project);
  setText(el.factRevision, state.revision);

  // 切片控件：crop 局部 Y 全范围
  const sy = info.size[1];
  for (const input of [el.sliceMin, el.sliceMax]) {
    input.min = "0";
    input.max = String(Math.max(0, sy - 1));
  }
  const initial = params.get("slice");
  let sMin = 0;
  let sMax = Math.max(0, sy - 1);
  if (initial && /^\d+:\d+$/.test(initial)) {
    const [a, b] = initial.split(":").map(Number);
    sMin = Math.max(0, Math.min(a, b));
    sMax = Math.min(sy - 1, Math.max(a, b));
  }
  state.slice = { min: sMin, max: sMax };
  el.sliceMin.value = String(sMin);
  el.sliceMax.value = String(sMax);
  updateSliceReadout();

  // 选择器接管新场景：传**原始载荷**（含 idx/state），俯视图要按它建高度图
  state.scenePayload = payload;
  selection.setScene(payload, adapter.getDisplaySwatches());
  el.planeY.min = "0";
  el.planeY.max = String(Math.max(0, sy - 1));
  el.planeY.value = String(sMin);
  adapter.setWorkPlaneY(sMin);

  // 净变化按 crop 生成几何盒（可能在场景之前就加载好了）
  applyDiff();

  // ?slice=min:max：初始切片不是全层时，真正切一次并等该 build 提交首帧
  if (sMin !== 0 || sMax !== sy - 1) {
    adapter.setSlice({ min: sMin, max: sMax }).catch((err) => {
      toast(`初始切片失败：${err && err.code ? err.code : ""} ${err && err.message ? err.message : ""}`, "error");
    });
  }

  // 状态栏
  const diag = adapter.getDiagnostics();
  state.diagnostics = diag;
  const statusLine = [
    `scene_id ${info.scene_id}`,
    `crop ${info.size.join("×")}`,
    `crop 原点（project_local）${fmtTriple(info.crop_origin_local)}`,
    `非空气 ${info.counts.non_air_voxels}`,
    `提交模型 ${diag.accepted_voxel_count}`,
    `渲染器构建 ${diag.renderer_builds} 次`,
    `build ${diag.renderer_build_hash ? shortHash(diag.renderer_build_hash, 12) : "—"}`,
    `帧 ${diag.last_frame_sequence}`,
  ].join(" · ");
  setStatus(info.status, statusLine);

  if (info.empty) {
    showNotice(
      "该选区没有方块（合法状态）",
      `counts.non_air_voxels = ${info.counts.non_air_voxels}；empty = true。` +
        `这是**空选区**，不是加载失败：请换 crop 或检查后端给出的选区。`,
      "empty",
    );
  }
  toast(`已渲染 ${info.scene_id}（帧 ${receipt.frames_rendered}，build ${receipt.build_id}）`);
  refreshDiagnostics();
  refreshHud();
}

function updateSliceReadout() {
  const info = state.sceneInfo;
  if (!info) {
    setText(el.sliceReadout, "—");
    return;
  }
  const originY = info.crop_origin_local[1];
  const full = state.slice.min === 0 && state.slice.max === info.size[1] - 1;
  setText(
    el.sliceReadout,
    full
      ? `全层 0–${info.size[1] - 1}（crop 局部）`
      : `crop 局部 ${state.slice.min}–${state.slice.max} · project_local ${originY + state.slice.min}–${originY + state.slice.max}`,
  );
}

let sliceTimer = null;
function scheduleSliceCommit() {
  if (sliceTimer) clearTimeout(sliceTimer);
  sliceTimer = setTimeout(() => {
    commitSlice();
  }, 160);
}

function commitSlice() {
  if (!state.sceneInfo) return;
  const min = Number(el.sliceMin.value);
  const max = Number(el.sliceMax.value);
  state.slice = { min: Math.min(min, max), max: Math.max(min, max) };
  updateSliceReadout();
  adapter
    .setSlice(state.slice)
    .then(() => refreshDiagnostics())
    .catch((err) => {
      if (err && err.code === "RENDER_DISPOSED") return;
      toast(`切片更新失败：${err && err.code ? err.code : "RENDER_FAILED"} ${err && err.message ? err.message : ""}`, "error");
      setStatus(err && err.code ? err.code : RENDER_STATUS.ERROR, err && err.message ? err.message : String(err));
    });
}

// ===========================================================================
// Diff 加载
// ===========================================================================

async function loadDiffUrl(url) {
  if (!url) {
    adapter.setDiff(null);
    state.diff = null;
    state.diffPayload = null;
    renderDiffLegend(null, null);
    return;
  }
  try {
    const payload = await fetchJson(url);
    state.diffPayload = payload;
    state.diff = normalizeDiff(payload);
    applyDiff();
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    state.diffPayload = null;
    renderDiffLegend(null, null);
    setText(el.diffNote, `净变化加载失败：${message}`);
    addLocalIssue("RENDER_PAYLOAD_MISMATCH", `净变化加载失败：${message}`, null);
  }
}

/**
 * 把净变化交给视图并刷新图例。几何盒要按 crop 转换，所以场景就绪后再调用一次。
 * 注意：叠加层不受渲染切片影响（切片只改变实体渲染），图例里会写明。
 */
function applyDiff() {
  if (!state.diffPayload) return;
  adapter.setDiff(state.diffPayload);
  const info = state.sceneInfo;
  const boxes = info
    ? buildDiffBoxes(state.diff, { size: info.size, crop_origin_local: info.crop_origin_local })
    : null;
  renderDiffLegend(state.diff, boxes);
}

// ===========================================================================
// 面板夹具
// ===========================================================================

async function loadPanelsUrl(url) {
  if (!url) return;
  try {
    const payload = await fetchJson(url);
    state.fixture = {
      protected_areas: Array.isArray(payload.protected_areas) ? payload.protected_areas : [],
      assets: Array.isArray(payload.assets) ? payload.assets : [],
      history: Array.isArray(payload.history) ? payload.history : [],
      issues: Array.isArray(payload.issues) ? payload.issues : [],
    };
    state.fixtureLoaded = true;
    renderPanels();
    renderIssues();
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    toast(`面板数据加载失败：${message}`, "error");
  }
}

// ===========================================================================
// 冻结请求预览 + 占位动作
// ===========================================================================

function updateRequestPreview() {
  const sel = selection.getSelection();
  const text = el.instruction.value;
  setText(el.instructionHint, `${text.length} 字符。生成需要后端 POST /api/projects/{p}/redesign-tasks；本构建只显示冻结请求预览（不会发送）。`);
  const yRange = selection.getYRange();
  const preview = {
    // 这些字段名对应任务书 §12.1 的 request.json（冻结 base / 选区 / 指令 / 权限）
    schema_version: "0.2",
    dry_run_preview: true,
    note: "本预览**没有发送**到任何后端：后端 redesign-tasks 路由未实现。",
    project_ref: state.project,
    base_revision: state.revision,
    scene_hash: state.sceneInfo ? state.sceneInfo.scene_hash : null,
    selection: sel
      ? { bounds: { min: sel.min, max_exclusive: sel.max_exclusive }, coordinate_space: sel.coordinate_space, source: sel.source }
      : null,
    authorization_y_range: { min: yRange.min, max: yRange.max, unit: "project_local Y（含端点）" },
    render_slice_note: "渲染切片只影响显示，不参与授权范围",
    instruction: text,
    requested_permissions: ["edit_selection_only"],
  };
  setText(el.requestPreview, JSON.stringify(preview, null, 2));
}

function reportPlaceholder(action, hint) {
  const message = `${action}：本构建未实现（${hint}）。没有执行任何写操作，也没有伪造结果。`;
  toast(message, "warn");
  setText(el.jobNote, message);
}

// ===========================================================================
// 诊断面板
// ===========================================================================

function refreshDiagnostics() {
  const diag = adapter.getDiagnostics();
  state.diagnostics = diag;
  const merged = { ...diag, local_issues: state.localIssues || [] };
  setText(el.diagJson, JSON.stringify(merged, null, 2));
  renderIssues();
  return merged;
}

// ===========================================================================
// 事件绑定
// ===========================================================================

function bindSegmented(container, attr, onPick) {
  container.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button");
    if (!btn || !container.contains(btn)) return;
    if (btn.getAttribute("aria-disabled") === "true") {
      toast(`该选项不可用：${btn.title || btn.textContent}`, "warn");
      return;
    }
    for (const other of container.querySelectorAll("button")) other.setAttribute("aria-checked", "false");
    btn.setAttribute("aria-checked", "true");
    onPick(btn.dataset[attr], btn);
  });
}

const MODE_HINTS = {
  navigate: "导航：左键旋转 · 中/右键或 Shift+左键平移 · 滚轮推拉 · WASD/方向键/Shift/Space 移动（输入框获得焦点时自动屏蔽）",
  select: "框选：在 3D 视口左键拖动 = 在工作平面 Y 上取 x/z 矩形（不旋转相机）；右下俯视图也能拖选",
  pick: "拾取：左键点击方块 = 设角点（第一次 A、第二次 B），命中体素格；不代表真实碰撞",
};

function setMode(mode) {
  adapter.setInteractionMode(mode);
  const canvas = adapter.element;
  canvas.classList.toggle("mode-select", mode === "select");
  canvas.classList.toggle("mode-pick", mode === "pick");
  setText(el.chipModeHint, MODE_HINTS[mode] || "");
  for (const btn of el.modeSwitch.querySelectorAll("button")) {
    btn.setAttribute("aria-checked", btn.dataset.mode === mode ? "true" : "false");
  }
}

function setView(view) {
  state.view = view;
  if (view === "current") {
    loadSceneUrl(urls.scene, { label: "当前" });
    return;
  }
  if (view === "candidate") {
    if (!urls.candidate) {
      showNotice(
        "没有候选场景",
        "本构建没有候选数据：候选由后端 GET /api/candidates/{c}/render-scene 提供（未实现）。" +
          "可用 ?candidate=<url> 指向一个候选 RenderScene JSON 做视图验证。",
        "empty",
      );
      addLocalIssue("RENDER_PAYLOAD_MISMATCH", "候选场景未提供（后端路由未实现）", null);
      return;
    }
    loadSceneUrl(urls.candidate, { label: "候选" });
    return;
  }
  // Diff：场景保持当前，叠加层打开
  if (!state.diff) {
    showNotice(
      "没有净变化数据",
      "Diff 叠加需要后端精确净补丁（GET /api/candidates/{c}/diff）。可用 ?diff=<url> 指向一个 JSON。",
      "empty",
    );
    return;
  }
  adapter.setDiffVisibility(null);
  hideNotice();
  refreshDiagnostics();
}

function resetView() {
  const info = state.sceneInfo;
  if (!info) return;
  const size = info.size;
  adapter.setCamera({ pitch: 0.8, yaw: 0.5, camera_pos: [-size[0] / 2, -size[1] / 2, -size[2] / 2] });
  refreshHud();
}

el.modeSwitch.addEventListener("keydown", (ev) => {
  if (!["ArrowLeft", "ArrowRight"].includes(ev.key)) return;
  const buttons = Array.from(el.modeSwitch.querySelectorAll("button"));
  const index = buttons.findIndex((b) => b.getAttribute("aria-checked") === "true");
  const next = buttons[(index + (ev.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length];
  ev.preventDefault();
  next.focus();
  next.click();
});

bindSegmented(el.modeSwitch, "mode", (mode) => setMode(mode));

bindSegmented(el.sceneSwitch, "view", (view) => {
  setView(view);
});

el.btnResetView.addEventListener("click", resetView);

for (const input of [el.sliceMin, el.sliceMax]) {
  input.addEventListener("input", () => {
    const a = Number(el.sliceMin.value);
    const b = Number(el.sliceMax.value);
    state.slice = { min: Math.min(a, b), max: Math.max(a, b) };
    updateSliceReadout();
    scheduleSliceCommit();
  });
  input.addEventListener("change", () => {
    if (sliceTimer) clearTimeout(sliceTimer);
    commitSlice();
  });
}

el.btnSliceFull.addEventListener("click", () => {
  const info = state.sceneInfo;
  if (!info) return;
  el.sliceMin.value = "0";
  el.sliceMax.value = String(info.size[1] - 1);
  commitSlice();
});

el.layerGrid.addEventListener("change", () => adapter.setDisplayOption("grid", el.layerGrid.checked));
el.layerHiddenOutlines.addEventListener("change", () => adapter.setDisplayOption("hidden_layer_outlines", el.layerHiddenOutlines.checked));
el.layerOverlay.addEventListener("change", () => adapter.setDisplayOption("overlay_boxes", el.layerOverlay.checked));
el.layerFade.addEventListener("change", () => adapter.setDisplayOption("frustum_fade", el.layerFade.checked));

el.planeY.addEventListener("change", () => {
  const value = Number(el.planeY.value);
  if (!Number.isFinite(value)) return;
  adapter.setWorkPlaneY(value);
  el.planeY.value = String(adapter.getWorkPlaneY());
  toast(`3D 框选工作平面：crop 局部 y=${adapter.getWorkPlaneY()}（project_local y=${(state.sceneInfo ? state.sceneInfo.crop_origin_local[1] : 0) + adapter.getWorkPlaneY()}）`);
});

el.instruction.addEventListener("input", updateRequestPreview);

el.btnGenerate.addEventListener("click", () => {
  reportPlaceholder("生成候选", "需要后端 POST /api/projects/{p}/redesign-tasks 与任务状态机");
  setText(el.jobState, "未开始（占位）");
  setText(el.jobNote, "冻结请求预览已生成，但未发送；后端任务接口未实现。");
});
el.btnExport.addEventListener("click", () => reportPlaceholder("导出", "需要后端 POST /api/projects/{p}/exports"));
el.btnAccept.addEventListener("click", () => reportPlaceholder("接受候选", "需要后端 CAS 提交（expected_head/generation）"));
el.btnReject.addEventListener("click", () => reportPlaceholder("拒绝候选", "需要后端候选资格状态"));
el.btnUndo.addEventListener("click", () => reportPlaceholder("撤销", "需要后端线性历史与 expected_head 校验"));
el.btnRedo.addEventListener("click", () => reportPlaceholder("重做", "需要后端线性历史与 expected_head 校验"));

el.btnRefreshDiag.addEventListener("click", () => {
  refreshDiagnostics();
  toast("诊断已刷新");
});

el.btnCopyDiag.addEventListener("click", async () => {
  const text = el.diagJson.textContent || "";
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      toast("诊断 JSON 已复制到剪贴板");
    } else {
      throw new Error("剪贴板 API 不可用（需要安全上下文）");
    }
  } catch (err) {
    toast(`复制失败：${err && err.message ? err.message : err}（可以直接从面板里选中文本）`, "warn");
  }
});

window.addEventListener("keydown", (ev) => {
  // 输入框里绝不拦截：让浏览器的文本撤销正常工作
  if (isEditableTarget(ev.target) || isEditableTarget(document.activeElement)) return;
  const mod = ev.ctrlKey || ev.metaKey;
  if (!mod) return;
  const key = ev.key.toLowerCase();
  if (key === "z") {
    ev.preventDefault();
    reportPlaceholder(ev.shiftKey ? "重做" : "撤销", "需要后端线性历史");
  } else if (key === "y") {
    ev.preventDefault();
    reportPlaceholder("重做", "需要后端线性历史");
  }
});

// 适配层事件
adapter.on("camera", () => refreshHud());
adapter.on("scene", () => refreshHud());
adapter.on("viewport", () => refreshHud());
adapter.on("hover", (info) => {
  state.hoverInfo = info;
  if (!info) {
    setText(el.pickBlock, "—");
    return;
  }
  const v = info.info;
  setText(el.pickBlock, `${v.display_state ? v.display_state.name : "空"}${v.render_state && v.render_state.proxy ? `（显示代理 ${DISPLAY_PROXY_BLOCK}）` : ""}`);
  setText(el.pickFace, info.face || "—");
});
adapter.on("pick", ({ hit, info }) => {
  if (!hit || !info) {
    toast("这一击没有命中任何体素（射线穿过空处或超出 crop）", "warn");
    return;
  }
  if (!state.pickA) {
    state.pickA = hit.pos_local;
    setText(el.pickA, fmtTriple(hit.pos_local));
    setText(el.pickB, "—");
    toast(`角点 A = ${fmtTriple(hit.pos_local)}（体素格，命中面 ${hit.face || "—"}）`);
  } else if (!state.pickB) {
    state.pickB = hit.pos_local;
    setText(el.pickB, fmtTriple(hit.pos_local));
    const a = state.pickA;
    const b = state.pickB;
    const min = [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.min(a[2], b[2])];
    const maxExclusive = [Math.max(a[0], b[0]) + 1, Math.max(a[1], b[1]) + 1, Math.max(a[2], b[2]) + 1];
    selection.setSelection({ min, max_exclusive: maxExclusive }, "pick");
    toast(`两角点选区：${fmtTriple(min)} → ${fmtTriple(maxExclusive)}（体素格范围，非碰撞结论）`);
  } else {
    state.pickA = hit.pos_local;
    state.pickB = null;
    setText(el.pickA, fmtTriple(hit.pos_local));
    setText(el.pickB, "—");
    toast(`重新开始：角点 A = ${fmtTriple(hit.pos_local)}`);
  }
  const probe = info;
  updateBeforeAfter(hit.pos_local, probe, null);
});

adapter.on("marquee_commit", ({ rect_local }) => {
  const yRange = selection.getYRange();
  const min = [rect_local.min[0], yRange.min, rect_local.min[2]];
  const maxExclusive = [rect_local.max_exclusive[0], yRange.max + 1, rect_local.max_exclusive[2]];
  selection.setSelection({ min, max_exclusive: maxExclusive }, "plane");
  toast(`3D 平面框选：x/z 取自工作平面，Y 取自授权范围 [${yRange.min}, ${yRange.max}]（切片不参与）`);
});

// ===========================================================================
// 启动
// ===========================================================================

function boot() {
  setMode(params.get("mode") || "navigate");
  setStatus("IDLE", `场景 URL：${urls.scene}${urls.candidate ? `；候选 URL：${urls.candidate}` : ""}`);
  setText(el.factProject, state.project);
  setText(el.factRevision, state.revision);
  setText(el.diagJson, "（尚未加载场景）");
  renderPanels();
  renderSelectionReadout(null);
  updateRequestPreview();
  renderDiffLegend(null, null);

  if (urls.candidate) {
    el.viewCandidate.title = urls.candidate;
  } else {
    el.viewCandidate.setAttribute("aria-disabled", "true");
    el.viewCandidate.title = "未提供 ?candidate=<url>：后端候选路由未实现";
  }
  if (demoMode) {
    toast("演示模式：?demo=1 加载 web/fixtures/ 下的演示数据（界面上会标注「演示数据」）", "warn");
  }

  loadPanelsUrl(urls.panels);
  loadDiffUrl(urls.diff);
  loadSceneUrl(urls.scene, { label: "当前" });

  // 定时刷新诊断（只读，不触发渲染）
  setInterval(() => {
    if (document.hidden) return;
    refreshDiagnostics();
    refreshHud();
  }, 4000);
}

boot();
