# Granada backend. ONE image serves all three roles - API, fleet worker, outbox relay -
# because they run the same code and differ only in their command. Three images would
# drift apart; one image with three commands cannot.
#
# WHY THE FIRST VERSION OF THIS FILE COULD NEVER START THE APPLICATION
# -------------------------------------------------------------------
# It ended with:
#
#     ENV PYTHONPATH=/app
#     CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
#
# `Auth/backend` is NOT a package (there is no `__init__.py`), and every module inside it
# uses absolute imports (`from database import engine`, `import models`, `from config
# import settings`). So `import backend.main` resolves `backend` and then fails on the
# first internal import:
#
#     ModuleNotFoundError: No module named 'database'
#
# Proven by simulating it, not by reading it:
#
#     PYTHONPATH=<Auth>          python -c "import backend.main"   -> FAILS
#     PYTHONPATH=<Auth/backend>  python -c "import main"           -> OK
#
# So the working directory must BE `backend`, and the app is `main:app`. The old
# invocation would have failed on first boot with an import error - the kind of thing a
# Dockerfile that has never been built hides perfectly.

# ---------------------------------------------------------------------------
# Stage 1: build the virtual environment
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build tools, discarded with this stage. `psycopg2-binary` ships its own libpq so the
# runtime needs no system libraries - but a wheel that has to build from source needs a
# compiler, and failing here with a clear error beats failing at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# `tini` reaps zombies and forwards signals. Without an init, PID 1 is uvicorn, which does
# not forward SIGTERM to its own children - so `docker stop` waits out the timeout and
# then SIGKILLs, and the graceful shutdown this codebase implements never runs at all.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# A non-root user. A container process that can write to its own code is a container
# process that can rewrite the application, and root in a base image is a privilege nobody
# chose.
RUN groupadd --gid 10001 granada \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin granada

WORKDIR /app/backend
COPY --chown=granada:granada . /app/backend

# Work from `backend` and put `backend` on the path. See the note at the top: the app is
# `main:app`, NOT `backend.main:app`.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

USER granada

EXPOSE 8000

# `/livez`, not `/readyz`. A container healthcheck answers "is this process working". If it
# consulted readiness, a database blip would make Docker report the container unhealthy
# and restart a process that is fine - turning a thirty-second failover into a restart
# loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/livez" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# The API role. The worker and the relay override this command in compose; it is the same
# image, which is what keeps the three roles from drifting.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
