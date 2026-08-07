import uuid
from typing import Any, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.auth import (
    AuthContext,
    client_ip,
    require_recent_reauthentication,
    require_session_csrf,
    user_agent,
)
from h100_portal_api.database import SessionLocal, get_db
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.enums import OnboardingState, OperationStatus
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalOperationApproval,
    PortalOperationEvent,
    PortalSshKey,
    PortalUser,
    utcnow,
)
from h100_portal_api.operations import (
    HIGH_RISK_OPERATION_TYPES,
    OPERATION_PERMISSIONS,
    WRITE_OPERATION_TYPES,
    can_transition,
    risk_for,
)
from h100_portal_api.schemas import (
    ApprovalRequest,
    OperationCreateRequest,
    OperationResponse,
    OperationSubmitRequest,
    UserActivatePayload,
    UserStagePayload,
)
from h100_portal_api.security import SAFE_TARGET_RE, safe_metadata, safe_target
from h100_portal_api.worker_client import WorkerClientError, call_worker

router = APIRouter(prefix="/operations", tags=["operations"])

STAGE_PUBLIC_KEY_FIELDS = {
    "public_key_file",
    "public_key_path",
    "public_key",
    "raw_public_key",
    "approved_ssh_key_record_ids",
}
FORBIDDEN_SECRET_OR_COMMAND_FIELDS = {
    "raw_private_key",
    "private_key",
    "password",
    "command",
    "argv",
    "path",
}
APPROVED_ORIGIN_PILOT_STAGE: dict[str, Any] = {
    "username": "origin-pilot",
    "uid": 20001,
    "gid": 20001,
    "project_id": 30001,
    "ssh_port": 22023,
    "quota_gb": 300,
    "slurm_account": "company",
    "slurm_qos": "general",
    "max_gpus": 1,
    "container_name": "gpu-dev-origin-pilot",
    "cpus": 8,
    "memory_gb": 32,
    "pids_limit": 4096,
    "gpu": "none",
    "expected_state": "DRAFT",
}
PORTAL3C_STAGE_IDEMPOTENCY_KEY = "portal3b-r-origin-pilot-stage-v1"
PORTAL3C_STAGE_APPROVAL_REFERENCE = "portal3b-r-lifecycle-revalidated"


class OperationPayloadError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def validate_activate_database_bindings(
    db: Session, *, owner_id: uuid.UUID, target_id: str, payload: dict[str, Any]
) -> None:
    """Bind Activate IDs to the owner's staged origin-pilot record.

    The Worker independently validates the root-controlled key bytes.  The API
    must still prevent a caller from mixing a managed-user UUID or approved key
    records belonging to another Portal account into this operation.
    """
    try:
        managed_id = uuid.UUID(str(payload["managed_user_id"]))
        key_ids = [uuid.UUID(str(value)) for value in payload["approved_ssh_key_record_ids"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise OperationPayloadError(
            "ACTIVATE_PAYLOAD_REJECTED", "invalid managed user or SSH key record ID"
        ) from exc
    managed = db.get(PortalManagedUser, managed_id)
    if (
        managed is None
        or managed.portal_user_id != owner_id
        or managed.unix_username != target_id
        or managed.onboarding_state != OnboardingState.STAGED
        or managed.shell != "/usr/sbin/nologin"
    ):
        raise OperationPayloadError(
            "USER_NOT_IN_STAGED_STATE",
            "managed identity is not the owner's staged origin-pilot record",
        )
    records = db.scalars(select(PortalSshKey).where(PortalSshKey.id.in_(key_ids))).all()
    by_id = {record.id: record for record in records}
    if len(by_id) != len(key_ids):
        raise OperationPayloadError(
            "PUBLIC_KEY_RECORD_NOT_FOUND", "one or more approved SSH key records do not exist"
        )
    for key_id in key_ids:
        record = by_id[key_id]
        if record.managed_user_id != managed.id:
            raise OperationPayloadError(
                "PUBLIC_KEY_RECORD_OWNER_MISMATCH",
                "approved SSH key record belongs to another managed identity",
            )
        if not record.active or record.revoked_at is not None:
            raise OperationPayloadError(
                "PUBLIC_KEY_RECORD_NOT_ACTIVE", "approved SSH key record is not active"
            )
        if record.approved_by is None or record.approved_at is None:
            raise OperationPayloadError(
                "PUBLIC_KEY_RECORD_NOT_APPROVED", "SSH key record has no independent approval"
            )


def operation_response(operation: PortalOperation) -> OperationResponse:
    return OperationResponse(
        id=operation.id,
        operation_type=operation.operation_type,
        target_type=operation.target_type,
        target_id=operation.target_id,
        requested_by=operation.requested_by,
        approved_by=operation.approved_by,
        request_summary=operation.request_summary,
        risk_level=operation.risk_level,
        status=operation.status,
        created_at=operation.created_at,
        approved_at=operation.approved_at,
        started_at=operation.started_at,
        finished_at=operation.finished_at,
        dry_run_result=operation.dry_run_result,
        result_summary=operation.result_summary,
        error_code=operation.error_code,
    )


def validate_operation_payload(operation_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if operation_type == "user.stage":
        if set(payload) & STAGE_PUBLIC_KEY_FIELDS:
            raise OperationPayloadError(
                "PUBLIC_KEY_NOT_ALLOWED_DURING_STAGE",
                "SSH public keys are accepted only by user.activate",
            )
        if set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
            raise OperationPayloadError(
                "PAYLOAD_REJECTED", "secret or command fields are forbidden"
            )
        try:
            validated = UserStagePayload.model_validate(payload).model_dump(
                mode="json", exclude_none=True
            )
        except ValidationError as exc:
            raise OperationPayloadError(
                "STAGE_PAYLOAD_REJECTED", "invalid user.stage payload"
            ) from exc
        for field, expected in APPROVED_ORIGIN_PILOT_STAGE.items():
            if validated[field] != expected:
                raise OperationPayloadError(
                    "STAGE_PLAN_MISMATCH", "user.stage payload differs from the validated plan"
                )
        return validated
    if operation_type == "user.activate":
        if set(payload) & (STAGE_PUBLIC_KEY_FIELDS - {"approved_ssh_key_record_ids"}):
            raise OperationPayloadError(
                "ARBITRARY_PATH_REJECTED", "Activate accepts approved key record IDs, not paths"
            )
        if set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
            raise OperationPayloadError(
                "PAYLOAD_REJECTED", "secret or command fields are forbidden"
            )
        key_ids = payload.get("approved_ssh_key_record_ids")
        if not isinstance(key_ids, list) or not key_ids:
            raise OperationPayloadError(
                "PUBLIC_KEY_REQUIRED_FOR_ACTIVATION",
                "at least one approved SSH key record is required",
            )
        try:
            return UserActivatePayload.model_validate(payload).model_dump(mode="json")
        except ValidationError as exc:
            raise OperationPayloadError(
                "ACTIVATE_PAYLOAD_REJECTED", "invalid user.activate payload"
            ) from exc
    allowed: dict[str, set[str]] = {
        "user.plan": {"username"},
        "user.suspend": {"username"},
        "container.start": {"name"},
        "container.stop": {"name"},
        "container.restart": {"name"},
        "container.rebuild": {"name"},
        "slurm.drain": {"node_name"},
        "slurm.resume": {"node_name"},
        "job.cancel": {"job_id"},
        "quota.update": {"username", "quota_bytes"},
        "ssh_key.add": {"username", "key_type", "fingerprint", "comment_summary"},
        "ssh_key.revoke": {"username", "fingerprint"},
    }
    if operation_type not in allowed:
        raise OperationPayloadError("UNKNOWN_OPERATION", "unknown write operation")
    if set(payload) - allowed[operation_type]:
        raise OperationPayloadError("PAYLOAD_REJECTED", "payload contains unsupported fields")
    result: dict[str, Any] = {}
    if "username" in allowed[operation_type]:
        username = payload.get("username")
        normalized_username = username.casefold() if isinstance(username, str) else ""
        protected = normalized_username in {"root", "origin-al", "codexops"}
        if (
            not isinstance(username, str)
            or not SAFE_TARGET_RE.fullmatch(username)
            or (operation_type == "user.plan" and normalized_username != "origin-pilot")
            or protected
        ):
            raise ValueError("protected or invalid username")
        result["username"] = normalized_username
    if "name" in allowed[operation_type]:
        name = payload.get("name")
        if (
            not isinstance(name, str)
            or not SAFE_TARGET_RE.fullmatch(name)
            or not (name.startswith("gpu-dev-") or name.startswith("h100-"))
        ):
            raise ValueError("invalid managed container name")
        result["name"] = name
    if "node_name" in allowed[operation_type]:
        node = payload.get("node_name")
        if node != "sagsh100server":
            raise ValueError("only the known single node is in scope")
        result["node_name"] = node
    if "job_id" in allowed[operation_type]:
        job_id = payload.get("job_id")
        if not isinstance(job_id, int) or not 0 < job_id < 2**63:
            raise ValueError("invalid job id")
        result["job_id"] = job_id
    if "quota_bytes" in allowed[operation_type]:
        quota = payload.get("quota_bytes")
        if not isinstance(quota, int) or not 0 < quota <= 10 * 1024**4:
            raise ValueError("invalid quota")
        result["quota_bytes"] = quota
    for field in ("key_type", "fingerprint", "comment_summary"):
        if field in payload:
            value = payload[field]
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 255
                or "PRIVATE KEY" in value
            ):
                raise ValueError("invalid SSH metadata")
            result[field] = value[:255]
    return result


def transition(
    operation: PortalOperation, target: OperationStatus, message: str, db: Session
) -> None:
    if not can_transition(operation.status, target):
        raise RuntimeError(f"invalid operation transition {operation.status}->{target}")
    previous = operation.status
    operation.status = target
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=previous,
            to_status=target,
            safe_message=message[:1000],
            created_at=utcnow(),
        )
    )


def is_portal3c_real_stage(
    operation: PortalOperation, requester: PortalUser | None, approver: PortalUser | None
) -> bool:
    """Recognize the single administrator-approved Portal-3C write."""
    if (
        operation.operation_type != "user.stage"
        or operation.target_type != "compute_identity"
        or operation.target_id != "origin-pilot"
        or operation.idempotency_key != PORTAL3C_STAGE_IDEMPOTENCY_KEY
        or requester is None
        or approver is None
        or requester.id != approver.id
        or requester.normalized_login != "origin-al"
        or approver.normalized_login != "origin-al"
        or requester.account_state.value != "ACTIVE"
        or not any(role.name == "platform_owner" for role in requester.roles)
    ):
        return False
    try:
        validated = validate_operation_payload("user.stage", operation.validated_payload)
    except ValueError:
        return False
    return (
        validated == operation.validated_payload
        and validated.get("approval_reference") == PORTAL3C_STAGE_APPROVAL_REFERENCE
    )


def persist_portal3c_staged_identity(
    db: Session,
    *,
    owner: PortalUser,
    operation: PortalOperation,
    worker_result: dict[str, Any],
) -> PortalManagedUser:
    """Persist the exact host-verified STAGED result without changing management identity."""
    stage = worker_result.get("stage")
    if not isinstance(stage, dict):
        raise RuntimeError("Worker Stage response has no structured postcondition record")
    expected = {
        "username": "origin-pilot",
        "uid": 20001,
        "gid": 20001,
        "shell": "/usr/sbin/nologin",
        "password": "LOCKED",
        "host_access": "DISABLED",
        "onboarding_state": "STAGED",
        "ssh_key_state": "REQUIRED_BEFORE_ACTIVATION",
    }
    if any(stage.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Worker Stage identity result differs from approval")
    if stage.get("authorized_keys") != "ABSENT":
        raise RuntimeError("Worker did not prove authorized_keys absence")
    gpu_policy = stage.get("gpu_policy")
    quota = stage.get("quota")
    slurm = stage.get("slurm")
    container_stage = stage.get("container")
    guard = stage.get("guard")
    if (
        not isinstance(gpu_policy, dict)
        or not isinstance(quota, dict)
        or not isinstance(slurm, dict)
        or not isinstance(container_stage, dict)
        or not isinstance(guard, dict)
    ):
        raise RuntimeError("Worker Stage resource result is incomplete")
    if (
        gpu_policy.get("unit") != "user-20001.slice"
        or gpu_policy.get("device_policy") != "closed"
        or gpu_policy.get("device_allow") != []
        or gpu_policy.get("open_probe") != "DENIED"
        or gpu_policy.get("cuda_context_probe") != "DENIED"
        or quota.get("project_id") != 30001
        or quota.get("hard_limit_gb") != 300
        or slurm.get("account") != "company"
        or slurm.get("qos") != "general"
        or slurm.get("max_gpus") != 1
        or slurm.get("node_state") != "DRAIN"
        or slurm.get("queue") != "EMPTY"
        or container_stage.get("name") != "gpu-dev-origin-pilot"
        or container_stage.get("state") != "STOPPED"
        or container_stage.get("gpu") != "NONE"
        or container_stage.get("cpus") != 8
        or container_stage.get("memory_gb") != 32
        or container_stage.get("pids_limit") != 4096
        or container_stage.get("ssh_port") != 22023
        or guard.get("timer") != "ENABLED_ACTIVE"
        or guard.get("status") != "PASSING"
    ):
        raise RuntimeError("Worker Stage resource result differs from approval")
    image_digest = str(container_stage.get("image_digest", ""))
    if not image_digest.startswith("sha256:") or len(image_digest) > 255:
        raise RuntimeError("Worker did not return a fixed container image digest")
    if owner.unix_username != "origin-al":
        raise RuntimeError("Origin-al management mapping changed before Stage persistence")

    managed = db.scalar(
        select(PortalManagedUser).where(PortalManagedUser.portal_user_id == owner.id)
    )
    if managed is None:
        managed = PortalManagedUser(  # noqa: S604 -- ORM shell field, no subprocess shell.
            portal_user_id=owner.id,
            unix_username="origin-pilot",
            uid=20001,
            gid=20001,
            shell="/usr/sbin/nologin",
            host_access_state="DISABLED",
            gpu_isolation_state="VERIFIED",
            slurm_account="company",
            slurm_qos="general",
            project_id=30001,
            quota_bytes=300 * 1024**3,
            container_name="gpu-dev-origin-pilot",
            container_port=22023,
            onboarding_state=OnboardingState.STAGED,
            ssh_key_state="REQUIRED_BEFORE_ACTIVATION",
            ssh_key_count=0,
            staged_at=utcnow(),
            compute_activated_at=None,
        )
        db.add(managed)
        db.flush()
    else:
        current_identity = (
            managed.unix_username,
            managed.uid,
            managed.gid,
            managed.project_id,
            managed.container_name,
            managed.container_port,
        )
        if current_identity != (
            "origin-pilot",
            20001,
            20001,
            30001,
            "gpu-dev-origin-pilot",
            22023,
        ):
            raise RuntimeError("existing managed identity conflicts with Portal-3C approval")
        managed.shell = "/usr/sbin/nologin"
        managed.host_access_state = "DISABLED"
        managed.gpu_isolation_state = "VERIFIED"
        managed.slurm_account = "company"
        managed.slurm_qos = "general"
        managed.quota_bytes = 300 * 1024**3
        managed.onboarding_state = OnboardingState.STAGED
        managed.ssh_key_state = "REQUIRED_BEFORE_ACTIVATION"
        managed.ssh_key_count = 0
        managed.compute_activated_at = None
        managed.staged_at = managed.staged_at or utcnow()

    container = db.scalar(
        select(PortalContainer).where(PortalContainer.name == "gpu-dev-origin-pilot")
    )
    safe_spec = {
        "cpus": 8,
        "memory_gb": 32,
        "pids_limit": 4096,
        "gpu": "NONE",
        "privileged": False,
        "host_network": False,
        "host_pid": False,
        "host_ipc": False,
        "docker_socket": False,
        "password_authentication": False,
        "root_login": False,
        "authorized_keys": "ABSENT",
        "guard": "PASSING",
        "gpu_open_probe": "DENIED",
        "cuda_context_probe": "DENIED",
        "max_gpus": 1,
    }
    if container is None:
        container = PortalContainer(
            managed_user_id=managed.id,
            name="gpu-dev-origin-pilot",
            image_digest=image_digest,
            ssh_port=22023,
            desired_state="STOPPED",
            observed_state="STOPPED",
            safe_spec=safe_spec,
        )
        db.add(container)
    else:
        if container.managed_user_id not in {None, managed.id}:
            raise RuntimeError("existing container record belongs to another identity")
        container.managed_user_id = managed.id
        container.image_digest = image_digest
        container.ssh_port = 22023
        container.desired_state = "STOPPED"
        container.observed_state = "STOPPED"
        container.safe_spec = safe_spec

    owner.resource_onboarding_state = OnboardingState.STAGED
    for event_type, object_type, metadata in (
        ("user.stage.linux", "managed_user", {"uid": 20001, "gid": 20001, "shell": "nologin"}),
        ("user.stage.gpu_policy", "gpu_policy", {"unit": "user-20001.slice", "verified": True}),
        ("user.stage.gpu_self_test", "gpu_policy", {"open": "DENIED", "cuda": "DENIED"}),
        ("user.stage.quota", "xfs_project", {"project_id": 30001, "hard_limit_gb": 300}),
        (
            "user.stage.slurm",
            "slurm_association",
            {"account": "company", "qos": "general", "max_gpus": 1},
        ),
        (
            "user.stage.container",
            "container",
            {"name": "gpu-dev-origin-pilot", "state": "STOPPED", "gpu": "NONE"},
        ),
        ("user.stage.guard", "gpu_guard", {"timer": "ENABLED", "status": "PASSING"}),
        (
            "user.stage.completed",
            "managed_user",
            {"state": "STAGED", "ssh_key_state": "REQUIRED_BEFORE_ACTIVATION"},
        ),
    ):
        record_audit(
            db,
            event_type=event_type,
            actor="h100-portal-worker",
            actor_role="root_worker",
            source_ip="local-worker-socket",
            user_agent="h100-portal-api",
            object_type=object_type,
            object_id="origin-pilot",
            result="SUCCESS",
            metadata=metadata,
            operation_id=operation.id,
        )
    return managed


def execute_operation(operation_id: uuid.UUID, actor_login: str) -> None:
    with SessionLocal() as db:
        operation = db.get(PortalOperation, operation_id)
        if operation is None or operation.status != OperationStatus.QUEUED:
            return
        requester = db.get(PortalUser, operation.requested_by)
        approver = db.get(PortalUser, operation.approved_by) if operation.approved_by else None
        real_stage = is_portal3c_real_stage(operation, requester, approver)
        transition(
            operation,
            OperationStatus.RUNNING,
            "Controlled Worker Stage started" if real_stage else "Worker dry-run started",
            db,
        )
        operation.started_at = utcnow()
        db.commit()
        try:
            result = call_worker(
                operation.operation_type,
                payload=operation.validated_payload,
                requested_by=requester.normalized_login if requester else "portal",
                approved_by=approver.normalized_login if approver else actor_login,
                idempotency_key=operation.idempotency_key,
                dry_run=not real_stage,
                timeout_seconds=1830 if real_stage else 30,
            )
            if real_stage and result.get("status") == "SUCCEEDED" and requester is not None:
                persist_portal3c_staged_identity(
                    db,
                    owner=requester,
                    operation=operation,
                    worker_result=result,
                )
                execution_record = cast(dict[str, Any], safe_metadata(result))
                previous_plan = operation.dry_run_result or {}
                operation.dry_run_result = {
                    **previous_plan,
                    "execution_result": execution_record,
                    "stage_status": "STAGED",
                    "execution_enabled": True,
                }
                transition(operation, OperationStatus.SUCCEEDED, "origin-pilot Stage verified", db)
                operation.result_summary = (
                    "origin-pilot 已 STAGED；nologin、密码锁定、无 authorized_keys；"
                    "GPU/quota/Slurm/容器/Guard 已验证；未 Activate"
                )
                operation.worker_execution_id = str(result.get("request_id", "worker"))[:64]
                record_audit(
                    db,
                    event_type="worker.execute",
                    actor=actor_login,
                    actor_role="platform_owner",
                    source_ip="local-worker-socket",
                    user_agent="h100-portal-api",
                    object_type="operation",
                    object_id=str(operation.id),
                    result="SUCCESS",
                    metadata={"operation_type": "user.stage", "execution_mode": "real-stage"},
                    operation_id=operation.id,
                )
            elif not real_stage and result.get("status") == "DRY_RUN":
                operation.dry_run_result = cast(dict[str, Any], safe_metadata(result))
                transition(operation, OperationStatus.SUCCEEDED, "dry-run plan validated", db)
                operation.result_summary = (
                    "dry-run 计划已通过 Worker schema 和脚本完整性检查；未执行宿主写操作"
                )
                operation.worker_execution_id = str(result.get("request_id", "dry-run"))[:64]
                record_audit(
                    db,
                    event_type="worker.execute",
                    actor=actor_login,
                    actor_role="platform_owner",
                    source_ip="local-worker-socket",
                    user_agent="h100-portal-api",
                    object_type="operation",
                    object_id=str(operation.id),
                    result="DRY_RUN",
                    metadata={"operation_type": operation.operation_type},
                    operation_id=operation.id,
                )
            else:
                rollback_status = str(result.get("rollback_status", ""))[:32]
                if real_stage and rollback_status == "ROLLED_BACK":
                    transition(
                        operation, OperationStatus.ROLLING_BACK, "Worker rollback recorded", db
                    )
                    transition(
                        operation, OperationStatus.ROLLED_BACK, "Worker rollback completed", db
                    )
                else:
                    transition(
                        operation,
                        OperationStatus.FAILED,
                        "Worker rejected Stage" if real_stage else "Worker rejected dry-run",
                        db,
                    )
                operation.rollback_status = rollback_status or None
                operation.error_code = str(result.get("error", {}).get("code", "WORKER_FAILED"))[
                    :64
                ]
                operation.result_summary = (
                    "Stage 失败；保持 nologin/密码锁定/无公钥，需按 rollback_status 核查宿主"
                    if real_stage
                    else "Worker 未执行宿主写操作"
                )
                record_audit(
                    db,
                    event_type="worker.stage_failed" if real_stage else "worker.reject",
                    actor=actor_login,
                    actor_role="platform_owner",
                    source_ip="local-worker-socket",
                    user_agent="h100-portal-api",
                    object_type="operation",
                    object_id=str(operation.id),
                    result="DENIED",
                    metadata={"operation_type": operation.operation_type},
                    operation_id=operation.id,
                )
            # Force every Portal-side write issued above to reach the database
            # while it is still covered by the persistence error boundary.  A
            # successful Worker Stage must never leave a half-persisted managed
            # identity if a later constraint rejects the Portal transaction.
            db.flush()
        except WorkerClientError as exc:
            transition(operation, OperationStatus.FAILED, "Worker unavailable or rejected", db)
            operation.error_code = getattr(exc, "code", "WORKER_FAILED")[:64]
            operation.result_summary = (
                "Worker 连接失败；Stage 宿主状态未知，保持 Slurm DRAIN 并人工核查"
                if real_stage
                else "受控 Worker 不可用；未执行宿主写操作"
            )
            record_audit(
                db,
                event_type="worker.reject",
                actor=actor_login,
                actor_role="platform_owner",
                source_ip="local-worker-socket",
                user_agent="h100-portal-api",
                object_type="operation",
                object_id=str(operation.id),
                result="FAILED",
                metadata={
                    "operation_type": operation.operation_type,
                    "error_code": operation.error_code,
                },
                operation_id=operation.id,
            )
        except (RuntimeError, IntegrityError) as exc:
            # RUNNING was committed before invoking the Worker.  Roll back all
            # uncommitted managed-user/container/audit writes, then reload that
            # durable RUNNING operation before recording the manual-review
            # failure.  Reusing the pre-rollback ORM objects could otherwise
            # commit a partial Portal representation of a successful host Stage.
            db.rollback()
            operation = db.get(PortalOperation, operation_id)
            if operation is None or operation.status != OperationStatus.RUNNING:
                return
            transition(operation, OperationStatus.FAILED, "Portal Stage persistence rejected", db)
            operation.error_code = "PORTAL_STAGE_STATE_REJECTED"
            operation.rollback_status = "REQUIRES_MANUAL_REVIEW"
            operation.result_summary = (
                "Worker 返回后 Portal 状态持久化失败；宿主可能已 STAGED，保持 DRAIN 并人工核查"
            )
            record_audit(
                db,
                event_type="portal.stage_persist_failed",
                actor=actor_login,
                actor_role="platform_owner",
                source_ip="local-worker-socket",
                user_agent="h100-portal-api",
                object_type="operation",
                object_id=str(operation.id),
                result="FAILED",
                metadata={
                    "operation_type": operation.operation_type,
                    "detail": (
                        "database integrity constraint rejected Portal Stage state"
                        if isinstance(exc, IntegrityError)
                        else str(exc)[:255]
                    ),
                },
                operation_id=operation.id,
            )
        operation.finished_at = utcnow()
        db.add(
            PortalOperationEvent(
                operation_id=operation.id,
                from_status=operation.status,
                to_status=operation.status,
                safe_message="Worker execution recorded",
                created_at=utcnow(),
            )
        )
        db.commit()


@router.get("")
def list_operations(
    context: AuthContext = Depends(permission_dependency("operations.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.scalars(
        select(PortalOperation).order_by(PortalOperation.created_at.desc()).limit(200)
    ).all()
    return {
        "status": "OK",
        "operations": [operation_response(row).model_dump(mode="json") for row in rows],
    }


def enrich_user_plan_with_portal_state(
    plan: dict[str, Any], db: Session, requested_by: uuid.UUID
) -> dict[str, Any]:
    """Add database-side conflict checks without creating a managed identity."""
    enriched = dict(plan)
    conflicts = [item for item in plan.get("conflicts", []) if isinstance(item, dict)]
    checks = [item for item in plan.get("validation_results", []) if isinstance(item, dict)]
    username = str(plan.get("proposed_username", ""))
    managed = db.scalar(
        select(PortalManagedUser).where(PortalManagedUser.unix_username == username)
    )
    portal_login = db.scalar(select(PortalUser).where(PortalUser.normalized_login == username))
    retained_history = db.scalar(
        select(PortalAuditEvent).where(
            PortalAuditEvent.object_id == username,
            or_(
                PortalAuditEvent.event_type.ilike("%decommission%"),
                PortalAuditEvent.event_type.ilike("%depart%"),
                PortalAuditEvent.event_type.ilike("%delete%"),
            ),
        )
    )
    candidate_filters = []
    for field, key in (
        (PortalManagedUser.uid, "proposed_uid"),
        (PortalManagedUser.gid, "proposed_gid"),
        (PortalManagedUser.project_id, "proposed_project_id"),
        (PortalManagedUser.container_port, "proposed_ssh_port"),
    ):
        value = plan.get(key)
        if isinstance(value, int):
            candidate_filters.append(select(PortalManagedUser).where(field == value))
    if managed is not None:
        conflicts.append(
            {"code": "PORTAL_MANAGED_USER_CONFLICT", "message": "Portal 已存在同名受管计算身份"}
        )
    if portal_login is not None:
        conflicts.append(
            {"code": "PORTAL_LOGIN_CONFLICT", "message": "Portal 已存在 origin-pilot 登录身份"}
        )
    if retained_history is not None:
        conflicts.append(
            {
                "code": "RETAINED_IDENTITY_HISTORY",
                "message": "审计中存在禁止自动复用的离职/删除记录",
            }
        )
    for statement in candidate_filters:
        row = db.scalar(statement)
        if row is not None and row is not managed:
            conflicts.append(
                {
                    "code": "PORTAL_RESOURCE_RESERVATION_CONFLICT",
                    "message": "Portal 已登记候选 UID/GID/project/端口",
                }
            )
            break
    checks.append(
        {
            "check": "portal_managed_identity_absent",
            "status": "PASS" if managed is None else "FAIL",
            "detail": "Portal 尚无 origin-pilot managed identity；本操作只创建计划"
            if managed is None
            else "Portal managed identity 已存在",
        }
    )
    checks.extend(
        [
            {
                "check": "portal_login_name_available",
                "status": "PASS" if portal_login is None else "FAIL",
                "detail": "Portal 登录名 origin-pilot 未使用"
                if portal_login is None
                else "Portal 登录名已存在",
            },
            {
                "check": "retained_identity_history_absent",
                "status": "PASS" if retained_history is None else "FAIL",
                "detail": "未发现禁止复用的离职/删除审计记录"
                if retained_history is None
                else "存在禁止自动复用的审计记录",
            },
        ]
    )
    # A previous plan by this same owner is informational, not a resource
    # reservation. It is deliberately not treated as a conflict.
    enriched["portal_database_checks"] = {
        "requested_by": str(requested_by),
        "managed_identity_created": False,
        "candidate_values_reserved": False,
    }
    enriched["validation_results"] = checks
    enriched["conflicts"] = conflicts
    enriched["plan_status"] = (
        "READY" if not conflicts and plan.get("plan_status") == "READY" else "CONFLICT"
    )
    if enriched["plan_status"] != "READY":
        enriched["execution_blocked_reason"] = "Portal 或宿主冲突检查未通过；没有执行任何写操作"
    return enriched


@router.post("")
def create_operation(
    body: OperationCreateRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("operations.write")),
    db: Session = Depends(get_db),
) -> OperationResponse:
    require_session_csrf(request, context)
    operation_type = body.operation_type
    if operation_type not in WRITE_OPERATION_TYPES:
        raise HTTPException(
            status_code=422,
            detail={"code": "OPERATION_NOT_WRITABLE", "message": "仅允许受控写操作"},
        )
    permission = OPERATION_PERMISSIONS.get(operation_type)
    if permission is None:
        raise HTTPException(
            status_code=403, detail={"code": "FORBIDDEN", "message": "当前账号无权执行此操作"}
        )
    from h100_portal_api.rbac import require_permission

    require_permission(context.user, permission)
    try:
        target_id = safe_target(body.target_id)
        validated = validate_operation_payload(operation_type, body.payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": getattr(exc, "code", "PAYLOAD_REJECTED"), "message": str(exc)},
        ) from exc
    if operation_type == "user.plan" and target_id != "origin-pilot":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PLAN_TARGET_REJECTED",
                "message": "Portal-3A 计划目标必须为 origin-pilot",
            },
        )
    if operation_type in {"user.stage", "user.activate"} and target_id != "origin-pilot":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "LIFECYCLE_TARGET_REJECTED",
                "message": "当前两阶段生命周期仅允许 origin-pilot",
            },
        )
    if operation_type == "user.activate":
        try:
            validate_activate_database_bindings(
                db, owner_id=context.user.id, target_id=target_id, payload=validated
            )
        except OperationPayloadError as exc:
            raise HTTPException(
                status_code=409, detail={"code": exc.code, "message": str(exc)}
            ) from exc
    existing = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.user.id,
            PortalOperation.idempotency_key == body.idempotency_key,
        )
    )
    if existing:
        return operation_response(existing)
    operation = PortalOperation(
        operation_type=operation_type,
        target_type=body.target_type,
        target_id=target_id,
        requested_by=context.user.id,
        request_summary=body.request_summary[:500],
        validated_payload=safe_metadata(validated),
        idempotency_key=body.idempotency_key,
        risk_level=risk_for(operation_type),
        status=OperationStatus.DRAFT,
        created_at=utcnow(),
    )
    db.add(operation)
    db.flush()
    if operation_type in {"user.plan", "user.stage", "user.activate"}:
        try:
            worker_plan = call_worker(
                operation_type,
                payload=validated,
                requested_by=context.user.normalized_login,
                approved_by=None,
                idempotency_key=body.idempotency_key,
                dry_run=True,
                timeout_seconds=25,
            )
            if worker_plan.get("status") == "DRY_RUN":
                if operation_type == "user.plan":
                    worker_plan = enrich_user_plan_with_portal_state(
                        worker_plan, db, context.user.id
                    )
                operation.dry_run_result = cast(dict[str, Any], safe_metadata(worker_plan))
                lifecycle_status = worker_plan.get(
                    "plan_status", worker_plan.get("stage_status", "READY")
                )
                operation.result_summary = (
                    f"{operation_type} dry-run 已验证；未执行任何宿主写操作"
                    if lifecycle_status == "READY"
                    else f"{operation_type} dry-run 发现冲突；未执行宿主写操作"
                )
                if lifecycle_status != "READY":
                    operation.error_code = "PLAN_CONFLICT"
            else:
                operation.dry_run_result = cast(dict[str, Any], safe_metadata(worker_plan))
                operation.error_code = str(
                    worker_plan.get("error", {}).get("code", "WORKER_FAILED")
                )[:64]
                operation.result_summary = "Worker 未能完成规划；未执行宿主写操作"
            record_audit(
                db,
                event_type="worker.plan" if operation_type == "user.plan" else "worker.dry_run",
                actor=context.user.normalized_login,
                actor_role="/".join(sorted(role.name for role in context.user.roles)),
                source_ip=client_ip(request),
                user_agent=user_agent(request),
                object_type="compute_identity_plan",
                object_id="origin-pilot",
                result="DRY_RUN" if worker_plan.get("status") == "DRY_RUN" else "FAILED",
                metadata={
                    "operation_type": operation_type,
                    "plan_status": worker_plan.get(
                        "plan_status", worker_plan.get("stage_status", "UNKNOWN")
                    ),
                },
                operation_id=operation.id,
            )
        except WorkerClientError as exc:
            operation.error_code = exc.code[:64]
            operation.result_summary = "受控 Worker 不可用；未执行宿主写操作"
            operation.dry_run_result = {
                "status": "UNKNOWN",
                "plan_status": "CONFLICT",
                "execution_enabled": False,
                "conflicts": [{"code": exc.code[:64], "message": "Worker 暂不可用"}],
            }
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=None,
            to_status=OperationStatus.DRAFT,
            safe_message="Operation draft created; no host action executed",
            created_at=utcnow(),
        )
    )
    record_audit(
        db,
        event_type="operation.draft",
        actor=context.user.normalized_login,
        actor_role="/".join(sorted(role.name for role in context.user.roles)),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="operation",
        object_id=str(operation.id),
        result="SUCCESS",
        metadata={"operation_type": operation_type, "target_type": body.target_type},
        operation_id=operation.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(
            select(PortalOperation).where(
                PortalOperation.requested_by == context.user.id,
                PortalOperation.idempotency_key == body.idempotency_key,
            )
        )
        if existing:
            return operation_response(existing)
        raise HTTPException(
            status_code=409,
            detail={"code": "IDEMPOTENCY_CONFLICT", "message": "幂等键冲突"},
        ) from exc
    return operation_response(operation)


@router.post("/{operation_id}/submit")
def submit_operation(
    operation_id: str,
    body: OperationSubmitRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("operations.write")),
    db: Session = Depends(get_db),
) -> OperationResponse:
    require_session_csrf(request, context)
    try:
        operation = db.get(PortalOperation, uuid.UUID(operation_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "OPERATION_NOT_FOUND", "message": "任务不存在"}
        ) from exc
    if operation is None:
        raise HTTPException(
            status_code=404, detail={"code": "OPERATION_NOT_FOUND", "message": "任务不存在"}
        )
    if operation.status != OperationStatus.DRAFT:
        raise HTTPException(
            status_code=409,
            detail={"code": "INVALID_OPERATION_STATE", "message": "仅草稿可以提交审批"},
        )
    if operation.operation_type in {"user.stage", "user.activate"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "LIFECYCLE_APPROVAL_GATE_CLOSED",
                "message": "Portal-3B-R 尚停在新的 Stage/Activate 审批 Gate",
            },
        )
    if body.confirmation != operation.target_id:
        raise HTTPException(
            status_code=428,
            detail={"code": "CONFIRMATION_REQUIRED", "message": "请输入准确的对象名确认"},
        )
    if operation.operation_type in HIGH_RISK_OPERATION_TYPES:
        require_recent_reauthentication(context)
    transition(operation, OperationStatus.PENDING_APPROVAL, "submitted for dry-run approval", db)
    record_audit(
        db,
        event_type="operation.submit",
        actor=context.user.normalized_login,
        actor_role="/".join(sorted(role.name for role in context.user.roles)),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="operation",
        object_id=str(operation.id),
        metadata={"operation_type": operation.operation_type, "execution_mode": "dry-run"},
        operation_id=operation.id,
    )
    db.commit()
    return operation_response(operation)


@router.post("/{operation_id}/approval")
def approve_operation(
    operation_id: str,
    body: ApprovalRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    context: AuthContext = Depends(permission_dependency("operations.write")),
    db: Session = Depends(get_db),
) -> OperationResponse:
    require_session_csrf(request, context)
    try:
        operation = db.get(PortalOperation, uuid.UUID(operation_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "OPERATION_NOT_FOUND", "message": "任务不存在"}
        ) from exc
    if operation is None:
        raise HTTPException(
            status_code=404, detail={"code": "OPERATION_NOT_FOUND", "message": "任务不存在"}
        )
    if operation.status != OperationStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail={"code": "INVALID_OPERATION_STATE", "message": "任务不在待审批状态"},
        )
    if body.confirmation != operation.target_id:
        raise HTTPException(
            status_code=428,
            detail={"code": "CONFIRMATION_REQUIRED", "message": "请输入准确的对象名确认"},
        )
    if operation.operation_type in HIGH_RISK_OPERATION_TYPES:
        require_recent_reauthentication(context)
    approval = PortalOperationApproval(
        operation_id=operation.id,
        approver_id=context.user.id,
        decision=body.decision,
        safe_comment=(body.comment or "")[:500],
        decided_at=utcnow(),
    )
    db.add(approval)
    if body.decision == "REJECT":
        transition(operation, OperationStatus.CANCELLED, "approval rejected", db)
    else:
        operation.approved_by = context.user.id
        operation.approved_at = utcnow()
        transition(operation, OperationStatus.APPROVED, "approval granted", db)
        transition(operation, OperationStatus.QUEUED, "queued for controlled Worker dry-run", db)
        background_tasks.add_task(execute_operation, operation.id, context.user.normalized_login)
    record_audit(
        db,
        event_type="operation.approval_simulation",
        actor=context.user.normalized_login,
        actor_role="/".join(sorted(role.name for role in context.user.roles)),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="operation",
        object_id=str(operation.id),
        result=body.decision,
        metadata={"decision": body.decision, "execution_mode": "dry-run"},
        operation_id=operation.id,
    )
    db.commit()
    return operation_response(operation)
