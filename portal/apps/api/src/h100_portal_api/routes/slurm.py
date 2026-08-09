from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from h100_portal_api.auth import AuthContext
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.routes.platform import adapter, production_pilot_state

router = APIRouter(prefix="/slurm", tags=["slurm"])


@router.get("/nodes")
def nodes(context: AuthContext = Depends(permission_dependency("slurm.read"))) -> dict[str, Any]:
    return adapter("slurm.node.read", context)


@router.get("/jobs")
def jobs(context: AuthContext = Depends(permission_dependency("jobs.read"))) -> dict[str, Any]:
    return adapter("slurm.jobs.read", context)


@router.get("/accounts")
def accounts(context: AuthContext = Depends(permission_dependency("slurm.read"))) -> dict[str, Any]:
    return adapter("slurm.accounts.read", context)


@router.get("/history")
def history(context: AuthContext = Depends(permission_dependency("slurm.read"))) -> dict[str, Any]:
    return adapter("slurm.history.read", context)


@router.get("/production-pilot")
def production_pilot(
    _context: AuthContext = Depends(permission_dependency("slurm.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return production_pilot_state(db)
