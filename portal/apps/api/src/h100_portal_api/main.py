import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from h100_portal_api.config import get_settings
from h100_portal_api.database import engine
from h100_portal_api.routes import (
    audit,
    auth,
    containers,
    images,
    operations,
    platform,
    self_service,
    slurm,
    ssh_keys,
    users,
)

LOG = logging.getLogger("h100-portal-api")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    yield


app = FastAPI(
    title="H100 管理平台 API", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Content-Type", "X-CSRF-Token"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    return response


@app.exception_handler(Exception)
async def safe_exception_handler(_request: Request, _exc: Exception) -> JSONResponse:
    LOG.exception("unhandled API exception")
    return JSONResponse(
        status_code=500, content={"detail": {"code": "INTERNAL_ERROR", "message": "服务器内部错误"}}
    )


@app.get("/health/live", tags=["health"])
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def health_ready() -> JSONResponse:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        worker_ready = __import__("os").path.exists(settings.worker_socket)
    except Exception:
        worker_ready = False
    if not worker_ready:
        return JSONResponse(status_code=503, content={"status": "not_ready", "worker": False})
    return JSONResponse(
        status_code=200, content={"status": "ready", "worker": True, "database": True}
    )


app.include_router(auth.router, prefix="/api/v1")
app.include_router(platform.router, prefix="/api/v1")
app.include_router(self_service.router, prefix="/api/v1")
app.include_router(slurm.router, prefix="/api/v1")
app.include_router(containers.router, prefix="/api/v1")
app.include_router(images.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(ssh_keys.router, prefix="/api/v1")
app.include_router(operations.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
