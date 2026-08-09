import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from h100_portal_api import expiry_service
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordState,
    RiskLevel,
)
from h100_portal_api.lease_service import (
    MAX_LEASE_DURATION_SECONDS,
    RenewalLeaseExpiredError,
    create_lease,
    decide_renewal,
    ensure_active_lease,
    request_renewal,
)
from h100_portal_api.models import (
    PortalComputeLease,
    PortalContainer,
    PortalJob,
    PortalLeaseRenewalRequest,
    PortalManagedUser,
    PortalOperation,
    PortalPasswordCredential,
    PortalResourceRecycleItem,
    PortalRole,
    PortalStorageResource,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.security import hash_password
from h100_portal_api.worker_client import WorkerClientError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

PASSWORD = "Portal ordinary user test passphrase 2026"


def _identity(
    db: Session,
    *,
    login: str = "origin-pilot",
    username: str = "origin-pilot",
    uid: int = 20001,
    port: int = 22023,
    remaining_hours: int = 96,
) -> SimpleNamespace:
    role = db.scalar(select(PortalRole).where(PortalRole.name == "user"))
    user = PortalUser(
        login_name=login,
        normalized_login=login,
        display_name=login,
        unix_username=username,
        account_state=AccountState.ACTIVE,
        password_state=PasswordState.SET,
        resource_onboarding_state=OnboardingState.ACTIVE,
        activated_at=utcnow(),
        roles=[role],
    )
    db.add(user)
    db.flush()
    db.add(
        PortalPasswordCredential(
            user_id=user.id,
            password_hash=hash_password(PASSWORD),
            password_changed_at=utcnow(),
        )
    )
    managed = PortalManagedUser(  # noqa: S604 -- ORM shell field, not subprocess execution.
        portal_user_id=user.id,
        unix_username=username,
        uid=uid,
        gid=uid,
        shell="/usr/sbin/nologin",
        host_access_state="DISABLED_BY_PLATFORM_POLICY",
        compute_environment_state="ACTIVE",
        gpu_isolation_state="VERIFIED",
        slurm_account="company",
        slurm_qos="general",
        project_id=uid + 10000,
        quota_bytes=300 * 1024**3,
        container_name=f"gpu-dev-{username}",
        container_port=port,
        onboarding_state=OnboardingState.ACTIVE,
        ssh_key_state="SSH_READY",
        ssh_key_count=1,
        compute_activated_at=utcnow(),
    )
    db.add(managed)
    db.flush()
    container = PortalContainer(
        managed_user_id=managed.id,
        owner_managed_user_id=managed.id,
        name=f"gpu-dev-{username}",
        image_digest="sha256:" + f"{uid:064x}"[-64:],
        ssh_port=port,
        desired_state="RUNNING",
        observed_state="RUNNING",
        safe_spec={"gpu": "NONE", "privileged": False},
    )
    db.add(container)
    db.add(
        PortalStorageResource(
            owner_managed_user_id=managed.id,
            root_path=f"/srv/gpu-platform/users/{username}",
            quota_bytes=300 * 1024**3,
            state="ACTIVE",
        )
    )
    start = utcnow() - timedelta(hours=96 - remaining_hours)
    lease = create_lease(
        managed_user_id=managed.id,
        starts_at=start,
        duration_seconds=MAX_LEASE_DURATION_SECONDS,
        gpu_count=1,
        approved_by=user.id,
    )
    db.add(lease)
    db.commit()
    return SimpleNamespace(user=user, managed=managed, container=container, lease=lease)


def _admin(db: Session) -> PortalUser:
    role = db.scalar(select(PortalRole).where(PortalRole.name == "platform_owner"))
    user = PortalUser(
        login_name="origin-al",
        normalized_login="origin-al",
        display_name="Origin-al",
        unix_username="origin-al",
        account_state=AccountState.ACTIVE,
        password_state=PasswordState.SET,
        resource_onboarding_state=OnboardingState.NOT_ENROLLED,
        activated_at=utcnow(),
        roles=[role],
    )
    db.add(user)
    db.flush()
    db.add(
        PortalPasswordCredential(
            user_id=user.id,
            password_hash=hash_password(PASSWORD),
            password_changed_at=utcnow(),
        )
    )
    db.commit()
    return user


def _login(client, origin_headers: dict[str, str], username: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        headers={**origin_headers, "X-CSRF-Token": csrf},
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200
    return {**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]}


def _job(db: Session, identity: SimpleNamespace, name: str) -> PortalJob:
    operation = PortalOperation(
        operation_type="self.job.submit",
        target_type="slurm_job",
        target_id=str(uuid.uuid4()),
        requested_by=identity.user.id,
        owner_managed_user_id=identity.managed.id,
        approved_by=identity.user.id,
        request_summary="test owned job",
        validated_payload={},
        idempotency_key=f"test-job:{uuid.uuid4()}",
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.SUCCEEDED,
    )
    db.add(operation)
    db.flush()
    job_id = uuid.uuid4()
    job = PortalJob(
        id=job_id,
        owner_managed_user_id=identity.managed.id,
        lease_id=identity.lease.id,
        operation_id=operation.id,
        name=name,
        state="PENDING",
        script_relative_path="workspace/job.sh",
        workdir_relative_path="workspace",
        stdout_relative_path=f"workspace/.portal/jobs/{job_id}.out",
        stderr_relative_path=f"workspace/.portal/jobs/{job_id}.err",
        requested_cpus=1,
        memory_mb=1024,
        gpu_count=0,
        time_limit_seconds=600,
        lease_deadline_at=identity.lease.expires_at,
    )
    db.add(job)
    db.flush()
    return job


def test_lease_duration_is_enforced_by_domain_and_database(database: Session) -> None:
    identity = _identity(database)
    with pytest.raises(ValueError, match="345600"):
        create_lease(
            managed_user_id=identity.managed.id,
            starts_at=utcnow(),
            duration_seconds=MAX_LEASE_DURATION_SECONDS + 1,
            gpu_count=1,
            approved_by=identity.user.id,
        )
    invalid = create_lease(
        managed_user_id=identity.managed.id,
        starts_at=identity.lease.expires_at,
        duration_seconds=MAX_LEASE_DURATION_SECONDS,
        gpu_count=1,
        approved_by=identity.user.id,
        state="APPROVED",
        previous_lease_id=identity.lease.id,
    )
    invalid.duration_seconds = MAX_LEASE_DURATION_SECONDS + 1
    database.add(invalid)
    with pytest.raises(IntegrityError):
        database.commit()
    database.rollback()


def test_renewal_window_approval_and_chain_preserve_remaining_time(database: Session) -> None:
    identity = _identity(database)
    with pytest.raises(HTTPException) as before_window:
        request_renewal(
            database,
            owner_id=identity.managed.id,
            duration_seconds=MAX_LEASE_DURATION_SECONDS,
            idempotency_key="before-window",
        )
    assert before_window.value.detail["code"] == "RENEWAL_WINDOW_NOT_OPEN"

    identity.lease.starts_at = utcnow() - timedelta(hours=80)
    identity.lease.expires_at = identity.lease.starts_at + timedelta(hours=96)
    database.commit()
    old_expiry = ensure_utc(identity.lease.expires_at)
    renewal = request_renewal(
        database,
        owner_id=identity.managed.id,
        duration_seconds=MAX_LEASE_DURATION_SECONDS,
        idempotency_key="inside-window",
    )
    renewal, successor = decide_renewal(
        database,
        request_id=renewal.id,
        decision="APPROVE",
        decided_by=identity.user.id,
        comment="approved test renewal",
    )
    database.commit()
    assert renewal.state == "APPROVED"
    assert successor is not None
    assert ensure_utc(successor.starts_at) == old_expiry
    assert ensure_utc(successor.expires_at) == old_expiry + timedelta(hours=96)


def test_expired_renewal_requires_restore_and_boundary_decision_persists(
    database: Session,
) -> None:
    identity = _identity(database, remaining_hours=12)
    renewal = request_renewal(
        database,
        owner_id=identity.managed.id,
        duration_seconds=3600,
        idempotency_key="boundary",
    )
    database.commit()
    boundary = ensure_utc(identity.lease.expires_at)
    with pytest.raises(RenewalLeaseExpiredError):
        decide_renewal(
            database,
            request_id=renewal.id,
            decision="APPROVE",
            decided_by=identity.user.id,
            comment="too late",
            now=boundary,
        )
    database.commit()
    database.expire_all()
    assert database.get(PortalComputeLease, identity.lease.id).state == "EXPIRED"
    assert database.get(PortalLeaseRenewalRequest, renewal.id).state == "CANCELLED"
    with pytest.raises(HTTPException) as after_expiry:
        request_renewal(
            database,
            owner_id=identity.managed.id,
            duration_seconds=3600,
            idempotency_key="after-expiry",
            now=boundary + timedelta(seconds=1),
        )
    assert after_expiry.value.detail["code"] == "LEASE_EXPIRED_RESTORE_REQUIRED"


def test_admin_boundary_response_commits_expired_and_cancelled(
    client, database: Session, origin_headers: dict[str, str]
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database, remaining_hours=12)
    renewal = request_renewal(
        database,
        owner_id=identity.managed.id,
        duration_seconds=3600,
        idempotency_key="api-boundary",
    )
    _admin(database)
    identity.lease.expires_at = utcnow() - timedelta(seconds=1)
    database.commit()
    headers = _login(client, origin_headers, "origin-al")
    response = client.post(
        f"/api/v1/admin/lease-renewals/{renewal.id}/decision",
        headers=headers,
        json={"decision": "APPROVE", "comment": "boundary test"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "LEASE_EXPIRED_RESTORE_REQUIRED"
    database.expire_all()
    assert database.get(PortalComputeLease, identity.lease.id).state == "EXPIRED"
    assert database.get(PortalLeaseRenewalRequest, renewal.id).state == "CANCELLED"


def test_expiry_skips_superseded_lease_and_retries_worker_failure(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity(database, remaining_hours=0)
    successor = create_lease(
        managed_user_id=identity.managed.id,
        starts_at=identity.lease.expires_at,
        duration_seconds=MAX_LEASE_DURATION_SECONDS,
        gpu_count=1,
        approved_by=identity.user.id,
        state="APPROVED",
        previous_lease_id=identity.lease.id,
    )
    database.add(successor)
    database.commit()
    activated = ensure_active_lease(
        database,
        identity.managed.id,
        now=ensure_utc(identity.lease.expires_at) + timedelta(seconds=1),
        lock=True,
    )
    assert activated.id == successor.id
    database.commit()
    factory = sessionmaker(bind=database.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(expiry_service, "SessionLocal", factory)
    monkeypatch.setattr(
        expiry_service,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("superseded lease must not be recycled"),
    )
    assert expiry_service.process_due_leases() == (0, 0)

    retry_identity = _identity(
        database,
        login="fixture-retry",
        username="fixture-retry",
        uid=20003,
        port=22025,
        remaining_hours=0,
    )
    database.commit()
    calls = 0

    def unavailable(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise WorkerClientError("WORKER_UNAVAILABLE", "test outage")

    monkeypatch.setattr(expiry_service, "call_worker", unavailable)
    assert expiry_service.process_due_leases() == (1, 0)
    assert expiry_service.process_due_leases() == (1, 0)
    database.expire_all()
    retried = database.get(PortalComputeLease, retry_identity.lease.id)
    assert retried.state == "ACTIVE"
    assert retried.expired_at is None
    assert calls == 2


def test_ordinary_user_self_routes_are_owner_scoped_and_admin_routes_are_denied(
    client, database: Session, origin_headers: dict[str, str]
) -> None:  # type: ignore[no-untyped-def]
    first = _identity(database)
    second = _identity(
        database,
        login="fixture-user-b",
        username="fixture-user-b",
        uid=20002,
        port=22024,
    )
    first_job = _job(database, first, "first-job")
    second_job = _job(database, second, "second-job")
    recycle = PortalResourceRecycleItem(
        owner_managed_user_id=second.managed.id,
        lease_id=second.lease.id,
        container_id=second.container.id,
        state="RECYCLE_BIN",
        resource_name=second.container.name,
        image_digest=second.container.image_digest,
        retained_spec=second.container.safe_spec,
        connection_state="DISABLED",
        data_preserved=True,
        auto_permanent_delete=False,
        expires_at=second.lease.expires_at,
        recycled_at=utcnow(),
    )
    database.add(recycle)
    database.commit()
    headers = _login(client, origin_headers, "origin-pilot")

    own = client.get("/api/v1/self/jobs")
    assert own.status_code == 200
    assert [row["id"] for row in own.json()["jobs"]] == [str(first_job.id)]
    assert client.get(f"/api/v1/self/jobs/{second_job.id}").status_code == 404
    assert (
        client.post(
            f"/api/v1/self/jobs/{second_job.id}/cancel",
            headers=headers,
            json={"idempotency_key": str(uuid.uuid4())},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/self/recycle-bin/{recycle.id}/restore-requests",
            headers=headers,
            json={"duration_seconds": 3600, "idempotency_key": str(uuid.uuid4())},
        ).status_code
        == 404
    )
    container = client.get("/api/v1/self/container").json()["container"]
    assert container["id"] == str(first.container.id)
    assert second.container.name not in str(container)
    assert client.get(f"/api/v1/containers/{second.container.name}").status_code == 403
    assert client.get("/api/v1/users").status_code == 403
    assert client.get("/api/v1/admin/lease-renewals").status_code == 403
    assert (
        client.post(
            f"/api/v1/admin/lease-renewals/{uuid.uuid4()}/decision",
            headers=headers,
            json={"decision": "APPROVE"},
        ).status_code
        == 403
    )


def test_job_and_container_operations_enforce_active_lease_gpu_and_time(
    client,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database, remaining_hours=2)
    headers = _login(client, origin_headers, "origin-pilot")
    calls: list[dict[str, object]] = []

    def worker(operation_type: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"operation_type": operation_type, **kwargs})
        return {
            "status": "SUCCEEDED",
            "request_id": str(uuid.uuid4()),
            "slurm_job_id": 101,
            "slurm_user": "origin-pilot",
        }

    monkeypatch.setattr("h100_portal_api.routes.self_service.call_worker", worker)
    base = {
        "name": "portal-job",
        "script_path": "workspace/job.sh",
        "workdir": "workspace",
        "cpus": 1,
        "memory_mb": 1024,
        "gpu_count": 1,
        "time_limit_seconds": 600,
        "image_ref": None,
        "idempotency_key": str(uuid.uuid4()),
    }
    invalid_gpu = client.post("/api/v1/self/jobs", headers=headers, json={**base, "gpu_count": 2})
    assert invalid_gpu.status_code == 422
    too_long = client.post(
        "/api/v1/self/jobs",
        headers=headers,
        json={**base, "time_limit_seconds": 3 * 3600},
    )
    assert too_long.status_code == 422
    submitted = client.post("/api/v1/self/jobs", headers=headers, json=base)
    assert submitted.status_code == 200
    assert submitted.json()["job"]["gpu_count"] == 1
    assert calls[-1]["requested_by"] == "origin-pilot"

    identity.lease.state = "EXPIRED"
    identity.lease.expired_at = utcnow()
    identity.managed.compute_environment_state = "RECYCLED"
    database.commit()
    denied = client.post(
        "/api/v1/self/container/start",
        headers=headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "CONTAINER_OPERATION_DENIED_LEASE_INACTIVE"
    expired_job = client.post(
        "/api/v1/self/jobs",
        headers=headers,
        json={**base, "idempotency_key": str(uuid.uuid4())},
    )
    assert expired_job.status_code == 409
    assert expired_job.json()["detail"]["code"] == "LEASE_INACTIVE"


def test_api_rejects_97_hour_renewal_and_restore_requests(
    client, database: Session, origin_headers: dict[str, str]
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database, remaining_hours=12)
    recycle = PortalResourceRecycleItem(
        owner_managed_user_id=identity.managed.id,
        lease_id=identity.lease.id,
        container_id=identity.container.id,
        state="RECYCLE_BIN",
        resource_name=identity.container.name,
        image_digest=identity.container.image_digest,
        retained_spec=identity.container.safe_spec,
        connection_state="DISABLED",
        data_preserved=True,
        auto_permanent_delete=False,
        expires_at=identity.lease.expires_at,
        recycled_at=utcnow(),
    )
    database.add(recycle)
    database.commit()
    headers = _login(client, origin_headers, "origin-pilot")
    renewal = client.post(
        "/api/v1/self/lease/renewals",
        headers=headers,
        json={
            "duration_seconds": MAX_LEASE_DURATION_SECONDS + 3600,
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    restore = client.post(
        f"/api/v1/self/recycle-bin/{recycle.id}/restore-requests",
        headers=headers,
        json={
            "duration_seconds": MAX_LEASE_DURATION_SECONDS + 3600,
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert renewal.status_code == 422
    assert restore.status_code == 422
