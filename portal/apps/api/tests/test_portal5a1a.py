import uuid
from datetime import timedelta

import pytest
from fastapi import Request
from fastapi.exceptions import HTTPException
from h100_portal_api.auth import AuthContext
from h100_portal_api.enums import AccountState, OnboardingState, PasswordState
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalComputeLease,
    PortalComputeResourceRequest,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalPasswordCredential,
    PortalProvisionPlan,
    PortalResourceReservation,
    PortalRole,
    PortalSession,
    PortalStorageResource,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.routes.compute_requests import (
    _active_reservations,
    _rolled_back_stage_retry,
    _safe_operation_view,
    assert_zero_compute_side_effects,
    authorize_provision_retry,
    create_provision_plan,
)
from h100_portal_api.schemas import (
    ComputeProvisionActionRequest,
    ComputeProvisionRetryAuthorizationRequest,
)
from h100_portal_api.security import hash_password
from h100_portal_api.worker_client import WorkerClientError
from sqlalchemy import func, select

PASSWORD = "A long Portal compute request passphrase 2026"
STANDARD_REQUEST = {
    "requested_gpu_max": 1,
    "requested_storage_bytes": 322122547200,
    "requested_container_profile": "STANDARD_8CPU_32GB",
    "requested_lease_seconds": 345600,
    "purpose": "多用户平台验收",
    "user_note": None,
}


def test_immutable_first_failure_evidence_is_view_only_and_exactly_bound() -> None:
    operation = PortalOperation(
        id=uuid.UUID("2a32b900-4dd9-4962-82fe-127e537ba452"),
        operation_type="compute.provision.stage",
        target_type="compute_resource_request",
        target_id="25aafaf9-b4f8-4cb7-beb0-127ed9923d83",
        validated_payload={
            "request_id": "25aafaf9-b4f8-4cb7-beb0-127ed9923d83",
            "plan_id": "4160b0d8-612e-406a-92cc-00c3af9e8356",
        },
        error_code="COMPUTE_STAGE_FAILED",
        rollback_status="ROLLED_BACK",
        dry_run_result=None,
    )
    view = _safe_operation_view(operation)
    assert view is not None
    assert view["side_effect_classification"] == "NO_SIDE_EFFECT"
    assert view["first_failed_step"] == "EXPLICIT_STAGE_CONFIRMATION_GATE"
    assert view["failed_handler"] == "h100-provision-stage"
    assert operation.dry_run_result is None

    operation.validated_payload = {
        **operation.validated_payload,
        "plan_id": str(uuid.uuid4()),
    }
    unbound = _safe_operation_view(operation)
    assert unbound is not None
    assert unbound["first_failed_step"] is None


def account(database, *, login: str, role: str = "user") -> PortalUser:  # type: ignore[no-untyped-def]
    selected_role = database.scalar(select(PortalRole).where(PortalRole.name == role))
    user = PortalUser(
        login_name=login,
        normalized_login=login,
        display_name=login,
        unix_username=None,
        account_state=AccountState.ACTIVE,
        password_state=PasswordState.SET,
        resource_onboarding_state=OnboardingState.NOT_ENROLLED,
        roles=[selected_role],
    )
    database.add(user)
    database.flush()
    database.add(
        PortalPasswordCredential(
            user_id=user.id,
            password_hash=hash_password(PASSWORD),
            password_changed_at=utcnow(),
        )
    )
    database.commit()
    return user


def login(client, origin_headers, username: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        headers={**origin_headers, "X-CSRF-Token": csrf},
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200
    return {**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]}


def submit(client, headers, **overrides):  # type: ignore[no-untyped-def]
    payload = {
        **STANDARD_REQUEST,
        "idempotency_key": str(uuid.uuid4()),
        **overrides,
    }
    return client.post("/api/v1/self/compute-request", headers=headers, json=payload)


def review(client, headers, request_id: str, decision: str, note: str | None = None):  # type: ignore[no-untyped-def]
    return client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/review",
        headers=headers,
        json={
            "decision": decision,
            "review_note": note,
            "idempotency_key": str(uuid.uuid4()),
        },
    )


def stage_contract_fixture() -> dict[str, object]:
    return {
        "status": "PASS",
        "contract_sha256": "d" * 64,
        "handler": {
            "identity": "h100-provision-stage",
            "deployed_path": "/usr/local/sbin/h100-provision-stage",
            "sha256": "a" * 64,
            "integrity_status": "PASS",
        },
        "argv_contract": {
            "version": "compute-provision-stage-argv-v1",
            "sha256": "b" * 64,
            "shape_status": "PASS",
            "shell_argument_count": 14,
            "expected_shell_argument_count": 14,
            "multi_digit_position_status": "PASS",
            "argument_13": {
                "index": 13,
                "semantic_role": "EXPLICIT_STAGE_CONFIRMATION_FLAG",
                "binding_status": "VALID",
            },
            "argument_14": {
                "index": 14,
                "semantic_role": "CONFIRMED_TARGET_USERNAME",
                "binding_status": "VALID",
            },
        },
        "confirmation_gate": {
            "identity": "EXPLICIT_STAGE_CONFIRMATION_GATE",
            "validator_version": "compute-provision-stage-confirmation-validator-v1",
            "validator_sha256": "c" * 64,
            "status": "PASS",
        },
    }


def worker_plan(uid: int = 20002, project_id: int = 30002, port: int = 22024):
    def call(operation_type, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["dry_run"] is True
        payload = kwargs["payload"]
        if operation_type == "compute.provision.plan":
            return {
                "status": "DRY_RUN",
                "handler": operation_type,
                "plan_status": "READY",
                "execution_enabled": False,
                "proposed_uid": uid,
                "proposed_gid": uid,
                "proposed_project_id": project_id,
                "proposed_ssh_port": port,
                "proposed_container_name": f"gpu-dev-{payload['username']}",
                "validation_results": [
                    {"check": "host_truth", "status": "PASS", "detail": "read only"}
                ],
                "conflicts": [],
                "infrastructure_side_effects": "NONE",
            }
        if operation_type == "compute.provision.retry_verify":
            return {
                "status": "DRY_RUN",
                "handler": operation_type,
                "retry_verification_status": "VERIFIED_ZERO_RESIDUE",
                "script_integrity": "PASS",
                "failed_scripts": [],
                "resource_residue": [],
                "unknown_resource_state": [],
                "infrastructure_side_effects": "NONE",
                "execution_enabled": False,
            }
        assert operation_type == "compute.provision.dry_run"
        return {
            "status": "DRY_RUN",
            "handler": operation_type,
            "dry_run_status": "READY_FOR_PROVISION",
            "execution_enabled": False,
            "validation_results": [
                {"check": "exact_reservation", "status": "PASS", "detail": "read only"}
            ],
            "conflicts": [],
            "resource_writes": {
                "linux_user": False,
                "container": False,
                "xfs_quota": False,
                "slurm_association": False,
                "gpu_policy": False,
                "lease": False,
            },
            "stage_contract": stage_contract_fixture(),
            "infrastructure_side_effects": "NONE",
        }

    return call


def worker_stage():
    plan_worker = worker_plan()

    def call(operation_type, **kwargs):  # type: ignore[no-untyped-def]
        if operation_type != "compute.provision.stage":
            return plan_worker(operation_type, **kwargs)
        assert kwargs["dry_run"] is False
        payload = kwargs["payload"]
        assert payload["execution_enabled"] is True
        assert payload["dry_run_stage_contract"] == stage_contract_fixture()
        assert kwargs["idempotency_key"] == f"compute-stage:{payload['stage_operation_id']}"
        assert set(payload["reservation_ids"]) == {
            "UID",
            "GID",
            "PROJECT_ID",
            "SSH_PORT",
            "CONTAINER_NAME",
        }
        return {
            "status": "SUCCEEDED",
            "handler": operation_type,
            "request_id": str(uuid.uuid4()),
            "idempotent_replay": False,
            "stage": {
                "username": payload["username"],
                "uid": payload["uid"],
                "gid": payload["gid"],
                "shell": "/usr/sbin/nologin",
                "password": "LOCKED",
                "host_ssh": "DISABLED",
                "host_authorized_keys": "ABSENT",
                "container_authorized_keys": "ABSENT",
                "onboarding_state": "STAGED",
                "ssh_key_state": "REQUIRED_BEFORE_ACTIVATION",
                "storage_path": f"/srv/gpu-platform/users/{payload['username']}",
                "slurm": {
                    "account": "company",
                    "qos": "general",
                    "max_gpus": payload["gpu_max"],
                    "max_tres": f"gres/gpu={payload['gpu_max']}",
                },
                "container": {
                    "name": payload["container_name"],
                    "state": "STOPPED",
                    "gpu": "NONE",
                    "ssh_port": payload["ssh_port"],
                    "image_digest": "sha256:" + "a" * 64,
                },
                "filesystem_isolation": {
                    "origin_pilot_to_target": "DENIED",
                    "target_to_origin_pilot": "DENIED",
                    "markers_removed": True,
                },
                "lease": {"state": "NOT_STARTED", "starts_at": None, "expires_at": None},
            },
        }

    return call


def failed_stage_fixture(database, *, login: str = "rolled-back-stage-user"):
    """Persist the exact clean-rollback aggregate accepted by the retry gate."""
    owner = account(database, login=login)
    now = utcnow()
    item = PortalComputeResourceRequest(
        portal_account_id=owner.id,
        requested_by=owner.id,
        username=owner.normalized_login,
        status="FAILED",
        active_slot=None,
        requested_gpu_max=1,
        requested_storage_bytes=300 * 1024**3,
        requested_container_profile="STANDARD_8CPU_32GB",
        requested_lease_seconds=96 * 60 * 60,
        purpose="clean rollback retry fixture",
        submitted_at=now,
        reviewed_at=now,
        reviewed_by=owner.id,
        approved_at=now,
    )
    database.add(item)
    database.flush()
    plan = PortalProvisionPlan(  # noqa: S604 - ORM shell field, not subprocess execution
        request_id=item.id,
        portal_account_id=owner.id,
        state="FAILED",
        username=owner.normalized_login,
        uid=20021,
        gid=20021,
        project_id=30021,
        container_name=f"gpu-dev-{owner.normalized_login}",
        container_ssh_port=22041,
        storage_bytes=300 * 1024**3,
        container_profile="STANDARD_8CPU_32GB",
        container_cpus=8,
        container_memory_gb=32,
        container_pids_limit=4096,
        container_gpu=0,
        slurm_account="company",
        slurm_qos="general",
        gpu_max=1,
        lease_seconds=96 * 60 * 60,
        lease_state="NOT_STARTED",
        host_ssh_enabled=False,
        shell="/usr/sbin/nologin",
        password_state="LOCKED",
        execution_enabled=True,
        reservation_expires_at=now + timedelta(hours=1),
        allocator_result={"plan_status": "READY"},
        dry_run_result={"dry_run_status": "READY_FOR_PROVISION"},
        dry_run_at=now,
        created_by=owner.id,
    )
    database.add(plan)
    database.flush()
    item.provision_plan_id = plan.id
    values = {
        "UID": str(plan.uid),
        "GID": str(plan.gid),
        "PROJECT_ID": str(plan.project_id),
        "SSH_PORT": str(plan.container_ssh_port),
        "CONTAINER_NAME": plan.container_name,
    }
    reservations = []
    for resource_type, resource_value in values.items():
        row = PortalResourceReservation(
            plan_id=plan.id,
            request_id=item.id,
            portal_account_id=owner.id,
            resource_type=resource_type,
            resource_value=resource_value,
            active_key=None,
            state="RELEASED",
            reserved_at=now - timedelta(minutes=5),
            expires_at=now + timedelta(hours=1),
            consumed_at=None,
            released_at=now,
        )
        database.add(row)
        reservations.append(row)
    database.flush()
    reservation_ids = {row.resource_type: str(row.id) for row in reservations}
    operation = PortalOperation(
        operation_type="compute.provision.stage",
        target_type="compute_resource_request",
        target_id=str(item.id),
        requested_by=owner.id,
        approved_by=owner.id,
        request_summary="failed transactional Stage fixture",
        validated_payload={
            "request_id": str(item.id),
            "plan_id": str(plan.id),
            "reservation_ids": reservation_ids,
        },
        idempotency_key=f"compute.provision.stage:{uuid.uuid4()}",
        risk_level="CRITICAL",
        status="FAILED",
        created_at=now,
        started_at=now,
        finished_at=now,
        error_code="COMPUTE_STAGE_FAILED",
        rollback_status="ROLLED_BACK",
    )
    database.add(operation)
    database.commit()
    return owner, item, plan, reservations, operation


def test_user_submits_fixed_own_request_and_abuse_is_rejected(
    client, database, origin_headers
) -> None:  # type: ignore[no-untyped-def]
    user = account(database, login="request-user")
    headers = login(client, origin_headers, user.normalized_login)
    xss = '<img src=x onerror="alert(1)">\nlog-forge'
    created = submit(client, headers, purpose=xss)
    assert created.status_code == 201
    body = created.json()["request"]
    assert body["status"] == "REQUESTED"
    assert body["purpose"] == xss
    assert body["requested_gpu_max"] == 1
    assert body["requested_storage_bytes"] == 300 * 1024**3
    assert body["requested_lease_seconds"] == 96 * 60 * 60
    assert body["plan"] is None
    request_id = body["id"]

    duplicate = submit(client, headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "ACTIVE_COMPUTE_REQUEST_EXISTS"
    assert submit(client, headers, requested_gpu_max=2).status_code == 422
    assert submit(client, headers, requested_storage_bytes=100 * 1024**3).status_code == 422
    assert submit(client, headers, requested_lease_seconds=97 * 3600).status_code == 422
    assert submit(client, headers, uid=20002).status_code == 422
    assert (
        client.post(
            "/api/v1/self/compute-request",
            headers=origin_headers,
            json={**STANDARD_REQUEST, "idempotency_key": str(uuid.uuid4())},
        ).status_code
        == 403
    )

    item = database.get(PortalComputeResourceRequest, uuid.UUID(request_id))
    assert item is not None
    assert item.portal_account_id == user.id == item.requested_by
    assert item.managed_user_id is None
    assert item.active_slot == 1
    operation = database.scalar(
        select(PortalOperation).where(
            PortalOperation.operation_type == "compute_resource_request.create"
        )
    )
    assert operation is not None
    assert operation.owner_managed_user_id is None
    assert "purpose" not in operation.validated_payload
    assert all(assert_zero_compute_side_effects(database, user.id).values())
    assert database.scalar(select(func.count()).select_from(PortalProvisionPlan)) == 0
    assert database.scalar(select(func.count()).select_from(PortalResourceReservation)) == 0
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 0
    events = database.scalars(select(PortalAuditEvent.event_type)).all()
    assert "COMPUTE_RESOURCE_REQUEST_CREATED" in events


def test_compute_request_submission_uses_existing_rate_limiter(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    user = account(database, login="rate-limited-user")
    headers = login(client, origin_headers, user.normalized_login)
    monkeypatch.setattr(
        "h100_portal_api.routes.compute_requests.rate_limiter.allowed",
        lambda *_args, **_kwargs: False,
    )
    denied = submit(client, headers)
    assert denied.status_code == 429
    assert denied.json()["detail"]["code"] == "COMPUTE_REQUEST_RATE_LIMITED"
    assert database.scalar(select(func.count()).select_from(PortalComputeResourceRequest)) == 0
    assert all(assert_zero_compute_side_effects(database, user.id).values())


def test_compute_request_rejects_existing_managed_username_collision(
    client, database, origin_headers
) -> None:  # type: ignore[no-untyped-def]
    existing_owner = account(database, login="existing-compute-owner")
    target = account(database, login="managed-name-collision")
    database.add(
        PortalManagedUser(  # noqa: S604 - ORM login-shell field, not subprocess execution
            portal_user_id=existing_owner.id,
            unix_username=target.normalized_login,
            uid=20500,
            gid=20500,
            shell="/usr/sbin/nologin",
            host_access_state="DISABLED_BY_PLATFORM_POLICY",
            compute_environment_state="ACTIVE",
            gpu_isolation_state="PLANNED",
            onboarding_state=OnboardingState.DRAFT,
            ssh_key_state="NOT_REQUIRED_FOR_STAGE",
            ssh_key_count=0,
        )
    )
    database.commit()

    headers = login(client, origin_headers, target.normalized_login)
    denied = submit(client, headers)
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "COMPUTE_USERNAME_CONFLICT"
    assert all(assert_zero_compute_side_effects(database, target.id).values())


def test_self_scope_idor_cancel_and_approval_permissions(client, database, origin_headers) -> None:  # type: ignore[no-untyped-def]
    owner = account(database, login="request-owner")
    attacker = account(database, login="request-attacker")
    owner_headers = login(client, origin_headers, owner.normalized_login)
    request_id = submit(client, owner_headers).json()["request"]["id"]

    attacker_headers = login(client, origin_headers, attacker.normalized_login)
    own_view = client.get("/api/v1/self/compute-request", headers=attacker_headers)
    assert own_view.status_code == 200
    assert own_view.json()["request"] is None
    denied_cancel = client.post(
        f"/api/v1/self/compute-request/{request_id}/cancel",
        headers=attacker_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert denied_cancel.status_code == 404
    assert (
        client.get(
            f"/api/v1/admin/compute-resource-requests/{request_id}", headers=attacker_headers
        ).status_code
        == 403
    )
    assert review(client, attacker_headers, request_id, "APPROVE").status_code == 403
    assert (
        client.post(
            f"/api/v1/admin/compute-resource-requests/{request_id}/plan",
            headers=attacker_headers,
            json={"idempotency_key": str(uuid.uuid4())},
        ).status_code
        == 403
    )

    owner_headers = login(client, origin_headers, owner.normalized_login)
    cancelled = client.post(
        f"/api/v1/self/compute-request/{request_id}/cancel",
        headers=owner_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["request"]["status"] == "CANCELLED"
    assert submit(client, owner_headers).status_code == 201


def test_only_owner_or_admin_can_review_and_rejection_requires_safe_note(
    client, database, origin_headers
) -> None:  # type: ignore[no-untyped-def]
    user = account(database, login="review-user")
    admin = account(database, login="review-admin", role="platform_admin")
    operator = account(database, login="review-operator", role="operator")
    user_headers = login(client, origin_headers, user.normalized_login)
    request_id = submit(client, user_headers).json()["request"]["id"]

    operator_headers = login(client, origin_headers, operator.normalized_login)
    assert review(client, operator_headers, request_id, "APPROVE").status_code == 403
    admin_headers = login(client, origin_headers, admin.normalized_login)
    assert review(client, admin_headers, request_id, "REJECT").status_code == 422
    rejected = review(
        client,
        admin_headers,
        request_id,
        "REJECT",
        "资源暂不可用 <script>alert(1)</script>",
    )
    assert rejected.status_code == 200
    assert rejected.json()["request"]["status"] == "REJECTED"
    assert rejected.json()["request"]["review_note"] == "资源暂不可用 <script>alert(1)</script>"
    database.expire_all()
    item = database.get(PortalComputeResourceRequest, uuid.UUID(request_id))
    assert item is not None and item.active_slot is None
    assert all(assert_zero_compute_side_effects(database, user.id).values())


def test_dual_role_applicant_cannot_review_or_plan_own_request(
    client, database, origin_headers
) -> None:  # type: ignore[no-untyped-def]
    applicant = account(database, login="dual-role-applicant")
    admin_role = database.scalar(select(PortalRole).where(PortalRole.name == "platform_admin"))
    assert admin_role is not None
    applicant.roles.append(admin_role)
    database.commit()

    headers = login(client, origin_headers, applicant.normalized_login)
    request_id = submit(client, headers).json()["request"]["id"]
    denied = review(client, headers, request_id, "APPROVE", "self approval")
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "COMPUTE_REQUEST_SELF_ADMINISTRATION_DENIED"
    database.expire_all()
    item = database.get(PortalComputeResourceRequest, uuid.UUID(request_id))
    assert item is not None and item.status == "REQUESTED"


def test_admin_approval_reservation_and_dry_run_remain_zero_side_effect_before_stage(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    user = account(database, login="plan-user")
    admin = account(database, login="plan-admin", role="platform_owner")
    user_headers = login(client, origin_headers, user.normalized_login)
    request_id = submit(client, user_headers).json()["request"]["id"]
    admin_headers = login(client, origin_headers, admin.normalized_login)
    approved = review(client, admin_headers, request_id, "APPROVE", "标准规格批准")
    assert approved.status_code == 200
    assert approved.json()["request"]["status"] == "APPROVED"

    monkeypatch.setattr("h100_portal_api.routes.compute_requests.call_worker", worker_plan())
    planned = client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/plan",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert planned.status_code == 200
    plan = planned.json()["plan"]
    assert plan["state"] == "RESERVED"
    assert plan["uid"] == plan["gid"] == 20002
    assert plan["project_id"] == 30002
    assert plan["container_ssh_port"] == 22024
    assert plan["container_name"] == "gpu-dev-plan-user"
    assert plan["host_ssh"] == "DISABLED"
    assert plan["shell"] == "/usr/sbin/nologin"
    assert plan["lease_state"] == "NOT_STARTED"
    assert plan["execution_enabled"] is False
    assert database.scalar(select(func.count()).select_from(PortalResourceReservation)) == 5

    dry_run = client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/dry-run",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["status"] == "READY_FOR_PROVISION"
    assert dry_run.json()["plan"]["state"] == "READY_FOR_PROVISION"
    dry_run_operation = database.scalar(
        select(PortalOperation).where(PortalOperation.operation_type == "compute.provision.dry_run")
    )
    assert dry_run_operation is not None
    assert dry_run_operation.dry_run_result is not None
    assert dry_run_operation.dry_run_result["stage_contract"]["status"] == "PASS"
    assert dry_run_operation.validated_payload["stage_contract_sha256"] == "d" * 64
    denied = client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/provision",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert denied.status_code == 428
    assert denied.json()["detail"]["code"] == "REAUTH_REQUIRED"

    database.expire_all()
    item = database.get(PortalComputeResourceRequest, uuid.UUID(request_id))
    assert item is not None and item.status == "PROVISION_PLAN_READY"
    assert item.managed_user_id is None
    assert all(assert_zero_compute_side_effects(database, user.id).values())
    assert database.scalar(select(func.count()).select_from(PortalManagedUser)) == 0
    assert database.scalar(select(func.count()).select_from(PortalContainer)) == 0
    assert database.scalar(select(func.count()).select_from(PortalStorageResource)) == 0
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 0
    event_types = database.scalars(select(PortalAuditEvent.event_type)).all()
    assert "COMPUTE_RESOURCE_REQUEST_APPROVED" in event_types
    assert "PROVISION_PLAN_CREATED" in event_types
    assert "PROVISION_DRY_RUN_COMPLETED" in event_types


def test_dry_run_missing_stage_contract_evidence_never_becomes_ready(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    user = account(database, login="contract-incomplete-user")
    admin = account(database, login="contract-incomplete-admin", role="platform_owner")
    request_id = submit(client, login(client, origin_headers, user.normalized_login)).json()[
        "request"
    ]["id"]
    admin_headers = login(client, origin_headers, admin.normalized_login)
    assert review(client, admin_headers, request_id, "APPROVE", "approved once").status_code == 200
    complete_worker = worker_plan()

    def incomplete_worker(operation_type, **kwargs):  # type: ignore[no-untyped-def]
        result = complete_worker(operation_type, **kwargs)
        if operation_type == "compute.provision.dry_run":
            result.pop("stage_contract")
        return result

    monkeypatch.setattr("h100_portal_api.routes.compute_requests.call_worker", incomplete_worker)
    assert (
        client.post(
            f"/api/v1/admin/compute-resource-requests/{request_id}/plan",
            headers=admin_headers,
            json={"idempotency_key": str(uuid.uuid4())},
        ).status_code
        == 200
    )
    rejected = client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/dry-run",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "PROVISION_DRY_RUN_CONTRACT_INCOMPLETE"
    database.expire_all()
    item = database.get(PortalComputeResourceRequest, uuid.UUID(request_id))
    assert item is not None and item.status == "PROVISION_PLAN_READY"
    plan = database.get(PortalProvisionPlan, item.provision_plan_id)
    assert plan is not None and plan.state == "RESERVED"
    assert (
        database.scalar(
            select(func.count())
            .select_from(PortalOperation)
            .where(PortalOperation.operation_type == "compute.provision.dry_run")
        )
        == 0
    )
    assert all(assert_zero_compute_side_effects(database, user.id).values())


def test_reserved_stage_requires_reauth_consumes_plan_without_reapproval_or_lease(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    user = account(database, login="stage-user")
    admin = account(database, login="stage-admin", role="platform_owner")
    user_headers = login(client, origin_headers, user.normalized_login)
    request_id = submit(client, user_headers).json()["request"]["id"]
    admin_headers = login(client, origin_headers, admin.normalized_login)
    assert review(client, admin_headers, request_id, "APPROVE", "approved once").status_code == 200
    database.expire_all()
    approved_item = database.get(PortalComputeResourceRequest, uuid.UUID(request_id))
    assert approved_item is not None
    approval_actor = approved_item.reviewed_by
    approval_time = approved_item.approved_at
    approval_audits_before = database.scalar(
        select(func.count())
        .select_from(PortalAuditEvent)
        .where(PortalAuditEvent.event_type == "COMPUTE_RESOURCE_REQUEST_APPROVED")
    )

    monkeypatch.setattr("h100_portal_api.routes.compute_requests.call_worker", worker_stage())
    assert (
        client.post(
            f"/api/v1/admin/compute-resource-requests/{request_id}/plan",
            headers=admin_headers,
            json={"idempotency_key": str(uuid.uuid4())},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/admin/compute-resource-requests/{request_id}/dry-run",
            headers=admin_headers,
            json={"idempotency_key": str(uuid.uuid4())},
        ).status_code
        == 200
    )

    blocked = client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/provision",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert blocked.status_code == 428
    reauth = client.post(
        "/api/v1/auth/reauthenticate",
        headers=admin_headers,
        json={"password": PASSWORD},
    )
    assert reauth.status_code == 200
    staged = client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/provision",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert staged.status_code == 200
    assert staged.json()["status"] == "KEY_ENROLLMENT_PENDING"

    database.expire_all()
    item = database.get(PortalComputeResourceRequest, uuid.UUID(request_id))
    assert item is not None and item.status == "KEY_ENROLLMENT_PENDING"
    assert item.reviewed_by == approval_actor and item.approved_at == approval_time
    managed = database.get(PortalManagedUser, item.managed_user_id)
    assert managed is not None
    assert managed.onboarding_state == OnboardingState.STAGED
    assert managed.compute_environment_state == "STAGED"
    assert managed.shell == "/usr/sbin/nologin"
    assert managed.host_access_state == "DISABLED_BY_PLATFORM_POLICY"
    assert managed.ssh_key_count == 0
    assert (
        database.scalar(
            select(func.count())
            .select_from(PortalComputeLease)
            .where(PortalComputeLease.managed_user_id == managed.id)
        )
        == 0
    )
    container = database.scalar(
        select(PortalContainer).where(PortalContainer.owner_managed_user_id == managed.id)
    )
    assert container is not None and container.observed_state == "STOPPED"
    assert container.safe_spec["gpu"] == "NONE"
    storage = database.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == managed.id
        )
    )
    assert storage is not None and storage.state == "STAGED"
    plan = database.get(PortalProvisionPlan, item.provision_plan_id)
    assert plan is not None and plan.state == "STAGED" and plan.execution_enabled is True
    reservations = database.scalars(
        select(PortalResourceReservation).where(PortalResourceReservation.plan_id == plan.id)
    ).all()
    assert len(reservations) == 5
    assert all(row.state == "CONSUMED" and row.active_key is None for row in reservations)
    assert (
        database.scalar(
            select(func.count())
            .select_from(PortalAuditEvent)
            .where(PortalAuditEvent.event_type == "COMPUTE_RESOURCE_REQUEST_APPROVED")
        )
        == approval_audits_before
    )
    assert (
        database.scalar(
            select(func.count())
            .select_from(PortalOperation)
            .where(PortalOperation.operation_type == "compute.provision.stage")
        )
        == 1
    )


def test_stage_worker_timeout_persists_failed_hold_and_never_reapproves(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    user = account(database, login="uncertain-stage-user")
    admin = account(database, login="uncertain-stage-admin", role="platform_owner")
    request_id = submit(client, login(client, origin_headers, user.normalized_login)).json()[
        "request"
    ]["id"]
    admin_headers = login(client, origin_headers, admin.normalized_login)
    assert review(client, admin_headers, request_id, "APPROVE", "approved once").status_code == 200
    approval_count = database.scalar(
        select(func.count())
        .select_from(PortalAuditEvent)
        .where(PortalAuditEvent.event_type == "COMPUTE_RESOURCE_REQUEST_APPROVED")
    )
    plan_worker = worker_plan()

    def uncertain_worker(operation_type, **kwargs):  # type: ignore[no-untyped-def]
        if operation_type == "compute.provision.stage":
            persisted = database.scalar(
                select(PortalComputeResourceRequest).where(
                    PortalComputeResourceRequest.id == uuid.UUID(request_id)
                )
            )
            assert persisted is not None
            assert persisted.status == "PROVISIONING"
            assert persisted.active_slot == 1
            operation = database.scalar(
                select(PortalOperation).where(
                    PortalOperation.operation_type == "compute.provision.stage"
                )
            )
            assert operation is not None and operation.status == "RUNNING"
            raise WorkerClientError("WORKER_UNAVAILABLE", "ambiguous response")
        return plan_worker(operation_type, **kwargs)

    monkeypatch.setattr("h100_portal_api.routes.compute_requests.call_worker", uncertain_worker)
    assert (
        client.post(
            f"/api/v1/admin/compute-resource-requests/{request_id}/plan",
            headers=admin_headers,
            json={"idempotency_key": str(uuid.uuid4())},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/admin/compute-resource-requests/{request_id}/dry-run",
            headers=admin_headers,
            json={"idempotency_key": str(uuid.uuid4())},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/auth/reauthenticate",
            headers=admin_headers,
            json={"password": PASSWORD},
        ).status_code
        == 200
    )
    failed = client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/provision",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert failed.status_code == 503

    database.expire_all()
    item = database.get(PortalComputeResourceRequest, uuid.UUID(request_id))
    assert item is not None and item.status == "FAILED" and item.active_slot is None
    plan = database.get(PortalProvisionPlan, item.provision_plan_id)
    assert plan is not None and plan.state == "FAILED" and plan.execution_enabled is True
    reservations = database.scalars(
        select(PortalResourceReservation).where(PortalResourceReservation.plan_id == plan.id)
    ).all()
    assert len(reservations) == 5
    assert all(row.state == "FAILED_HOLD" and row.active_key for row in reservations)
    operation = database.scalar(
        select(PortalOperation).where(PortalOperation.operation_type == "compute.provision.stage")
    )
    assert operation is not None
    assert operation.status == "FAILED"
    assert operation.rollback_status == "REQUIRES_MANUAL_REVIEW"
    assert (
        database.scalar(
            select(func.count())
            .select_from(PortalAuditEvent)
            .where(PortalAuditEvent.event_type == "COMPUTE_RESOURCE_REQUEST_APPROVED")
        )
        == approval_count
    )


def test_first_stage_gate_failure_records_no_side_effect_and_preserves_approval(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    user = account(database, login="no-side-effect-stage-user")
    admin = account(database, login="no-side-effect-stage-admin", role="platform_owner")
    user_headers = login(client, origin_headers, user.normalized_login)
    request_id = submit(client, user_headers).json()["request"]["id"]
    admin_headers = login(client, origin_headers, admin.normalized_login)
    assert review(client, admin_headers, request_id, "APPROVE", "approved once").status_code == 200
    item = database.get(PortalComputeResourceRequest, uuid.UUID(request_id))
    credential = database.scalar(
        select(PortalPasswordCredential).where(PortalPasswordCredential.user_id == user.id)
    )
    assert item is not None and credential is not None
    approved_at = item.approved_at
    reviewed_by = item.reviewed_by
    password_hash = credential.password_hash
    approval_count = database.scalar(
        select(func.count())
        .select_from(PortalAuditEvent)
        .where(PortalAuditEvent.event_type == "COMPUTE_RESOURCE_REQUEST_APPROVED")
    )
    plan_worker = worker_plan()

    def gate_failure(operation_type, **kwargs):  # type: ignore[no-untyped-def]
        if operation_type != "compute.provision.stage":
            return plan_worker(operation_type, **kwargs)
        payload = kwargs["payload"]
        assert kwargs["idempotency_key"] == f"compute-stage:{payload['stage_operation_id']}"
        return {
            "status": "ERROR",
            "handler": "compute.provision.stage",
            "error": {
                "code": "COMPUTE_STAGE_FAILED",
                "message": "explicit Stage confirmation is invalid",
            },
            "side_effect_classification": "NO_SIDE_EFFECT",
            "rollback_status": "NOT_REQUIRED",
            "last_successful_step": "SCRIPT_ARGUMENT_COUNT",
            "first_failed_step": "EXPLICIT_STAGE_CONFIRMATION_GATE",
            "failed_handler": "h100-provision-stage",
            "retained_resources": [],
        }

    monkeypatch.setattr("h100_portal_api.routes.compute_requests.call_worker", gate_failure)
    assert (
        client.post(
            f"/api/v1/admin/compute-resource-requests/{request_id}/plan",
            headers=admin_headers,
            json={"idempotency_key": str(uuid.uuid4())},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/admin/compute-resource-requests/{request_id}/dry-run",
            headers=admin_headers,
            json={"idempotency_key": str(uuid.uuid4())},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/auth/reauthenticate",
            headers=admin_headers,
            json={"password": PASSWORD},
        ).status_code
        == 200
    )
    failed = client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/provision",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert failed.status_code == 409
    assert failed.json()["detail"]["code"] == "COMPUTE_STAGE_FAILED"

    database.expire_all()
    item = database.get(PortalComputeResourceRequest, uuid.UUID(request_id))
    assert item is not None and item.status == "FAILED" and item.active_slot is None
    assert item.approved_at == approved_at and item.reviewed_by == reviewed_by
    plan = database.get(PortalProvisionPlan, item.provision_plan_id)
    assert plan is not None and plan.state == "FAILED" and plan.attempt_number == 1
    reservations = database.scalars(
        select(PortalResourceReservation).where(PortalResourceReservation.plan_id == plan.id)
    ).all()
    assert len(reservations) == 5
    assert all(row.state == "RELEASED" and row.active_key is None for row in reservations)
    operation = database.scalar(
        select(PortalOperation).where(PortalOperation.operation_type == "compute.provision.stage")
    )
    assert operation is not None
    assert operation.status == "FAILED" and operation.rollback_status == "NOT_REQUIRED"
    assert operation.dry_run_result is not None
    assert operation.dry_run_result["side_effect_classification"] == "NO_SIDE_EFFECT"
    assert operation.dry_run_result["first_failed_step"] == "EXPLICIT_STAGE_CONFIRMATION_GATE"
    assert all(assert_zero_compute_side_effects(database, user.id).values())
    assert (
        database.scalar(
            select(PortalPasswordCredential.password_hash).where(
                PortalPasswordCredential.user_id == user.id
            )
        )
        == password_hash
    )
    assert (
        database.scalar(
            select(func.count())
            .select_from(PortalAuditEvent)
            .where(PortalAuditEvent.event_type == "COMPUTE_RESOURCE_REQUEST_APPROVED")
        )
        == approval_count
    )
    duplicate = submit(client, login(client, origin_headers, user.normalized_login))
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "APPROVED_COMPUTE_REQUEST_RETRY_PENDING"


def test_clean_rolled_back_stage_requires_authorization_and_creates_fresh_attempt(
    database, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _owner, item, failed_plan, failed_reservations, failed_operation = failed_stage_fixture(
        database
    )
    admin = account(database, login="rollback-retry-admin", role="platform_owner")
    monkeypatch.setattr(
        "h100_portal_api.routes.compute_requests._deny_self_administration",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "h100_portal_api.routes.compute_requests.require_session_csrf",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "h100_portal_api.routes.compute_requests.require_recent_reauthentication",
        lambda *_args: None,
    )
    monkeypatch.setattr("h100_portal_api.routes.compute_requests.call_worker", worker_plan())
    session = PortalSession(
        user_id=admin.id,
        session_hash="a" * 64,
        csrf_hash="b" * 64,
        source_ip="127.0.0.1",
        user_agent_digest="c" * 64,
        idle_expires_at=utcnow() + timedelta(hours=1),
        absolute_expires_at=utcnow() + timedelta(hours=1),
    )
    database.add(session)
    database.commit()
    context = AuthContext(admin, session, "fixture")
    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [], "client": None}
    )
    original_approved_at = item.approved_at
    original_reviewed_by = item.reviewed_by
    approval_count = database.scalar(
        select(func.count())
        .select_from(PortalAuditEvent)
        .where(PortalAuditEvent.event_type == "COMPUTE_RESOURCE_REQUEST_APPROVED")
    )

    with pytest.raises(HTTPException) as unauthorized_plan:
        create_provision_plan(
            str(item.id),
            ComputeProvisionActionRequest(idempotency_key=uuid.uuid4()),
            request,
            context,
            database,
        )
    assert getattr(unauthorized_plan.value, "detail", {}).get("code") == (
        "COMPUTE_REQUEST_NOT_READY_FOR_PLAN"
    )

    authorized = authorize_provision_retry(
        str(item.id),
        ComputeProvisionRetryAuthorizationRequest(
            idempotency_key=uuid.uuid4(),
            failure_classification="NO_SIDE_EFFECT",
            safe_root_cause="fixed Stage confirmation used ambiguous Bash positional parameters",
            authorization_reason="rollback and zero residue independently verified",
            remediation_git_commit="a" * 40,
        ),
        request,
        context,
        database,
    )
    assert authorized["status"] == "RETRY_AUTHORIZED"
    assert database.scalar(select(func.count()).select_from(PortalProvisionPlan)) == 1
    assert database.scalar(select(func.count()).select_from(PortalResourceReservation)) == 5
    database.refresh(failed_plan)
    assert failed_plan.state == "FAILED" and failed_plan.execution_enabled is True
    assert all(row.state == "RELEASED" and row.active_key is None for row in failed_reservations)

    result = create_provision_plan(
        str(item.id),
        ComputeProvisionActionRequest(idempotency_key=uuid.uuid4()),
        request,
        context,
        database,
    )

    assert result["status"] == "RESERVED"
    database.expire_all()
    preserved = database.get(PortalProvisionPlan, failed_plan.id)
    assert preserved is not None and preserved.state == "FAILED"
    assert preserved.execution_enabled is True
    assert preserved.attempt_number == 1 and preserved.attempt_reason == "INITIAL"
    aggregate = database.get(PortalComputeResourceRequest, item.id)
    assert aggregate is not None
    assert aggregate.status == "PROVISION_PLAN_READY" and aggregate.active_slot == 1
    assert aggregate.approved_at is not None and original_approved_at is not None
    assert ensure_utc(aggregate.approved_at) == ensure_utc(original_approved_at)
    assert aggregate.reviewed_by == original_reviewed_by
    assert aggregate.provision_plan_id != failed_plan.id
    retry_plan = database.get(PortalProvisionPlan, aggregate.provision_plan_id)
    assert retry_plan is not None
    assert retry_plan.state == "RESERVED" and retry_plan.execution_enabled is False
    assert retry_plan.attempt_number == 2 and retry_plan.attempt_reason == "STAGE_RETRY"
    assert retry_plan.previous_plan_id == failed_plan.id
    assert retry_plan.failed_stage_operation_id == failed_operation.id
    assert retry_plan.retry_authorization_operation_id == uuid.UUID(authorized["operation_id"])
    old_rows = database.scalars(
        select(PortalResourceReservation).where(PortalResourceReservation.plan_id == failed_plan.id)
    ).all()
    assert {row.id for row in old_rows} == {row.id for row in failed_reservations}
    assert all(row.state == "RELEASED" and row.active_key is None for row in old_rows)
    new_rows = database.scalars(
        select(PortalResourceReservation).where(PortalResourceReservation.plan_id == retry_plan.id)
    ).all()
    assert len(new_rows) == 5
    assert {row.id for row in new_rows}.isdisjoint({row.id for row in old_rows})
    assert all(row.state == "RESERVED" and row.active_key for row in new_rows)
    persisted_failed = database.get(PortalOperation, failed_operation.id)
    assert persisted_failed is not None
    assert persisted_failed.status == "FAILED"
    assert persisted_failed.rollback_status == "ROLLED_BACK"
    operations = database.scalars(
        select(PortalOperation)
        .where(PortalOperation.target_id == str(item.id))
        .order_by(PortalOperation.created_at)
    ).all()
    assert [row.operation_type for row in operations] == [
        "compute.provision.stage",
        "compute.provision.retry_authorize",
        "compute.provision.plan",
    ]
    assert operations[-1].validated_payload["plan_id"] == str(retry_plan.id)
    assert operations[-1].validated_payload["failed_stage_operation_id"] == str(failed_operation.id)
    assert (
        database.scalar(
            select(func.count())
            .select_from(PortalAuditEvent)
            .where(PortalAuditEvent.event_type == "COMPUTE_RESOURCE_REQUEST_APPROVED")
        )
        == approval_count
    )
    assert database.scalar(select(func.count()).select_from(PortalManagedUser)) == 0
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 0


def test_failed_stage_retry_rejects_unknown_rollback_failed_hold_and_binding_drift(
    database,
) -> None:  # type: ignore[no-untyped-def]
    _owner, item, _plan, reservations, operation = failed_stage_fixture(
        database, login="rollback-retry-denied"
    )

    operation.rollback_status = "UNKNOWN"
    database.commit()
    with pytest.raises(HTTPException) as unknown:
        _rolled_back_stage_retry(database, item)
    assert getattr(unknown.value, "detail", {}).get("code") == (
        "PROVISION_STAGE_RETRY_RECONCILIATION_REQUIRED"
    )

    operation.rollback_status = "ROLLED_BACK"
    reservations[0].state = "FAILED_HOLD"
    reservations[0].active_key = f"UID:{reservations[0].resource_value}"
    reservations[0].released_at = None
    database.commit()
    with pytest.raises(HTTPException) as held:
        _rolled_back_stage_retry(database, item)
    assert getattr(held.value, "detail", {}).get("code") == (
        "PROVISION_STAGE_RETRY_RECONCILIATION_REQUIRED"
    )

    reservations[0].state = "RELEASED"
    reservations[0].active_key = None
    reservations[0].released_at = utcnow()
    operation.validated_payload = {
        **operation.validated_payload,
        "reservation_ids": {
            **operation.validated_payload["reservation_ids"],
            "UID": str(uuid.uuid4()),
        },
    }
    database.commit()
    with pytest.raises(HTTPException) as drifted:
        _rolled_back_stage_retry(database, item)
    assert getattr(drifted.value, "detail", {}).get("code") == (
        "PROVISION_STAGE_RETRY_RECONCILIATION_REQUIRED"
    )


def test_dry_run_rejects_tampered_reservation_binding(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    user = account(database, login="binding-user")
    admin = account(database, login="binding-admin", role="platform_admin")
    user_headers = login(client, origin_headers, user.normalized_login)
    request_id = submit(client, user_headers).json()["request"]["id"]
    admin_headers = login(client, origin_headers, admin.normalized_login)
    assert review(client, admin_headers, request_id, "APPROVE").status_code == 200
    monkeypatch.setattr("h100_portal_api.routes.compute_requests.call_worker", worker_plan())
    planned = client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/plan",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert planned.status_code == 200
    plan_id = uuid.UUID(planned.json()["plan"]["id"])
    reservation = database.scalar(
        select(PortalResourceReservation).where(
            PortalResourceReservation.plan_id == plan_id,
            PortalResourceReservation.resource_type == "UID",
        )
    )
    assert reservation is not None
    reservation.resource_value = "20003"
    database.commit()

    denied = client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/dry-run",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "RESOURCE_RESERVATION_BINDING_FAILED"
    assert all(assert_zero_compute_side_effects(database, user.id).values())


def test_allocator_unique_reservation_blocks_concurrent_duplicate(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    first = account(database, login="reservation-one")
    second = account(database, login="reservation-two")
    admin = account(database, login="reservation-admin", role="platform_admin")
    first_headers = login(client, origin_headers, first.normalized_login)
    first_id = submit(client, first_headers).json()["request"]["id"]
    second_headers = login(client, origin_headers, second.normalized_login)
    second_id = submit(client, second_headers).json()["request"]["id"]
    admin_headers = login(client, origin_headers, admin.normalized_login)
    assert review(client, admin_headers, first_id, "APPROVE").status_code == 200
    assert review(client, admin_headers, second_id, "APPROVE").status_code == 200
    monkeypatch.setattr("h100_portal_api.routes.compute_requests.call_worker", worker_plan())
    first_plan = client.post(
        f"/api/v1/admin/compute-resource-requests/{first_id}/plan",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert first_plan.status_code == 200
    conflict = client.post(
        f"/api/v1/admin/compute-resource-requests/{second_id}/plan",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "RESOURCE_RESERVATION_CONFLICT"
    assert database.scalar(select(func.count()).select_from(PortalProvisionPlan)) == 1
    assert database.scalar(select(func.count()).select_from(PortalResourceReservation)) == 5


def test_failed_hold_remains_an_allocator_exclusion(database) -> None:  # type: ignore[no-untyped-def]
    user = account(database, login="failed-hold-user")
    item = PortalComputeResourceRequest(
        portal_account_id=user.id,
        requested_by=user.id,
        username=user.normalized_login,
        status="APPROVED",
        active_slot=1,
        requested_gpu_max=1,
        requested_storage_bytes=300 * 1024**3,
        requested_container_profile="STANDARD_8CPU_32GB",
        requested_lease_seconds=96 * 60 * 60,
        purpose="failed hold allocator fixture",
        submitted_at=utcnow(),
        reviewed_at=utcnow(),
        reviewed_by=user.id,
        approved_at=utcnow(),
    )
    database.add(item)
    database.flush()
    plan = PortalProvisionPlan(  # noqa: S604 - ORM shell field, not subprocess execution
        request_id=item.id,
        portal_account_id=user.id,
        state="FAILED",
        username=user.normalized_login,
        uid=20009,
        gid=20009,
        project_id=30009,
        container_name="gpu-dev-failed-hold-user",
        container_ssh_port=22029,
        storage_bytes=300 * 1024**3,
        container_profile="STANDARD_8CPU_32GB",
        container_cpus=8,
        container_memory_gb=32,
        container_pids_limit=4096,
        container_gpu=0,
        slurm_account="company",
        slurm_qos="general",
        gpu_max=1,
        lease_seconds=96 * 60 * 60,
        lease_state="NOT_STARTED",
        host_ssh_enabled=False,
        shell="/usr/sbin/nologin",
        password_state="LOCKED",
        execution_enabled=True,
        reservation_expires_at=utcnow() + timedelta(hours=1),
        allocator_result={},
        created_by=user.id,
    )
    database.add(plan)
    database.flush()
    item.provision_plan_id = plan.id
    reservation = PortalResourceReservation(
        plan_id=plan.id,
        request_id=item.id,
        portal_account_id=user.id,
        resource_type="UID",
        resource_value="20009",
        active_key="UID:20009",
        state="FAILED_HOLD",
        reserved_at=utcnow(),
        expires_at=utcnow() + timedelta(hours=1),
    )
    database.add(reservation)
    database.commit()

    assert "20009" in _active_reservations(database)["UID"]
