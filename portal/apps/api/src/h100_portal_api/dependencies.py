from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from h100_portal_api.auth import AuthContext, load_context, load_delegated_context
from h100_portal_api.database import get_db
from h100_portal_api.delegated_auth import DELEGATED_PERMISSION_SCOPES
from h100_portal_api.rbac import require_permission


def auth_context(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    return load_context(db, request)


def request_auth_context(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    if request.headers.get("authorization") is not None:
        return load_delegated_context(db, request)
    return load_context(db, request)


def require_delegated_scope(context: AuthContext, scope: str) -> None:
    if context.is_delegated and scope not in context.delegated_scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "DELEGATED_SCOPE_DENIED",
                "message": "委托测试会话不包含此权限",
            },
        )


def permission_dependency(permission: str) -> Callable[..., AuthContext]:
    def dependency(context: AuthContext = Depends(request_auth_context)) -> AuthContext:
        if context.is_delegated:
            scope = DELEGATED_PERMISSION_SCOPES.get(permission)
            if scope is None or scope not in context.delegated_scopes:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "DELEGATED_SCOPE_DENIED",
                        "message": "委托测试会话不包含此权限",
                    },
                )
        require_permission(context.user, permission)
        return context

    return dependency
