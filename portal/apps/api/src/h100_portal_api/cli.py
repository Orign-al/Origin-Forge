import argparse
import pwd
import sys
import uuid
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.config import get_settings
from h100_portal_api.database import SessionLocal
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordState,
    RiskLevel,
)
from h100_portal_api.models import (
    PortalManagedUser,
    PortalOperation,
    PortalOperationApproval,
    PortalOperationEvent,
    PortalPasswordSetupToken,
    PortalRole,
    PortalSshKey,
    PortalUser,
    utcnow,
)
from h100_portal_api.rbac import PERMISSIONS
from h100_portal_api.routes.operations import (
    APPROVED_ORIGIN_PILOT_STAGE,
    PORTAL3C_STAGE_APPROVAL_REFERENCE,
    PORTAL3C_STAGE_IDEMPOTENCY_KEY,
    PORTAL3E_FINAL_APPROVAL_REFERENCE,
    PORTAL3E_FINAL_APPROVAL_TEXT,
    PORTAL3E_FINAL_DRY_RUN_OPERATION_ID,
    PORTAL3E_FINAL_IDEMPOTENCY_KEY,
    PORTAL3E_FINAL_KEY_FINGERPRINT,
    PORTAL3E_FINAL_KEY_RECORD_ID,
    PORTAL3E_FINAL_MANAGED_USER_ID,
    OperationPayloadError,
    enrich_user_plan_with_portal_state,
    execute_operation,
    transition,
    validate_activate_database_bindings,
    validate_activate_execution_result,
    validate_activate_worker_result,
    validate_operation_payload,
)
from h100_portal_api.security import digest_secret, normalize_login, random_token, safe_metadata
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
                .values(revoked_at=utcnow())
            )
        token = random_token(48)
        db.add(
            PortalPasswordSetupToken(
                user_id=user.id,
                token_hash=digest_secret(token),
                created_at=utcnow(),
                expires_at=utcnow() + timedelta(minutes=get_settings().setup_token_minutes),
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
    print(f"{base_url.rstrip('/')}/setup-password?token={token}")
    print()
    print("SSH Tunnel 仍可作为回退访问方式。")
    print("链接 30 分钟有效，仅可使用一次。")
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
        validate_activate_execution_result(final_records, execution)
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
