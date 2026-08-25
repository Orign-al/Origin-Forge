import base64
import hashlib
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from h100_portal_api import expiry_service
from h100_portal_api.auth import AuthContext
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
from h100_portal_api.main import app
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalComputeLease,
    PortalContainer,
    PortalJob,
    PortalLeaseRenewalRequest,
    PortalManagedUser,
    PortalOperation,
    PortalPasswordCredential,
    PortalResourceRecycleItem,
    PortalResourceRestoreRequest,
    PortalRole,
    PortalSession,
    PortalSshKey,
    PortalStorageResource,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.routes import self_service
from h100_portal_api.schemas import RestoreCreateRequest
from h100_portal_api.security import digest_secret, hash_password
from h100_portal_api.terminal_service import TerminalServiceError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

PASSWORD = "Portal ordinary user test passphrase 2026"


def _identity(
    db: Session,
    *,
    login: str = "origin-pilot",
    username: str = "origin-pilot",
    uid: int = 20001,
    port: int = 22023,
    remaining_hours: int = 96,
    development_profile: str = "STANDARD_8CPU_32GB",
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
    enrollment = PortalOperation(
        operation_type="ssh_key.enroll.fixture",
        target_type="ssh_public_key",
        target_id=username,
        requested_by=user.id,
        owner_managed_user_id=managed.id,
        approved_by=user.id,
        request_summary="fixture SSH key enrollment",
        validated_payload={},
        idempotency_key=f"fixture-key:{username}",
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.SUCCEEDED,
    )
    db.add(enrollment)
    db.flush()
    fingerprint = (
        "SHA256:nek6vyEb3GT+UJAcY5y/8PgY4achF2ouNy+8C2JqUVc"
        if uid == 20001
        else f"SHA256:fixture{uid}"
    )
    key = PortalSshKey(
        managed_user_id=managed.id,
        owner_managed_user_id=managed.id,
        key_type="ssh-ed25519",
        fingerprint_sha256=fingerprint,
        public_key="ssh-ed25519 fixture",
        comment=f"{username} fixture",
        scope="CONTAINER",
        state="INSTALLED",
        host_install_state="REMOVED_BY_POLICY",
        container_install_state="INSTALLED",
        generation_method="IMPORTED",
        created_by=user.id,
        enrollment_operation_id=enrollment.id,
        staging_file_name=f"fixture-{uid}.pub",
        content_sha256=f"{uid:064x}"[-64:],
        approved_by=user.id,
        approved_at=utcnow(),
        validated_at=utcnow(),
        installed_at=utcnow(),
        active=True,
    )
    db.add(key)
    container_gpu = 1 if development_profile == "GPU_1_8CPU_32GB" else 0
    allocation_job_id = 701 if container_gpu else None
    allocation_uuid = "GPU-11111111-2222-3333-4444-555555555555" if container_gpu else None
    container = PortalContainer(
        managed_user_id=managed.id,
        owner_managed_user_id=managed.id,
        name=f"gpu-dev-{username}",
        image_digest="sha256:" + f"{uid:064x}"[-64:],
        ssh_port=port,
        desired_state="RUNNING",
        observed_state="RUNNING",
        development_profile=development_profile,
        gpu_count=container_gpu,
        gpu_allocation_job_id=allocation_job_id,
        gpu_allocation_uuid=allocation_uuid,
        safe_spec={
            "gpu": "SLURM_ALLOCATED_1" if container_gpu else "NONE",
            "privileged": False,
        },
    )
    db.add(container)
    db.add(
        PortalStorageResource(
            owner_managed_user_id=managed.id,
            root_path=f"/storage/users/{uid}",
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
    return SimpleNamespace(user=user, managed=managed, container=container, lease=lease, key=key)


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


def _restore_http_request(item_id: uuid.UUID) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": f"/api/v1/self/recycle-bin/{item_id}/restore-requests",
            "raw_path": b"",
            "query_string": b"",
            "headers": [(b"user-agent", b"portal-restore-unit-test")],
            "client": ("127.0.0.1", 41000),
            "server": ("127.0.0.1", 18080),
        }
    )


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
        script_relative_path=f".portal/job-scripts/{job_id}.sh",
        workdir_relative_path="projects",
        stdout_relative_path=f"outputs/{job_id}.out",
        stderr_relative_path=f"outputs/{job_id}.err",
        requested_cpus=1,
        memory_mb=1024,
        gpu_count=0,
        time_limit_seconds=600,
        lease_deadline_at=identity.lease.expires_at,
    )
    db.add(job)
    db.flush()
    return job


def test_ordinary_user_can_record_own_page_access_without_platform_read(
    client, database: Session, origin_headers: dict[str, str]
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database)
    headers = _login(client, origin_headers, identity.user.normalized_login)

    response = client.post(
        "/api/v1/audit/page-access",
        headers=headers,
        json={"path": "/terminal"},
    )

    assert response.status_code == 204
    event = database.scalar(
        select(PortalAuditEvent).where(
            PortalAuditEvent.event_type == "page.access",
            PortalAuditEvent.actor == identity.user.normalized_login,
        )
    )
    assert event is not None
    assert event.object_id == "/terminal"


def test_page_access_still_requires_session_csrf(
    client, database: Session, origin_headers: dict[str, str]
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database)
    _login(client, origin_headers, identity.user.normalized_login)

    response = client.post(
        "/api/v1/audit/page-access",
        headers=origin_headers,
        json={"path": "/terminal"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CSRF_REJECTED"


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


def test_expiry_skips_superseded_lease_and_retries_cleanup_failure_fail_safe(
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
    clock = ensure_utc(retry_identity.lease.expires_at) + timedelta(seconds=1)
    monkeypatch.setattr(expiry_service, "utcnow", lambda: clock)
    calls = 0

    def fail_then_succeed(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "status": "ERROR",
                "request_id": str(uuid.uuid4()),
                "error": {"code": "CONTAINER_STOP_FAILED", "message": "fixture stop failed"},
                "first_failed_step": "CONTAINER_STOP",
                "cleanup_retryable": True,
                "container_key_state": "SUSPENDED_BY_RECYCLE",
                "container_key_fingerprints": [retry_identity.key.fingerprint_sha256],
                "new_access": "DENIED",
                "cancelled_pending_job_ids": [],
                "cancelled_running_job_ids": [],
                "data_preserved": True,
            }
        return {
            "status": "SUCCEEDED",
            "request_id": str(uuid.uuid4()),
            "container_state": "STOPPED",
            "container_gpu": "NONE",
            "container_key_state": "SUSPENDED_BY_RECYCLE",
            "container_key_fingerprints": [retry_identity.key.fingerprint_sha256],
            "cancelled_pending_job_ids": [],
            "cancelled_running_job_ids": [],
            "data_preserved": True,
        }

    monkeypatch.setattr(expiry_service, "call_worker", fail_then_succeed)
    assert expiry_service.process_due_leases() == (1, 0)
    database.expire_all()
    failed = database.get(PortalComputeLease, retry_identity.lease.id)
    failed_operation = database.scalar(
        select(PortalOperation).where(
            PortalOperation.idempotency_key == f"lease-expire:{retry_identity.lease.id}"
        )
    )
    failed_storage = database.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == retry_identity.managed.id
        )
    )
    assert failed.state == "EXPIRED"
    assert failed.expired_at is not None
    assert retry_identity.managed.compute_environment_state == "SUSPENDED"
    assert retry_identity.container.desired_state == "STOPPED"
    assert retry_identity.container.observed_state == "RUNNING"
    assert retry_identity.key.container_install_state == "SUSPENDED_BY_RECYCLE"
    assert failed_storage.state == "PRESERVED"
    assert failed_operation.result_summary == (
        "Lease entitlement expired; cleanup incomplete; automatic retry scheduled"
    )
    evidence = expiry_service.cleanup_evidence(failed_operation)
    assert evidence == {
        "version": expiry_service.RECYCLE_EVIDENCE_VERSION,
        "mode": "AUTOMATIC",
        "attempt_count": 1,
        "status": "FAILED",
        "error_code": "CONTAINER_STOP_FAILED",
        "first_failed_step": "CONTAINER_STOP",
        "lease_entitlement": "EXPIRED",
        "new_access": "DENIED",
        "container_stop": "FAILED",
        "container_key_state": "SUSPENDED_BY_RECYCLE",
        "cancelled_pending_job_ids": [],
        "cancelled_running_job_ids": [],
        "data_preserved": True,
        "quota_preserved": True,
        "linux_identity_preserved": True,
        "slurm_history_preserved": True,
        "auto_permanent_delete": False,
        "automatic_retry": True,
        "next_retry_at": (clock + timedelta(seconds=60)).isoformat(),
        "manual_review_required": False,
        "raw_argv_recorded": False,
    }
    # The every-minute scanner may see the row, but backoff prevents a second
    # Worker side effect until the structured retry deadline.
    assert expiry_service.process_due_leases() == (1, 0)
    assert calls == 1
    clock += timedelta(seconds=61)
    assert expiry_service.process_due_leases() == (1, 1)
    database.expire_all()
    retried = database.get(PortalComputeLease, retry_identity.lease.id)
    assert retried.state == "RECYCLE_BIN"
    assert retried.expired_at is not None
    assert retried.recycled_at is not None
    assert calls == 2


def test_legacy_failed_expiry_is_manual_review_and_not_replayed(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity(database, remaining_hours=0)
    operation = PortalOperation(
        operation_type="lease.expire",
        target_type="compute_lease",
        target_id=str(identity.lease.id),
        requested_by=identity.user.id,
        owner_managed_user_id=identity.managed.id,
        request_summary="legacy failed expiry fixture",
        validated_payload={},
        idempotency_key=f"lease-expire:{identity.lease.id}",
        risk_level=RiskLevel.HIGH,
        status=OperationStatus.FAILED,
        error_code="CONTAINER_STOP_FAILED",
    )
    database.add(operation)
    database.commit()
    factory = sessionmaker(bind=database.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(expiry_service, "SessionLocal", factory)
    monkeypatch.setattr(
        expiry_service,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("legacy incident requires platform_owner recovery"),
    )

    assert expiry_service.process_due_leases() == (1, 0)
    database.expire_all()
    assert database.get(PortalComputeLease, identity.lease.id).state == "ACTIVE"
    assert operation.status == OperationStatus.FAILED
    assert expiry_service.cleanup_evidence(operation) is None


def test_expiry_recycles_owned_resources_and_binds_the_approved_key(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity(database, remaining_hours=0)
    pending = _job(database, identity, "pending-at-expiry")
    pending.slurm_job_id = 401
    database.commit()
    factory = sessionmaker(bind=database.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(expiry_service, "SessionLocal", factory)

    def recycle(operation_type: str, **kwargs):  # type: ignore[no-untyped-def]
        assert operation_type == "resource.recycle"
        assert kwargs["payload"]["expected_key_fingerprints"] == [identity.key.fingerprint_sha256]
        return {
            "status": "SUCCEEDED",
            "request_id": str(uuid.uuid4()),
            "container_state": "STOPPED",
            "container_gpu": "NONE",
            "container_key_state": "SUSPENDED_BY_RECYCLE",
            "container_key_fingerprints": [identity.key.fingerprint_sha256],
            "cancelled_pending_job_ids": [401],
            "cancelled_running_job_ids": [],
            "data_preserved": True,
        }

    monkeypatch.setattr(expiry_service, "call_worker", recycle)
    assert expiry_service.process_due_leases() == (1, 1)
    database.expire_all()
    lease = database.get(PortalComputeLease, identity.lease.id)
    container = database.get(PortalContainer, identity.container.id)
    key = database.get(PortalSshKey, identity.key.id)
    storage = database.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == identity.managed.id
        )
    )
    recycle_item = database.scalar(
        select(PortalResourceRecycleItem).where(
            PortalResourceRecycleItem.lease_id == identity.lease.id
        )
    )
    assert lease.state == "RECYCLE_BIN"
    assert identity.managed.compute_environment_state == "RECYCLED"
    assert container.desired_state == container.observed_state == "STOPPED"
    assert key.container_install_state == "SUSPENDED_BY_RECYCLE"
    assert storage.state == "PRESERVED"
    assert pending.state == "CANCELLED"
    assert recycle_item.data_preserved is True
    assert recycle_item.auto_permanent_delete is False


def test_compute_lease_version_rejects_expiry_renewal_lost_update(database: Session) -> None:
    identity = _identity(database, remaining_hours=12)
    database.commit()
    factory = sessionmaker(bind=database.get_bind(), autoflush=False, expire_on_commit=False)
    with factory() as renewal_session, factory() as expiry_session:
        renewal_lease = renewal_session.get(PortalComputeLease, identity.lease.id)
        expiry_lease = expiry_session.get(PortalComputeLease, identity.lease.id)
        renewal_lease.state = "RENEWAL_PENDING"
        renewal_session.commit()
        expiry_lease.state = "EXPIRED"
        expiry_lease.expired_at = utcnow()
        with pytest.raises(StaleDataError):
            expiry_session.commit()


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
    assert client.get(f"/api/v1/self/jobs/{second_job.id}/logs").status_code == 404
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
    assert client.get(f"/api/v1/users/{second.user.id}/ssh-keys").status_code == 403
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


def test_independent_cookie_jars_isolate_sessions_logout_relogin_and_self_resources(
    database: Session, origin_headers: dict[str, str]
) -> None:
    first = _identity(database)
    second = _identity(
        database,
        login="fixture-user-b",
        username="fixture-user-b",
        uid=20002,
        port=22024,
    )
    first_job = _job(database, first, "first-session-job")
    second_job = _job(database, second, "second-session-job")
    database.commit()

    with (
        TestClient(app, base_url="http://127.0.0.1:18080") as context_a,
        TestClient(app, base_url="http://127.0.0.1:18080") as context_b,
    ):
        headers_a = _login(context_a, origin_headers, first.user.normalized_login)
        _login(context_b, origin_headers, second.user.normalized_login)
        cookie_a = context_a.cookies["h100_session"]
        cookie_b = context_b.cookies["h100_session"]
        assert cookie_a != cookie_b

        for _ in range(3):
            me_a = context_a.get("/api/v1/auth/me")
            me_b = context_b.get("/api/v1/auth/me")
            assert me_a.status_code == me_b.status_code == 200
            assert me_a.json()["user"]["id"] == str(first.user.id)
            assert me_b.json()["user"]["id"] == str(second.user.id)

            jobs_a = context_a.get("/api/v1/self/jobs").json()["jobs"]
            jobs_b = context_b.get("/api/v1/self/jobs").json()["jobs"]
            assert [row["id"] for row in jobs_a] == [str(first_job.id)]
            assert [row["id"] for row in jobs_b] == [str(second_job.id)]

            container_a = context_a.get("/api/v1/self/container").json()["container"]
            container_b = context_b.get("/api/v1/self/container").json()["container"]
            assert container_a["id"] == str(first.container.id)
            assert container_b["id"] == str(second.container.id)

            lease_a = context_a.get("/api/v1/self/lease").json()["lease"]
            lease_b = context_b.get("/api/v1/self/lease").json()["lease"]
            assert lease_a["id"] == str(first.lease.id)
            assert lease_b["id"] == str(second.lease.id)

            keys_a = context_a.get(f"/api/v1/users/{first.user.id}/ssh-keys")
            keys_b = context_b.get(f"/api/v1/users/{second.user.id}/ssh-keys")
            assert keys_a.status_code == keys_b.status_code == 200
            assert [row["id"] for row in keys_a.json()["keys"]] == [str(first.key.id)]
            assert [row["id"] for row in keys_b.json()["keys"]] == [str(second.key.id)]

        assert context_a.get(f"/api/v1/self/jobs/{second_job.id}/logs").status_code == 404
        assert context_b.get(f"/api/v1/self/jobs/{first_job.id}/logs").status_code == 404
        assert context_a.get(f"/api/v1/users/{second.user.id}/ssh-keys").status_code == 403
        assert context_b.get(f"/api/v1/users/{first.user.id}/ssh-keys").status_code == 403

        logged_out = context_a.post("/api/v1/auth/logout", headers=headers_a)
        assert logged_out.status_code == 204
        assert context_a.get("/api/v1/auth/me").status_code == 401
        assert context_b.get("/api/v1/auth/me").json()["user"]["id"] == str(second.user.id)
        assert context_b.cookies["h100_session"] == cookie_b

        headers_a = _login(context_a, origin_headers, first.user.normalized_login)
        assert context_a.cookies["h100_session"] != cookie_a
        assert context_a.get("/api/v1/auth/me").json()["user"]["id"] == str(first.user.id)
        assert context_b.get("/api/v1/auth/me").json()["user"]["id"] == str(second.user.id)
        assert context_a.post("/api/v1/auth/logout", headers=headers_a).status_code == 204

    with TestClient(app, base_url="http://127.0.0.1:18080") as shared_context:
        _login(shared_context, origin_headers, first.user.normalized_login)
        first_shared_cookie = shared_context.cookies["h100_session"]
        assert shared_context.get("/api/v1/auth/me").json()["user"]["id"] == str(first.user.id)

        _login(shared_context, origin_headers, second.user.normalized_login)
        assert shared_context.cookies["h100_session"] != first_shared_cookie
        assert shared_context.get("/api/v1/auth/me").json()["user"]["id"] == str(second.user.id)

    sessions = database.scalars(select(PortalSession)).all()
    first_shared = next(
        row for row in sessions if row.session_hash == digest_secret(first_shared_cookie)
    )
    assert first_shared.revoked_at is not None


def test_job_and_container_operations_enforce_active_lease_gpu_and_time(
    client,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(
        database,
        login="origin-pilot2",
        username="origin-pilot2",
        uid=20002,
        port=22024,
        remaining_hours=2,
    )
    headers = _login(client, origin_headers, "origin-pilot2")
    calls: list[dict[str, object]] = []

    def worker(operation_type: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"operation_type": operation_type, **kwargs})
        return {
            "status": "SUCCEEDED",
            "request_id": str(uuid.uuid4()),
            "slurm_job_id": 101,
            "slurm_user": "origin-pilot2",
        }

    monkeypatch.setattr("h100_portal_api.routes.self_service.call_worker", worker)
    base = {
        "name": "portal-job",
        "script": "set -eu\nwhoami\nid\n",
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
    spoofed_owner = client.post(
        "/api/v1/self/jobs",
        headers=headers,
        json={**base, "username": "fixture-user-b", "uid": 20003},
    )
    assert spoofed_owner.status_code == 422
    assert calls == []
    submitted = client.post("/api/v1/self/jobs", headers=headers, json=base)
    assert submitted.status_code == 200
    assert submitted.json()["job"]["gpu_count"] == 1
    assert calls[-1]["requested_by"] == "origin-pilot2"
    worker_payload = calls[-1]["payload"]
    assert worker_payload["managed_user_id"] == str(identity.managed.id)
    assert worker_payload["username"] == "origin-pilot2"
    assert worker_payload["uid"] == worker_payload["gid"] == 20002
    assert worker_payload["slurm_account"] == "company"
    assert worker_payload["slurm_qos"] == "general"
    assert worker_payload["max_gpu"] == 1
    assert worker_payload["workspace_path"] == "/storage/users/20002"
    assert worker_payload["workdir_relative_path"] == "projects"
    assert worker_payload["script_relative_path"].startswith(".portal/job-scripts/")
    assert worker_payload["stdout_relative_path"].startswith("outputs/")
    assert worker_payload["script_content"].startswith("#!/bin/bash\nset -eu")
    assert (
        worker_payload["script_sha256"]
        == hashlib.sha256(worker_payload["script_content"].encode()).hexdigest()
    )

    identity.lease.state = "EXPIRED"
    identity.lease.expired_at = utcnow()
    identity.managed.compute_environment_state = "RESTORE_PENDING"
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


def test_expired_timestamp_denies_new_access_even_when_database_state_is_active(
    client,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database)
    owned_job = _job(database, identity, "expired-log-denied")
    identity.lease.expires_at = utcnow() - timedelta(seconds=1)
    identity.lease.starts_at = identity.lease.expires_at - timedelta(
        seconds=MAX_LEASE_DURATION_SECONDS
    )
    identity.lease.state = "ACTIVE"
    database.commit()
    headers = _login(client, origin_headers, identity.user.normalized_login)
    monkeypatch.setattr(
        "h100_portal_api.routes.self_service.call_worker",
        lambda *_args, **_kwargs: pytest.fail("expired entitlement must fail before Worker"),
    )

    environment = client.get("/api/v1/self/environment")
    connection = client.get("/api/v1/self/container/connection")
    terminal = client.post(
        "/api/v1/self/container/terminal/sessions",
        headers=headers,
        json={"idempotency_key": str(uuid.uuid4()), "cols": 120, "rows": 32},
    )
    container_start = client.post(
        "/api/v1/self/container/start",
        headers=headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    job = client.post(
        "/api/v1/self/jobs",
        headers=headers,
        json={
            "name": "expired-denied",
            "script": "set -eu\nwhoami\n",
            "cpus": 1,
            "memory_mb": 1024,
            "gpu_count": 0,
            "time_limit_seconds": 600,
            "image_ref": None,
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    renewal = client.post(
        "/api/v1/self/lease/renewals",
        headers=headers,
        json={"duration_seconds": 3600, "idempotency_key": str(uuid.uuid4())},
    )

    assert environment.status_code == 200
    assert environment.json()["environment"]["lease"]["state"] == "EXPIRED"
    assert environment.json()["environment"]["lease"]["active"] is False
    assert connection.status_code == 200
    assert connection.json()["connection"]["available"] is False
    assert connection.json()["connection"]["host"] == "20.10.10.3"
    assert connection.json()["connection"]["command"] is None
    assert terminal.status_code == 409
    assert terminal.json()["detail"]["code"] == "LEASE_INACTIVE"
    assert container_start.status_code == 409
    assert container_start.json()["detail"]["code"] == "CONTAINER_OPERATION_DENIED_LEASE_INACTIVE"
    assert job.status_code == 409
    assert job.json()["detail"]["code"] == "LEASE_INACTIVE"
    jobs = client.get("/api/v1/self/jobs")
    logs = client.get(f"/api/v1/self/jobs/{owned_job.id}/logs")
    assert jobs.status_code == logs.status_code == 409
    assert logs.json()["detail"]["code"] == "LEASE_INACTIVE"
    assert renewal.status_code == 409
    assert renewal.json()["detail"]["code"] == "LEASE_EXPIRED_RESTORE_REQUIRED"


def test_container_connection_reports_only_a_live_slurm_gpu_allocation(
    client,
    database: Session,
    origin_headers: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database, development_profile="GPU_1_8CPU_32GB")
    _login(client, origin_headers, identity.user.normalized_login)

    running = client.get("/api/v1/self/container/connection")
    assert running.status_code == 200
    assert running.json()["connection"]["available"] is True
    assert running.json()["connection"]["profile"] == "GPU_1_8CPU_32GB"
    assert running.json()["connection"]["gpu"] == "SLURM_ALLOCATED_1"

    identity.container.desired_state = "STOPPED"
    identity.container.observed_state = "STOPPED"
    identity.container.gpu_allocation_job_id = None
    identity.container.gpu_allocation_uuid = None
    database.commit()

    stopped = client.get("/api/v1/self/container/connection")
    assert stopped.status_code == 200
    assert stopped.json()["connection"]["available"] is False
    assert stopped.json()["connection"]["profile"] == "GPU_1_8CPU_32GB"
    assert stopped.json()["connection"]["gpu"] == "NONE"


def test_failed_gpu_restart_persists_worker_confirmed_allocation_cleanup(
    client,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(
        database,
        login="gpu-restart-owner",
        username="gpu-restart-owner",
        uid=20011,
        port=22031,
        development_profile="GPU_1_8CPU_32GB",
    )
    headers = _login(client, origin_headers, identity.user.normalized_login)
    captured: dict[str, object] = {}

    def worker(operation_type: str, **kwargs):  # type: ignore[no-untyped-def]
        captured["operation_type"] = operation_type
        captured["payload"] = kwargs["payload"]
        return {
            "status": "ERROR",
            "gpu_allocation_state_known": True,
            "gpu_allocation_job_id": None,
            "gpu_allocation_uuid": None,
            "error": {"code": "CONTAINER_START_FAILED", "message": "fixture failure"},
        }

    monkeypatch.setattr("h100_portal_api.routes.self_service.call_worker", worker)
    response = client.post(
        "/api/v1/self/container/restart",
        headers=headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONTAINER_START_FAILED"
    assert captured["operation_type"] == "container.restart"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["gpu_allocation_job_id"] == 701
    assert payload["gpu_allocation_uuid"] == "GPU-11111111-2222-3333-4444-555555555555"
    assert payload["lease_id"] == str(identity.lease.id)
    assert payload["lease_starts_at"] == ensure_utc(identity.lease.starts_at).isoformat()
    assert payload["lease_expires_at"] == ensure_utc(identity.lease.expires_at).isoformat()

    database.refresh(identity.container)
    assert identity.container.observed_state == "STOPPED"
    assert identity.container.desired_state == "STOPPED"
    assert identity.container.gpu_allocation_job_id is None
    assert identity.container.gpu_allocation_uuid is None


def test_platform_owner_recovery_route_is_csrf_reauth_bound_and_idempotent(
    client,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database, remaining_hours=0)
    prior = PortalOperation(
        operation_type="lease.expire",
        target_type="compute_lease",
        target_id=str(identity.lease.id),
        requested_by=identity.user.id,
        owner_managed_user_id=identity.managed.id,
        request_summary="failed automatic expiry fixture",
        validated_payload={},
        idempotency_key=f"lease-expire:{identity.lease.id}",
        risk_level=RiskLevel.HIGH,
        status=OperationStatus.FAILED,
        error_code="CONTAINER_STOP_FAILED",
    )
    database.add(prior)
    admin = _admin(database)
    database.commit()
    ordinary_headers = _login(client, origin_headers, identity.user.normalized_login)
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "confirmation": str(identity.lease.id),
        "safe_reason": "approved fixture cleanup recovery",
    }
    ordinary = client.post(
        f"/api/v1/admin/compute-leases/{identity.lease.id}/recycle-retry",
        headers=ordinary_headers,
        json=body,
    )
    assert ordinary.status_code == 403

    client.cookies.clear()
    admin_headers = _login(client, origin_headers, admin.normalized_login)
    without_reauth = client.post(
        f"/api/v1/admin/compute-leases/{identity.lease.id}/recycle-retry",
        headers=admin_headers,
        json=body,
    )
    assert without_reauth.status_code == 428
    assert without_reauth.json()["detail"]["code"] == "REAUTH_REQUIRED"
    missing_csrf = client.post(
        f"/api/v1/admin/compute-leases/{identity.lease.id}/recycle-retry",
        headers=origin_headers,
        json=body,
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"]["code"] == "CSRF_REJECTED"
    reauthenticated = client.post(
        "/api/v1/auth/reauthenticate",
        headers=admin_headers,
        json={"password": PASSWORD},
    )
    assert reauthenticated.status_code == 200
    wrong_confirmation = client.post(
        f"/api/v1/admin/compute-leases/{identity.lease.id}/recycle-retry",
        headers=admin_headers,
        json={**body, "confirmation": str(uuid.uuid4())},
    )
    assert wrong_confirmation.status_code == 428
    calls = 0

    def recycle(operation_type: str, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        assert operation_type == "resource.recycle"
        return {
            "status": "SUCCEEDED",
            "request_id": str(uuid.uuid4()),
            "container_state": "STOPPED",
            "container_gpu": "NONE",
            "container_key_state": "SUSPENDED_BY_RECYCLE",
            "container_key_fingerprints": [identity.key.fingerprint_sha256],
            "cancelled_pending_job_ids": [],
            "cancelled_running_job_ids": [],
            "data_preserved": True,
        }

    monkeypatch.setattr(expiry_service, "call_worker", recycle)
    applied = client.post(
        f"/api/v1/admin/compute-leases/{identity.lease.id}/recycle-retry",
        headers=admin_headers,
        json=body,
    )
    assert applied.status_code == 200
    assert applied.json()["status"] == "SUCCEEDED"
    assert applied.json()["idempotent_replay"] is False
    replay = client.post(
        f"/api/v1/admin/compute-leases/{identity.lease.id}/recycle-retry",
        headers=admin_headers,
        json=body,
    )
    assert replay.status_code == 200
    assert replay.json()["operation_id"] == applied.json()["operation_id"]
    assert replay.json()["idempotent_replay"] is True
    assert calls == 1
    database.expire_all()
    lease = database.get(PortalComputeLease, identity.lease.id)
    storage = database.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == identity.managed.id
        )
    )
    item = database.scalar(
        select(PortalResourceRecycleItem).where(
            PortalResourceRecycleItem.lease_id == identity.lease.id
        )
    )
    recovery = database.get(PortalOperation, uuid.UUID(applied.json()["operation_id"]))
    assert lease.state == "RECYCLE_BIN"
    assert identity.managed.compute_environment_state == "RECYCLED"
    assert identity.container.desired_state == identity.container.observed_state == "STOPPED"
    assert identity.key.container_install_state == "SUSPENDED_BY_RECYCLE"
    assert storage.state == "PRESERVED"
    assert item.data_preserved is True
    assert item.auto_permanent_delete is False
    assert recovery.requested_by == admin.id
    assert recovery.approved_by == admin.id
    assert recovery.status == OperationStatus.SUCCEEDED


def test_admin_recovery_incident_read_model_is_safe_and_owner_only(
    client,
    database: Session,
    origin_headers: dict[str, str],
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database, remaining_hours=0)
    prior = PortalOperation(
        operation_type="lease.expire",
        target_type="compute_lease",
        target_id=str(identity.lease.id),
        requested_by=identity.user.id,
        owner_managed_user_id=identity.managed.id,
        request_summary="failed automatic expiry fixture",
        validated_payload={"secret_marker": "MUST_NOT_REACH_ADMIN_UI"},
        idempotency_key=f"lease-expire:{identity.lease.id}",
        risk_level=RiskLevel.HIGH,
        status=OperationStatus.FAILED,
        error_code="CONTAINER_STOP_FAILED",
        result_summary="internal traceback MUST_NOT_REACH_ADMIN_UI",
        started_at=utcnow(),
        finished_at=utcnow(),
    )
    database.add(prior)
    admin = _admin(database)
    database.commit()

    ordinary_headers = _login(client, origin_headers, identity.user.normalized_login)
    ordinary = client.get("/api/v1/admin/lease-recovery-incidents", headers=ordinary_headers)
    assert ordinary.status_code == 403

    client.cookies.clear()
    admin_headers = _login(client, origin_headers, admin.normalized_login)
    response = client.get("/api/v1/admin/lease-recovery-incidents", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["count"] == 1
    incident = response.json()["incidents"][0]
    assert incident == {
        **incident,
        "lease_id": str(identity.lease.id),
        "portal_user_id": str(identity.user.id),
        "username": "origin-pilot",
        "owner": "origin-pilot",
        "time_expired": True,
        "lease_state": "ACTIVE",
        "recycle_state": "FAILED",
        "compute_environment_state": "ACTIVE",
        "container_name": "gpu-dev-origin-pilot",
        "container_desired_state": "RUNNING",
        "container_observed_state": "RUNNING",
        "connection_authorization_state": "DENIED_EXPIRED_LEASE",
        "container_ssh_authorization_state": "KEY_INSTALLED",
        "operation_id": str(prior.id),
        "operation_type": "lease.expire",
        "operation_status": "FAILED",
        "error_code": "CONTAINER_STOP_FAILED",
        "attempt_count": None,
        "next_retry_at": None,
        "manual_review_required": True,
        "recovery_available": True,
        "data_delete_allowed": False,
    }
    serialized = json.dumps(response.json())
    assert "MUST_NOT_REACH_ADMIN_UI" not in serialized
    assert "traceback" not in serialized.lower()

    detail = client.get(f"/api/v1/users/{identity.user.id}", headers=admin_headers)
    assert detail.status_code == 200
    lifecycle = detail.json()["user"]["compute_lifecycle"]
    assert lifecycle["has_lease"] is True
    assert lifecycle["lease_id"] == str(identity.lease.id)
    assert lifecycle["time_expired"] is True
    assert lifecycle["lease_state"] == "ACTIVE"


def test_web_terminal_is_owner_scoped_lease_gated_and_does_not_audit_input(
    client,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    first = _identity(
        database,
        login="origin-pilot2",
        username="origin-pilot2",
        uid=20002,
        port=22024,
    )
    _identity(
        database,
        login="fixture-user-b",
        username="fixture-user-b",
        uid=20003,
        port=22025,
    )
    headers = _login(client, origin_headers, "origin-pilot2")

    class FakeWorker:
        def __init__(self) -> None:
            self.ready = {
                "request_id": str(uuid.uuid4()),
                "idle_timeout_seconds": 900,
                "max_duration_seconds": 3600,
            }
            self.state = "RUNNING"
            self.reason = None
            self.exit_code = None
            self.inputs: list[bytes] = []
            self.sizes: list[tuple[int, int]] = []

        def output(self, cursor: int) -> dict[str, object]:
            assert cursor == 0
            return {
                "data_b64": base64.b64encode(b"container-output").decode(),
                "cursor": len(b"container-output"),
                "state": self.state,
                "reason": self.reason,
                "exit_code": self.exit_code,
            }

        def send_input(self, data: bytes) -> None:
            self.inputs.append(data)

        def resize(self, cols: int, rows: int) -> None:
            self.sizes.append((cols, rows))

        def request_close(self) -> None:
            self.state = "CLOSING"

    class FakeRegistry:
        def __init__(self) -> None:
            self.record = None
            self.open_payload = None

        def open(self, **kwargs):  # type: ignore[no-untyped-def]
            self.open_payload = kwargs["payload"]
            worker = FakeWorker()
            self.record = SimpleNamespace(
                id=uuid.uuid4(),
                owner_managed_user_id=kwargs["owner_managed_user_id"],
                portal_session_id=kwargs["portal_session_id"],
                operation_id=kwargs["operation_id"],
                container_id=kwargs["container_id"],
                expires_at=utcnow() + timedelta(hours=1),
                worker=worker,
            )
            return self.record

        def owned(self, terminal_id, *, owner_managed_user_id, portal_session_id):  # type: ignore[no-untyped-def]
            if (
                self.record is None
                or terminal_id != self.record.id
                or owner_managed_user_id != self.record.owner_managed_user_id
                or portal_session_id != self.record.portal_session_id
            ):
                raise TerminalServiceError("TERMINAL_NOT_FOUND", "网页终端不存在")
            return self.record

    registry = FakeRegistry()
    monkeypatch.setattr("h100_portal_api.routes.self_service.terminal_registry", registry)
    opened = client.post(
        "/api/v1/self/container/terminal/sessions",
        headers=headers,
        json={"idempotency_key": str(uuid.uuid4()), "cols": 100, "rows": 30},
    )
    assert opened.status_code == 200
    terminal_id = opened.json()["terminal"]["id"]
    assert registry.open_payload == {
        "managed_user_id": str(first.managed.id),
        "username": "origin-pilot2",
        "uid": 20002,
        "gid": 20002,
        "name": "gpu-dev-origin-pilot2",
        "workspace_path": "/storage/users/20002",
        "development_profile": "STANDARD_8CPU_32GB",
        "container_gpu": 0,
        "gpu_allocation_job_id": None,
        "gpu_allocation_uuid": None,
        "slurm_account": "company",
        "slurm_qos": "general",
        "lease_id": str(first.lease.id),
        "lease_expires_at": ensure_utc(first.lease.expires_at).isoformat(),
        "expected_gpu": "NONE",
        "host_access": "DISABLED_BY_PLATFORM_POLICY",
        "expected_key_fingerprints": [first.key.fingerprint_sha256],
        "cols": 100,
        "rows": 30,
    }
    output = client.get(f"/api/v1/self/container/terminal/sessions/{terminal_id}/output?cursor=0")
    assert output.status_code == 200
    assert base64.b64decode(output.json()["data_b64"]) == b"container-output"
    sent = client.post(
        f"/api/v1/self/container/terminal/sessions/{terminal_id}/input",
        headers=headers,
        json={"data": "pwd\r"},
    )
    resized = client.post(
        f"/api/v1/self/container/terminal/sessions/{terminal_id}/resize",
        headers=headers,
        json={"cols": 132, "rows": 40},
    )
    assert sent.status_code == resized.status_code == 200
    assert registry.record.worker.inputs == [b"pwd\r"]
    assert registry.record.worker.sizes == [(132, 40)]
    serialized_audit = json.dumps(
        [event.safe_metadata for event in database.scalars(select(PortalAuditEvent)).all()]
    )
    assert "pwd" not in serialized_audit

    client.cookies.clear()
    _login(client, origin_headers, "fixture-user-b")
    denied = client.get(f"/api/v1/self/container/terminal/sessions/{terminal_id}/output?cursor=0")
    assert denied.status_code == 404
    assert denied.json()["detail"]["code"] == "TERMINAL_NOT_FOUND"

    client.cookies.clear()
    headers = _login(client, origin_headers, "origin-pilot2")
    # A different Portal login session cannot attach to an existing terminal,
    # even when it belongs to the same managed user.
    rebound = client.get(f"/api/v1/self/container/terminal/sessions/{terminal_id}/output?cursor=0")
    assert rebound.status_code == 404


def test_web_terminal_refuses_host_access_regression_and_expired_lease(
    client, database: Session, origin_headers: dict[str, str]
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database)
    headers = _login(client, origin_headers, "origin-pilot")
    identity.managed.host_access_state = "ENABLED"
    identity.managed.shell = "/bin/bash"
    database.commit()
    host_regression = client.post(
        "/api/v1/self/container/terminal/sessions",
        headers=headers,
        json={"idempotency_key": str(uuid.uuid4()), "cols": 120, "rows": 32},
    )
    assert host_regression.status_code == 409
    assert host_regression.json()["detail"]["code"] == "SELF_COMPUTE_CONTEXT_INVALID"

    identity.managed.host_access_state = "DISABLED_BY_PLATFORM_POLICY"
    identity.managed.shell = "/usr/sbin/nologin"
    identity.lease.state = "EXPIRED"
    identity.lease.expired_at = utcnow()
    database.commit()
    expired = client.post(
        "/api/v1/self/container/terminal/sessions",
        headers=headers,
        json={"idempotency_key": str(uuid.uuid4()), "cols": 120, "rows": 32},
    )
    assert expired.status_code == 409
    assert expired.json()["detail"]["code"] == "LEASE_INACTIVE"


def test_owner_restore_is_idempotent_and_reactivates_all_resources_without_admin(
    database: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database, remaining_hours=0)
    identity.lease.state = "RECYCLE_BIN"
    identity.lease.expired_at = utcnow()
    identity.lease.recycled_at = utcnow()
    identity.managed.compute_environment_state = "RESTORE_PENDING"
    identity.container.desired_state = "STOPPED"
    identity.container.observed_state = "STOPPED"
    identity.key.container_install_state = "SUSPENDED_BY_RECYCLE"
    storage = database.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == identity.managed.id
        )
    )
    storage.state = "PRESERVED"
    item = PortalResourceRecycleItem(
        owner_managed_user_id=identity.managed.id,
        lease_id=identity.lease.id,
        container_id=identity.container.id,
        state="RESTORE_PENDING",
        resource_name=identity.container.name,
        image_digest=identity.container.image_digest,
        retained_spec=identity.container.safe_spec,
        connection_state="DISABLED",
        data_preserved=True,
        auto_permanent_delete=False,
        expires_at=identity.lease.expires_at,
        recycled_at=utcnow(),
    )
    database.add(item)
    database.flush()
    legacy_restore = PortalResourceRestoreRequest(
        owner_managed_user_id=identity.managed.id,
        recycle_item_id=item.id,
        state="REQUESTED",
        requested_duration_seconds=MAX_LEASE_DURATION_SECONDS,
        idempotency_key=str(uuid.uuid4()),
    )
    database.add(legacy_restore)
    database.commit()

    calls: list[dict[str, object]] = []

    def restore_worker(operation_type: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"operation_type": operation_type, **kwargs})
        assert operation_type == "self.resource.restore"
        assert kwargs["requested_by"] == identity.user.normalized_login
        assert kwargs["approved_by"] == identity.user.normalized_login
        assert kwargs["payload"]["expected_key_fingerprints"] == [identity.key.fingerprint_sha256]
        assert kwargs["payload"]["recycle_lease_id"] == str(identity.lease.id)
        assert (
            kwargs["payload"]["recycle_lease_starts_at"]
            == ensure_utc(identity.lease.starts_at).isoformat()
        )
        assert (
            kwargs["payload"]["recycle_lease_expires_at"]
            == ensure_utc(identity.lease.expires_at).isoformat()
        )
        return {
            "status": "SUCCEEDED",
            "request_id": str(uuid.uuid4()),
            "container_state": "RUNNING",
            "container_gpu": "NONE",
            "container_key_state": "INSTALLED",
            "container_key_fingerprints": [identity.key.fingerprint_sha256],
        }

    monkeypatch.setattr("h100_portal_api.routes.self_service.call_worker", restore_worker)
    monkeypatch.setattr(self_service, "require_session_csrf", lambda *_args: None)
    idempotency_key = str(uuid.uuid4())
    body = RestoreCreateRequest(
        duration_seconds=MAX_LEASE_DURATION_SECONDS,
        idempotency_key=uuid.UUID(idempotency_key),
    )
    context = AuthContext(identity.user, None, "")
    request = _restore_http_request(item.id)
    first = self_service.create_restore_request(item.id, body, request, context, database)
    repeated = self_service.create_restore_request(item.id, body, request, context, database)
    with pytest.raises(HTTPException) as conflict:
        self_service.create_restore_request(
            item.id,
            RestoreCreateRequest(
                duration_seconds=3600,
                idempotency_key=uuid.UUID(idempotency_key),
            ),
            request,
            context,
            database,
        )
    assert first["status"] == repeated["status"] == "RESTORED"
    assert first["restore_request_id"] == repeated["restore_request_id"]
    assert first["lease_id"] == repeated["lease_id"]
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "IDEMPOTENCY_CONFLICT"
    requested_audits = database.scalars(
        select(PortalAuditEvent).where(PortalAuditEvent.event_type == "RESOURCE_RESTORE_REQUESTED")
    ).all()
    assert [event.object_id for event in requested_audits] == [first["restore_request_id"]]
    assert requested_audits[0].actor == "origin-pilot"
    assert requested_audits[0].safe_metadata["approval_required"] is False
    database.expire_all()
    restored = database.get(PortalResourceRestoreRequest, uuid.UUID(first["restore_request_id"]))
    successor = database.get(PortalComputeLease, restored.restored_lease_id)
    operation = database.scalar(
        select(PortalOperation).where(
            PortalOperation.operation_type == "self.resource.restore",
            PortalOperation.owner_managed_user_id == identity.managed.id,
        )
    )
    assert len(calls) == 1
    assert restored.state == "RESTORED"
    assert database.get(PortalResourceRestoreRequest, legacy_restore.id).state == "CANCELLED"
    assert successor.state == "ACTIVE"
    assert successor.duration_seconds == MAX_LEASE_DURATION_SECONDS
    assert identity.managed.compute_environment_state == "ACTIVE"
    assert identity.managed.host_access_state == "DISABLED_BY_PLATFORM_POLICY"
    assert identity.managed.shell == "/usr/sbin/nologin"
    assert identity.container.desired_state == identity.container.observed_state == "RUNNING"
    assert identity.key.container_install_state == "INSTALLED"
    assert storage.state == "ACTIVE"
    assert item.state == "RESTORED"
    assert operation.status == OperationStatus.SUCCEEDED
    assert operation.owner_managed_user_id == identity.managed.id
    assert operation.requested_by == identity.user.id
    assert operation.approved_by == identity.user.id


def test_owner_restore_failure_with_verified_rollback_remains_self_retryable(
    database: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    identity = _identity(database, remaining_hours=0)
    identity.lease.state = "RECYCLE_BIN"
    identity.lease.expired_at = utcnow()
    identity.lease.recycled_at = utcnow()
    identity.managed.compute_environment_state = "RECYCLED"
    identity.container.desired_state = "STOPPED"
    identity.container.observed_state = "STOPPED"
    identity.key.container_install_state = "SUSPENDED_BY_RECYCLE"
    storage = database.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == identity.managed.id
        )
    )
    storage.state = "PRESERVED"
    item = PortalResourceRecycleItem(
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
    database.add(item)
    database.commit()

    attempts = 0

    def restore_worker(operation_type: str, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        assert operation_type == "self.resource.restore"
        if attempts == 1:
            return {
                "status": "ERROR",
                "error": {
                    "code": "CONTAINER_START_FAILED",
                    "message": "restored container failed to start",
                    "rollback_status": "ROLLED_BACK",
                },
            }
        return {
            "status": "SUCCEEDED",
            "request_id": str(uuid.uuid4()),
            "container_state": "RUNNING",
            "container_gpu": "NONE",
            "container_key_state": "INSTALLED",
            "container_key_fingerprints": [identity.key.fingerprint_sha256],
        }

    monkeypatch.setattr("h100_portal_api.routes.self_service.call_worker", restore_worker)
    monkeypatch.setattr(self_service, "require_session_csrf", lambda *_args: None)
    context = AuthContext(identity.user, None, "")
    request = _restore_http_request(item.id)

    def restore_once() -> dict[str, object]:
        return self_service.create_restore_request(
            item.id,
            RestoreCreateRequest(duration_seconds=3600, idempotency_key=uuid.uuid4()),
            request,
            context,
            database,
        )

    with pytest.raises(HTTPException) as first:
        restore_once()
    assert first.value.status_code == 409
    assert first.value.detail == {
        "code": "CONTAINER_START_FAILED",
        "message": "restored container failed to start",
        "rollback_status": "ROLLED_BACK",
    }
    database.expire_all()
    assert database.get(PortalResourceRecycleItem, item.id).state == "RECYCLE_BIN"
    assert database.get(PortalManagedUser, identity.managed.id).compute_environment_state == (
        "RECYCLED"
    )

    # Production ea69317 could lose the Worker's nested rollback marker and
    # leave an otherwise safely stopped/suspended resource in FAILED. The
    # owner must be able to submit a fresh, owner-bound restore request; the
    # Worker still proves the runtime and lifecycle preconditions.
    recovered_item = database.get(PortalResourceRecycleItem, item.id)
    recovered_managed = database.get(PortalManagedUser, identity.managed.id)
    assert recovered_item is not None and recovered_managed is not None
    recovered_item.state = "FAILED"
    recovered_managed.compute_environment_state = "FAILED"
    database.commit()

    retry = restore_once()
    assert retry["status"] == "RESTORED"
    assert attempts == 2


def test_owner_self_restore_cannot_target_another_users_recycle_item(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _identity(database)
    other = _identity(
        database,
        login="fixture-user-b",
        username="fixture-user-b",
        uid=20002,
        port=22024,
    )
    item = PortalResourceRecycleItem(
        owner_managed_user_id=other.managed.id,
        lease_id=other.lease.id,
        container_id=other.container.id,
        state="RECYCLE_BIN",
        resource_name=other.container.name,
        image_digest=other.container.image_digest,
        retained_spec=other.container.safe_spec,
        connection_state="DISABLED",
        data_preserved=True,
        auto_permanent_delete=False,
        expires_at=other.lease.expires_at,
        recycled_at=utcnow(),
    )
    database.add(item)
    database.commit()
    monkeypatch.setattr(self_service, "require_session_csrf", lambda *_args: None)
    monkeypatch.setattr(
        self_service,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("cross-owner restore must not reach Worker"),
    )

    with pytest.raises(HTTPException) as denied:
        self_service.create_restore_request(
            item.id,
            RestoreCreateRequest(duration_seconds=3600, idempotency_key=uuid.uuid4()),
            _restore_http_request(item.id),
            AuthContext(owner.user, None, ""),
            database,
        )
    assert denied.value.status_code == 404
    assert denied.value.detail["code"] == "RECYCLE_ITEM_NOT_FOUND"


def test_restore_rollback_recycles_container_and_preserves_workspace(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(database)
    context = AuthContext(admin, None, "fixture")
    lease_id = str(uuid.uuid4())
    payload = {
        "restore_request_id": str(uuid.uuid4()),
        "managed_user_id": str(uuid.uuid4()),
        "username": "origin-pilot2",
        "uid": 20002,
        "gid": 20002,
        "container_name": "gpu-dev-origin-pilot2",
        "workspace_path": "/storage/users/20002",
        "development_profile": "GPU_1_8CPU_32GB",
        "container_gpu": 1,
        "gpu_allocation_job_id": None,
        "gpu_allocation_uuid": None,
        "slurm_account": "company",
        "slurm_qos": "general",
        "lease_id": lease_id,
        "lease_starts_at": utcnow().isoformat(),
        "lease_expires_at": (utcnow() + timedelta(hours=2)).isoformat(),
        "expected_gpu": "SLURM_ALLOCATED_1",
        "host_access": "DISABLED_BY_PLATFORM_POLICY",
        "expected_key_fingerprints": ["SHA256:fixture20002"],
    }
    captured: dict[str, object] = {}

    def worker(operation_type: str, **kwargs):  # type: ignore[no-untyped-def]
        captured["operation_type"] = operation_type
        captured["payload"] = kwargs["payload"]
        return {
            "status": "SUCCEEDED",
            "container_state": "STOPPED",
            "gpu_allocation_job_id": None,
            "gpu_allocation_uuid": None,
            "container_key_state": "SUSPENDED_BY_RECYCLE",
            "data_preserved": True,
        }

    monkeypatch.setattr(self_service, "call_worker", worker)
    recycled_lease_id = uuid.uuid4()
    recycled_lease_expires_at = utcnow() - timedelta(hours=1)
    recycled_lease_starts_at = recycled_lease_expires_at - timedelta(hours=96)
    rolled_back = self_service._rollback_restored_resource(
        payload=payload,
        worker_result={
            "gpu_allocation_job_id": 701,
            "gpu_allocation_uuid": "GPU-11111111-2222-3333-4444-555555555555",
        },
        context=context,
        restore_id=uuid.UUID(payload["restore_request_id"]),
        recycled_lease_id=recycled_lease_id,
        recycled_lease_starts_at=recycled_lease_starts_at,
        recycled_lease_expires_at=recycled_lease_expires_at,
    )
    assert rolled_back is True
    assert captured["operation_type"] == "resource.restore.rollback"
    recycle = captured["payload"]
    assert isinstance(recycle, dict)
    assert recycle["workspace_path"] == "/storage/users/20002"
    assert recycle["attempted_lease_id"] == lease_id
    assert recycle["recycle_lease_id"] == str(recycled_lease_id)
    assert recycle["restore_request_id"] == payload["restore_request_id"]
    assert recycle["gpu_allocation_job_id"] == 701
    assert recycle["gpu_allocation_uuid"] == "GPU-11111111-2222-3333-4444-555555555555"
    assert recycle["attempted_lease_starts_at"] == payload["lease_starts_at"]
    assert recycle["attempted_lease_expires_at"] == payload["lease_expires_at"]


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
