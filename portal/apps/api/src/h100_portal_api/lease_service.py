import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from h100_portal_api.models import (
    PortalComputeLease,
    PortalLeaseRenewalRequest,
    PortalManagedUser,
    PortalUser,
    ensure_utc,
    utcnow,
)

MAX_LEASE_DURATION_SECONDS = 96 * 60 * 60
RENEWAL_WINDOW_SECONDS = 24 * 60 * 60
REMINDER_WINDOW_SECONDS = 48 * 60 * 60
ACTIVE_LEASE_STATES = {"ACTIVE", "RENEWAL_WINDOW", "RENEWAL_PENDING"}


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def managed_identity_for_user(
    db: Session, user: PortalUser, *, lock: bool = False
) -> PortalManagedUser:
    query = select(PortalManagedUser).where(PortalManagedUser.portal_user_id == user.id)
    if lock:
        query = query.with_for_update()
    managed = db.scalar(query)
    if managed is None:
        raise _error(404, "MANAGED_IDENTITY_NOT_FOUND", "当前账号没有受管计算身份")
    return managed


def _active_query(owner_id: uuid.UUID, now: datetime) -> Select[tuple[PortalComputeLease]]:
    return (
        select(PortalComputeLease)
        .where(
            PortalComputeLease.owner_managed_user_id == owner_id,
            PortalComputeLease.state.in_(ACTIVE_LEASE_STATES),
            PortalComputeLease.starts_at <= now,
        )
        .order_by(PortalComputeLease.starts_at.desc())
    )


def ensure_active_lease(
    db: Session,
    owner_id: uuid.UUID,
    *,
    now: datetime | None = None,
    lock: bool = False,
) -> PortalComputeLease:
    trusted_now = ensure_utc(now or utcnow())
    query = _active_query(owner_id, trusted_now)
    if lock:
        query = query.with_for_update()
    lease = db.scalar(query)
    if lease is not None and ensure_utc(lease.expires_at) > trusted_now:
        return lease

    expired = lease
    if expired is None:
        expired_query = (
            select(PortalComputeLease)
            .where(
                PortalComputeLease.owner_managed_user_id == owner_id,
                PortalComputeLease.state.in_(ACTIVE_LEASE_STATES),
                PortalComputeLease.expires_at <= trusted_now,
            )
            .order_by(PortalComputeLease.expires_at.desc())
        )
        if lock:
            expired_query = expired_query.with_for_update()
        expired = db.scalar(expired_query)

    if expired is not None:
        successor_query = select(PortalComputeLease).where(
            PortalComputeLease.previous_lease_id == expired.id,
            PortalComputeLease.state == "APPROVED",
            PortalComputeLease.starts_at <= trusted_now,
            PortalComputeLease.expires_at > trusted_now,
        )
        if lock:
            successor_query = successor_query.with_for_update()
        successor = db.scalar(successor_query)
        if successor is not None:
            expired.state = "EXPIRED"
            expired.expired_at = trusted_now
            successor.state = "ACTIVE"
            successor.approved_at = successor.approved_at or trusted_now
            return successor

    raise _error(409, "LEASE_INACTIVE", "计算租约已失效，不能执行该操作")


def lease_chain_terminal(
    db: Session, lease: PortalComputeLease, *, lock: bool = False
) -> PortalComputeLease:
    current = lease
    seen = {current.id}
    while True:
        query = select(PortalComputeLease).where(
            PortalComputeLease.previous_lease_id == current.id,
            PortalComputeLease.state == "APPROVED",
        )
        if lock:
            query = query.with_for_update()
        successor = db.scalar(query)
        if successor is None:
            return current
        if successor.id in seen or ensure_utc(successor.starts_at) != ensure_utc(
            current.expires_at
        ):
            raise _error(409, "LEASE_CHAIN_INVALID", "计算租约续期链不连续")
        seen.add(successor.id)
        current = successor


def entitlement(
    db: Session,
    owner_id: uuid.UUID,
    *,
    now: datetime | None = None,
    lock: bool = False,
) -> tuple[PortalComputeLease, PortalComputeLease]:
    active = ensure_active_lease(db, owner_id, now=now, lock=lock)
    return active, lease_chain_terminal(db, active, lock=lock)


def lease_view(
    db: Session,
    owner_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    trusted_now = ensure_utc(now or utcnow())
    try:
        active, terminal = entitlement(db, owner_id, now=trusted_now)
    except HTTPException:
        latest = db.scalar(
            select(PortalComputeLease)
            .where(PortalComputeLease.owner_managed_user_id == owner_id)
            .order_by(PortalComputeLease.expires_at.desc())
        )
        if latest is None:
            return {
                "state": "NOT_CREATED",
                "active": False,
                "renewal_available": False,
                "max_duration_seconds": MAX_LEASE_DURATION_SECONDS,
                "renewal_window_seconds": RENEWAL_WINDOW_SECONDS,
                "auto_renew": False,
            }
        return {
            "id": str(latest.id),
            "state": "EXPIRED" if ensure_utc(latest.expires_at) <= trusted_now else latest.state,
            "active": False,
            "starts_at": ensure_utc(latest.starts_at).isoformat(),
            "expires_at": ensure_utc(latest.expires_at).isoformat(),
            "remaining_seconds": 0,
            "renewal_available": False,
            "restore_required": True,
            "gpu_count": latest.gpu_count,
            "max_duration_seconds": MAX_LEASE_DURATION_SECONDS,
            "renewal_window_seconds": RENEWAL_WINDOW_SECONDS,
            "auto_renew": False,
        }

    effective_expiry = ensure_utc(terminal.expires_at)
    remaining = max(0, int((effective_expiry - trusted_now).total_seconds()))
    pending = db.scalar(
        select(PortalLeaseRenewalRequest).where(
            PortalLeaseRenewalRequest.owner_managed_user_id == owner_id,
            PortalLeaseRenewalRequest.lease_id == terminal.id,
            PortalLeaseRenewalRequest.state == "REQUESTED",
        )
    )
    renewal_available = 0 < remaining <= RENEWAL_WINDOW_SECONDS and pending is None
    display_state = (
        "RENEWAL_PENDING"
        if pending is not None
        else ("RENEWAL_WINDOW" if remaining <= RENEWAL_WINDOW_SECONDS else "ACTIVE")
    )
    return {
        "id": str(active.id),
        "terminal_lease_id": str(terminal.id),
        "state": display_state,
        "active": True,
        "starts_at": ensure_utc(active.starts_at).isoformat(),
        "expires_at": effective_expiry.isoformat(),
        "current_period_expires_at": ensure_utc(active.expires_at).isoformat(),
        "remaining_seconds": remaining,
        "renewal_available": renewal_available,
        "renewal_available_from": (
            effective_expiry - timedelta(seconds=RENEWAL_WINDOW_SECONDS)
        ).isoformat(),
        "reminder_48h": remaining <= REMINDER_WINDOW_SECONDS,
        "gpu_count": active.gpu_count,
        "max_duration_seconds": MAX_LEASE_DURATION_SECONDS,
        "renewal_window_seconds": RENEWAL_WINDOW_SECONDS,
        "maximum_renewal_seconds": MAX_LEASE_DURATION_SECONDS,
        "pending_renewal_id": str(pending.id) if pending is not None else None,
        "auto_renew": False,
    }


def create_lease(
    *,
    managed_user_id: uuid.UUID,
    starts_at: datetime,
    duration_seconds: int,
    gpu_count: int,
    approved_by: uuid.UUID,
    state: str = "ACTIVE",
    previous_lease_id: uuid.UUID | None = None,
    restored: bool = False,
) -> PortalComputeLease:
    if not 1 <= duration_seconds <= MAX_LEASE_DURATION_SECONDS:
        raise ValueError("lease duration must be between 1 and 345600 seconds")
    if gpu_count not in {0, 1}:
        raise ValueError("lease GPU count must be 0 or 1")
    trusted_start = ensure_utc(starts_at)
    now = utcnow()
    return PortalComputeLease(
        managed_user_id=managed_user_id,
        owner_managed_user_id=managed_user_id,
        state=state,
        gpu_count=gpu_count,
        starts_at=trusted_start,
        expires_at=trusted_start + timedelta(seconds=duration_seconds),
        duration_seconds=duration_seconds,
        max_duration_seconds=MAX_LEASE_DURATION_SECONDS,
        renewal_window_seconds=RENEWAL_WINDOW_SECONDS,
        approved_at=now,
        approved_by=approved_by,
        previous_lease_id=previous_lease_id,
        restored_at=now if restored else None,
        version=1,
    )


def request_renewal(
    db: Session,
    *,
    owner_id: uuid.UUID,
    duration_seconds: int,
    idempotency_key: str,
    now: datetime | None = None,
) -> PortalLeaseRenewalRequest:
    if not 1 <= duration_seconds <= MAX_LEASE_DURATION_SECONDS:
        raise _error(422, "LEASE_DURATION_EXCEEDED", "单次续期最长为96小时")
    trusted_now = ensure_utc(now or utcnow())
    existing = db.scalar(
        select(PortalLeaseRenewalRequest).where(
            PortalLeaseRenewalRequest.owner_managed_user_id == owner_id,
            PortalLeaseRenewalRequest.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.requested_duration_seconds != duration_seconds:
            raise _error(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于不同续期请求")
        return existing
    active, terminal = entitlement(db, owner_id, now=trusted_now, lock=True)
    remaining = int((ensure_utc(terminal.expires_at) - trusted_now).total_seconds())
    if remaining <= 0:
        raise _error(409, "LEASE_EXPIRED_RESTORE_REQUIRED", "租约已过期，请申请恢复")
    if remaining > RENEWAL_WINDOW_SECONDS:
        raise _error(409, "RENEWAL_WINDOW_NOT_OPEN", "续期将在到期前24小时开放")
    pending = db.scalar(
        select(PortalLeaseRenewalRequest).where(
            PortalLeaseRenewalRequest.owner_managed_user_id == owner_id,
            PortalLeaseRenewalRequest.state == "REQUESTED",
        )
    )
    if pending is not None:
        raise _error(409, "RENEWAL_ALREADY_PENDING", "已有待审批续期申请")
    request = PortalLeaseRenewalRequest(
        owner_managed_user_id=owner_id,
        lease_id=terminal.id,
        state="REQUESTED",
        requested_duration_seconds=duration_seconds,
        idempotency_key=idempotency_key,
        requested_at=trusted_now,
    )
    db.add(request)
    active.state = "RENEWAL_PENDING"
    db.flush()
    return request


def decide_renewal(
    db: Session,
    *,
    request_id: uuid.UUID,
    decision: str,
    decided_by: uuid.UUID,
    comment: str | None,
    now: datetime | None = None,
) -> tuple[PortalLeaseRenewalRequest, PortalComputeLease | None]:
    trusted_now = ensure_utc(now or utcnow())
    request = db.scalar(
        select(PortalLeaseRenewalRequest)
        .where(PortalLeaseRenewalRequest.id == request_id)
        .with_for_update()
    )
    if request is None:
        raise _error(404, "RENEWAL_REQUEST_NOT_FOUND", "续期申请不存在")
    if request.state != "REQUESTED":
        raise _error(409, "RENEWAL_ALREADY_DECIDED", "续期申请已处理")
    lease = db.scalar(
        select(PortalComputeLease)
        .where(PortalComputeLease.id == request.lease_id)
        .with_for_update()
    )
    if lease is None:
        raise _error(409, "LEASE_NOT_FOUND", "续期对应租约不存在")
    if ensure_utc(lease.expires_at) <= trusted_now:
        lease.state = "EXPIRED"
        lease.expired_at = trusted_now
        raise _error(409, "LEASE_EXPIRED_RESTORE_REQUIRED", "租约已过期，请改用恢复流程")
    request.decided_at = trusted_now
    request.decided_by = decided_by
    request.decision_comment = comment
    if decision == "REJECT":
        request.state = "REJECTED"
        lease.state = "RENEWAL_WINDOW"
        return request, None
    if decision != "APPROVE":
        raise _error(422, "INVALID_DECISION", "审批决定无效")
    successor = create_lease(
        managed_user_id=request.owner_managed_user_id,
        starts_at=ensure_utc(lease.expires_at),
        duration_seconds=request.requested_duration_seconds,
        gpu_count=lease.gpu_count,
        approved_by=decided_by,
        state="APPROVED",
        previous_lease_id=lease.id,
    )
    successor.renewed_at = trusted_now
    db.add(successor)
    db.flush()
    request.state = "APPROVED"
    request.resulting_lease_id = successor.id
    lease.state = "RENEWAL_WINDOW"
    return request, successor
