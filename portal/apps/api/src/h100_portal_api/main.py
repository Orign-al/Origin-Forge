import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from h100_portal_api.audit import record_audit
from h100_portal_api.auth import client_ip, user_agent
from h100_portal_api.config import get_settings
from h100_portal_api.database import SessionLocal, engine
from h100_portal_api.routes import (
    activation,
    audit,
    auth,
    compute_requests,
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
    title="Origin Forge API", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Authorization", "Content-Type", "X-CSRF-Token"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    if "token" in request.query_params:
        response = JSONResponse(
            status_code=400,
            content={
                "detail": {
                    "code": "TOKEN_QUERY_REJECTED",
                    "message": "凭据不得放入查询字符串",
                }
            },
        )
    else:
        response = await call_next(request)
    delegated = getattr(request.state, "delegated_audit", None)
    if isinstance(delegated, dict):
        try:
            with SessionLocal() as db:
                status_code = int(response.status_code)
                record_audit(
                    db,
                    event_type="DELEGATED_TEST_REQUEST",
                    actor=str(delegated["actor_user"]),
                    actor_role="platform_owner",
                    source_ip=client_ip(request),
                    user_agent=user_agent(request),
                    object_type="delegated_request",
                    object_id=request.url.path,
                    result="SUCCESS" if status_code < 400 else "DENIED",
                    metadata={
                        **delegated,
                        "action": f"{request.method} {request.url.path}",
                        "target": request.url.path,
                        "status_code": status_code,
                    },
                )
                db.commit()
        except Exception:
            LOG.exception("failed to persist delegated request audit")
    cli_token = getattr(request.state, "cli_token_audit", None)
    if isinstance(cli_token, dict):
        try:
            with SessionLocal() as db:
                status_code = int(response.status_code)
                record_audit(
                    db,
                    event_type="CLI_TOKEN_USE",
                    actor=str(cli_token["user"]),
                    actor_role="user",
                    source_ip=client_ip(request),
                    user_agent=user_agent(request),
                    object_type="cli_token",
                    object_id=str(cli_token["token_id"]),
                    result="SUCCESS" if status_code < 400 else "DENIED",
                    metadata={
                        "label": str(cli_token["label"]),
                        "action": f"{request.method} {request.url.path}",
                        "status_code": status_code,
                    },
                )
                db.commit()
        except Exception:
            LOG.exception("failed to persist CLI token request audit")
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


@app.exception_handler(RequestValidationError)
async def safe_validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    # FastAPI's default validation response includes the rejected raw input.
    # Passwords, action tokens, SSH keys and other credentials must never be
    # reflected into proxy diagnostics or browser error capture.
    errors = [
        {
            "loc": [str(item) for item in error.get("loc", ())],
            "msg": str(error.get("msg", "Invalid request"))[:256],
            "type": str(error.get("type", "value_error"))[:128],
        }
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


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


@app.get("/api/v1/cli/identity", tags=["health"])
def cli_service_identity() -> JSONResponse:
    """Return the non-secret service contract checked before CLI credentials are sent."""

    return JSONResponse(
        status_code=200,
        content={
            "service": "H100 Portal",
            "api_compatibility": "h100.cli.v1",
            "cli_versions": ["1.0.0"],
        },
        headers={"Cache-Control": "no-store"},
    )


app.include_router(auth.router, prefix="/api/v1")
app.include_router(platform.router, prefix="/api/v1")
app.include_router(compute_requests.router, prefix="/api/v1")
app.include_router(activation.router, prefix="/api/v1")
app.include_router(self_service.router, prefix="/api/v1")
app.include_router(slurm.router, prefix="/api/v1")
app.include_router(containers.router, prefix="/api/v1")
app.include_router(images.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(ssh_keys.router, prefix="/api/v1")
app.include_router(operations.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
