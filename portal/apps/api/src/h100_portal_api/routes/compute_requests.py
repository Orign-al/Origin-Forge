import re
import uuid
from contextvars import ContextVar
from datetime import timedelta
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from h100_portal_contracts.workspace import (
    CPU_DEVELOPMENT_PROFILE,
    profile_gpu_count,
    workspace_path,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.auth import (
    AuthContext,
    client_ip,
    rate_limiter,
    require_recent_reauthentication,
    require_session_csrf,
    user_agent,
)
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordState,
    RiskLevel,
)
from h100_portal_api.models import (
    PortalAllocatorLock,
    PortalComputeLease,
    PortalComputeResourceRequest,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalOperationEvent,
    PortalProvisionPlan,
    PortalResourceReservation,
    PortalSession,
    PortalStorageResource,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.provision_reconciliation import authoritative_zero_residue
from h100_portal_api.rbac import highest_role
from h100_portal_api.runtime_identity import deployment_version
from h100_portal_api.schemas import (
    ComputeProvisionActionRequest,
    ComputeProvisionApprovalRequest,
    ComputeProvisionReconciliationRequest,
    ComputeProvisionRetryAuthorizationRequest,
    ComputeProvisionRetryRequest,
    ComputeResourceRequestCancel,
    ComputeResourceRequestCreate,
    ComputeResourceReviewRequest,
)
from h100_portal_api.worker_client import WorkerClientError, call_worker

router = APIRouter(tags=["compute-resource-requests"])

_ORCHESTRATION_OPERATION_ID: ContextVar[uuid.UUID | None] = ContextVar(
    "compute_provision_orchestration_operation_id", default=None
)

STANDARD_STORAGE_BYTES = 300 * 1024**3
STANDARD_CONTAINER_PROFILE = CPU_DEVELOPMENT_PROFILE
STANDARD_LEASE_SECONDS = 96 * 60 * 60
RESERVATION_LIFETIME = timedelta(hours=24)
PROTECTED_USERNAMES = {"root", "origin-al", "codexops"}
IMMUTABLE_STAGE_FAILURE_EVIDENCE: dict[str, dict[str, str]] = {
    # Portal-5A-1B Attempt #1 predates structured Stage failure fields.  Keep
    # the database row immutable and expose the independently verified,
    # target-bound safe evidence only when every historical binding matches.
    "2a32b900-4dd9-4962-82fe-127e537ba452": {
        "request_id": "25aafaf9-b4f8-4cb7-beb0-127ed9923d83",
        "plan_id": "4160b0d8-612e-406a-92cc-00c3af9e8356",
        "error_code": "COMPUTE_STAGE_FAILED",
        "rollback_status": "ROLLED_BACK",
        "safe_error_message": (
            "固定 Stage 脚本错误解析双位数位置参数；显式确认 Gate 在任何资源写入前拒绝合法 argv"
        ),
        "side_effect_classification": "NO_SIDE_EFFECT",
        "last_successful_step": "SCRIPT_ARGUMENT_COUNT",
        "first_failed_step": "EXPLICIT_STAGE_CONFIRMATION_GATE",
        "failed_handler": "h100-provision-stage",
    }
}


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _ready_stage_contract(result: Any) -> dict[str, Any] | None:
    """Return only complete, non-secret Stage contract evidence from a successful dry-run."""
    if not isinstance(result, dict):
        return None
    contract = result.get("stage_contract")
    if not isinstance(contract, dict) or contract.get("status") != "PASS":
        return None
    contract_sha256 = contract.get("contract_sha256")
    handler = contract.get("handler")
    argv_contract = contract.get("argv_contract")
    confirmation_gate = contract.get("confirmation_gate")
    image_contract = contract.get("image_contract")
    argument_14 = argv_contract.get("argument_14") if isinstance(argv_contract, dict) else None
    argument_15 = argv_contract.get("argument_15") if isinstance(argv_contract, dict) else None
    if (
        set(contract)
        != {
            "status",
            "contract_sha256",
            "handler",
            "argv_contract",
            "confirmation_gate",
            "image_contract",
        }
        or not isinstance(contract_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", contract_sha256) is None
        or not isinstance(handler, dict)
        or set(handler) != {"identity", "deployed_path", "sha256", "integrity_status"}
        or handler.get("identity") != "h100-provision-stage"
        or handler.get("deployed_path") != "/usr/local/sbin/h100-provision-stage"
        or handler.get("integrity_status") != "PASS"
        or not isinstance(handler.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", handler["sha256"]) is None
        or not isinstance(argv_contract, dict)
        or set(argv_contract)
        != {
            "version",
            "sha256",
            "shape_status",
            "shell_argument_count",
            "expected_shell_argument_count",
            "multi_digit_position_status",
            "argument_14",
            "argument_15",
        }
        or argv_contract.get("version") != "compute-provision-stage-argv-v2"
        or not isinstance(argv_contract.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", argv_contract["sha256"]) is None
        or argv_contract.get("shape_status") != "PASS"
        or argv_contract.get("shell_argument_count") != 15
        or argv_contract.get("expected_shell_argument_count") != 15
        or argv_contract.get("multi_digit_position_status") != "PASS"
        or not isinstance(argument_14, dict)
        or set(argument_14) != {"index", "semantic_role", "binding_status"}
        or argument_14.get("index") != 14
        or argument_14.get("semantic_role") != "EXPLICIT_STAGE_CONFIRMATION_FLAG"
        or argument_14.get("binding_status") != "VALID"
        or not isinstance(argument_15, dict)
        or set(argument_15) != {"index", "semantic_role", "binding_status"}
        or argument_15.get("index") != 15
        or argument_15.get("semantic_role") != "CONFIRMED_TARGET_USERNAME"
        or argument_15.get("binding_status") != "VALID"
        or not isinstance(confirmation_gate, dict)
        or set(confirmation_gate) != {"identity", "validator_version", "validator_sha256", "status"}
        or confirmation_gate.get("identity") != "EXPLICIT_STAGE_CONFIRMATION_GATE"
        or confirmation_gate.get("validator_version")
        != "compute-provision-stage-confirmation-validator-v2"
        or not isinstance(confirmation_gate.get("validator_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", confirmation_gate["validator_sha256"]) is None
        or confirmation_gate.get("status") != "PASS"
        or not isinstance(image_contract, dict)
        or set(image_contract)
        != {
            "status",
            "validator_version",
            "source_type",
            "canonical_local_image_identity",
            "artifact_path",
            "manifest_digest",
            "platform",
            "effective_user",
            "source_reference",
            "source_build_version",
            "approved_deployment_version",
            "failure_code",
        }
        or image_contract.get("status") != "PASS"
        or image_contract.get("validator_version") != "compute-provision-stage-local-image-v2"
        or image_contract.get("source_type") != "LOCAL_OCI_LAYOUT"
        or not isinstance(image_contract.get("canonical_local_image_identity"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_contract["canonical_local_image_identity"])
        is None
        or not isinstance(image_contract.get("artifact_path"), str)
        or re.fullmatch(
            r"/srv/gpu-platform/artifacts/oci/standard-dev-base/[0-9a-f]{64}/layout",
            image_contract["artifact_path"],
        )
        is None
        or not isinstance(image_contract.get("manifest_digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_contract["manifest_digest"]) is None
        or not image_contract["artifact_path"].endswith(
            f"/{image_contract['manifest_digest'].removeprefix('sha256:')}/layout"
        )
        or image_contract.get("platform") != "linux/amd64"
        or image_contract.get("effective_user") != "root"
        or image_contract.get("source_reference")
        != "h100-local/dev-container:ubuntu24.04-origin-pilot-20260804"
        or image_contract.get("source_build_version") != "ubuntu24.04-origin-pilot-20260804"
        or not isinstance(image_contract.get("approved_deployment_version"), str)
        or re.fullmatch(r"[0-9a-f]{40}", image_contract["approved_deployment_version"]) is None
        or image_contract.get("failure_code") is not None
    ):
        return None
    return contract


def _request_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise _error(404, "COMPUTE_REQUEST_NOT_FOUND", "计算资源申请不存在") from exc


def _audit(
    db: Session,
    request: Request,
    context: AuthContext,
    *,
    event_type: str,
    object_type: str,
    object_id: str,
    result: str = "SUCCESS",
    metadata: dict[str, Any] | None = None,
    operation_id: uuid.UUID | None = None,
) -> None:
    parent_operation_id = _ORCHESTRATION_OPERATION_ID.get()
    if parent_operation_id is not None and operation_id != parent_operation_id:
        db.add(
            PortalOperationEvent(
                operation_id=parent_operation_id,
                from_status=OperationStatus.RUNNING,
                to_status=OperationStatus.RUNNING,
                safe_message=f"Internal step {event_type}: {result}"[:1000],
                created_at=utcnow(),
            )
        )
        return
    record_audit(
        db,
        event_type=event_type,
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type=object_type,
        object_id=object_id,
        result=result,
        metadata=metadata,
        operation_id=operation_id,
    )


def _operation(
    db: Session,
    *,
    context: AuthContext,
    operation_type: str,
    target_id: uuid.UUID,
    idempotency_key: str,
    summary: str,
    payload: dict[str, Any],
    status_value: OperationStatus = OperationStatus.SUCCEEDED,
    result_summary: str | None = None,
    risk_level: RiskLevel = RiskLevel.MEDIUM,
) -> PortalOperation:
    now = utcnow()
    parent_operation_id = _ORCHESTRATION_OPERATION_ID.get()
    validated_payload = dict(payload)
    if parent_operation_id is not None:
        validated_payload.update(
            {
                "parent_operation_id": str(parent_operation_id),
                "visibility": "INTERNAL_STEP",
            }
        )
    operation = PortalOperation(
        operation_type=operation_type,
        target_type="compute_resource_request",
        target_id=str(target_id),
        requested_by=context.user.id,
        owner_managed_user_id=None,
        approved_by=context.user.id,
        request_summary=summary,
        validated_payload=validated_payload,
        idempotency_key=f"{operation_type}:{idempotency_key}",
        risk_level=risk_level,
        status=status_value,
        created_at=now,
        approved_at=now,
        started_at=now,
        finished_at=now,
        result_summary=result_summary,
    )
    db.add(operation)
    db.flush()
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=None,
            to_status=status_value,
            safe_message=result_summary or "Fixed compute request operation completed",
            created_at=now,
        )
    )
    return operation


def _plan_view(plan: PortalProvisionPlan | None, *, internal: bool) -> dict[str, Any] | None:
    if plan is None:
        return None
    common: dict[str, Any] = {
        "id": str(plan.id),
        "attempt_number": plan.attempt_number,
        "attempt_reason": plan.attempt_reason,
        "state": plan.state,
        "storage_bytes": plan.storage_bytes,
        "container_profile": plan.container_profile,
        "container_cpus": plan.container_cpus,
        "container_memory_gb": plan.container_memory_gb,
        "container_pids_limit": plan.container_pids_limit,
        "container_gpu": plan.container_gpu,
        "gpu_max": plan.gpu_max,
        "lease_seconds": plan.lease_seconds,
        "lease_state": plan.lease_state,
        "host_ssh": "DISABLED",
        "execution_enabled": plan.execution_enabled,
        "reservation_expires_at": plan.reservation_expires_at,
        "dry_run_at": plan.dry_run_at,
    }
    if internal:
        common.update(
            {
                "username": plan.username,
                "uid": plan.uid,
                "gid": plan.gid,
                "project_id": plan.project_id,
                "container_name": plan.container_name,
                "container_ssh_port": plan.container_ssh_port,
                "slurm_account": plan.slurm_account,
                "slurm_qos": plan.slurm_qos,
                "shell": plan.shell,
                "password_state": plan.password_state,
                "allocator_result": plan.allocator_result,
                "dry_run_result": plan.dry_run_result,
                "previous_plan_id": str(plan.previous_plan_id) if plan.previous_plan_id else None,
                "failed_stage_operation_id": str(plan.failed_stage_operation_id)
                if plan.failed_stage_operation_id
                else None,
                "retry_authorization_operation_id": str(plan.retry_authorization_operation_id)
                if plan.retry_authorization_operation_id
                else None,
            }
        )
    return common


def _safe_operation_view(operation: PortalOperation | None) -> dict[str, Any] | None:
    if operation is None:
        return None
    evidence = operation.dry_run_result if isinstance(operation.dry_run_result, dict) else {}
    legacy = IMMUTABLE_STAGE_FAILURE_EVIDENCE.get(str(operation.id))
    payload = operation.validated_payload if isinstance(operation.validated_payload, dict) else {}
    reconciliation = evidence.get("automatic_reconciliation")
    safe_reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
    if (
        not evidence
        and legacy is not None
        and operation.operation_type == "compute.provision.stage"
        and operation.error_code == legacy["error_code"]
        and operation.rollback_status == legacy["rollback_status"]
        and payload.get("request_id") == legacy["request_id"]
        and payload.get("plan_id") == legacy["plan_id"]
    ):
        evidence = legacy
    return {
        "id": str(operation.id),
        "operation_type": operation.operation_type,
        "status": str(operation.status),
        "started_at": operation.started_at,
        "finished_at": operation.finished_at,
        "error_code": operation.error_code,
        "rollback_status": operation.rollback_status,
        "safe_summary": operation.result_summary,
        "safe_root_cause": evidence.get("safe_error_message") or operation.result_summary,
        "side_effect_classification": evidence.get("side_effect_classification"),
        "last_successful_step": evidence.get("last_successful_step"),
        "first_failed_step": evidence.get("first_failed_step"),
        "failed_handler": evidence.get("failed_handler"),
        "retained_resources": evidence.get("retained_resources"),
        "rollback_steps": evidence.get("rollback_steps"),
        "stage_failure_code": evidence.get("stage_failure_code"),
        "workflow_steps": _workflow_steps(operation),
        "deployment_version": payload.get("deployment_version"),
        "canonical_execution_contract": payload.get("canonical_execution_contract"),
        "canonical_local_image_identity": payload.get("canonical_local_image_identity"),
        "reconciliation_status": safe_reconciliation.get("status"),
        "resource_residue": safe_reconciliation.get("host_residue"),
        "unknown_resource_state": safe_reconciliation.get("unknown_resource_state"),
    }


def _attempt_history(
    db: Session, item: PortalComputeResourceRequest
) -> tuple[list[dict[str, Any]], bool]:
    plans = list(
        db.scalars(
            select(PortalProvisionPlan)
            .where(PortalProvisionPlan.request_id == item.id)
            .order_by(PortalProvisionPlan.attempt_number, PortalProvisionPlan.created_at)
        ).all()
    )
    operations = list(
        db.scalars(
            select(PortalOperation)
            .where(
                PortalOperation.target_type == "compute_resource_request",
                PortalOperation.target_id == str(item.id),
                PortalOperation.operation_type.in_(
                    {
                        "compute.provision.plan",
                        "compute.provision.dry_run",
                        "compute.provision.retry_authorize",
                        "compute.provision.reconcile",
                        "compute.provision.stage",
                        "compute.provision",
                        "compute.provision.retry",
                    }
                ),
            )
            .order_by(PortalOperation.created_at)
        ).all()
    )
    history: list[dict[str, Any]] = []
    retry_available = False
    for plan in plans:
        bound = [
            operation
            for operation in operations
            if isinstance(operation.validated_payload, dict)
            and operation.validated_payload.get("plan_id") == str(plan.id)
        ]
        if len(plans) == 1:
            bound_ids = {operation.id for operation in bound}
            bound.extend(
                operation
                for operation in operations
                if operation.id not in bound_ids
                and operation.operation_type
                in {
                    "compute.provision.plan",
                    "compute.provision.dry_run",
                    "compute.provision.stage",
                }
            )
        bound.sort(key=lambda operation: ensure_utc(operation.created_at))
        stage = next(
            (
                operation
                for operation in reversed(bound)
                if operation.operation_type == "compute.provision.stage"
            ),
            None,
        )
        provision_operation = next(
            (
                operation
                for operation in reversed(bound)
                if operation.operation_type in {"compute.provision", "compute.provision.retry"}
            ),
            None,
        )
        lifecycle_operation = stage or provision_operation
        reservations = list(
            db.scalars(
                select(PortalResourceReservation).where(
                    PortalResourceReservation.plan_id == plan.id
                )
            ).all()
        )
        reservation_states = {
            row.resource_type: {"id": str(row.id), "state": row.state} for row in reservations
        }
        history.append(
            {
                "attempt_number": plan.attempt_number,
                "attempt_reason": plan.attempt_reason,
                "plan": _plan_view(plan, internal=True),
                "operations": [_safe_operation_view(operation) for operation in bound],
                "provision_operation": _safe_operation_view(provision_operation),
                "stage_operation": _safe_operation_view(lifecycle_operation),
                "reservations": reservation_states,
            }
        )
        if (
            item.status == "FAILED"
            and item.provision_plan_id == plan.id
            and plan.state == "FAILED"
            and lifecycle_operation is not None
            and lifecycle_operation.status == OperationStatus.FAILED
            and lifecycle_operation.rollback_status in {"NOT_REQUIRED", "ROLLED_BACK"}
            and len(reservations) == 5
            and all(
                row.state == "RELEASED"
                and row.active_key is None
                and row.consumed_at is None
                and row.released_at is not None
                for row in reservations
            )
        ):
            retry_available = True
    return history, retry_available


def _request_view(
    db: Session, item: PortalComputeResourceRequest, *, internal: bool
) -> dict[str, Any]:
    plan = db.get(PortalProvisionPlan, item.provision_plan_id) if item.provision_plan_id else None
    lifecycle_state = {
        "REQUESTED": "PENDING",
        "UNDER_REVIEW": "PENDING",
        "APPROVED": "PROVISIONING",
        "RETRY_AUTHORIZED": "PROVISIONING",
        "PROVISION_PLAN_READY": "PROVISIONING",
        "KEY_ENROLLMENT_PENDING": "STAGED",
    }.get(item.status, item.status)
    result: dict[str, Any] = {
        "id": str(item.id),
        "portal_account_id": str(item.portal_account_id),
        "username": item.username,
        "status": item.status,
        "lifecycle_state": lifecycle_state,
        "approval_state": "APPROVED" if item.approved_at is not None else "NOT_APPROVED",
        "requested_gpu_max": item.requested_gpu_max,
        "requested_storage_bytes": item.requested_storage_bytes,
        "requested_container_profile": item.requested_container_profile,
        "requested_lease_seconds": item.requested_lease_seconds,
        "purpose": item.purpose,
        "user_note": item.user_note,
        "submitted_at": item.submitted_at,
        "review_note": item.review_note,
        "reviewed_at": item.reviewed_at,
        "approved_at": item.approved_at,
        "rejected_at": item.rejected_at,
        "cancelled_at": item.cancelled_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "plan": _plan_view(plan, internal=internal),
    }
    if item.status == "FAILED":
        result["user_status_message"] = (
            "计算环境创建失败，平台管理员正在处理。你的申请仍被保留，无需重新提交。"
        )
    if internal:
        attempts, retry_available = _attempt_history(db, item)
        account = db.get(PortalUser, item.portal_account_id)
        result.update(
            {
                "requested_by": str(item.requested_by),
                "managed_user_id": str(item.managed_user_id) if item.managed_user_id else None,
                "reviewed_by": str(item.reviewed_by) if item.reviewed_by else None,
                "portal_user": {
                    "login_name": account.login_name if account else item.username,
                    "display_name": account.display_name if account else item.username,
                    "role": highest_role(account) if account else "UNKNOWN",
                    "account_state": str(account.account_state) if account else "UNKNOWN",
                    "password_state": str(account.password_state) if account else "UNKNOWN",
                    "compute_state": str(account.resource_onboarding_state)
                    if account
                    else "UNKNOWN",
                },
                "attempts": attempts,
                "retry_available": retry_available,
                # Compatibility projection for historical clients. New Portal
                # workflows use retry_available and create no authorization step.
                "retry_authorization_available": retry_available,
                "retry_state": (
                    "PROVISIONING"
                    if item.status in {"RETRY_AUTHORIZED", "PROVISION_PLAN_READY", "PROVISIONING"}
                    else "RETRY_ELIGIBLE"
                    if item.status == "FAILED" and retry_available
                    else "MANUAL_REVIEW"
                    if item.status == "FAILED"
                    else "NOT_REQUIRED"
                ),
            }
        )
    return result


def _owned_request(
    db: Session,
    request_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    lock: bool = False,
) -> PortalComputeResourceRequest:
    query = select(PortalComputeResourceRequest).where(
        PortalComputeResourceRequest.id == request_id,
        PortalComputeResourceRequest.portal_account_id == owner_id,
    )
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        # A uniform 404 prevents account/request enumeration.
        raise _error(404, "COMPUTE_REQUEST_NOT_FOUND", "计算资源申请不存在")
    return item


def _admin_request(
    db: Session, request_id: uuid.UUID, *, lock: bool = False
) -> PortalComputeResourceRequest:
    query = select(PortalComputeResourceRequest).where(
        PortalComputeResourceRequest.id == request_id
    )
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        raise _error(404, "COMPUTE_REQUEST_NOT_FOUND", "计算资源申请不存在")
    return item


def _deny_self_administration(item: PortalComputeResourceRequest, context: AuthContext) -> None:
    """Keep human review and allocator actions separate from the applicant."""
    if item.portal_account_id == context.user.id:
        raise _error(
            403,
            "COMPUTE_REQUEST_SELF_ADMINISTRATION_DENIED",
            "申请人不能审批或规划自己的计算资源申请",
        )


def _assert_identity_only(db: Session, account: PortalUser) -> None:
    if account.account_state != AccountState.ACTIVE or account.password_state != PasswordState.SET:
        raise _error(409, "PORTAL_ACCOUNT_NOT_READY", "Portal 账号尚未完成激活")
    managed = db.scalar(
        select(PortalManagedUser.id).where(PortalManagedUser.portal_user_id == account.id)
    )
    if managed is not None or account.resource_onboarding_state != OnboardingState.NOT_ENROLLED:
        raise _error(409, "COMPUTE_ALREADY_PROVISIONED", "已有计算身份不能重复申请首次开户")
    username_owner = db.scalar(
        select(PortalManagedUser.id).where(
            PortalManagedUser.unix_username == account.normalized_login
        )
    )
    if username_owner is not None:
        raise _error(409, "COMPUTE_USERNAME_CONFLICT", "该登录名已被计算身份占用")
    if account.normalized_login in PROTECTED_USERNAMES:
        raise _error(409, "COMPUTE_USERNAME_PROTECTED", "该 Portal 身份不能申请普通计算资源")


def _expire_reservations(db: Session) -> None:
    now = utcnow()
    expired = db.scalars(
        select(PortalResourceReservation)
        .where(
            PortalResourceReservation.state == "RESERVED",
            PortalResourceReservation.expires_at <= now,
        )
        .with_for_update()
    ).all()
    for reservation in expired:
        reservation.state = "RELEASED"
        reservation.active_key = None
        reservation.released_at = now
        plan = db.get(PortalProvisionPlan, reservation.plan_id)
        if plan is not None and plan.state in {"RESERVED", "READY_FOR_PROVISION"}:
            plan.state = "EXPIRED"
            request_item = db.get(PortalComputeResourceRequest, plan.request_id)
            if request_item is not None and request_item.status == "PROVISION_PLAN_READY":
                request_item.status = "APPROVED"
                # Keep the expired attempt as the request's current historical
                # pointer. A later allocator pass creates a new Plan row.


def _allocator_lock(db: Session) -> PortalAllocatorLock:
    row = db.scalar(
        select(PortalAllocatorLock).where(PortalAllocatorLock.id == 1).with_for_update()
    )
    if row is None:
        row = PortalAllocatorLock(id=1, version=1, updated_at=utcnow())
        db.add(row)
        db.flush()
        # The migration seeds this row in production. This branch supports
        # isolated metadata-created test databases.
        row = db.scalar(
            select(PortalAllocatorLock).where(PortalAllocatorLock.id == 1).with_for_update()
        )
    if row is None:  # pragma: no cover - defensive database invariant
        raise _error(503, "ALLOCATOR_LOCK_UNAVAILABLE", "资源分配器锁不可用")
    row.version += 1
    row.updated_at = utcnow()
    return row


def _active_reservations(db: Session) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {
        "UID": set(),
        "GID": set(),
        "PROJECT_ID": set(),
        "SSH_PORT": set(),
        "CONTAINER_NAME": set(),
    }
    rows = db.scalars(
        select(PortalResourceReservation).where(
            PortalResourceReservation.active_key.is_not(None),
        )
    ).all()
    for row in rows:
        result.setdefault(row.resource_type, set()).add(row.resource_value)
    for managed in db.scalars(select(PortalManagedUser)).all():
        result["UID"].add(str(managed.uid))
        result["GID"].add(str(managed.gid))
        if managed.project_id is not None:
            result["PROJECT_ID"].add(str(managed.project_id))
        if managed.container_port is not None:
            result["SSH_PORT"].add(str(managed.container_port))
        if managed.container_name:
            result["CONTAINER_NAME"].add(managed.container_name)
    for container in db.scalars(select(PortalContainer)).all():
        result["CONTAINER_NAME"].add(container.name)
        if container.ssh_port is not None:
            result["SSH_PORT"].add(str(container.ssh_port))
    return result


def _worker_dry_run(
    operation_type: str,
    *,
    payload: dict[str, Any],
    context: AuthContext,
    idempotency_key: str,
) -> dict[str, Any]:
    try:
        result = call_worker(
            operation_type,
            payload=payload,
            requested_by=context.user.normalized_login,
            approved_by=context.user.normalized_login,
            idempotency_key=idempotency_key,
            dry_run=True,
            timeout_seconds=90,
        )
    except WorkerClientError as exc:
        raise _error(503, exc.code, str(exc)) from exc
    if result.get("status") != "DRY_RUN":
        raw_error = result.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        raise _error(
            409,
            str(error.get("code", "PROVISION_DRY_RUN_FAILED")),
            str(error.get("message", "资源规划 dry-run 未通过")),
        )
    return result


def _rolled_back_stage_retry(
    db: Session, item: PortalComputeResourceRequest
) -> tuple[PortalProvisionPlan, list[PortalResourceReservation], PortalOperation]:
    """Lock and validate a failed Stage before separate retry authorization.

    This function never changes the failed Plan, Operation, or Reservations.
    """
    if item.provision_plan_id is None or item.managed_user_id is not None:
        raise _error(
            409,
            "PROVISION_STAGE_RETRY_RECONCILIATION_REQUIRED",
            "失败的 Stage 尚未满足重新规划条件",
        )
    plan = db.scalar(
        select(PortalProvisionPlan)
        .where(PortalProvisionPlan.id == item.provision_plan_id)
        .with_for_update()
    )
    operation = db.scalar(
        select(PortalOperation)
        .where(
            PortalOperation.operation_type == "compute.provision.stage",
            PortalOperation.target_id == str(item.id),
        )
        .order_by(PortalOperation.created_at.desc())
        .with_for_update()
    )
    reservations = list(
        db.scalars(
            select(PortalResourceReservation)
            .where(PortalResourceReservation.plan_id == item.provision_plan_id)
            .with_for_update()
        ).all()
    )
    expected_values = (
        {
            ("UID", str(plan.uid)),
            ("GID", str(plan.gid)),
            ("PROJECT_ID", str(plan.project_id)),
            ("SSH_PORT", str(plan.container_ssh_port)),
            ("CONTAINER_NAME", plan.container_name),
        }
        if plan is not None
        else set()
    )
    reservation_ids = {row.resource_type: str(row.id) for row in reservations}
    payload = operation.validated_payload if operation is not None else {}
    eligible = (
        item.status in {"FAILED", "RETRY_AUTHORIZED"}
        and (
            (item.status == "FAILED" and item.active_slot is None)
            or (item.status == "RETRY_AUTHORIZED" and item.active_slot == 1)
        )
        and plan is not None
        and plan.request_id == item.id
        and plan.portal_account_id == item.portal_account_id
        and plan.state == "FAILED"
        and plan.execution_enabled
        and operation is not None
        and operation.status == OperationStatus.FAILED
        and operation.rollback_status in {"NOT_REQUIRED", "ROLLED_BACK"}
        and payload.get("request_id") == str(item.id)
        and payload.get("plan_id") == str(plan.id)
        and payload.get("reservation_ids") == reservation_ids
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
    if not eligible or plan is None or operation is None:
        raise _error(
            409,
            "PROVISION_STAGE_RETRY_RECONCILIATION_REQUIRED",
            "失败的 Stage 需要人工对账，不能重新规划",
        )
    return plan, reservations, operation


def _retryable_failed_attempt(
    db: Session, item: PortalComputeResourceRequest
) -> tuple[PortalProvisionPlan, list[PortalResourceReservation], PortalOperation]:
    """Return one immutable failed attempt whose rollback truth is already known."""
    if item.provision_plan_id is None or item.managed_user_id is not None:
        raise _error(
            409,
            "PROVISION_RETRY_NOT_ELIGIBLE",
            "失败的 Provision Attempt 尚未满足安全重试条件",
        )
    plan = db.scalar(
        select(PortalProvisionPlan)
        .where(PortalProvisionPlan.id == item.provision_plan_id)
        .with_for_update()
    )
    reservations = list(
        db.scalars(
            select(PortalResourceReservation)
            .where(PortalResourceReservation.plan_id == item.provision_plan_id)
            .with_for_update()
        ).all()
    )
    operations = list(
        db.scalars(
            select(PortalOperation)
            .where(
                PortalOperation.target_type == "compute_resource_request",
                PortalOperation.target_id == str(item.id),
                PortalOperation.operation_type.in_(
                    {
                        "compute.provision.stage",
                        "compute.provision",
                        "compute.provision.retry",
                    }
                ),
                PortalOperation.status == OperationStatus.FAILED,
            )
            .order_by(PortalOperation.created_at.desc())
            .with_for_update()
        ).all()
    )
    failure = next(
        (
            operation
            for operation in operations
            if isinstance(operation.validated_payload, dict)
            and operation.validated_payload.get("plan_id") == str(item.provision_plan_id)
        ),
        None,
    )
    expected_values = (
        {
            ("UID", str(plan.uid)),
            ("GID", str(plan.gid)),
            ("PROJECT_ID", str(plan.project_id)),
            ("SSH_PORT", str(plan.container_ssh_port)),
            ("CONTAINER_NAME", plan.container_name),
        }
        if plan is not None
        else set()
    )
    eligible = bool(
        item.status in {"FAILED", "RETRY_AUTHORIZED"}
        and plan is not None
        and plan.request_id == item.id
        and plan.portal_account_id == item.portal_account_id
        and plan.state == "FAILED"
        and failure is not None
        and failure.rollback_status in {"NOT_REQUIRED", "ROLLED_BACK"}
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
    if not eligible or plan is None or failure is None:
        raise _error(
            409,
            "UNKNOWN_RESOURCE_STATE",
            "Rollback 尚未被权威验证；FAILED_HOLD 保持不变",
        )
    return plan, reservations, failure


def _retry_verification_payload(
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


def _failed_stage_reconciliation_source(
    db: Session,
    item: PortalComputeResourceRequest,
    *,
    plan_id: uuid.UUID,
    failed_stage_operation_id: uuid.UUID,
    lock: bool,
) -> tuple[PortalProvisionPlan, list[PortalResourceReservation], PortalOperation]:
    """Validate the exact manual-review Stage and its five allocator holds."""
    if item.provision_plan_id != plan_id or item.managed_user_id is not None:
        raise _error(
            409,
            "PROVISION_RECONCILIATION_BINDING_FAILED",
            "人工对账未绑定当前失败 Attempt",
        )
    plan_query = select(PortalProvisionPlan).where(PortalProvisionPlan.id == plan_id)
    stage_query = select(PortalOperation).where(PortalOperation.id == failed_stage_operation_id)
    reservation_query = select(PortalResourceReservation).where(
        PortalResourceReservation.plan_id == plan_id
    )
    if lock:
        plan_query = plan_query.with_for_update()
        stage_query = stage_query.with_for_update()
        reservation_query = reservation_query.with_for_update()
    plan = db.scalar(plan_query)
    failed_stage = db.scalar(stage_query)
    reservations = list(db.scalars(reservation_query).all())
    expected_values = (
        {
            ("UID", str(plan.uid)),
            ("GID", str(plan.gid)),
            ("PROJECT_ID", str(plan.project_id)),
            ("SSH_PORT", str(plan.container_ssh_port)),
            ("CONTAINER_NAME", plan.container_name),
        }
        if plan is not None
        else set()
    )
    reservation_ids = {row.resource_type: str(row.id) for row in reservations}
    stage_payload = failed_stage.validated_payload if failed_stage is not None else {}
    stage_result = failed_stage.dry_run_result if failed_stage is not None else {}
    eligible = (
        item.status == "FAILED"
        and item.active_slot is None
        and plan is not None
        and plan.request_id == item.id
        and plan.portal_account_id == item.portal_account_id
        and plan.state == "FAILED"
        and plan.execution_enabled
        and failed_stage is not None
        and failed_stage.operation_type == "compute.provision.stage"
        and failed_stage.target_id == str(item.id)
        and failed_stage.status == OperationStatus.FAILED
        and failed_stage.rollback_status == "REQUIRES_MANUAL_REVIEW"
        and isinstance(stage_result, dict)
        and stage_result.get("side_effect_classification")
        in {"PARTIAL_UNKNOWN", "PARTIAL_ROLLBACK_FAILED"}
        and stage_payload.get("request_id") == str(item.id)
        and stage_payload.get("plan_id") == str(plan.id)
        and stage_payload.get("reservation_ids") == reservation_ids
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
    if not eligible or plan is None or failed_stage is None:
        raise _error(
            409,
            "PROVISION_RECONCILIATION_NOT_ELIGIBLE",
            "失败 Attempt 不满足人工 rollback 对账条件",
        )
    return plan, reservations, failed_stage


def _successful_reconciliation(
    db: Session,
    item: PortalComputeResourceRequest,
    plan: PortalProvisionPlan,
    failed_stage: PortalOperation,
) -> PortalOperation | None:
    reconciliation = db.scalar(
        select(PortalOperation)
        .where(
            PortalOperation.operation_type == "compute.provision.reconcile",
            PortalOperation.target_id == str(item.id),
            PortalOperation.status == OperationStatus.SUCCEEDED,
        )
        .order_by(PortalOperation.created_at.desc())
    )
    payload = reconciliation.validated_payload if reconciliation is not None else {}
    if (
        reconciliation is None
        or payload.get("request_id") != str(item.id)
        or payload.get("plan_id") != str(plan.id)
        or payload.get("failed_stage_operation_id") != str(failed_stage.id)
        or payload.get("rollback_verified") is not True
        or payload.get("verified_classification") != "PARTIAL_ROLLED_BACK"
        or payload.get("reservation_state") != "RELEASED"
    ):
        return None
    return reconciliation


def _authorized_retry_source(
    db: Session, item: PortalComputeResourceRequest
) -> tuple[
    PortalProvisionPlan,
    list[PortalResourceReservation],
    PortalOperation,
    PortalOperation,
]:
    if item.status != "RETRY_AUTHORIZED":
        raise _error(
            409,
            "PROVISION_RETRY_NOT_AUTHORIZED",
            "失败的 Provision Attempt 尚未获得独立重试授权",
        )
    failed_plan, released, failed_stage = _rolled_back_stage_retry(db, item)
    authorization = db.scalar(
        select(PortalOperation)
        .where(
            PortalOperation.operation_type == "compute.provision.retry_authorize",
            PortalOperation.target_id == str(item.id),
            PortalOperation.status == OperationStatus.SUCCEEDED,
        )
        .order_by(PortalOperation.created_at.desc())
        .with_for_update()
    )
    payload = authorization.validated_payload if authorization is not None else {}
    if (
        authorization is None
        or payload.get("plan_id") != str(failed_plan.id)
        or payload.get("failed_stage_operation_id") != str(failed_stage.id)
        or payload.get("rollback_verified") is not True
        or payload.get("root_cause_remediated") is not True
    ):
        raise _error(
            409,
            "PROVISION_RETRY_AUTHORIZATION_INVALID",
            "Provision 重试授权与失败 Attempt 绑定不完整",
        )
    return failed_plan, released, failed_stage, authorization


@router.get("/self/compute-request")
def self_compute_request(
    context: AuthContext = Depends(permission_dependency("self.compute_requests.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = db.scalar(
        select(PortalComputeResourceRequest)
        .where(PortalComputeResourceRequest.portal_account_id == context.user.id)
        .order_by(PortalComputeResourceRequest.created_at.desc())
    )
    return {
        "status": "OK",
        "compute_identity": "NOT_PROVISIONED",
        "request": _request_view(db, item, internal=False) if item else None,
    }


@router.post("/self/compute-request", status_code=status.HTTP_201_CREATED)
def create_self_compute_request(
    body: ComputeResourceRequestCreate,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.compute_requests.create")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    source_ip = client_ip(request)
    if not rate_limiter.allowed(
        f"compute-request-ip:{source_ip}", 30, 300
    ) or not rate_limiter.allowed(f"compute-request-account:{context.user.id}", 10, 300):
        raise _error(429, "COMPUTE_REQUEST_RATE_LIMITED", "计算资源申请提交过于频繁，请稍后重试")
    account = db.scalar(
        select(PortalUser).where(PortalUser.id == context.user.id).with_for_update()
    )
    if account is None:  # pragma: no cover - authenticated FK invariant
        raise _error(404, "PORTAL_ACCOUNT_NOT_FOUND", "Portal 账号不存在")
    _assert_identity_only(db, account)
    existing = db.scalar(
        select(PortalComputeResourceRequest).where(
            PortalComputeResourceRequest.portal_account_id == account.id,
            PortalComputeResourceRequest.active_slot == 1,
        )
    )
    if existing is not None:
        raise _error(409, "ACTIVE_COMPUTE_REQUEST_EXISTS", "已有进行中的计算资源申请")
    retained_approval = db.scalar(
        select(PortalComputeResourceRequest.id).where(
            PortalComputeResourceRequest.portal_account_id == account.id,
            PortalComputeResourceRequest.status == "FAILED",
            PortalComputeResourceRequest.approved_at.is_not(None),
        )
    )
    if retained_approval is not None:
        raise _error(
            409,
            "APPROVED_COMPUTE_REQUEST_RETRY_PENDING",
            "原申请审批仍然有效，平台管理员正在处理，无需重新提交",
        )
    now = utcnow()
    item = PortalComputeResourceRequest(
        portal_account_id=account.id,
        requested_by=account.id,
        managed_user_id=None,
        username=account.normalized_login,
        status="REQUESTED",
        active_slot=1,
        requested_gpu_max=body.requested_gpu_max,
        requested_storage_bytes=body.requested_storage_bytes,
        requested_container_profile=body.requested_container_profile,
        requested_lease_seconds=body.requested_lease_seconds,
        purpose=body.purpose,
        user_note=body.user_note,
        submitted_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    try:
        db.flush()
        operation = _operation(
            db,
            context=context,
            operation_type="compute_resource_request.create",
            target_id=item.id,
            idempotency_key=str(body.idempotency_key),
            summary="Ordinary user submitted own standard compute resource request",
            payload={
                "portal_account_id": str(account.id),
                "gpu_max": body.requested_gpu_max,
                "storage_bytes": STANDARD_STORAGE_BYTES,
                "container_profile": item.requested_container_profile,
                "lease_seconds": STANDARD_LEASE_SECONDS,
            },
            result_summary="Compute request entered REQUESTED; no infrastructure was created",
        )
        _audit(
            db,
            request,
            context,
            event_type="COMPUTE_RESOURCE_REQUEST_CREATED",
            object_type="compute_resource_request",
            object_id=str(item.id),
            metadata={
                "gpu_max": item.requested_gpu_max,
                "storage_bytes": item.requested_storage_bytes,
                "container_profile": item.requested_container_profile,
                "lease_seconds": item.requested_lease_seconds,
            },
            operation_id=operation.id,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error(409, "ACTIVE_COMPUTE_REQUEST_EXISTS", "已有进行中的计算资源申请") from exc
    return {"status": "REQUESTED", "request": _request_view(db, item, internal=False)}


@router.post("/self/compute-request/{request_id}/cancel")
def cancel_self_compute_request(
    request_id: str,
    body: ComputeResourceRequestCancel,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.compute_requests.cancel")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    item = _owned_request(db, _request_id(request_id), context.user.id, lock=True)
    if item.status != "REQUESTED":
        raise _error(409, "COMPUTE_REQUEST_NOT_CANCELLABLE", "只有待审批申请可以撤回")
    item.status = "CANCELLED"
    item.active_slot = None
    item.cancelled_at = utcnow()
    item.updated_at = item.cancelled_at
    operation = _operation(
        db,
        context=context,
        operation_type="compute_resource_request.cancel",
        target_id=item.id,
        idempotency_key=str(body.idempotency_key),
        summary="Ordinary user cancelled own pending compute request",
        payload={"portal_account_id": str(item.portal_account_id), "expected_state": "REQUESTED"},
        result_summary="Compute request cancelled; no infrastructure existed",
    )
    _audit(
        db,
        request,
        context,
        event_type="COMPUTE_RESOURCE_REQUEST_CANCELLED",
        object_type="compute_resource_request",
        object_id=str(item.id),
        operation_id=operation.id,
    )
    db.commit()
    return {"status": "CANCELLED", "request": _request_view(db, item, internal=False)}


@router.get("/admin/compute-resource-requests")
def admin_compute_requests(
    context: AuthContext = Depends(permission_dependency("compute_requests.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.scalars(
        select(PortalComputeResourceRequest).order_by(
            PortalComputeResourceRequest.submitted_at.desc(),
            PortalComputeResourceRequest.created_at.desc(),
        )
    ).all()
    return {
        "status": "OK",
        "requests": [_request_view(db, item, internal=True) for item in rows],
        "count": len(rows),
    }


@router.get("/admin/compute-resource-requests/{request_id}")
def admin_compute_request_detail(
    request_id: str,
    context: AuthContext = Depends(permission_dependency("compute_requests.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = _admin_request(db, _request_id(request_id))
    return {"status": "OK", "request": _request_view(db, item, internal=True)}


@router.post("/admin/compute-resource-requests/{request_id}/review", deprecated=True)
def review_compute_request(
    request_id: str,
    body: ComputeResourceReviewRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.review")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    item = _admin_request(db, _request_id(request_id), lock=True)
    _deny_self_administration(item, context)
    if item.status not in {"REQUESTED", "UNDER_REVIEW"}:
        raise _error(409, "COMPUTE_REQUEST_ALREADY_REVIEWED", "申请当前状态不能再次审批")
    account = db.get(PortalUser, item.portal_account_id)
    if account is None:
        raise _error(409, "PORTAL_ACCOUNT_NOT_FOUND", "申请所属 Portal 账号不存在")
    _assert_identity_only(db, account)
    now = utcnow()
    item.reviewed_at = now
    item.reviewed_by = context.user.id
    item.review_note = body.review_note
    item.updated_at = now
    approved = body.decision == "APPROVE"
    if approved:
        item.status = "APPROVED"
        item.approved_at = now
        event_type = "COMPUTE_RESOURCE_REQUEST_APPROVED"
    else:
        item.status = "REJECTED"
        item.active_slot = None
        item.rejected_at = now
        event_type = "COMPUTE_RESOURCE_REQUEST_REJECTED"
    operation_type = f"compute_resource_request.{body.decision.casefold()}"
    operation = _operation(
        db,
        context=context,
        operation_type=operation_type,
        target_id=item.id,
        idempotency_key=str(body.idempotency_key),
        summary=f"Administrator {body.decision.casefold()}d compute resource request",
        payload={
            "portal_account_id": str(item.portal_account_id),
            "decision": body.decision,
            "gpu_max": item.requested_gpu_max,
            "storage_bytes": item.requested_storage_bytes,
            "container_profile": item.requested_container_profile,
            "lease_seconds": item.requested_lease_seconds,
        },
        result_summary=(
            "Request approved; no provisioning or lease activation performed"
            if approved
            else "Request rejected; no infrastructure existed"
        ),
    )
    _audit(
        db,
        request,
        context,
        event_type=event_type,
        object_type="compute_resource_request",
        object_id=str(item.id),
        metadata={"decision": body.decision, "gpu_max": item.requested_gpu_max},
        operation_id=operation.id,
    )
    db.commit()
    return {"status": item.status, "request": _request_view(db, item, internal=True)}


def _workflow_steps(operation: PortalOperation) -> dict[str, str]:
    result = operation.dry_run_result if isinstance(operation.dry_run_result, dict) else {}
    raw = result.get("workflow_steps")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if str(key) in {"PREPARING", "VALIDATING", "CREATING_ENVIRONMENT", "FINALIZING"}
        and str(value) in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED"}
    }


def _set_workflow_step(
    db: Session,
    operation_id: uuid.UUID,
    step: str,
    state: str,
) -> None:
    operation = db.get(PortalOperation, operation_id)
    if operation is None or operation.status != OperationStatus.RUNNING:
        raise _error(
            409,
            "PROVISION_ORCHESTRATION_STATE_INVALID",
            "顶层 Provision Operation 状态不完整",
        )
    steps = _workflow_steps(operation)
    steps[step] = state
    result = operation.dry_run_result if isinstance(operation.dry_run_result, dict) else {}
    operation.dry_run_result = {**result, "workflow_steps": steps}
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=OperationStatus.RUNNING,
            to_status=OperationStatus.RUNNING,
            safe_message=f"{step}: {state}",
            created_at=utcnow(),
        )
    )
    db.commit()


def _active_orchestration(
    db: Session, item: PortalComputeResourceRequest
) -> PortalOperation | None:
    return db.scalar(
        select(PortalOperation)
        .where(
            PortalOperation.target_type == "compute_resource_request",
            PortalOperation.target_id == str(item.id),
            PortalOperation.operation_type.in_({"compute.provision", "compute.provision.retry"}),
            PortalOperation.status == OperationStatus.RUNNING,
        )
        .order_by(PortalOperation.created_at.desc())
    )


def _orchestration_by_key(
    db: Session,
    *,
    item: PortalComputeResourceRequest,
    context: AuthContext,
    operation_type: str,
    idempotency_key: uuid.UUID,
) -> PortalOperation | None:
    return db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.user.id,
            PortalOperation.target_id == str(item.id),
            PortalOperation.operation_type == operation_type,
            PortalOperation.idempotency_key == f"{operation_type}:{idempotency_key}",
        )
    )


def _orchestration_response(
    db: Session,
    item: PortalComputeResourceRequest,
    operation: PortalOperation,
    *,
    idempotent_replay: bool,
) -> dict[str, Any]:
    public_status = (
        "PROVISIONING"
        if operation.status == OperationStatus.RUNNING
        else "KEY_ENROLLMENT_PENDING"
        if operation.status == OperationStatus.SUCCEEDED
        else "FAILED"
    )
    return {
        "status": public_status,
        "operation_id": str(operation.id),
        "operation": _safe_operation_view(operation),
        "idempotent_replay": idempotent_replay,
        "request": _request_view(db, item, internal=True),
    }


def _new_orchestration_operation(
    db: Session,
    *,
    item: PortalComputeResourceRequest,
    context: AuthContext,
    operation_type: str,
    idempotency_key: uuid.UUID,
    payload: dict[str, Any],
    summary: str,
) -> PortalOperation:
    now = utcnow()
    operation = PortalOperation(
        operation_type=operation_type,
        target_type="compute_resource_request",
        target_id=str(item.id),
        requested_by=context.user.id,
        owner_managed_user_id=None,
        approved_by=context.user.id,
        request_summary=summary,
        validated_payload={
            "request_id": str(item.id),
            "portal_account_id": str(item.portal_account_id),
            "deployment_version": deployment_version(),
            **payload,
        },
        idempotency_key=f"{operation_type}:{idempotency_key}",
        risk_level=RiskLevel.CRITICAL,
        status=OperationStatus.RUNNING,
        created_at=now,
        approved_at=now,
        started_at=now,
        rollback_status="NOT_REQUIRED",
        dry_run_result={
            "workflow_steps": {
                "PREPARING": "PENDING",
                "VALIDATING": "PENDING",
                "CREATING_ENVIRONMENT": "PENDING",
                "FINALIZING": "PENDING",
            }
        },
    )
    db.add(operation)
    db.flush()
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=None,
            to_status=OperationStatus.RUNNING,
            safe_message="Provision orchestration accepted; internal safety steps started",
            created_at=now,
        )
    )
    return operation


def _attempt_failure_operation(
    db: Session,
    item: PortalComputeResourceRequest,
    plan: PortalProvisionPlan | None,
) -> PortalOperation | None:
    if plan is None:
        return None
    operations = db.scalars(
        select(PortalOperation)
        .where(
            PortalOperation.target_type == "compute_resource_request",
            PortalOperation.target_id == str(item.id),
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


def _finalize_orchestration_failure(
    db: Session,
    request: Request,
    context: AuthContext,
    *,
    request_id: uuid.UUID,
    operation_id: uuid.UUID,
    error_code: str,
    safe_error: str,
) -> dict[str, Any]:
    db.rollback()
    item = _admin_request(db, request_id, lock=True)
    operation = db.get(PortalOperation, operation_id)
    if operation is None:
        raise _error(409, "PROVISION_ORCHESTRATION_STATE_INVALID", "顶层 Operation 不存在")
    if operation.status != OperationStatus.RUNNING:
        return _orchestration_response(db, item, operation, idempotent_replay=True)
    plan = db.get(PortalProvisionPlan, item.provision_plan_id) if item.provision_plan_id else None
    failure = _attempt_failure_operation(db, item, plan)
    now = utcnow()
    if (
        failure is None
        and plan is not None
        and plan.state
        in {
            "RESERVED",
            "READY_FOR_PROVISION",
        }
    ):
        reservations = list(
            db.scalars(
                select(PortalResourceReservation)
                .where(PortalResourceReservation.plan_id == plan.id)
                .with_for_update()
            ).all()
        )
        for reservation in reservations:
            if reservation.state == "RESERVED":
                reservation.state = "RELEASED"
                reservation.active_key = None
                reservation.consumed_at = None
                reservation.released_at = now
        plan.state = "FAILED"
        plan.execution_enabled = False
        plan.updated_at = now
    if item.status not in {"FAILED", "KEY_ENROLLMENT_PENDING", "STAGED"}:
        item.status = "FAILED"
        item.active_slot = None
        item.updated_at = now
    operation.status = OperationStatus.FAILED
    operation.finished_at = now
    operation.error_code = error_code[:64]
    operation.rollback_status = failure.rollback_status if failure is not None else "NOT_REQUIRED"
    operation.result_summary = safe_error[:1000]
    result = operation.dry_run_result if isinstance(operation.dry_run_result, dict) else {}
    steps = _workflow_steps(operation)
    running_step = next((name for name, state in steps.items() if state == "RUNNING"), None)
    if running_step is not None:
        steps[running_step] = "FAILED"
    failure_evidence = (
        failure.dry_run_result
        if failure is not None and isinstance(failure.dry_run_result, dict)
        else {}
    )
    operation.dry_run_result = {
        **result,
        "workflow_steps": steps,
        "side_effect_classification": failure_evidence.get(
            "side_effect_classification", "NO_SIDE_EFFECT"
        ),
        "rollback_status": operation.rollback_status,
        "first_failed_step": failure_evidence.get("first_failed_step", running_step),
        "last_successful_step": failure_evidence.get("last_successful_step"),
        "safe_error_message": safe_error[:500],
        "automatic_reconciliation": failure_evidence.get("automatic_reconciliation"),
    }
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=OperationStatus.RUNNING,
            to_status=OperationStatus.FAILED,
            safe_message=(
                "Provision failed; automatic rollback convergence completed or failed closed"
            ),
            created_at=now,
        )
    )
    _audit(
        db,
        request,
        context,
        event_type="COMPUTE_PROVISION_FAILED",
        object_type="compute_resource_request",
        object_id=str(item.id),
        result="FAILED",
        metadata={
            "attempt_number": plan.attempt_number if plan is not None else None,
            "error_code": operation.error_code,
            "rollback_result": (
                "VERIFIED"
                if operation.rollback_status in {"NOT_REQUIRED", "ROLLED_BACK"}
                else "MANUAL_REVIEW"
            ),
            "deployment_version": operation.validated_payload.get("deployment_version"),
        },
        operation_id=operation.id,
    )
    db.commit()
    return _orchestration_response(db, item, operation, idempotent_replay=False)


def _finalize_orchestration_success(
    db: Session,
    request: Request,
    context: AuthContext,
    *,
    request_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> dict[str, Any]:
    item = _admin_request(db, request_id, lock=True)
    operation = db.get(PortalOperation, operation_id)
    plan = db.get(PortalProvisionPlan, item.provision_plan_id) if item.provision_plan_id else None
    if (
        operation is None
        or operation.status != OperationStatus.RUNNING
        or plan is None
        or plan.state != "STAGED"
        or item.status != "KEY_ENROLLMENT_PENDING"
        or item.managed_user_id is None
    ):
        raise _error(
            409,
            "PROVISION_ORCHESTRATION_POSTCONDITION_FAILED",
            "Provision 成功结果与 Portal 状态不一致",
        )
    now = utcnow()
    steps = _workflow_steps(operation)
    steps["FINALIZING"] = "SUCCEEDED"
    operation.owner_managed_user_id = item.managed_user_id
    operation.status = OperationStatus.SUCCEEDED
    operation.finished_at = now
    operation.rollback_status = "NOT_REQUIRED"
    operation.result_summary = (
        "Compute environment STAGED; container key enrollment and Lease activation remain pending"
    )
    result = operation.dry_run_result if isinstance(operation.dry_run_result, dict) else {}
    operation.dry_run_result = {
        **result,
        "workflow_steps": steps,
        "environment_checks": "PASS",
        "image": "PASS",
        "storage": "PASS",
        "slurm": "PASS",
        "gpu_policy": "PASS",
    }
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=OperationStatus.RUNNING,
            to_status=OperationStatus.SUCCEEDED,
            safe_message="Provision orchestration completed at STAGED",
            created_at=now,
        )
    )
    _audit(
        db,
        request,
        context,
        event_type="COMPUTE_PROVISION_COMPLETED",
        object_type="compute_resource_request",
        object_id=str(item.id),
        metadata={
            "attempt_number": plan.attempt_number,
            "result": "STAGED",
            "deployment_version": operation.validated_payload.get("deployment_version"),
            "rollback_result": "NOT_REQUIRED",
        },
        operation_id=operation.id,
    )
    db.commit()
    return _orchestration_response(db, item, operation, idempotent_replay=False)


def _run_provision_orchestration(
    db: Session,
    request: Request,
    context: AuthContext,
    *,
    request_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> dict[str, Any]:
    current_item = _admin_request(db, request_id)
    current_operation = db.get(PortalOperation, operation_id)
    if current_operation is None:
        raise _error(409, "PROVISION_ORCHESTRATION_STATE_INVALID", "顶层 Operation 不存在")
    if current_item.status in {"KEY_ENROLLMENT_PENDING", "STAGED"}:
        return _finalize_orchestration_success(
            db,
            request,
            context,
            request_id=request_id,
            operation_id=operation_id,
        )
    if current_item.status == "FAILED":
        return _finalize_orchestration_failure(
            db,
            request,
            context,
            request_id=request_id,
            operation_id=operation_id,
            error_code="PROVISION_FAILED",
            safe_error="Provision failed safely",
        )
    if current_item.status == "PROVISIONING":
        return _orchestration_response(db, current_item, current_operation, idempotent_replay=True)
    token = _ORCHESTRATION_OPERATION_ID.set(operation_id)
    try:
        _set_workflow_step(db, operation_id, "PREPARING", "RUNNING")
        create_provision_plan(
            str(request_id),
            ComputeProvisionActionRequest(
                idempotency_key=uuid.uuid5(operation_id, "allocate-and-reserve")
            ),
            request,
            context,
            db,
        )
        _set_workflow_step(db, operation_id, "PREPARING", "SUCCEEDED")
        _set_workflow_step(db, operation_id, "VALIDATING", "RUNNING")
        dry_run_provision_plan(
            str(request_id),
            ComputeProvisionActionRequest(
                idempotency_key=uuid.uuid5(operation_id, "automatic-dry-run")
            ),
            request,
            context,
            db,
        )
        _set_workflow_step(db, operation_id, "VALIDATING", "SUCCEEDED")
        _set_workflow_step(db, operation_id, "CREATING_ENVIRONMENT", "RUNNING")
        provision_reserved_compute_environment(
            str(request_id),
            ComputeProvisionActionRequest(
                idempotency_key=uuid.uuid5(operation_id, "automatic-stage")
            ),
            request,
            context,
            db,
        )
        _set_workflow_step(db, operation_id, "CREATING_ENVIRONMENT", "SUCCEEDED")
        _set_workflow_step(db, operation_id, "FINALIZING", "RUNNING")
        return _finalize_orchestration_success(
            db,
            request,
            context,
            request_id=request_id,
            operation_id=operation_id,
        )
    except HTTPException as exc:
        detail = cast(dict[str, Any], exc.detail)
        return _finalize_orchestration_failure(
            db,
            request,
            context,
            request_id=request_id,
            operation_id=operation_id,
            error_code=str(detail.get("code", "PROVISION_FAILED")),
            safe_error=str(detail.get("message", "Provision failed safely")),
        )
    except Exception:
        return _finalize_orchestration_failure(
            db,
            request,
            context,
            request_id=request_id,
            operation_id=operation_id,
            error_code="PROVISION_ORCHESTRATION_FAILED",
            safe_error="Provision orchestration failed closed",
        )
    finally:
        _ORCHESTRATION_OPERATION_ID.reset(token)


@router.post("/admin/compute-resource-requests/{request_id}/approve-and-provision")
def approve_and_provision(
    request_id: str,
    body: ComputeProvisionApprovalRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.review")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """One Approval action starts the complete fixed Provision orchestration."""
    require_session_csrf(request, context)
    parsed_request_id = _request_id(request_id)
    item = _admin_request(db, parsed_request_id, lock=True)
    _deny_self_administration(item, context)
    existing = _orchestration_by_key(
        db,
        item=item,
        context=context,
        operation_type="compute.provision",
        idempotency_key=body.idempotency_key,
    )
    if existing is not None:
        if existing.status == OperationStatus.RUNNING:
            db.commit()
            return _run_provision_orchestration(
                db,
                request,
                context,
                request_id=parsed_request_id,
                operation_id=existing.id,
            )
        return _orchestration_response(db, item, existing, idempotent_replay=True)
    running = _active_orchestration(db, item)
    if running is not None:
        return _orchestration_response(db, item, running, idempotent_replay=True)
    require_recent_reauthentication(context)
    if item.status not in {"REQUESTED", "UNDER_REVIEW"}:
        raise _error(409, "COMPUTE_REQUEST_ALREADY_REVIEWED", "申请当前状态不能再次审批")
    account = db.get(PortalUser, item.portal_account_id)
    if account is None:
        raise _error(409, "PORTAL_ACCOUNT_NOT_FOUND", "申请所属 Portal 账号不存在")
    _assert_identity_only(db, account)
    now = utcnow()
    item.reviewed_at = now
    item.reviewed_by = context.user.id
    item.review_note = body.review_note
    item.approved_at = now
    item.status = "APPROVED"
    item.active_slot = 1
    item.updated_at = now
    operation = _new_orchestration_operation(
        db,
        item=item,
        context=context,
        operation_type="compute.provision",
        idempotency_key=body.idempotency_key,
        payload={
            "action": "APPROVE_AND_PROVISION",
            "approval_repeated": False,
            "review_note": body.review_note,
        },
        summary="Administrator approved request and started fixed Provision orchestration",
    )
    _audit(
        db,
        request,
        context,
        event_type="COMPUTE_PROVISION_STARTED",
        object_type="compute_resource_request",
        object_id=str(item.id),
        metadata={"action": "APPROVE_AND_PROVISION", "approval_count": 1},
        operation_id=operation.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error(
            409,
            "PROVISION_ORCHESTRATION_CONFLICT",
            "Provision 已由另一个管理员动作启动",
        ) from exc
    return _run_provision_orchestration(
        db,
        request,
        context,
        request_id=parsed_request_id,
        operation_id=operation.id,
    )


@router.post("/admin/compute-resource-requests/{request_id}/retry")
def retry_provision(
    request_id: str,
    body: ComputeProvisionRetryRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.plan")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """One recently authenticated action runs a complete fresh retry attempt."""
    require_session_csrf(request, context)
    parsed_request_id = _request_id(request_id)
    item = _admin_request(db, parsed_request_id, lock=True)
    _deny_self_administration(item, context)
    existing = _orchestration_by_key(
        db,
        item=item,
        context=context,
        operation_type="compute.provision.retry",
        idempotency_key=body.idempotency_key,
    )
    if existing is not None:
        if existing.status == OperationStatus.RUNNING:
            db.commit()
            return _run_provision_orchestration(
                db,
                request,
                context,
                request_id=parsed_request_id,
                operation_id=existing.id,
            )
        return _orchestration_response(db, item, existing, idempotent_replay=True)
    running = _active_orchestration(db, item)
    if running is not None:
        return _orchestration_response(db, item, running, idempotent_replay=True)
    require_recent_reauthentication(context)
    if item.status != "FAILED":
        raise _error(409, "PROVISION_RETRY_NOT_ELIGIBLE", "当前申请不能执行 Retry")
    previous_plan, _released, failure = _retryable_failed_attempt(db, item)
    account = db.get(PortalUser, item.portal_account_id)
    if account is None:
        raise _error(409, "PORTAL_ACCOUNT_NOT_FOUND", "申请所属 Portal 账号不存在")
    _assert_identity_only(db, account)
    failure_evidence = failure.dry_run_result if isinstance(failure.dry_run_result, dict) else {}
    operation = _new_orchestration_operation(
        db,
        item=item,
        context=context,
        operation_type="compute.provision.retry",
        idempotency_key=body.idempotency_key,
        payload={
            "action": "RETRY_PROVISION",
            "failed_plan_id": str(previous_plan.id),
            "failed_operation_id": str(failure.id),
            "previous_attempt_number": previous_plan.attempt_number,
            "previous_error_code": failure.error_code,
            "previous_failed_step": failure_evidence.get("first_failed_step"),
            "rollback_verified": True,
            "approval_repeated": False,
            "admin_note": body.admin_note,
        },
        summary="Administrator started one complete fresh Provision retry",
    )
    item.status = "RETRY_AUTHORIZED"
    item.active_slot = 1
    item.updated_at = utcnow()
    _audit(
        db,
        request,
        context,
        event_type="COMPUTE_PROVISION_RETRY_STARTED",
        object_type="compute_resource_request",
        object_id=str(item.id),
        metadata={
            "previous_attempt_number": previous_plan.attempt_number,
            "rollback_result": "VERIFIED",
            "approval_repeated": False,
        },
        operation_id=operation.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error(
            409,
            "PROVISION_RETRY_CONFLICT",
            "Retry 已由另一个管理员动作启动",
        ) from exc
    return _run_provision_orchestration(
        db,
        request,
        context,
        request_id=parsed_request_id,
        operation_id=operation.id,
    )


@router.get(
    "/admin/compute-resource-requests/{request_id}/failed-provision-reconciliation-readiness",
    deprecated=True,
)
def failed_provision_reconciliation_readiness(
    request_id: str,
    plan_id: uuid.UUID,
    failed_stage_operation_id: uuid.UUID,
    context: AuthContext = Depends(permission_dependency("compute_requests.reconcile")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Read-only, target-bound preview of the formal reconciliation gate."""
    item = _admin_request(db, _request_id(request_id))
    _deny_self_administration(item, context)
    plan, reservations, failed_stage = _failed_stage_reconciliation_source(
        db,
        item,
        plan_id=plan_id,
        failed_stage_operation_id=failed_stage_operation_id,
        lock=False,
    )
    portal_checks = assert_zero_compute_side_effects(db, item.portal_account_id)
    verification = _worker_dry_run(
        "compute.provision.retry_verify",
        payload=_retry_verification_payload(item, plan, failed_stage),
        context=context,
        idempotency_key=f"compute-reconcile-readiness:{failed_stage.id}",
    )
    host_residue = verification.get("resource_residue")
    unknown_state = verification.get("unknown_resource_state")
    portal_residue = sorted(name for name, passed in portal_checks.items() if not passed)
    verified = bool(
        not portal_residue
        and verification.get("handler") == "compute.provision.retry_verify"
        and verification.get("retry_verification_status") == "VERIFIED_ZERO_RESIDUE"
        and verification.get("script_integrity") == "PASS"
        and host_residue == []
        and unknown_state == []
    )
    return {
        "status": "ZERO_VERIFIED" if verified else "CONFLICT",
        "checked_at": utcnow(),
        "request_id": str(item.id),
        "attempt_number": plan.attempt_number,
        "plan_id": str(plan.id),
        "failed_stage_operation_id": str(failed_stage.id),
        "rollback_status": failed_stage.rollback_status,
        "failed_hold_reservations": len(reservations),
        "portal_residue": portal_residue,
        "host_residue": host_residue if isinstance(host_residue, list) else ["UNKNOWN"],
        "unknown_resource_state": (
            unknown_state if isinstance(unknown_state, list) else ["UNKNOWN"]
        ),
        "script_integrity": verification.get("script_integrity", "UNKNOWN"),
        "state_changed": False,
        "attempt_created": False,
    }


@router.post(
    "/admin/compute-resource-requests/{request_id}/reconcile-failed-provision",
    deprecated=True,
)
def reconcile_failed_provision(
    request_id: str,
    body: ComputeProvisionReconciliationRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.reconcile")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Formally verify zero residue, then release only the bound FAILED_HOLD rows."""
    require_session_csrf(request, context)
    require_recent_reauthentication(context)
    item = _admin_request(db, _request_id(request_id), lock=True)
    _deny_self_administration(item, context)
    idempotency_key = f"compute.provision.reconcile:{body.idempotency_key}"
    existing = db.scalar(
        select(PortalOperation).where(
            PortalOperation.operation_type == "compute.provision.reconcile",
            PortalOperation.target_id == str(item.id),
            PortalOperation.idempotency_key == idempotency_key,
            PortalOperation.status == OperationStatus.SUCCEEDED,
        )
    )
    if existing is not None:
        payload = existing.validated_payload
        if payload.get("plan_id") != str(body.plan_id) or payload.get(
            "failed_stage_operation_id"
        ) != str(body.failed_stage_operation_id):
            raise _error(
                409,
                "PROVISION_RECONCILIATION_IDEMPOTENCY_CONFLICT",
                "对账幂等键已绑定其他失败 Attempt",
            )
        return {
            "status": "RECONCILED",
            "operation_id": str(existing.id),
            "idempotent_replay": True,
            "attempt_created": False,
            "request": _request_view(db, item, internal=True),
        }

    plan, reservations, failed_stage = _failed_stage_reconciliation_source(
        db,
        item,
        plan_id=body.plan_id,
        failed_stage_operation_id=body.failed_stage_operation_id,
        lock=True,
    )
    portal_zero_residue = assert_zero_compute_side_effects(db, item.portal_account_id)
    if not all(portal_zero_residue.values()):
        raise _error(
            409,
            "PROVISION_RECONCILIATION_PORTAL_RESIDUE_DETECTED",
            "Portal 仍记录计算资源，FAILED_HOLD 保持不变",
        )
    verification = _worker_dry_run(
        "compute.provision.retry_verify",
        payload=_retry_verification_payload(item, plan, failed_stage),
        context=context,
        idempotency_key=f"compute-reconcile-verify:{failed_stage.id}:{body.idempotency_key}",
    )
    if (
        verification.get("handler") != "compute.provision.retry_verify"
        or verification.get("retry_verification_status") != "VERIFIED_ZERO_RESIDUE"
        or verification.get("script_integrity") != "PASS"
        or verification.get("resource_residue") != []
        or verification.get("unknown_resource_state") != []
    ):
        raise _error(
            409,
            "PROVISION_RECONCILIATION_VERIFICATION_FAILED",
            "Root Worker 未能形式化证明零残留；FAILED_HOLD 保持不变",
        )

    initial_result = (
        failed_stage.dry_run_result if isinstance(failed_stage.dry_run_result, dict) else {}
    )
    initial_classification = str(
        initial_result.get("side_effect_classification", "PARTIAL_UNKNOWN")
    )[:64]
    operation = _operation(
        db,
        context=context,
        operation_type="compute.provision.reconcile",
        target_id=item.id,
        idempotency_key=str(body.idempotency_key),
        summary="Platform owner verified failed Provision rollback",
        payload={
            "request_id": str(item.id),
            "plan_id": str(plan.id),
            "failed_stage_operation_id": str(failed_stage.id),
            "reservation_ids": {
                row.resource_type: str(row.id)
                for row in sorted(reservations, key=lambda row: row.resource_type)
            },
            "initial_classification": initial_classification,
            "verified_classification": "PARTIAL_ROLLED_BACK",
            "rollback_verified": True,
            "portal_zero_residue": True,
            "worker_zero_residue": True,
            "script_integrity": "PASS",
            "reservation_state": "RELEASED",
            "review_note": body.review_note,
            "approval_repeated": False,
            "attempt_created": False,
        },
        result_summary=(
            "Rollback verified from Portal and Root Worker zero-residue evidence; "
            "FAILED_HOLD reservations released; no retry attempt created"
        ),
        risk_level=RiskLevel.CRITICAL,
    )
    now = utcnow()
    failed_stage.rollback_status = "ROLLED_BACK"
    for reservation in reservations:
        reservation.state = "RELEASED"
        reservation.active_key = None
        reservation.consumed_at = None
        reservation.released_at = now
    _audit(
        db,
        request,
        context,
        event_type="COMPUTE_PROVISION_ROLLBACK_RECONCILED",
        object_type="provision_plan",
        object_id=str(plan.id),
        metadata={
            "request_id": str(item.id),
            "failed_stage_operation_id": str(failed_stage.id),
            "initial_classification": initial_classification,
            "verified_classification": "PARTIAL_ROLLED_BACK",
            "rollback_verified": True,
            "reservation_state": "RELEASED",
            "approval_repeated": False,
            "attempt_created": False,
        },
        operation_id=operation.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error(
            409,
            "PROVISION_RECONCILIATION_CONFLICT",
            "失败 Attempt 对账并发冲突；请刷新后重新核验",
        ) from exc
    return {
        "status": "RECONCILED",
        "operation_id": str(operation.id),
        "idempotent_replay": False,
        "attempt_created": False,
        "rollback": "VERIFIED",
        "reservation_state": "RELEASED",
        "request": _request_view(db, item, internal=True),
    }


@router.post(
    "/admin/compute-resource-requests/{request_id}/retry-authorize",
    deprecated=True,
)
def authorize_provision_retry(
    request_id: str,
    body: ComputeProvisionRetryAuthorizationRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.plan")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    require_recent_reauthentication(context)
    item = _admin_request(db, _request_id(request_id), lock=True)
    _deny_self_administration(item, context)
    if item.status == "RETRY_AUTHORIZED":
        existing = db.scalar(
            select(PortalOperation)
            .where(
                PortalOperation.operation_type == "compute.provision.retry_authorize",
                PortalOperation.target_id == str(item.id),
                PortalOperation.status == OperationStatus.SUCCEEDED,
            )
            .order_by(PortalOperation.created_at.desc())
        )
        if existing is None:
            raise _error(
                409,
                "PROVISION_RETRY_AUTHORIZATION_INVALID",
                "Provision 重试授权记录不存在",
            )
        return {
            "status": "RETRY_AUTHORIZED",
            "operation_id": str(existing.id),
            "idempotent_replay": True,
            "request": _request_view(db, item, internal=True),
        }
    failed_plan, _released, failed_stage = _rolled_back_stage_retry(db, item)
    account = db.get(PortalUser, item.portal_account_id)
    if account is None:
        raise _error(409, "PORTAL_ACCOUNT_NOT_FOUND", "申请所属 Portal 账号不存在")
    _assert_identity_only(db, account)
    portal_zero_residue = assert_zero_compute_side_effects(db, item.portal_account_id)
    if not all(portal_zero_residue.values()):
        raise _error(
            409,
            "PROVISION_RETRY_PORTAL_RESIDUE_DETECTED",
            "Portal 仍记录计算资源，不能授权重试",
        )
    recorded = failed_stage.dry_run_result if isinstance(failed_stage.dry_run_result, dict) else {}
    recorded_classification = recorded.get("side_effect_classification")
    reconciled_classification = bool(
        recorded_classification in {"PARTIAL_UNKNOWN", "PARTIAL_ROLLBACK_FAILED"}
        and body.failure_classification == "PARTIAL_ROLLED_BACK"
        and _successful_reconciliation(db, item, failed_plan, failed_stage) is not None
    )
    if recorded_classification is not None and (
        recorded_classification != body.failure_classification and not reconciled_classification
    ):
        raise _error(
            409,
            "PROVISION_RETRY_EVIDENCE_MISMATCH",
            "管理员失败分类与 Worker 证据不一致",
        )
    verification = _worker_dry_run(
        "compute.provision.retry_verify",
        payload=_retry_verification_payload(item, failed_plan, failed_stage),
        context=context,
        idempotency_key=f"compute-retry-verify:{failed_stage.id}:{body.idempotency_key}",
    )
    if (
        verification.get("handler") != "compute.provision.retry_verify"
        or verification.get("retry_verification_status") != "VERIFIED_ZERO_RESIDUE"
        or verification.get("script_integrity") != "PASS"
        or verification.get("resource_residue") != []
        or verification.get("unknown_resource_state") != []
    ):
        raise _error(
            409,
            "PROVISION_RETRY_VERIFICATION_FAILED",
            "Root Worker 未能证明零残留与修复脚本完整性",
        )
    now = utcnow()
    operation = _operation(
        db,
        context=context,
        operation_type="compute.provision.retry_authorize",
        target_id=item.id,
        idempotency_key=str(body.idempotency_key),
        summary="Administrator authorized a fresh attempt after verified rollback",
        payload={
            "request_id": str(item.id),
            "plan_id": str(failed_plan.id),
            "failed_stage_operation_id": str(failed_stage.id),
            "failure_classification": body.failure_classification,
            "safe_root_cause": body.safe_root_cause,
            "authorization_reason": body.authorization_reason,
            "remediation_git_commit": body.remediation_git_commit,
            "rollback_verified": True,
            "root_cause_remediated": True,
            "approval_repeated": False,
        },
        result_summary=(
            "Retry authorized after live zero-residue and script-integrity verification; "
            "original approval preserved"
        ),
    )
    item.status = "RETRY_AUTHORIZED"
    item.active_slot = 1
    item.updated_at = now
    _audit(
        db,
        request,
        context,
        event_type="COMPUTE_PROVISION_RETRY_AUTHORIZED",
        object_type="compute_resource_request",
        object_id=str(item.id),
        metadata={
            "failed_plan_id": str(failed_plan.id),
            "failed_stage_operation_id": str(failed_stage.id),
            "failure_classification": body.failure_classification,
            "rollback_verified": True,
            "root_cause_remediated": True,
            "remediation_git_commit": body.remediation_git_commit,
            "approval_repeated": False,
        },
        operation_id=operation.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error(
            409,
            "PROVISION_RETRY_AUTHORIZATION_CONFLICT",
            "Provision 重试授权冲突，未创建新 Attempt",
        ) from exc
    return {
        "status": "RETRY_AUTHORIZED",
        "operation_id": str(operation.id),
        "idempotent_replay": False,
        "request": _request_view(db, item, internal=True),
    }


@router.post("/admin/compute-resource-requests/{request_id}/plan", deprecated=True)
def create_provision_plan(
    request_id: str,
    body: ComputeProvisionActionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.plan")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    item = _admin_request(db, _request_id(request_id), lock=True)
    _deny_self_administration(item, context)
    if item.status not in {"APPROVED", "RETRY_AUTHORIZED", "PROVISION_PLAN_READY"}:
        raise _error(
            409,
            "COMPUTE_REQUEST_NOT_READY_FOR_PLAN",
            "申请需要有效批准或独立 Provision 重试授权",
        )
    account = db.get(PortalUser, item.portal_account_id)
    if account is None:
        raise _error(409, "PORTAL_ACCOUNT_NOT_FOUND", "申请所属 Portal 账号不存在")
    _assert_identity_only(db, account)
    if account.normalized_login != item.username:
        raise _error(409, "REQUEST_OWNER_BINDING_FAILED", "申请用户名与所属账号不一致")

    _allocator_lock(db)
    _expire_reservations(db)
    if item.status == "PROVISION_PLAN_READY" and item.provision_plan_id:
        existing = db.get(PortalProvisionPlan, item.provision_plan_id)
        if existing is not None and existing.state in {"RESERVED", "READY_FOR_PROVISION"}:
            return {"status": existing.state, "plan": _plan_view(existing, internal=True)}

    previous_plan: PortalProvisionPlan | None = None
    failed_stage: PortalOperation | None = None
    retry_authorization: PortalOperation | None = None
    attempt_number = 1
    attempt_reason = "INITIAL"
    if item.status == "RETRY_AUTHORIZED":
        parent_operation_id = _ORCHESTRATION_OPERATION_ID.get()
        if parent_operation_id is None:
            (
                previous_plan,
                _released_reservations,
                failed_stage,
                retry_authorization,
            ) = _authorized_retry_source(db, item)
        else:
            retry_authorization = db.get(PortalOperation, parent_operation_id)
            previous_plan, _released_reservations, failed_stage = _retryable_failed_attempt(
                db, item
            )
            authorization_payload = (
                retry_authorization.validated_payload
                if retry_authorization is not None
                and isinstance(retry_authorization.validated_payload, dict)
                else {}
            )
            if (
                retry_authorization is None
                or retry_authorization.operation_type != "compute.provision.retry"
                or retry_authorization.status != OperationStatus.RUNNING
                or authorization_payload.get("failed_plan_id") != str(previous_plan.id)
                or authorization_payload.get("failed_operation_id") != str(failed_stage.id)
                or authorization_payload.get("rollback_verified") is not True
            ):
                raise _error(
                    409,
                    "PROVISION_RETRY_ORCHESTRATION_INVALID",
                    "顶层 Retry Operation 与失败 Attempt 绑定不完整",
                )
        attempt_number = previous_plan.attempt_number + 1
        attempt_reason = "STAGE_RETRY"
    elif item.status == "APPROVED" and item.provision_plan_id is not None:
        previous_plan = db.scalar(
            select(PortalProvisionPlan)
            .where(PortalProvisionPlan.id == item.provision_plan_id)
            .with_for_update()
        )
        if (
            previous_plan is None
            or previous_plan.request_id != item.id
            or previous_plan.portal_account_id != item.portal_account_id
            or previous_plan.state != "EXPIRED"
        ):
            raise _error(
                409,
                "PROVISION_ATTEMPT_LINEAGE_INVALID",
                "历史 Provision Plan 不能作为新 Attempt 的来源",
            )
        attempt_number = previous_plan.attempt_number + 1
        attempt_reason = "RESERVATION_EXPIRED"
    elif item.status != "APPROVED":
        raise _error(
            409,
            "COMPUTE_REQUEST_NOT_READY_FOR_PLAN",
            "申请需要有效批准或独立 Provision 重试授权",
        )

    exclusions = _active_reservations(db)
    payload = {
        "request_id": str(item.id),
        "portal_account_id": str(item.portal_account_id),
        "username": item.username,
        "requested_gpu_max": item.requested_gpu_max,
        "requested_storage_bytes": item.requested_storage_bytes,
        "requested_container_profile": item.requested_container_profile,
        "requested_lease_seconds": item.requested_lease_seconds,
        "reserved_uids": sorted(int(value) for value in exclusions["UID"]),
        "reserved_gids": sorted(int(value) for value in exclusions["GID"]),
        "reserved_project_ids": sorted(int(value) for value in exclusions["PROJECT_ID"]),
        "reserved_ssh_ports": sorted(int(value) for value in exclusions["SSH_PORT"]),
        "reserved_container_names": sorted(exclusions["CONTAINER_NAME"]),
    }
    worker = _worker_dry_run(
        "compute.provision.plan",
        payload=payload,
        context=context,
        idempotency_key=f"compute-plan:{item.id}:{body.idempotency_key}",
    )
    if worker.get("plan_status") != "READY":
        raise _error(409, "PROVISION_PLAN_CONFLICT", "资源分配器发现冲突，未建立 reservation")
    now = utcnow()
    reservation_expires_at = now + RESERVATION_LIFETIME
    plan = PortalProvisionPlan(  # noqa: S604 - ORM login shell, not subprocess
        request_id=item.id,
        attempt_number=attempt_number,
        attempt_reason=attempt_reason,
        previous_plan_id=previous_plan.id if previous_plan is not None else None,
        failed_stage_operation_id=failed_stage.id if failed_stage is not None else None,
        retry_authorization_operation_id=(
            retry_authorization.id if retry_authorization is not None else None
        ),
        portal_account_id=item.portal_account_id,
        state="RESERVED",
        username=item.username,
        uid=int(worker["proposed_uid"]),
        gid=int(worker["proposed_gid"]),
        project_id=int(worker["proposed_project_id"]),
        container_name=str(worker["proposed_container_name"]),
        container_ssh_port=int(worker["proposed_ssh_port"]),
        storage_bytes=STANDARD_STORAGE_BYTES,
        container_profile=item.requested_container_profile,
        container_cpus=8,
        container_memory_gb=32,
        container_pids_limit=4096,
        container_gpu=profile_gpu_count(item.requested_container_profile),
        slurm_account="company",
        slurm_qos="general",
        gpu_max=item.requested_gpu_max,
        lease_seconds=STANDARD_LEASE_SECONDS,
        lease_state="NOT_STARTED",
        host_ssh_enabled=False,
        shell="/usr/sbin/nologin",
        password_state="LOCKED",  # noqa: S106 - lifecycle state, never a credential
        execution_enabled=False,
        reservation_expires_at=reservation_expires_at,
        allocator_result={
            **worker,
            "attempt_number": attempt_number,
            "attempt_reason": attempt_reason,
            "previous_plan_id": str(previous_plan.id) if previous_plan is not None else None,
            "failed_stage_operation_id": str(failed_stage.id) if failed_stage is not None else None,
            "retry_authorization_operation_id": (
                str(retry_authorization.id) if retry_authorization is not None else None
            ),
        },
        created_by=context.user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(plan)
    db.flush()
    values = {
        "UID": str(plan.uid),
        "GID": str(plan.gid),
        "PROJECT_ID": str(plan.project_id),
        "SSH_PORT": str(plan.container_ssh_port),
        "CONTAINER_NAME": plan.container_name,
    }
    for resource_type, resource_value in values.items():
        db.add(
            PortalResourceReservation(
                plan_id=plan.id,
                request_id=item.id,
                portal_account_id=item.portal_account_id,
                resource_type=resource_type,
                resource_value=resource_value,
                active_key=f"{resource_type}:{resource_value}",
                state="RESERVED",
                reserved_at=now,
                expires_at=reservation_expires_at,
            )
        )
    parent_operation_id = _ORCHESTRATION_OPERATION_ID.get()
    if parent_operation_id is not None:
        parent_operation = db.get(PortalOperation, parent_operation_id)
        if parent_operation is None or parent_operation.status != OperationStatus.RUNNING:
            raise _error(
                409,
                "PROVISION_ORCHESTRATION_STATE_INVALID",
                "顶层 Provision Operation 状态不完整",
            )
        parent_payload = dict(parent_operation.validated_payload)
        parent_payload.update(
            {
                "plan_id": str(plan.id),
                "attempt_number": plan.attempt_number,
                "reservation_ids": {
                    row.resource_type: str(row.id)
                    for row in db.scalars(
                        select(PortalResourceReservation).where(
                            PortalResourceReservation.plan_id == plan.id
                        )
                    ).all()
                },
            }
        )
        parent_operation.validated_payload = parent_payload
    item.status = "PROVISION_PLAN_READY"
    item.active_slot = 1
    item.provision_plan_id = plan.id
    item.updated_at = now
    try:
        db.flush()
        operation = _operation(
            db,
            context=context,
            operation_type="compute.provision.plan",
            target_id=item.id,
            idempotency_key=str(body.idempotency_key),
            summary="Allocator created a new immutable provision attempt",
            payload={
                "request_id": str(item.id),
                "plan_id": str(plan.id),
                "portal_account_id": str(item.portal_account_id),
                "attempt_number": plan.attempt_number,
                "attempt_reason": plan.attempt_reason,
                "previous_plan_id": str(plan.previous_plan_id) if plan.previous_plan_id else None,
                "failed_stage_operation_id": str(plan.failed_stage_operation_id)
                if plan.failed_stage_operation_id
                else None,
                "retry_authorization_operation_id": str(plan.retry_authorization_operation_id)
                if plan.retry_authorization_operation_id
                else None,
                "gpu_max": plan.gpu_max,
                "storage_bytes": plan.storage_bytes,
                "container_profile": plan.container_profile,
                "lease_seconds": plan.lease_seconds,
                "execution_enabled": False,
            },
            result_summary=(
                "Fresh UID/GID/project/port/name reservation rows created; host unchanged"
            ),
        )
        _audit(
            db,
            request,
            context,
            event_type="PROVISION_PLAN_CREATED",
            object_type="provision_plan",
            object_id=str(plan.id),
            metadata={
                "request_id": str(item.id),
                "attempt_number": plan.attempt_number,
                "attempt_reason": plan.attempt_reason,
                "previous_plan_id": str(plan.previous_plan_id) if plan.previous_plan_id else None,
                "uid": plan.uid,
                "gid": plan.gid,
                "project_id": plan.project_id,
                "ssh_port": plan.container_ssh_port,
                "reservation_state": "RESERVED",
                "execution_enabled": False,
                "failed_stage_operation_id": str(plan.failed_stage_operation_id)
                if plan.failed_stage_operation_id
                else None,
                "retry_authorization_operation_id": str(plan.retry_authorization_operation_id)
                if plan.retry_authorization_operation_id
                else None,
            },
            operation_id=operation.id,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error(
            409,
            "RESOURCE_RESERVATION_CONFLICT",
            "并发分配产生冲突；未建立资源计划，请重试",
        ) from exc
    return {"status": "RESERVED", "plan": _plan_view(plan, internal=True)}


def _dry_run_payload(
    item: PortalComputeResourceRequest, plan: PortalProvisionPlan
) -> dict[str, Any]:
    return {
        "request_id": str(item.id),
        "plan_id": str(plan.id),
        "portal_account_id": str(item.portal_account_id),
        "username": plan.username,
        "uid": plan.uid,
        "gid": plan.gid,
        "project_id": plan.project_id,
        "ssh_port": plan.container_ssh_port,
        "container_name": plan.container_name,
        "storage_bytes": plan.storage_bytes,
        "container_profile": plan.container_profile,
        "container_cpus": plan.container_cpus,
        "container_memory_gb": plan.container_memory_gb,
        "container_pids_limit": plan.container_pids_limit,
        "container_gpu": plan.container_gpu,
        "slurm_account": plan.slurm_account,
        "slurm_qos": plan.slurm_qos,
        "gpu_max": plan.gpu_max,
        "lease_seconds": plan.lease_seconds,
        "lease_state": plan.lease_state,
        "host_ssh": "DISABLED",
        "shell": plan.shell,
        "password_state": plan.password_state,
        "execution_enabled": False,
    }


def _bound_reservations(
    db: Session,
    item: PortalComputeResourceRequest,
    plan: PortalProvisionPlan,
    *,
    lock: bool,
) -> list[PortalResourceReservation]:
    statement = select(PortalResourceReservation).where(
        PortalResourceReservation.plan_id == plan.id,
        PortalResourceReservation.state == "RESERVED",
        PortalResourceReservation.active_key.is_not(None),
    )
    if lock:
        statement = statement.with_for_update()
    reservations = list(db.scalars(statement).all())
    expected = {
        ("UID", str(plan.uid)),
        ("GID", str(plan.gid)),
        ("PROJECT_ID", str(plan.project_id)),
        ("SSH_PORT", str(plan.container_ssh_port)),
        ("CONTAINER_NAME", plan.container_name),
    }
    now = utcnow()
    actual = {(row.resource_type, row.resource_value) for row in reservations}
    if actual != expected or any(
        row.request_id != item.id
        or row.portal_account_id != item.portal_account_id
        or row.active_key != f"{row.resource_type}:{row.resource_value}"
        or ensure_utc(row.expires_at) <= now
        for row in reservations
    ):
        raise _error(409, "RESOURCE_RESERVATION_BINDING_FAILED", "资源 reservation 绑定不完整")
    return reservations


def _hold_uncertain_stage(
    db: Session,
    *,
    request: Request,
    context: AuthContext,
    item: PortalComputeResourceRequest,
    plan: PortalProvisionPlan,
    operation: PortalOperation,
    reservations: list[PortalResourceReservation],
    error_code: str,
    summary: str,
) -> None:
    """Verify host truth once; release only when every source is authoritative and absent."""
    failed_at = utcnow()
    verification = authoritative_zero_residue(
        db,
        item,
        plan,
        operation,
        actor=context.user.normalized_login,
        worker_call=call_worker,
    )
    verified = verification.all_absent
    operation.status = OperationStatus.FAILED
    operation.finished_at = failed_at
    operation.error_code = error_code[:64]
    operation.rollback_status = "ROLLED_BACK" if verified else "REQUIRES_MANUAL_REVIEW"
    operation.result_summary = summary[:1000]
    operation.dry_run_result = {
        "side_effect_classification": ("PARTIAL_ROLLED_BACK" if verified else "PARTIAL_UNKNOWN"),
        "rollback_status": operation.rollback_status,
        "last_successful_step": "UNKNOWN",
        "first_failed_step": "WORKER_RESPONSE_OR_PERSISTENCE",
        "failed_handler": "compute.provision.stage",
        "retained_resources": (
            []
            if verified
            else sorted(
                {
                    *verification.portal_residue,
                    *verification.host_residue,
                    *verification.unknown_resource_state,
                }
            )
        ),
        "automatic_reconciliation": verification.evidence(),
    }
    item.status = "FAILED"
    item.active_slot = None
    item.updated_at = failed_at
    plan.state = "FAILED"
    plan.execution_enabled = True
    plan.updated_at = failed_at
    for reservation in reservations:
        if verified:
            reservation.state = "RELEASED"
            reservation.active_key = None
            reservation.consumed_at = None
            reservation.released_at = failed_at
        elif reservation.state == "RESERVED":
            reservation.state = "FAILED_HOLD"
            reservation.active_key = f"{reservation.resource_type}:{reservation.resource_value}"
            reservation.consumed_at = None
            reservation.released_at = None
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=OperationStatus.RUNNING,
            to_status=OperationStatus.FAILED,
            safe_message=(
                "Authoritative verifier proved zero residue; reservations released"
                if verified
                else "Resource state is unknown; reservations held for manual review"
            ),
            created_at=failed_at,
        )
    )
    _audit(
        db,
        request,
        context,
        event_type="PROVISION_STAGE_RECONCILIATION_REQUIRED",
        object_type="provision_plan",
        object_id=str(plan.id),
        result="FAILED",
        metadata={
            "request_id": str(item.id),
            "error_code": operation.error_code,
            "rollback_status": operation.rollback_status,
            "reservation_state": "RELEASED" if verified else "FAILED_HOLD",
            "unknown_resource_state": list(verification.unknown_resource_state),
            "approval_repeated": False,
        },
        operation_id=operation.id,
    )


@router.post("/admin/compute-resource-requests/{request_id}/dry-run", deprecated=True)
def dry_run_provision_plan(
    request_id: str,
    body: ComputeProvisionActionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.plan")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    item = _admin_request(db, _request_id(request_id), lock=True)
    _deny_self_administration(item, context)
    if item.status != "PROVISION_PLAN_READY" or item.provision_plan_id is None:
        raise _error(409, "PROVISION_PLAN_NOT_READY", "资源计划尚未建立")
    plan = db.scalar(
        select(PortalProvisionPlan)
        .where(PortalProvisionPlan.id == item.provision_plan_id)
        .with_for_update()
    )
    if plan is None or plan.state not in {"RESERVED", "READY_FOR_PROVISION"}:
        raise _error(409, "PROVISION_PLAN_NOT_READY", "资源计划不可用于 dry-run")
    if plan.request_id != item.id or plan.portal_account_id != item.portal_account_id:
        raise _error(409, "PROVISION_PLAN_BINDING_FAILED", "资源计划与申请归属不一致")
    account = db.get(PortalUser, item.portal_account_id)
    if account is None:
        raise _error(409, "PORTAL_ACCOUNT_NOT_FOUND", "申请所属 Portal 账号不存在")
    _assert_identity_only(db, account)
    operation_key = f"compute.provision.dry_run:{body.idempotency_key}"
    existing_operation = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.user.id,
            PortalOperation.target_id == str(item.id),
            PortalOperation.operation_type == "compute.provision.dry_run",
            PortalOperation.idempotency_key == operation_key,
        )
    )
    if plan.state == "READY_FOR_PROVISION" and existing_operation is not None:
        existing_payload = (
            existing_operation.validated_payload
            if isinstance(existing_operation.validated_payload, dict)
            else {}
        )
        stage_contract = _ready_stage_contract(plan.dry_run_result)
        if (
            existing_operation.status != OperationStatus.SUCCEEDED
            or existing_payload.get("plan_id") != str(plan.id)
            or stage_contract is None
            or existing_payload.get("stage_contract_sha256") != stage_contract["contract_sha256"]
        ):
            raise _error(
                409,
                "PROVISION_DRY_RUN_IDEMPOTENCY_CONFLICT",
                "Dry-run 幂等记录与当前 Attempt 不一致",
            )
        parent_operation_id = _ORCHESTRATION_OPERATION_ID.get()
        if parent_operation_id is not None:
            parent_operation = db.get(PortalOperation, parent_operation_id)
            if parent_operation is None or parent_operation.status != OperationStatus.RUNNING:
                raise _error(
                    409,
                    "PROVISION_ORCHESTRATION_STATE_INVALID",
                    "顶层 Provision Operation 状态不完整",
                )
            parent_payload = dict(parent_operation.validated_payload)
            parent_payload["canonical_execution_contract"] = stage_contract["contract_sha256"]
            parent_payload["canonical_local_image_identity"] = stage_contract["image_contract"][
                "canonical_local_image_identity"
            ]
            parent_operation.validated_payload = parent_payload
            db.commit()
        return {
            "status": "READY_FOR_PROVISION",
            "plan": _plan_view(plan, internal=True),
            "idempotent_replay": True,
        }
    now = utcnow()
    if ensure_utc(plan.reservation_expires_at) <= now:
        raise _error(409, "RESOURCE_RESERVATION_EXPIRED", "资源 reservation 已过期")
    _bound_reservations(db, item, plan, lock=False)
    worker = _worker_dry_run(
        "compute.provision.dry_run",
        payload=_dry_run_payload(item, plan),
        context=context,
        idempotency_key=f"compute-dry-run:{plan.id}:{body.idempotency_key}",
    )
    if worker.get("dry_run_status") != "READY_FOR_PROVISION":
        plan.dry_run_result = worker
        plan.updated_at = utcnow()
        db.commit()
        raise _error(409, "PROVISION_DRY_RUN_CONFLICT", "Provision dry-run 未通过")
    stage_contract = _ready_stage_contract(worker)
    if stage_contract is None:
        plan.dry_run_result = worker
        plan.updated_at = utcnow()
        db.commit()
        raise _error(
            409,
            "PROVISION_DRY_RUN_CONTRACT_INCOMPLETE",
            "Provision dry-run 缺少完整 Stage confirmation contract 证据",
        )
    now = utcnow()
    plan.state = "READY_FOR_PROVISION"
    plan.dry_run_result = worker
    plan.dry_run_at = now
    plan.updated_at = now
    parent_operation_id = _ORCHESTRATION_OPERATION_ID.get()
    if parent_operation_id is not None:
        parent_operation = db.get(PortalOperation, parent_operation_id)
        if parent_operation is None or parent_operation.status != OperationStatus.RUNNING:
            raise _error(
                409,
                "PROVISION_ORCHESTRATION_STATE_INVALID",
                "顶层 Provision Operation 状态不完整",
            )
        parent_payload = dict(parent_operation.validated_payload)
        parent_payload["canonical_execution_contract"] = stage_contract["contract_sha256"]
        parent_payload["canonical_local_image_identity"] = stage_contract["image_contract"][
            "canonical_local_image_identity"
        ]
        parent_operation.validated_payload = parent_payload
    operation = _operation(
        db,
        context=context,
        operation_type="compute.provision.dry_run",
        target_id=item.id,
        idempotency_key=str(body.idempotency_key),
        summary="Worker revalidated the exact reserved plan without writes",
        payload={
            "request_id": str(item.id),
            "plan_id": str(plan.id),
            "portal_account_id": str(item.portal_account_id),
            "execution_enabled": False,
            "stage_contract_sha256": stage_contract["contract_sha256"],
        },
        result_summary="READY_FOR_PROVISION; infrastructure side effects remained zero",
    )
    operation.dry_run_result = worker
    _audit(
        db,
        request,
        context,
        event_type="PROVISION_DRY_RUN_COMPLETED",
        object_type="provision_plan",
        object_id=str(plan.id),
        metadata={
            "request_id": str(item.id),
            "dry_run_status": "READY_FOR_PROVISION",
            "execution_enabled": False,
            "infrastructure_side_effects": "NONE",
            "lease_state": "NOT_STARTED",
            "stage_contract_status": "PASS",
            "stage_contract_sha256": stage_contract["contract_sha256"],
        },
        operation_id=operation.id,
    )
    db.commit()
    return {"status": "READY_FOR_PROVISION", "plan": _plan_view(plan, internal=True)}


@router.post(
    "/admin/compute-resource-requests/{request_id}/provision",
    deprecated=True,
)
def provision_reserved_compute_environment(
    request_id: str,
    body: ComputeProvisionActionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.plan")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    parent_operation_id = _ORCHESTRATION_OPERATION_ID.get()
    if parent_operation_id is None:
        require_recent_reauthentication(context)
    parsed_request_id = _request_id(request_id)
    item = _admin_request(db, parsed_request_id, lock=True)
    _deny_self_administration(item, context)
    if item.status in {"STAGED", "KEY_ENROLLMENT_PENDING"} and item.managed_user_id is not None:
        plan = db.get(PortalProvisionPlan, item.provision_plan_id)
        managed = db.get(PortalManagedUser, item.managed_user_id)
        if plan is None or managed is None or plan.state != "STAGED":
            raise _error(409, "PROVISION_STAGE_STATE_MISMATCH", "已 Stage 状态绑定不完整")
        operation = db.scalar(
            select(PortalOperation).where(
                PortalOperation.operation_type == "compute.provision.stage",
                PortalOperation.target_id == str(item.id),
                PortalOperation.status == OperationStatus.SUCCEEDED,
            )
        )
        return {
            "status": "KEY_ENROLLMENT_PENDING",
            "operation_id": str(operation.id) if operation else None,
            "managed_user_id": str(managed.id),
            "plan": _plan_view(plan, internal=True),
            "idempotent_replay": True,
        }
    if item.status == "PROVISIONING":
        running = db.scalar(
            select(PortalOperation)
            .where(
                PortalOperation.operation_type == "compute.provision.stage",
                PortalOperation.target_id == str(item.id),
                PortalOperation.status == OperationStatus.RUNNING,
            )
            .order_by(PortalOperation.started_at.desc())
        )
        raise _error(
            409,
            "PROVISION_STAGE_IN_PROGRESS",
            f"Compute Stage 正在执行或等待人工对账（Operation {running.id if running else 'UNKNOWN'}）",
        )
    if item.status != "PROVISION_PLAN_READY" or item.provision_plan_id is None:
        raise _error(409, "PROVISION_PLAN_NOT_READY", "资源计划尚未完成 Dry-run")
    plan = db.scalar(
        select(PortalProvisionPlan)
        .where(PortalProvisionPlan.id == item.provision_plan_id)
        .with_for_update()
    )
    if (
        plan is None
        or plan.state != "READY_FOR_PROVISION"
        or plan.dry_run_at is None
        or not isinstance(plan.dry_run_result, dict)
        or plan.dry_run_result.get("dry_run_status") != "READY_FOR_PROVISION"
    ):
        raise _error(409, "PROVISION_DRY_RUN_REQUIRED", "真实 Stage 需要成功的精确 Dry-run")
    stage_contract = _ready_stage_contract(plan.dry_run_result)
    if stage_contract is None:
        raise _error(
            409,
            "PROVISION_DRY_RUN_CONTRACT_INCOMPLETE",
            "真实 Stage 需要完整且有效的 Dry-run Stage contract",
        )
    if parent_operation_id is not None:
        parent_operation = db.get(PortalOperation, parent_operation_id)
        parent_payload = (
            parent_operation.validated_payload
            if parent_operation is not None and isinstance(parent_operation.validated_payload, dict)
            else {}
        )
        if (
            parent_operation is None
            or parent_operation.status != OperationStatus.RUNNING
            or parent_payload.get("deployment_version") != deployment_version()
            or parent_payload.get("canonical_execution_contract")
            != stage_contract["contract_sha256"]
            or parent_payload.get("canonical_local_image_identity")
            != stage_contract["image_contract"]["canonical_local_image_identity"]
        ):
            raise _error(
                409,
                "PROVISION_RUNTIME_CONTRACT_MISMATCH",
                "Runtime 或 canonical execution contract 已变化；Stage 在写入前停止",
            )
    if plan.request_id != item.id or plan.portal_account_id != item.portal_account_id:
        raise _error(409, "PROVISION_PLAN_BINDING_FAILED", "资源计划与申请归属不一致")
    account = db.get(PortalUser, item.portal_account_id)
    if account is None:
        raise _error(409, "PORTAL_ACCOUNT_NOT_FOUND", "申请所属 Portal 账号不存在")
    _assert_identity_only(db, account)
    if ensure_utc(plan.reservation_expires_at) <= utcnow():
        raise _error(409, "RESOURCE_RESERVATION_EXPIRED", "资源 reservation 已过期")
    reservations = _bound_reservations(db, item, plan, lock=True)
    dry_run_operations = list(
        db.scalars(
            select(PortalOperation)
            .where(
                PortalOperation.operation_type == "compute.provision.dry_run",
                PortalOperation.target_id == str(item.id),
                PortalOperation.status == OperationStatus.SUCCEEDED,
            )
            .order_by(PortalOperation.finished_at.desc())
        ).all()
    )
    dry_run_operation = next(
        (
            operation
            for operation in dry_run_operations
            if isinstance(operation.validated_payload, dict)
            and operation.validated_payload.get("plan_id") == str(plan.id)
            and operation.validated_payload.get("stage_contract_sha256")
            == stage_contract["contract_sha256"]
            and (_ready_stage_contract(operation.dry_run_result) or {}).get("contract_sha256")
            == stage_contract["contract_sha256"]
        ),
        None,
    )
    if dry_run_operation is None or dry_run_operation.finished_at is None:
        raise _error(409, "PROVISION_DRY_RUN_OPERATION_MISSING", "Dry-run Operation 证据不存在")
    reservation_ids = {row.resource_type: str(row.id) for row in reservations}
    operation_id = uuid.uuid4()
    payload = {
        **_dry_run_payload(item, plan),
        "stage_operation_id": str(operation_id),
        "dry_run_operation_id": str(dry_run_operation.id),
        "dry_run_stage_contract": stage_contract,
        "reservation_ids": reservation_ids,
        "execution_enabled": True,
    }
    operation_payload = dict(payload)
    if parent_operation_id is not None:
        operation_payload.update(
            {
                "parent_operation_id": str(parent_operation_id),
                "visibility": "INTERNAL_STEP",
            }
        )
    now = utcnow()
    operation = PortalOperation(
        id=operation_id,
        operation_type="compute.provision.stage",
        target_type="compute_resource_request",
        target_id=str(item.id),
        requested_by=context.user.id,
        owner_managed_user_id=None,
        approved_by=context.user.id,
        request_summary="Stage exact reserved compute environment after administrator reauthentication",
        validated_payload=operation_payload,
        idempotency_key=f"compute.provision.stage:{body.idempotency_key}",
        risk_level=RiskLevel.CRITICAL,
        status=OperationStatus.RUNNING,
        created_at=now,
        approved_at=now,
        started_at=now,
    )
    db.add(operation)
    db.flush()
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=None,
            to_status=OperationStatus.RUNNING,
            safe_message="Reserved compute Stage dispatched to fixed Root Worker handler",
            created_at=now,
        )
    )
    item.status = "PROVISIONING"
    item.active_slot = 1
    item.updated_at = now
    plan.state = "PROVISIONING"
    plan.execution_enabled = True
    plan.updated_at = now
    try:
        # The Operation and execution gate must be durable before the Root
        # Worker can mutate the host. A timeout can therefore never make a
        # real Stage look as if it had not started.
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error(
            409, "PROVISION_STAGE_OPERATION_CONFLICT", "Compute Stage Operation 冲突"
        ) from exc

    try:
        worker = call_worker(
            "compute.provision.stage",
            payload=payload,
            requested_by=context.user.normalized_login,
            approved_by=context.user.normalized_login,
            idempotency_key=f"compute-stage:{operation.id}",
            dry_run=False,
            timeout_seconds=1800,
        )
    except WorkerClientError as exc:
        db.rollback()
        item = _admin_request(db, parsed_request_id, lock=True)
        plan = db.scalar(
            select(PortalProvisionPlan)
            .where(PortalProvisionPlan.id == item.provision_plan_id)
            .with_for_update()
        )
        operation = db.get(PortalOperation, operation_id)
        if plan is None or operation is None or operation.status != OperationStatus.RUNNING:
            raise _error(
                409, "PROVISION_STAGE_RECONCILIATION_REQUIRED", "Stage 状态需要人工对账"
            ) from exc
        reservations = list(
            db.scalars(
                select(PortalResourceReservation)
                .where(PortalResourceReservation.plan_id == plan.id)
                .with_for_update()
            ).all()
        )
        _hold_uncertain_stage(
            db,
            request=request,
            context=context,
            item=item,
            plan=plan,
            operation=operation,
            reservations=reservations,
            error_code=exc.code,
            summary="Worker response is unknown; host truth must be reconciled before any retry",
        )
        db.commit()
        raise _error(503, exc.code, "Root Worker 无法执行 Compute Stage") from exc

    # Do not retain ORM objects loaded before the external call. Re-lock and
    # validate the durable PROVISIONING state before consuming Worker results.
    db.rollback()
    item = _admin_request(db, parsed_request_id, lock=True)
    persisted_plan_id = item.provision_plan_id
    plan = db.scalar(
        select(PortalProvisionPlan)
        .where(PortalProvisionPlan.id == item.provision_plan_id)
        .with_for_update()
    )
    operation = db.get(PortalOperation, operation_id)
    if (
        plan is None
        or operation is None
        or operation.status != OperationStatus.RUNNING
        or item.status != "PROVISIONING"
        or plan.state != "PROVISIONING"
        or not plan.execution_enabled
    ):
        raise _error(409, "PROVISION_STAGE_RECONCILIATION_REQUIRED", "Stage 持久状态发生变化")
    account = db.get(PortalUser, item.portal_account_id)
    if account is None:
        raise _error(409, "PORTAL_ACCOUNT_NOT_FOUND", "申请所属 Portal 账号不存在")
    reservations = list(
        db.scalars(
            select(PortalResourceReservation)
            .where(PortalResourceReservation.plan_id == plan.id)
            .with_for_update()
        ).all()
    )
    if len(reservations) != 5 or any(
        row.request_id != item.id
        or row.portal_account_id != item.portal_account_id
        or row.state != "RESERVED"
        or row.active_key != f"{row.resource_type}:{row.resource_value}"
        for row in reservations
    ):
        _hold_uncertain_stage(
            db,
            request=request,
            context=context,
            item=item,
            plan=plan,
            operation=operation,
            reservations=reservations,
            error_code="RESOURCE_RESERVATION_BINDING_FAILED",
            summary="Worker returned after reservation binding changed; reconcile host truth",
        )
        db.commit()
        raise _error(409, "RESOURCE_RESERVATION_BINDING_FAILED", "Stage reservation 持久状态异常")
    if worker.get("status") != "SUCCEEDED" or worker.get("handler") != "compute.provision.stage":
        raw_error = worker.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        operation.status = OperationStatus.FAILED
        operation.finished_at = utcnow()
        operation.error_code = str(error.get("code", "COMPUTE_STAGE_FAILED"))[:64]
        classification = str(worker.get("side_effect_classification", "PARTIAL_UNKNOWN"))
        rollback_status = str(worker.get("rollback_status", "REQUIRES_MANUAL_REVIEW"))
        verification = authoritative_zero_residue(
            db,
            item,
            plan,
            operation,
            actor=context.user.normalized_login,
            worker_call=call_worker,
        )
        if verification.all_absent:
            classification = (
                "NO_SIDE_EFFECT" if classification == "NO_SIDE_EFFECT" else "PARTIAL_ROLLED_BACK"
            )
            rollback_status = (
                "NOT_REQUIRED" if classification == "NO_SIDE_EFFECT" else "ROLLED_BACK"
            )
            retained: list[str] = []
        else:
            classification = (
                "PARTIAL_UNKNOWN"
                if verification.unknown_resource_state or verification.script_integrity != "PASS"
                else "PARTIAL_ROLLBACK_FAILED"
            )
            rollback_status = "REQUIRES_MANUAL_REVIEW"
            retained = sorted(
                {
                    *verification.portal_residue,
                    *verification.host_residue,
                    *verification.unknown_resource_state,
                }
            )
        raw_rollback_steps = worker.get("rollback_steps")
        rollback_steps = (
            {
                str(key)[:64]: str(value)[:32]
                for key, value in sorted(raw_rollback_steps.items())
                if re.fullmatch(r"[A-Z0-9_]+", str(key))
                and str(value) in {"SUCCEEDED", "FAILED", "BLOCKED", "NOT_REQUIRED"}
            }
            if isinstance(raw_rollback_steps, dict)
            else {}
        )
        expected_rollback = {
            "NO_SIDE_EFFECT": "NOT_REQUIRED",
            "PARTIAL_ROLLED_BACK": "ROLLED_BACK",
        }
        releasable = (
            classification in expected_rollback
            and rollback_status == expected_rollback[classification]
            and not retained
        )
        held = not releasable
        operation.rollback_status = rollback_status[:32]
        operation.result_summary = (
            "Compute Stage failed closed; approval preserved; "
            f"side effects classified {classification}"
        )[:1000]
        operation.dry_run_result = {
            "side_effect_classification": classification,
            "rollback_status": rollback_status,
            "last_successful_step": str(worker.get("last_successful_step", "UNKNOWN"))[:128],
            "first_failed_step": str(worker.get("first_failed_step", "UNKNOWN"))[:128],
            "failed_handler": str(worker.get("failed_handler", "compute.provision.stage"))[:128],
            "safe_error_message": str(error.get("message", "Compute Stage failed"))[:500],
            "retained_resources": retained,
            "rollback_steps": rollback_steps,
            "stage_failure_code": str(
                worker.get("stage_failure_code", "FIXED_STAGE_SCRIPT_FAILED")
            )[:128],
            "automatic_reconciliation": verification.evidence(),
        }
        for reservation in reservations:
            reservation.state = "FAILED_HOLD" if held else "RELEASED"
            if held:
                reservation.active_key = f"{reservation.resource_type}:{reservation.resource_value}"
                reservation.consumed_at = None
                reservation.released_at = None
            else:
                reservation.active_key = None
                reservation.consumed_at = None
                reservation.released_at = utcnow()
        plan.state = "FAILED"
        # TRUE is a historical fact: the real Stage gate was opened.  Keeping
        # it prevents a failed execution from looking like a dry-run-only plan.
        plan.execution_enabled = True
        item.status = "FAILED"
        item.active_slot = None
        item.updated_at = utcnow()
        db.add(
            PortalOperationEvent(
                operation_id=operation.id,
                from_status=OperationStatus.RUNNING,
                to_status=OperationStatus.FAILED,
                safe_message="Compute Stage failed closed; no re-approval was created",
                created_at=utcnow(),
            )
        )
        _audit(
            db,
            request,
            context,
            event_type="PROVISION_STAGE_FAILED",
            object_type="provision_plan",
            object_id=str(plan.id),
            result="FAILED",
            metadata={
                "request_id": str(item.id),
                "error_code": operation.error_code,
                "rollback_status": operation.rollback_status,
                "side_effect_classification": classification,
                "last_successful_step": operation.dry_run_result["last_successful_step"],
                "first_failed_step": operation.dry_run_result["first_failed_step"],
                "reservation_state": "FAILED_HOLD" if held else "RELEASED",
                "approval_repeated": False,
            },
            operation_id=operation.id,
        )
        db.commit()
        raise _error(409, operation.error_code, "Compute Stage 未完成，已停止后续步骤")

    stage = worker.get("stage")
    if not isinstance(stage, dict):
        _hold_uncertain_stage(
            db,
            request=request,
            context=context,
            item=item,
            plan=plan,
            operation=operation,
            reservations=reservations,
            error_code="COMPUTE_STAGE_RESULT_INVALID",
            summary="Worker reported success without a complete Stage result",
        )
        db.commit()
        raise _error(409, "COMPUTE_STAGE_RESULT_INVALID", "Worker Stage 结果不完整")
    expected_stage = {
        "username": plan.username,
        "uid": plan.uid,
        "gid": plan.gid,
        "shell": "/usr/sbin/nologin",
        "password": "LOCKED",
        "host_ssh": "DISABLED",
        "host_authorized_keys": "ABSENT",
        "container_authorized_keys": "ABSENT",
        "onboarding_state": "STAGED",
        "ssh_key_state": "REQUIRED_BEFORE_ACTIVATION",
    }
    if any(stage.get(key) != value for key, value in expected_stage.items()):
        _hold_uncertain_stage(
            db,
            request=request,
            context=context,
            item=item,
            plan=plan,
            operation=operation,
            reservations=reservations,
            error_code="COMPUTE_STAGE_RESULT_MISMATCH",
            summary="Worker Stage identity result differs from reserved plan",
        )
        db.commit()
        raise _error(409, "COMPUTE_STAGE_RESULT_MISMATCH", "Worker Stage 结果与 Plan 不一致")
    container_stage = stage.get("container")
    slurm_stage = stage.get("slurm")
    lease_stage = stage.get("lease")
    filesystem_stage = stage.get("filesystem_isolation")
    if not (
        isinstance(container_stage, dict)
        and container_stage.get("name") == plan.container_name
        and container_stage.get("state") == "STOPPED"
        and container_stage.get("gpu") == "NONE"
        and container_stage.get("ssh_port") == plan.container_ssh_port
        and stage.get("storage_path") == str(workspace_path(plan.uid))
        and stage.get("container_workspace") == "/workspace"
        and stage.get("default_job_workdir") == str(workspace_path(plan.uid) / "projects")
        and isinstance(slurm_stage, dict)
        and slurm_stage.get("account") == plan.slurm_account
        and slurm_stage.get("qos") == plan.slurm_qos
        and slurm_stage.get("max_gpus") == plan.gpu_max
        and slurm_stage.get("max_tres") == f"gres/gpu={plan.gpu_max}"
        and isinstance(lease_stage, dict)
        and lease_stage == {"state": "NOT_STARTED", "starts_at": None, "expires_at": None}
        and isinstance(filesystem_stage, dict)
        and filesystem_stage
        == {
            "origin_pilot_to_target": "DENIED",
            "target_to_origin_pilot": "DENIED",
            "markers_removed": True,
        }
    ):
        _hold_uncertain_stage(
            db,
            request=request,
            context=context,
            item=item,
            plan=plan,
            operation=operation,
            reservations=reservations,
            error_code="COMPUTE_STAGE_RESOURCE_MISMATCH",
            summary="Worker Stage resource result differs from reserved plan",
        )
        db.commit()
        raise _error(409, "COMPUTE_STAGE_RESOURCE_MISMATCH", "Worker 资源结果与 Plan 不一致")
    image_digest = str(container_stage.get("image_digest", ""))
    if not image_digest.startswith("sha256:") or len(image_digest) > 255:
        _hold_uncertain_stage(
            db,
            request=request,
            context=context,
            item=item,
            plan=plan,
            operation=operation,
            reservations=reservations,
            error_code="COMPUTE_STAGE_IMAGE_INVALID",
            summary="Worker Stage image identity is incomplete",
        )
        db.commit()
        raise _error(409, "COMPUTE_STAGE_IMAGE_INVALID", "Worker 未返回固定镜像摘要")

    try:
        staged_at = utcnow()
        managed = PortalManagedUser(  # noqa: S604 - ORM shell path, not subprocess execution
            portal_user_id=account.id,
            unix_username=plan.username,
            uid=plan.uid,
            gid=plan.gid,
            shell="/usr/sbin/nologin",
            host_access_state="DISABLED_BY_PLATFORM_POLICY",
            compute_environment_state="STAGED",
            gpu_isolation_state="VERIFIED",
            slurm_account=plan.slurm_account,
            slurm_qos=plan.slurm_qos,
            project_id=plan.project_id,
            quota_bytes=plan.storage_bytes,
            container_name=plan.container_name,
            container_port=plan.container_ssh_port,
            onboarding_state=OnboardingState.STAGED,
            ssh_key_state="REQUIRED_BEFORE_ACTIVATION",
            ssh_key_count=0,
            staged_at=staged_at,
            compute_activated_at=None,
        )
        db.add(managed)
        db.flush()
        safe_spec = {
            "cpus": 8,
            "memory_gb": 32,
            "pids_limit": 4096,
            "gpu": "NONE" if plan.container_gpu == 0 else "SLURM_ALLOCATED_1",
            "development_profile": plan.container_profile,
            "privileged": False,
            "host_network": False,
            "host_pid": False,
            "host_ipc": False,
            "docker_socket": False,
            "munge": False,
            "authorized_keys": "ABSENT",
            "host_authorized_keys": "ABSENT",
            "container_authorized_keys": "ABSENT",
            "guard": "PASSING",
            "gpu_open_probe": "DENIED",
            "cuda_context_probe": "DENIED",
            "filesystem_isolation": filesystem_stage,
            "max_gpus": plan.gpu_max,
            "lease_state": "NOT_STARTED",
            "provision_plan_id": str(plan.id),
        }
        db.add(
            PortalContainer(
                managed_user_id=managed.id,
                owner_managed_user_id=managed.id,
                name=plan.container_name,
                image_digest=image_digest,
                ssh_port=plan.container_ssh_port,
                desired_state="STOPPED",
                observed_state="STOPPED",
                development_profile=plan.container_profile,
                gpu_count=plan.container_gpu,
                safe_spec=safe_spec,
            )
        )
        db.add(
            PortalStorageResource(
                owner_managed_user_id=managed.id,
                root_path=str(workspace_path(plan.uid)),
                quota_bytes=plan.storage_bytes,
                state="STAGED",
            )
        )
        account.unix_username = plan.username
        account.resource_onboarding_state = OnboardingState.STAGED
        item.managed_user_id = managed.id
        item.status = "KEY_ENROLLMENT_PENDING"
        item.active_slot = None
        item.updated_at = staged_at
        plan.state = "STAGED"
        plan.execution_enabled = True
        plan.updated_at = staged_at
        for reservation in reservations:
            reservation.state = "CONSUMED"
            reservation.active_key = None
            reservation.consumed_at = staged_at
        db.query(PortalSession).filter(PortalSession.user_id == account.id).update(
            {PortalSession.owner_managed_user_id: managed.id}, synchronize_session=False
        )
        operation.owner_managed_user_id = managed.id
        operation.status = OperationStatus.SUCCEEDED
        operation.finished_at = staged_at
        operation.worker_execution_id = str(worker.get("request_id", "worker"))[:64]
        operation.result_summary = (
            "Compute environment STAGED; SSH key and activation remain pending"
        )
        db.add(
            PortalOperationEvent(
                operation_id=operation.id,
                from_status=OperationStatus.RUNNING,
                to_status=OperationStatus.SUCCEEDED,
                safe_message="Reserved resources consumed; Lease remains NOT_STARTED",
                created_at=staged_at,
            )
        )
        _audit(
            db,
            request,
            context,
            event_type="PROVISION_STAGE_COMPLETED",
            object_type="provision_plan",
            object_id=str(plan.id),
            metadata={
                "request_id": str(item.id),
                "managed_user_id": str(managed.id),
                "uid": plan.uid,
                "gid": plan.gid,
                "project_id": plan.project_id,
                "ssh_port": plan.container_ssh_port,
                "container_state": "STOPPED",
                "host_ssh": "DISABLED",
                "lease_state": "NOT_STARTED",
                "filesystem_isolation": "DENIED_BOTH_DIRECTIONS",
                "reservation_state": "CONSUMED",
                "approval_repeated": False,
            },
            operation_id=operation.id,
        )
        # Include flush failures in the same reconciliation window as commit
        # failures: the host has already changed at this point.
        db.flush()
        db.commit()
    except Exception as exc:
        # Host Stage has already succeeded. Never pretend it rolled back merely
        # because Portal postcondition persistence failed or was ambiguous.
        db.rollback()
        recovery = db.get(PortalOperation, operation_id)
        recovery_item = db.get(PortalComputeResourceRequest, parsed_request_id)
        recovery_plan = db.get(PortalProvisionPlan, persisted_plan_id)
        if recovery is not None and recovery.status == OperationStatus.SUCCEEDED:
            return {
                "status": "KEY_ENROLLMENT_PENDING",
                "operation_id": str(recovery.id),
                "managed_user_id": str(recovery.owner_managed_user_id),
                "plan": _plan_view(recovery_plan, internal=True),
                "idempotent_replay": True,
            }
        if (
            recovery is not None
            and recovery.status == OperationStatus.RUNNING
            and recovery_item is not None
            and recovery_plan is not None
        ):
            held_reservations = list(
                db.scalars(
                    select(PortalResourceReservation).where(
                        PortalResourceReservation.plan_id == recovery_plan.id
                    )
                ).all()
            )
            _hold_uncertain_stage(
                db,
                request=request,
                context=context,
                item=recovery_item,
                plan=recovery_plan,
                operation=recovery,
                reservations=held_reservations,
                error_code="PROVISION_STAGE_PERSISTENCE_UNCERTAIN",
                summary=(
                    "Worker Stage succeeded but Portal persistence is incomplete; "
                    "reconcile host truth"
                ),
            )
            db.commit()
        raise _error(
            409,
            "PROVISION_STAGE_RECONCILIATION_REQUIRED",
            "宿主 Stage 已执行，但 Portal 状态需要人工对账",
        ) from exc
    return {
        "status": "KEY_ENROLLMENT_PENDING",
        "operation_id": str(operation.id),
        "managed_user_id": str(managed.id),
        "plan": _plan_view(plan, internal=True),
        "idempotent_replay": bool(worker.get("idempotent_replay", False)),
    }


def assert_zero_compute_side_effects(db: Session, account_id: uuid.UUID) -> dict[str, bool]:
    """Reusable acceptance assertion for API tests and deployment diagnostics."""
    managed_ids = select(PortalManagedUser.id).where(PortalManagedUser.portal_user_id == account_id)
    return {
        "managed_user_absent": db.scalar(
            select(PortalManagedUser.id).where(PortalManagedUser.portal_user_id == account_id)
        )
        is None,
        "container_absent": db.scalar(
            select(PortalContainer.id).where(PortalContainer.managed_user_id.in_(managed_ids))
        )
        is None,
        "storage_absent": db.scalar(
            select(PortalStorageResource.id).where(
                PortalStorageResource.owner_managed_user_id.in_(managed_ids)
            )
        )
        is None,
        "lease_absent": db.scalar(
            select(PortalComputeLease.id).where(PortalComputeLease.managed_user_id.in_(managed_ids))
        )
        is None,
    }
