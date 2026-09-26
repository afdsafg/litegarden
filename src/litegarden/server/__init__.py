"""Local HTTP service for the litegarden workbench (spec 17.1/19.2).

Standard library only: ``http.server.ThreadingHTTPServer`` plus ``json``. The
frontend in ``web/`` is served from the same origin as the API, so the workbench
and its data always agree on where the project lives and no third-party web
framework becomes a runtime dependency.
"""
from __future__ import annotations

from .app import (  # noqa: F401
    API_PREFIX,
    SERVER_SCHEMA_VERSION,
    ApiError,
    ProjectService,
    ServerConfig,
    build_server,
    serve_forever,
)

__all__ = [
    "API_PREFIX",
    "SERVER_SCHEMA_VERSION",
    "ApiError",
    "ProjectService",
    "ServerConfig",
    "build_server",
    "serve_forever",
]
