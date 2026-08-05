from collections.abc import Callable

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from h100_portal_api.auth import AuthContext, load_context
from h100_portal_api.database import get_db
from h100_portal_api.rbac import require_permission


def auth_context(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    return load_context(db, request)


def permission_dependency(permission: str) -> Callable[..., AuthContext]:
    def dependency(context: AuthContext = Depends(auth_context)) -> AuthContext:
        require_permission(context.user, permission)
        return context

    return dependency
