import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from h100_portal_api.delegated_auth import (
    DELEGATED_CREDENTIAL_PREFIX,
    DelegatedTestAuthError,
    issue_delegated_test_session,
)
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordState,
    RiskLevel,
)
from h100_portal_api.lease_service import MAX_LEASE_DURATION_SECONDS, create_lease
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalComputeLease,
    PortalContainer,
    PortalDelegatedTestSession,
    PortalJob,
    PortalManagedUser,
    PortalOperation,
    PortalPasswordCredential,
    PortalRole,
    PortalSession,
    PortalStorageResource,
    PortalUser,
    utcnow,
)
from h100_portal_api.security import digest_secret, hash_password
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def _portal_user(
    db: Session,
    *,
    login: str,
    role_name: str,
    unix_username: str | None = None,
) -> PortalUser:
    role = db.scalar(select(PortalRole).where(PortalRole.name == role_name))
    assert role is not None
    user = PortalUser(
        login_name=login,
        normalized_login=login,
        display_name=login,
        unix_username=unix_username,
        account_state=AccountState.ACTIVE,
        password_state=PasswordState.SET,
        resource_onboarding_state=(
            OnboardingState.ACTIVE if role_name == "user" else OnboardingState.NOT_ENROLLED
        ),
        activated_at=utcnow(),
        roles=[role],
    )
    db.add(user)
    db.flush()
    return user


def _managed_identity(db: Session, *, login: str, uid: int, port: int) -> SimpleNamespace:
    user = _portal_user(db, login=login, role_name="user", unix_username=login)
    password_hash = hash_password(f"Fixture password for {login} 2026")
    credential = PortalPasswordCredential(
        user_id=user.id,
        password_hash=password_hash,
        password_changed_at=utcnow(),
    )
    db.add(credential)
    managed = PortalManagedUser(  # noqa: S604 -- ORM shell field, not subprocess execution.
        portal_user_id=user.id,
        unix_username=login,
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
        container_name=f"gpu-dev-{login}",
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
        name=f"gpu-dev-{login}",
        image_digest="sha256:" + f"{uid:064x}"[-64:],
        ssh_port=port,
        desired_state="RUNNING",
        observed_state="RUNNING",
        safe_spec={"gpu": "NONE", "privileged": False},
    )
    lease = create_lease(
        managed_user_id=managed.id,
        starts_at=utcnow(),
        duration_seconds=MAX_LEASE_DURATION_SECONDS,
        gpu_count=1,
        approved_by=user.id,
    )
    storage = PortalStorageResource(
        owner_managed_user_id=managed.id,
        root_path=f"/storage/users/{uid}",
        quota_bytes=300 * 1024**3,
        state="ACTIVE",
    )
    db.add_all([container, lease, storage])
    db.commit()
    return SimpleNamespace(
        user=user,
        credential=credential,
        password_hash=password_hash,
        managed=managed,
        container=container,
        lease=lease,
    )


def _issue(
    db: Session,
    owner: PortalUser,
    identity: SimpleNamespace,
    scopes: list[str],
    *,
    now=None,  # type: ignore[no-untyped-def]
):
    issued = issue_delegated_test_session(
        db,
        actor=owner,
        effective_user=identity.user,
        scopes=scopes,
        ttl_seconds=600,
        source_ip="local-console",
        user_agent="delegated-test-fixture",
        now=now,
    )
    db.commit()
    return issued


def _headers(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


def _job(
    db: Session,
    identity: SimpleNamespace,
    *,
    slurm_job_id: int,
    state: str = "RUNNING",
    requested_by: PortalUser | None = None,
    delegated_session_id: uuid.UUID | None = None,
) -> PortalJob:
    actor = requested_by or identity.user
    delegated_context = (
        {
            "delegated_test_context": {
                "delegation_id": str(delegated_session_id),
                "actor_user": actor.normalized_login,
                "effective_user": identity.user.normalized_login,
                "scopes": ["self.jobs.cancel"],
            }
        }
        if delegated_session_id is not None
        else {}
    )
    operation = PortalOperation(
        operation_type="self.job.submit",
        target_type="slurm_job",
        target_id=str(uuid.uuid4()),
        requested_by=actor.id,
        owner_managed_user_id=identity.managed.id,
        approved_by=actor.id,
        request_summary="delegated fixture job",
        validated_payload=delegated_context,
        idempotency_key=f"delegated-fixture-{slurm_job_id}",
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
        slurm_job_id=slurm_job_id,
        name=f"delegated-job-{slurm_job_id}",
        state=state,
        script_relative_path=f".portal/job-scripts/{job_id}.sh",
        workdir_relative_path="projects",
        stdout_relative_path=f"outputs/{job_id}.out",
        stderr_relative_path=f"outputs/{job_id}.err",
        requested_cpus=2,
        memory_mb=4096,
        gpu_count=1,
        time_limit_seconds=300,
        lease_deadline_at=identity.lease.expires_at,
        submitted_at=utcnow(),
    )
    db.add(job)
    db.commit()
    return job


def test_platform_owner_issues_without_password_or_target_credential_mutation(database) -> None:  # type: ignore[no-untyped-def]
    owner = _portal_user(database, login="origin-al", role_name="platform_owner")
    first = _managed_identity(database, login="origin-pilot", uid=20001, port=22023)
    second = _managed_identity(database, login="origin-pilot2", uid=20002, port=22024)
    before_hash = first.credential.password_hash

    issued = _issue(
        database,
        owner,
        first,
        ["self.jobs.submit", "self.jobs.read", "self.container.read"],
    )
    assert issued.credential.startswith(DELEGATED_CREDENTIAL_PREFIX)
    assert issued.session.token_hash == digest_secret(issued.credential)
    assert issued.credential not in issued.session.token_hash
    assert first.credential.password_hash == before_hash
    assert database.scalar(select(func.count()).select_from(PortalSession)) == 0
    event = database.scalar(
        select(PortalAuditEvent).where(
            PortalAuditEvent.event_type == "DELEGATED_TEST_SESSION_CREATED"
        )
    )
    assert event is not None
    assert event.actor == "origin-al"
    assert event.safe_metadata["actor_user"] == "origin-al"
    assert event.safe_metadata["effective_user"] == "origin-pilot"

    with pytest.raises(DelegatedTestAuthError, match="platform_owner"):
        issue_delegated_test_session(
            database,
            actor=first.user,
            effective_user=second.user,
            scopes=["self.jobs.read"],
            ttl_seconds=600,
            source_ip="fixture",
            user_agent="fixture",
        )


def test_delegated_ttl_expiration_and_scope_enforcement(client, database) -> None:  # type: ignore[no-untyped-def]
    owner = _portal_user(database, login="origin-al", role_name="platform_owner")
    identity = _managed_identity(database, login="origin-pilot", uid=20001, port=22023)
    limited = _issue(database, owner, identity, ["self.container.read"])

    container = client.get("/api/v1/self/container", headers=_headers(limited.credential))
    assert container.status_code == 200
    assert container.json()["container"]["name"] == "gpu-dev-origin-pilot"
    assert client.get("/api/v1/self/jobs", headers=_headers(limited.credential)).status_code == 403
    assert client.get("/api/v1/self/lease", headers=_headers(limited.credential)).status_code == 403
    # Bare auth routes remain cookie-only; bearer delegation cannot reach password actions.
    assert client.get("/api/v1/auth/me", headers=_headers(limited.credential)).status_code == 401

    expired = _issue(
        database,
        owner,
        identity,
        ["self.jobs.read"],
        now=utcnow() - timedelta(seconds=601),
    )
    response = client.get("/api/v1/self/jobs", headers=_headers(expired.credential))
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "DELEGATED_TEST_SESSION_EXPIRED"


def test_two_delegations_are_isolated_and_cross_user_logs_are_denied(
    client, database, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    owner = _portal_user(database, login="origin-al", role_name="platform_owner")
    first = _managed_identity(database, login="origin-pilot", uid=20001, port=22023)
    second = _managed_identity(database, login="origin-pilot2", uid=20002, port=22024)
    scopes = ["self.jobs.read", "self.jobs.logs.read", "self.container.read"]
    delegated_a = _issue(database, owner, first, scopes)
    delegated_b = _issue(database, owner, second, scopes)
    job_a = _job(database, first, slurm_job_id=501, state="COMPLETED")
    job_b = _job(database, second, slurm_job_id=502, state="COMPLETED")

    def worker(operation_type: str, **_kwargs):  # type: ignore[no-untyped-def]
        if operation_type == "self.job.status.read":
            return {
                "status": "OK",
                "slurm_user": _kwargs["payload"]["username"],
                "job_state": "COMPLETED",
                "exit_code": "0:0",
            }
        return {"status": "OK", "stdout": "fixture", "stderr": ""}

    monkeypatch.setattr("h100_portal_api.routes.self_service.call_worker", worker)
    jobs_a = client.get("/api/v1/self/jobs", headers=_headers(delegated_a.credential))
    jobs_b = client.get("/api/v1/self/jobs", headers=_headers(delegated_b.credential))
    assert [row["id"] for row in jobs_a.json()["jobs"]] == [str(job_a.id)]
    assert [row["id"] for row in jobs_b.json()["jobs"]] == [str(job_b.id)]
    assert delegated_a.credential != delegated_b.credential

    cross_a = client.get(
        f"/api/v1/self/jobs/{job_b.id}/logs", headers=_headers(delegated_a.credential)
    )
    cross_b = client.get(
        f"/api/v1/self/jobs/{job_a.id}/logs", headers=_headers(delegated_b.credential)
    )
    assert cross_a.status_code == cross_b.status_code == 404
    database.expire_all()
    request_events = database.scalars(
        select(PortalAuditEvent).where(
            PortalAuditEvent.event_type == "DELEGATED_TEST_REQUEST",
            PortalAuditEvent.result == "DENIED",
        )
    ).all()
    assert {event.safe_metadata["effective_user"] for event in request_events} == {
        "origin-pilot",
        "origin-pilot2",
    }
    assert all(event.actor == "origin-al" for event in request_events)


def test_delegated_submit_gpu_two_rejects_before_worker_and_cancel_is_session_bound(
    client, database, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    owner = _portal_user(database, login="origin-al", role_name="platform_owner")
    identity = _managed_identity(database, login="origin-pilot", uid=20001, port=22023)
    delegated = _issue(
        database,
        owner,
        identity,
        ["self.jobs.submit", "self.jobs.read", "self.jobs.cancel"],
    )
    calls: list[dict[str, object]] = []

    def worker(operation_type: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"operation_type": operation_type, **kwargs})
        return {
            "status": "SUCCEEDED",
            "request_id": str(uuid.uuid4()),
            "slurm_job_id": 601,
            "slurm_user": "origin-pilot",
        }

    monkeypatch.setattr("h100_portal_api.routes.self_service.call_worker", worker)
    body = {
        "name": "delegated-production-test",
        "script": "set -eu\nwhoami\n",
        "cpus": 2,
        "memory_mb": 4096,
        "gpu_count": 1,
        "time_limit_seconds": 300,
        "image_ref": None,
        "idempotency_key": str(uuid.uuid4()),
    }
    submitted = client.post("/api/v1/self/jobs", headers=_headers(delegated.credential), json=body)
    assert submitted.status_code == 200
    assert calls[-1]["requested_by"] == "origin-pilot"
    portal_job_id = uuid.UUID(submitted.json()["job"]["id"])
    job = database.get(PortalJob, portal_job_id)
    assert job is not None
    operation = database.get(PortalOperation, job.operation_id)
    assert operation is not None
    assert operation.requested_by == owner.id
    assert operation.owner_managed_user_id == identity.managed.id
    assert operation.validated_payload["delegated_test_context"] == {
        "delegation_id": str(delegated.session.id),
        "actor_user": "origin-al",
        "effective_user": "origin-pilot",
        "scopes": ["self.jobs.cancel", "self.jobs.read", "self.jobs.submit"],
    }

    before_jobs = database.scalar(select(func.count()).select_from(PortalJob))
    before_operations = database.scalar(select(func.count()).select_from(PortalOperation))
    before_calls = len(calls)
    rejected = client.post(
        "/api/v1/self/jobs",
        headers=_headers(delegated.credential),
        json={**body, "gpu_count": 2, "idempotency_key": str(uuid.uuid4())},
    )
    assert rejected.status_code == 422
    assert len(calls) == before_calls
    assert database.scalar(select(func.count()).select_from(PortalJob)) == before_jobs
    assert database.scalar(select(func.count()).select_from(PortalOperation)) == before_operations

    other = _job(database, identity, slurm_job_id=602, state="RUNNING")
    denied_cancel = client.post(
        f"/api/v1/self/jobs/{other.id}/cancel",
        headers=_headers(delegated.credential),
        json={"idempotency_key": str(uuid.uuid4())},
    )
    assert denied_cancel.status_code == 403
    assert denied_cancel.json()["detail"]["code"] == "DELEGATED_JOB_CANCEL_DENIED"


def test_job_logs_refresh_converges_stale_running_projection(client, database, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    owner = _portal_user(database, login="origin-al", role_name="platform_owner")
    identity = _managed_identity(database, login="origin-pilot", uid=20001, port=22023)
    delegated = _issue(database, owner, identity, ["self.jobs.read", "self.jobs.logs.read"])
    stale = _job(database, identity, slurm_job_id=45, state="RUNNING")
    calls: list[str] = []

    def worker(operation_type: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(operation_type)
        if operation_type == "self.job.status.read":
            return {
                "status": "OK",
                "slurm_user": kwargs["payload"]["username"],
                "job_state": "COMPLETED",
                "exit_code": "0:0",
            }
        return {"status": "OK", "stdout": "done\n", "stderr": ""}

    monkeypatch.setattr("h100_portal_api.routes.self_service.call_worker", worker)
    response = client.get(
        f"/api/v1/self/jobs/{stale.id}/logs", headers=_headers(delegated.credential)
    )
    assert response.status_code == 200
    assert response.json()["stdout"] == "done\n"
    database.expire_all()
    converged = database.get(PortalJob, stale.id)
    assert converged is not None
    assert converged.state == "COMPLETED"
    assert converged.exit_code == "0:0"
    assert converged.finished_at is not None
    assert calls == ["self.job.status.read", "self.job.logs.read"]
    assert database.scalar(select(func.count()).select_from(PortalDelegatedTestSession)) == 1
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 1
