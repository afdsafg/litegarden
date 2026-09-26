"""P5 Agent adapter: one declared contract, two transports, no authority.

This module turns "let an Agent propose a plan" and "let an Agent review the
evidence" into two calls with a *defined* contract, a decidable outcome and no
way for the model side to gain authority.

Two transports, one contract
----------------------------
``runner``
    a local executable started through :mod:`subprocess`; the request JSON is
    written to its stdin (which is then closed: there is no interactive stdin)
    and the response JSON is read from its stdout.
``provider``
    an HTTP endpoint called through :mod:`urllib.request`; the same JSON
    envelope is POSTed and the response body is the same envelope.

Both transports are bounded and both validate the *same* request/response
contract, so a plan that passes through one of them cannot behave differently
from the other.  With neither configured the adapter is ``mode="none"``: the
task is parked in ``WAITING_AGENT`` and **nothing is fabricated** - no plan is
invented, no attempt is created, and no review is ever reported as executed.

Request payload (what the Agent is told)
----------------------------------------
``schema_version`` / ``request_kind`` (``generate_plan`` or
``review_evidence``), the frozen task (``TaskRequest.to_dict()``), the frozen
``selection`` (half-open ``p_local``), the read-only ``context_halo``, the
``coordinate_convention`` (the Agent only ever touches ``p_local``), the
``brief`` (user instruction, mode, target ids, seed), the ``budget``, the
``readonly_tools`` declarations and the ``reference_inventory`` (which
sites / zones / anchors / assets / palettes are usable *and which of them the
selection forbids*).  A review request additionally carries the evidence
bundle (A/B reports, render receipts, screenshot manifest, candidate hashes).

Response envelope (what the Agent must return)
---------------------------------------------
``generate_plan``: ``schema_version``, ``request_kind``, ``task_id``,
``attempt_id``, ``plan``, ``agent_evidence`` (self-reported - it is recorded as
``self_reported`` and never treated as proof).
``review_evidence``: the same identity fields plus ``candidate_id``,
``scene_hash``, ``review_kind`` (only ``hard_error_only``), ``verdict`` (one of
``no_issue_observed`` / ``suspected_issue`` / ``insufficient_evidence`` /
``render_issue``), ``findings`` (each finding names a code from
``HARD_ERROR_CODES``) and optional ``evidence_requests`` - read-only queries
only.  Everything is strict JSON: unknown fields, unknown codes and aesthetic
"repair reasons" are refused instead of interpreted.

Error codes
-----------
======================  ===================================================
code                    meaning
======================  ===================================================
AGENT_UNAVAILABLE        no runner/provider configured, the runner cannot be
                         started, or the provider is unreachable.  With no
                         configuration the task is parked in WAITING_AGENT.
AGENT_TIMEOUT            the wall-clock budget was exceeded; the runner is
                         killed and its partial output is discarded.
AGENT_EXIT_NONZERO       the runner exited with a non-zero status.
AGENT_PROTOCOL_INVALID   stdout/body is not one strict JSON envelope for the
                         requested kind (malformed, missing, unknown fields,
                         wrong schema_version/task id, unknown verdict, or a
                         finding whose code is not a hard-error code).
AGENT_OUTPUT_TOO_LARGE   stdout/body exceeded ``max_output_bytes``.
AGENT_PLAN_INVALID       the plan object failed the shared plan schema, or it
                         names a reference outside the frozen allow-lists.
AGENT_PLAN_OUT_OF_SELECTION
                         an operation's write box (or the declared write
                         intent) leaves the frozen selection.  The plan is
                         rejected **as a whole**; it is never trimmed.
AGENT_PLAN_UNVERIFIABLE  the adapter cannot prove an operation stays inside the
                         selection from the declared references alone, so it is
                         refused rather than assumed safe.
AGENT_REVIEW_INVALID     a review request that is malformed or outside the
                         read-only tool contract (unknown tool, unknown
                         parameter, wrong type).
AGENT_REVIEW_NOT_READONLY
                         a review request that asks for authority: a mutating
                         action, a filesystem path, an environment value or a
                         secret.  Nothing is executed.
AGENT_TOOL_FAILED        a declared read-only tool raised while answering.
AGENT_BUDGET_EXCEEDED    the per-attempt evidence-request budget was exceeded.
AGENT_STALE_EVIDENCE     the review answers for a different candidate/scene
                         hash than the evidence bundle it was given.
AGENT_CANCELED           the caller's cancel token was set.
======================  ===================================================

Authority
---------
The adapter never widens anything.  It validates plans against the frozen
selection with its own independent check (reference allow-lists + per-operation
write boxes) and refuses the whole plan when any operation is out of scope.
Where a write box cannot be derived from references (path routing, terrain
following base heights) the operation is reported as *unverifiable* instead of
being accepted, and the compiler's frozen ``task_authorized`` mask remains the
authoritative gate - the adapter is an early refusal, never a replacement.

Only read-only query tools are declared and executed; a tool that mutates, a
tool name that reads like a write, a parameter that names a file path or a
secret, and a review envelope that carries a plan are all refused.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .constraints import Box3, load_block_rules, state_id
from .redesign import (
    RedesignError,
    Selection,
    TaskRequest,
    TaskStore,
    assert_transition,
    halo_for,
)

__all__ = [
    "AGENT_CONFIG_ENV",
    "AGENT_ERROR_CODES",
    "AGENT_EXIT_NONZERO",
    "AGENT_OUTPUT_TOO_LARGE",
    "AGENT_PLAN_INVALID",
    "AGENT_PLAN_OUT_OF_SELECTION",
    "AGENT_PLAN_UNVERIFIABLE",
    "AGENT_PROTOCOL_INVALID",
    "AGENT_REVIEW_INVALID",
    "AGENT_REVIEW_NOT_READONLY",
    "AGENT_STALE_EVIDENCE",
    "AGENT_TIMEOUT",
    "AGENT_TOOL_FAILED",
    "AGENT_UNAVAILABLE",
    "COORDINATE_CONVENTION",
    "DEFAULT_ENV_ALLOWLIST",
    "DEFAULT_MAX_EVIDENCE_REQUESTS",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "HARD_ERROR_CODES",
    "PROTOCOL_VERSION",
    "REVIEW_VERDICTS",
    "AgentConfig",
    "AgentConfigError",
    "AgentContext",
    "AgentAdapter",
    "AgentError",
    "PlanOutcome",
    "ProviderSpec",
    "ReadonlyTool",
    "ReadonlyToolRegistry",
    "ReviewOutcome",
    "RunBudget",
    "RunnerSpec",
    "build_plan_payload",
    "build_review_payload",
    "run_agent_plan",
    "run_agent_review",
    "validate_plan",
]

PROTOCOL_VERSION = "0.2"
AGENT_CONFIG_ENV = "LITEGARDEN_AGENT_CONFIG"

DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_EVIDENCE_REQUESTS = 8
DEFAULT_MAX_PLAN_ATTEMPTS = 3
DEFAULT_RECORDED_CHARS = 2000
STDERR_CAPTURE_BYTES = 64 * 1024
_READ_POLL_SECONDS = 0.05
_JOIN_SECONDS = 5.0
_READ_CHUNK = 65536

# ---- error codes (exact strings, see the module docstring) ----------------

AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
AGENT_TIMEOUT = "AGENT_TIMEOUT"
AGENT_EXIT_NONZERO = "AGENT_EXIT_NONZERO"
AGENT_PROTOCOL_INVALID = "AGENT_PROTOCOL_INVALID"
AGENT_OUTPUT_TOO_LARGE = "AGENT_OUTPUT_TOO_LARGE"
AGENT_PLAN_INVALID = "AGENT_PLAN_INVALID"
AGENT_PLAN_OUT_OF_SELECTION = "AGENT_PLAN_OUT_OF_SELECTION"
AGENT_PLAN_UNVERIFIABLE = "AGENT_PLAN_UNVERIFIABLE"
AGENT_REVIEW_INVALID = "AGENT_REVIEW_INVALID"
AGENT_REVIEW_NOT_READONLY = "AGENT_REVIEW_NOT_READONLY"
AGENT_TOOL_FAILED = "AGENT_TOOL_FAILED"
AGENT_BUDGET_EXCEEDED = "AGENT_BUDGET_EXCEEDED"
AGENT_STALE_EVIDENCE = "AGENT_STALE_EVIDENCE"
AGENT_CANCELED = "AGENT_CANCELED"

#: code -> one-line meaning, and the HTTP status the service layer should use
#: when it surfaces the failure (a caller never has to invent either).
AGENT_ERROR_CODES: Dict[str, str] = {
    AGENT_UNAVAILABLE: "no usable Agent runner/provider is configured or reachable",
    AGENT_TIMEOUT: "the bounded run exceeded its wall-clock budget",
    AGENT_EXIT_NONZERO: "the local runner exited with a non-zero status",
    AGENT_PROTOCOL_INVALID: "the response is not a strict envelope for the requested kind",
    AGENT_OUTPUT_TOO_LARGE: "the response exceeded the configured output size cap",
    AGENT_PLAN_INVALID: "the plan failed the shared schema or named a reference outside the allow-lists",
    AGENT_PLAN_OUT_OF_SELECTION: "an operation would write outside the frozen selection (plan rejected whole)",
    AGENT_PLAN_UNVERIFIABLE: "the adapter cannot prove the operation stays inside the selection",
    AGENT_REVIEW_INVALID: "the review request is malformed or outside the read-only tool contract",
    AGENT_REVIEW_NOT_READONLY: "the review request asks for write/path/secret authority; refused unexecuted",
    AGENT_TOOL_FAILED: "a declared read-only tool raised while answering",
    AGENT_BUDGET_EXCEEDED: "the per-attempt evidence-request budget was exceeded",
    AGENT_STALE_EVIDENCE: "the review is bound to a different candidate/scene hash than the evidence",
    AGENT_CANCELED: "the caller canceled the run",
}

HTTP_STATUS: Dict[str, int] = {
    AGENT_UNAVAILABLE: 503,
    AGENT_TIMEOUT: 504,
    AGENT_EXIT_NONZERO: 502,
    AGENT_PROTOCOL_INVALID: 422,
    AGENT_OUTPUT_TOO_LARGE: 422,
    AGENT_PLAN_INVALID: 422,
    AGENT_PLAN_OUT_OF_SELECTION: 409,
    AGENT_PLAN_UNVERIFIABLE: 409,
    AGENT_REVIEW_INVALID: 422,
    AGENT_REVIEW_NOT_READONLY: 403,
    AGENT_TOOL_FAILED: 502,
    AGENT_BUDGET_EXCEEDED: 409,
    AGENT_STALE_EVIDENCE: 409,
    AGENT_CANCELED: 409,
}

#: The first-batch hard-error catalogue (spec 15.2).  A finding whose code is
#: not in here - "not pretty", "colour is off", "not grand enough" - is refused
#: instead of being turned into a repair.
HARD_ERROR_CODES = frozenset({
    "WRITE_OUTSIDE_SELECTION",
    "WRITE_PROTECTED",
    "BEFORE_MISMATCH",
    "NBT_UNAUTHORIZED_CHANGE",
    "BLOCK_ENTITY_HOST_CHANGED",
    "ASSET_COLLISION",
    "ASSET_REQUIRED_EMPTY_BLOCKED",
    "ENTRY_BLOCKED",
    "PATH_HEADROOM_BLOCKED",
    "PATH_STEP_INVALID",
    "PATH_DISCONNECTED",
    "BOUNDARY_ANCHOR_BROKEN",
    "SUPPORT_RULE_VIOLATION",
    "ASSET_STATE_MISMATCH",
    "RENDER_PAYLOAD_MISMATCH",
    "RENDER_RESOURCE_MISSING",
    "REVIEW_STALE_EVIDENCE",
})

#: The only model verdicts the protocol accepts (spec 15.4).
REVIEW_VERDICTS = (
    "no_issue_observed",
    "suspected_issue",
    "insufficient_evidence",
    "render_issue",
)
REVIEW_KIND = "hard_error_only"

#: Environment names that must never be inherited by a local runner, even when
#: a caller names them explicitly in the allowlist.
_SECRET_ENV_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|SESSION|COOKIE|API_?KEY|"
    r"PRIVATE|ACCESS_?KEY|BEARER)",
    re.IGNORECASE,
)

#: Never inherited (only this set is, and only names that actually exist).
DEFAULT_ENV_ALLOWLIST: Tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
)

#: A tool name that reads like a mutation never reaches the Agent declaration.
_WRITE_TOOL_RE = re.compile(
    r"(write|delete|remove|apply|save|export|accept|reject|commit|mutate|"
    r"exec|shell|eval|cmd|rename|chmod|grant|undo|rollback)",
    re.IGNORECASE,
)

#: A parameter key that asks for authority (a file, an environment value, a
#: secret, a shell) is refused.  Read-only queries need none of these.
_FORBIDDEN_PARAM_KEYS = frozenset({
    "path", "paths", "file", "files", "filename", "filepath", "dir",
    "directory", "folder", "out", "output", "write", "writes", "delete",
    "remove", "apply", "save", "mutate", "exec", "execute", "command",
    "shell", "script", "code", "eval", "url", "uri", "http", "endpoint",
    "token", "key", "secret", "password", "credential", "env", "environment",
    "cwd", "redirect", "stdin", "stdout",
})

_PATH_LIKE_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\|/)")


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class AgentConfigError(ValueError):
    """The adapter itself is misconfigured; nothing was sent anywhere."""


class AgentError(RuntimeError):
    """A structured Agent failure; ``code`` is one of the module constants."""

    def __init__(self, code: str, message: str, detail: Optional[Mapping] = None):
        super().__init__(message)
        self.code = code
        self.detail = dict(detail or {})

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "detail": self.detail}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _truncate(text: str, limit: int = DEFAULT_RECORDED_CHARS) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} char(s)]"


def _positive_number(value: Any, what: str, default: float) -> float:
    if value is None:
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentConfigError(f"{what} must be a number, got {value!r}")
    if value <= 0:
        raise AgentConfigError(f"{what} must be > 0, got {value!r}")
    return float(value)


def _positive_int(value: Any, what: str, default: int) -> int:
    if value is None:
        return int(default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentConfigError(f"{what} must be an integer, got {value!r}")
    if value <= 0:
        raise AgentConfigError(f"{what} must be > 0, got {value!r}")
    return int(value)


def _string_list(value: Any, what: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise AgentConfigError(f"{what} must be a list of strings, got {value!r}")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise AgentConfigError(f"{what} must be non-empty strings, got {item!r}")
        out.append(item)
    return tuple(out)


def _box_inside(inner: Box3, outer: Box3) -> bool:
    return all(
        outer.min[i] <= inner.min[i] and inner.max_exclusive[i] <= outer.max_exclusive[i]
        for i in range(3)
    )


def _parse_box3(raw: Any, what: str) -> Box3:
    if not isinstance(raw, Mapping):
        raise AgentError(AGENT_PROTOCOL_INVALID, f"{what} must be an object, got {raw!r}",
                         {"field": what})
    lo = raw.get("min")
    hi = raw.get("max_exclusive")
    for name, value in (("min", lo), ("max_exclusive", hi)):
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 3
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
        ):
            raise AgentError(
                AGENT_PROTOCOL_INVALID,
                f"{what}.{name} must be three integers, got {value!r}",
                {"field": f"{what}.{name}"},
            )
    box = Box3((lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2]))
    if any(box.size[i] <= 0 for i in range(3)):
        raise AgentError(AGENT_PROTOCOL_INVALID, f"{what} is empty or inverted: {raw!r}",
                         {"field": what})
    return box


def _strict_json(text: str) -> Any:
    """Parse strict JSON: NaN/Infinity and trailing data are not accepted."""
    def reject(constant: str) -> Any:
        raise ValueError(f"non-finite JSON constant {constant!r} is not allowed")

    return json.loads(text, parse_constant=reject)


# --------------------------------------------------------------------------
# read-only tools
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadonlyTool:
    """A declared read-only query the Agent may ask for.

    ``params`` maps a parameter name to its type (``"int"``, ``"str"``,
    ``"int3"``, ``"box"``, ``"str_list"``).  ``handler`` is optional: a tool
    that is declared but not wired is refused honestly instead of being
    answered with invented data.
    """

    name: str
    description: str
    params: Mapping[str, str] = field(default_factory=dict)
    handler: Optional[Callable[[dict], Any]] = None

    def describe(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "params": dict(self.params),
            "writes": False,
        }


#: The read-only toolbox from spec 15.5 (plus the scene summary / height map /
#: block statistics this project's adapter is asked to expose).  Handlers are
#: wired by the caller (server or CLI) - the adapter only guarantees that only
#: these names, with these parameters, can ever be executed.
DEFAULT_READONLY_TOOLS: Tuple[ReadonlyTool, ...] = (
    ReadonlyTool("scene_summary", "scene/revision summary: bounds, data version, counts",
                 {}),
    ReadonlyTool("height_map", "surface/ground height per column inside a box",
                 {"bounds": "box"}),
    ReadonlyTool("block_counts", "block-state histogram inside a box",
                 {"bounds": "box"}),
    ReadonlyTool("inspect_voxels", "read the block state of every voxel in a box",
                 {"bounds": "box"}),
    ReadonlyTool("inspect_object", "read a registered object: voxels, contract, source",
                 {"object_id": "str"}),
    ReadonlyTool("inspect_patch", "read the exact net patch inside a box",
                 {"bounds": "box"}),
    ReadonlyTool("check_entry", "check one entry's clearance/traversal profile",
                 {"entry_id": "str"}),
    ReadonlyTool("check_path", "check one path's headroom, steps and connectivity",
                 {"path_id": "str"}),
    ReadonlyTool("get_render_diagnostics", "renderer/asset coverage and failures",
                 {"candidate_id": "str"}),
    ReadonlyTool("request_evidence", "request one additional read-only render/slice",
                 {"preset": "str"}),
)


class ReadonlyToolRegistry:
    """The only way an Agent request can reach real data.  Read-only by design."""

    def __init__(self, declarations: Iterable[ReadonlyTool] = DEFAULT_READONLY_TOOLS):
        self._tools: Dict[str, ReadonlyTool] = {}
        for tool in declarations:
            self.register(tool)

    def register(self, tool: ReadonlyTool) -> None:
        if not isinstance(tool, ReadonlyTool) or not tool.name:
            raise AgentConfigError(f"a tool declaration needs a name, got {tool!r}")
        if _WRITE_TOOL_RE.search(tool.name):
            raise AgentConfigError(
                f"tool name {tool.name!r} reads like a mutation; the Agent side may "
                "only ever be given read-only tools"
            )
        bad = sorted(set(tool.params) & _FORBIDDEN_PARAM_KEYS)
        if bad:
            raise AgentConfigError(
                f"tool {tool.name!r} declares forbidden parameter(s) {bad}: a read-only "
                "query never needs a file path, an environment value or a secret"
            )
        for param, kind in tool.params.items():
            if kind not in ("int", "str", "int3", "box", "str_list"):
                raise AgentConfigError(
                    f"tool {tool.name!r} parameter {param!r} has unknown type {kind!r}"
                )
        self._tools[tool.name] = tool

    # -- declaration -----------------------------------------------------

    def names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._tools))

    def describe(self) -> List[dict]:
        return [self._tools[name].describe() for name in self.names()]

    def get(self, name: str) -> Optional[ReadonlyTool]:
        return self._tools.get(name)

    # -- execution -------------------------------------------------------

    def validate(self, name: Any, params: Any) -> Tuple[str, dict]:
        """Resolve and type-check one request *without* running anything.

        A review round validates every request it received before executing any
        of them, so a batch containing one out-of-contract query is refused as a
        whole instead of partially running.
        """
        tool = self._resolve(name)
        return tool.name, self._validate_params(tool, params)

    def call(self, name: str, params: Mapping) -> Any:
        tool_name, kwargs = self.validate(name, params)
        return self.execute_validated(tool_name, kwargs)

    def execute_validated(self, name: str, kwargs: Mapping) -> Any:
        """Run a request that already passed :meth:`validate`."""
        tool = self._tools[name]
        if tool.handler is None:
            raise AgentError(
                AGENT_TOOL_FAILED,
                f"tool {name!r} is declared but not wired up on this server",
                {"tool": name},
            )
        try:
            return tool.handler(dict(kwargs))
        except AgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            raise AgentError(
                AGENT_TOOL_FAILED,
                f"tool {name!r} failed: {type(exc).__name__}: {exc}",
                {"tool": name},
            ) from None

    # -- validation ------------------------------------------------------

    def _resolve(self, name: Any) -> ReadonlyTool:
        if not isinstance(name, str) or not name.strip():
            raise AgentError(AGENT_REVIEW_INVALID, f"a request needs a tool name, got {name!r}",
                             {"tool": name})
        if name in self._tools:
            return self._tools[name]
        if _WRITE_TOOL_RE.search(name):
            raise AgentError(
                AGENT_REVIEW_NOT_READONLY,
                f"requested tool {name!r} is not a read-only query",
                {"tool": name, "declared": list(self.names())},
            )
        raise AgentError(
            AGENT_REVIEW_INVALID,
            f"unknown tool {name!r}: the Agent may only use the declared tools",
            {"tool": name, "declared": list(self.names())},
        )

    @staticmethod
    def _validate_params(tool: ReadonlyTool, params: Any) -> dict:
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            raise AgentError(AGENT_REVIEW_INVALID,
                             f"tool {tool.name!r}: params must be an object, got {params!r}",
                             {"tool": tool.name})
        forbidden = sorted(set(params) & _FORBIDDEN_PARAM_KEYS)
        if forbidden:
            raise AgentError(
                AGENT_REVIEW_NOT_READONLY,
                f"tool {tool.name!r}: parameter(s) {forbidden} ask for authority "
                "(a file, a secret or an environment value) that a read-only query "
                "must not have",
                {"tool": tool.name, "forbidden_params": forbidden},
            )
        unknown = sorted(set(params) - set(tool.params))
        if unknown:
            raise AgentError(
                AGENT_REVIEW_INVALID,
                f"tool {tool.name!r}: unknown parameter(s) {unknown}",
                {"tool": tool.name, "declared_params": sorted(tool.params)},
            )
        missing = sorted(set(tool.params) - set(params))
        if missing:
            raise AgentError(
                AGENT_REVIEW_INVALID,
                f"tool {tool.name!r}: missing parameter(s) {missing}",
                {"tool": tool.name, "declared_params": sorted(tool.params)},
            )
        out: dict = {}
        for param, kind in tool.params.items():
            out[param] = ReadonlyToolRegistry._check_value(
                tool.name, param, kind, params[param]
            )
        return out

    @staticmethod
    def _check_value(tool: str, param: str, kind: str, value: Any) -> Any:
        def bad(expected: str) -> AgentError:
            return AgentError(
                AGENT_REVIEW_INVALID,
                f"tool {tool!r}: {param} must be {expected}, got {value!r}",
                {"tool": tool, "param": param},
            )

        if kind == "str":
            if not isinstance(value, str) or not value.strip():
                raise bad("a non-empty string")
            if _PATH_LIKE_RE.match(value.strip()) or value.strip().startswith(".."):
                raise AgentError(
                    AGENT_REVIEW_NOT_READONLY,
                    f"tool {tool!r}: {param} looks like a filesystem path ({value!r}); "
                    "the Agent never names a server path",
                    {"tool": tool, "param": param},
                )
            return value
        if kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise bad("an integer")
            return value
        if kind == "int3":
            if (
                not isinstance(value, (list, tuple))
                or len(value) != 3
                or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
            ):
                raise bad("three integers")
            return [int(v) for v in value]
        if kind == "str_list":
            if (
                not isinstance(value, (list, tuple))
                or not all(isinstance(v, str) for v in value)
            ):
                raise bad("a list of strings")
            return [str(v) for v in value]
        if kind == "box":
            try:
                box = _parse_box3(value, param)
            except AgentError as exc:
                raise AgentError(
                    AGENT_REVIEW_INVALID,
                    f"tool {tool!r}: {exc}",
                    {"tool": tool, "param": param},
                ) from None
            return {"min": list(box.min), "max_exclusive": list(box.max_exclusive)}
        raise bad("of a declared type")


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerSpec:
    """A local, non-interactive Agent runner: JSON on stdin, JSON on stdout."""

    argv: Tuple[str, ...]
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    working_dir: Optional[Path] = None
    env_allowlist: Tuple[str, ...] = DEFAULT_ENV_ALLOWLIST

    @staticmethod
    def from_dict(raw: Mapping, *, working_dir: Optional[Path] = None) -> "RunnerSpec":
        if not isinstance(raw, Mapping):
            raise AgentConfigError(f"runner config must be an object, got {raw!r}")
        unknown = sorted(set(raw) - {
            "argv", "command", "timeout_seconds", "max_output_bytes",
            "working_dir", "work_dir", "env_allowlist",
        })
        if unknown:
            raise AgentConfigError(f"runner config has unknown field(s) {unknown}")
        argv = _string_list(raw.get("argv", raw.get("command")), "runner.argv")
        if not argv:
            raise AgentConfigError("runner.argv must be a non-empty command line")
        wd = raw.get("working_dir", raw.get("work_dir"))
        if wd is not None:
            if not isinstance(wd, str) or not wd.strip():
                raise AgentConfigError(f"runner.working_dir must be a path string, got {wd!r}")
            working_dir = Path(wd)
        env_allowlist = _string_list(raw.get("env_allowlist"), "runner.env_allowlist")
        if not env_allowlist:
            env_allowlist = DEFAULT_ENV_ALLOWLIST
        _reject_secret_env_names(env_allowlist)
        return RunnerSpec(
            argv=argv,
            timeout_seconds=_positive_number(
                raw.get("timeout_seconds"), "runner.timeout_seconds", DEFAULT_TIMEOUT_SECONDS
            ),
            max_output_bytes=_positive_int(
                raw.get("max_output_bytes"), "runner.max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES
            ),
            working_dir=working_dir,
            env_allowlist=env_allowlist,
        )

    def to_dict(self) -> dict:
        return {
            "argv": list(self.argv),
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "working_dir": str(self.working_dir) if self.working_dir else None,
            "env_allowlist": list(self.env_allowlist),
        }


@dataclass(frozen=True)
class ProviderSpec:
    """An HTTP Agent endpoint; the same envelope goes over the wire."""

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    @staticmethod
    def from_dict(raw: Mapping) -> "ProviderSpec":
        if not isinstance(raw, Mapping):
            raise AgentConfigError(f"provider config must be an object, got {raw!r}")
        unknown = sorted(set(raw) - {
            "url", "endpoint", "headers", "timeout_seconds", "max_output_bytes",
        })
        if unknown:
            raise AgentConfigError(f"provider config has unknown field(s) {unknown}")
        url = raw.get("url", raw.get("endpoint"))
        if not isinstance(url, str) or not url.strip():
            raise AgentConfigError(f"provider.url must be a URL string, got {url!r}")
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if scheme not in ("http", "https"):
            raise AgentConfigError(
                f"provider.url must be http(s)://..., got {url!r}: other schemes would "
                "hand the Agent side another protocol"
            )
        headers_raw = raw.get("headers") or {}
        if not isinstance(headers_raw, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers_raw.items()
        ):
            raise AgentConfigError("provider.headers must map strings to strings")
        return ProviderSpec(
            url=url,
            headers=dict(headers_raw),
            timeout_seconds=_positive_number(
                raw.get("timeout_seconds"), "provider.timeout_seconds", DEFAULT_TIMEOUT_SECONDS
            ),
            max_output_bytes=_positive_int(
                raw.get("max_output_bytes"), "provider.max_output_bytes",
                DEFAULT_MAX_OUTPUT_BYTES,
            ),
        )

    def to_dict(self) -> dict:
        # header *values* are never recorded (they may carry a key)
        return {
            "url": self.url,
            "header_names": sorted(self.headers),
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
        }


@dataclass(frozen=True)
class RunBudget:
    """The declared, bounded budget.  It is reported to the Agent and enforced."""

    timeout_seconds: float
    max_output_bytes: int
    max_evidence_requests: int
    max_plan_attempts: int = DEFAULT_MAX_PLAN_ATTEMPTS

    def to_dict(self) -> dict:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_evidence_requests": self.max_evidence_requests,
            "max_plan_attempts": self.max_plan_attempts,
            "note": (
                "these are hard limits enforced by the adapter, not performance "
                "promises; the Agent cannot raise them"
            ),
        }


@dataclass(frozen=True)
class AgentConfig:
    """Which Agent, if any, this server may call.

    ``mode`` is explicit - ``"runner"``, ``"provider"`` or ``"none"``.  There is
    no implicit fallback: a half-configured adapter refuses instead of quietly
    behaving like a mock.
    """

    mode: str = "none"
    runner: Optional[RunnerSpec] = None
    provider: Optional[ProviderSpec] = None
    max_evidence_requests: int = DEFAULT_MAX_EVIDENCE_REQUESTS
    max_plan_attempts: int = DEFAULT_MAX_PLAN_ATTEMPTS
    source: str = "default"

    @staticmethod
    def disabled(source: str = "default") -> "AgentConfig":
        return AgentConfig(mode="none", source=source)

    @staticmethod
    def from_dict(raw: Mapping, *, source: str = "inline") -> "AgentConfig":
        if not isinstance(raw, Mapping):
            raise AgentConfigError(f"agent config must be an object, got {raw!r}")
        unknown = sorted(set(raw) - {
            "mode", "runner", "provider", "max_evidence_requests", "max_plan_attempts",
        })
        if unknown:
            raise AgentConfigError(f"agent config has unknown field(s) {unknown}")
        mode = raw.get("mode", "none")
        if mode not in ("none", "runner", "provider"):
            raise AgentConfigError(f"agent mode must be none|runner|provider, got {mode!r}")
        runner = raw.get("runner")
        provider = raw.get("provider")
        if mode == "runner":
            if not isinstance(runner, Mapping):
                raise AgentConfigError("mode 'runner' needs a runner object")
            if provider is not None:
                raise AgentConfigError(
                    "mode 'runner' must not also configure a provider; this round "
                    "integrates exactly one Agent service"
                )
        elif mode == "provider":
            if not isinstance(provider, Mapping):
                raise AgentConfigError("mode 'provider' needs a provider object")
            if runner is not None:
                raise AgentConfigError("mode 'provider' must not also configure a runner")
        else:
            if runner is not None or provider is not None:
                raise AgentConfigError(
                    "mode 'none' means no Agent is configured; remove runner/provider "
                    "or declare the mode explicitly"
                )
        return AgentConfig(
            mode=mode,
            runner=RunnerSpec.from_dict(runner) if isinstance(runner, Mapping) else None,
            provider=ProviderSpec.from_dict(provider) if isinstance(provider, Mapping) else None,
            max_evidence_requests=_positive_int(
                raw.get("max_evidence_requests"), "max_evidence_requests",
                DEFAULT_MAX_EVIDENCE_REQUESTS,
            ),
            max_plan_attempts=_positive_int(
                raw.get("max_plan_attempts"), "max_plan_attempts", DEFAULT_MAX_PLAN_ATTEMPTS
            ),
            source=source,
        )

    @staticmethod
    def load(path: Any = None, *, env: Optional[Mapping[str, str]] = None) -> "AgentConfig":
        """Load ``agent.json``; a named-but-missing file is an error.

        Without a path the ``LITEGARDEN_AGENT_CONFIG`` environment variable is
        honoured; with neither, the adapter is explicitly disabled (``mode`` =
        ``"none"``, which parks tasks in ``WAITING_AGENT``).
        """
        env = os.environ if env is None else env
        chosen = path or env.get(AGENT_CONFIG_ENV)
        if not chosen:
            return AgentConfig.disabled(source="unconfigured")
        p = Path(str(chosen))
        if not p.exists():
            raise AgentConfigError(
                f"agent config {p} does not exist: a named-but-missing config must not "
                "silently disable the Agent (or worse, look like an empty mock)"
            )
        raw = json.loads(p.read_text(encoding="utf-8"))
        return AgentConfig.from_dict(raw, source=str(p))

    @property
    def available(self) -> bool:
        return self.mode in ("runner", "provider")

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "available": self.available,
            "runner": self.runner.to_dict() if self.runner else None,
            "provider": self.provider.to_dict() if self.provider else None,
            "max_evidence_requests": self.max_evidence_requests,
            "max_plan_attempts": self.max_plan_attempts,
            "source": self.source,
        }


# --------------------------------------------------------------------------
# context (the frozen inputs the Agent may see)
# --------------------------------------------------------------------------


@dataclass
class AgentContext:
    """Everything a request is built from.  All of it is server-owned.

    ``sites`` / ``zones`` / ``anchors`` / ``assets`` / ``palettes`` /
    ``allowed_blocks`` are the allow-lists the Agent may reference; the adapter
    uses the same data to reject a plan that names anything else.
    """

    request: TaskRequest
    attempt_id: str
    sites: Dict[str, dict] = field(default_factory=dict)
    zones: Dict[str, dict] = field(default_factory=dict)
    anchors: Dict[str, Sequence[int]] = field(default_factory=dict)
    assets_dir: Optional[Path] = None
    selection_report: Optional[dict] = None
    scene_summary: Optional[dict] = None
    write_policy: Optional[dict] = None
    assets: Dict[str, dict] = field(default_factory=dict)
    palettes: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.request, TaskRequest):
            raise AgentConfigError("AgentContext needs a frozen TaskRequest")
        if not self.attempt_id or not isinstance(self.attempt_id, str):
            raise AgentConfigError("AgentContext needs an attempt id")
        if self.assets_dir is not None:
            self.assets_dir = Path(self.assets_dir)
            self._load_whitelists()

    # -- construction ----------------------------------------------------

    @staticmethod
    def from_task(
        request: TaskRequest,
        *,
        attempt_id: str,
        analysis: Any = None,
        assets_dir: Any = None,
        selection_report: Optional[Mapping] = None,
        scene_summary: Optional[Mapping] = None,
        write_policy: Optional[Mapping] = None,
    ) -> "AgentContext":
        """Build the context from the frozen task plus read-only project data.

        ``analysis`` is duck-typed: anything exposing ``site_candidates``,
        ``zones`` and ``anchors`` (``litegarden.terrain.TerrainAnalysis``)
        works, so nothing here depends on the terrain module.
        """
        sites: Dict[str, dict] = {}
        for site in getattr(analysis, "site_candidates", None) or []:
            if isinstance(site, Mapping) and site.get("id"):
                sites[str(site["id"])] = dict(site)
        zones: Dict[str, dict] = {}
        for zone_id, zone in (getattr(analysis, "zones", None) or {}).items():
            zones[str(zone_id)] = dict(zone) if isinstance(zone, Mapping) else {"id": zone_id}
        anchors = {
            str(name): tuple(int(v) for v in pos)
            for name, pos in (getattr(analysis, "anchors", None) or {}).items()
        }
        return AgentContext(
            request=request,
            attempt_id=str(attempt_id),
            sites=sites,
            zones=zones,
            anchors=anchors,
            assets_dir=Path(assets_dir) if assets_dir else None,
            selection_report=dict(selection_report) if selection_report else None,
            scene_summary=dict(scene_summary) if scene_summary else None,
            write_policy=dict(write_policy) if write_policy else None,
        )

    def _load_whitelists(self) -> None:
        """Read the same whitelist files the Agent pack copies into its input."""
        assert self.assets_dir is not None
        catalog = self.assets_dir / "catalog.json"
        if catalog.exists() and not self.assets:
            raw = json.loads(catalog.read_text(encoding="utf-8"))
            self.assets = {
                str(k): dict(v) for k, v in (raw.get("assets") or {}).items()
            }
        palettes = self.assets_dir / "palettes.json"
        if palettes.exists() and not self.palettes:
            raw = json.loads(palettes.read_text(encoding="utf-8"))
            self.palettes = dict(raw.get("palettes") or {})
        rules = load_block_rules(self.assets_dir / "block_rules.json")
        declared = rules.get("allowed_new_blocks")
        self.allowed_blocks = (
            frozenset(str(b) for b in declared) if isinstance(declared, list) else None
        )

    #: ``None`` means "the project declared no block whitelist" - reported as an
    #: inactive gate, exactly like the compiler's legacy permissive mode.
    allowed_blocks: Optional[frozenset] = None

    # -- derived ---------------------------------------------------------

    @property
    def selection(self) -> Selection:
        return self.request.selection

    @property
    def halo(self):
        return halo_for(self.selection, self.request.context_halo_xz)

    def reference_box(self, x0: int, z0: int, x1: int, z1: int) -> Box3:
        """A reference's x/z extent with the selection's y range.

        The y axis is deliberately the frozen selection's y range: an
        asset base height follows the terrain, which only the compiler knows.
        Treating y as equal to the selection keeps the x/z containment check
        exact and never lets an out-of-selection column slip through.
        """
        sel = self.selection
        return Box3(
            (min(x0, x1), sel.min[1], min(z0, z1)),
            (max(x0, x1) + 1, sel.max_exclusive[1], max(z0, z1) + 1),
        )

    def site_box(self, site_id: str) -> Optional[Box3]:
        site = self.sites.get(site_id)
        if site is None:
            return None
        origin = site.get("origin")
        footprint = site.get("footprint")
        if (
            not isinstance(origin, (list, tuple))
            or len(origin) != 2
            or not isinstance(footprint, (list, tuple))
            or len(footprint) != 2
        ):
            return None
        ox, oz = int(origin[0]), int(origin[1])
        fx, fz = int(footprint[0]), int(footprint[1])
        return self.reference_box(ox, oz, ox + fx - 1, oz + fz - 1)

    def asset_box(self, site_id: str, asset_id: str) -> Optional[Box3]:
        """Footprint of the asset as the compiler will stamp it at this site."""
        site = self.sites.get(site_id)
        asset = self.assets.get(asset_id)
        if site is None:
            return None
        origin = site.get("origin")
        if not isinstance(origin, (list, tuple)) or len(origin) != 2:
            return None
        footprint = (asset or {}).get("footprint") or site.get("footprint")
        if (
            not isinstance(footprint, (list, tuple))
            or len(footprint) != 2
        ):
            return None
        ox, oz = int(origin[0]), int(origin[1])
        fx, fz = int(footprint[0]), int(footprint[1])
        return self.reference_box(ox, oz, ox + fx - 1, oz + fz - 1)

    def zone_box(self, zone_id: str) -> Optional[Box3]:
        zone = self.zones.get(zone_id)
        if not isinstance(zone, Mapping):
            return None
        bbox = zone.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            x0, z0, x1, z1 = (int(v) for v in bbox)
            return self.reference_box(x0, z0, x1, z1)
        box = zone.get("box")
        if isinstance(box, Mapping) and "min" in box and "max_exclusive" in box:
            try:
                parsed = _parse_box3(box, "zone")
            except AgentError:
                return None
            return self.reference_box(
                parsed.min[0], parsed.min[2], parsed.max_exclusive[0] - 1,
                parsed.max_exclusive[2] - 1,
            )
        if all(k in zone for k in ("min_x", "min_z", "max_x", "max_z")):
            return self.reference_box(
                int(zone["min_x"]), int(zone["min_z"]), int(zone["max_x"]), int(zone["max_z"])
            )
        return None

    def anchor_point(self, name: str) -> Optional[Tuple[int, int]]:
        pos = self.anchors.get(name)
        if pos is None:
            return None
        if len(pos) < 2:
            return None
        return (int(pos[0]), int(pos[1]))

    def reference_inventory(self) -> dict:
        """Which references are usable, which the selection forbids, which are opaque.

        ``usable`` fits inside the frozen selection; ``refused`` exists in the
        project but lies (partly) outside it, so naming it can only be an attempt
        to write outside the authorisation; ``unverifiable`` is a reference whose
        extent cannot be derived here (no footprint/bbox/position), which is
        refused rather than assumed safe.
        """
        sel = self.selection
        usable = {"sites": [], "zones": [], "anchors": []}
        refused: List[dict] = []
        unverifiable: List[dict] = []
        for site_id in sorted(self.sites):
            box = self.site_box(site_id)
            if box is None:
                unverifiable.append({"kind": "site", "id": site_id, "reason": "no_footprint"})
            elif _box_inside(box, sel.box):
                usable["sites"].append(site_id)
            else:
                refused.append({
                    "kind": "site", "id": site_id, "reason": "outside_selection",
                    "box": box.to_dict(),
                })
        for zone_id in sorted(self.zones):
            box = self.zone_box(zone_id)
            if box is None:
                unverifiable.append({"kind": "zone", "id": zone_id, "reason": "no_bbox"})
            elif _box_inside(box, sel.box):
                usable["zones"].append(zone_id)
            else:
                refused.append({
                    "kind": "zone", "id": zone_id, "reason": "outside_selection",
                    "box": box.to_dict(),
                })
        for name in sorted(self.anchors):
            point = self.anchor_point(name)
            if point is None:
                unverifiable.append({"kind": "anchor", "id": name, "reason": "no_position"})
            elif sel.contains((point[0], sel.min[1], point[1])):
                usable["anchors"].append(name)
            else:
                refused.append({
                    "kind": "anchor", "id": name, "reason": "outside_selection",
                    "x": point[0], "z": point[1],
                })
        return {
            "space": "project_local",
            "selection": sel.to_dict(),
            "usable": usable,
            "refused": refused,
            "unverifiable": unverifiable,
            "assets": sorted(self.assets),
            "palettes": sorted(self.palettes),
            "allowed_blocks": (
                sorted(self.allowed_blocks) if self.allowed_blocks is not None else None
            ),
            "block_whitelist": (
                "active" if self.allowed_blocks is not None else "inactive"
            ),
            "note": (
                "a plan that names a refused reference is rejected as a whole; the "
                "selection is frozen by the user and never widened by the Agent"
            ),
        }


COORDINATE_CONVENTION: dict = {
    "space": "project_local",
    "agent_uses": "p_local only",
    "bounds": "half-open [min, max_exclusive)",
    "y_is_up": True,
    "writes_allowed_inside": "selection only",
    "read_only": ["context_halo", "everything outside the selection"],
    "forbidden": [
        "region/schematic coordinates",
        "widening the selection",
        "assuming the halo may be written",
    ],
    "note": (
        "the server converts to region coordinates at export time; the Agent never "
        "sees or produces p_region / p_schematic"
    ),
}


# --------------------------------------------------------------------------
# request payloads
# --------------------------------------------------------------------------

PLAN_RESPONSE_REQUIRED = (
    "schema_version", "request_kind", "task_id", "attempt_id", "plan", "agent_evidence",
)
PLAN_RESPONSE_OPTIONAL = ("notes",)
REVIEW_RESPONSE_REQUIRED = (
    "schema_version", "request_kind", "task_id", "attempt_id", "candidate_id",
    "scene_hash", "review_kind", "verdict", "findings",
)
REVIEW_RESPONSE_OPTIONAL = ("evidence_requests", "notes")
AGENT_EVIDENCE_REQUIRED = ("summary",)
AGENT_EVIDENCE_OPTIONAL = ("assumptions", "intended_write_bounds", "confidence", "notes")
FINDING_REQUIRED = ("id", "code", "observation")
FINDING_OPTIONAL = (
    "op_id", "object_id", "bounds_local", "evidence_image_ids", "requested_check",
    "repair_hint",
)

PLAN_FIELDS = frozenset({"schema_version", "scene_id", "seed", "style_id", "operations"})
PLAN_OPERATIONS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "place_asset": {"required": ("asset_id", "site_id"), "optional": ("variant",)},
    "connect_path": {"required": ("from", "to", "width", "palette_id"), "optional": ()},
    "decorate_path": {"required": ("path_id", "asset_id", "spacing"), "optional": ()},
    "scatter_assets": {"required": ("zone_id", "asset_id", "count"), "optional": ()},
}


def _response_contract(adapter: "AgentAdapter") -> dict:
    return {
        "json": (
            "exactly one JSON object on stdout / in the body: no markdown fences, "
            "no numbers like NaN, no extra fields"
        ),
        "generate_plan": {
            "required": list(PLAN_RESPONSE_REQUIRED),
            "optional": list(PLAN_RESPONSE_OPTIONAL),
            "agent_evidence": {
                "required": list(AGENT_EVIDENCE_REQUIRED),
                "optional": list(AGENT_EVIDENCE_OPTIONAL),
                "trust": "self_reported - recorded for the human reviewer, never proof",
            },
        },
        "review_evidence": {
            "required": list(REVIEW_RESPONSE_REQUIRED),
            "optional": list(REVIEW_RESPONSE_OPTIONAL),
            "review_kind": [REVIEW_KIND],
            "verdicts": list(REVIEW_VERDICTS),
            "findings": {
                "required": list(FINDING_REQUIRED),
                "optional": list(FINDING_OPTIONAL),
                "codes": sorted(HARD_ERROR_CODES),
                "note": (
                    "only hard errors with a catalogue code may be reported; aesthetic "
                    "reasons are not part of this protocol"
                ),
            },
            "must_not_carry_a_plan": True,
        },
        "plan_schema": {
            "fields": sorted(PLAN_FIELDS),
            "operations": {
                name: {"required": ["id", "op", *spec["required"]],
                       "optional": list(spec["optional"])}
                for name, spec in PLAN_OPERATIONS.items()
            },
            "note": (
                "the plan is validated against the shared plan schema and against the "
                "frozen selection; a single out-of-selection operation rejects the whole "
                "plan (no trimming)"
            ),
        },
        "limits": adapter.budget.to_dict(),
    }


def build_plan_payload(adapter: "AgentAdapter", context: AgentContext) -> dict:
    """The exact payload sent to the Agent for a plan (also the file-mode pack)."""
    request = context.request
    return {
        "schema_version": PROTOCOL_VERSION,
        "request_kind": "generate_plan",
        "attempt_id": context.attempt_id,
        "task": request.to_dict(),
        "selection": request.selection.to_dict(),
        "context_halo": context.halo.to_dict(),
        "coordinate_convention": COORDINATE_CONVENTION,
        "brief": {
            "instruction": request.instruction,
            "mode": request.mode,
            "target_ids": list(request.target_ids),
            "seed": request.seed,
        },
        "budget": adapter.budget.to_dict(),
        "readonly_tools": adapter.tools.describe(),
        "reference_inventory": context.reference_inventory(),
        "selection_report": context.selection_report,
        "scene_summary": context.scene_summary,
        "write_policy": context.write_policy,
        "agent_restrictions": [
            "no direct access to project files, NBT or the scene",
            "no write of any kind; only the declared read-only tools answer questions",
            "no widening of the selection, no new rules, budgets or target versions",
            "no accept/reject/export: only the user may accept a candidate",
        ],
        "response_contract": _response_contract(adapter),
    }


def build_review_payload(adapter: "AgentAdapter", context: AgentContext,
                         evidence: Mapping) -> dict:
    """The exact payload sent to the Agent for an evidence review."""
    payload = build_plan_payload(adapter, context)
    payload["request_kind"] = "review_evidence"
    payload["known_write_policy"] = payload.pop("write_policy", None)
    payload["evidence"] = dict(evidence)
    payload["evidence_sha256"] = _sha256(_json_bytes(dict(evidence)))
    payload["review_instructions"] = {
        "layers": {
            "A": "deterministic data/construction checks (already attached in evidence)",
            "B": "render consistency and tool health (already attached in evidence)",
            "C": "your visual review: point at locatable suspicions only",
        },
        "must_not": [
            "claim a finding is confirmed (the backend re-verifies)",
            "report aesthetic reasons as hard errors",
            "return a plan here; repairs go through a new generate_plan request",
            "answer for a different candidate_id/scene_hash",
        ],
        "evidence_requests": (
            "ask for read-only queries only; write/cancel/accept/export are not tools "
            "and are refused unexecuted"
        ),
    }
    return payload


# --------------------------------------------------------------------------
# transport results
# --------------------------------------------------------------------------


@dataclass
class TransportResult:
    transport: str
    payload_text: str
    stderr_text: str = ""
    exit_code: Optional[int] = None
    duration_ms: int = 0
    bytes_read: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    endpoint: Optional[str] = None

    def summary(self) -> dict:
        return {
            "transport": self.transport,
            "endpoint": self.endpoint,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "bytes_read": self.bytes_read,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "stdout_sha256": _sha256(self.payload_text.encode("utf-8", "replace")),
            "stderr_summary": _truncate(self.stderr_text),
            "stdout_summary": _truncate(self.payload_text),
        }


def _filtered_env(allowlist: Sequence[str]) -> Dict[str, str]:
    """Only the allowlisted names are inherited; a secret name is an error."""
    _reject_secret_env_names(allowlist)
    env: Dict[str, str] = {}
    for name in allowlist:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


class _PipeReader(threading.Thread):
    """Reads a pipe with a hard cap; never lets the child flood memory."""

    def __init__(self, stream, cap: int, name: str):
        super().__init__(name=name, daemon=True)
        self._stream = stream
        self._cap = cap
        self.data = bytearray()
        self.overflowed = False

    def run(self) -> None:  # pragma: no cover - exercised through the runner tests
        reader = getattr(self._stream, "read1", None) or self._stream.read
        try:
            while True:
                chunk = reader(_READ_CHUNK)
                if not chunk:
                    break
                room = self._cap - len(self.data)
                if room <= 0:
                    self.overflowed = True
                    break
                if len(chunk) > room:
                    self.data.extend(chunk[:room])
                    self.overflowed = True
                    break
                self.data.extend(chunk)
        except (OSError, ValueError):
            # the pipe was closed under us (process killed) - what we have is what
            # the process produced, and it is recorded as such
            pass


def _reject_secret_env_names(allowlist: Sequence[str]) -> None:
    """Refuse a secret-looking name in the runner environment allowlist."""
    for name in allowlist:
        if _SECRET_ENV_RE.search(name):
            raise AgentConfigError(
                f"{name!r} must not be in env_allowlist: tokens, keys and credentials "
                "are never inherited by the Agent runner"
            )


class _PipeWriter(threading.Thread):
    """Writes the request to stdin and closes it: no interactive stdin, ever."""

    def __init__(self, stream, payload: bytes):
        super().__init__(name="agent-stdin", daemon=True)
        self._stream = stream
        self._payload = payload
        self.error: Optional[str] = None

    def run(self) -> None:  # pragma: no cover - exercised through the runner tests
        try:
            self._stream.write(self._payload)
            self._stream.flush()
        except (OSError, ValueError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass


def _call_runner(spec: RunnerSpec, payload: bytes, *,
                 cancel: Optional[Callable[[], bool]]) -> TransportResult:
    """Run a local command with a bounded deadline, bounded output, fixed cwd."""
    if not spec.argv:
        raise AgentConfigError("runner.argv is empty")
    cwd = None
    if spec.working_dir is not None:
        cwd = Path(spec.working_dir)
        if not cwd.is_dir():
            raise AgentConfigError(
                f"runner working directory {cwd} does not exist: the Agent runner "
                "always runs in a fixed directory, never in whatever the caller had"
            )
    else:
        raise AgentConfigError(
            "runner mode needs a fixed working directory (RunnerSpec.working_dir or "
            "AgentAdapter(work_dir=...)); inheriting the caller's cwd is not allowed"
        )
    env = _filtered_env(spec.env_allowlist)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv comes from the operator config
            list(spec.argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            shell=False,
            # buffered pipes: a raw unbuffered write may be partial on Windows, and
            # the reader uses read1() so a bound is still enforced per chunk
        )
    except (OSError, ValueError) as exc:
        raise AgentError(
            AGENT_UNAVAILABLE,
            f"cannot start the configured Agent runner: {type(exc).__name__}: {exc}",
            {"argv0": str(spec.argv[0])},
        ) from None

    stdout_reader = _PipeReader(proc.stdout, spec.max_output_bytes, "agent-stdout")
    stderr_reader = _PipeReader(proc.stderr, STDERR_CAPTURE_BYTES, "agent-stderr")
    writer = _PipeWriter(proc.stdin, payload)
    stdout_reader.start()
    stderr_reader.start()
    writer.start()

    def stop() -> None:
        try:
            proc.kill()
        except OSError:  # pragma: no cover - already gone
            pass
        try:
            proc.wait(timeout=_JOIN_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass

    def result(payload_bytes: bytearray, *, truncated: bool) -> TransportResult:
        return TransportResult(
            transport="runner",
            payload_text=bytes(payload_bytes).decode("utf-8", errors="replace"),
            stderr_text=bytes(stderr_reader.data).decode("utf-8", errors="replace"),
            exit_code=proc.returncode,
            duration_ms=int((time.monotonic() - started) * 1000),
            bytes_read=len(payload_bytes),
            stdout_truncated=truncated,
            stderr_truncated=stderr_reader.overflowed,
        )

    deadline = started + spec.timeout_seconds
    timed_out = False
    canceled = False
    while True:
        if cancel is not None and cancel():
            canceled = True
            stop()
            break
        if stdout_reader.overflowed:
            stop()
            break
        if proc.poll() is not None:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            stop()
            break
        time.sleep(_READ_POLL_SECONDS)

    stdout_reader.join(_JOIN_SECONDS)
    stderr_reader.join(_JOIN_SECONDS)
    writer.join(_JOIN_SECONDS)
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        try:
            stream.close()
        except (OSError, ValueError):  # pragma: no cover - defensive
            pass

    if canceled:
        raise AgentError(AGENT_CANCELED, "the run was canceled by the caller",
                         {"transport": "runner", "phase": "runner"})
    if timed_out:
        raise AgentError(
            AGENT_TIMEOUT,
            f"the Agent runner exceeded the {spec.timeout_seconds:.1f}s budget and was killed",
            {
                "transport": "runner",
                "timeout_seconds": spec.timeout_seconds,
                "stdout_summary": _truncate(
                    bytes(stdout_reader.data).decode("utf-8", errors="replace")
                ),
                "stderr_summary": _truncate(
                    bytes(stderr_reader.data).decode("utf-8", errors="replace")
                ),
            },
        )
    if stdout_reader.overflowed:
        raise AgentError(
            AGENT_OUTPUT_TOO_LARGE,
            f"the Agent runner produced more than {spec.max_output_bytes} byte(s) of "
            "stdout; the output was cut off and the run was killed",
            {"transport": "runner", "max_output_bytes": spec.max_output_bytes},
        )
    outcome = result(stdout_reader.data, truncated=False)
    if outcome.exit_code != 0:
        raise AgentError(
            AGENT_EXIT_NONZERO,
            f"the Agent runner exited with status {outcome.exit_code}",
            {
                "transport": "runner",
                "exit_code": outcome.exit_code,
                "stderr_summary": _truncate(outcome.stderr_text),
                "stdout_summary": _truncate(outcome.payload_text),
            },
        )
    return outcome


def _call_provider(spec: ProviderSpec, payload: bytes, *,
                   cancel: Optional[Callable[[], bool]]) -> TransportResult:
    """POST the envelope with urllib, bounded by a timeout and a size cap."""
    if cancel is not None and cancel():
        raise AgentError(AGENT_CANCELED, "the run was canceled by the caller",
                         {"transport": "provider", "phase": "before_request"})
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        **{str(k): str(v) for k, v in spec.headers.items()},
    }
    request = urllib.request.Request(spec.url, data=payload, headers=headers, method="POST")
    started = time.monotonic()

    def duration_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        with urllib.request.urlopen(request, timeout=spec.timeout_seconds) as response:
            declared = response.headers.get("Content-Length") if response.headers else None
            if declared is not None:
                try:
                    if int(declared) > spec.max_output_bytes:
                        raise AgentError(
                            AGENT_OUTPUT_TOO_LARGE,
                            f"provider declared {declared} byte(s), over the "
                            f"{spec.max_output_bytes} byte cap",
                            {"transport": "provider", "max_output_bytes": spec.max_output_bytes},
                        )
                except ValueError:
                    declared = None
            raw = response.read(spec.max_output_bytes + 1)
    except AgentError:
        raise
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - diagnostics only
            body = ""
        raise AgentError(
            AGENT_UNAVAILABLE,
            f"the Agent provider answered HTTP {exc.code}",
            {"transport": "provider", "status": exc.code,
             "body_summary": _truncate(body, 500), "endpoint": spec.url},
        ) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise AgentError(
                AGENT_TIMEOUT,
                f"the Agent provider timed out after {spec.timeout_seconds:.1f}s",
                {"transport": "provider", "timeout_seconds": spec.timeout_seconds},
            ) from None
        raise AgentError(
            AGENT_UNAVAILABLE,
            f"the Agent provider is unreachable: {type(reason).__name__}: {reason}",
            {"transport": "provider", "endpoint": spec.url},
        ) from None
    except (TimeoutError, socket.timeout):
        raise AgentError(
            AGENT_TIMEOUT,
            f"the Agent provider timed out after {spec.timeout_seconds:.1f}s",
            {"transport": "provider", "timeout_seconds": spec.timeout_seconds},
        ) from None
    except OSError as exc:
        raise AgentError(
            AGENT_UNAVAILABLE,
            f"the Agent provider is unreachable: {type(exc).__name__}: {exc}",
            {"transport": "provider", "endpoint": spec.url},
        ) from None

    if len(raw) > spec.max_output_bytes:
        raise AgentError(
            AGENT_OUTPUT_TOO_LARGE,
            f"the Agent provider returned more than {spec.max_output_bytes} byte(s)",
            {"transport": "provider", "max_output_bytes": spec.max_output_bytes},
        )
    if cancel is not None and cancel():
        # a blocking HTTP call cannot be interrupted mid-flight; a late answer
        # after the user canceled is never allowed to advance the task.
        raise AgentError(AGENT_CANCELED, "the run was canceled while the provider answered",
                         {"transport": "provider", "phase": "after_response"})
    return TransportResult(
        transport="provider",
        payload_text=raw.decode("utf-8", errors="replace"),
        stderr_text="",
        exit_code=None,
        duration_ms=duration_ms(),
        bytes_read=len(raw),
        endpoint=spec.url,
    )


# --------------------------------------------------------------------------
# envelope + plan validation
# --------------------------------------------------------------------------


def _envelope_fields(kind: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    if kind == "generate_plan":
        return PLAN_RESPONSE_REQUIRED, PLAN_RESPONSE_OPTIONAL
    return REVIEW_RESPONSE_REQUIRED, REVIEW_RESPONSE_OPTIONAL


def _validate_identity(raw: Mapping, kind: str, *, task_id: str, attempt_id: str) -> None:
    if raw.get("schema_version") != PROTOCOL_VERSION:
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"response schema_version must be {PROTOCOL_VERSION!r}, got "
            f"{raw.get('schema_version')!r}",
            {"field": "schema_version"},
        )
    if raw.get("request_kind") != kind:
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"response request_kind must be {kind!r}, got {raw.get('request_kind')!r}",
            {"field": "request_kind"},
        )
    for field_name, expected in (("task_id", task_id), ("attempt_id", attempt_id)):
        if raw.get(field_name) != expected:
            raise AgentError(
                AGENT_PROTOCOL_INVALID,
                f"response {field_name} {raw.get(field_name)!r} does not match the "
                f"request {expected!r}",
                {"field": field_name},
            )


def _parse_envelope(text: str, kind: str, *, task_id: str, attempt_id: str) -> dict:
    """Parse and strictly validate one response envelope."""
    if not text.strip():
        raise AgentError(AGENT_PROTOCOL_INVALID, "the response is empty", {"kind": kind})
    try:
        raw = _strict_json(text)
    except ValueError as exc:
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"the response is not strict JSON: {exc}",
            {"kind": kind, "body_summary": _truncate(text, 500)},
        ) from None
    if not isinstance(raw, Mapping):
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"the response must be a JSON object, got {type(raw).__name__}",
            {"kind": kind},
        )
    required, optional = _envelope_fields(kind)
    unknown = sorted(set(raw) - set(required) - set(optional))
    if unknown:
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"response has unknown field(s) {unknown}",
            {"kind": kind, "allowed": list(required + optional)},
        )
    missing = sorted(set(required) - set(raw))
    if missing:
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"response is missing required field(s) {missing}",
            {"kind": kind, "required": list(required)},
        )
    _validate_identity(raw, kind, task_id=task_id, attempt_id=attempt_id)
    if kind == "generate_plan":
        if not isinstance(raw["plan"], Mapping):
            raise AgentError(AGENT_PROTOCOL_INVALID, "response 'plan' must be an object",
                             {"field": "plan"})
        _validate_agent_evidence(raw["agent_evidence"])
    else:
        _validate_review(raw)
    return dict(raw)


def _validate_agent_evidence(evidence: Any) -> None:
    if not isinstance(evidence, Mapping):
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            "response 'agent_evidence' must be an object describing what the Agent did",
            {"field": "agent_evidence"},
        )
    unknown = sorted(set(evidence) - set(AGENT_EVIDENCE_REQUIRED) - set(AGENT_EVIDENCE_OPTIONAL))
    if unknown:
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"agent_evidence has unknown field(s) {unknown}",
            {"field": "agent_evidence", "allowed": list(AGENT_EVIDENCE_REQUIRED + AGENT_EVIDENCE_OPTIONAL)},
        )
    missing = sorted(set(AGENT_EVIDENCE_REQUIRED) - set(evidence))
    if missing:
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"agent_evidence is missing required field(s) {missing}",
            {"field": "agent_evidence"},
        )
    if not isinstance(evidence["summary"], str) or not evidence["summary"].strip():
        raise AgentError(AGENT_PROTOCOL_INVALID, "agent_evidence.summary must be a non-empty string",
                         {"field": "agent_evidence.summary"})
    confidence = evidence.get("confidence")
    if confidence is not None and confidence not in ("low", "medium", "high"):
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"agent_evidence.confidence must be low|medium|high, got {confidence!r}",
            {"field": "agent_evidence.confidence"},
        )
    assumptions = evidence.get("assumptions")
    if assumptions is not None and (
        not isinstance(assumptions, (list, tuple))
        or not all(isinstance(a, str) for a in assumptions)
    ):
        raise AgentError(AGENT_PROTOCOL_INVALID,
                         "agent_evidence.assumptions must be a list of strings",
                         {"field": "agent_evidence.assumptions"})


def _validate_review(raw: Mapping) -> None:
    if raw["review_kind"] != REVIEW_KIND:
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"review_kind must be {REVIEW_KIND!r}, got {raw['review_kind']!r}: this round "
            "reviews hard errors only",
            {"field": "review_kind"},
        )
    if raw["verdict"] not in REVIEW_VERDICTS:
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"verdict {raw['verdict']!r} is not one of {list(REVIEW_VERDICTS)}",
            {"field": "verdict", "allowed": list(REVIEW_VERDICTS)},
        )
    for field_name in ("candidate_id", "scene_hash"):
        if not isinstance(raw[field_name], str) or not raw[field_name].strip():
            raise AgentError(
                AGENT_PROTOCOL_INVALID,
                f"{field_name} must be a non-empty string",
                {"field": field_name},
            )
    findings = raw["findings"]
    if not isinstance(findings, (list, tuple)):
        raise AgentError(AGENT_PROTOCOL_INVALID, "findings must be an array",
                         {"field": "findings"})
    for index, finding in enumerate(findings):
        _validate_finding(finding, index)
    requests = raw.get("evidence_requests") or []
    if not isinstance(requests, (list, tuple)):
        raise AgentError(AGENT_PROTOCOL_INVALID, "evidence_requests must be an array",
                         {"field": "evidence_requests"})
    for index, request in enumerate(requests):
        if not isinstance(request, Mapping):
            raise AgentError(AGENT_PROTOCOL_INVALID,
                             f"evidence_requests[{index}] must be an object",
                             {"field": f"evidence_requests[{index}]"})
        unknown = sorted(set(request) - {"tool", "params"})
        if unknown:
            raise AgentError(
                AGENT_PROTOCOL_INVALID,
                f"evidence_requests[{index}] has unknown field(s) {unknown}",
                {"field": f"evidence_requests[{index}]"},
            )
        if "tool" not in request:
            raise AgentError(AGENT_PROTOCOL_INVALID,
                             f"evidence_requests[{index}] needs a tool name",
                             {"field": f"evidence_requests[{index}]"})


def _validate_finding(finding: Any, index: int) -> None:
    where = f"findings[{index}]"
    if not isinstance(finding, Mapping):
        raise AgentError(AGENT_PROTOCOL_INVALID, f"{where} must be an object", {"field": where})
    unknown = sorted(set(finding) - set(FINDING_REQUIRED) - set(FINDING_OPTIONAL))
    if unknown:
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"{where} has unknown field(s) {unknown}",
            {"field": where, "allowed": list(FINDING_REQUIRED + FINDING_OPTIONAL)},
        )
    missing = sorted(set(FINDING_REQUIRED) - set(finding))
    if missing:
        raise AgentError(AGENT_PROTOCOL_INVALID, f"{where} is missing {missing}",
                         {"field": where})
    if not isinstance(finding["id"], str) or not finding["id"].strip():
        raise AgentError(AGENT_PROTOCOL_INVALID, f"{where}.id must be a non-empty string",
                         {"field": f"{where}.id"})
    if not isinstance(finding["observation"], str) or not finding["observation"].strip():
        raise AgentError(AGENT_PROTOCOL_INVALID,
                         f"{where}.observation must be a non-empty string",
                         {"field": f"{where}.observation"})
    if finding["code"] not in HARD_ERROR_CODES:
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            f"{where}.code {finding['code']!r} is not a hard-error code: this protocol "
            "has no aesthetic or 'make it nicer' findings",
            {"field": f"{where}.code", "allowed_codes": sorted(HARD_ERROR_CODES)},
        )
    images = finding.get("evidence_image_ids")
    if images is not None and (
        not isinstance(images, (list, tuple)) or not all(isinstance(i, str) for i in images)
    ):
        raise AgentError(AGENT_PROTOCOL_INVALID,
                         f"{where}.evidence_image_ids must be a list of strings",
                         {"field": f"{where}.evidence_image_ids"})
    for field_name in ("op_id", "object_id", "requested_check", "repair_hint"):
        value = finding.get(field_name)
        if value is not None and not isinstance(value, str):
            raise AgentError(AGENT_PROTOCOL_INVALID,
                             f"{where}.{field_name} must be a string or omitted",
                             {"field": f"{where}.{field_name}"})


# ---- the plan -------------------------------------------------------------


def _validate_plan_shape(plan: Mapping) -> None:
    """Stdlib-strict structural check (mirrors litegarden.schema.Plan)."""
    if not isinstance(plan, Mapping):
        raise AgentError(AGENT_PLAN_INVALID, f"the plan must be an object, got {type(plan).__name__}",
                         {"field": "plan"})
    unknown = sorted(set(plan) - PLAN_FIELDS)
    if unknown:
        raise AgentError(AGENT_PLAN_INVALID, f"the plan has unknown field(s) {unknown}",
                         {"field": "plan", "allowed": sorted(PLAN_FIELDS)})
    missing = sorted({"schema_version", "scene_id", "operations"} - set(plan))
    if missing:
        raise AgentError(AGENT_PLAN_INVALID, f"the plan is missing {missing}",
                         {"field": "plan"})
    if not isinstance(plan["schema_version"], str) or not plan["schema_version"]:
        raise AgentError(AGENT_PLAN_INVALID, "plan.schema_version must be a non-empty string",
                         {"field": "schema_version"})
    if not isinstance(plan["scene_id"], str) or not plan["scene_id"]:
        raise AgentError(AGENT_PLAN_INVALID, "plan.scene_id must be a non-empty string",
                         {"field": "scene_id"})
    for field_name in ("seed",):
        if field_name in plan and (
            isinstance(plan[field_name], bool) or not isinstance(plan[field_name], int)
        ):
            raise AgentError(AGENT_PLAN_INVALID, f"plan.{field_name} must be an integer",
                             {"field": field_name})
    if "style_id" in plan and not isinstance(plan["style_id"], str):
        raise AgentError(AGENT_PLAN_INVALID, "plan.style_id must be a string",
                         {"field": "style_id"})
    operations = plan["operations"]
    if not isinstance(operations, (list, tuple)):
        raise AgentError(AGENT_PLAN_INVALID, "plan.operations must be an array",
                         {"field": "operations"})
    seen: List[str] = []
    for index, op in enumerate(operations):
        where = f"operations[{index}]"
        if not isinstance(op, Mapping):
            raise AgentError(AGENT_PLAN_INVALID, f"{where} must be an object", {"field": where})
        if not isinstance(op.get("id"), str) or not op["id"].strip():
            raise AgentError(AGENT_PLAN_INVALID, f"{where}.id must be a non-empty string",
                             {"field": f"{where}.id"})
        op_id = str(op["id"])
        if op_id in seen:
            raise AgentError(AGENT_PLAN_INVALID, f"duplicate op id {op_id!r}",
                             {"field": f"{where}.id"})
        seen.append(op_id)
        name = op.get("op")
        if name not in PLAN_OPERATIONS:
            raise AgentError(
                AGENT_PLAN_INVALID,
                f"{where}.op {name!r} is not one of {sorted(PLAN_OPERATIONS)}",
                {"field": f"{where}.op"},
            )
        spec = PLAN_OPERATIONS[name]
        allowed = {"id", "op", *spec["required"], *spec["optional"]}
        extra = sorted(set(op) - allowed)
        if extra:
            raise AgentError(AGENT_PLAN_INVALID, f"{where} has unknown field(s) {extra}",
                             {"field": where, "allowed": sorted(allowed)})
        miss = sorted(set(spec["required"]) - set(op))
        if miss:
            raise AgentError(AGENT_PLAN_INVALID, f"{where} ({name}) is missing {miss}",
                             {"field": where})
        for field_name in spec["required"]:
            value = op[field_name]
            if field_name in ("width", "spacing", "count"):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise AgentError(
                        AGENT_PLAN_INVALID,
                        f"{where}.{field_name} must be an integer, got {value!r} "
                        "(booleans and floats are never coerced)",
                        {"field": f"{where}.{field_name}"},
                    )
            elif field_name == "variant" and value is None:
                continue
            elif not isinstance(value, str) or not value.strip():
                raise AgentError(
                    AGENT_PLAN_INVALID,
                    f"{where}.{field_name} must be a non-empty string, got {value!r}",
                    {"field": f"{where}.{field_name}"},
                )
        if name == "connect_path" and not 1 <= int(op["width"]) <= 5:
            raise AgentError(AGENT_PLAN_INVALID, f"{where}.width must be 1..5",
                             {"field": f"{where}.width"})
        if name == "decorate_path" and not 2 <= int(op["spacing"]) <= 64:
            raise AgentError(AGENT_PLAN_INVALID, f"{where}.spacing must be 2..64",
                             {"field": f"{where}.spacing"})
        if name == "scatter_assets" and not 1 <= int(op["count"]) <= 1000:
            raise AgentError(AGENT_PLAN_INVALID, f"{where}.count must be 1..1000",
                             {"field": f"{where}.count"})
    if not operations:
        # an empty plan is legal (it changes nothing) but must be reported
        return


def _reuse_shared_schema(plan: Mapping) -> str:
    """Run the project's pydantic plan schema when it is importable.

    The adapter's own stdlib check above is authoritative for its error codes;
    reusing the shared schema keeps plan semantics identical to the compiler's.
    An environment without pydantic reports the check as skipped instead of
    pretending it passed.
    """
    try:
        from .schema import parse_plan  # local import: keeps this module stdlib-only
    except Exception:  # pragma: no cover - only without pydantic installed
        return "skipped: litegarden.schema is unavailable"
    try:
        parse_plan(json.dumps(plan))
    except Exception as exc:  # noqa: BLE001 - pydantic.ValidationError, not imported
        raise AgentError(
            AGENT_PLAN_INVALID,
            f"the plan failed the shared plan schema: {_truncate(str(exc), 800)}",
            {"check": "shared_plan_schema"},
        ) from None
    return "reused"


def _palette_block_ids(raw: Any) -> List[str]:
    """Every block id mentioned by a palette definition (any nesting)."""
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if ":" in node:
                found.append(state_id(node))
        elif isinstance(node, Mapping):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(raw)
    return sorted(set(found))


def _prefab_block_ids(assets_dir: Optional[Path], asset_id: str) -> List[str]:
    if assets_dir is None:
        return []
    path = Path(assets_dir) / "prefabs" / f"{asset_id}.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    blocks = raw.get("blocks") or {}
    if not isinstance(blocks, Mapping):
        return []
    return sorted({state_id(str(v)) for v in blocks.values()})


def _plan_op_boxes(plan: Mapping, context: AgentContext) -> Tuple[List[dict], List[dict]]:
    """Per-operation write boxes: (violations, unverifiable)."""
    selection = context.selection.box
    boxes: Dict[str, Box3] = {}
    violations: List[dict] = []
    unverifiable: List[dict] = []
    for index, op in enumerate(plan.get("operations") or []):
        op_id = str(op["id"])
        name = op["op"]
        box: Optional[Box3] = None
        reason = ""
        asset_id = op.get("asset_id")
        if name == "place_asset":
            box = context.asset_box(str(op["site_id"]), str(asset_id))
            reason = f"site {op['site_id']!r} has no usable footprint"
        elif name == "scatter_assets":
            box = context.zone_box(str(op["zone_id"]))
            reason = f"zone {op['zone_id']!r} has no usable bbox"
        elif name == "decorate_path":
            target = boxes.get(str(op["path_id"]))
            if target is None:
                unverifiable.append({
                    "op_id": op_id, "ref": str(op["path_id"]),
                    "reason": "decorate_path references a path op that has no verified "
                              "write box earlier in this plan",
                })
                continue
            # litegarden.operations.scatter offsets decorations 2 cells sideways
            box = Box3(
                (target.min[0] - 2, target.min[1], target.min[2] - 2),
                (target.max_exclusive[0] + 2, target.max_exclusive[1],
                 target.max_exclusive[2] + 2),
            )
            reason = ""
        elif name == "connect_path":
            points = [_endpoint(op["from"], plan, context, boxes),
                      _endpoint(op["to"], plan, context, boxes)]
            if any(p is None for p in points):
                unverifiable.append({
                    "op_id": op_id,
                    "from": str(op["from"]), "to": str(op["to"]),
                    "reason": "an endpoint is not resolvable from the frozen anchors and "
                              "allowed assets, so the road's write box is unknown",
                })
                continue
            (x0, z0), (x1, z1) = points  # type: ignore[misc]
            box = context.reference_box(min(x0, x1), min(z0, z1), max(x0, x1), max(z0, z1))
            reason = ""
        if box is None:
            unverifiable.append({"op_id": op_id, "reason": reason or "no write box derivable"})
            continue
        boxes[op_id] = box
        if not _box_inside(box, selection):
            violations.append({
                "op_id": op_id,
                "op": name,
                "write_box": box.to_dict(),
                "selection": context.selection.to_dict(),
                "rule": "operation_write_box_outside_selection",
            })
    return violations, unverifiable


def _endpoint(ref: str, plan: Mapping, context: AgentContext,
              boxes: Dict[str, Box3]) -> Optional[Tuple[int, int]]:
    """Resolve 'entry_00' or '<op_id>.<entry_id>' to a local (x, z)."""
    point = context.anchor_point(ref)
    if point is not None:
        return point
    if "." not in ref:
        return None
    op_id, entry_id = ref.split(".", 1)
    origin = None
    for op in plan.get("operations") or []:
        if str(op.get("id")) != op_id:
            continue
        if op.get("op") != "place_asset":
            return None
        site = context.sites.get(str(op.get("site_id")))
        asset = context.assets.get(str(op.get("asset_id")))
        if not site or not isinstance(site.get("origin"), (list, tuple)):
            return None
        origin = (int(site["origin"][0]), int(site["origin"][1]))
        for entry in (asset or {}).get("entries") or []:
            if str(entry.get("id")) == entry_id and isinstance(entry.get("offset"), (list, tuple)):
                offset = entry["offset"]
                return (origin[0] + int(offset[0]), origin[1] + int(offset[1]))
        return None
    return None


def validate_plan(plan: Mapping, context: AgentContext) -> List[dict]:
    """Independently validate an Agent plan against the frozen authorisation.

    Raises :class:`AgentError` with ``AGENT_PLAN_INVALID`` (schema or reference
    allow-list), ``AGENT_PLAN_OUT_OF_SELECTION`` (an operation's write box
    leaves the selection) or ``AGENT_PLAN_UNVERIFIABLE`` (the adapter cannot
    prove containment).  A refusal is always whole-batch: the returned checks
    describe what did pass, and nothing here ever rewrites the plan.
    """
    checks: List[dict] = []
    _validate_plan_shape(plan)
    checks.append({"check": "plan_shape", "ok": True})
    checks.append({"check": "shared_plan_schema", "ok": True,
                   "result": _reuse_shared_schema(plan)})

    inventory = context.reference_inventory()
    usable = inventory["usable"]
    refused_ids = {entry["id"] for entry in inventory["refused"]}
    opaque_ids = {entry["id"] for entry in inventory["unverifiable"]}
    out_of_selection_reason = "reference lies outside the frozen selection"

    operations = list(plan.get("operations") or [])
    place_ids = {str(op["id"]) for op in operations if op["op"] == "place_asset"}
    path_ids = {str(op["id"]) for op in operations if op["op"] == "connect_path"}
    bad_refs: List[dict] = []
    unknown_refs: List[dict] = []
    unspecified_refs: List[dict] = []

    def _endpoint_ref(op_id: str, key: str, ref: str) -> None:
        """A path endpoint must be a frozen anchor or an earlier asset entry."""
        if context.anchor_point(ref) is not None:
            if ref in refused_ids:
                bad_refs.append({"op_id": op_id, "kind": "endpoint", "id": ref,
                                 "reason": out_of_selection_reason})
            return
        if "." not in ref:
            unknown_refs.append({"op_id": op_id, "kind": key, "id": ref,
                                 "reason": "not a frozen anchor and not '<op_id>.<entry_id>'"})
            return
        target, entry_id = ref.split(".", 1)
        if target not in place_ids:
            unknown_refs.append({
                "op_id": op_id, "kind": key, "id": ref,
                "reason": "no earlier place_asset op with that id in this plan",
            })
            return
        owner = next(op for op in operations if str(op["id"]) == target)
        asset = context.assets.get(str(owner.get("asset_id"))) or {}
        entries = [str(e.get("id")) for e in asset.get("entries") or []]
        if entry_id not in entries:
            unknown_refs.append({
                "op_id": op_id, "kind": key, "id": ref,
                "reason": f"asset {owner.get('asset_id')!r} declares no entry {entry_id!r}",
            })

    for op in operations:
        op_id = str(op["id"])
        name = op["op"]
        refs: List[Tuple[str, str]] = []
        if name == "place_asset":
            refs.append(("site_id", str(op["site_id"])))
            refs.append(("asset_id", str(op["asset_id"])))
            if op.get("variant") is not None:
                variants = (context.assets.get(str(op["asset_id"])) or {}).get("variants") or []
                if variants and str(op["variant"]) not in [str(v) for v in variants]:
                    unknown_refs.append({
                        "op_id": op_id, "kind": "variant", "id": str(op["variant"]),
                        "reason": "not a declared variant of the asset",
                    })
        elif name == "scatter_assets":
            refs.append(("zone_id", str(op["zone_id"])))
            refs.append(("asset_id", str(op["asset_id"])))
        elif name == "decorate_path":
            refs.append(("asset_id", str(op["asset_id"])))
            if str(op["path_id"]) not in path_ids:
                unknown_refs.append({
                    "op_id": op_id, "kind": "path_id", "id": str(op["path_id"]),
                    "reason": "no earlier connect_path op with that id in this plan",
                })
        elif name == "connect_path":
            refs.append(("palette_id", str(op["palette_id"])))
            _endpoint_ref(op_id, "from", str(op["from"]))
            _endpoint_ref(op_id, "to", str(op["to"]))
        for kind, ref in refs:
            exists = (
                ref in (context.sites if kind == "site_id" else
                        context.zones if kind == "zone_id" else
                        context.assets if kind == "asset_id" else
                        context.palettes)
            )
            if not exists:
                unknown_refs.append({"op_id": op_id, "kind": kind, "id": ref,
                                     "reason": "not in the frozen allow-list"})
            elif ref in refused_ids and kind in ("site_id", "zone_id"):
                bad_refs.append({
                    "op_id": op_id, "kind": kind, "id": ref,
                    "reason": out_of_selection_reason,
                })
            elif ref in opaque_ids and kind in ("site_id", "zone_id"):
                unspecified_refs.append({
                    "op_id": op_id, "kind": kind, "id": ref,
                    "reason": "the reference carries no extent, so containment "
                              "cannot be proven",
                })
    if unknown_refs:
        raise AgentError(
            AGENT_PLAN_INVALID,
            "the plan names a reference the frozen task does not allow",
            {"unknown_references": unknown_refs, "usable": usable,
             "out_of_selection_references": bad_refs},
        )
    if bad_refs:
        raise AgentError(
            AGENT_PLAN_OUT_OF_SELECTION,
            "the plan names a reference that lies outside the frozen selection; "
            "nothing is trimmed and no selection is widened",
            {"out_of_selection_references": bad_refs, "usable": usable,
             "selection": context.selection.to_dict(), "rejected_whole": True},
        )
    if unspecified_refs:
        raise AgentError(
            AGENT_PLAN_UNVERIFIABLE,
            "a referenced site/zone carries no usable extent, so the adapter cannot "
            "prove the operation stays inside the frozen selection",
            {"unverifiable_references": unspecified_refs,
             "selection": context.selection.to_dict()},
        )
    checks.append({"check": "reference_allow_list", "ok": True,
                   "usable": {k: len(v) for k, v in usable.items()},
                   "refused": sorted(refused_ids),
                   "unverifiable": sorted(opaque_ids)})

    # block / palette whitelist (the same data the compiler will use)
    palette_blocks: Dict[str, List[str]] = {}
    asset_blocks: Dict[str, List[str]] = {}
    for op in plan.get("operations") or []:
        if op["op"] == "connect_path":
            palette_id = str(op["palette_id"])
            palette_blocks[palette_id] = _palette_block_ids(context.palettes.get(palette_id))
        elif op.get("asset_id"):
            asset_id = str(op["asset_id"])
            asset_blocks[asset_id] = _prefab_block_ids(context.assets_dir, asset_id)
    if context.allowed_blocks is not None:
        rejected: List[dict] = []
        for palette_id, blocks in palette_blocks.items():
            for block in blocks:
                if block not in context.allowed_blocks:
                    rejected.append({"kind": "palette", "id": palette_id, "block": block})
        for asset_id, blocks in asset_blocks.items():
            for block in blocks:
                if block not in context.allowed_blocks:
                    rejected.append({"kind": "asset", "id": asset_id, "block": block})
        if rejected:
            raise AgentError(
                AGENT_PLAN_INVALID,
                "the plan references a block outside the verified allow-list",
                {"blocks": rejected, "allowed_blocks": sorted(context.allowed_blocks)},
            )
        checks.append({"check": "block_whitelist", "gate": "active", "ok": True,
                       "palettes": {k: v for k, v in palette_blocks.items()},
                       "assets": {k: v for k, v in asset_blocks.items()}})
    else:
        checks.append({
            "check": "block_whitelist", "gate": "inactive", "ok": True,
            "note": "the project declares no allowed_new_blocks list; reported, never "
                    "presented as verified",
        })

    violations, unverifiable = _plan_op_boxes(plan, context)
    if violations:
        raise AgentError(
            AGENT_PLAN_OUT_OF_SELECTION,
            "the plan would write outside the frozen selection; the whole plan is "
            "rejected and nothing is trimmed",
            {
                "violations": violations,
                "unverifiable": unverifiable,
                "selection": context.selection.to_dict(),
                "rejected_whole": True,
                "authoritative_gate": "compiler task_authorized mask (unchanged)",
            },
        )
    if unverifiable:
        raise AgentError(
            AGENT_PLAN_UNVERIFIABLE,
            "the adapter cannot prove every operation stays inside the frozen "
            "selection; refused rather than assumed safe",
            {
                "unverifiable": unverifiable,
                "selection": context.selection.to_dict(),
                "authoritative_gate": "compiler task_authorized mask (unchanged)",
            },
        )
    checks.append({
        "check": "write_box_within_selection", "ok": True,
        "operations": len(plan.get("operations") or []),
        "note": (
            "reference-level containment is proven by the adapter; the compiler's "
            "task_authorized mask remains the authoritative gate for every voxel"
        ),
    })
    return checks


def _check_declared_intent(intent: Any, context: AgentContext) -> Optional[dict]:
    """The Agent's self-reported write box must also stay inside the selection."""
    if intent is None:
        return None
    if not isinstance(intent, Mapping):
        raise AgentError(
            AGENT_PROTOCOL_INVALID,
            "agent_evidence.intended_write_bounds must be an object",
            {"field": "agent_evidence.intended_write_bounds"},
        )
    box = _parse_box3(intent, "agent_evidence.intended_write_bounds")
    if not _box_inside(box, context.selection.box):
        raise AgentError(
            AGENT_PLAN_OUT_OF_SELECTION,
            "the Agent declared a write intent outside the frozen selection",
            {
                "declared_write_bounds": box.to_dict(),
                "selection": context.selection.to_dict(),
                "rejected_whole": True,
            },
        )
    return {"declared_write_bounds": box.to_dict(), "within_selection": True}


# --------------------------------------------------------------------------
# outcomes
# --------------------------------------------------------------------------


@dataclass
class PlanOutcome:
    """The result of one plan attempt; ``ok`` means "validated and recorded"."""

    status: str
    attempt_id: Optional[str] = None
    plan: Optional[dict] = None
    envelope: Optional[dict] = None
    agent_evidence: Optional[dict] = None
    checks: List[dict] = field(default_factory=list)
    errors: List[dict] = field(default_factory=list)
    events: List[dict] = field(default_factory=list)
    transport: Optional[dict] = None
    task_state: Optional[str] = None
    state_changed: bool = False
    mode: str = "none"
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "PLAN_READY"

    @property
    def error_code(self) -> Optional[str]:
        return self.errors[0]["code"] if self.errors else None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "mode": self.mode,
            "attempt_id": self.attempt_id,
            "has_plan": self.plan is not None,
            "plan": self.plan,
            "agent_evidence": self.agent_evidence,
            "agent_evidence_trust": "self_reported",
            "checks": self.checks,
            "errors": self.errors,
            "events": self.events,
            "transport": self.transport,
            "task_state": self.task_state,
            "state_changed": self.state_changed,
            "notes": self.notes,
        }


@dataclass
class ReviewOutcome:
    """The result of one evidence-review round."""

    status: str
    attempt_id: Optional[str] = None
    verdict: Optional[str] = None
    findings: List[dict] = field(default_factory=list)
    evidence_results: List[dict] = field(default_factory=list)
    evidence_ok: bool = True
    review_executed: bool = False
    errors: List[dict] = field(default_factory=list)
    events: List[dict] = field(default_factory=list)
    transport: Optional[dict] = None
    task_state: Optional[str] = None
    state_changed: bool = False
    mode: str = "none"
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "REVIEWED"

    @property
    def error_code(self) -> Optional[str]:
        return self.errors[0]["code"] if self.errors else None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "mode": self.mode,
            "attempt_id": self.attempt_id,
            "verdict": self.verdict,
            "findings": self.findings,
            "evidence_results": self.evidence_results,
            "evidence_ok": self.evidence_ok,
            "review_executed": self.review_executed,
            "errors": self.errors,
            "events": self.events,
            "transport": self.transport,
            "task_state": self.task_state,
            "state_changed": self.state_changed,
            "notes": self.notes,
            "note": (
                "review_executed is only True when a real Agent answered; a finding is "
                "evidence for a human, never a confirmation"
            ),
        }


# --------------------------------------------------------------------------
# the adapter
# --------------------------------------------------------------------------


class AgentAdapter:
    """Calls one configured Agent service, bounded, read-only, no authority.

    ``run_call`` / ``provider_call`` are injection points: the defaults are the
    real subprocess and urllib transports, and offline tests replace them (or
    use a local ``python -c`` runner) without touching the adapter.
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        *,
        tools: Optional[ReadonlyToolRegistry] = None,
        work_dir: Any = None,
        run_call: Optional[Callable[..., TransportResult]] = None,
        provider_call: Optional[Callable[..., TransportResult]] = None,
        record_chars: int = DEFAULT_RECORDED_CHARS,
    ) -> None:
        self.config = config or AgentConfig.disabled()
        self.tools = tools or ReadonlyToolRegistry()
        self.work_dir = Path(work_dir) if work_dir else None
        self._run_call = run_call or _call_runner
        self._provider_call = provider_call or _call_provider
        self.record_chars = int(record_chars)
        if self.config.mode == "runner":
            spec = self.config.runner
            assert spec is not None
            if spec.working_dir is None and self.work_dir is None:
                raise AgentConfigError(
                    "runner mode needs a fixed working directory: pass work_dir=... "
                    "(the project/task directory) or RunnerSpec.working_dir"
                )
            if spec.working_dir is None:
                object.__setattr__(
                    self.config, "runner", replace(spec, working_dir=self.work_dir)
                )

    # -- construction ----------------------------------------------------

    @staticmethod
    def from_config(raw: Any = None, **kwargs: Any) -> "AgentAdapter":
        """``AgentAdapter.from_config(dict | AgentConfig | path | None)``."""
        if raw is None or isinstance(raw, AgentConfig):
            config = raw or AgentConfig.disabled()
        elif isinstance(raw, (str, Path)):
            config = AgentConfig.load(raw)
        elif isinstance(raw, Mapping):
            config = AgentConfig.from_dict(raw)
        else:
            raise AgentConfigError(f"cannot build an adapter from {type(raw).__name__}")
        return AgentAdapter(config, **kwargs)

    @property
    def mode(self) -> str:
        return self.config.mode

    @property
    def available(self) -> bool:
        return self.config.available

    @property
    def budget(self) -> RunBudget:
        spec = self.config.runner or self.config.provider
        timeout = spec.timeout_seconds if spec else DEFAULT_TIMEOUT_SECONDS
        cap = spec.max_output_bytes if spec else DEFAULT_MAX_OUTPUT_BYTES
        return RunBudget(
            timeout_seconds=timeout,
            max_output_bytes=cap,
            max_evidence_requests=self.config.max_evidence_requests,
            max_plan_attempts=self.config.max_plan_attempts,
        )

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "available": self.available,
            "config": self.config.to_dict(),
            "tools": self.tools.describe(),
            "budget": self.budget.to_dict(),
            "capabilities": {
                "generate_plan": self.available,
                "review_evidence": self.available,
                "writes": False,
                "waits_for_human_when_unconfigured": True,
            },
            "error_codes": dict(AGENT_ERROR_CODES),
        }

    # -- transports ------------------------------------------------------

    def _call(self, payload: dict, *, cancel: Optional[Callable[[], bool]]) -> TransportResult:
        if not self.available:
            raise AgentError(
                AGENT_UNAVAILABLE,
                "no Agent runner or provider is configured: the task waits for an Agent",
                {"mode": self.mode, "config_source": self.config.source},
            )
        body = _json_bytes(payload)
        if self.config.mode == "runner":
            spec = self.config.runner
            assert spec is not None
            return self._run_call(spec, body, cancel=cancel)
        spec = self.config.provider
        assert spec is not None
        return self._provider_call(spec, body, cancel=cancel)

    # -- generate_plan ---------------------------------------------------

    def generate_plan(self, context: AgentContext, *,
                      cancel: Optional[Callable[[], bool]] = None) -> PlanOutcome:
        """Ask the Agent for a plan candidate and validate it independently."""
        outcome = PlanOutcome(status="FAILED", attempt_id=context.attempt_id, mode=self.mode)
        payload = build_plan_payload(self, context)
        outcome.events.append(self._event(
            "plan_request", attempt_id=context.attempt_id, mode=self.mode,
            payload_sha256=_sha256(_json_bytes(payload)),
            tools=list(self.tools.names()), budget=self.budget.to_dict(),
        ))
        if not self.available:
            outcome.status = "WAITING_AGENT"
            outcome.errors.append(AgentError(
                AGENT_UNAVAILABLE,
                "no Agent runner or provider is configured: no plan was produced",
                {"mode": self.mode, "config_source": self.config.source,
                 "waited_for": "a configured runner/provider or a file-mode submission"},
            ).to_dict())
            outcome.events.append(self._event("plan_not_executed", mode=self.mode))
            return outcome

        try:
            transport = self._call(payload, cancel=cancel)
        except AgentError as exc:
            outcome.errors.append(exc.to_dict())
            outcome.events.append(self._event(
                "plan_transport_failed", code=exc.code, detail=_truncate(str(exc.detail), 1000)
            ))
            return outcome
        outcome.transport = transport.summary()
        outcome.events.append(self._event("plan_response", **{
            k: v for k, v in transport.summary().items() if k != "stdout_summary"
        }))
        try:
            envelope = _parse_envelope(
                transport.payload_text, "generate_plan",
                task_id=context.request.task_id, attempt_id=context.attempt_id,
            )
            outcome.envelope = envelope
            intent_check = _check_declared_intent(
                (envelope.get("agent_evidence") or {}).get("intended_write_bounds"), context
            )
            checks = validate_plan(envelope["plan"], context)
            if intent_check:
                checks.append({"check": "declared_write_intent", "ok": True, **intent_check})
        except AgentError as exc:
            outcome.errors.append(exc.to_dict())
            outcome.events.append(self._event("plan_rejected", code=exc.code,
                                              detail=exc.detail.get("violations")
                                              or exc.detail.get("unverifiable")
                                              or exc.detail))
            return outcome
        outcome.plan = dict(envelope["plan"])
        outcome.agent_evidence = dict(envelope["agent_evidence"])
        outcome.checks = checks
        outcome.status = "PLAN_READY"
        outcome.events.append(self._event(
            "plan_accepted", operations=len(outcome.plan.get("operations") or []),
            checks=[c["check"] for c in checks],
        ))
        return outcome

    # -- review_evidence -------------------------------------------------

    def review_evidence(self, context: AgentContext, *, evidence: Mapping,
                        cancel: Optional[Callable[[], bool]] = None) -> ReviewOutcome:
        """Ask the Agent to review evidence, then answer only read-only queries."""
        outcome = ReviewOutcome(status="FAILED", attempt_id=context.attempt_id, mode=self.mode)
        if not isinstance(evidence, Mapping):
            raise AgentConfigError(f"evidence must be an object, got {type(evidence).__name__}")
        payload = build_review_payload(self, context, evidence)
        outcome.events.append(self._event(
            "review_request", attempt_id=context.attempt_id, mode=self.mode,
            payload_sha256=_sha256(_json_bytes(payload)),
            evidence_sha256=payload["evidence_sha256"], tools=list(self.tools.names()),
        ))
        if not self.available:
            outcome.status = "WAITING_AGENT"
            outcome.review_executed = False
            outcome.errors.append(AgentError(
                AGENT_UNAVAILABLE,
                "no Agent runner or provider is configured: the evidence review was not "
                "executed and is reported as not executed",
                {"mode": self.mode, "config_source": self.config.source},
            ).to_dict())
            outcome.notes.append(
                "a waiting state is never presented as a completed autonomous review"
            )
            outcome.events.append(self._event("review_not_executed", mode=self.mode))
            return outcome

        try:
            transport = self._call(payload, cancel=cancel)
        except AgentError as exc:
            outcome.errors.append(exc.to_dict())
            outcome.events.append(self._event(
                "review_transport_failed", code=exc.code, detail=_truncate(str(exc.detail), 1000)
            ))
            return outcome
        outcome.transport = transport.summary()
        outcome.events.append(self._event("review_response", **{
            k: v for k, v in transport.summary().items() if k != "stdout_summary"
        }))
        try:
            envelope = _parse_envelope(
                transport.payload_text, "review_evidence",
                task_id=context.request.task_id, attempt_id=context.attempt_id,
            )
            self._check_evidence_binding(envelope, evidence)
            requests = list(envelope.get("evidence_requests") or [])
            findings = [self._bound_finding(f, context) for f in envelope["findings"]]
            if len(requests) > self.config.max_evidence_requests:
                raise AgentError(
                    AGENT_BUDGET_EXCEEDED,
                    f"the review asked for {len(requests)} evidence queries, over the "
                    f"budget of {self.config.max_evidence_requests}",
                    {"requested": len(requests),
                     "max_evidence_requests": self.config.max_evidence_requests},
                )
            # validate every request before executing any of them: a batch that
            # contains one out-of-contract query is refused as a whole, and no
            # handler runs at all.
            validated = [
                self.tools.validate(
                    self._tool_name(request), dict(request.get("params") or {})
                )
                for request in requests
            ]
        except AgentError as exc:
            outcome.errors.append(exc.to_dict())
            outcome.events.append(self._event("review_rejected", code=exc.code,
                                              detail=exc.detail))
            return outcome

        outcome.verdict = envelope["verdict"]
        outcome.findings = findings
        for name, params in validated:
            try:
                result = self.tools.execute_validated(name, params)
                digest = _sha256(_json_bytes(result)) if _is_jsonable(result) else None
                outcome.evidence_results.append({
                    "tool": name, "params": params, "ok": True, "result": result,
                    "result_sha256": digest, "read_only": True,
                })
                outcome.events.append(self._event(
                    "evidence_answered", tool=name, params=params,
                    result_sha256=digest, bytes=len(_json_bytes(result))
                    if _is_jsonable(result) else None,
                ))
            except AgentError as exc:
                outcome.evidence_ok = False
                outcome.evidence_results.append({
                    "tool": name, "params": params, "ok": False, "error": exc.to_dict(),
                })
                outcome.events.append(self._event("evidence_failed", tool=name,
                                                  params=params, code=exc.code))
        outcome.review_executed = True
        outcome.status = "REVIEWED"
        outcome.notes.append(
            "findings are locatable suspicions with evidence; confirmation stays with the "
            "backend verifiers and the human reviewer"
        )
        if not outcome.evidence_ok:
            outcome.notes.append(
                "at least one read-only query failed: a 'no_issue_observed' verdict with "
                "failed evidence must not be treated as a passed review"
            )
        return outcome

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _tool_name(request: Mapping) -> str:
        name = request.get("tool")
        if not isinstance(name, str) or not name.strip():
            raise AgentError(AGENT_REVIEW_INVALID, "an evidence request needs a tool name",
                             {"tool": name})
        return name

    def _bound_finding(self, finding: Mapping, context: AgentContext) -> dict:
        """Findings may only point inside the read-only halo."""
        record = dict(finding)
        record["bounds_local"] = None
        bounds = finding.get("bounds_local")
        if bounds is not None:
            box = _parse_box3(bounds, "findings[].bounds_local")
            halo = context.halo.box
            if not _box_inside(box, halo):
                raise AgentError(
                    AGENT_PROTOCOL_INVALID,
                    "a finding points outside the read-only halo of this task",
                    {"field": "bounds_local", "finding_id": finding["id"],
                     "bounds": box.to_dict(), "halo": halo.to_dict()},
                )
            record["bounds_local"] = box.to_dict()
        return record

    def _check_evidence_binding(self, envelope: Mapping, evidence: Mapping) -> None:
        bundle = evidence.get("candidate") if isinstance(evidence, Mapping) else None
        if not isinstance(bundle, Mapping):
            return
        for field_name in ("candidate_id", "scene_hash"):
            expected = bundle.get(field_name)
            if expected is None:
                continue
            if str(envelope.get(field_name)) != str(expected):
                raise AgentError(
                    AGENT_STALE_EVIDENCE,
                    f"the review answers for {field_name} {envelope.get(field_name)!r} "
                    f"but the evidence belongs to {expected!r}",
                    {"field": field_name, "expected": expected,
                     "answered": envelope.get(field_name)},
                )

    @staticmethod
    def _event(kind: str, **fields: Any) -> dict:
        return {"ts": _utc_now(), "kind": kind, **fields}


def _is_jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------
# task-level wiring (bounded, records everything in the attempt)
# --------------------------------------------------------------------------


def _can_transition(current: str, target: str) -> bool:
    try:
        assert_transition(current, target)
        return True
    except RedesignError:
        return False


def _advance(store: TaskStore, task_id: str, target: str, *,
             candidates: Sequence[str] = ()) -> Tuple[str, bool]:
    """Move the task state along a legal path; illegal sources raise."""
    state = store.read_state(task_id)["state"]
    if state == target:
        return state, False
    for step in candidates:
        if step == target or state == target:
            continue
        if _can_transition(state, step):
            store.set_state(task_id, step)
            state = step
    store.set_state(task_id, target)
    return target, True


def _park_waiting_for_agent(store: TaskStore, task_id: str) -> Tuple[str, bool]:
    """Park the task in WAITING_AGENT when that is legal, else report it as is."""
    try:
        return _advance(store, task_id, "WAITING_AGENT", candidates=("CONTEXT_READY",))
    except RedesignError:
        return store.read_state(task_id)["state"], False


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _append_events(path: Path, events: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")


def _record_attempt_files(store: TaskStore, task_id: str, attempt_id: str, *,
                          payload: dict, outcome: Any) -> None:
    """Write what the Agent saw, what it said, and the full event trace."""
    attempt_dir = store.task_dir(task_id) / "attempts" / attempt_id
    _write_json(attempt_dir / "agent_request.json", payload)
    _write_json(attempt_dir / "agent_response.json", {
        "mode": outcome.mode,
        "status": outcome.status,
        "transport": outcome.transport,
        "envelope": outcome.envelope,
        "findings": getattr(outcome, "findings", None) or None,
        "evidence_results": getattr(outcome, "evidence_results", None) or None,
        "errors": outcome.errors,
        "checks": getattr(outcome, "checks", None) or None,
        "note": (
            "stdout/stderr summaries are truncated; the hashes bind this record to the "
            "exact bytes the adapter read"
        ),
    })
    _append_events(attempt_dir / "agent_trace.jsonl", outcome.events)
    store.write_attempt_json(task_id, attempt_id, "agent_summary.json",
                             outcome.to_dict())


def run_agent_plan(
    adapter: AgentAdapter,
    store: TaskStore,
    task_id: str,
    *,
    attempt_id: Optional[str] = None,
    analysis: Any = None,
    assets_dir: Any = None,
    selection_report: Optional[Mapping] = None,
    scene_summary: Optional[Mapping] = None,
    write_policy: Optional[Mapping] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> PlanOutcome:
    """Run one bounded plan attempt for a frozen task and record all of it.

    No Agent configured: the task is parked in ``WAITING_AGENT``, no attempt is
    created and no plan is invented.  A failed attempt is marked ``FAILED`` with
    its structured error and the truncated stdout/stderr digest; the project
    state (scene, HEAD, revisions) is never touched, and the task state is left
    where it was so the bounded retry policy stays in charge.
    """
    request = store.read_request(task_id)
    if not adapter.available:
        state, changed = _park_waiting_for_agent(store, task_id)
        outcome = PlanOutcome(status="WAITING_AGENT", mode=adapter.mode,
                              task_state=state, state_changed=changed)
        outcome.errors.append(AgentError(
            AGENT_UNAVAILABLE,
            "no Agent runner or provider is configured: the task waits for an Agent",
            {"mode": adapter.mode, "config_source": adapter.config.source,
             "how_to_submit": "configure a runner/provider, or submit a plan from a file"},
        ).to_dict())
        outcome.events.append(adapter._event("plan_not_executed", task_id=task_id,
                                             mode=adapter.mode))
        outcome.notes.append(
            "nothing was fabricated: no attempt was created and no plan was written"
        )
        _write_json(store.task_dir(task_id) / "agent_state.json", {
            "state": "WAITING_AGENT",
            "mode": adapter.mode,
            "reason": AGENT_UNAVAILABLE,
            "updated": _utc_now(),
            "note": "waiting for a real Agent or a file-mode plan submission",
        })
        return outcome

    state, changed = _advance(store, task_id, "PLANNING", candidates=("CONTEXT_READY",))
    attempt_dir = store.create_attempt(task_id, attempt_id=attempt_id)
    aid = attempt_dir.name
    context = AgentContext.from_task(
        request, attempt_id=aid, analysis=analysis, assets_dir=assets_dir,
        selection_report=selection_report, scene_summary=scene_summary,
        write_policy=write_policy,
    )
    payload = build_plan_payload(adapter, context)
    outcome = adapter.generate_plan(context, cancel=cancel)
    outcome.attempt_id = aid
    outcome.state_changed = changed
    _record_attempt_files(store, task_id, aid, payload=payload, outcome=outcome)

    record = store.read_attempt(task_id, aid)
    if outcome.ok:
        # ``AttemptRecord.to_dict`` deliberately exposes only ``has_plan``, so the
        # accepted plan is also written next to the attempt record (the same
        # ``plan.json`` name the service layer uses for a submitted plan).  A
        # rejected plan is never written here - only the response record keeps
        # what the Agent sent, so nothing rejected can be mistaken for a plan.
        store.write_attempt_json(task_id, aid, "plan.json", outcome.plan)
        store.update_attempt(
            task_id, record, state="PLAN_READY", plan=outcome.plan,
            validation={"ok": True, "protocol": PROTOCOL_VERSION, "checks": outcome.checks,
                        "agent_evidence": outcome.agent_evidence,
                        "agent_evidence_trust": "self_reported",
                        "plan_file": "plan.json"},
        )
        after, moved = _advance(store, task_id, "PLAN_READY")
        outcome.task_state = after
        outcome.state_changed = changed or moved
        return outcome
    store.update_attempt(task_id, record, state="FAILED", plan=None,
                         validation={"ok": False, "protocol": PROTOCOL_VERSION,
                                     "checks": outcome.checks},
                         errors=outcome.errors)
    outcome.task_state = store.read_state(task_id)["state"]
    outcome.state_changed = changed
    outcome.notes.append(
        "the attempt is FAILED and immutable; the task state and the project state were "
        "left untouched so the bounded retry policy stays in charge"
    )
    return outcome


def run_agent_review(
    adapter: AgentAdapter,
    store: TaskStore,
    task_id: str,
    attempt_id: str,
    *,
    evidence: Mapping,
    analysis: Any = None,
    selection_report: Optional[Mapping] = None,
    scene_summary: Optional[Mapping] = None,
    cancel: Optional[Callable[[], bool]] = None,
    advance_state: bool = False,
) -> ReviewOutcome:
    """Run one review round and record every interaction in the attempt.

    Without a configured Agent the review is recorded as *not executed* and the
    task is parked in ``WAITING_AGENT``.  A review never writes to the scene: the
    only files touched are the attempt's own record files.
    """
    request = store.read_request(task_id)
    attempt_dir = store.task_dir(task_id) / "attempts" / attempt_id
    if not attempt_dir.exists():
        raise RedesignError("INVALID_TASK_STATE", f"unknown attempt {attempt_id!r}")
    context = AgentContext.from_task(
        request, attempt_id=attempt_id, analysis=analysis,
        selection_report=selection_report, scene_summary=scene_summary or {
            "candidate": evidence.get("candidate") if isinstance(evidence, Mapping) else None
        },
    )
    payload = build_review_payload(adapter, context, evidence)
    outcome = adapter.review_evidence(context, evidence=evidence, cancel=cancel)
    outcome.attempt_id = attempt_id
    record = store.read_attempt(task_id, attempt_id)
    previous_review = record.review or {}
    review = {
        "protocol": PROTOCOL_VERSION,
        "review_executed": outcome.review_executed,
        "status": outcome.status,
        "verdict": outcome.verdict,
        "findings": outcome.findings,
        "evidence": [
            {k: v for k, v in entry.items() if k != "result"} | {"has_result": entry.get("ok")}
            for entry in outcome.evidence_results
        ],
        "evidence_ok": outcome.evidence_ok,
        "events": outcome.events,
        "evidence_sha256": payload.get("evidence_sha256"),
        "errors": outcome.errors,
        "rounds": list(previous_review.get("rounds") or []) + [{
            "at": _utc_now(), "status": outcome.status, "verdict": outcome.verdict,
            "review_executed": outcome.review_executed,
            "evidence_ok": outcome.evidence_ok, "error": outcome.error_code,
        }],
        "note": (
            "this records exactly what the Agent saw (payload + evidence hashes) and said; "
            "confirmation/refutation of a finding is a backend decision"
        ),
    }
    if outcome.status == "WAITING_AGENT":
        state, changed = _park_waiting_for_agent(store, task_id)
        outcome.task_state = state
        outcome.state_changed = changed
    else:
        outcome.task_state = store.read_state(task_id)["state"]
    store.update_attempt(task_id, record, review=review)
    _write_json(attempt_dir / "agent_request.json", payload)
    _write_json(attempt_dir / "agent_response.json", {
        "mode": outcome.mode,
        "status": outcome.status,
        "transport": outcome.transport,
        "verdict": outcome.verdict,
        "findings": outcome.findings,
        "evidence_results": outcome.evidence_results,
        "errors": outcome.errors,
        "note": "review round: evidence results are read-only tool answers",
    })
    _append_events(attempt_dir / "agent_trace.jsonl", outcome.events)
    store.write_attempt_json(task_id, attempt_id, "agent_review_summary.json",
                             outcome.to_dict())
    if advance_state and outcome.review_executed:
        try:
            state, moved = _advance(store, task_id, "VERIFYING_FINDINGS")
            outcome.task_state = state
            outcome.state_changed = outcome.state_changed or moved
        except RedesignError as exc:
            outcome.notes.append(
                f"task state left unchanged: {exc} (the orchestrator owns the state machine)"
            )
    return outcome


def load_plan_file(path: Any) -> dict:
    """Read a file-mode plan submission (JSON only, never executed)."""
    p = Path(path)
    if not p.exists():
        raise AgentConfigError(f"plan file {p} does not exist")
    raw = _strict_json(p.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise AgentError(AGENT_PLAN_INVALID, "a plan file must contain one JSON object")
    return dict(raw)


#: Re-exported so a caller can build a context without importing redesign first.
__all__ += ["load_plan_file"]
