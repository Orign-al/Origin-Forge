from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from h100_portal_api.auth import AuthContext
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.models import PortalImage

router = APIRouter(prefix="/images", tags=["images"])


@router.get("")
def images(
    context: AuthContext = Depends(permission_dependency("images.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.scalars(select(PortalImage).order_by(PortalImage.imported_at.desc())).all()
    return {
        "status": "OK",
        "count": len(rows),
        "images": [
            {
                "id": str(row.id),
                "registry": row.registry_name,
                "repository": row.repository,
                "tag": row.tag,
                "digest": row.digest,
                "size_bytes": row.size_bytes,
                "architecture": row.architecture,
                "approval_state": row.approval_state,
                "imported_at": row.imported_at.isoformat() if row.imported_at else None,
                "imported_by": str(row.imported_by) if row.imported_by else None,
                "containers_in_use": None,
                "jobs_in_use": None,
            }
            for row in rows
        ],
    }
