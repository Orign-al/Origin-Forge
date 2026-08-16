import argparse
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.database import SessionLocal
from h100_portal_api.enums import OperationStatus
from h100_portal_api.models import (
    PortalComputeLease,
    PortalComputeResourceRequest,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalOperationEvent,
    PortalProvisionPlan,
    PortalResourceReservation,
    PortalSshKey,
    PortalStorageResource,
    utcnow,
)
from h100_portal_api.worker_client import WorkerClientError, call_worker

ReconciliationStatus = Literal["VERIFIED", "MANUAL_REVIEW", "NOT_ELIGIBLE"]
RECONCILER_ACTOR = "portal-provision-reconciler"
WorkerCaller = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class ZeroResidueResult:
    status: Literal["VERIFIED", "MANUAL_REVIEW"]
    portal_residue: tuple[str, ...]
    host_residue: tuple[str, ...]
    unknown_resource_state: tuple[str, ...]
    script_integrity: str

    @property
    def all_absent(self) -> bool:
        return (
            self.status == "VERIFIED"
            and not self.portal_residue
            and not self.host_residue
            and not self.unknown_resource_state
            and self.script_integrity == "PASS"
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "portal_residue": list(self.portal_residue),
            "host_residue": list(self.host_residue),
            "unknown_resource_state": list(self.unknown_resource_state),
            "script_integrity": self.script_integrity,
            "checked_at": utcnow().isoformat(),
            "authoritative": True,
        }


@dataclass(frozen=True)
class ReconciliationResult:
    status: ReconciliationStatus
    changed: bool
    plan_id: uuid.UUID
    stage_operation_id: uuid.UUID | None
    verification: ZeroResidueResult | None


def portal_residue(db: Session, account_id: uuid.UUID) -> tuple[str, ...]:
    managed_ids = select(PortalManagedUser.id).where(PortalManagedUser.portal_user_id == account_id)
    checks = {
        "PORTAL_COMPUTE_IDENTITY": db.scalar(
            select(PortalManagedUser.id).where(PortalManagedUser.portal_user_id == account_id)
        ),
        "PORTAL_CONTAINER": db.scalar(
            select(PortalContainer.id).where(PortalContainer.managed_user_id.in_(managed_ids))
        ),
        "PORTAL_STORAGE": db.scalar(
            select(PortalStorageResource.id).where(
                PortalStorageResource.owner_managed_user_id.in_(managed_ids)
            )
        ),
        "PORTAL_LEASE": db.scalar(
            select(PortalComputeLease.id).where(PortalComputeLease.managed_user_id.in_(managed_ids))
        ),
        "PORTAL_SSH_KEY": db.scalar(
            select(PortalSshKey.id).where(PortalSshKey.owner_managed_user_id.in_(managed_ids))
        ),
    }
    return tuple(sorted(name for name, value in checks.items() if value is not None))


def _verification_payload(
    item: PortalComputeResourceRequest,
    plan: PortalProvisionPlan,
    failed_stage: PortalOperation,
) -> dict[str, Any]:
    return {
        "request_id": str(item.id),
        "plan_id": str(plan.id),
        "stage_operation_id": str(failed_stage.id),
        "portal_account_id": str(item.portal_account_id),
        "username": plan.username,
        "uid": plan.uid,
        "gid": plan.gid,
        "project_id": plan.project_id,
        "ssh_port": plan.container_ssh_port,
        "container_name": plan.container_name,
    }


def authoritative_zero_residue(
    db: Session,
    item: PortalComputeResourceRequest,
    plan: PortalProvisionPlan,
    failed_stage: PortalOperation,
    *,
    actor: str,
    worker_call: WorkerCaller | None = None,
) -> ZeroResidueResult:
    """Run the only authoritative post-rollback verifier for one attempt."""
    invoke_worker = worker_call or call_worker
    portal = portal_residue(db, item.portal_account_id)
    try:
        worker = invoke_worker(
            "compute.provision.retry_verify",
            payload=_verification_payload(item, plan, failed_stage),
            requested_by=actor,
            approved_by=actor,
            idempotency_key=(
                f"compute-auto-reconcile:{failed_stage.id}:{int(utcnow().timestamp())}"
            ),
            dry_run=True,
            timeout_seconds=90,
        )
    except WorkerClientError as exc:
        return ZeroResidueResult(
            status="MANUAL_REVIEW",
            portal_residue=portal,
            host_residue=(),
            unknown_resource_state=(f"ROOT_WORKER:{exc.code}"[:128],),
            script_integrity="UNKNOWN",
        )
    raw_host = worker.get("resource_residue")
    raw_unknown = worker.get("unknown_resource_state")
    if isinstance(raw_host, list):
        host_schema_valid = True
        host = tuple(sorted(str(value)[:128] for value in raw_host))
    else:
        host_schema_valid = False
        host = ()
    if isinstance(raw_unknown, list):
        unknown_schema_valid = True
        unknown_values = tuple(sorted(str(value)[:128] for value in raw_unknown))
    else:
        unknown_schema_valid = False
        unknown_values = ()
    unknown = (
        unknown_values
        if host_schema_valid and unknown_schema_valid
        else tuple(sorted({*unknown_values, "ROOT_WORKER_RESULT"}))
    )
    script_integrity = str(worker.get("script_integrity", "UNKNOWN"))[:16]
    verified = bool(
        not portal
        and worker.get("status") == "DRY_RUN"
        and worker.get("handler") == "compute.provision.retry_verify"
        and worker.get("retry_verification_status") == "VERIFIED_ZERO_RESIDUE"
        and script_integrity == "PASS"
        and not host
        and not unknown
    )
    return ZeroResidueResult(
        status="VERIFIED" if verified else "MANUAL_REVIEW",
        portal_residue=portal,
        host_residue=host,
        unknown_resource_state=unknown,
        script_integrity=script_integrity,
    )


def _failed_stage_for_plan(db: Session, plan: PortalProvisionPlan) -> PortalOperation | None:
    operations = db.scalars(
        select(PortalOperation)
        .where(
            PortalOperation.target_type == "compute_resource_request",
            PortalOperation.target_id == str(plan.request_id),
            PortalOperation.status == OperationStatus.FAILED,
            PortalOperation.operation_type.in_(
                {
                    "compute.provision.stage",
                    "compute.provision",
                    "compute.provision.retry",
                }
            ),
        )
        .order_by(PortalOperation.created_at.desc())
        .with_for_update()
    ).all()
    return next(
        (
            operation
            for operation in operations
            if isinstance(operation.validated_payload, dict)
            and operation.validated_payload.get("plan_id") == str(plan.id)
        ),
        None,
    )


def _eligible_bindings(
    item: PortalComputeResourceRequest,
    plan: PortalProvisionPlan,
    failed_stage: PortalOperation,
    reservations: list[PortalResourceReservation],
) -> bool:
    payload = (
        failed_stage.validated_payload if isinstance(failed_stage.validated_payload, dict) else {}
    )
    expected_values = {
        ("UID", str(plan.uid)),
        ("GID", str(plan.gid)),
        ("PROJECT_ID", str(plan.project_id)),
        ("SSH_PORT", str(plan.container_ssh_port)),
        ("CONTAINER_NAME", plan.container_name),
    }
    expected_ids = {row.resource_type: str(row.id) for row in reservations}
    return bool(
        item.status == "FAILED"
        and item.active_slot is None
        and item.managed_user_id is None
        and item.provision_plan_id == plan.id
        and plan.request_id == item.id
        and plan.portal_account_id == item.portal_account_id
        and plan.state == "FAILED"
        and failed_stage.target_id == str(item.id)
        and failed_stage.status == OperationStatus.FAILED
        and failed_stage.rollback_status == "REQUIRES_MANUAL_REVIEW"
        and payload.get("request_id") == str(item.id)
        and payload.get("plan_id") == str(plan.id)
        and payload.get("reservation_ids") == expected_ids
        and len(reservations) == 5
        and {(row.resource_type, row.resource_value) for row in reservations} == expected_values
        and all(
            row.request_id == item.id
            and row.portal_account_id == item.portal_account_id
            and row.state == "FAILED_HOLD"
            and row.active_key == f"{row.resource_type}:{row.resource_value}"
            and row.consumed_at is None
            and row.released_at is None
            for row in reservations
        )
    )


def _already_reconciled_bindings(
    item: PortalComputeResourceRequest,
    plan: PortalProvisionPlan,
    failed_stage: PortalOperation,
    reservations: list[PortalResourceReservation],
) -> bool:
    """Recognize the exact terminal state so reconciliation is idempotent."""
    payload = (
        failed_stage.validated_payload if isinstance(failed_stage.validated_payload, dict) else {}
    )
    evidence = failed_stage.dry_run_result if isinstance(failed_stage.dry_run_result, dict) else {}
    verification = evidence.get("automatic_reconciliation")
    verified = verification if isinstance(verification, dict) else {}
    expected_values = {
        ("UID", str(plan.uid)),
        ("GID", str(plan.gid)),
        ("PROJECT_ID", str(plan.project_id)),
        ("SSH_PORT", str(plan.container_ssh_port)),
        ("CONTAINER_NAME", plan.container_name),
    }
    expected_ids = {row.resource_type: str(row.id) for row in reservations}
    return bool(
        item.status == "FAILED"
        and item.active_slot is None
        and item.managed_user_id is None
        and item.provision_plan_id == plan.id
        and plan.request_id == item.id
        and plan.portal_account_id == item.portal_account_id
        and plan.state == "FAILED"
        and failed_stage.target_id == str(item.id)
        and failed_stage.status == OperationStatus.FAILED
        and failed_stage.rollback_status == "ROLLED_BACK"
        and payload.get("request_id") == str(item.id)
        and payload.get("plan_id") == str(plan.id)
        and payload.get("reservation_ids") == expected_ids
        and evidence.get("rollback_status") == "ROLLED_BACK"
        and verified.get("status") == "VERIFIED"
        and verified.get("portal_residue") == []
        and verified.get("host_residue") == []
        and verified.get("unknown_resource_state") == []
        and verified.get("script_integrity") == "PASS"
        and len(reservations) == 5
        and {(row.resource_type, row.resource_value) for row in reservations} == expected_values
        and all(
            row.request_id == item.id
            and row.portal_account_id == item.portal_account_id
            and row.state == "RELEASED"
            and row.active_key is None
            and row.consumed_at is None
            and row.released_at is not None
            for row in reservations
        )
    )


def reconcile_plan(
    db: Session,
    plan_id: uuid.UUID,
    *,
    actor: str = RECONCILER_ACTOR,
    actor_role: str = "system",
) -> ReconciliationResult:
    """Converge a held failed attempt only from fresh authoritative observations."""
    plan = db.scalar(
        select(PortalProvisionPlan).where(PortalProvisionPlan.id == plan_id).with_for_update()
    )
    if plan is None:
        return ReconciliationResult("NOT_ELIGIBLE", False, plan_id, None, None)
    item = db.scalar(
        select(PortalComputeResourceRequest)
        .where(PortalComputeResourceRequest.id == plan.request_id)
        .with_for_update()
    )
    failed_stage = _failed_stage_for_plan(db, plan)
    reservations = list(
        db.scalars(
            select(PortalResourceReservation)
            .where(PortalResourceReservation.plan_id == plan.id)
            .with_for_update()
        ).all()
    )
    if item is None or failed_stage is None:
        return ReconciliationResult(
            "NOT_ELIGIBLE",
            False,
            plan_id,
            failed_stage.id if failed_stage is not None else None,
            None,
        )
    if _already_reconciled_bindings(item, plan, failed_stage, reservations):
        return ReconciliationResult("VERIFIED", False, plan.id, failed_stage.id, None)
    if not _eligible_bindings(item, plan, failed_stage, reservations):
        return ReconciliationResult("NOT_ELIGIBLE", False, plan.id, failed_stage.id, None)

    verification = authoritative_zero_residue(db, item, plan, failed_stage, actor=actor)
    previous = failed_stage.dry_run_result if isinstance(failed_stage.dry_run_result, dict) else {}
    failed_stage.dry_run_result = {
        **previous,
        "automatic_reconciliation": verification.evidence(),
    }
    if not verification.all_absent:
        failed_stage.rollback_status = "REQUIRES_MANUAL_REVIEW"
        return ReconciliationResult("MANUAL_REVIEW", False, plan.id, failed_stage.id, verification)

    now = utcnow()
    initial_classification = str(previous.get("side_effect_classification", "PARTIAL_UNKNOWN"))[:64]
    failed_stage.rollback_status = "ROLLED_BACK"
    failed_stage.dry_run_result = {
        **failed_stage.dry_run_result,
        "side_effect_classification": (
            "NO_SIDE_EFFECT"
            if initial_classification == "NO_SIDE_EFFECT"
            else "PARTIAL_ROLLED_BACK"
        ),
        "rollback_status": "ROLLED_BACK",
    }
    failed_stage.result_summary = (
        "Failed Provision preserved; authoritative zero-residue verification completed; "
        "FAILED_HOLD reservations released"
    )
    for reservation in reservations:
        reservation.state = "RELEASED"
        reservation.active_key = None
        reservation.consumed_at = None
        reservation.released_at = now
    db.add(
        PortalOperationEvent(
            operation_id=failed_stage.id,
            from_status=OperationStatus.FAILED,
            to_status=OperationStatus.FAILED,
            safe_message=(
                "Automatic reconciliation verified all resources absent; attempt remains failed"
            ),
            created_at=now,
        )
    )
    record_audit(
        db,
        event_type="COMPUTE_PROVISION_ROLLBACK_AUTO_VERIFIED",
        actor=actor,
        actor_role=actor_role,
        source_ip="local-worker-socket",
        user_agent="h100-portal-provision-reconciler",
        object_type="provision_plan",
        object_id=str(plan.id),
        result="SUCCESS",
        metadata={
            "request_id": str(item.id),
            "stage_operation_id": str(failed_stage.id),
            "rollback_result": "VERIFIED",
            "reservation_state": "RELEASED",
            "attempt_state": "FAILED",
            "attempt_created": False,
        },
        operation_id=failed_stage.id,
    )
    return ReconciliationResult("VERIFIED", True, plan.id, failed_stage.id, verification)


def pending_plan_ids(db: Session, *, limit: int = 32) -> list[uuid.UUID]:
    """Select only failed attempts whose allocator holds still require convergence."""
    return list(
        db.scalars(
            select(PortalProvisionPlan.id)
            .join(
                PortalComputeResourceRequest,
                PortalComputeResourceRequest.id == PortalProvisionPlan.request_id,
            )
            .join(
                PortalResourceReservation,
                PortalResourceReservation.plan_id == PortalProvisionPlan.id,
            )
            .where(
                PortalProvisionPlan.state == "FAILED",
                PortalComputeResourceRequest.status == "FAILED",
                PortalResourceReservation.state == "FAILED_HOLD",
            )
            .distinct()
            .order_by(PortalProvisionPlan.created_at)
            .limit(limit)
        ).all()
    )


def process_pending(*, limit: int = 32) -> tuple[int, int, int]:
    """Run bounded automatic reconciliation without accepting browser-supplied IDs."""
    with SessionLocal() as db:
        plan_ids = pending_plan_ids(db, limit=limit)
    verified = 0
    manual_review = 0
    for plan_id in plan_ids:
        with SessionLocal() as db:
            result = reconcile_plan(db, plan_id)
            if result.status == "VERIFIED":
                verified += 1
            elif result.status == "MANUAL_REVIEW":
                manual_review += 1
            db.commit()
    return len(plan_ids), verified, manual_review


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile failed Provision attempts")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pending", action="store_true")
    mode.add_argument("--plan-id", type=uuid.UUID)
    parser.add_argument("--request-id", type=uuid.UUID)
    parser.add_argument("--stage-operation-id", type=uuid.UUID)
    args = parser.parse_args()
    if args.pending:
        if args.request_id is not None or args.stage_operation_id is not None:
            parser.error("--pending cannot be combined with exact binding arguments")
        try:
            processed, verified, manual_review = process_pending()
        except Exception as exc:
            print(
                f"failed provision reconciler failed: {exc.__class__.__name__}",
                file=sys.stderr,
            )
            return 1
        print(f"processed={processed} verified={verified} manual_review={manual_review}")
        return 0
    if args.request_id is None or args.plan_id is None or args.stage_operation_id is None:
        parser.error("exact mode requires request, plan, and Stage Operation IDs")
    try:
        with SessionLocal() as db:
            result = reconcile_plan(db, args.plan_id)
            if result.stage_operation_id != args.stage_operation_id:
                db.rollback()
                print("status=NOT_ELIGIBLE reason=STAGE_BINDING", file=sys.stderr)
                return 2
            plan = db.get(PortalProvisionPlan, args.plan_id)
            if plan is None or plan.request_id != args.request_id:
                db.rollback()
                print("status=NOT_ELIGIBLE reason=REQUEST_BINDING", file=sys.stderr)
                return 2
            db.commit()
    except Exception as exc:
        print(
            f"failed provision reconciler failed: {exc.__class__.__name__}",
            file=sys.stderr,
        )
        return 1
    print(f"status={result.status} changed={str(result.changed).lower()}")
    return 0 if result.status in {"VERIFIED", "MANUAL_REVIEW"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
