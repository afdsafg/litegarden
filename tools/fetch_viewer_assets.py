"""Fetch and hash-verify the pinned Viewer runtime resources.

The upstream renderer pulls Deepslate and gl-matrix from a CDN and ships a large
texture atlas plus a generated assets file. Committing ~3.6 MB of third-party
binaries into this repository is not wanted, so this script materialises them
into ``web/vendor/litematica-viewer/`` (git-ignored) and verifies every byte
against ``third_party/viewer/viewer.lock.json`` before handing them to the
browser. Screenshot automation therefore never depends on a CDN, and a tampered
resource fails loudly instead of rendering something subtly wrong.

Usage:
    python tools/fetch_viewer_assets.py            # fetch what is missing
    python tools/fetch_viewer_assets.py --verify   # only re-verify
    python tools/fetch_viewer_assets.py --force    # re-download everything
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "third_party" / "viewer" / "viewer.lock.json"
VENDOR = ROOT / "web" / "vendor" / "litematica-viewer"

# Local file name -> (upstream source path in the lock, or an explicit URL)
FROM_LOCK = {
    "resource/assets.js": "script/JSrender/resource/assets.js",
    "resource/opaque.js": "script/JSrender/resource/opaque.js",
    "resource/atlas.png": "script/JSrender/resource/atlas.png",
}
FROM_CDN = {
    "resource/vendor/deepslate-0.10.1.js": "https://unpkg.com/deepslate@0.10.1",
    "resource/vendor/gl-matrix-3.4.3.js": "https://unpkg.com/gl-matrix@3.4.3/gl-matrix-min.js",
}


def sha256(path: Path, *, normalize_text: bool = True) -> str:
    """Hash a file the way viewer.lock.json records it: LF-normalised text.

    The same upstream blob is CRLF in a Windows checkout and LF over GitHub raw,
    so hashing raw bytes would report false mismatches. Binaries are hashed
    as-is (detected by a NUL byte in the first block).
    """
    data = path.read_bytes()
    if normalize_text and b"\x00" not in data[:8192]:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    tmp.replace(dest)


def main(argv: list[str]) -> int:
    verify_only = "--verify" in argv
    force = "--force" in argv
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    commit = lock["commit"]

    failures: list[str] = []
    fetched: list[str] = []

    # upstream resources: prefer a local clone of the pinned commit, else GitHub raw
    for local_rel, upstream_rel in FROM_LOCK.items():
        dest = VENDOR / local_rel
        expected = lock["runtime_resources"].get(upstream_rel, {}).get("sha256")
        if dest.exists() and not force and sha256(dest) == expected:
            continue
        if verify_only:
            failures.append(f"{local_rel}: missing or hash mismatch")
            continue
        url = (
            f"https://raw.githubusercontent.com/albertchen857/Litematica-viewer/"
            f"{commit}/{upstream_rel}"
        )
        try:
            download(url, dest)
            fetched.append(local_rel)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{local_rel}: download failed ({exc})")
            continue
        got = sha256(dest)
        if expected and got != expected:
            failures.append(f"{local_rel}: sha256 {got} != locked {expected}")

    for local_rel, url in FROM_CDN.items():
        dest = VENDOR / local_rel
        if dest.exists() and not force and not verify_only:
            continue
        if verify_only:
            if not dest.exists():
                failures.append(f"{local_rel}: missing")
            continue
        try:
            download(url, dest)
            fetched.append(local_rel)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{local_rel}: download failed ({exc})")

    if failures:
        print("viewer assets NOT ready:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"viewer assets ready in {VENDOR}")
    if fetched:
        print("fetched: " + ", ".join(fetched))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
