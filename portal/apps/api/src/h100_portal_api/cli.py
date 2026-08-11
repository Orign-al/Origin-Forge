import argparse
import pwd
import sys
import time
import uuid
from datetime import timedelta
from typing import Any, cast

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.config import get_settings
from h100_portal_api.database import SessionLocal
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordActionPurpose,
    PasswordActionTokenState,
    PasswordState,
    RiskLevel,
)
from h100_portal_api.lease_service import create_lease
from h100_portal_api.models import (
    PortalComputeLease,
    PortalContainer,
    PortalJob,
    PortalManagedUser,
    PortalOperation,
    PortalOperationApproval,
    PortalOperationEvent,
    PortalPasswordCredential,
    PortalPasswordSetupToken,
    PortalRole,
    PortalSession,
    PortalSetting,
    PortalSshKey,
    PortalStorageResource,
    PortalUser,
    utcnow,
)
from h100_portal_api.rbac import PERMISSIONS
from h100_portal_api.routes.operations import (
    APPROVED_ORIGIN_PILOT_STAGE,
    APPROVED_PORTAL3F_CLIENT_VALIDATION,
    APPROVED_PORTAL3F_PILOT_ACCEPTANCE,
    APPROVED_PORTAL3G_PRODUCTION_PILOT,
    PORTAL3C_STAGE_APPROVAL_REFERENCE,
    PORTAL3C_STAGE_IDEMPOTENCY_KEY,
    PORTAL3E_FINAL_APPROVAL_REFERENCE,
    PORTAL3E_FINAL_APPROVAL_TEXT,
    PORTAL3E_FINAL_DRY_RUN_OPERATION_ID,
    PORTAL3E_FINAL_IDEMPOTENCY_KEY,
    PORTAL3E_FINAL_KEY_FINGERPRINT,
    PORTAL3E_FINAL_KEY_RECORD_ID,
    PORTAL3E_FINAL_MANAGED_USER_ID,
    PORTAL3F_APPROVAL_TEXT,
    PORTAL3F_CLIENT_VALIDATION_IDEMPOTENCY_KEY,
    PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY,
    PORTAL3G_APPROVAL_REFERENCE,
    PORTAL3G_APPROVAL_TEXT,
    PORTAL3G_CLIENT_VALIDATION_OPERATION_ID,
    PORTAL3G_DRAIN_IDEMPOTENCY_KEY,
    PORTAL3G_PILOT_ACCEPTANCE_OPERATION_ID,
    PORTAL3G_PRODUCTION_PILOT_IDEMPOTENCY_KEY,
    PORTAL3G_SETTING_KEY,
    OperationPayloadError,
    enrich_user_plan_with_portal_state,
    execute_operation,
    persist_portal3f_client_validation,
    persist_portal3f_pilot_acceptance,
    persist_portal3g_production_pilot,
    transition,
    validate_activate_database_bindings,
    validate_activate_worker_result,
    validate_operation_payload,
    validate_persisted_activate_execution_result,
    validate_portal3f_client_validation_plan,
    validate_portal3f_client_validation_result,
    validate_portal3f_pilot_acceptance_plan,
    validate_portal3f_pilot_acceptance_result,
    validate_portal3g_production_pilot_plan,
    validate_portal3g_production_pilot_result,
)
from h100_portal_api.security import (
    digest_secret,
    hash_password,
    normalize_login,
    random_token,
    safe_metadata,
    validate_password,
)
from h100_portal_api.worker_client import WorkerClientError, call_worker

ROLE_DESCRIPTIONS = {
    "platform_owner": "网页平台所有者",
    "platform_admin": "平台管理员",
    "operator": "日常运维操作员",
    "auditor": "只读审计员",
    "user": "普通用户",
}
PORTAL3C_APPROVAL_TEXT = "允许使用修订并重新验收通过的两阶段流程 Stage 独立计算用户 origin-pilot"
PORTAL3ER_APPROVAL_REFERENCE = "portal3e-r-host-ssh-policy-v1"
PORTAL3ER_KEY_RECORD_ID = uuid.UUID("7427da72-37b9-4ac2-8ada-2f0c83b7718e")
PORTAL3ER_KEY_FINGERPRINT = "SHA256:nek6vyEb3GT+UJAcY5y/8PgY4achF2ouNy+8C2JqUVc"
PORTAL4A_FINAL_CREDENTIAL_SETTING_KEY = "portal4a.final_ordinary_user_credential"
PORTAL4A_LOCAL_API_BASE = "http://127.0.0.1:18081/api/v1"
PORTAL4A_BROWSER_ORIGIN = "http://127.0.0.1:18080"
PORTAL4A_CPU_GATE_SCRIPT = "workspace/portal4a-cpu-gate.sh"
PORTAL4A_GPU_GATE_SCRIPT = "workspace/portal4a-gpu-gate.sh"
PORTAL4A_APPROVED_IMAGE = (
    "nvcr.io#nvidia/cuda:13.2.0-base-ubuntu24.04@"
    "sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a"
)


def ensure_roles(db: Session) -> None:
    for name, permissions in PERMISSIONS.items():
        role = db.scalar(select(PortalRole).where(PortalRole.name == name))
        if role is None:
            db.add(
                PortalRole(
                    name=name,
                    description=ROLE_DESCRIPTIONS.get(name, name),
                    permissions=sorted(permissions),
                )
            )
        else:
            role.permissions = sorted(permissions)
    db.flush()


def ensure_origin_al_record(db: Session) -> tuple[PortalUser, bool]:
    normalized = normalize_login("origin-al")
    ensure_roles(db)
    user = db.scalar(select(PortalUser).where(PortalUser.normalized_login == normalized))
    created = user is None
    if user is None:
        user = PortalUser(
            login_name="Origin-al",
            normalized_login=normalized,
            display_name="Origin-al",
            unix_username="origin-al",
            account_state=AccountState.INVITED,
            password_state=PasswordState.SETUP_REQUIRED,
            resource_onboarding_state=OnboardingState.NOT_ENROLLED,
        )
        db.add(user)
        db.flush()
    if user.unix_username != "origin-al":
        raise RuntimeError("existing Origin-al account is mapped to a different Linux user")
    owner = db.scalar(select(PortalRole).where(PortalRole.name == "platform_owner"))
    if owner is None:
        raise RuntimeError("platform_owner role is missing")
    if owner not in user.roles:
        user.roles.append(owner)
    if user.account_state == AccountState.DECOMMISSIONED:
        raise RuntimeError("Origin-al account is decommissioned")
    return user, created


def linux_origin_al_exists() -> bool:
    try:
        pwd.getpwnam("origin-al")
    except KeyError:
        print("ORIGIN-AL BOOTSTRAP BLOCKED — Linux user origin-al does not exist", file=sys.stderr)
        return False
    return True


def prepare_origin_al() -> int:
    if not linux_origin_al_exists():
        return 2
    with SessionLocal() as db:
        user, created = ensure_origin_al_record(db)
        record_audit(
            db,
            event_type="portal_owner.prepare",
            actor="local-bootstrap",
            actor_role="bootstrap",
            source_ip="local-console",
            user_agent="h100-portal-admin",
            object_type="portal_user",
            object_id=str(user.id),
            metadata={"record_action": "created" if created else "verified"},
        )
        db.commit()
        outcome = "created" if created else "verified"
        print(f"Origin-al portal account {outcome}; no setup token generated.")
        print(
            f"account_state={user.account_state} password_state={user.password_state} "
            f"resource_onboarding_state={user.resource_onboarding_state} role=platform_owner"
        )
    return 0


def bootstrap_origin_al(reset: bool, base_url: str) -> int:
    if not linux_origin_al_exists():
        return 2
    with SessionLocal() as db:
        user, created = ensure_origin_al_record(db)
        if not created and not reset:
            db.commit()
            print("Origin-al portal account already exists; setup token not regenerated.")
            print(
                f"account_state={user.account_state} password_state={user.password_state} "
                "role=platform_owner"
            )
            return 0
        if reset:
            db.execute(
                update(PortalPasswordSetupToken)
                .where(
                    PortalPasswordSetupToken.user_id == user.id,
                    PortalPasswordSetupToken.used_at.is_(None),
                )
                .values(
                    revoked_at=utcnow(),
                    state=PasswordActionTokenState.REVOKED,
                    challenge_hash=None,
                    challenge_expires_at=None,
                )
            )
        token = random_token(48)
        db.add(
            PortalPasswordSetupToken(
                user_id=user.id,
                token_hash=digest_secret(token),
                purpose=PasswordActionPurpose.INITIAL_PASSWORD_SETUP,
                state=PasswordActionTokenState.ACTIVE,
                created_at=utcnow(),
                expires_at=utcnow()
                + timedelta(hours=get_settings().initial_password_setup_token_hours),
            )
        )
        if created:
            user.account_state = AccountState.INVITED
            user.password_state = PasswordState.SETUP_REQUIRED
        record_audit(
            db,
            event_type="password.setup_link.issue",
            actor="local-bootstrap",
            actor_role="bootstrap",
            source_ip="local-console",
            user_agent="h100-portal-admin",
            object_type="portal_user",
            object_id=str(user.id),
            metadata={"replaced_previous_link": reset},
        )
        db.commit()
    # This is deliberately the only place that prints the one-time secret.
    print("--------------------------------------------------")
    print("Origin-al 网页密码设置入口")
    print()
    print("通过已批准的私有隧道网络打开：")
    print(f"{base_url.rstrip('/')}/setup-password#token={token}")
    print()
    print("SSH Tunnel 仍可作为回退访问方式。")
    print("链接 24 小时有效，仅可使用一次。")
    print("--------------------------------------------------")
    return 0


def status_origin_al() -> int:
    with SessionLocal() as db:
        user = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-al"))
        if user is None:
            print("origin-al portal account: ABSENT")
            return 1
        roles = ",".join(sorted(role.name for role in user.roles))
        print(f"login_name={user.login_name}")
        print(f"normalized_login={user.normalized_login}")
        print(f"account_state={user.account_state}")
        print(f"password_state={user.password_state}")
        print(f"resource_onboarding_state={user.resource_onboarding_state}")
        print(f"unix_username={user.unix_username}")
        print(f"roles={roles}")
        return 0


def plan_origin_pilot() -> int:
    """Create one Portal DRAFT after a bounded, host-only Worker dry-run."""
    if not linux_origin_al_exists():
        return 2
    with SessionLocal() as db:
        user = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-al"))
        if user is None:
            print("PORTAL-3A PLAN BLOCKED — Origin-al portal account is absent", file=sys.stderr)
            return 2
        if user.account_state != AccountState.ACTIVE:
            print(
                "PORTAL-3A PLAN BLOCKED — Origin-al portal account is not ACTIVE", file=sys.stderr
            )
            return 2
        if not any(role.name == "platform_owner" for role in user.roles):
            print("PORTAL-3A PLAN BLOCKED — platform_owner role is absent", file=sys.stderr)
            return 2
        idempotency_key = "portal3a-origin-pilot-plan-v1"
        existing = db.scalar(
            select(PortalOperation).where(
                PortalOperation.requested_by == user.id,
                PortalOperation.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            print("Origin-pilot Portal DRAFT already exists; no duplicate plan created.")
            print(f"operation_status={existing.status} target=origin-pilot")
            return 0
        try:
            result = call_worker(
                "user.plan",
                payload={"username": "origin-pilot"},
                requested_by="origin-al",
                approved_by="origin-al",
                idempotency_key=idempotency_key,
                dry_run=True,
                timeout_seconds=25,
            )
        except WorkerClientError as exc:
            result = {
                "status": "UNKNOWN",
                "plan_status": "CONFLICT",
                "execution_enabled": False,
                "conflicts": [{"code": exc.code[:64], "message": "Worker 暂不可用"}],
            }
        if result.get("status") == "DRY_RUN":
            result = enrich_user_plan_with_portal_state(result, db, user.id)
        operation = PortalOperation(
            operation_type="user.plan",
            target_type="compute_identity",
            target_id="origin-pilot",
            requested_by=user.id,
            request_summary="为 Origin-al 规划独立 origin-pilot 计算身份（仅 dry-run）",
            validated_payload={"username": "origin-pilot"},
            idempotency_key=idempotency_key,
            risk_level=RiskLevel.MEDIUM,
            status=OperationStatus.DRAFT,
            dry_run_result=safe_metadata(result),
            result_summary=(
                "user.plan dry-run 已验证；未创建任何宿主资源"
                if result.get("status") == "DRY_RUN" and result.get("plan_status") == "READY"
                else "user.plan dry-run 发现冲突或不可用；未执行宿主写操作"
            ),
            error_code=(
                None
                if result.get("status") == "DRY_RUN" and result.get("plan_status") == "READY"
                else "PLAN_CONFLICT"
            ),
            created_at=utcnow(),
        )
        db.add(operation)
        db.flush()
        db.add(
            PortalOperationEvent(
                operation_id=operation.id,
                from_status=None,
                to_status=OperationStatus.DRAFT,
                safe_message="Portal-3A plan draft created; no host write executed",
                created_at=utcnow(),
            )
        )
        record_audit(
            db,
            event_type="operation.draft",
            actor="origin-al",
            actor_role="platform_owner",
            source_ip="local-console",
            user_agent="h100-portal-admin",
            object_type="compute_identity_plan",
            object_id="origin-pilot",
            result="SUCCESS" if result.get("status") == "DRY_RUN" else "FAILED",
            metadata={"operation_type": "user.plan", "execution_mode": "dry-run"},
            operation_id=operation.id,
        )
        db.commit()
        print("Origin-pilot Portal DRAFT created; no Linux identity or host reservation created.")
        print(
            f"operation_status={operation.status} plan_status={result.get('plan_status', 'UNKNOWN')}"
        )
        for key in ("proposed_uid", "proposed_gid", "proposed_project_id", "proposed_ssh_port"):
            value = result.get(key)
            if value is not None:
                print(f"{key}={value} (PROPOSED — NOT RESERVED)")
    return 0


def draft_origin_pilot_stage() -> int:
    """Create an idempotent Stage DRAFT after a no-key Worker dry-run."""
    with SessionLocal() as db:
        user = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-al"))
        if (
            user is None
            or user.account_state != AccountState.ACTIVE
            or not any(role.name == "platform_owner" for role in user.roles)
        ):
            print(
                "PORTAL-3B-R STAGE DRAFT BLOCKED — Origin-al owner is unavailable", file=sys.stderr
            )
            return 2
        idempotency_key = "portal3b-r-origin-pilot-stage-v1"
        existing = db.scalar(
            select(PortalOperation).where(
                PortalOperation.requested_by == user.id,
                PortalOperation.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            print("Origin-pilot user.stage DRAFT already exists; no duplicate created.")
            print(f"operation_status={existing.status} target=origin-pilot")
            return 0
        payload = {
            **APPROVED_ORIGIN_PILOT_STAGE,
            "approval_reference": "portal3b-r-lifecycle-revalidated",
        }
        try:
            result = call_worker(
                "user.stage",
                payload=payload,
                requested_by="origin-al",
                approved_by=None,
                idempotency_key=idempotency_key,
                dry_run=True,
                timeout_seconds=30,
            )
        except WorkerClientError as exc:
            print(f"PORTAL-3B-R STAGE DRAFT BLOCKED — {exc.code}", file=sys.stderr)
            return 2
        if result.get("status") != "DRY_RUN" or result.get("stage_status") != "READY":
            error = result.get("error", {})
            code = (
                error.get("code", "STAGE_DRY_RUN_BLOCKED")
                if isinstance(error, dict)
                else "STAGE_DRY_RUN_BLOCKED"
            )
            print(f"PORTAL-3B-R STAGE DRAFT BLOCKED — {code}", file=sys.stderr)
            return 2
        operation = PortalOperation(
            operation_type="user.stage",
            target_type="compute_identity",
            target_id="origin-pilot",
            requested_by=user.id,
            request_summary="按重新验收契约 Stage origin-pilot（当前仅 DRAFT/dry-run）",
            validated_payload=payload,
            idempotency_key=idempotency_key,
            risk_level=RiskLevel.HIGH,
            status=OperationStatus.DRAFT,
            dry_run_result=safe_metadata(result),
            result_summary="user.stage 无公钥 dry-run READY；未执行宿主写操作",
            created_at=utcnow(),
        )
        db.add(operation)
        db.flush()
        db.add(
            PortalOperationEvent(
                operation_id=operation.id,
                from_status=None,
                to_status=OperationStatus.DRAFT,
                safe_message="Portal-3B-R Stage draft created after no-key dry-run",
                created_at=utcnow(),
            )
        )
        record_audit(
            db,
            event_type="operation.draft",
            actor="origin-al",
            actor_role="platform_owner",
            source_ip="local-console",
            user_agent="h100-portal-admin",
            object_type="compute_identity_stage",
            object_id="origin-pilot",
            result="SUCCESS",
            metadata={
                "operation_type": "user.stage",
                "execution_mode": "dry-run",
                "ssh_key_state": "NOT_REQUIRED_FOR_STAGE",
            },
            operation_id=operation.id,
        )
        db.commit()
        print("Origin-pilot user.stage DRAFT created after Worker dry-run.")
        print("stage_status=READY execution_enabled=false ssh_key=NOT_REQUIRED_FOR_STAGE")
        print("No Linux user, policy, quota, association, container, or SSH key was created.")
    return 0


def reopen_portal3c_stage_retry(
    db: Session, *, operation: PortalOperation, owner: PortalUser
) -> None:
    """Reopen only the exact fully rolled-back Portal-3C operation.

    The caller must still run the complete Worker dry-run before this database
    transaction is committed.  If that preflight fails, the session closes and
    this transition is rolled back to the terminal ROLLED_BACK record.
    """
    if (
        operation.status != OperationStatus.ROLLED_BACK
        or operation.operation_type != "user.stage"
        or operation.target_type != "compute_identity"
        or operation.target_id != "origin-pilot"
        or operation.requested_by != owner.id
        or operation.approved_by != owner.id
        or operation.idempotency_key != PORTAL3C_STAGE_IDEMPOTENCY_KEY
        or operation.error_code != "STAGE_EXECUTION_FAILED"
        or operation.rollback_status != "ROLLED_BACK"
    ):
        raise RuntimeError("rolled-back Operation is not eligible for Portal-3C retry")
    validated = validate_operation_payload(operation.operation_type, operation.validated_payload)
    if (
        validated != operation.validated_payload
        or validated.get("approval_reference") != PORTAL3C_STAGE_APPROVAL_REFERENCE
    ):
        raise RuntimeError("rolled-back Operation payload no longer matches Portal-3C approval")

    transition(operation, OperationStatus.DRAFT, "rolled-back Stage reopened for revalidation", db)
    operation.approved_by = None
    operation.approved_at = None
    operation.started_at = None
    operation.finished_at = None
    operation.worker_execution_id = None
    operation.error_code = None
    operation.rollback_status = None
    operation.result_summary = "Portal-3C retry requires a fresh dry-run and administrator approval"
    record_audit(
        db,
        event_type="user.stage.retry_reopened",
        actor="origin-al",
        actor_role="platform_owner",
        source_ip="local-console",
        user_agent="h100-portal-admin",
        object_type="operation",
        object_id=str(operation.id),
        result="SUCCESS",
        metadata={
            "target": "origin-pilot",
            "prior_status": "ROLLED_BACK",
            "idempotency_key_reused": True,
        },
        operation_id=operation.id,
    )


def stage_origin_pilot(approval_text: str) -> int:
    """Advance and execute the one pre-existing Portal-3C Stage Operation."""
    if approval_text != PORTAL3C_APPROVAL_TEXT:
        print(
            "PORTAL-3C STAGE BLOCKED — exact administrator approval text required", file=sys.stderr
        )
        return 2
    operation_id: uuid.UUID
    with SessionLocal() as db:
        owner = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-al"))
        if (
            owner is None
            or owner.account_state != AccountState.ACTIVE
            or owner.unix_username != "origin-al"
            or not any(role.name == "platform_owner" for role in owner.roles)
        ):
            print("PORTAL-3C STAGE BLOCKED — Origin-al owner binding is invalid", file=sys.stderr)
            return 2
        operation = db.scalar(
            select(PortalOperation).where(
                PortalOperation.requested_by == owner.id,
                PortalOperation.idempotency_key == PORTAL3C_STAGE_IDEMPOTENCY_KEY,
            )
        )
        if operation is None:
            print("PORTAL-3C STAGE BLOCKED — approved DRAFT Operation is absent", file=sys.stderr)
            return 2
        operation_id = operation.id
        if operation.status == OperationStatus.SUCCEEDED:
            managed = db.scalar(
                select(PortalManagedUser).where(PortalManagedUser.portal_user_id == owner.id)
            )
            if managed is not None and managed.onboarding_state == OnboardingState.STAGED:
                print("Origin-pilot Portal Stage already SUCCEEDED; no duplicate execution.")
                print(f"operation_id={operation.id} status={operation.status}")
                return 0
            print("PORTAL-3C STAGE BLOCKED — Operation/result state mismatch", file=sys.stderr)
            return 2
        if operation.status == OperationStatus.ROLLED_BACK:
            try:
                reopen_portal3c_stage_retry(db, operation=operation, owner=owner)
            except (RuntimeError, ValueError) as exc:
                print(f"PORTAL-3C STAGE BLOCKED — {exc}", file=sys.stderr)
                return 2
        if operation.status == OperationStatus.QUEUED:
            print("Origin-pilot Stage is already QUEUED; resuming controlled execution.")
        elif operation.status != OperationStatus.DRAFT:
            print(
                f"PORTAL-3C STAGE BLOCKED — Operation state is {operation.status}",
                file=sys.stderr,
            )
            return 2
        else:
            if (
                operation.operation_type != "user.stage"
                or operation.target_type != "compute_identity"
                or operation.target_id != "origin-pilot"
            ):
                print("PORTAL-3C STAGE BLOCKED — DRAFT target mismatch", file=sys.stderr)
                return 2
            try:
                validated = validate_operation_payload(
                    operation.operation_type, operation.validated_payload
                )
            except ValueError as exc:
                print(f"PORTAL-3C STAGE BLOCKED — {exc}", file=sys.stderr)
                return 2
            if (
                validated != operation.validated_payload
                or validated.get("approval_reference") != PORTAL3C_STAGE_APPROVAL_REFERENCE
            ):
                print("PORTAL-3C STAGE BLOCKED — validated payload changed", file=sys.stderr)
                return 2
            try:
                preflight = call_worker(
                    "user.stage",
                    payload=validated,
                    requested_by="origin-al",
                    approved_by=None,
                    idempotency_key=PORTAL3C_STAGE_IDEMPOTENCY_KEY,
                    dry_run=True,
                    timeout_seconds=45,
                )
            except WorkerClientError as exc:
                print(f"PORTAL-3C STAGE BLOCKED — {exc.code}", file=sys.stderr)
                return 2
            if preflight.get("status") != "DRY_RUN" or preflight.get("stage_status") != "READY":
                error = preflight.get("error", {})
                code = (
                    error.get("code", "STAGE_DRY_RUN_BLOCKED")
                    if isinstance(error, dict)
                    else "STAGE_DRY_RUN_BLOCKED"
                )
                print(f"PORTAL-3C STAGE BLOCKED — {code}", file=sys.stderr)
                return 2
            operation.dry_run_result = cast(dict[str, Any], safe_metadata(preflight))
            operation.result_summary = (
                "Portal-3C execution preflight READY; awaiting controlled Worker"
            )
            transition(
                operation,
                OperationStatus.PENDING_APPROVAL,
                "submitted by current administrator console for real Stage",
                db,
            )
            record_audit(
                db,
                event_type="user.stage.request",
                actor="origin-al",
                actor_role="platform_owner",
                source_ip="local-console",
                user_agent="h100-portal-admin",
                object_type="operation",
                object_id=str(operation.id),
                result="SUCCESS",
                metadata={"target": "origin-pilot", "execution_mode": "real-stage"},
                operation_id=operation.id,
            )
            record_audit(
                db,
                event_type="user.stage.validation",
                actor="origin-al",
                actor_role="platform_owner",
                source_ip="local-console",
                user_agent="h100-portal-admin",
                object_type="operation",
                object_id=str(operation.id),
                result="PASS",
                metadata={"stage_status": "READY", "conflicts": []},
                operation_id=operation.id,
            )
            db.add(
                PortalOperationApproval(
                    operation_id=operation.id,
                    approver_id=owner.id,
                    decision="APPROVE",
                    safe_comment=approval_text,
                    decided_at=utcnow(),
                )
            )
            operation.approved_by = owner.id
            operation.approved_at = utcnow()
            transition(
                operation, OperationStatus.APPROVED, "administrator console approval granted", db
            )
            transition(operation, OperationStatus.QUEUED, "queued for controlled real Stage", db)
            record_audit(
                db,
                event_type="operation.approval",
                actor="origin-al",
                actor_role="platform_owner",
                source_ip="local-console",
                user_agent="h100-portal-admin",
                object_type="operation",
                object_id=str(operation.id),
                result="APPROVE",
                metadata={
                    "approval_source": "current_administrator_console",
                    "approval_text": approval_text,
                    "execution_mode": "real-stage",
                },
                operation_id=operation.id,
            )
            db.commit()
            print(f"operation_id={operation.id} status=QUEUED target=origin-pilot")

    execute_operation(operation_id, "origin-al")
    with SessionLocal() as db:
        operation = db.get(PortalOperation, operation_id)
        if operation is None:
            print("PORTAL-3C STAGE BLOCKED — Operation disappeared", file=sys.stderr)
            return 2
        print(f"operation_id={operation.id}")
        print(f"operation_status={operation.status}")
        print(f"error_code={operation.error_code or 'NONE'}")
        print(f"rollback_status={operation.rollback_status or 'NONE'}")
        if operation.status == OperationStatus.SUCCEEDED:
            print("origin-pilot compute identity state=STAGED")
            print("SSH key state=REQUIRED_BEFORE_ACTIVATION")
            return 0
        print("PORTAL-3C STAGE OPERATION DID NOT SUCCEED", file=sys.stderr)
        return 2


def revalidate_origin_pilot_activate() -> int:
    """Create a fresh, non-executable Activate dry-run after SSH policy remediation."""
    with SessionLocal() as db:
        owner = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-al"))
        if (
            owner is None
            or owner.account_state != AccountState.ACTIVE
            or owner.unix_username != "origin-al"
            or not any(role.name == "platform_owner" for role in owner.roles)
        ):
            print(
                "PORTAL-3E-R DRY-RUN BLOCKED — Origin-al owner binding is invalid",
                file=sys.stderr,
            )
            return 2
        managed = db.scalar(
            select(PortalManagedUser).where(PortalManagedUser.portal_user_id == owner.id)
        )
        if (
            managed is None
            or managed.unix_username != "origin-pilot"
            or managed.onboarding_state != OnboardingState.STAGED
            or managed.shell != "/usr/sbin/nologin"
        ):
            print("PORTAL-3E-R DRY-RUN BLOCKED — origin-pilot is not STAGED", file=sys.stderr)
            return 2
        payload = {
            "managed_user_id": str(managed.id),
            "approved_ssh_key_record_ids": [str(PORTAL3ER_KEY_RECORD_ID)],
            "expected_state": "STAGED",
            "approval_reference": PORTAL3ER_APPROVAL_REFERENCE,
        }
        try:
            records = validate_activate_database_bindings(
                db, owner_id=owner.id, target_id="origin-pilot", payload=payload
            )
        except OperationPayloadError as exc:
            print(f"PORTAL-3E-R DRY-RUN BLOCKED — {exc.code}", file=sys.stderr)
            return 2
        if (
            len(records) != 1
            or records[0].id != PORTAL3ER_KEY_RECORD_ID
            or records[0].fingerprint_sha256 != PORTAL3ER_KEY_FINGERPRINT
            or records[0].key_type != "ssh-ed25519"
            or records[0].scope != "BOTH"
        ):
            print("PORTAL-3E-R DRY-RUN BLOCKED — approved SSH key changed", file=sys.stderr)
            return 2
        idempotency_key = f"portal3e-r-activate:{uuid.uuid4()}"
        try:
            result = call_worker(
                "user.activate",
                payload=payload,
                requested_by="origin-al",
                approved_by=None,
                idempotency_key=idempotency_key,
                dry_run=True,
                timeout_seconds=45,
            )
            if result.get("status") != "DRY_RUN" or result.get("activate_status") != "READY":
                error = result.get("error", {})
                code = (
                    error.get("code", "ACTIVATE_DRY_RUN_BLOCKED")
                    if isinstance(error, dict)
                    else "ACTIVATE_DRY_RUN_BLOCKED"
                )
                print(f"PORTAL-3E-R DRY-RUN BLOCKED — {code}", file=sys.stderr)
                return 2
            validate_activate_worker_result(records, result)
        except (WorkerClientError, OperationPayloadError) as exc:
            print(
                f"PORTAL-3E-R DRY-RUN BLOCKED — {getattr(exc, 'code', 'WORKER_FAILED')}",
                file=sys.stderr,
            )
            return 2
        operation = PortalOperation(
            operation_type="user.activate",
            target_type="compute_identity",
            target_id="origin-pilot",
            requested_by=owner.id,
            request_summary=(
                "Portal-3E-R host SSH public-key-only policy revalidated Activate dry-run"
            ),
            validated_payload=payload,
            idempotency_key=idempotency_key,
            risk_level=RiskLevel.CRITICAL,
            status=OperationStatus.DRAFT,
            dry_run_result=cast(dict[str, Any], safe_metadata(result)),
            result_summary=(
                "Activate dry-run READY；host SSH public-key-only policy PASSING；"
                "execution_enabled=false"
            ),
            created_at=utcnow(),
        )
        db.add(operation)
        db.flush()
        db.add(
            PortalOperationEvent(
                operation_id=operation.id,
                from_status=None,
                to_status=OperationStatus.DRAFT,
                safe_message="Portal-3E-R fresh Activate dry-run created; execution disabled",
                created_at=utcnow(),
            )
        )
        record_audit(
            db,
            event_type="user.activate.dry_run_revalidated",
            actor="origin-al",
            actor_role="platform_owner",
            source_ip="local-console",
            user_agent="h100-portal-admin",
            object_type="operation",
            object_id=str(operation.id),
            result="DRY_RUN",
            metadata={
                "target": "origin-pilot",
                "host_ssh_policy": "PUBLIC_KEY_ONLY",
                "execution_enabled": False,
                "key_fingerprint": PORTAL3ER_KEY_FINGERPRINT,
            },
            operation_id=operation.id,
        )
        db.commit()
        print(f"operation_id={operation.id}")
        print("operation_status=DRAFT")
        print("activate_status=READY")
        print("host_ssh_policy=PUBLIC_KEY_ONLY")
        print("execution_enabled=false")
        return 0


def activate_origin_pilot_final(approval_text: str) -> int:
    """Execute the one Portal-3E-FINAL Activate authorized by the administrator."""
    if approval_text != PORTAL3E_FINAL_APPROVAL_TEXT:
        print("PORTAL-3E-FINAL BLOCKED — approval text mismatch", file=sys.stderr)
        return 2
    with SessionLocal() as db:
        owner = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-al"))
        if (
            owner is None
            or owner.account_state != AccountState.ACTIVE
            or owner.unix_username != "origin-al"
            or owner.resource_onboarding_state != OnboardingState.STAGED
            or not any(role.name == "platform_owner" for role in owner.roles)
        ):
            print("PORTAL-3E-FINAL BLOCKED — Origin-al binding changed", file=sys.stderr)
            return 2
        managed = db.get(PortalManagedUser, PORTAL3E_FINAL_MANAGED_USER_ID)
        if (
            managed is None
            or managed.portal_user_id != owner.id
            or managed.unix_username != "origin-pilot"
            or managed.uid != 20001
            or managed.gid != 20001
            or managed.shell != "/usr/sbin/nologin"
            or managed.onboarding_state != OnboardingState.STAGED
            or managed.host_access_state != "DISABLED"
            or managed.project_id != 30001
            or managed.quota_bytes != 300 * 1024**3
            or managed.slurm_account != "company"
            or managed.slurm_qos != "general"
            or managed.container_name != "gpu-dev-origin-pilot"
            or managed.container_port != 22023
        ):
            print(
                "PORTAL-3E-FINAL BLOCKED — origin-pilot is not approved STAGED identity",
                file=sys.stderr,
            )
            return 2
        referenced_dry_run = db.get(PortalOperation, PORTAL3E_FINAL_DRY_RUN_OPERATION_ID)
        dry_result = referenced_dry_run.dry_run_result if referenced_dry_run is not None else None
        expected_dry_payload = {
            "managed_user_id": str(PORTAL3E_FINAL_MANAGED_USER_ID),
            "approved_ssh_key_record_ids": [str(PORTAL3E_FINAL_KEY_RECORD_ID)],
            "expected_state": "STAGED",
            "approval_reference": PORTAL3ER_APPROVAL_REFERENCE,
        }
        if not (
            referenced_dry_run is not None
            and referenced_dry_run.operation_type == "user.activate"
            and referenced_dry_run.target_type == "compute_identity"
            and referenced_dry_run.target_id == "origin-pilot"
            and referenced_dry_run.requested_by == owner.id
            and referenced_dry_run.approved_by is None
            and referenced_dry_run.status == OperationStatus.DRAFT
            and referenced_dry_run.validated_payload == expected_dry_payload
            and isinstance(dry_result, dict)
            and dry_result.get("status") == "DRY_RUN"
            and dry_result.get("activate_status") == "READY"
            and dry_result.get("execution_enabled") is False
        ):
            print("PORTAL-3E-FINAL BLOCKED — approved dry-run changed", file=sys.stderr)
            return 2

        existing = db.scalar(
            select(PortalOperation).where(
                PortalOperation.requested_by == owner.id,
                PortalOperation.idempotency_key == PORTAL3E_FINAL_IDEMPOTENCY_KEY,
            )
        )
        if existing is not None:
            print(f"operation_id={existing.id}")
            print(f"operation_status={existing.status}")
            print(f"error_code={existing.error_code or 'NONE'}")
            print(f"rollback_status={existing.rollback_status or 'NONE'}")
            return 0 if existing.status == OperationStatus.SUCCEEDED else 2

        payload = validate_operation_payload(
            "user.activate",
            {
                "managed_user_id": str(PORTAL3E_FINAL_MANAGED_USER_ID),
                "approved_ssh_key_record_ids": [str(PORTAL3E_FINAL_KEY_RECORD_ID)],
                "expected_state": "STAGED",
                "approval_reference": PORTAL3E_FINAL_APPROVAL_REFERENCE,
                "dry_run_operation_id": str(PORTAL3E_FINAL_DRY_RUN_OPERATION_ID),
            },
        )
        try:
            records = validate_activate_database_bindings(
                db, owner_id=owner.id, target_id="origin-pilot", payload=payload
            )
        except OperationPayloadError as exc:
            print(f"PORTAL-3E-FINAL BLOCKED — {exc.code}", file=sys.stderr)
            return 2
        if (
            len(records) != 1
            or records[0].id != PORTAL3E_FINAL_KEY_RECORD_ID
            or records[0].fingerprint_sha256 != PORTAL3E_FINAL_KEY_FINGERPRINT
            or records[0].key_type != "ssh-ed25519"
            or records[0].scope != "BOTH"
            or records[0].state != "VALIDATED"
        ):
            print("PORTAL-3E-FINAL BLOCKED — approved SSH key changed", file=sys.stderr)
            return 2
        try:
            preflight = call_worker(
                "user.activate",
                payload=payload,
                requested_by="origin-al",
                approved_by=None,
                idempotency_key=f"portal3e-final-preflight:{uuid.uuid4()}",
                dry_run=True,
                timeout_seconds=60,
            )
            if preflight.get("status") != "DRY_RUN" or preflight.get("activate_status") != "READY":
                error = preflight.get("error", {})
                code = (
                    error.get("code", "ACTIVATE_PREFLIGHT_BLOCKED")
                    if isinstance(error, dict)
                    else "ACTIVATE_PREFLIGHT_BLOCKED"
                )
                print(f"PORTAL-3E-FINAL BLOCKED — {code}", file=sys.stderr)
                return 2
            validate_activate_worker_result(records, preflight)
        except (WorkerClientError, OperationPayloadError) as exc:
            print(
                f"PORTAL-3E-FINAL BLOCKED — {getattr(exc, 'code', 'WORKER_FAILED')}",
                file=sys.stderr,
            )
            return 2

        operation = PortalOperation(
            operation_type="user.activate",
            target_type="compute_identity",
            target_id="origin-pilot",
            requested_by=owner.id,
            request_summary=(
                "Portal-3E-FINAL activate origin-pilot Host/Container public-key SSH access"
            ),
            validated_payload=payload,
            idempotency_key=PORTAL3E_FINAL_IDEMPOTENCY_KEY,
            risk_level=RiskLevel.CRITICAL,
            status=OperationStatus.DRAFT,
            dry_run_result={
                **cast(dict[str, Any], safe_metadata(preflight)),
                "referenced_dry_run_operation_id": str(PORTAL3E_FINAL_DRY_RUN_OPERATION_ID),
                "execution_enabled": False,
            },
            result_summary="Portal-3E-FINAL preflight READY; awaiting approved Worker execution",
            created_at=utcnow(),
        )
        db.add(operation)
        db.flush()
        operation_id = operation.id
        db.add(
            PortalOperationEvent(
                operation_id=operation.id,
                from_status=None,
                to_status=OperationStatus.DRAFT,
                safe_message="Portal-3E-FINAL real Activate operation created",
                created_at=utcnow(),
            )
        )
        transition(
            operation,
            OperationStatus.PENDING_APPROVAL,
            "submitted by current administrator console for real Activate",
            db,
        )
        db.add(
            PortalOperationApproval(
                operation_id=operation.id,
                approver_id=owner.id,
                decision="APPROVE",
                safe_comment=approval_text,
                decided_at=utcnow(),
            )
        )
        operation.approved_by = owner.id
        operation.approved_at = utcnow()
        transition(
            operation,
            OperationStatus.APPROVED,
            "Portal-3E-FINAL administrator approval granted",
            db,
        )
        transition(operation, OperationStatus.QUEUED, "queued for controlled real Activate", db)
        record_audit(
            db,
            event_type="user.activate.request",
            actor="origin-al",
            actor_role="platform_owner",
            source_ip="local-console",
            user_agent="h100-portal-admin",
            object_type="operation",
            object_id=str(operation.id),
            result="APPROVE",
            metadata={
                "target": "origin-pilot",
                "managed_user_id": str(PORTAL3E_FINAL_MANAGED_USER_ID),
                "key_record_id": str(PORTAL3E_FINAL_KEY_RECORD_ID),
                "key_fingerprint": PORTAL3E_FINAL_KEY_FINGERPRINT,
                "key_scope": "BOTH",
                "dry_run_operation_id": str(PORTAL3E_FINAL_DRY_RUN_OPERATION_ID),
                "execution_mode": "real-activate",
            },
            operation_id=operation.id,
        )
        db.commit()
        print(f"operation_id={operation.id} status=QUEUED target=origin-pilot")

    execute_operation(operation_id, "origin-al")
    with SessionLocal() as db:
        final_operation = db.get(PortalOperation, operation_id)
        if final_operation is None:
            print("PORTAL-3E-FINAL BLOCKED — Operation disappeared", file=sys.stderr)
            return 2
        print(f"operation_id={final_operation.id}")
        print(f"operation_status={final_operation.status}")
        print(f"error_code={final_operation.error_code or 'NONE'}")
        print(f"rollback_status={final_operation.rollback_status or 'NONE'}")
        if final_operation.status != OperationStatus.SUCCEEDED:
            print("PORTAL USER ACTIVATE OPERATION DID NOT SUCCEED", file=sys.stderr)
            return 2
        execution = (
            final_operation.dry_run_result.get("execution_result", {})
            if isinstance(final_operation.dry_run_result, dict)
            else {}
        )
        activate = execution.get("activate", {}) if isinstance(execution, dict) else {}
        try:
            final_records = validate_activate_database_bindings(
                db,
                owner_id=final_operation.requested_by,
                target_id="origin-pilot",
                payload=final_operation.validated_payload,
            )
        except OperationPayloadError:
            # Installed records intentionally no longer satisfy the VALIDATED-only
            # pre-write binding.  Reconstruct the exact approved record for the
            # immutable Worker-result check below.
            final_records = list(
                db.scalars(
                    select(PortalSshKey).where(PortalSshKey.id == PORTAL3E_FINAL_KEY_RECORD_ID)
                ).all()
            )
        validate_persisted_activate_execution_result(final_records, execution)
        print("compute_identity=ACTIVE")
        print("ssh_key_state=INSTALLED")
        print("host_ssh_server=READY_FOR_CLIENT_VALIDATION")
        print("container_ssh_server=READY_FOR_CLIENT_VALIDATION")
        print("host_ssh_client_validation=PENDING")
        print("container_ssh_client_validation=PENDING")
        print(f"host_server_fingerprint={activate.get('host_server_fingerprint')}")
        print(f"container_server_fingerprint={activate.get('container_server_fingerprint')}")
        print("slurm_node=DRAIN")
        print("slurm_queue=EMPTY")
        return 0


def _portal3f_database_baseline(
    db: Session,
    *,
    expected_slurm_node_state: str = "DRAIN",
) -> tuple[PortalUser, PortalManagedUser, PortalSshKey, PortalContainer]:
    if expected_slurm_node_state not in {"DRAIN", "IDLE"}:
        raise RuntimeError("unsupported Portal Pilot Slurm state")
    owner = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-al"))
    managed = db.get(PortalManagedUser, PORTAL3E_FINAL_MANAGED_USER_ID)
    key = db.get(PortalSshKey, PORTAL3E_FINAL_KEY_RECORD_ID)
    container = db.scalar(
        select(PortalContainer).where(
            PortalContainer.managed_user_id == PORTAL3E_FINAL_MANAGED_USER_ID,
            PortalContainer.name == "gpu-dev-origin-pilot",
        )
    )
    activate = (
        db.scalar(
            select(PortalOperation).where(
                PortalOperation.requested_by == owner.id,
                PortalOperation.idempotency_key == PORTAL3E_FINAL_IDEMPOTENCY_KEY,
            )
        )
        if owner is not None
        else None
    )
    safe_spec = container.safe_spec if container is not None else None
    if not (
        owner is not None
        and owner.account_state == AccountState.ACTIVE
        and owner.unix_username == "origin-al"
        and owner.resource_onboarding_state == OnboardingState.ACTIVE
        and any(role.name == "platform_owner" for role in owner.roles)
        and managed is not None
        and managed.portal_user_id == owner.id
        and managed.unix_username == "origin-pilot"
        and managed.uid == 20001
        and managed.gid == 20001
        and managed.shell == "/bin/bash"
        and managed.host_access_state == "ENABLED"
        and managed.onboarding_state == OnboardingState.ACTIVE
        and managed.ssh_key_state == "INSTALLED"
        and managed.slurm_account == "company"
        and managed.slurm_qos == "general"
        and key is not None
        and key.managed_user_id == managed.id
        and key.state == "INSTALLED"
        and key.active is True
        and key.fingerprint_sha256 == PORTAL3E_FINAL_KEY_FINGERPRINT
        and key.scope == "BOTH"
        and key.installed_at is not None
        and container is not None
        and container.observed_state == "RUNNING"
        and container.desired_state == "RUNNING"
        and isinstance(safe_spec, dict)
        and safe_spec.get("gpu") == "NONE"
        and safe_spec.get("authorized_keys") == "INSTALLED"
        and safe_spec.get("container_authorized_keys") == "INSTALLED"
        and safe_spec.get("slurm_node_state") == expected_slurm_node_state
        and safe_spec.get("slurm_queue") == "EMPTY"
        and activate is not None
        and activate.status == OperationStatus.SUCCEEDED
    ):
        raise RuntimeError("Portal-3F ACTIVE database baseline changed")
    return owner, managed, key, container


def _portal3f_approved_operation(
    db: Session,
    *,
    owner: PortalUser,
    operation_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    risk_level: RiskLevel,
    summary: str,
    dry_run_result: dict[str, Any],
) -> PortalOperation:
    operation = PortalOperation(
        operation_type=operation_type,
        target_type="compute_identity",
        target_id="origin-pilot",
        requested_by=owner.id,
        request_summary=summary,
        validated_payload=payload,
        idempotency_key=idempotency_key,
        risk_level=risk_level,
        status=OperationStatus.DRAFT,
        dry_run_result=cast(dict[str, Any], safe_metadata(dry_run_result)),
        result_summary="Portal-3F fixed preflight passed; approved execution queued",
        created_at=utcnow(),
    )
    db.add(operation)
    db.flush()
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=None,
            to_status=OperationStatus.DRAFT,
            safe_message="Portal-3F fixed operation created after Worker preflight",
            created_at=utcnow(),
        )
    )
    transition(
        operation,
        OperationStatus.PENDING_APPROVAL,
        "submitted under the current Portal-3F administrator approval",
        db,
    )
    db.add(
        PortalOperationApproval(
            operation_id=operation.id,
            approver_id=owner.id,
            decision="APPROVE",
            safe_comment=PORTAL3F_APPROVAL_TEXT,
            decided_at=utcnow(),
        )
    )
    operation.approved_by = owner.id
    operation.approved_at = utcnow()
    transition(operation, OperationStatus.APPROVED, "Portal-3F approval bound", db)
    transition(operation, OperationStatus.QUEUED, "queued for the fixed Root Worker", db)
    record_audit(
        db,
        event_type=f"{operation_type}.request",
        actor="origin-al",
        actor_role="platform_owner",
        source_ip="local-console",
        user_agent="h100-portal-admin",
        object_type="operation",
        object_id=str(operation.id),
        result="APPROVE",
        metadata={
            "target": "origin-pilot",
            "operation_type": operation_type,
            "approval_reference": APPROVED_PORTAL3F_CLIENT_VALIDATION["approval_reference"],
            "private_key_handling": "NOT_ACCESSED",
        },
        operation_id=operation.id,
    )
    return operation


def _portal3f_record_client_validation() -> uuid.UUID:
    with SessionLocal() as db:
        owner, _managed, _key, container = _portal3f_database_baseline(db)
        existing = db.scalar(
            select(PortalOperation).where(
                PortalOperation.requested_by == owner.id,
                PortalOperation.idempotency_key == PORTAL3F_CLIENT_VALIDATION_IDEMPOTENCY_KEY,
            )
        )
        if existing is not None:
            if not (
                existing.status == OperationStatus.SUCCEEDED
                and container.safe_spec.get("host_ssh_client_validation") == "PASS"
                and container.safe_spec.get("container_ssh_client_validation") == "PASS"
            ):
                raise RuntimeError(
                    "existing Portal-3F client-validation operation is not successful"
                )
            return existing.id
        payload = validate_operation_payload(
            "user.ssh_client_validation.record", APPROVED_PORTAL3F_CLIENT_VALIDATION
        )
        preflight = call_worker(
            "user.ssh_client_validation.record",
            payload=payload,
            requested_by="origin-al",
            approved_by=None,
            idempotency_key=f"portal3f-client-preflight:{uuid.uuid4()}",
            dry_run=True,
            timeout_seconds=180,
        )
        validate_portal3f_client_validation_plan(preflight)
        created_operation = _portal3f_approved_operation(
            db,
            owner=owner,
            operation_type="user.ssh_client_validation.record",
            payload=payload,
            idempotency_key=PORTAL3F_CLIENT_VALIDATION_IDEMPOTENCY_KEY,
            risk_level=RiskLevel.MEDIUM,
            summary="Record user-confirmed Host and Container SSH client validation PASS",
            dry_run_result=preflight,
        )
        operation_id = created_operation.id
        db.commit()

    with SessionLocal() as db:
        operation = db.get(PortalOperation, operation_id)
        if operation is None or operation.status != OperationStatus.QUEUED:
            raise RuntimeError("Portal-3F client-validation operation disappeared")
        transition(operation, OperationStatus.RUNNING, "server readiness revalidation started", db)
        operation.started_at = utcnow()
        db.commit()
    try:
        result = call_worker(
            "user.ssh_client_validation.record",
            payload=APPROVED_PORTAL3F_CLIENT_VALIDATION,
            requested_by="origin-al",
            approved_by="origin-al",
            idempotency_key=PORTAL3F_CLIENT_VALIDATION_IDEMPOTENCY_KEY,
            dry_run=False,
            timeout_seconds=180,
        )
    except WorkerClientError as exc:
        with SessionLocal() as db:
            operation = db.get(PortalOperation, operation_id)
            if operation is not None and operation.status == OperationStatus.RUNNING:
                transition(
                    operation, OperationStatus.FAILED, "client validation Worker unavailable", db
                )
                operation.error_code = exc.code[:64]
                operation.finished_at = utcnow()
                operation.result_summary = "SSH client confirmation was not persisted"
                db.commit()
        raise

    with SessionLocal() as db:
        operation = db.get(PortalOperation, operation_id)
        owner, _managed, _key, _container = _portal3f_database_baseline(db)
        if operation is None or operation.status != OperationStatus.RUNNING:
            raise RuntimeError("Portal-3F client-validation operation state changed")
        if result.get("status") != "SUCCEEDED":
            transition(
                operation, OperationStatus.FAILED, "server readiness revalidation failed", db
            )
            error = result.get("error", {})
            operation.error_code = str(
                error.get("code", "PORTAL3F_CLIENT_VALIDATION_FAILED")
                if isinstance(error, dict)
                else "PORTAL3F_CLIENT_VALIDATION_FAILED"
            )[:64]
            operation.finished_at = utcnow()
            operation.dry_run_result = {
                **(operation.dry_run_result or {}),
                "execution_result": cast(dict[str, Any], safe_metadata(result)),
            }
            operation.result_summary = "Server revalidation failed; client PASS was not persisted"
            db.commit()
            raise RuntimeError(operation.error_code)
        validate_portal3f_client_validation_result(result)
        persist_portal3f_client_validation(
            db, owner=owner, operation=operation, worker_result=result
        )
        operation.dry_run_result = {
            **(operation.dry_run_result or {}),
            "execution_result": cast(dict[str, Any], safe_metadata(result)),
            "client_validation_status": "PASS",
            "execution_enabled": True,
        }
        transition(
            operation, OperationStatus.SUCCEEDED, "both real-client confirmations recorded", db
        )
        operation.worker_execution_id = str(result.get("request_id", "worker"))[:64]
        operation.finished_at = utcnow()
        operation.result_summary = (
            "Host and Container SSH Client Validation PASS; no private key was accessed"
        )
        record_audit(
            db,
            event_type="user.ssh_client_validation.recorded",
            actor="origin-al",
            actor_role="platform_owner",
            source_ip="local-console",
            user_agent="h100-portal-admin",
            object_type="managed_user",
            object_id="origin-pilot",
            result="SUCCESS",
            metadata={
                "host": "PASS",
                "container": "PASS",
                "confirmation_source": "USER_CONFIRMED_REAL_CLIENT_CONNECTIONS",
                "private_key_handling": "NOT_ACCESSED",
            },
            operation_id=operation.id,
        )
        db.commit()
        return operation.id


def _portal3f_run_pilot_acceptance() -> uuid.UUID:
    with SessionLocal() as db:
        owner, _managed, _key, container = _portal3f_database_baseline(db)
        if not (
            container.safe_spec.get("host_ssh_client_validation") == "PASS"
            and container.safe_spec.get("container_ssh_client_validation") == "PASS"
        ):
            raise RuntimeError("SSH client validation PASS is required before Pilot acceptance")
        existing = db.scalar(
            select(PortalOperation).where(
                PortalOperation.requested_by == owner.id,
                PortalOperation.idempotency_key == PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY,
            )
        )
        if existing is not None:
            if not (
                existing.status == OperationStatus.SUCCEEDED
                and container.safe_spec.get("pilot_acceptance_status") == "PASSED"
                and container.safe_spec.get("pilot_final_node_state") == "DRAIN"
            ):
                raise RuntimeError("existing Portal-3F Pilot acceptance is not successful")
            return existing.id
        payload = validate_operation_payload(
            "user.pilot.acceptance", APPROVED_PORTAL3F_PILOT_ACCEPTANCE
        )
        preflight = call_worker(
            "user.pilot.acceptance",
            payload=payload,
            requested_by="origin-al",
            approved_by=None,
            idempotency_key=f"portal3f-pilot-preflight:{uuid.uuid4()}",
            dry_run=True,
            timeout_seconds=240,
        )
        validate_portal3f_pilot_acceptance_plan(preflight)
        created_operation = _portal3f_approved_operation(
            db,
            owner=owner,
            operation_type="user.pilot.acceptance",
            payload=payload,
            idempotency_key=PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY,
            risk_level=RiskLevel.CRITICAL,
            summary="Run first origin-pilot CPU, single-GPU, Pyxis/Enroot isolation acceptance",
            dry_run_result=preflight,
        )
        operation_id = created_operation.id
        db.commit()

    with SessionLocal() as db:
        operation = db.get(PortalOperation, operation_id)
        if operation is None or operation.status != OperationStatus.QUEUED:
            raise RuntimeError("Portal-3F Pilot acceptance operation disappeared")
        transition(operation, OperationStatus.RUNNING, "fixed Pilot acceptance started", db)
        operation.started_at = utcnow()
        db.commit()
    try:
        result = call_worker(
            "user.pilot.acceptance",
            payload=APPROVED_PORTAL3F_PILOT_ACCEPTANCE,
            requested_by="origin-al",
            approved_by="origin-al",
            idempotency_key=PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY,
            dry_run=False,
            timeout_seconds=1600,
        )
    except WorkerClientError as exc:
        with SessionLocal() as db:
            operation = db.get(PortalOperation, operation_id)
            if operation is not None and operation.status == OperationStatus.RUNNING:
                transition(
                    operation, OperationStatus.FAILED, "Pilot acceptance Worker unavailable", db
                )
                operation.error_code = exc.code[:64]
                operation.rollback_status = "REQUIRES_MANUAL_REVIEW"
                operation.finished_at = utcnow()
                operation.result_summary = (
                    "Worker response unknown; verify and preserve Slurm DRAIN"
                )
                db.commit()
        raise

    with SessionLocal() as db:
        operation = db.get(PortalOperation, operation_id)
        owner, _managed, _key, _container = _portal3f_database_baseline(db)
        if operation is None or operation.status != OperationStatus.RUNNING:
            raise RuntimeError("Portal-3F Pilot acceptance operation state changed")
        if result.get("status") != "SUCCEEDED":
            rollback_status = str(result.get("rollback_status", ""))[:32]
            if rollback_status == "DRAIN_RESTORED":
                transition(
                    operation, OperationStatus.ROLLING_BACK, "Slurm DRAIN recovery recorded", db
                )
                transition(
                    operation, OperationStatus.ROLLED_BACK, "Slurm DRAIN recovery verified", db
                )
            else:
                transition(operation, OperationStatus.FAILED, "Pilot acceptance failed closed", db)
            error = result.get("error", {})
            operation.error_code = str(
                error.get("code", "PORTAL3F_ACCEPTANCE_FAILED")
                if isinstance(error, dict)
                else "PORTAL3F_ACCEPTANCE_FAILED"
            )[:64]
            operation.rollback_status = rollback_status or "REQUIRES_MANUAL_REVIEW"
            operation.finished_at = utcnow()
            operation.dry_run_result = {
                **(operation.dry_run_result or {}),
                "execution_result": cast(dict[str, Any], safe_metadata(result)),
            }
            operation.result_summary = "Pilot acceptance failed; node recovery status recorded"
            record_audit(
                db,
                event_type="user.pilot.acceptance.failed",
                actor="origin-al",
                actor_role="platform_owner",
                source_ip="local-worker-socket",
                user_agent="h100-portal-admin",
                object_type="managed_user",
                object_id="origin-pilot",
                result="FAILED",
                metadata={
                    "error_code": operation.error_code,
                    "rollback_status": operation.rollback_status,
                },
                operation_id=operation.id,
            )
            db.commit()
            raise RuntimeError(operation.error_code)
        acceptance = validate_portal3f_pilot_acceptance_result(result)
        persist_portal3f_pilot_acceptance(
            db, owner=owner, operation=operation, worker_result=result
        )
        operation.dry_run_result = {
            **(operation.dry_run_result or {}),
            "execution_result": cast(dict[str, Any], safe_metadata(result)),
            "pilot_acceptance_status": "PASSED",
            "execution_enabled": True,
        }
        transition(operation, OperationStatus.SUCCEEDED, "first Pilot acceptance verified", db)
        operation.worker_execution_id = str(result.get("request_id", "worker"))[:64]
        operation.rollback_status = "NOT_REQUIRED"
        operation.finished_at = utcnow()
        operation.result_summary = (
            "origin-pilot CPU and single-GPU Pyxis acceptance PASSED; final Slurm state DRAIN"
        )
        record_audit(
            db,
            event_type="user.pilot.acceptance.passed",
            actor="origin-al",
            actor_role="platform_owner",
            source_ip="local-worker-socket",
            user_agent="h100-portal-admin",
            object_type="managed_user",
            object_id="origin-pilot",
            result="SUCCESS",
            metadata={
                "cpu_job_id": acceptance["cpu_job_id"],
                "gpu_job_id": acceptance["gpu_job_id"],
                "allocated_gpu_uuid": acceptance["allocated_gpu_uuid"],
                "account": "company",
                "qos": "general",
                "max_gpus": 1,
                "final_node_state": "DRAIN",
            },
            operation_id=operation.id,
        )
        db.commit()
        return operation.id


def portal3f_origin_pilot(approval_text: str) -> int:
    """Record real-client confirmations and run the fixed first Pilot acceptance."""
    if approval_text != PORTAL3F_APPROVAL_TEXT:
        print("PORTAL-3F BLOCKED — approval text mismatch", file=sys.stderr)
        return 2
    try:
        client_operation_id = _portal3f_record_client_validation()
        print(f"client_validation_operation_id={client_operation_id}")
        print("host_ssh_client_validation=PASS")
        print("container_ssh_client_validation=PASS")
        pilot_operation_id = _portal3f_run_pilot_acceptance()
    except (OperationPayloadError, WorkerClientError, RuntimeError, SQLAlchemyError) as exc:
        code = getattr(exc, "code", exc.__class__.__name__)
        print(f"PORTAL-3F PILOT ACCEPTANCE BLOCKED — {code}", file=sys.stderr)
        return 2
    with SessionLocal() as db:
        operation = db.get(PortalOperation, pilot_operation_id)
        _owner, _managed, _key, container = _portal3f_database_baseline(db)
        if operation is None or operation.status != OperationStatus.SUCCEEDED:
            print("PORTAL-3F PILOT ACCEPTANCE DID NOT SUCCEED", file=sys.stderr)
            return 2
        print(f"pilot_acceptance_operation_id={operation.id}")
        print(f"pilot_acceptance_status={container.safe_spec.get('pilot_acceptance_status')}")
        print(f"cpu_job_id={container.safe_spec.get('pilot_cpu_job_id')}")
        print(f"gpu_job_id={container.safe_spec.get('pilot_gpu_job_id')}")
        print(f"allocated_gpu_uuid={container.safe_spec.get('pilot_allocated_gpu_uuid')}")
        print("out_of_job_gpu_access=DENIED")
        print("in_job_allocated_gpu=ALLOWED")
        print("in_job_unallocated_gpus=DENIED")
        print("in_job_cuda_context=PASSED")
        print("slurm_node=DRAIN")
        print("slurm_queue=EMPTY")
        print("controlled_single_node_pilot_started=NO_AWAITING_FINAL_APPROVAL")
        return 0


def _portal3g_database_baseline(
    db: Session,
    *,
    expected_slurm_node_state: str = "DRAIN",
) -> tuple[PortalUser, PortalManagedUser, PortalContainer]:
    owner, managed, _key, container = _portal3f_database_baseline(
        db, expected_slurm_node_state=expected_slurm_node_state
    )
    client_validation = db.get(PortalOperation, PORTAL3G_CLIENT_VALIDATION_OPERATION_ID)
    pilot_acceptance = db.get(PortalOperation, PORTAL3G_PILOT_ACCEPTANCE_OPERATION_ID)
    if not (
        client_validation is not None
        and client_validation.status == OperationStatus.SUCCEEDED
        and client_validation.operation_type == "user.ssh_client_validation.record"
        and client_validation.target_id == "origin-pilot"
        and pilot_acceptance is not None
        and pilot_acceptance.status == OperationStatus.SUCCEEDED
        and pilot_acceptance.operation_type == "user.pilot.acceptance"
        and pilot_acceptance.target_id == "origin-pilot"
        and container.safe_spec.get("host_ssh_client_validation") == "PASS"
        and container.safe_spec.get("container_ssh_client_validation") == "PASS"
        and container.safe_spec.get("client_validation_operation_id")
        == str(PORTAL3G_CLIENT_VALIDATION_OPERATION_ID)
        and container.safe_spec.get("pilot_acceptance_status") == "PASSED"
        and container.safe_spec.get("pilot_acceptance_operation_id")
        == str(PORTAL3G_PILOT_ACCEPTANCE_OPERATION_ID)
        and container.safe_spec.get("gpu") == "NONE"
    ):
        raise RuntimeError("Portal-3G database evidence differs from approved Portal-3F results")
    return owner, managed, container


def _portal3g_mark_failed(
    operation_id: uuid.UUID,
    *,
    error_code: str,
    rollback_status: str,
    result: dict[str, Any] | None = None,
) -> None:
    with SessionLocal() as db:
        operation = db.get(PortalOperation, operation_id)
        if operation is None or operation.status != OperationStatus.RUNNING:
            return
        if rollback_status in {"DRAIN_RESTORED", "NOT_REQUIRED_NODE_REMAINS_DRAINED"}:
            transition(operation, OperationStatus.ROLLING_BACK, "Production Pilot safety DRAIN", db)
            transition(
                operation,
                OperationStatus.ROLLED_BACK,
                "Production Pilot did not start; Slurm DRAIN verified",
                db,
            )
        else:
            transition(operation, OperationStatus.FAILED, "Production Pilot failed closed", db)
        operation.error_code = error_code[:64]
        operation.rollback_status = rollback_status[:32]
        operation.finished_at = utcnow()
        operation.result_summary = "Production Pilot not started; Slurm safety state recorded"
        if result is not None:
            operation.dry_run_result = {
                **(operation.dry_run_result or {}),
                "execution_result": cast(dict[str, Any], safe_metadata(result)),
            }
        record_audit(
            db,
            event_type="slurm.production_pilot.start.failed",
            actor="origin-al",
            actor_role="platform_owner",
            source_ip="local-worker-socket",
            user_agent="h100-portal-admin",
            object_type="slurm_node",
            object_id="sagsh100server",
            result="FAILED",
            metadata={"error_code": error_code, "rollback_status": rollback_status},
            operation_id=operation.id,
        )
        db.commit()


def _portal3g_request_safety_drain() -> str:
    try:
        result = call_worker(
            "slurm.drain",
            payload={"node_name": "sagsh100server"},
            requested_by="origin-al",
            approved_by="origin-al",
            idempotency_key=PORTAL3G_DRAIN_IDEMPOTENCY_KEY,
            dry_run=False,
            timeout_seconds=90,
        )
    except WorkerClientError:
        return "REQUIRES_MANUAL_REVIEW"
    return str(result.get("rollback_status", "REQUIRES_MANUAL_REVIEW"))[:32]


def _portal3g_create_operation(
    db: Session,
    *,
    owner: PortalUser,
    payload: dict[str, Any],
    preflight: dict[str, Any],
) -> PortalOperation:
    operation = PortalOperation(
        operation_type="slurm.production_pilot.start",
        target_type="slurm_node",
        target_id="sagsh100server",
        requested_by=owner.id,
        request_summary=("Start the approved single-node, single-managed-user Production Pilot"),
        validated_payload=payload,
        idempotency_key=PORTAL3G_PRODUCTION_PILOT_IDEMPOTENCY_KEY,
        risk_level=RiskLevel.CRITICAL,
        status=OperationStatus.DRAFT,
        dry_run_result=cast(dict[str, Any], safe_metadata(preflight)),
        result_summary="Portal-3G fixed preflight passed; approved RESUME queued",
        created_at=utcnow(),
    )
    db.add(operation)
    db.flush()
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=None,
            to_status=OperationStatus.DRAFT,
            safe_message="Portal-3G operation created after fixed Worker preflight",
            created_at=utcnow(),
        )
    )
    transition(
        operation,
        OperationStatus.PENDING_APPROVAL,
        "submitted under the current Portal-3G administrator approval",
        db,
    )
    db.add(
        PortalOperationApproval(
            operation_id=operation.id,
            approver_id=owner.id,
            decision="APPROVE",
            safe_comment=PORTAL3G_APPROVAL_TEXT,
            decided_at=utcnow(),
        )
    )
    operation.approved_by = owner.id
    operation.approved_at = utcnow()
    transition(operation, OperationStatus.APPROVED, "Portal-3G approval bound", db)
    transition(operation, OperationStatus.QUEUED, "queued for fixed Slurm RESUME handler", db)
    record_audit(
        db,
        event_type="slurm.production_pilot.start.request",
        actor="origin-al",
        actor_role="platform_owner",
        source_ip="local-console",
        user_agent="h100-portal-admin",
        object_type="slurm_node",
        object_id="sagsh100server",
        result="APPROVE",
        metadata={
            "approval_reference": PORTAL3G_APPROVAL_REFERENCE,
            "previous_state": "DRAIN",
            "requested_state": "IDLE",
            "scope": "single-node/single-managed-user/max-1-gpu",
        },
        operation_id=operation.id,
    )
    return operation


def portal3g_start_production_pilot(approval_text: str) -> int:
    """Start the fixed Portal-3G Production Pilot without submitting a job."""
    if approval_text != PORTAL3G_APPROVAL_TEXT:
        print("PORTAL-3G BLOCKED — approval text mismatch", file=sys.stderr)
        return 2
    try:
        with SessionLocal() as db:
            owner, _managed, _container = _portal3g_database_baseline(db)
            existing = db.scalar(
                select(PortalOperation).where(
                    PortalOperation.requested_by == owner.id,
                    PortalOperation.idempotency_key == PORTAL3G_PRODUCTION_PILOT_IDEMPOTENCY_KEY,
                )
            )
            setting = db.get(PortalSetting, PORTAL3G_SETTING_KEY)
            if existing is not None:
                if not (
                    existing.status == OperationStatus.SUCCEEDED
                    and setting is not None
                    and setting.value.get("state") == "ACTIVE"
                    and setting.value.get("node_state") == "IDLE"
                ):
                    raise RuntimeError("existing Portal-3G operation is not successful")
                print(f"production_pilot_operation_id={existing.id}")
                print("production_pilot_state=ACTIVE")
                print("slurm_node=IDLE")
                print("slurm_queue=EMPTY")
                return 0
            if setting is not None:
                raise RuntimeError("unexpected Production Pilot setting exists before start")
            payload = validate_operation_payload(
                "slurm.production_pilot.start", APPROVED_PORTAL3G_PRODUCTION_PILOT
            )
            preflight = call_worker(
                "slurm.production_pilot.start",
                payload=payload,
                requested_by="origin-al",
                approved_by=None,
                idempotency_key=f"portal3g-production-preflight:{uuid.uuid4()}",
                dry_run=True,
                timeout_seconds=360,
            )
            validate_portal3g_production_pilot_plan(preflight)
            operation = _portal3g_create_operation(
                db, owner=owner, payload=payload, preflight=preflight
            )
            operation_id = operation.id
            db.commit()

        with SessionLocal() as db:
            running_operation = db.get(PortalOperation, operation_id)
            if running_operation is None or running_operation.status != OperationStatus.QUEUED:
                raise RuntimeError("Portal-3G operation disappeared before execution")
            transition(
                running_operation,
                OperationStatus.RUNNING,
                "fixed Production Pilot start began",
                db,
            )
            running_operation.started_at = utcnow()
            db.commit()

        try:
            result = call_worker(
                "slurm.production_pilot.start",
                payload=APPROVED_PORTAL3G_PRODUCTION_PILOT,
                requested_by="origin-al",
                approved_by="origin-al",
                idempotency_key=PORTAL3G_PRODUCTION_PILOT_IDEMPOTENCY_KEY,
                dry_run=False,
                timeout_seconds=480,
            )
        except WorkerClientError as exc:
            rollback_status = _portal3g_request_safety_drain()
            _portal3g_mark_failed(
                operation_id,
                error_code=exc.code,
                rollback_status=rollback_status,
            )
            raise
        if result.get("status") != "SUCCEEDED":
            error = result.get("error", {})
            error_code = str(
                error.get("code", "PORTAL3G_START_FAILED")
                if isinstance(error, dict)
                else "PORTAL3G_START_FAILED"
            )
            rollback_status = str(result.get("rollback_status", "REQUIRES_MANUAL_REVIEW"))[:32]
            _portal3g_mark_failed(
                operation_id,
                error_code=error_code,
                rollback_status=rollback_status,
                result=result,
            )
            raise RuntimeError(error_code)
        validate_portal3g_production_pilot_result(result)

        try:
            with SessionLocal() as db:
                persisted_operation = db.get(PortalOperation, operation_id)
                owner, _managed, _container = _portal3g_database_baseline(db)
                if (
                    persisted_operation is None
                    or persisted_operation.status != OperationStatus.RUNNING
                ):
                    raise RuntimeError("Portal-3G operation state changed during execution")
                persist_portal3g_production_pilot(
                    db, owner=owner, operation=persisted_operation, worker_result=result
                )
                persisted_operation.dry_run_result = {
                    **(persisted_operation.dry_run_result or {}),
                    "execution_result": cast(dict[str, Any], safe_metadata(result)),
                    "production_pilot_state": "ACTIVE",
                    "execution_enabled": True,
                }
                transition(
                    persisted_operation,
                    OperationStatus.SUCCEEDED,
                    "Slurm IDLE and Production Pilot ACTIVE verified",
                    db,
                )
                persisted_operation.worker_execution_id = str(result.get("request_id", "worker"))[
                    :64
                ]
                persisted_operation.rollback_status = "NOT_REQUIRED"
                persisted_operation.finished_at = utcnow()
                persisted_operation.result_summary = (
                    "Single-node single-managed-user Production Pilot ACTIVE; Slurm IDLE"
                )
                record_audit(
                    db,
                    event_type="slurm.production_pilot.started",
                    actor="origin-al",
                    actor_role="platform_owner",
                    source_ip="local-worker-socket",
                    user_agent="h100-portal-admin",
                    object_type="slurm_node",
                    object_id="sagsh100server",
                    result="SUCCESS",
                    metadata={
                        "previous_state": "DRAIN",
                        "new_state": "IDLE",
                        "production_pilot": "ACTIVE",
                        "scope": "single-node/single-managed-user/max-1-gpu",
                        "guard": "PASSING",
                        "gpu_health": "4/4 PASS",
                        "jobs_submitted": 0,
                    },
                    operation_id=persisted_operation.id,
                )
                db.commit()
        except OperationPayloadError, RuntimeError, SQLAlchemyError:
            rollback_status = _portal3g_request_safety_drain()
            _portal3g_mark_failed(
                operation_id,
                error_code="PORTAL3G_PERSISTENCE_FAILED",
                rollback_status=rollback_status,
                result=result,
            )
            raise

        with SessionLocal() as db:
            _owner, _managed, _container = _portal3g_database_baseline(
                db, expected_slurm_node_state="IDLE"
            )
            final_operation = db.get(PortalOperation, operation_id)
            setting = db.get(PortalSetting, PORTAL3G_SETTING_KEY)
            if not (
                final_operation is not None
                and final_operation.status == OperationStatus.SUCCEEDED
                and setting is not None
                and setting.value.get("state") == "ACTIVE"
                and setting.value.get("node_state") == "IDLE"
            ):
                raise RuntimeError("Portal-3G persisted state is incomplete")
            print(f"production_pilot_operation_id={final_operation.id}")
            print("production_pilot_state=ACTIVE")
            print("production_pilot_mode=SINGLE_NODE_SINGLE_MANAGED_USER")
            print("active_managed_user=origin-pilot")
            print("max_gpus=1")
            print("slurm_node=IDLE")
            print("slurm_scheduler=AVAILABLE")
            print("slurm_queue=EMPTY")
            print("jobs_submitted=0")
            return 0
    except (OperationPayloadError, WorkerClientError, RuntimeError, SQLAlchemyError) as exc:
        code = getattr(exc, "code", exc.__class__.__name__)
        print(f"PORTAL-3G PRODUCTION PILOT START BLOCKED — {code}", file=sys.stderr)
        return 2


def portal4a_create_origin_pilot_user(base_url: str) -> int:
    internal_password = random_token(48)
    validate_password(internal_password, "origin-pilot")
    with SessionLocal() as db:
        ensure_roles(db)
        existing = db.scalar(
            select(PortalUser).where(PortalUser.normalized_login == "origin-pilot")
        )
        if existing is not None:
            print(
                "PORTAL-4A-R USER CREATE BLOCKED — origin-pilot Portal login already exists",
                file=sys.stderr,
            )
            return 2
        managed = db.scalar(
            select(PortalManagedUser)
            .where(PortalManagedUser.unix_username == "origin-pilot")
            .with_for_update()
        )
        owner = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-al"))
        role = db.scalar(select(PortalRole).where(PortalRole.name == "user"))
        if managed is None or owner is None or role is None:
            print(
                "PORTAL-4A-R USER CREATE BLOCKED — platform identity baseline missing",
                file=sys.stderr,
            )
            return 2
        if (
            managed.onboarding_state != OnboardingState.ACTIVE
            or managed.uid != 20001
            or managed.gid != 20001
            or managed.container_name != "gpu-dev-origin-pilot"
            or managed.quota_bytes != 300 * 1024**3
        ):
            print(
                "PORTAL-4A-R USER CREATE BLOCKED — managed identity baseline changed",
                file=sys.stderr,
            )
            return 2
        if db.scalar(
            select(PortalComputeLease.id).where(PortalComputeLease.managed_user_id == managed.id)
        ):
            print("PORTAL-4A-R USER CREATE BLOCKED — initial lease already exists", file=sys.stderr)
            return 2
        user = PortalUser(
            login_name="origin-pilot",
            normalized_login="origin-pilot",
            display_name="Origin Pilot",
            unix_username="origin-pilot",
            account_state=AccountState.ACTIVE,
            password_state=PasswordState.RESET_REQUIRED,
            resource_onboarding_state=OnboardingState.ACTIVE,
            activated_at=utcnow(),
            roles=[role],
        )
        db.add(user)
        db.flush()
        db.add(
            PortalPasswordCredential(
                user_id=user.id,
                password_hash=hash_password(internal_password),
                password_changed_at=utcnow(),
            )
        )
        managed.portal_user_id = user.id
        owner.resource_onboarding_state = OnboardingState.NOT_ENROLLED
        db.execute(
            update(PortalSession)
            .where(PortalSession.user_id == owner.id)
            .values(owner_managed_user_id=None)
        )
        for record in db.scalars(
            select(PortalSshKey).where(PortalSshKey.managed_user_id == managed.id)
        ):
            record.owner_managed_user_id = managed.id
        container = db.scalar(
            select(PortalContainer).where(PortalContainer.managed_user_id == managed.id)
        )
        if container is None:
            print("PORTAL-4A-R USER CREATE BLOCKED — managed container missing", file=sys.stderr)
            return 2
        container.owner_managed_user_id = managed.id
        storage = PortalStorageResource(
            owner_managed_user_id=managed.id,
            root_path="/srv/gpu-platform/users/origin-pilot",
            quota_bytes=300 * 1024**3,
            state="ACTIVE",
        )
        db.add(storage)
        lease_start = utcnow()
        lease = create_lease(
            managed_user_id=managed.id,
            starts_at=lease_start,
            duration_seconds=96 * 60 * 60,
            gpu_count=1,
            approved_by=owner.id,
        )
        db.add(lease)
        db.flush()
        record_audit(
            db,
            event_type="portal.ordinary_user.created",
            actor="origin-al",
            actor_role="platform_owner",
            source_ip="local-console",
            user_agent="h100-portal-admin",
            object_type="portal_user",
            object_id=str(user.id),
            metadata={
                "role": "user",
                "managed_user_id": str(managed.id),
                "password_change_required": True,
                "credential_logged": False,
            },
        )
        record_audit(
            db,
            event_type="LEASE_CREATED",
            actor="origin-al",
            actor_role="platform_owner",
            source_ip="local-console",
            user_agent="h100-portal-admin",
            object_type="compute_lease",
            object_id=str(lease.id),
            metadata={
                "duration_seconds": 345600,
                "gpu_count": 1,
                "migration_created_at": lease_start.isoformat(),
                "auto_renew": False,
            },
        )
        db.commit()
        print("ordinary_user=origin-pilot")
        print("role=user")
        print("internal_credential=CREATED_NOT_DISCLOSED")
        print(f"login_url={base_url.rstrip('/')}/login")
        print(f"LEASE ID: {lease.id}")
        print(f"LEASE START: {lease.starts_at.isoformat()}")
        print(f"LEASE EXPIRES: {lease.expires_at.isoformat()}")
    return 0


def _portal4a_response(
    response: httpx.Response, expected_status: int, action: str
) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise RuntimeError(f"{action} returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{action} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{action} returned an invalid response object")
    return cast(dict[str, Any], payload)


def _portal4a_csrf_headers(client: httpx.Client) -> dict[str, str]:
    token = client.cookies.get(get_settings().csrf_cookie_name)
    if not token:
        raise RuntimeError("Portal session CSRF cookie is missing")
    return {
        "Origin": PORTAL4A_BROWSER_ORIGIN,
        "X-CSRF-Token": token,
        "User-Agent": "h100-portal4a-controlled-job-gate",
    }


def _portal4a_authenticate_for_job_gate(
    client: httpx.Client, temporary_password: str, changed_password: str
) -> None:
    csrf = _portal4a_response(client.get("/auth/csrf"), 200, "pre-auth CSRF").get("csrf_token")
    if not isinstance(csrf, str):
        raise RuntimeError("pre-auth CSRF response is incomplete")
    login = _portal4a_response(
        client.post(
            "/auth/login",
            headers={
                "Origin": PORTAL4A_BROWSER_ORIGIN,
                "X-CSRF-Token": csrf,
                "User-Agent": "h100-portal4a-controlled-job-gate",
            },
            json={"username": "origin-pilot", "password": temporary_password},
        ),
        200,
        "ordinary-user login",
    )
    user = login.get("user")
    if not isinstance(user, dict) or user.get("normalized_login") != "origin-pilot":
        raise RuntimeError("ordinary-user login identity is incorrect")
    _portal4a_response(
        client.post(
            "/auth/password",
            headers=_portal4a_csrf_headers(client),
            json={
                "current_password": temporary_password,
                "new_password": changed_password,
                "confirmation": changed_password,
            },
        ),
        200,
        "required password change",
    )
    current = _portal4a_response(client.get("/auth/me"), 200, "ordinary-user identity read")
    current_user = current.get("user")
    if (
        not isinstance(current_user, dict)
        or current_user.get("normalized_login") != "origin-pilot"
        or current_user.get("password_state") != "SET"
        or [item.get("name") for item in current_user.get("roles", [])] != ["user"]
    ):
        raise RuntimeError("ordinary-user session did not complete the password-change gate")


def _portal4a_submit_gate_job(
    client: httpx.Client,
    *,
    name: str,
    script_path: str,
    gpu_count: int,
    image_ref: str | None,
) -> dict[str, Any]:
    payload = _portal4a_response(
        client.post(
            "/self/jobs",
            headers=_portal4a_csrf_headers(client),
            json={
                "name": name,
                "script_path": script_path,
                "workdir": "workspace",
                "cpus": 1,
                "memory_mb": 2048 if gpu_count else 1024,
                "gpu_count": gpu_count,
                "time_limit_seconds": 600,
                "image_ref": image_ref,
                "idempotency_key": str(uuid.uuid4()),
            },
        ),
        200,
        f"{name} submission",
    )
    job = payload.get("job")
    if not isinstance(job, dict) or not isinstance(job.get("id"), str):
        raise RuntimeError(f"{name} submission response is incomplete")
    return cast(dict[str, Any], job)


def _portal4a_wait_for_gate_job(
    client: httpx.Client,
    job: dict[str, Any],
    *,
    expected_marker: str,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    job_id = str(job["id"])
    label = str(job.get("name", "Portal job"))
    deadline = time.monotonic() + timeout_seconds
    last_state: str | None = None
    terminal_states = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"}
    while time.monotonic() < deadline:
        current_payload = _portal4a_response(
            client.get(f"/self/jobs/{job_id}"), 200, f"{label} status"
        )
        current = current_payload.get("job")
        if not isinstance(current, dict):
            raise RuntimeError(f"{label} status response is incomplete")
        state = str(current.get("state", "UNKNOWN")).split("+", 1)[0]
        if state != last_state:
            print(f"{label}_state={state}")
            last_state = state
        if state in terminal_states:
            if state != "COMPLETED" or current.get("exit_code") not in {None, "0:0"}:
                raise RuntimeError(f"{label} finished with state={state}")
            logs = _portal4a_response(client.get(f"/self/jobs/{job_id}/logs"), 200, f"{label} logs")
            stdout = logs.get("stdout")
            if not isinstance(stdout, str) or expected_marker not in stdout:
                raise RuntimeError(f"{label} acceptance marker is missing")
            return cast(dict[str, Any], current)
        time.sleep(2)
    raise RuntimeError(f"{label} did not complete within the controlled timeout")


def portal4a_run_origin_pilot_job_gate() -> int:
    login_password = random_token(48)
    changed_password = random_token(48)
    parked_password = random_token(48)
    credential_armed = False
    cpu_job: dict[str, Any] | None = None
    gpu_job: dict[str, Any] | None = None
    try:
        with SessionLocal() as db:
            user = db.scalar(
                select(PortalUser).where(PortalUser.normalized_login == "origin-pilot")
            )
            managed = (
                db.scalar(
                    select(PortalManagedUser).where(PortalManagedUser.portal_user_id == user.id)
                )
                if user is not None
                else None
            )
            credential = (
                db.scalar(
                    select(PortalPasswordCredential).where(
                        PortalPasswordCredential.user_id == user.id
                    )
                )
                if user is not None
                else None
            )
            lease = (
                db.scalar(
                    select(PortalComputeLease).where(
                        PortalComputeLease.owner_managed_user_id == managed.id,
                        PortalComputeLease.state.in_(
                            {"ACTIVE", "RENEWAL_WINDOW", "RENEWAL_PENDING"}
                        ),
                        PortalComputeLease.expires_at > utcnow(),
                    )
                )
                if managed is not None
                else None
            )
            container = (
                db.scalar(
                    select(PortalContainer).where(
                        PortalContainer.owner_managed_user_id == managed.id,
                        PortalContainer.observed_state == "RUNNING",
                    )
                )
                if managed is not None
                else None
            )
            if (
                user is None
                or {role.name for role in user.roles} != {"user"}
                or managed is None
                or credential is None
                or lease is None
                or container is None
                or managed.host_access_state != "ENABLED"
                or managed.shell != "/bin/bash"
                or managed.compute_environment_state != "ACTIVE"
                or db.get(PortalSetting, PORTAL4A_FINAL_CREDENTIAL_SETTING_KEY) is not None
            ):
                raise RuntimeError("ordinary-user Portal job gate baseline is incomplete")
            credential.password_hash = hash_password(login_password)
            credential.password_changed_at = utcnow()
            user.password_state = PasswordState.RESET_REQUIRED
            user.failed_login_count = 0
            user.locked_until = None
            db.execute(
                update(PortalSession)
                .where(PortalSession.user_id == user.id, PortalSession.revoked_at.is_(None))
                .values(revoked_at=utcnow())
            )
            db.commit()
            credential_armed = True

        with httpx.Client(
            base_url=PORTAL4A_LOCAL_API_BASE,
            timeout=httpx.Timeout(75.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            _portal4a_authenticate_for_job_gate(client, login_password, changed_password)
            cpu_job = _portal4a_wait_for_gate_job(
                client,
                _portal4a_submit_gate_job(
                    client,
                    name="portal4a-cpu-gate",
                    script_path=PORTAL4A_CPU_GATE_SCRIPT,
                    gpu_count=0,
                    image_ref=None,
                ),
                expected_marker="PORTAL4A_CPU_GATE_PASS uid=20001 user=origin-pilot",
            )
            gpu_job = _portal4a_wait_for_gate_job(
                client,
                _portal4a_submit_gate_job(
                    client,
                    name="portal4a-gpu-gate",
                    script_path=PORTAL4A_GPU_GATE_SCRIPT,
                    gpu_count=1,
                    image_ref=PORTAL4A_APPROVED_IMAGE,
                ),
                expected_marker="PORTAL4A_GPU_GATE_PASS uid=20001 user=origin-pilot gpu_count=1",
            )
    except (RuntimeError, httpx.HTTPError, SQLAlchemyError) as exc:
        print(f"PORTAL-4A-R PORTAL JOB GATE BLOCKED — {exc}", file=sys.stderr)
        return_code = 2
    else:
        return_code = 0
    finally:
        if credential_armed:
            with SessionLocal() as db:
                user = db.scalar(
                    select(PortalUser).where(PortalUser.normalized_login == "origin-pilot")
                )
                credential = (
                    db.scalar(
                        select(PortalPasswordCredential).where(
                            PortalPasswordCredential.user_id == user.id
                        )
                    )
                    if user is not None
                    else None
                )
                if user is not None and credential is not None:
                    now = utcnow()
                    credential.password_hash = hash_password(parked_password)
                    credential.password_changed_at = now
                    user.password_state = PasswordState.RESET_REQUIRED
                    db.execute(
                        update(PortalSession)
                        .where(PortalSession.user_id == user.id, PortalSession.revoked_at.is_(None))
                        .values(revoked_at=now)
                    )
                    record_audit(
                        db,
                        event_type="portal4a.job_gate.credential_discarded",
                        actor="origin-al",
                        actor_role="platform_owner",
                        source_ip="local-console",
                        user_agent="h100-portal-admin",
                        object_type="portal_user",
                        object_id=str(user.id),
                        result="SUCCESS" if return_code == 0 else "FAILED",
                        metadata={"credential_logged": False, "sessions_revoked": True},
                    )
                    db.commit()
    if return_code == 0 and cpu_job is not None and gpu_job is not None:
        print("portal_job_submission=PASSED")
        print(f"cpu_portal_job_id={cpu_job['id']}")
        print(f"cpu_slurm_job_id={cpu_job['slurm_job_id']}")
        print(f"gpu_portal_job_id={gpu_job['id']}")
        print(f"gpu_slurm_job_id={gpu_job['slurm_job_id']}")
        print("slurm_job_owner=origin-pilot")
        print("internal_gate_credential=DISCARDED")
    return return_code


def _portal4a_completed_job(db: Session, managed: PortalManagedUser, gpu_count: int) -> PortalJob:
    jobs = db.scalars(
        select(PortalJob)
        .where(
            PortalJob.owner_managed_user_id == managed.id,
            PortalJob.gpu_count == gpu_count,
            PortalJob.slurm_job_id.is_not(None),
        )
        .order_by(PortalJob.created_at.desc())
    ).all()
    for job in jobs:
        result = call_worker(
            "self.job.status.read",
            payload={
                "portal_job_id": str(job.id),
                "managed_user_id": str(managed.id),
                "username": managed.unix_username,
                "uid": managed.uid,
                "gid": managed.gid,
                "slurm_job_id": job.slurm_job_id,
            },
            requested_by="origin-pilot",
            idempotency_key=f"portal4a-final-job-status:{job.id}",
            dry_run=False,
        )
        if (
            result.get("status") == "OK"
            and result.get("slurm_user") == "origin-pilot"
            and result.get("job_state") == "COMPLETED"
            and result.get("exit_code") in {None, "0:0"}
        ):
            job.state = "COMPLETED"
            job.exit_code = str(result.get("exit_code") or "0:0")
            job.finished_at = job.finished_at or utcnow()
            return job
    raise RuntimeError(f"completed Portal job with gpu_count={gpu_count} is missing")


def portal4a_issue_origin_pilot_test_login(base_url: str) -> int:
    temporary_password = random_token(48)
    validate_password(temporary_password, "origin-pilot")
    with SessionLocal() as db:
        if db.get(PortalSetting, PORTAL4A_FINAL_CREDENTIAL_SETTING_KEY) is not None:
            print(
                "PORTAL-4A-R CREDENTIAL ISSUE BLOCKED — final credential was already issued",
                file=sys.stderr,
            )
            return 2
        owner = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-al"))
        user = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-pilot"))
        if owner is None or user is None or {role.name for role in user.roles} != {"user"}:
            print(
                "PORTAL-4A-R CREDENTIAL ISSUE BLOCKED — ordinary-user account is not final",
                file=sys.stderr,
            )
            return 2
        managed = db.scalar(
            select(PortalManagedUser).where(PortalManagedUser.portal_user_id == user.id)
        )
        if (
            managed is None
            or managed.host_access_state != "DISABLED_BY_PLATFORM_POLICY"
            or managed.shell != "/usr/sbin/nologin"
            or managed.compute_environment_state != "ACTIVE"
        ):
            print(
                "PORTAL-4A-R CREDENTIAL ISSUE BLOCKED — Host policy or compute state is not final",
                file=sys.stderr,
            )
            return 2
        lease = db.scalar(
            select(PortalComputeLease)
            .where(
                PortalComputeLease.owner_managed_user_id == managed.id,
                PortalComputeLease.state.in_({"ACTIVE", "RENEWAL_WINDOW", "RENEWAL_PENDING"}),
                PortalComputeLease.expires_at > utcnow(),
            )
            .order_by(PortalComputeLease.expires_at.desc())
        )
        container = db.scalar(
            select(PortalContainer).where(
                PortalContainer.owner_managed_user_id == managed.id,
                PortalContainer.observed_state == "RUNNING",
            )
        )
        key = db.scalar(
            select(PortalSshKey).where(
                PortalSshKey.owner_managed_user_id == managed.id,
                PortalSshKey.active.is_(True),
                PortalSshKey.scope == "CONTAINER",
                PortalSshKey.host_install_state == "REMOVED_BY_POLICY",
                PortalSshKey.container_install_state == "INSTALLED",
            )
        )
        credential = db.scalar(
            select(PortalPasswordCredential).where(PortalPasswordCredential.user_id == user.id)
        )
        if lease is None or container is None or key is None or credential is None:
            print(
                "PORTAL-4A-R CREDENTIAL ISSUE BLOCKED — final resource baseline is incomplete",
                file=sys.stderr,
            )
            return 2
        try:
            _portal4a_completed_job(db, managed, 0)
            _portal4a_completed_job(db, managed, 1)
        except RuntimeError, WorkerClientError:
            print(
                "PORTAL-4A-R CREDENTIAL ISSUE BLOCKED — Portal CPU/GPU entry gate is incomplete",
                file=sys.stderr,
            )
            return 2
        now = utcnow()
        credential.password_hash = hash_password(temporary_password)
        credential.password_changed_at = now
        user.password_state = PasswordState.RESET_REQUIRED
        db.execute(
            update(PortalSession)
            .where(PortalSession.user_id == user.id, PortalSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        db.add(
            PortalSetting(
                key=PORTAL4A_FINAL_CREDENTIAL_SETTING_KEY,
                value={"issued_at": now.isoformat(), "username": "origin-pilot", "displayed": True},
                sensitive=False,
            )
        )
        record_audit(
            db,
            event_type="portal.ordinary_user.final_credential_issued",
            actor="origin-al",
            actor_role="platform_owner",
            source_ip="local-console",
            user_agent="h100-portal-admin",
            object_type="portal_user",
            object_id=str(user.id),
            metadata={
                "password_change_required": True,
                "prior_sessions_revoked": True,
                "credential_logged": False,
            },
        )
        db.commit()
        print("========================================")
        print("ORDINARY USER TEST LOGIN")
        print("========================================")
        print(f"LOGIN URL: {base_url.rstrip('/')}/login")
        print("USERNAME: origin-pilot")
        print(f"TEMPORARY PASSWORD: {temporary_password}")
        print("ROLE: user")
        print("PASSWORD CHANGE REQUIRED: YES")
        print("HOST SSH: DISABLED")
        print("DEVELOPMENT CONTAINER: AVAILABLE")
        print("MAX GPU: 1")
        print(f"LEASE EXPIRES: {lease.expires_at.isoformat()}")
        print("========================================")
    return 0


def portal4a_revoke_origin_pilot_host_access(approval_text: str) -> int:
    if approval_text != "批准 Portal-4A-R 撤销 origin-pilot 宿主访问":
        print("PORTAL-4A-R HOST REVOKE BLOCKED — approval text mismatch", file=sys.stderr)
        return 2
    with SessionLocal() as db:
        owner = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-al"))
        user = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-pilot"))
        managed = db.scalar(
            select(PortalManagedUser)
            .where(PortalManagedUser.unix_username == "origin-pilot")
            .with_for_update()
        )
        if owner is None or user is None or managed is None or managed.portal_user_id != user.id:
            print(
                "PORTAL-4A-R HOST REVOKE BLOCKED — Portal ownership binding missing",
                file=sys.stderr,
            )
            return 2
        lease = db.scalar(
            select(PortalComputeLease).where(
                PortalComputeLease.owner_managed_user_id == managed.id,
                PortalComputeLease.state.in_({"ACTIVE", "RENEWAL_WINDOW", "RENEWAL_PENDING"}),
                PortalComputeLease.expires_at > utcnow(),
            )
        )
        key = db.scalar(
            select(PortalSshKey).where(
                PortalSshKey.owner_managed_user_id == managed.id,
                PortalSshKey.fingerprint_sha256 == PORTAL3E_FINAL_KEY_FINGERPRINT,
                PortalSshKey.active.is_(True),
            )
        )
        container = db.scalar(
            select(PortalContainer).where(PortalContainer.owner_managed_user_id == managed.id)
        )
        if lease is None or key is None or container is None:
            print(
                "PORTAL-4A-R HOST REVOKE BLOCKED — lease/key/container baseline missing",
                file=sys.stderr,
            )
            return 2
        cpu_job = _portal4a_completed_job(db, managed, 0)
        gpu_job = _portal4a_completed_job(db, managed, 1)
        operation = PortalOperation(
            operation_type="host_access.revoke_managed_user",
            target_type="managed_user",
            target_id="origin-pilot",
            requested_by=owner.id,
            owner_managed_user_id=managed.id,
            approved_by=owner.id,
            request_summary="Portal计算入口通过后按普通用户政策撤销宿主SSH",
            validated_payload={
                "managed_user_id": str(managed.id),
                "key_record_id": str(key.id),
                "key_fingerprint": key.fingerprint_sha256,
                "cpu_job_id": cpu_job.slurm_job_id,
                "gpu_job_id": gpu_job.slurm_job_id,
            },
            idempotency_key="portal4a-origin-pilot-host-access-revoke-v1",
            risk_level=RiskLevel.HIGH,
            status=OperationStatus.RUNNING,
            approved_at=utcnow(),
            started_at=utcnow(),
        )
        db.add(operation)
        db.flush()
        result = call_worker(
            "host_access.revoke_managed_user",
            payload={
                "managed_user_id": str(managed.id),
                "username": managed.unix_username,
                "uid": managed.uid,
                "gid": managed.gid,
                "key_record_id": str(key.id),
                "key_fingerprint": key.fingerprint_sha256,
                "container_name": container.name,
                "expected_scope": "BOTH",
            },
            requested_by="origin-al",
            approved_by="origin-al",
            idempotency_key="portal4a-origin-pilot-host-access-revoke-v1",
            dry_run=False,
            timeout_seconds=120,
        )
        if (
            result.get("status") != "SUCCEEDED"
            or result.get("shell") != "/usr/sbin/nologin"
            or result.get("password") != "LOCKED"
            or result.get("host_authorized_keys") != "ABSENT"
            or result.get("container_authorized_keys") != "INSTALLED"
        ):
            operation.status = OperationStatus.FAILED
            operation.error_code = str(result.get("error", {}).get("code", "HOST_REVOKE_FAILED"))
            operation.finished_at = utcnow()
            db.commit()
            print("PORTAL-4A-R HOST REVOKE BLOCKED — Worker postcondition failed", file=sys.stderr)
            return 2
        managed.shell = "/usr/sbin/nologin"
        managed.host_access_state = "DISABLED_BY_PLATFORM_POLICY"
        managed.host_access_revoked_at = utcnow()
        key.scope = "CONTAINER"
        key.host_install_state = "REMOVED_BY_POLICY"
        key.container_install_state = "INSTALLED"
        operation.status = OperationStatus.SUCCEEDED
        operation.finished_at = utcnow()
        operation.worker_execution_id = str(result.get("request_id", ""))[:64] or None
        operation.result_summary = (
            "Host SSH removed by policy; container access and Portal jobs retained"
        )
        record_audit(
            db,
            event_type="HOST_ACCESS_REVOKED_BY_POLICY",
            actor="origin-al",
            actor_role="platform_owner",
            source_ip="local-console",
            user_agent="h100-portal-admin",
            object_type="managed_user",
            object_id=str(managed.id),
            metadata={
                "shell": "/usr/sbin/nologin",
                "host_authorized_keys": "ABSENT",
                "container_authorized_keys": "INSTALLED",
                "key_scope": "CONTAINER",
                "cpu_job_id": cpu_job.slurm_job_id,
                "gpu_job_id": gpu_job.slurm_job_id,
                "backup_path": result.get("backup_path"),
                "backup_sha256": result.get("backup_sha256"),
            },
            operation_id=operation.id,
        )
        db.commit()
        print(f"host_revoke_operation_id={operation.id}")
        print("host_access=DISABLED_BY_PLATFORM_POLICY")
        print("host_authorized_keys=ABSENT")
        print("shell=/usr/sbin/nologin")
        print("password=LOCKED")
        print("container_authorized_keys=INSTALLED")
        print("key_scope=CONTAINER")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="H100 Portal administrator bootstrap")
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap = subparsers.add_parser("bootstrap-origin-al")
    bootstrap.add_argument("--reset-setup-token", action="store_true")
    bootstrap.add_argument("--base-url", default="http://10.10.10.2:18080")
    subparsers.add_parser("prepare-origin-al")
    subparsers.add_parser("status-origin-al")
    subparsers.add_parser("plan-origin-pilot")
    subparsers.add_parser("draft-origin-pilot-stage")
    stage = subparsers.add_parser("stage-origin-pilot")
    stage.add_argument("--approval-text", required=True)
    subparsers.add_parser("revalidate-origin-pilot-activate")
    activate = subparsers.add_parser("activate-origin-pilot-final")
    activate.add_argument("--approval-text", required=True)
    portal3f = subparsers.add_parser("portal3f-origin-pilot")
    portal3f.add_argument("--approval-text", required=True)
    portal3g = subparsers.add_parser("portal3g-start-production-pilot")
    portal3g.add_argument("--approval-text", required=True)
    portal4a_user = subparsers.add_parser("portal4a-create-origin-pilot-user")
    portal4a_user.add_argument("--base-url", default="http://10.10.10.2:18080")
    subparsers.add_parser("portal4a-run-origin-pilot-job-gate")
    portal4a_credential = subparsers.add_parser("portal4a-issue-origin-pilot-test-login")
    portal4a_credential.add_argument("--base-url", default="http://10.10.10.2:18080")
    portal4a_revoke = subparsers.add_parser("portal4a-revoke-origin-pilot-host-access")
    portal4a_revoke.add_argument("--approval-text", required=True)
    args = parser.parse_args()
    if args.command == "prepare-origin-al":
        return prepare_origin_al()
    if args.command == "bootstrap-origin-al":
        return bootstrap_origin_al(args.reset_setup_token, args.base_url)
    if args.command == "status-origin-al":
        return status_origin_al()
    if args.command == "plan-origin-pilot":
        return plan_origin_pilot()
    if args.command == "draft-origin-pilot-stage":
        return draft_origin_pilot_stage()
    if args.command == "stage-origin-pilot":
        return stage_origin_pilot(args.approval_text)
    if args.command == "revalidate-origin-pilot-activate":
        return revalidate_origin_pilot_activate()
    if args.command == "activate-origin-pilot-final":
        return activate_origin_pilot_final(args.approval_text)
    if args.command == "portal3f-origin-pilot":
        return portal3f_origin_pilot(args.approval_text)
    if args.command == "portal3g-start-production-pilot":
        return portal3g_start_production_pilot(args.approval_text)
    if args.command == "portal4a-create-origin-pilot-user":
        return portal4a_create_origin_pilot_user(args.base_url)
    if args.command == "portal4a-run-origin-pilot-job-gate":
        return portal4a_run_origin_pilot_job_gate()
    if args.command == "portal4a-issue-origin-pilot-test-login":
        return portal4a_issue_origin_pilot_test_login(args.base_url)
    if args.command == "portal4a-revoke-origin-pilot-host-access":
        return portal4a_revoke_origin_pilot_host_access(args.approval_text)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
