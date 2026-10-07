"""Entrypoint for the Auth service.

Two defects are fixed here, both of which meant this file could not do its job.

**It could not run at all.** The original did ``from .config import settings``,
a relative import. Executed as ``python run.py`` - which is how the README and
the runbook both describe it - there is no package context, so it raised
``ImportError: attempted relative import with no known parent package`` before
reaching uvicorn. It only ever looked correct because nobody ran it directly.

**It hardcoded a port.** ``port=8000`` while ``.env.example`` advertised
``AUTH_SERVICE_PORT=8001``. Following the template would have put the service
somewhere the frontend proxy and the OAuth redirect URIs did not expect.
``ops/ports.yaml`` is now the source of truth and this reads it.

Usage::

    python run.py                 # binds ops/ports.yaml's auth-service port
    AUTH_SERVICE_PORT=9000 python run.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Imported after the sys.path fix above, because `config` is a top-level module
# in this directory and not part of a package.
import uvicorn  # noqa: E402

from config import settings  # noqa: E402

# ops/ sits beside Auth/, so it is two levels up from Auth/backend.
_OPS = BACKEND.parent.parent / "ops"
if str(_OPS) not in sys.path:
    sys.path.insert(0, str(_OPS))

DEFAULT_PORT = 8000


def _port() -> int:
    """Resolve the bind port: environment, then ops/ports.yaml, then the default."""
    from_env = os.environ.get("AUTH_SERVICE_PORT")
    if from_env:
        try:
            return int(from_env)
        except ValueError:
            raise SystemExit(
                f"AUTH_SERVICE_PORT must be an integer, got {from_env!r}"
            )
    try:
        import ports  # type: ignore[import-not-found]

        resolved = ports.port_for("auth-service", DEFAULT_PORT)
        return int(resolved) if resolved else DEFAULT_PORT
    except Exception:
        # A missing or unreadable ops/ports.yaml must not stop the service.
        return DEFAULT_PORT


def main() -> None:
    port = _port()
    host = os.environ.get("AUTH_SERVICE_HOST", "0.0.0.0")
    print(
        f"[granada-auth] binding {host}:{port} "
        f"(app_env={settings.app_env}, debug={settings.debug})",
        file=sys.stderr,
    )
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
        access_log=True,
    )


if __name__ == "__main__":
    main()
