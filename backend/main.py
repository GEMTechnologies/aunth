
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import logging
import time
import uuid

from .database import engine, Base, create_tables, DatabaseManager
from .router import router
from .config import settings

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins if settings.app_env != "development" else ["*"],
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
app.include_router(router, prefix="/api/v1")

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
