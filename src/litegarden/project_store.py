"""Project store: immutable revisions, HEAD pointer, atomic commits, linear undo/redo.

Implements the minimal persistence layout of spec 12.1 for one project:

.. code-block:: text

    projects/<project_id>/
      project.json                     project settings, schema, target game version
      source/original.litematic        B0; never overwritten
      source/source_manifest.json      source sha256, size, import time, region summary
      config/rules.<config_revision>.json
      revisions/<id>/manifest.json
      revisions/<id>/patch.json        net patch relative to the parent revision
      revisions/<id>/objects.json      generation-source / interface registry
      revisions/<id>/scene.litematic   the complete scene of that revision
      tasks/<task_id>/...
      HEAD.json
      exports/<export_id>/
      logs/

Hard guarantees (spec 12.4 / 12.5):

* **HEAD never points at a revision that is not fully on disk.** A commit writes
  every artefact into ``revisions/.tmp-<id>-<pid>``, re-reads and verifies it,
  promotes the directory with :func:`os.replace`, and only then replaces
  ``HEAD.json`` (itself a temporary file plus ``os.replace``). A crash therefore
  leaves either the previous HEAD or the complete new revision, never a
  half-written one.
* **Compare-and-swap.** :meth:`ProjectStore.commit_revision`,
  :meth:`ProjectStore.undo` and :meth:`ProjectStore.redo` compare
  ``expected_head`` and ``expected_generation`` before writing anything and raise
  :class:`StaleHeadError` (carrying expected/actual) on a mismatch; a stale
  request never touches the disk.
* **Immutability.** A promoted revision directory is never overwritten; a second
  commit with an existing revision id is refused.
* **Crash recovery.** Reading :attr:`ProjectStore.head` validates the referenced
  revision and raises :class:`IncompleteRevisionError` instead of guessing. A
  complete but unreferenced revision stays on disk and never becomes HEAD on its
  own.
* All timestamps are ``datetime.now(timezone.utc).isoformat()``; every path is a
  :class:`pathlib.Path`.

The store never holds the project lock for the caller: wrap a read-modify-write
sequence in :class:`ProjectLock` so two processes cannot interleave.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .io import LoadedScene, load_scene

SCHEMA_VERSION = "0.2"
HEAD_NAME = "HEAD.json"
PROJECT_NAME = "project.json"
DEFAULT_CONFIG_REVISION = "cfg001"

_REVISION_DIR_RE = re.compile(r"^r(\d+)$")
_CONFIG_FILE_RE = re.compile(r"^rules\.(cfg\d+)\.json$")
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_LOCK_NAME = ".project.lock"

#: Artefacts that must all exist before a revision may be referenced by HEAD.
REVISION_ARTIFACTS: tuple[str, ...] = (
    "manifest.json",
    "patch.json",
    "objects.json",
    "scene.litematic",
)

#: Keys a revision manifest must carry (spec 12.4).
MANIFEST_KEYS: tuple[str, ...] = (
    "revision_id",
    "parent_revision",
    "created",
    "scene_sha256",
    "patch_count",
    "objects_count",
    "config_revision",
    "config_hash",
    "task_id",
    "source_sha256",
)


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class ProjectStoreError(RuntimeError):
    """Any project-store failure that is not a CAS or a completeness problem."""

    def to_dict(self) -> dict:
        return {"code": type(self).__name__, "message": str(self)}


class StaleHeadError(ProjectStoreError):
    """``expected_head``/``expected_generation`` do not match the current HEAD.

    Carries both the expected and the actual head/generation so the caller can
    rebase its request; nothing was written when this is raised.
    """

    def __init__(
        self,
        message: str,
        *,
        expected_head: Any = None,
        actual_head: Any = None,
        expected_generation: Any = None,
        actual_generation: Any = None,
    ) -> None:
        super().__init__(message)
        self.expected_head = expected_head
        self.actual_head = actual_head
        self.expected_generation = expected_generation
        self.actual_generation = actual_generation

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(
            {
                "expected_head": self.expected_head,
                "actual_head": self.actual_head,
                "expected_generation": self.expected_generation,
                "actual_generation": self.actual_generation,
            }
        )
        return d


class IncompleteRevisionError(ProjectStoreError):
    """A revision referenced by HEAD (or by the redo chain) is not complete on disk."""

    def __init__(self, message: str, *, revision_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.revision_id = revision_id

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["revision_id"] = self.revision_id
        return d


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def utc_now() -> str:
    """The single timestamp format used everywhere in the store."""
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    """Streaming sha256 of a file (bytes, not semantics)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(payload: Any) -> str:
    """Deterministic JSON encoding used for every content hash."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(payload: Any) -> str:
    """sha256 over :func:`canonical_json`, stable across pretty-printing."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _safe_component(value: Any, what: str) -> str:
    """Refuse anything that could escape or collide inside the project tree."""
    if not isinstance(value, str) or not _COMPONENT_RE.match(value):
        raise ProjectStoreError(
            f"{what} must be a non-empty [A-Za-z0-9._-] string, got {value!r}"
        )
    if value in {".", ".."}:
        raise ProjectStoreError(f"{what} must not be {value!r}")
    return value


def _read_json(path: Path, what: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise ProjectStoreError(f"{what}: cannot read {path}: {exc}") from exc


def _write_json(path: Path, payload: Any) -> None:
    """Write one JSON document (the caller decides whether it is atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, sort_keys=True, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Temporary file next to ``path`` plus :func:`os.replace`."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        _write_json(tmp, payload)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _region_summary(scene: LoadedScene) -> dict:
    """Region identity + data version of a loaded scene (used for manifest/guards)."""
    info = scene.snapshot.transform.region
    sx, sy, sz = info.abs_size
    return {
        "region_id": info.region_id,
        "position": list(info.position),
        "size": list(info.size),
        "abs_size": list(info.abs_size),
        "min_schematic": list(info.min_schem),
        "max_schematic": list(info.max_schem),
        "volume": int(sx) * int(sy) * int(sz),
        "data_version": int(scene.data_version),
    }


def _objects_count(objects: Mapping[str, Any]) -> int:
    """Number of registered objects in an ``objects.json`` payload.

    A registry envelope (``{"schema_version": .., "objects": [...]}``) is counted
    by its entries; a bare ``{object_id: record}`` mapping by its keys.
    """
    inner = objects.get("objects")
    if isinstance(inner, (list, dict)):
        return len(inner)
    return len(objects)


@dataclass(frozen=True)
class HeadState:
    """The store's current pointer; ``generation`` changes on every transition."""

    revision_id: str
    generation: int
    config_revision: str
    config_hash: str

    def to_dict(self) -> dict:
        return {
            "revision_id": self.revision_id,
            "generation": self.generation,
            "config_revision": self.config_revision,
            "config_hash": self.config_hash,
        }


# --------------------------------------------------------------------------
# lock
# --------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` currently exists (Windows and POSIX)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return int(code.value) == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ProjectLock:
    """Single-writer, file-based project lock: ``with ProjectLock(project_dir):``.

    The lock file carries the writer pid and an acquisition timestamp. A second
    concurrent acquisition waits and then raises :class:`ProjectStoreError`
    instead of silently writing in parallel. A stale lock whose process no longer
    exists is reclaimed. The lock never creates the project directory.
    """

    def __init__(
        self,
        project_dir: Path,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.05,
        stale_age: float = 900.0,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.timeout = float(timeout)
        self.poll_interval = max(0.0, float(poll_interval))
        self.stale_age = float(stale_age)
        self._held = False

    @property
    def path(self) -> Path:
        return self.project_dir / _LOCK_NAME

    def _read_owner(self) -> Optional[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _reclaim_if_stale(self) -> bool:
        """Drop a lock whose owner is gone; never touch a lock we cannot judge."""
        owner = self._read_owner()
        if owner is None:
            # Unreadable/corrupt lock: only reclaim it once it is clearly old.
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            if age < self.stale_age:
                return False
        else:
            if _pid_alive(owner.get("pid")):
                return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return True

    def __enter__(self) -> "ProjectLock":
        if not self.project_dir.is_dir():
            raise ProjectStoreError(
                f"{self.project_dir}: project directory does not exist; the lock never creates it"
            )
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                self._reclaim_if_stale()
                if time.monotonic() >= deadline:
                    owner = self._read_owner() or {}
                    raise ProjectStoreError(
                        f"{self.path}: project is locked by another writer "
                        f"(pid={owner.get('pid')}, acquired_at={owner.get('acquired_at')}); "
                        f"refusing to write in parallel"
                    )
                time.sleep(self.poll_interval)
                continue
            except OSError as exc:  # pragma: no cover - unexpected FS failure
                raise ProjectStoreError(f"{self.path}: cannot acquire lock: {exc}") from exc
            try:
                payload = {
                    "pid": os.getpid(),
                    "acquired_at": utc_now(),
                    "lock": _LOCK_NAME,
                }
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    json.dump(payload, fh, sort_keys=True)
                    fh.write("\n")
            except OSError as exc:  # pragma: no cover - unexpected FS failure
                try:
                    self.path.unlink()
                except OSError:
                    pass
                raise ProjectStoreError(f"{self.path}: cannot record lock owner: {exc}") from exc
            self._held = True
            return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._held:
            return
        owner = self._read_owner() or {}
        if owner.get("pid") == os.getpid():
            try:
                self.path.unlink()
            except OSError:  # pragma: no cover - already gone
                pass
        self._held = False


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------


class ProjectStore:
    """Immutable revisions + HEAD pointer for one project directory."""

    def __init__(self, project_dir: Path) -> None:
        # Deliberately creates nothing: only ``create`` materialises the layout.
        self.dir = Path(project_dir)

    # -- layout ----------------------------------------------------------

    @property
    def project_path(self) -> Path:
        return self.dir / PROJECT_NAME

    @property
    def head_path(self) -> Path:
        return self.dir / HEAD_NAME

    @property
    def source_dir(self) -> Path:
        return self.dir / "source"

    @property
    def source_scene_path(self) -> Path:
        return self.source_dir / "original.litematic"

    @property
    def source_manifest_path(self) -> Path:
        return self.source_dir / "source_manifest.json"

    @property
    def config_dir(self) -> Path:
        return self.dir / "config"

    @property
    def revisions_dir(self) -> Path:
        return self.dir / "revisions"

    @property
    def tasks_dir(self) -> Path:
        return self.dir / "tasks"

    @property
    def exports_dir(self) -> Path:
        return self.dir / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.dir / "logs"

    def revision_dir(self, revision_id: str) -> Path:
        return self.revisions_dir / _safe_component(revision_id, "revision_id")

    # -- creation --------------------------------------------------------

    @staticmethod
    def create(
        project_dir: Path,
        source_litematic: Path,
        *,
        project_id: Optional[str] = None,
        game_version: Optional[str] = None,
        config: Optional[dict] = None,
    ) -> "ProjectStore":
        """Import a scene and establish ``r000``.

        The user's file is only ever *read*: it is copied to
        ``source/original.litematic`` (B0, never overwritten) and the original
        path is left untouched. The input is loaded first, so an unsupported
        multi-region or unknown-version file stops the import before anything is
        written. ``config`` is stored as ``config/rules.cfg001.json``; its hash is
        the sha256 of the canonical JSON encoding, so reformatting the file does
        not change the recorded config identity.
        """
        project_dir = Path(project_dir)
        source = Path(source_litematic)
        if not source.is_file():
            raise ProjectStoreError(f"source litematic does not exist: {source}")
        if project_dir.exists():
            if not project_dir.is_dir():
                raise ProjectStoreError(f"{project_dir} exists and is not a directory")
            if any(project_dir.iterdir()):
                raise ProjectStoreError(
                    f"{project_dir} already exists and is not empty; refusing to overwrite a project"
                )

        # Validate/describe the input *before* creating any directory.
        scene = load_scene(str(source))

        store = ProjectStore(project_dir)
        pid = _safe_component(project_id if project_id is not None else project_dir.name, "project_id")
        created = utc_now()
        source_sha = sha256_file(source)
        source_size = source.stat().st_size

        for directory in (
            store.source_dir,
            store.config_dir,
            store.revisions_dir,
            store.tasks_dir,
            store.exports_dir,
            store.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        shutil.copyfile(source, store.source_scene_path)
        if sha256_file(store.source_scene_path) != source_sha:
            raise ProjectStoreError(
                f"{store.source_scene_path}: copy does not match the source file; import aborted"
            )
        if sha256_file(source) != source_sha:
            raise ProjectStoreError(f"{source}: the source file changed while it was imported")

        config_payload = dict(config) if config is not None else {}
        config_hash = canonical_hash(config_payload)
        _write_json_atomic(
            store.config_dir / f"rules.{DEFAULT_CONFIG_REVISION}.json", config_payload
        )

        _write_json_atomic(
            store.project_path,
            {
                "schema_version": SCHEMA_VERSION,
                "project_id": pid,
                "created": created,
                "game_version": game_version,
                "minecraft_data_version": int(scene.data_version),
                "region": _region_summary(scene),
                "source_sha256": source_sha,
                "source_bytes": int(source_size),
            },
        )
        _write_json_atomic(
            store.source_manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "project_id": pid,
                "source_name": source.name,
                "source_path": str(source),
                "sha256": source_sha,
                "size_bytes": int(source_size),
                "imported_at": created,
                "copied_to": "source/original.litematic",
                "minecraft_data_version": int(scene.data_version),
                "region": _region_summary(scene),
            },
        )

        store._stage_revision(
            revision_id="r000",
            scene_source=store.source_scene_path,
            patch=[],
            objects={"schema_version": SCHEMA_VERSION, "count": 0, "objects": []},
            manifest={
                "schema_version": SCHEMA_VERSION,
                "revision_id": "r000",
                "parent_revision": None,
                "created": created,
                "patch_count": 0,
                "objects_count": 0,
                "config_revision": DEFAULT_CONFIG_REVISION,
                "config_hash": config_hash,
                "task_id": None,
                "source_sha256": source_sha,
                "import_summary": _region_summary(scene),
            },
        )
        store._write_head(
            revision_id="r000",
            generation=0,
            config_revision=DEFAULT_CONFIG_REVISION,
            config_hash=config_hash,
            redo=[],
            redo_history=[],
        )
        store.log(
            "projects",
            {"event_detail": "created", "project_id": pid, "source_sha256": source_sha},
        )
        return store

    # -- metadata --------------------------------------------------------

    @property
    def project_id(self) -> str:
        payload = _read_json(self.project_path, "project.json")
        if not isinstance(payload, dict) or not isinstance(payload.get("project_id"), str):
            raise ProjectStoreError(f"{self.project_path}: missing 'project_id'")
        return payload["project_id"]

    def project_json(self) -> dict:
        payload = _read_json(self.project_path, "project.json")
        if not isinstance(payload, dict):
            raise ProjectStoreError(f"{self.project_path}: project settings must be a JSON object")
        return payload

    def source_manifest(self) -> dict:
        payload = _read_json(self.source_manifest_path, "source_manifest.json")
        if not isinstance(payload, dict) or not isinstance(payload.get("sha256"), str):
            raise ProjectStoreError(f"{self.source_manifest_path}: missing 'sha256'")
        return payload

    def source_sha256(self) -> str:
        return self.source_manifest()["sha256"]

    def source_scene_sha256(self) -> str:
        """sha256 of the B0 copy, checked against the recorded source hash."""
        recorded = self.source_sha256()
        actual = sha256_file(self.source_scene_path)
        if actual != recorded:
            raise IncompleteRevisionError(
                f"{self.source_scene_path}: sha256 {actual} does not match the recorded source "
                f"hash {recorded}; B0 must stay byte-identical to what was imported"
            )
        return actual

    # -- config ----------------------------------------------------------

    def config_path(self, config_revision: str) -> Path:
        rev = _safe_component(config_revision, "config_revision")
        return self.config_dir / f"rules.{rev}.json"

    def load_config(self, config_revision: str) -> dict:
        path = self.config_path(config_revision)
        payload = _read_json(path, f"config revision '{config_revision}'")
        if not isinstance(payload, dict):
            raise ProjectStoreError(f"{path}: config must be a JSON object")
        return payload

    def _next_config_revision(self, current: str) -> str:
        highest = 0
        if self.config_dir.is_dir():
            for name in self.config_dir.iterdir():
                m = _CONFIG_FILE_RE.match(name.name)
                if m:
                    highest = max(highest, int(m.group(1)[3:]))
        m = re.fullmatch(r"cfg(\d+)", current or "")
        if m:
            highest = max(highest, int(m.group(1)))
        return f"cfg{highest + 1:03d}"

    def _resolve_config(
        self,
        *,
        config_revision: Optional[str],
        config: Optional[dict],
        head: HeadState,
    ) -> tuple[str, str]:
        """Return the ``(config_revision, config_hash)`` a commit must record."""
        if config is None:
            rev = _safe_component(
                config_revision if config_revision is not None else head.config_revision,
                "config_revision",
            )
            path = self.config_path(rev)
            if not path.is_file():
                raise ProjectStoreError(
                    f"config revision '{rev}' does not exist ({path}); a commit may not "
                    f"reference a config that is not on disk"
                )
            return rev, canonical_hash(self.load_config(rev))

        payload = dict(config)
        if config_revision is None:
            rev = self._next_config_revision(head.config_revision)
        else:
            rev = _safe_component(config_revision, "config_revision")
        path = self.config_path(rev)
        new_hash = canonical_hash(payload)
        if path.is_file():
            existing = canonical_hash(self.load_config(rev))
            if existing != new_hash:
                raise ProjectStoreError(
                    f"config revision '{rev}' already exists with hash {existing}; "
                    f"config revisions are immutable"
                )
        else:
            _write_json_atomic(path, payload)
        return rev, new_hash

    # -- HEAD ------------------------------------------------------------

    def _read_head_raw(self) -> dict:
        if not self.head_path.is_file():
            raise ProjectStoreError(
                f"{self.head_path}: no HEAD.json; use ProjectStore.create to import a project"
            )
        payload = _read_json(self.head_path, "HEAD.json")
        if not isinstance(payload, dict):
            raise ProjectStoreError(f"{self.head_path}: HEAD must be a JSON object")
        revision_id = payload.get("revision_id")
        if not isinstance(revision_id, str) or not revision_id:
            raise ProjectStoreError(f"{self.head_path}: missing 'revision_id'")
        generation = payload.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise ProjectStoreError(f"{self.head_path}: 'generation' must be an integer")
        return payload

    def _head_and_raw(self) -> tuple[HeadState, dict]:
        raw = self._read_head_raw()
        revision_id = str(raw["revision_id"])
        manifest = self.verify_revision_complete(revision_id)
        return (
            HeadState(
                revision_id=revision_id,
                generation=int(raw["generation"]),
                config_revision=str(manifest["config_revision"]),
                config_hash=str(manifest["config_hash"]),
            ),
            raw,
        )

    @property
    def head(self) -> HeadState:
        """Current HEAD, validated against the artefacts actually on disk."""
        return self._head_and_raw()[0]

    @property
    def redo_stack(self) -> list[str]:
        """Revision ids the linear redo chain would walk, next redo target last."""
        raw = self._read_head_raw()
        redo = raw.get("redo") or []
        if not isinstance(redo, list):
            raise ProjectStoreError(f"{self.head_path}: 'redo' must be a list")
        return [str(r) for r in redo]

    def _write_head(
        self,
        *,
        revision_id: str,
        generation: int,
        config_revision: str,
        config_hash: str,
        redo: Iterable[str],
        redo_history: Iterable[str],
    ) -> None:
        _write_json_atomic(
            self.head_path,
            {
                "schema_version": SCHEMA_VERSION,
                "revision_id": revision_id,
                "generation": int(generation),
                "config_revision": config_revision,
                "config_hash": config_hash,
                "redo": [str(r) for r in redo],
                "redo_history": [str(r) for r in redo_history],
                "updated_at": utc_now(),
            },
        )

    def _require_cas(
        self, head: HeadState, expected_head: Any, expected_generation: Any
    ) -> None:
        """Refuse a stale request. Called before any write happens."""
        if expected_head != head.revision_id:
            raise StaleHeadError(
                f"stale request: expected HEAD '{expected_head}' but the project HEAD is "
                f"'{head.revision_id}' (generation {head.generation})",
                expected_head=expected_head,
                actual_head=head.revision_id,
                expected_generation=expected_generation,
                actual_generation=head.generation,
            )
        if (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation != head.generation
        ):
            raise StaleHeadError(
                f"stale request: expected generation {expected_generation!r} but the project "
                f"generation is {head.generation} (HEAD '{head.revision_id}')",
                expected_head=expected_head,
                actual_head=head.revision_id,
                expected_generation=expected_generation,
                actual_generation=head.generation,
            )

    # -- revisions -------------------------------------------------------

    def list_revisions(self) -> list[str]:
        """Every revision directory, ascending. Orphans are listed as well."""
        if not self.revisions_dir.is_dir():
            return []
        found = [
            (int(m.group(1)), child.name)
            for child in self.revisions_dir.iterdir()
            if child.is_dir() and (m := _REVISION_DIR_RE.match(child.name))
        ]
        return [name for _, name in sorted(found)]

    def verify_revision_complete(self, revision_id: str) -> dict:
        """Validate a revision directory and return its manifest.

        Raises :class:`IncompleteRevisionError` when an artefact is missing,
        unreadable, internally inconsistent (sha256 / revision id / config hash)
        or when the revision keeps a different region identity than the import.
        """
        revision_id = _safe_component(revision_id, "revision_id")
        rev_dir = self.revisions_dir / revision_id
        if not rev_dir.is_dir():
            raise IncompleteRevisionError(
                f"revision '{revision_id}' is referenced but {rev_dir} does not exist; "
                f"an orphan or half-written revision is never guessed or promoted",
                revision_id=revision_id,
            )
        missing = [name for name in REVISION_ARTIFACTS if not (rev_dir / name).is_file()]
        if missing:
            raise IncompleteRevisionError(
                f"revision '{revision_id}' is incomplete: missing {missing} in {rev_dir}",
                revision_id=revision_id,
            )

        manifest = self.load_manifest(revision_id)
        absent = [key for key in MANIFEST_KEYS if key not in manifest]
        if absent:
            raise IncompleteRevisionError(
                f"revision '{revision_id}': manifest.json is missing {absent}",
                revision_id=revision_id,
            )
        if manifest["revision_id"] != revision_id:
            raise IncompleteRevisionError(
                f"revision '{revision_id}': manifest.json claims revision_id "
                f"'{manifest['revision_id']}'",
                revision_id=revision_id,
            )

        scene_path = rev_dir / "scene.litematic"
        actual_sha = sha256_file(scene_path)
        if manifest["scene_sha256"] != actual_sha:
            raise IncompleteRevisionError(
                f"revision '{revision_id}': scene.litematic sha256 {actual_sha} does not match "
                f"the recorded {manifest['scene_sha256']}; the scene artefact is not the one "
                f"that was committed",
                revision_id=revision_id,
            )

        patch = self.load_patch(revision_id)
        if manifest["patch_count"] != len(patch):
            raise IncompleteRevisionError(
                f"revision '{revision_id}': patch.json holds {len(patch)} change(s) but the "
                f"manifest records patch_count={manifest['patch_count']}",
                revision_id=revision_id,
            )
        objects = self.load_objects(revision_id)
        if manifest["objects_count"] != _objects_count(objects):
            raise IncompleteRevisionError(
                f"revision '{revision_id}': objects.json holds "
                f"{_objects_count(objects)} object(s) but the manifest records "
                f"objects_count={manifest['objects_count']}",
                revision_id=revision_id,
            )

        cfg_rev = str(manifest["config_revision"])
        cfg_path = self.config_path(cfg_rev)
        if not cfg_path.is_file():
            raise IncompleteRevisionError(
                f"revision '{revision_id}': config revision '{cfg_rev}' is missing ({cfg_path})",
                revision_id=revision_id,
            )
        cfg_hash = canonical_hash(self.load_config(cfg_rev))
        if cfg_hash != manifest["config_hash"]:
            raise IncompleteRevisionError(
                f"revision '{revision_id}': config '{cfg_rev}' has hash {cfg_hash} but the "
                f"manifest records {manifest['config_hash']}",
                revision_id=revision_id,
            )

        # A revision may never silently change the imported region identity.
        source_region = (self.source_manifest().get("region") or {})
        recorded = manifest.get("import_summary")
        if isinstance(recorded, dict) and isinstance(source_region, dict):
            for key in ("region_id", "position", "size"):
                if key in source_region and recorded.get(key) != source_region.get(key):
                    raise IncompleteRevisionError(
                        f"revision '{revision_id}': region {key} is "
                        f"{recorded.get(key)!r} but the import has {source_region.get(key)!r}",
                        revision_id=revision_id,
                    )
        return manifest

    def load_manifest(self, revision_id: str) -> dict:
        revision_id = _safe_component(revision_id, "revision_id")
        path = self.revisions_dir / revision_id / "manifest.json"
        try:
            payload = _read_json(path, f"revision '{revision_id}' manifest")
        except ProjectStoreError as exc:
            raise IncompleteRevisionError(str(exc), revision_id=revision_id) from exc
        if not isinstance(payload, dict):
            raise IncompleteRevisionError(
                f"revision '{revision_id}': manifest.json must be a JSON object",
                revision_id=revision_id,
            )
        return payload

    def load_patch(self, revision_id: str) -> list[dict]:
        """The net patch stored with a revision (relative to its parent)."""
        revision_id = _safe_component(revision_id, "revision_id")
        path = self.revisions_dir / revision_id / "patch.json"
        try:
            payload = _read_json(path, f"revision '{revision_id}' patch")
        except ProjectStoreError as exc:
            raise IncompleteRevisionError(str(exc), revision_id=revision_id) from exc
        if not isinstance(payload, list) or not all(isinstance(c, dict) for c in payload):
            raise IncompleteRevisionError(
                f"revision '{revision_id}': patch.json must be a list of change objects",
                revision_id=revision_id,
            )
        return payload

    def load_objects(self, revision_id: str) -> dict:
        """The generation-source / interface registry stored with a revision."""
        revision_id = _safe_component(revision_id, "revision_id")
        path = self.revisions_dir / revision_id / "objects.json"
        try:
            payload = _read_json(path, f"revision '{revision_id}' objects")
        except ProjectStoreError as exc:
            raise IncompleteRevisionError(str(exc), revision_id=revision_id) from exc
        if not isinstance(payload, dict):
            raise IncompleteRevisionError(
                f"revision '{revision_id}': objects.json must be a JSON object",
                revision_id=revision_id,
            )
        return payload

    def parent_of(self, revision_id: str) -> Optional[str]:
        parent = self.load_manifest(revision_id).get("parent_revision")
        return None if parent is None else str(parent)

    def next_revision_id(self) -> str:
        """Next stable, monotonic revision id (``r000``, ``r001``, ...)."""
        highest = -1
        for name in self.list_revisions():
            highest = max(highest, int(_REVISION_DIR_RE.match(name).group(1)))  # type: ignore[union-attr]
        return f"r{highest + 1:03d}"

    def head_scene_path(self) -> Path:
        """``revisions/<head>/scene.litematic`` (validates HEAD first)."""
        return self.revision_dir(self.head.revision_id) / "scene.litematic"

    def load_head_scene(self) -> LoadedScene:
        """Re-read the HEAD scene from disk; never served from a cache."""
        return load_scene(str(self.head_scene_path()))

    # -- staging ---------------------------------------------------------

    def _verify_staged_scene(self, staged: Path, label: str) -> None:
        """The staged scene must be readable and keep the imported region identity."""
        source = self.source_manifest()
        try:
            scene = load_scene(str(staged))
        except Exception as exc:  # noqa: BLE001 - reported as a store failure
            raise ProjectStoreError(f"{label}: staged scene is not a readable litematic: {exc}") from exc
        summary = _region_summary(scene)
        region = source.get("region") or {}
        for key in ("region_id", "position", "size", "data_version"):
            if key in region and summary[key] != region[key]:
                raise ProjectStoreError(
                    f"{label}: staged scene has {key}={summary[key]!r} but the imported scene "
                    f"has {region[key]!r}; a revision may not change the region identity"
                )
        if source.get("minecraft_data_version") is not None and (
            summary["data_version"] != source["minecraft_data_version"]
        ):
            raise ProjectStoreError(
                f"{label}: MinecraftDataVersion changed from "
                f"{source['minecraft_data_version']} to {summary['data_version']}"
            )

    def _stage_revision(
        self,
        *,
        revision_id: str,
        scene_source: Path,
        patch: list,
        objects: dict,
        manifest: dict,
    ) -> Path:
        """Write a full revision into a temp dir, verify it, then promote it.

        Returns the promoted directory. Nothing outside ``revisions/`` is touched,
        so a failure here can never move HEAD. The temp directory is always
        removed on failure, leaving no half-written revision behind.
        """
        revision_id = _safe_component(revision_id, "revision_id")
        target = self.revisions_dir / revision_id
        if target.exists():
            raise ProjectStoreError(
                f"revision '{revision_id}' already exists at {target}; revisions are immutable "
                f"and are never overwritten"
            )
        tmp = self.revisions_dir / f".tmp-{revision_id}-{os.getpid()}"
        try:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            shutil.copyfile(scene_source, tmp / "scene.litematic")
            scene_sha = sha256_file(tmp / "scene.litematic")
            self._verify_staged_scene(tmp / "scene.litematic", f"revision '{revision_id}'")

            staged_manifest = dict(manifest)
            staged_manifest["scene_sha256"] = scene_sha
            if staged_manifest.get("patch_count") != len(patch):
                raise ProjectStoreError(
                    f"revision '{revision_id}': patch_count "
                    f"{staged_manifest.get('patch_count')!r} does not match {len(patch)} change(s)"
                )
            if staged_manifest.get("objects_count") != _objects_count(objects):
                raise ProjectStoreError(
                    f"revision '{revision_id}': objects_count "
                    f"{staged_manifest.get('objects_count')!r} does not match "
                    f"{_objects_count(objects)} object(s)"
                )
            _write_json(tmp / "patch.json", patch)
            _write_json(tmp / "objects.json", objects)
            _write_json(tmp / "manifest.json", staged_manifest)

            # Re-read every artefact; only a round-trip clean revision is promoted.
            for name, expected in (
                ("manifest.json", staged_manifest),
                ("patch.json", patch),
                ("objects.json", objects),
            ):
                read_back = _read_json(tmp / name, f"staged revision '{revision_id}' {name}")
                if canonical_json(read_back) != canonical_json(expected):
                    raise ProjectStoreError(
                        f"revision '{revision_id}': {name} did not round-trip on disk"
                    )
            if sha256_file(tmp / "scene.litematic") != scene_sha:
                raise ProjectStoreError(
                    f"revision '{revision_id}': staged scene changed while it was written"
                )
            os.replace(tmp, target)
        except ProjectStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - every staging failure is a store failure
            raise ProjectStoreError(f"writing revision '{revision_id}' failed: {exc}") from exc
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        return target

    # -- commit ----------------------------------------------------------

    def commit_revision(
        self,
        *,
        scene_path: Path,
        patch: list[dict],
        objects: dict,
        expected_head: str,
        expected_generation: int,
        config_revision: Optional[str] = None,
        config: Optional[dict] = None,
        parent_revision: Optional[str] = None,
        task_id: Optional[str] = None,
        manifest_extra: Optional[dict] = None,
    ) -> HeadState:
        """Atomically accept a new revision on top of the current HEAD.

        Order of operations: CAS check (read-only) -> stage every artefact into
        ``revisions/.tmp-<id>-<pid>`` -> re-read/verify -> ``os.replace`` to
        ``revisions/<id>`` -> replace ``HEAD.json``. HEAD therefore never points
        at a revision that is not fully on disk; a crash anywhere before the last
        step leaves the previous HEAD in place. Callers should hold
        :class:`ProjectLock` for the whole read-modify-write sequence.
        """
        head, head_raw = self._head_and_raw()
        # 1. Compare-and-swap before anything is written.
        self._require_cas(head, expected_head, expected_generation)

        scene_path = Path(scene_path)
        if not scene_path.is_file():
            raise ProjectStoreError(f"scene_path does not exist: {scene_path}")
        if not isinstance(patch, list) or not all(isinstance(c, dict) for c in patch):
            raise ProjectStoreError("patch must be a list of change objects")
        if not isinstance(objects, dict):
            raise ProjectStoreError("objects must be a mapping (the objects.json payload)")
        if task_id is not None:
            _safe_component(task_id, "task_id")
        parent = head.revision_id if parent_revision is None else str(parent_revision)
        if parent != head.revision_id:
            raise ProjectStoreError(
                f"parent_revision '{parent}' is not the current HEAD '{head.revision_id}'; "
                f"a revision can only be committed on top of HEAD"
            )

        revision_id = self.next_revision_id()
        cfg_rev, cfg_hash = self._resolve_config(
            config_revision=config_revision, config=config, head=head
        )

        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "revision_id": revision_id,
            "parent_revision": parent,
            "created": utc_now(),
            "patch_count": len(patch),
            "objects_count": _objects_count(objects),
            "config_revision": cfg_rev,
            "config_hash": cfg_hash,
            "task_id": task_id,
            "source_sha256": self.source_sha256(),
        }
        if manifest_extra is not None:
            if not isinstance(manifest_extra, dict):
                raise ProjectStoreError("manifest_extra must be a mapping")
            clashes = sorted(set(manifest_extra) & set(MANIFEST_KEYS))
            if clashes:
                raise ProjectStoreError(
                    f"manifest_extra may not override the required manifest keys {clashes}"
                )
            manifest.update(manifest_extra)

        # 2. Stage + verify + promote the revision directory.
        self._stage_revision(
            revision_id=revision_id,
            scene_source=scene_path,
            patch=patch,
            objects=objects,
            manifest=manifest,
        )

        # 3. Only now move HEAD, atomically. A new commit makes the old redo chain
        # read-only history (spec 12.5): it never becomes the direct redo target.
        discarded = [str(r) for r in (head_raw.get("redo") or [])]
        history = [str(r) for r in (head_raw.get("redo_history") or [])] + discarded
        new_head = HeadState(
            revision_id=revision_id,
            generation=head.generation + 1,
            config_revision=cfg_rev,
            config_hash=cfg_hash,
        )
        self._write_head(
            revision_id=revision_id,
            generation=new_head.generation,
            config_revision=cfg_rev,
            config_hash=cfg_hash,
            redo=[],
            redo_history=history,
        )
        self.log(
            "commits",
            {
                "revision_id": revision_id,
                "parent_revision": parent,
                "generation": new_head.generation,
                "patch_count": len(patch),
                "objects_count": _objects_count(objects),
                "task_id": task_id,
                "config_revision": cfg_rev,
            },
        )
        return new_head

    # -- undo / redo -----------------------------------------------------

    def undo(self, *, expected_head: str, expected_generation: int) -> HeadState:
        """Move HEAD back to the parent revision and push the old one on redo."""
        head, head_raw = self._head_and_raw()
        self._require_cas(head, expected_head, expected_generation)

        parent = self.parent_of(head.revision_id)
        if parent is None:
            raise ProjectStoreError(
                f"revision '{head.revision_id}' has no parent revision; there is nothing to undo"
            )
        manifest = self.verify_revision_complete(parent)
        redo = [str(r) for r in (head_raw.get("redo") or [])]
        new_head = HeadState(
            revision_id=parent,
            generation=head.generation + 1,
            config_revision=str(manifest["config_revision"]),
            config_hash=str(manifest["config_hash"]),
        )
        self._write_head(
            revision_id=parent,
            generation=new_head.generation,
            config_revision=new_head.config_revision,
            config_hash=new_head.config_hash,
            redo=redo + [head.revision_id],
            redo_history=[str(r) for r in (head_raw.get("redo_history") or [])],
        )
        self.log(
            "revisions",
            {
                "action": "undo",
                "from_revision": head.revision_id,
                "to_revision": parent,
                "generation": new_head.generation,
            },
        )
        return new_head

    def redo(self, *, expected_head: str, expected_generation: int) -> HeadState:
        """Re-apply the most recently undone revision (linear redo chain only)."""
        head, head_raw = self._head_and_raw()
        self._require_cas(head, expected_head, expected_generation)

        redo = [str(r) for r in (head_raw.get("redo") or [])]
        if not redo:
            raise ProjectStoreError("nothing to redo: the redo chain is empty")
        target = redo[-1]
        manifest = self.verify_revision_complete(target)
        if manifest.get("parent_revision") != head.revision_id:
            raise ProjectStoreError(
                f"redo target '{target}' was not created from HEAD '{head.revision_id}'; "
                f"the redo chain is stale and is not replayed blindly"
            )
        new_head = HeadState(
            revision_id=target,
            generation=head.generation + 1,
            config_revision=str(manifest["config_revision"]),
            config_hash=str(manifest["config_hash"]),
        )
        self._write_head(
            revision_id=target,
            generation=new_head.generation,
            config_revision=new_head.config_revision,
            config_hash=new_head.config_hash,
            redo=redo[:-1],
            redo_history=[str(r) for r in (head_raw.get("redo_history") or [])],
        )
        self.log(
            "revisions",
            {
                "action": "redo",
                "from_revision": head.revision_id,
                "to_revision": target,
                "generation": new_head.generation,
            },
        )
        return new_head

    # -- working directories ---------------------------------------------

    def create_task_dir(self, task_id: str) -> Path:
        path = self.tasks_dir / _safe_component(task_id, "task_id")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def create_attempt_dir(self, task_id: str, attempt_id: str) -> Path:
        path = (
            self.create_task_dir(task_id)
            / "attempts"
            / _safe_component(attempt_id, "attempt_id")
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def export_dir(self, export_id: str) -> Path:
        path = self.exports_dir / _safe_component(export_id, "export_id")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log(self, name: str, payload: dict) -> Path:
        """Append one JSON object to ``logs/<name>.jsonl`` and return the path."""
        if not isinstance(name, str) or "/" in name or "\\" in name or ":" in name:
            raise ProjectStoreError(f"log name must be a plain file name, got {name!r}")
        stem = name[:-6] if name.endswith(".jsonl") else name
        stem = _safe_component(stem, "log name")
        path = self.logs_dir / f"{stem}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {"payload": payload}
        record["event"] = stem
        record["logged_at"] = utc_now()
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        return path


__all__ = [
    "DEFAULT_CONFIG_REVISION",
    "HeadState",
    "IncompleteRevisionError",
    "MANIFEST_KEYS",
    "ProjectLock",
    "ProjectStore",
    "ProjectStoreError",
    "REVISION_ARTIFACTS",
    "SCHEMA_VERSION",
    "StaleHeadError",
    "canonical_hash",
    "canonical_json",
    "sha256_file",
    "utc_now",
]
