import uuid
from typing import Any, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
from h100_portal_api.enums import AccountState, OnboardingState, OperationStatus
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
    "private_key_password",
    "private_key_path",
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
PORTAL3E_FINAL_APPROVAL_TEXT = "允许按重新验证通过的 Activate 计划激活 origin-pilot"
PORTAL3E_FINAL_APPROVAL_REFERENCE = "portal3e-final-origin-pilot-v1"
PORTAL3E_FINAL_DRY_RUN_OPERATION_ID = uuid.UUID("f677d34a-4ef2-45ec-a323-99e4af148c0e")
PORTAL3E_FINAL_MANAGED_USER_ID = uuid.UUID("3b95b4f0-95d9-444a-8f0b-46288195a807")
PORTAL3E_FINAL_KEY_RECORD_ID = uuid.UUID("7427da72-37b9-4ac2-8ada-2f0c83b7718e")
PORTAL3E_FINAL_KEY_FINGERPRINT = "SHA256:nek6vyEb3GT+UJAcY5y/8PgY4achF2ouNy+8C2JqUVc"
PORTAL3E_FINAL_IDEMPOTENCY_KEY = "portal3e-final-origin-pilot-activate-v1"
PORTAL3E_FINAL_ROLLBACK_IDEMPOTENCY_KEY = "portal3e-final-origin-pilot-rollback-v1"
PORTAL3E_FINAL_APPROVED_HOST = "10.82.36.1"
PORTAL3F_APPROVAL_TEXT = (
    "允许进入 Portal-3F，记录 SSH Client Validation PASS 并执行首个 Slurm/GPU Pilot 验收。"
)
PORTAL3F_APPROVAL_REFERENCE = "portal3f-origin-pilot-first-acceptance-v1"
PORTAL3F_CLIENT_VALIDATION_IDEMPOTENCY_KEY = "portal3f-origin-pilot-client-validation-v1"
# v1 and v2 remain immutable ROLLED_BACK audit records. v3 binds the retry after
# granting the fixed Worker namespace access to the Guard metrics directory.
PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY = "portal3f-origin-pilot-acceptance-v3"
PORTAL3F_IMAGE_REF = (
    "nvcr.io#nvidia/cuda:13.2.0-base-ubuntu24.04@"
    "sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a"
)
APPROVED_PORTAL3F_CLIENT_VALIDATION: dict[str, Any] = {
    "managed_user_id": str(PORTAL3E_FINAL_MANAGED_USER_ID),
    "username": "origin-pilot",
    "expected_compute_state": "ACTIVE",
    "expected_ssh_key_state": "INSTALLED",
    "key_record_id": str(PORTAL3E_FINAL_KEY_RECORD_ID),
    "key_fingerprint": PORTAL3E_FINAL_KEY_FINGERPRINT,
    "host_client_validation": "PASS",
    "container_client_validation": "PASS",
    "confirmation_source": "USER_CONFIRMED_REAL_CLIENT_CONNECTIONS",
    "approval_reference": PORTAL3F_APPROVAL_REFERENCE,
}
APPROVED_PORTAL3F_PILOT_ACCEPTANCE: dict[str, Any] = {
    "managed_user_id": str(PORTAL3E_FINAL_MANAGED_USER_ID),
    "username": "origin-pilot",
    "node_name": "sagsh100server",
    "partition": "notebook",
    "account": "company",
    "qos": "general",
    "max_gpus": 1,
    "container_name": "gpu-dev-origin-pilot",
    "container_gpu": "NONE",
    "image_ref": PORTAL3F_IMAGE_REF,
    "expected_host_client_validation": "PASS",
    "expected_container_client_validation": "PASS",
    "final_node_state": "DRAIN",
    "approval_reference": PORTAL3F_APPROVAL_REFERENCE,
}


class OperationPayloadError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def validate_activate_database_bindings(
    db: Session, *, owner_id: uuid.UUID, target_id: str, payload: dict[str, Any]
) -> list[PortalSshKey]:
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
        if not record.active or record.revoked_at is not None or record.state != "VALIDATED":
            raise OperationPayloadError(
                "PUBLIC_KEY_RECORD_NOT_ACTIVE", "approved SSH key record is not active"
            )
        if record.approved_by is None or record.approved_at is None:
            raise OperationPayloadError(
                "PUBLIC_KEY_RECORD_NOT_APPROVED", "SSH key record has no independent approval"
            )
        if (
            record.validated_at is None
            or record.public_key is None
            or record.enrollment_operation_id is None
            or record.staging_file_name != f"{record.id}.pub"
            or record.content_sha256 is None
            or record.scope not in {"HOST", "CONTAINER", "BOTH"}
            or not record.fingerprint_sha256.startswith("SHA256:")
        ):
            raise OperationPayloadError(
                "PUBLIC_KEY_RECORD_NOT_VALIDATED",
                "SSH key record has no complete self-service enrollment binding",
            )
    ordered = [by_id[key_id] for key_id in key_ids]
    if not any(record.scope in {"HOST", "BOTH"} for record in ordered) or not any(
        record.scope in {"CONTAINER", "BOTH"} for record in ordered
    ):
        raise OperationPayloadError(
            "SSH_KEY_SCOPE_INCOMPLETE", "Activate requires key coverage for host and container"
        )
    return ordered


def validate_activate_worker_result(
    records: list[PortalSshKey], worker_result: dict[str, Any]
) -> None:
    observed = worker_result.get("approved_ssh_keys")
    if not isinstance(observed, list) or len(observed) != len(records):
        raise OperationPayloadError(
            "PUBLIC_KEY_WORKER_MISMATCH", "Worker did not return every approved SSH key"
        )
    by_id = {str(item.get("record_id")): item for item in observed if isinstance(item, dict)}
    for record in records:
        item = by_id.get(str(record.id))
        if (
            item is None
            or item.get("key_type") != record.key_type
            or item.get("fingerprint_sha256") != record.fingerprint_sha256
            or item.get("content_sha256") != record.content_sha256
            or item.get("scope") != record.scope
            or item.get("managed_user_id") != str(record.managed_user_id)
            or item.get("operation_id") != str(record.enrollment_operation_id)
        ):
            raise OperationPayloadError(
                "PUBLIC_KEY_WORKER_MISMATCH",
                "Worker key bytes do not match the approved Portal record",
            )
    expected_host_plan = [
        {
            "record_id": str(record.id),
            "fingerprint_sha256": record.fingerprint_sha256,
        }
        for record in records
        if record.scope in {"HOST", "BOTH"}
    ]
    expected_container_plan = [
        {
            "record_id": str(record.id),
            "fingerprint_sha256": record.fingerprint_sha256,
        }
        for record in records
        if record.scope in {"CONTAINER", "BOTH"}
    ]
    host_ssh_policy = worker_result.get("host_ssh_policy")
    if (
        worker_result.get("host_authorized_keys_install") != "PLANNED"
        or worker_result.get("container_authorized_keys_install") != "PLANNED"
        or worker_result.get("host_authorized_keys_plan") != expected_host_plan
        or worker_result.get("container_authorized_keys_plan") != expected_container_plan
        or worker_result.get("activate_cli_contract") != "TARGET_SCOPED_ROOT_CONTROLLED_BUNDLES"
        or worker_result.get("execution_enabled") is not False
        or not isinstance(host_ssh_policy, dict)
        or host_ssh_policy.get("pubkey_authentication") is not True
        or host_ssh_policy.get("password_authentication") is not False
        or host_ssh_policy.get("keyboard_interactive_authentication") is not False
        or host_ssh_policy.get("authentication_methods") != ["publickey"]
        or host_ssh_policy.get("status") != "PASSING"
    ):
        raise OperationPayloadError(
            "ACTIVATE_DRY_RUN_INCOMPLETE", "Worker Activate plan is incomplete"
        )


def validate_activate_execution_result(
    records: list[PortalSshKey], worker_result: dict[str, Any]
) -> dict[str, Any]:
    """Accept only the complete, host-re-read Portal-3E-FINAL postcondition."""
    observed = worker_result.get("approved_ssh_keys")
    if not isinstance(observed, list) or len(observed) != len(records):
        raise OperationPayloadError(
            "PUBLIC_KEY_WORKER_MISMATCH", "Worker did not return every installed SSH key"
        )
    by_id = {str(item.get("record_id")): item for item in observed if isinstance(item, dict)}
    for record in records:
        item = by_id.get(str(record.id))
        if (
            item is None
            or item.get("key_type") != record.key_type
            or item.get("fingerprint_sha256") != record.fingerprint_sha256
            or item.get("content_sha256") != record.content_sha256
            or item.get("scope") != record.scope
            or item.get("managed_user_id") != str(record.managed_user_id)
            or item.get("operation_id") != str(record.enrollment_operation_id)
        ):
            raise OperationPayloadError(
                "PUBLIC_KEY_WORKER_MISMATCH",
                "Worker installed-key bytes do not match the approved Portal record",
            )
    activate = worker_result.get("activate")
    if not isinstance(activate, dict):
        raise OperationPayloadError(
            "ACTIVATE_RESULT_INCOMPLETE", "Worker returned no structured Activate result"
        )
    expected_host_fingerprints = [
        record.fingerprint_sha256 for record in records if record.scope in {"HOST", "BOTH"}
    ]
    expected_container_fingerprints = [
        record.fingerprint_sha256 for record in records if record.scope in {"CONTAINER", "BOTH"}
    ]
    host_policy = activate.get("host_ssh_policy")
    container_policy = activate.get("container_ssh_policy")
    host_server = activate.get("host_ssh_server")
    container_server = activate.get("container_ssh_server")
    management_policy = activate.get("management_ssh_policy")
    gpu_policy = activate.get("gpu_policy")
    quota = activate.get("quota")
    slurm = activate.get("slurm")
    container = activate.get("container")
    guard = activate.get("guard")
    server_fingerprints = (
        activate.get("host_server_fingerprint"),
        activate.get("container_server_fingerprint"),
    )
    if not (
        worker_result.get("handler") == "user.activate"
        and worker_result.get("execution_enabled") is True
        and activate.get("username") == "origin-pilot"
        and activate.get("uid") == 20001
        and activate.get("gid") == 20001
        and activate.get("onboarding_state") == "ACTIVE"
        and activate.get("shell") == "/bin/bash"
        and activate.get("password") == "LOCKED"
        and activate.get("host_access") == "ENABLED"
        and activate.get("ssh_key_state") == "INSTALLED"
        and activate.get("host_authorized_keys") == "INSTALLED"
        and activate.get("container_authorized_keys") == "INSTALLED"
        and activate.get("host_key_fingerprints") == expected_host_fingerprints
        and activate.get("container_key_fingerprints") == expected_container_fingerprints
        and isinstance(host_policy, dict)
        and host_policy.get("pubkey_authentication") is True
        and host_policy.get("password_authentication") is False
        and host_policy.get("keyboard_interactive_authentication") is False
        and host_policy.get("authentication_methods") == ["publickey"]
        and host_policy.get("status") == "PASSING"
        and isinstance(container_policy, dict)
        and container_policy.get("pubkey_authentication") is True
        and container_policy.get("password_authentication") is False
        and container_policy.get("keyboard_interactive_authentication") is False
        and container_policy.get("authentication_methods") == "publickey"
        and container_policy.get("permit_root_login") == "no"
        and container_policy.get("authorized_keys_file") == ".ssh/authorized_keys"
        and container_policy.get("status") == "PASSING"
        and isinstance(host_server, dict)
        and host_server.get("service_state") == "ACTIVE"
        and host_server.get("config_validation") == "PASSED"
        and host_server.get("approved_address") == PORTAL3E_FINAL_APPROVED_HOST
        and host_server.get("port") == 22
        and host_server.get("status") == "READY_FOR_CLIENT_VALIDATION"
        and isinstance(container_server, dict)
        and container_server.get("service_state") == "ACTIVE"
        and container_server.get("internal_port") == 22
        and container_server.get("status") == "READY_FOR_CLIENT_VALIDATION"
        and isinstance(container_server.get("bind"), dict)
        and container_server["bind"].get("address") == PORTAL3E_FINAL_APPROVED_HOST
        and container_server["bind"].get("port") == 22023
        and container_server["bind"].get("status") == "LISTENING"
        and all(
            isinstance(value, str) and value.startswith("SHA256:") and len(value) <= 128
            for value in server_fingerprints
        )
        and isinstance(management_policy, dict)
        and management_policy.get("origin-al") == "UNCHANGED"
        and management_policy.get("codexops") == "UNCHANGED"
        and isinstance(gpu_policy, dict)
        and gpu_policy.get("unit") == "user-20001.slice"
        and gpu_policy.get("device_policy") == "closed"
        and gpu_policy.get("device_allow") == []
        and gpu_policy.get("status") == "PASSING"
        and gpu_policy.get("out_of_job_gpu") == "DENIED"
        and isinstance(quota, dict)
        and quota.get("project_id") == 30001
        and quota.get("hard_limit_gb") == 300
        and quota.get("enforcement") == "ON"
        and isinstance(slurm, dict)
        and slurm.get("account") == "company"
        and slurm.get("qos") == "general"
        and slurm.get("max_gpus") == 1
        and slurm.get("node_state") == "DRAIN"
        and slurm.get("queue") == "EMPTY"
        and isinstance(container, dict)
        and container.get("name") == "gpu-dev-origin-pilot"
        and container.get("state") == "RUNNING"
        and container.get("gpu") == "NONE"
        and container.get("cpus") == 8
        and container.get("memory_gb") == 32
        and container.get("pids_limit") == 4096
        and container.get("ssh_address") == PORTAL3E_FINAL_APPROVED_HOST
        and container.get("ssh_port") == 22023
        and isinstance(guard, dict)
        and guard.get("status") == "PASSING"
        and guard.get("managed_users") == 1
        and guard.get("users_verified") == 1
        and guard.get("nvidia_gpu_count") == 4
        and guard.get("slurm_gpu_count") == 4
        and activate.get("host_ssh_client_validation") == "PENDING"
        and activate.get("container_ssh_client_validation") == "PENDING"
    ):
        raise OperationPayloadError(
            "ACTIVATE_RESULT_INCOMPLETE", "Worker Activate postcondition differs from approval"
        )
    return activate


def validate_persisted_activate_execution_result(
    records: list[PortalSshKey], persisted_result: dict[str, Any]
) -> dict[str, Any]:
    """Revalidate the audit-safe result after its password field was redacted.

    The raw Worker response is validated before the Operation can become
    SUCCEEDED.  ``safe_metadata`` then deliberately replaces the non-secret
    password lock-state value because its key is named ``password``.  Require
    that exact redaction marker here and restore only the already-validated
    lock-state assertion in a copy used for read-back validation.
    """
    persisted_activate = persisted_result.get("activate")
    if (
        not isinstance(persisted_activate, dict)
        or persisted_activate.get("password") != "[REDACTED]"
    ):
        raise OperationPayloadError(
            "ACTIVATE_RESULT_INCOMPLETE",
            "Persisted Worker result is missing the required password redaction marker",
        )
    normalized_result = {
        **persisted_result,
        "activate": {**persisted_activate, "password": "LOCKED"},
    }
    return validate_activate_execution_result(records, normalized_result)


def _validate_portal3f_server_snapshot(snapshot: object) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise OperationPayloadError(
            "PORTAL3F_SERVER_PREFLIGHT_INCOMPLETE", "Worker returned no server snapshot"
        )
    host_policy = snapshot.get("host_ssh_policy")
    container_policy = snapshot.get("container_ssh_policy")
    host_server = snapshot.get("host_ssh_server")
    container_server = snapshot.get("container_ssh_server")
    management_policy = snapshot.get("management_ssh_policy")
    gpu_policy = snapshot.get("gpu_policy")
    quota = snapshot.get("quota")
    slurm = snapshot.get("slurm")
    container = snapshot.get("container")
    guard = snapshot.get("guard")
    gpu_health_snapshot = snapshot.get("gpu_health")
    if not (
        snapshot.get("username") == "origin-pilot"
        and snapshot.get("uid") == 20001
        and snapshot.get("gid") == 20001
        and snapshot.get("onboarding_state") == "ACTIVE"
        and snapshot.get("shell") == "/bin/bash"
        and snapshot.get("password") == "LOCKED"
        and snapshot.get("ssh_key_state") == "INSTALLED"
        and snapshot.get("host_authorized_keys") == "INSTALLED"
        and snapshot.get("container_authorized_keys") == "INSTALLED"
        and snapshot.get("host_key_fingerprints") == [PORTAL3E_FINAL_KEY_FINGERPRINT]
        and snapshot.get("container_key_fingerprints") == [PORTAL3E_FINAL_KEY_FINGERPRINT]
        and isinstance(host_policy, dict)
        and host_policy.get("pubkey_authentication") is True
        and host_policy.get("password_authentication") is False
        and host_policy.get("keyboard_interactive_authentication") is False
        and host_policy.get("authentication_methods") == ["publickey"]
        and host_policy.get("status") == "PASSING"
        and isinstance(container_policy, dict)
        and container_policy.get("pubkey_authentication") is True
        and container_policy.get("password_authentication") is False
        and container_policy.get("keyboard_interactive_authentication") is False
        and container_policy.get("authentication_methods") == "publickey"
        and container_policy.get("permit_root_login") == "no"
        and container_policy.get("status") == "PASSING"
        and isinstance(host_server, dict)
        and host_server.get("status") == "READY_FOR_CLIENT_VALIDATION"
        and host_server.get("approved_address") == PORTAL3E_FINAL_APPROVED_HOST
        and host_server.get("port") == 22
        and isinstance(container_server, dict)
        and container_server.get("status") == "READY_FOR_CLIENT_VALIDATION"
        and isinstance(container_server.get("bind"), dict)
        and container_server["bind"].get("address") == PORTAL3E_FINAL_APPROVED_HOST
        and container_server["bind"].get("port") == 22023
        and isinstance(management_policy, dict)
        and management_policy.get("origin-al") == "UNCHANGED"
        and management_policy.get("codexops") == "UNCHANGED"
        and isinstance(gpu_policy, dict)
        and gpu_policy.get("unit") == "user-20001.slice"
        and gpu_policy.get("device_policy") == "closed"
        and gpu_policy.get("device_allow") == []
        and gpu_policy.get("status") == "PASSING"
        and gpu_policy.get("out_of_job_gpu") == "DENIED"
        and isinstance(quota, dict)
        and quota.get("project_id") == 30001
        and quota.get("hard_limit_gb") == 300
        and quota.get("enforcement") == "ON"
        and isinstance(slurm, dict)
        and slurm.get("account") == "company"
        and slurm.get("qos") == "general"
        and slurm.get("max_gpus") == 1
        and slurm.get("node_state") == "DRAIN"
        and slurm.get("queue") == "EMPTY"
        and isinstance(container, dict)
        and container.get("name") == "gpu-dev-origin-pilot"
        and container.get("state") == "RUNNING"
        and container.get("gpu") == "NONE"
        and isinstance(guard, dict)
        and guard.get("status") == "PASSING"
        and guard.get("managed_users") == 1
        and guard.get("users_verified") == 1
        and isinstance(gpu_health_snapshot, dict)
        and gpu_health_snapshot.get("count") == 4
        and gpu_health_snapshot.get("mig") == "DISABLED"
        and gpu_health_snapshot.get("dcgm") == "4/4 PASS"
        and gpu_health_snapshot.get("kernel_errors") == "CLEAR"
        and snapshot.get("systemd_failed_units") == 0
    ):
        raise OperationPayloadError(
            "PORTAL3F_SERVER_PREFLIGHT_INCOMPLETE",
            "Worker server snapshot differs from the approved ACTIVE baseline",
        )
    return snapshot


def validate_portal3f_client_validation_result(worker_result: dict[str, Any]) -> dict[str, Any]:
    if not (
        worker_result.get("status") == "SUCCEEDED"
        and worker_result.get("handler") == "user.ssh_client_validation.record"
        and worker_result.get("execution_enabled") is True
        and worker_result.get("username") == "origin-pilot"
        and worker_result.get("host_client_validation") == "PASS"
        and worker_result.get("container_client_validation") == "PASS"
        and worker_result.get("confirmation_source") == "USER_CONFIRMED_REAL_CLIENT_CONNECTIONS"
        and worker_result.get("private_key_handling") == "NOT_ACCESSED"
    ):
        raise OperationPayloadError(
            "PORTAL3F_CLIENT_VALIDATION_RESULT_REJECTED",
            "Worker client-validation result is incomplete",
        )
    return _validate_portal3f_server_snapshot(worker_result.get("server_preflight"))


def validate_portal3f_client_validation_plan(worker_result: dict[str, Any]) -> dict[str, Any]:
    if not (
        worker_result.get("status") == "DRY_RUN"
        and worker_result.get("handler") == "user.ssh_client_validation.record"
        and worker_result.get("execution_enabled") is False
        and worker_result.get("validated_username") == "origin-pilot"
        and worker_result.get("host_client_validation") == "PASS"
        and worker_result.get("container_client_validation") == "PASS"
        and worker_result.get("confirmation_source") == "USER_CONFIRMED_REAL_CLIENT_CONNECTIONS"
        and worker_result.get("private_key_handling") == "NOT_ACCESSED"
        and worker_result.get("slurm_execution") == "NOT_PERFORMED"
    ):
        raise OperationPayloadError(
            "PORTAL3F_CLIENT_VALIDATION_PLAN_REJECTED",
            "Worker client-validation plan is incomplete",
        )
    return _validate_portal3f_server_snapshot(worker_result.get("server_preflight"))


def persist_portal3f_client_validation(
    db: Session,
    *,
    owner: PortalUser,
    operation: PortalOperation,
    worker_result: dict[str, Any],
) -> PortalContainer:
    validate_portal3f_client_validation_result(worker_result)
    managed = db.get(PortalManagedUser, PORTAL3E_FINAL_MANAGED_USER_ID)
    key = db.get(PortalSshKey, PORTAL3E_FINAL_KEY_RECORD_ID)
    container = db.scalar(
        select(PortalContainer).where(
            PortalContainer.managed_user_id == PORTAL3E_FINAL_MANAGED_USER_ID,
            PortalContainer.name == "gpu-dev-origin-pilot",
        )
    )
    if not (
        owner.normalized_login == "origin-al"
        and owner.unix_username == "origin-al"
        and owner.account_state == AccountState.ACTIVE
        and owner.resource_onboarding_state == OnboardingState.ACTIVE
        and managed is not None
        and managed.portal_user_id == owner.id
        and managed.onboarding_state == OnboardingState.ACTIVE
        and managed.shell == "/bin/bash"
        and managed.host_access_state == "ENABLED"
        and managed.ssh_key_state == "INSTALLED"
        and key is not None
        and key.managed_user_id == managed.id
        and key.state == "INSTALLED"
        and key.active is True
        and key.fingerprint_sha256 == PORTAL3E_FINAL_KEY_FINGERPRINT
        and container is not None
        and container.observed_state == "RUNNING"
        and isinstance(container.safe_spec, dict)
        and container.safe_spec.get("gpu") == "NONE"
        and container.safe_spec.get("host_ssh_client_validation") in {"PENDING", "PASS"}
        and container.safe_spec.get("container_ssh_client_validation") in {"PENDING", "PASS"}
        and operation.operation_type == "user.ssh_client_validation.record"
        and operation.target_id == "origin-pilot"
        and operation.idempotency_key == PORTAL3F_CLIENT_VALIDATION_IDEMPOTENCY_KEY
        and operation.validated_payload == APPROVED_PORTAL3F_CLIENT_VALIDATION
        and operation.status == OperationStatus.RUNNING
    ):
        raise OperationPayloadError(
            "PORTAL3F_DATABASE_BINDING_REJECTED",
            "Portal ACTIVE identity no longer matches the approved validation record",
        )
    confirmed_at = utcnow()
    container.safe_spec = {
        **container.safe_spec,
        "host_ssh_client_validation": "PASS",
        "container_ssh_client_validation": "PASS",
        "client_validation_source": "USER_CONFIRMED_REAL_CLIENT_CONNECTIONS",
        "client_validation_operation_id": str(operation.id),
        "client_validation_confirmed_at": confirmed_at.isoformat(),
    }
    return container


def validate_portal3f_pilot_acceptance_result(worker_result: dict[str, Any]) -> dict[str, Any]:
    acceptance = worker_result.get("acceptance")
    client_validation = worker_result.get("client_validation")
    request_id = worker_result.get("request_id")
    if not (
        worker_result.get("status") == "SUCCEEDED"
        and worker_result.get("handler") == "user.pilot.acceptance"
        and worker_result.get("execution_enabled") is True
        and worker_result.get("username") == "origin-pilot"
        and worker_result.get("approval_reference") == PORTAL3F_APPROVAL_REFERENCE
        and client_validation == {"host": "PASS", "container": "PASS"}
        and isinstance(request_id, str)
        and isinstance(acceptance, dict)
        and isinstance(acceptance.get("cpu_job_id"), int)
        and acceptance.get("cpu_job_id", 0) > 0
        and isinstance(acceptance.get("gpu_job_id"), int)
        and acceptance.get("gpu_job_id", 0) > 0
        and acceptance.get("cpu_job_id") != acceptance.get("gpu_job_id")
        and isinstance(acceptance.get("allocated_gpu_uuid"), str)
        and acceptance["allocated_gpu_uuid"].startswith("GPU-")
        and acceptance.get("out_of_job_gpu_open") == "DENIED"
        and acceptance.get("out_of_job_cuda_context") == "DENIED"
        and acceptance.get("in_job_allocated_gpu") == "ALLOWED"
        and acceptance.get("in_job_unallocated_gpus") == "DENIED"
        and acceptance.get("in_job_cuda_context") == "PASSED"
        and acceptance.get("final_node_state") == "DRAIN"
        and acceptance.get("worker_log_dir")
        == f"/srv/gpu-platform/platform/logs/portal3f-worker-{request_id}"
        and worker_result.get("rollback_status") == "NOT_REQUIRED"
    ):
        raise OperationPayloadError(
            "PORTAL3F_ACCEPTANCE_RESULT_REJECTED",
            "Worker Pilot acceptance result is incomplete",
        )
    _validate_portal3f_server_snapshot(worker_result.get("preflight"))
    _validate_portal3f_server_snapshot(worker_result.get("postflight"))
    return acceptance


def validate_portal3f_pilot_acceptance_plan(worker_result: dict[str, Any]) -> dict[str, Any]:
    expected_tests = [
        "CPU_JOB",
        "SINGLE_GPU_PYXIS_ENROOT",
        "IN_JOB_ALLOCATED_GPU_ALLOW",
        "IN_JOB_UNALLOCATED_GPU_DENY",
        "IN_JOB_CUDA_CONTEXT",
        "OUT_OF_JOB_GPU_OPEN_DENY_CONCURRENT",
        "OUT_OF_JOB_CUDA_CONTEXT_DENY_CONCURRENT",
    ]
    if not (
        worker_result.get("status") == "DRY_RUN"
        and worker_result.get("handler") == "user.pilot.acceptance"
        and worker_result.get("execution_enabled") is False
        and worker_result.get("validated_username") == "origin-pilot"
        and worker_result.get("node_name") == "sagsh100server"
        and worker_result.get("partition") == "notebook"
        and worker_result.get("account") == "company"
        and worker_result.get("qos") == "general"
        and worker_result.get("max_gpus") == 1
        and worker_result.get("image_ref") == PORTAL3F_IMAGE_REF
        and worker_result.get("tests") == expected_tests
        and worker_result.get("final_node_state") == "DRAIN"
    ):
        raise OperationPayloadError(
            "PORTAL3F_ACCEPTANCE_PLAN_REJECTED", "Worker Pilot acceptance plan is incomplete"
        )
    return _validate_portal3f_server_snapshot(worker_result.get("preflight"))


def persist_portal3f_pilot_acceptance(
    db: Session,
    *,
    owner: PortalUser,
    operation: PortalOperation,
    worker_result: dict[str, Any],
) -> PortalContainer:
    acceptance = validate_portal3f_pilot_acceptance_result(worker_result)
    managed = db.get(PortalManagedUser, PORTAL3E_FINAL_MANAGED_USER_ID)
    container = db.scalar(
        select(PortalContainer).where(
            PortalContainer.managed_user_id == PORTAL3E_FINAL_MANAGED_USER_ID,
            PortalContainer.name == "gpu-dev-origin-pilot",
        )
    )
    if not (
        owner.normalized_login == "origin-al"
        and managed is not None
        and managed.portal_user_id == owner.id
        and managed.onboarding_state == OnboardingState.ACTIVE
        and container is not None
        and isinstance(container.safe_spec, dict)
        and container.safe_spec.get("host_ssh_client_validation") == "PASS"
        and container.safe_spec.get("container_ssh_client_validation") == "PASS"
        and container.safe_spec.get("gpu") == "NONE"
        and operation.operation_type == "user.pilot.acceptance"
        and operation.target_id == "origin-pilot"
        and operation.idempotency_key == PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY
        and operation.validated_payload == APPROVED_PORTAL3F_PILOT_ACCEPTANCE
        and operation.status == OperationStatus.RUNNING
    ):
        raise OperationPayloadError(
            "PORTAL3F_DATABASE_BINDING_REJECTED",
            "Portal client validation or ACTIVE identity changed during acceptance",
        )
    accepted_at = utcnow()
    container.safe_spec = {
        **container.safe_spec,
        "pilot_acceptance_status": "PASSED",
        "pilot_acceptance_operation_id": str(operation.id),
        "pilot_acceptance_accepted_at": accepted_at.isoformat(),
        "pilot_cpu_job_id": acceptance["cpu_job_id"],
        "pilot_gpu_job_id": acceptance["gpu_job_id"],
        "pilot_allocated_gpu_uuid": acceptance["allocated_gpu_uuid"],
        "pilot_image_ref": PORTAL3F_IMAGE_REF,
        "pilot_final_node_state": "DRAIN",
        "slurm_node_state": "DRAIN",
        "slurm_queue": "EMPTY",
        "gpu_scheduling_available": False,
    }
    return container


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
    if operation_type in {"user.ssh_client_validation.record", "user.pilot.acceptance"}:
        if set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
            raise OperationPayloadError(
                "PORTAL3F_PAYLOAD_REJECTED",
                "secret, command, argv, and path fields are forbidden",
            )
        expected = (
            APPROVED_PORTAL3F_CLIENT_VALIDATION
            if operation_type == "user.ssh_client_validation.record"
            else APPROVED_PORTAL3F_PILOT_ACCEPTANCE
        )
        if payload != expected:
            raise OperationPayloadError(
                "PORTAL3F_PLAN_MISMATCH",
                "Portal-3F payload differs from the fixed approved plan",
            )
        return dict(expected)
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
            return UserActivatePayload.model_validate(payload).model_dump(
                mode="json", exclude_none=True
            )
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


def is_portal3e_final_real_activate(
    db: Session,
    operation: PortalOperation,
    requester: PortalUser | None,
    approver: PortalUser | None,
) -> bool:
    """Recognize only the administrator-approved Portal-3E-FINAL transaction."""
    if (
        operation.operation_type != "user.activate"
        or operation.target_type != "compute_identity"
        or operation.target_id != "origin-pilot"
        or operation.idempotency_key != PORTAL3E_FINAL_IDEMPOTENCY_KEY
        or requester is None
        or approver is None
        or requester.id != approver.id
        or requester.normalized_login != "origin-al"
        or approver.normalized_login != "origin-al"
        or requester.unix_username != "origin-al"
        or requester.account_state.value != "ACTIVE"
        or not any(role.name == "platform_owner" for role in requester.roles)
    ):
        return False
    try:
        validated = validate_operation_payload("user.activate", operation.validated_payload)
    except ValueError, OperationPayloadError:
        return False
    expected_payload = {
        "managed_user_id": str(PORTAL3E_FINAL_MANAGED_USER_ID),
        "approved_ssh_key_record_ids": [str(PORTAL3E_FINAL_KEY_RECORD_ID)],
        "expected_state": "STAGED",
        "approval_reference": PORTAL3E_FINAL_APPROVAL_REFERENCE,
        "dry_run_operation_id": str(PORTAL3E_FINAL_DRY_RUN_OPERATION_ID),
    }
    if validated != expected_payload or operation.validated_payload != expected_payload:
        return False
    approval = db.scalar(
        select(PortalOperationApproval).where(
            PortalOperationApproval.operation_id == operation.id,
            PortalOperationApproval.approver_id == requester.id,
            PortalOperationApproval.decision == "APPROVE",
        )
    )
    dry_run = db.get(PortalOperation, PORTAL3E_FINAL_DRY_RUN_OPERATION_ID)
    dry_result = dry_run.dry_run_result if dry_run is not None else None
    return bool(
        approval is not None
        and approval.safe_comment == PORTAL3E_FINAL_APPROVAL_TEXT
        and dry_run is not None
        and dry_run.operation_type == "user.activate"
        and dry_run.target_type == "compute_identity"
        and dry_run.target_id == "origin-pilot"
        and dry_run.requested_by == requester.id
        and dry_run.status == OperationStatus.DRAFT
        and dry_run.approved_by is None
        and dry_run.validated_payload
        == {
            "managed_user_id": str(PORTAL3E_FINAL_MANAGED_USER_ID),
            "approved_ssh_key_record_ids": [str(PORTAL3E_FINAL_KEY_RECORD_ID)],
            "expected_state": "STAGED",
            "approval_reference": "portal3e-r-host-ssh-policy-v1",
        }
        and isinstance(dry_result, dict)
        and dry_result.get("status") == "DRY_RUN"
        and dry_result.get("activate_status") == "READY"
        and dry_result.get("execution_enabled") is False
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


def persist_portal3e_activated_identity(
    db: Session,
    *,
    owner: PortalUser,
    operation: PortalOperation,
    worker_result: dict[str, Any],
) -> PortalManagedUser:
    """Persist ACTIVE only after both SSH targets and every server Gate pass."""
    records = validate_activate_database_bindings(
        db,
        owner_id=owner.id,
        target_id="origin-pilot",
        payload=operation.validated_payload,
    )
    activate = validate_activate_execution_result(records, worker_result)
    managed = db.get(PortalManagedUser, PORTAL3E_FINAL_MANAGED_USER_ID)
    if (
        managed is None
        or managed.portal_user_id != owner.id
        or managed.unix_username != "origin-pilot"
        or managed.uid != 20001
        or managed.gid != 20001
        or managed.shell != "/usr/sbin/nologin"
        or managed.onboarding_state != OnboardingState.STAGED
        or managed.ssh_key_state not in {"REQUIRED_BEFORE_ACTIVATION", "VALIDATED"}
        or managed.project_id != 30001
        or managed.quota_bytes != 300 * 1024**3
        or managed.slurm_account != "company"
        or managed.slurm_qos != "general"
        or managed.container_name != "gpu-dev-origin-pilot"
        or managed.container_port != 22023
        or owner.unix_username != "origin-al"
    ):
        raise RuntimeError("Portal managed identity changed before Activate persistence")
    container = db.scalar(
        select(PortalContainer).where(
            PortalContainer.managed_user_id == managed.id,
            PortalContainer.name == "gpu-dev-origin-pilot",
        )
    )
    if container is None or container.ssh_port != 22023:
        raise RuntimeError("Portal container binding changed before Activate persistence")
    if (
        len(records) != 1
        or records[0].id != PORTAL3E_FINAL_KEY_RECORD_ID
        or records[0].fingerprint_sha256 != PORTAL3E_FINAL_KEY_FINGERPRINT
        or records[0].key_type != "ssh-ed25519"
        or records[0].scope != "BOTH"
    ):
        raise RuntimeError("Portal approved SSH key changed before Activate persistence")

    now = utcnow()
    for record in records:
        record.state = "INSTALLED"
        record.installed_at = now
    managed.shell = "/bin/bash"
    managed.host_access_state = "ENABLED"
    managed.gpu_isolation_state = "VERIFIED"
    managed.onboarding_state = OnboardingState.ACTIVE
    managed.ssh_key_state = "INSTALLED"
    managed.ssh_key_count = len(records)
    managed.compute_activated_at = now
    owner.resource_onboarding_state = OnboardingState.ACTIVE

    host_server_fingerprint = str(activate["host_server_fingerprint"])
    container_server_fingerprint = str(activate["container_server_fingerprint"])
    container.desired_state = "RUNNING"
    container.observed_state = "RUNNING"
    container.safe_spec = {
        "cpus": 8,
        "memory_gb": 32,
        "pids_limit": 4096,
        "gpu": "NONE",
        "privileged": False,
        "host_network": False,
        "host_pid": False,
        "host_ipc": False,
        "docker_socket": False,
        "munge": False,
        "password_authentication": False,
        "keyboard_interactive_authentication": False,
        "pubkey_authentication": True,
        "authentication_methods": "publickey",
        "root_login": False,
        "authorized_keys": "INSTALLED",
        "host_authorized_keys": "INSTALLED",
        "container_authorized_keys": "INSTALLED",
        "user_key_fingerprints": [record.fingerprint_sha256 for record in records],
        "host_server_fingerprint": host_server_fingerprint,
        "container_server_fingerprint": container_server_fingerprint,
        "approved_host": PORTAL3E_FINAL_APPROVED_HOST,
        "host_ssh_port": 22,
        "container_ssh_port": 22023,
        "container_ssh_bind": f"{PORTAL3E_FINAL_APPROVED_HOST}:22023",
        "host_ssh_server": "READY_FOR_CLIENT_VALIDATION",
        "container_ssh_server": "READY_FOR_CLIENT_VALIDATION",
        "host_ssh_client_validation": "PENDING",
        "container_ssh_client_validation": "PENDING",
        "host_ssh_policy": activate["host_ssh_policy"],
        "container_ssh_policy": activate["container_ssh_policy"],
        "guard": "PASSING",
        "gpu_open_probe": "DENIED",
        "cuda_context_probe": "DENIED",
        "max_gpus": 1,
        "slurm_node_state": "DRAIN",
        "slurm_queue": "EMPTY",
        "gpu_scheduling_available": False,
    }

    audit_events: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (
            "user.activate.approval",
            "operation",
            {
                "actor": "Origin-al",
                "dry_run_operation_id": str(PORTAL3E_FINAL_DRY_RUN_OPERATION_ID),
            },
        ),
        (
            "user.activate.host_key_install",
            "ssh_key",
            {
                "key_record_id": str(records[0].id),
                "fingerprint": records[0].fingerprint_sha256,
                "status": "PASS",
            },
        ),
        (
            "user.activate.container_key_install",
            "ssh_key",
            {
                "key_record_id": str(records[0].id),
                "fingerprint": records[0].fingerprint_sha256,
                "status": "PASS",
            },
        ),
        ("user.activate.shell", "managed_user", {"from": "nologin", "to": "/bin/bash"}),
        (
            "user.activate.container_start",
            "container",
            {"state": "RUNNING", "gpu": "NONE", "bind": "10.82.36.1:22023"},
        ),
        (
            "user.activate.host_ssh_server",
            "ssh_server",
            {"status": "READY_FOR_CLIENT_VALIDATION", "fingerprint": host_server_fingerprint},
        ),
        (
            "user.activate.container_ssh_server",
            "ssh_server",
            {
                "status": "READY_FOR_CLIENT_VALIDATION",
                "fingerprint": container_server_fingerprint,
            },
        ),
        (
            "user.activate.completed",
            "managed_user",
            {"state": "ACTIVE", "client_validation": "PENDING", "slurm_node": "DRAIN"},
        ),
    )
    for event_type, object_type, metadata in audit_events:
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


def _attempt_portal3e_activate_rollback() -> dict[str, Any]:
    payload = {
        "managed_user_id": str(PORTAL3E_FINAL_MANAGED_USER_ID),
        "approved_ssh_key_record_ids": [str(PORTAL3E_FINAL_KEY_RECORD_ID)],
        "expected_state": "STAGED",
        "approval_reference": PORTAL3E_FINAL_APPROVAL_REFERENCE,
        "dry_run_operation_id": str(PORTAL3E_FINAL_DRY_RUN_OPERATION_ID),
    }
    try:
        return call_worker(
            "user.activate.rollback",
            payload=payload,
            requested_by="origin-al",
            approved_by="origin-al",
            idempotency_key=PORTAL3E_FINAL_ROLLBACK_IDEMPOTENCY_KEY,
            dry_run=False,
            timeout_seconds=240,
        )
    except WorkerClientError as exc:
        return {
            "status": "ERROR",
            "error": {"code": exc.code, "message": "controlled Activate rollback unavailable"},
            "rollback_status": "REQUIRES_MANUAL_REVIEW",
        }


def _finish_portal3e_operation_event(db: Session, operation: PortalOperation) -> None:
    operation.finished_at = utcnow()
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=operation.status,
            to_status=operation.status,
            safe_message="Portal-3E-FINAL Worker execution recorded",
            created_at=utcnow(),
        )
    )


def _execute_portal3e_final_activate(
    db: Session,
    *,
    operation: PortalOperation,
    requester: PortalUser,
    approver: PortalUser,
    actor_login: str,
) -> None:
    transition(operation, OperationStatus.RUNNING, "Controlled Worker Activate started", db)
    operation.started_at = utcnow()
    db.commit()
    try:
        result = call_worker(
            "user.activate",
            payload=operation.validated_payload,
            requested_by=requester.normalized_login,
            approved_by=approver.normalized_login,
            idempotency_key=operation.idempotency_key,
            dry_run=False,
            timeout_seconds=360,
        )
    except WorkerClientError as exc:
        rollback = _attempt_portal3e_activate_rollback()
        recovered_operation = db.get(PortalOperation, operation.id)
        if recovered_operation is None or recovered_operation.status != OperationStatus.RUNNING:
            return
        if (
            rollback.get("status") == "SUCCEEDED"
            and rollback.get("rollback_status") == "ROLLED_BACK"
        ):
            transition(
                recovered_operation, OperationStatus.ROLLING_BACK, "Worker recovery started", db
            )
            transition(
                recovered_operation, OperationStatus.ROLLED_BACK, "Worker recovery verified", db
            )
            recovered_operation.rollback_status = "ROLLED_BACK"
        else:
            transition(
                recovered_operation,
                OperationStatus.FAILED,
                "Worker unavailable during Activate",
                db,
            )
            recovered_operation.rollback_status = "REQUIRES_MANUAL_REVIEW"
        recovered_operation.error_code = exc.code[:64]
        recovered_operation.result_summary = (
            "Activate Worker 通信失败；受控回滚已验证，origin-pilot 保持 STAGED"
            if recovered_operation.rollback_status == "ROLLED_BACK"
            else "Activate Worker 通信失败；保持 Slurm DRAIN 并人工核查"
        )
        record_audit(
            db,
            event_type="user.activate.worker_unavailable",
            actor=actor_login,
            actor_role="platform_owner",
            source_ip="local-worker-socket",
            user_agent="h100-portal-api",
            object_type="operation",
            object_id=str(recovered_operation.id),
            result="FAILED",
            metadata={
                "error_code": recovered_operation.error_code,
                "rollback": recovered_operation.rollback_status,
            },
            operation_id=recovered_operation.id,
        )
        _finish_portal3e_operation_event(db, recovered_operation)
        db.commit()
        return

    if result.get("status") != "SUCCEEDED":
        rollback_status = str(result.get("rollback_status", ""))[:32]
        if rollback_status == "ROLLED_BACK":
            transition(operation, OperationStatus.ROLLING_BACK, "Worker rollback recorded", db)
            transition(operation, OperationStatus.ROLLED_BACK, "Worker rollback verified", db)
        else:
            transition(operation, OperationStatus.FAILED, "Worker rejected Activate", db)
        operation.rollback_status = rollback_status or None
        error = result.get("error", {})
        operation.error_code = str(
            error.get("code", "ACTIVATE_WORKER_FAILED")
            if isinstance(error, dict)
            else "ACTIVATE_WORKER_FAILED"
        )[:64]
        operation.result_summary = (
            "Activate 失败并已恢复 STAGED；两处 authorized_keys 缺失且容器停止"
            if rollback_status == "ROLLED_BACK"
            else "Activate 在写前或恢复验证期间失败；保持 Slurm DRAIN"
        )
        operation.worker_execution_id = str(result.get("request_id", "worker"))[:64]
        record_audit(
            db,
            event_type="user.activate.failed",
            actor=actor_login,
            actor_role="platform_owner",
            source_ip="local-worker-socket",
            user_agent="h100-portal-api",
            object_type="operation",
            object_id=str(operation.id),
            result="FAILED",
            metadata={"error_code": operation.error_code, "rollback": rollback_status or "NONE"},
            operation_id=operation.id,
        )
        _finish_portal3e_operation_event(db, operation)
        db.commit()
        return

    try:
        persist_portal3e_activated_identity(
            db,
            owner=requester,
            operation=operation,
            worker_result=result,
        )
        previous_plan = operation.dry_run_result or {}
        operation.dry_run_result = {
            **previous_plan,
            "referenced_dry_run_operation_id": str(PORTAL3E_FINAL_DRY_RUN_OPERATION_ID),
            "execution_result": cast(dict[str, Any], safe_metadata(result)),
            "activate_status": "ACTIVE",
            "execution_enabled": True,
        }
        transition(operation, OperationStatus.SUCCEEDED, "origin-pilot Activate verified", db)
        operation.result_summary = (
            "origin-pilot 已 ACTIVE；Host/Container 公钥已安装；容器 RUNNING/GPU NONE；"
            "Client Validation PENDING；Slurm DRAIN"
        )
        operation.worker_execution_id = str(result.get("request_id", "worker"))[:64]
        operation.rollback_status = "NOT_REQUIRED"
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
            metadata={
                "operation_type": "user.activate",
                "execution_mode": "portal3e-final-real-activate",
                "client_validation": "PENDING",
                "slurm_node": "DRAIN",
            },
            operation_id=operation.id,
        )
        _finish_portal3e_operation_event(db, operation)
        db.flush()
        db.commit()
        return
    except (OperationPayloadError, RuntimeError, SQLAlchemyError) as exc:
        operation_id = operation.id
        db.rollback()
        recovered_operation = db.get(PortalOperation, operation_id)
        # A database commit can report an ambiguous transport failure after the
        # transaction became durable.  Never roll back a host whose Portal
        # Operation is already durably SUCCEEDED.
        if recovered_operation is None or recovered_operation.status == OperationStatus.SUCCEEDED:
            return
        if recovered_operation.status != OperationStatus.RUNNING:
            return
        rollback = _attempt_portal3e_activate_rollback()
        if (
            rollback.get("status") == "SUCCEEDED"
            and rollback.get("rollback_status") == "ROLLED_BACK"
        ):
            transition(
                recovered_operation,
                OperationStatus.ROLLING_BACK,
                "Portal persistence rollback",
                db,
            )
            transition(
                recovered_operation,
                OperationStatus.ROLLED_BACK,
                "Host rollback verified",
                db,
            )
            recovered_operation.rollback_status = "ROLLED_BACK"
            summary = "Portal ACTIVE 持久化失败；宿主已恢复 STAGED"
        else:
            transition(
                recovered_operation,
                OperationStatus.FAILED,
                "Portal persistence rejected",
                db,
            )
            recovered_operation.rollback_status = "REQUIRES_MANUAL_REVIEW"
            summary = "Portal ACTIVE 持久化失败；保持 Slurm DRAIN 并人工核查"
        recovered_operation.error_code = "PORTAL_ACTIVATE_STATE_REJECTED"
        recovered_operation.result_summary = summary
        record_audit(
            db,
            event_type="portal.activate_persist_failed",
            actor=actor_login,
            actor_role="platform_owner",
            source_ip="local-worker-socket",
            user_agent="h100-portal-api",
            object_type="operation",
            object_id=str(recovered_operation.id),
            result="FAILED",
            metadata={
                "detail": (
                    "database integrity constraint rejected Portal Activate state"
                    if isinstance(exc, IntegrityError)
                    else str(exc)[:255]
                ),
                "rollback": recovered_operation.rollback_status,
            },
            operation_id=recovered_operation.id,
        )
        _finish_portal3e_operation_event(db, recovered_operation)
        db.commit()


def execute_operation(operation_id: uuid.UUID, actor_login: str) -> None:
    with SessionLocal() as db:
        operation = db.get(PortalOperation, operation_id)
        if operation is None or operation.status != OperationStatus.QUEUED:
            return
        requester = db.get(PortalUser, operation.requested_by)
        approver = db.get(PortalUser, operation.approved_by) if operation.approved_by else None
        real_stage = is_portal3c_real_stage(operation, requester, approver)
        real_activate = is_portal3e_final_real_activate(db, operation, requester, approver)
        if real_activate and requester is not None and approver is not None:
            _execute_portal3e_final_activate(
                db,
                operation=operation,
                requester=requester,
                approver=approver,
                actor_login=actor_login,
            )
            return
        if (
            operation.operation_type == "user.activate"
            and operation.idempotency_key == PORTAL3E_FINAL_IDEMPOTENCY_KEY
        ):
            transition(operation, OperationStatus.RUNNING, "Activate approval binding check", db)
            transition(operation, OperationStatus.FAILED, "Activate approval binding rejected", db)
            operation.started_at = utcnow()
            operation.finished_at = utcnow()
            operation.error_code = "ACTIVATE_APPROVAL_BINDING_REJECTED"
            operation.result_summary = "Portal-3E-FINAL 审批或 dry-run 绑定发生变化；未执行宿主写入"
            db.commit()
            return
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
    if operation_type in {"user.ssh_client_validation.record", "user.pilot.acceptance"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PORTAL3F_FIXED_CONSOLE_ONLY",
                "message": "Portal-3F 首次验收仅允许固定管理员流程执行",
            },
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
    activate_records: list[PortalSshKey] = []
    if operation_type == "user.activate":
        try:
            activate_records = validate_activate_database_bindings(
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
                if operation_type == "user.activate":
                    try:
                        validate_activate_worker_result(activate_records, worker_plan)
                    except OperationPayloadError as exc:
                        db.rollback()
                        raise HTTPException(
                            status_code=409,
                            detail={"code": exc.code, "message": str(exc)},
                        ) from exc
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
