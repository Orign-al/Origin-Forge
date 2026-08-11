import uuid

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
    PortalStorageResource,
    PortalUser,
    utcnow,
)
from h100_portal_api.routes.compute_requests import assert_zero_compute_side_effects
from h100_portal_api.security import hash_password
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
            "infrastructure_side_effects": "NONE",
        }

    return call


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


def test_admin_approval_reservation_dry_run_and_disabled_execution_are_zero_side_effect(
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
    denied = client.post(
        f"/api/v1/admin/compute-resource-requests/{request_id}/provision",
        headers=admin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "PROVISION_EXECUTION_DISABLED_NEXT_GATE"

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
    assert "PROVISION_EXECUTION_DENIED_NEXT_GATE" in event_types


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
