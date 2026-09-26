// ---------------------------------------------------------------------------
// 上游派生：只提取 Litematica-viewer 与「渲染」有关的部分
//   上游：Litematica-viewer @ 65f41744eb372c8ffa40e23462fd13cb6168133f
//   script/JSrender/src/viewer.js  ->  本文件（来源见 third_party/viewer/UPSTREAM.md）
//
// 提取内容（函数体与上游逐行一致，未做任何语义改动）：
//   * 全局变量：webglContext / deepslateRenderer / cameraPitch / cameraYaw / cameraPos
//   * setStructure(structure, reset_view = false)
//   * render()
//
// **不包含** 上游的 createRenderCanvas()。原因（不是遗漏）：
//   1. 它把 canvas 尺寸绑到 window.innerWidth/Height，本项目要按中央视口 + DPR 自适应；
//   2. 它把 mousedown/mousemove/keydown/keyup/wheel/touch 监听器挂在 document / window 上，
//      且没有任何解除路径；本项目要求「输入框获得焦点时屏蔽 WASD/空格」，
//      并且场景切换后只保留一组受管理的监听器；
//   3. 它内部还带 localStorage 设置读取（runMovementFunction 会读 document.getElementById(setting)），
//      与本项目的状态管理无关，且一旦抛错会连带毁掉渲染。
//   因此画布创建、输入绑定、DPR 处理由 web/viewer_adapter.js 自己实现，
//   本文件只提供「建立渲染器」和「画一帧」的语义。
//
// 单位与坐标系（与上游一致）：
//   * structure 的方块坐标是 **crop 局部整数格**（p_render），原点是裁剪盒的 min 角；
//   * cameraPos 是**参与 view matrix 的平移量**，不是世界相机 eye 位置；
//     上游初始值是 -size/2，即把结构中心搬到原点附近。
// ---------------------------------------------------------------------------

const { mat4, vec3 } = glMatrix;

// 由 web/viewer_adapter.js 赋值的 WebGL 上下文（同一个 canvas 只有一个）。
var webglContext;

// 当前渲染器实例。适配层复用同一个实例：切片变化走 renderer.setStructure()，
// 不重建（Deepslate 的 StructureRenderer 没有 dispose，重建会泄漏着色器与贴图）。
var deepslateRenderer;

var cameraPitch; // X rotation（弧度）
var cameraYaw;   // Y rotation（弧度）
var cameraPos;   // 参与 view matrix 的平移量（crop 局部单位）

// 上游语义：新建渲染器；reset_view 为真时把相机摆到结构中心的斜视角。
// 适配层只在**首次**建立场景时调用它，之后一律走 renderer.setStructure()。
function setStructure(structure, reset_view = false) {

  // Create Deepslate Renderer
  // Need chunksize 8 as seems to be a max number of faces per chunk that will render
  deepslateRenderer = new deepslate.StructureRenderer(webglContext, structure, deepslateResources, options={chunkSize: 8});

  if (reset_view) {
    cameraPitch = 0.8; cameraYaw = 0.5;
    const size = structure.getSize();
    vec3.set(cameraPos, -size[0] / 2, -size[1] / 2, -size[2] / 2);
  }

  requestAnimationFrame(render);
}

function render() {

  // Clamp / normalise view
  cameraYaw = cameraYaw % (Math.PI * 2);
  cameraPitch = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, cameraPitch));

  const view = mat4.create();
  mat4.rotateX(view, view, cameraPitch);
  mat4.rotateY(view, view, cameraYaw);
  mat4.translate(view, view, cameraPos);

  deepslateRenderer.drawStructure(view);
  deepslateRenderer.drawGrid(view);
}
