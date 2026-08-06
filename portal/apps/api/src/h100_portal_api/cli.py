import argparse
import pwd
import sys
from datetime import timedelta

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
    PortalOperation,
    PortalOperationEvent,
    PortalPasswordSetupToken,
    PortalRole,
    PortalUser,
    utcnow,
)
from h100_portal_api.rbac import PERMISSIONS
from h100_portal_api.routes.operations import (
    APPROVED_ORIGIN_PILOT_STAGE,
    enrich_user_plan_with_portal_state,
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
