from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from contextlib import asynccontextmanager
import logging
import threading
import os
import asyncio
import time
import uuid

from database import engine, Base, create_tables, DatabaseManager
from router import router
from oauth import router as oauth_router
from agent_api import router as agent_router
from ingestion_api import router as ingestion_router
from config import settings

# Configure logging
# The previous expression was getattr(settings.log_level.upper()), which calls
# a two-argument builtin with one argument and raises TypeError at import.
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format=settings.log_format
)
logger = logging.getLogger(__name__)

#: Set when shutdown begins. `/readyz` consults it so a load balancer stops routing
#: BEFORE the process stops answering - the difference between a clean drain and a burst
#: of connection-refused errors.
_SHUTTING_DOWN = threading.Event()

#: How long to stay up after reporting unready. It must outlast the load balancer's
#: health-check interval, or the drain accomplishes nothing; longer only delays deploys.
GRACEFUL_SHUTDOWN_SECONDS = float(os.environ.get("GRACEFUL_SHUTDOWN_SECONDS", "5"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    logger.info("Starting Granada Authentication Service...")
    _SHUTTING_DOWN.clear()

    # Create database tables
    try:
        create_tables()
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Failed to create database tables: {e}")
        raise

    # Verify database connection
    if DatabaseManager.health_check():
        logger.info("Database connection verified")
    else:
        logger.error("Database connection failed")
        raise Exception("Database connection failed")

    logger.info("Granada Authentication Service started successfully")

    yield

    # Shutdown
    # Shutdown. THE ORDER IS THE POINT, and it is the thing most services get wrong.
    #
    # 1. Mark unready FIRST. A load balancer only stops sending traffic once /readyz
    #    answers 503, and it needs time to notice. If the process simply exits, the LB
    #    keeps routing to it until the next health check, and every request in that
    #    window is a connection refused - exactly the error graceful shutdown exists to
    #    prevent.
    # 2. Wait the drain window. Uvicorn finishes in-flight requests itself, so this only
    #    has to outlast the LB's check interval.
    # 3. Release the pool. Disposing the engine closes pooled connections cleanly;
    #    leaving them to the OS produces "unexpected EOF on client connection" in the
    #    PostgreSQL log, which looks like an incident and is not one.
    _SHUTTING_DOWN.set()
    logger.info("Shutdown requested; /readyz now answers 503 so a load balancer can drain")
    await asyncio.sleep(GRACEFUL_SHUTDOWN_SECONDS)

    try:
        from database import engine as _engine

        _engine.dispose()
        logger.info("Database connection pool released")
    except Exception as exc:  # noqa: BLE001 - shutdown must not fail on a pool error
        logger.warning("Could not release the database pool: %s", exc)

# Create FastAPI application
app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description="Comprehensive authentication and user management service for the Granada platform",
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    lifespan=lifespan
)

# Security middleware
if settings.app_env == "production":
    # THE HOST LIST IS CONFIGURATION, NOT A CONSTANT.
    #
    # This was hardcoded to `["*.granada.example", "granada.example"]` - a placeholder domain that
    # no deployment owns. The effect on the first production launch was that every request,
    # including the container's own `curl /livez` healthcheck, came back `400 Invalid host header`,
    # so the stack reported unhealthy while being perfectly functional.
    #
    # It is also the correct fix rather than a workaround: a trusted-host allowlist exists to stop
    # DNS-rebinding, and a list that cannot name the real host provides none of that protection
    # while breaking the deployment. `ALLOWED_HOSTS` is comma-separated, and the container's own
    # loopback name is always included because the healthcheck uses it.
    allowed = [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()]
    for loopback in ("127.0.0.1", "localhost"):
        if loopback not in allowed:
            allowed.append(loopback)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed,
    )

# CORS middleware
# A wildcard origin combined with allow_credentials is rejected by browsers and
# would defeat cookie authentication if it ever took effect. Development is
# pinned to the configured local origins instead of "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"]
)

# Request tracking middleware
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Add unique request ID for tracking"""
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id

    start_time = time.time()

    response = await call_next(request)

    process_time = time.time() - start_time

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = str(process_time)

    # -- security headers -------------------------------------------
    # On EVERY response including errors, because an error page is exactly where a
    # browser is most likely to be persuaded to do something.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )
    # API responses carry organisation data, and a shared cache holding one tenant's
    # response is a disclosure.
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")
    if settings.app_env == "production":
        # Production only: HSTS from a development origin pins the browser to https
        # for localhost and breaks unrelated local work.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )

    # -- metrics -----------------------------------------------------
    # The ROUTE TEMPLATE, read after routing has run, so a request to
    # /api/v1/agent/grants/<uuid> is ONE series and not one per id.
    try:
        from prometheus_metrics import http_metrics

        http_metrics.observe(
            method=request.method,
            route=http_metrics.route_label(request),
            status_code=response.status_code,
            seconds=process_time,
        )
    except Exception:  # noqa: BLE001 - metrics must never break a request
        pass

    # Log request
    logger.info(
        f"Request: {request.method} {request.url.path} - "
        f"Status: {response.status_code} - "
        f"Duration: {process_time:.4f}s - "
        f"Request-ID: {request_id}"
    )

    return response

# Exception handlers
def _serialisable_errors(exc: RequestValidationError) -> list:
    """`exc.errors()`, made safe to serialise.

    THE DEFECT THIS FIXES, and it was system-wide rather than local.

    Pydantic v2 puts the ORIGINAL EXCEPTION OBJECT in `ctx` when a custom `field_validator` raises:

        {"type": "value_error", "loc": (...), "msg": "Value error, ...",
         "input": "...", "ctx": {"error": ValueError("...")}}

    `JSONResponse` serialises with `json.dumps`, which cannot encode a `ValueError`. So the handler
    meant to explain the error raised a `TypeError` instead, the generic handler caught it, and the
    client received **500 Internal server error** for what was a 422.

    On a producer API that is the worst possible failure: a crawler sending a malformed
    `content_hash` is told the server is broken rather than which field is wrong and why. Every
    endpoint with a custom validator had this, not only ingestion.

    `ctx` values are stringified rather than dropped, because the message is the useful part and
    discarding it would hide the reason a delivery was refused.
    """
    safe = []
    for error in exc.errors():
        item = dict(error)
        ctx = item.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {
                key: (value if isinstance(value, (str, int, float, bool, type(None))) else str(value))
                for key, value in ctx.items()
            }
        # `input` is whatever reached the validator; it is echoed for debugging, so it must be
        # stringified when it is not a primitive rather than crashing the response.
        raw_input = item.get("input")
        if raw_input is not None and not isinstance(raw_input, (str, int, float, bool, list, dict)):
            item["input"] = str(raw_input)
        safe.append(item)
    return safe


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors"""
    logger.warning(f"Validation error on {request.method} {request.url.path}: {exc}")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Validation error",
            "errors": _serialisable_errors(exc),
            "request_id": getattr(request.state, "request_id", None)
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected errors"""
    logger.error(f"Unexpected error on {request.method} {request.url.path}: {exc}")

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error",
            "request_id": getattr(request.state, "request_id", None)
        }
    )

# Include router
# Startup safety validation, run when the module is imported rather than in a startup
# hook so that a misconfigured deployment fails at import - which is when a container
# orchestrator can still be told the process never came up, rather than after it has
# begun accepting requests it cannot serve correctly.
#
# A WARNING is logged and the service runs: every warning describes a degradation. A
# REFUSAL stops the process, because the alternative is serving requests while making
# decisions with no provenance.
try:
    from health import validate_startup

    validate_startup(raise_on_refuse=True)
except RuntimeError:
    raise
except Exception as _exc:  # pragma: no cover - defensive
    logging.getLogger(__name__).warning("startup validation skipped: %s", _exc)

app.include_router(router, prefix="/api/v1")
app.include_router(oauth_router, prefix="/api/v1")
# The agent and mail surface. Registered here beside the auth routers because it uses
# the same authentication and tenant binding: the tenant comes from the validated
# token, never from a path parameter, so editing a URL cannot reach another
# organisation's correspondence.
app.include_router(agent_router, prefix="/api/v1")
# The producer doorway. Deliberately its own router with its own credential: a crawler is a
# machine identity feeding the shared catalogue, not a user acting inside one tenant.
app.include_router(ingestion_router, prefix="/api/v1")

# ---------------------------------------------------------------------------
# Liveness, readiness and metrics
# ---------------------------------------------------------------------------
# Three endpoints, deliberately separate, because they answer three different
# questions and conflating them causes outages.
#
#   /livez   is the PROCESS alive?      A failure means restart it.
#   /readyz  can it DO ITS JOB?         A failure means take it out of the pool.
#   /health  a human-readable summary   Not for a load balancer.
#
# A single endpoint that checks the database cannot serve both purposes: a database
# blip would make an orchestrator restart every healthy process, which turns a
# thirty-second database failover into a fleet-wide outage.

@app.get("/livez", tags=["Health"])
async def liveness():
    """Is the process running and able to answer?

    Deliberately checks NOTHING else. A liveness probe that fails when a dependency is
    down causes restart storms exactly when the system is least able to absorb them.
    """
    return {"status": "alive"}


@app.get("/readyz", tags=["Health"])
def readiness_probe():
    """Can this instance do its job? 503 when it cannot.

    Checks PostgreSQL and that the schema matches the code. Does NOT check Redis:
    its absence is DEGRADED, not unready, because PostgreSQL holds every durable fact
    and the outbox holds every undelivered event - refusing traffic would convert a
    transport outage into a data outage.
    """

    # NOTE: a SYNC `def`, deliberately.
    #
    # This handler does synchronous database work and contains no `await`. Declaring it
    # `async def` would run that work ON the event loop and block every other request for
    # its duration - which a load test measured at 6.7 seconds at the median under
    # concurrency 25, against 5 milliseconds for handlers that do nothing. A load balancer
    # probing this endpoint on every instance would serialise all traffic, so the health
    # check would become the outage.
    #
    # FastAPI runs a `def` handler in a threadpool worker. That is the correct place for
    # blocking I/O, and it is why these are not `async`.

    from health import readiness as _readiness
    if _SHUTTING_DOWN.is_set():
        # Unready although every dependency is healthy: this instance is going away, and
        # the honest answer to "can it do its job" is no.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "ready": False,
                "status": "NOT_READY",
                "detail": "shutting down; drain this instance",
            },
        )


    report = _readiness(include_redis=False)
    payload = report.as_dict()
    if not report.ready:
        # 503 so a load balancer removes the instance. The body still explains why,
        # because a bare 503 sends an operator to the logs for information the
        # service already had.
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)
    return payload


@app.get("/api/v1/health/deep", tags=["Health"])
def deep_health():
    """Every dependency, with a status and a detail for each.

    Readiness is enforced by the platform; this is for a person asking "what is
    wrong?", and it names the failing dependency rather than reporting that something
    is.
    """
    from health import readiness as _readiness

    report = _readiness()
    payload = report.as_dict()
    if not report.ready:
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)
    return payload


@app.get("/metrics", tags=["Health"], include_in_schema=False)
def prometheus_metrics():
    """Prometheus text exposition.

    Unauthenticated, and deliberately so: the values are counts and ages, never data. No
    organisation id, email address, message subject or request path appears in it, and
    the HTTP labels come from the **route template** rather than the path - which also
    means a scanner probing random URLs cannot create a time series per request.

    When the cross-tenant operational gauges cannot be measured they are **omitted** and
    `granada_metrics_operational_available` is 0. They are never reported as zero: a
    backlog gauge reading 0 because the role cannot see the table is worse than no gauge
    at all, because the alert on it never fires.
    """
    from prometheus_metrics import content_type as prometheus_content_type
    from prometheus_metrics import exposition

    body = exposition(metrics_url=settings.metrics_database_url)
    # The header is set directly rather than through `media_type=`, because Starlette
    # appends its own charset to a media type that already declares one.
    return Response(
        content=body,
        headers={"Content-Type": prometheus_content_type()},
    )


@app.get("/api/v1/metrics", tags=["Health"])
def metrics():
    """Counter and gauge snapshot.

    Not Prometheus-formatted yet, and that is recorded rather than implied: the
    snapshot is what a scraper adapter would read. Exposed without authentication
    because it carries counts rather than data - no organisation id, email address or
    message content appears in it, which is a property the observability layer
    maintains by construction.
    """
    try:
        # `observability` exposes the registry as an INSTANCE, not a module-level
        # function. The first version did `from observability import snapshot`, which
        # raises ImportError on every call - so this endpoint answered 503 permanently
        # and looked like a deliberate "metrics unavailable" state. Found by exercising
        # it with traffic while testing the Prometheus endpoint beside it.
        from observability import metrics as registry

        return {"metrics": registry.snapshot()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("metrics.unavailable: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "metrics are not available", "reason": type(exc).__name__},
        )


# Root endpoints
@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Granada Authentication Service",
        "version": settings.api_version,
        "status": "running",
        "docs": "/docs" if settings.debug else "disabled",
        "api": "/api/v1"
    }

@app.get("/health")
async def health_check():
    """Simple health check"""
    return {
        "status": "healthy",
        "service": "granada-auth",
        "version": settings.api_version,
        "environment": settings.app_env
    }

@app.get("/api/health")
def api_health_check():
    """Comprehensive health check with database"""
    try:
        db_healthy = DatabaseManager.health_check()

        return {
            "status": "healthy" if db_healthy else "degraded",
            "service": "granada-auth",
            "version": settings.api_version,
            "environment": settings.app_env,
            "database": "connected" if db_healthy else "disconnected",
            "components": {
                "database": "healthy" if db_healthy else "unhealthy",
                "api": "healthy"
            }
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unhealthy",
                "service": "granada-auth",
                "error": str(e)
            }
        )

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level=settings.log_level.lower()
    )