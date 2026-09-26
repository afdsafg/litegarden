# 上游 Viewer 锁定记录

- 仓库：https://github.com/albertchen857/Litematica-viewer.git
- commit：`65f41744eb372c8ffa40e23462fd13cb6168133f`（2026-08-21T15:08:17+10:00）
- 许可证：MIT（`third_party/viewer/upstream/LICENSE`，sha256 见 `viewer.lock.json`）

## 本项目实际派生/参考的文件（逐文件 hash 见 `viewer.lock.json`）

- `LICENSE` → `third_party/viewer/upstream/LICENSE`（1091 bytes, sha256 `ddd46856e91d5837…`）
- `script/JSrender/viewer.html` → `third_party/viewer/upstream/script__JSrender__viewer.html`（2805 bytes, sha256 `e48212bb4a20f141…`）
- `script/JSrender/src/bridge.js` → `third_party/viewer/upstream/script__JSrender__src__bridge.js`（5639 bytes, sha256 `104393f9d305bd2d…`）
- `script/JSrender/src/viewer.js` → `third_party/viewer/upstream/script__JSrender__src__viewer.js`（8026 bytes, sha256 `afaab5e8ccebf0e9…`）
- `script/JSrender/src/deepslate-helpers.js` → `third_party/viewer/upstream/script__JSrender__src__deepslate-helpers.js`（3823 bytes, sha256 `643df4d16ce1e302…`）
- `script/JSrender/src/litematic-utils.js` → `third_party/viewer/upstream/script__JSrender__src__litematic-utils.js`（5311 bytes, sha256 `45eb837c2a253c0e…`）

## 运行时资源（**未提交**，按 hash 拉取）

| 上游路径 | 字节 | sha256 前缀 |
|---|---|---|
| `script/JSrender/resource/assets.js` | 1711662 | `fa00806d24915b6e…` |
| `script/JSrender/resource/opaque.js` | 12955 | `95e0069cd1f0c2d0…` |
| `script/JSrender/resource/atlas.png` | 1815365 | `e1c6e94326cb45a6…` |

拉取并校验：`python tools/fetch_viewer_assets.py`（写入 `web/vendor/litematica-viewer/`，被 .gitignore 忽略）。

## 前端依赖（页面原为 CDN，本项目改为本地锁定副本）

- deepslate: https://unpkg.com/deepslate@0.10.1
- gl-matrix: https://unpkg.com/gl-matrix@3.4.3/gl-matrix-min.js

## 本地修改

未修改上游文件：`third_party/viewer/upstream/` 下是逐字节副本。
本项目在 `web/vendor/litematica-viewer/` 中**新增**适配层（`litematica-viewer-adapter.js`），
不改写上游脚本；上游仍是全局脚本风格，适配层负责单实例生命周期、相机协议、
切片、诊断与加载完成回报。

## 已验证范围

尚未验证任何 MinecraftDataVersion 与渲染器的对应关系（P6 游戏内验收未执行）。
地面真值以 Deepslate 版本与上游 `resource/assets.js` 为准，缺资源必须显式诊断而非静默少画。
