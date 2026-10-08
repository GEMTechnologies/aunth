"""Run Alembic in the container, where the local ``alembic/`` package shadows the real one.

THE BUG THIS FIXES, FOUND ON THE FIRST REAL DEPLOYMENT
------------------------------------------------------
`docker compose run migrate` died with:

    File "/opt/venv/bin/alembic", line 3, in <module>
        from alembic.config import main
    ModuleNotFoundError: No module named 'alembic.config'

`Auth/backend/alembic/` is an importable package - it has an `__init__.py` - and the Dockerfile sets
`PYTHONPATH=/app/backend`. So when the `alembic` console script tries to import the *installed*
distribution, Python finds the local directory first, which has no `config.py`.

This is ADR-0003. The test suite has always worked around it (`conftest.import_real_alembic`), which
is exactly why it survived 1237 passing tests and only appeared when the image was actually run -
the trap was documented and mitigated everywhere EXCEPT the path that ships.

WHAT THIS DOES
--------------
Removes the shadowing paths from ``sys.path`` before importing Alembic, so the installed distribution
wins. It does NOT remove ``/app/backend`` permanently: ``alembic.ini`` sets ``prepend_sys_path = .``,
which re-adds it when the config loads, and ``env.py`` needs it to import `models` and `config`.

So the application stays on the path for migrations, and only Alembic's own modules come from the
installed package - which is what both of them want.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    # The directory holding THIS file is the one that shadows alembic.
    here = Path(__file__).resolve().parent

    shadowing = {
        "",
        ".",
        str(here),
        str(here.parent),
    }
    sys.path[:] = [entry for entry in sys.path if entry not in shadowing]

    # Belt and braces: if the local package was already imported (it should not have been), drop it
    # so the real one is loaded fresh.
    for name in [m for m in sys.modules if m == "alembic" or m.startswith("alembic.")]:
        if not hasattr(sys.modules[name], "__path__"):
            continue
        del sys.modules[name]

    from alembic.config import main as alembic_main

    # Hand over to Alembic's own CLI, exactly as the console script would.
    sys.argv = ["alembic", *sys.argv[1:]]
    return alembic_main(argv=sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
