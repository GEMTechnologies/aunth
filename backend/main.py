from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import logging
import time
import uuid

from database import engine, Base, create_tables, DatabaseManager
from router import router
from oauth import router as oauth_router
from agent_api import router as agent_router
from config import settings

# Configure logging
# The previous expression was getattr(settings.log_level.upper()), which calls
# a two-argument builtin with one argument and raises TypeError at import.
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format=settings.log_format
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    logger.info("Starting Granada Authentication Service...")

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
    logger.info("Shutting down Granada Authentication Service...")

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
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["*.granada.example", "granada.example"]
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

    # Log request
    logger.info(
        f"Request: {request.method} {request.url.path} - "
        f"Status: {response.status_code} - "
        f"Duration: {process_time:.4f}s - "
        f"Request-ID: {request_id}"
    )

    return response

# Exception handlers
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors"""
    logger.warning(f"Validation error on {request.method} {request.url.path}: {exc}")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Validation error",
            "errors": exc.errors(),
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
async def readiness_probe():
    """Can this instance do its job? 503 when it cannot.

    Checks PostgreSQL and that the schema matches the code. Does NOT check Redis:
    its absence is DEGRADED, not unready, because PostgreSQL holds every durable fact
    and the outbox holds every undelivered event - refusing traffic would convert a
    transport outage into a data outage.
    """
    from health import readiness as _readiness

    report = _readiness(include_redis=False)
    payload = report.as_dict()
    if not report.ready:
        # 503 so a load balancer removes the instance. The body still explains why,
        # because a bare 503 sends an operator to the logs for information the
        # service already had.
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)
    return payload


@app.get("/api/v1/health/deep", tags=["Health"])
async def deep_health():
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


@app.get("/api/v1/metrics", tags=["Health"])
async def metrics():
    """Counter and gauge snapshot.

    Not Prometheus-formatted yet, and that is recorded rather than implied: the
    snapshot is what a scraper adapter would read. Exposed without authentication
    because it carries counts rather than data - no organisation id, email address or
    message content appears in it, which is a property the observability layer
    maintains by construction.
    """
    try:
        from observability import snapshot

        return {"metrics": snapshot()}
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
async def api_health_check():
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