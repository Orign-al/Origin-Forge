import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordState,
    RiskLevel,
)
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalComputeLease,
    PortalComputeResourceRequest,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalPasswordCredential,
    PortalProvisionPlan,
    PortalRole,
    PortalSshKey,
    PortalStorageResource,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.routes import activation as activation_routes
from h100_portal_api.security import hash_password
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

PASSWORD = "A long portal passphrase 2026"
LEASE_SECONDS = 96 * 60 * 60


@dataclass(frozen=True)
class StagedActivation:
    user: PortalUser
    managed: PortalManagedUser
    request: PortalComputeResourceRequest
    plan: PortalProvisionPlan
    stage: PortalOperation
    container: PortalContainer
    storage: PortalStorageResource
    key: PortalSshKey | None


def _account(
    database: Session,
    *,
    login: str,
    role_name: str = "user",
    onboarding_state: OnboardingState = OnboardingState.STAGED,
) -> PortalUser:
    role = database.scalar(select(PortalRole).where(PortalRole.name == role_name))
    assert role is not None
    user = PortalUser(
        login_name=login,
        normalized_login=login,
        display_name=login,
        unix_username=None,
        account_state=AccountState.ACTIVE,
        password_state=PasswordState.SET,
        resource_onboarding_state=onboarding_state,
        roles=[role],
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
    return user


def _staged_activation(
    database: Session,
    *,
    login: str = "activation-user",
    username: str = "origin-pilot2",
    uid: int = 20002,
    key_scope: str | None = "CONTAINER",
    development_profile: str = "STANDARD_8CPU_32GB",
) -> StagedActivation:
    user = _account(database, login=login)
    now = utcnow()
    managed = PortalManagedUser(  # noqa: S604 -- ORM shell field, not subprocess execution.
        portal_user_id=user.id,
        unix_username=username,
        uid=uid,
        gid=uid,
        shell="/usr/sbin/nologin",
        host_access_state="DISABLED_BY_PLATFORM_POLICY",
        compute_environment_state="STAGED",
        gpu_isolation_state="VERIFIED",
        slurm_account="company",
        slurm_qos="general",
        project_id=uid + 10000,
        quota_bytes=300 * 1024**3,
        container_name=f"gpu-dev-{username}",
        container_port=uid + 2021,
        onboarding_state=OnboardingState.STAGED,
        ssh_key_state="VALIDATED" if key_scope is not None else "REQUIRED_BEFORE_ACTIVATION",
        ssh_key_count=1 if key_scope is not None else 0,
        staged_at=now,
    )
    database.add(managed)
    database.flush()
    compute_request = PortalComputeResourceRequest(
        portal_account_id=user.id,
        requested_by=user.id,
        managed_user_id=managed.id,
        username=username,
        status="KEY_ENROLLMENT_PENDING",
        active_slot=None,
        requested_gpu_max=1,
        requested_storage_bytes=300 * 1024**3,
        requested_container_profile=development_profile,
        requested_lease_seconds=LEASE_SECONDS,
        purpose="owner-bound activation fixture",
        submitted_at=now,
        reviewed_at=now,
        reviewed_by=user.id,
        approved_at=now,
    )
    database.add(compute_request)
    database.flush()
    plan = PortalProvisionPlan(  # noqa: S604 -- ORM shell field, not subprocess execution.
        request_id=compute_request.id,
        portal_account_id=user.id,
        state="STAGED",
        username=username,
        uid=uid,
        gid=uid,
        project_id=uid + 10000,
        container_name=f"gpu-dev-{username}",
        container_ssh_port=uid + 2021,
        storage_bytes=300 * 1024**3,
        container_profile=development_profile,
        container_cpus=8,
        container_memory_gb=32,
        container_pids_limit=4096,
        container_gpu=1 if development_profile == "GPU_1_8CPU_32GB" else 0,
        slurm_account="company",
        slurm_qos="general",
        gpu_max=1,
        lease_seconds=LEASE_SECONDS,
        lease_state="NOT_STARTED",
        host_ssh_enabled=False,
        shell="/usr/sbin/nologin",
        password_state="LOCKED",
        execution_enabled=True,
        reservation_expires_at=now + timedelta(hours=1),
        allocator_result={"status": "RESERVED"},
        dry_run_result={"dry_run_status": "READY_FOR_PROVISION"},
        dry_run_at=now,
        created_by=user.id,
    )
    database.add(plan)
    database.flush()
    compute_request.provision_plan_id = plan.id
    dry_run_id = uuid.uuid4()
    stage = PortalOperation(
        operation_type="compute.provision.stage",
        target_type="compute_resource_request",
        target_id=str(compute_request.id),
        requested_by=user.id,
        owner_managed_user_id=managed.id,
        approved_by=user.id,
        request_summary="successful Stage fixture",
        validated_payload={
            "request_id": str(compute_request.id),
            "plan_id": str(plan.id),
            "dry_run_operation_id": str(dry_run_id),
        },
        idempotency_key=f"stage:{uuid.uuid4()}",
        risk_level=RiskLevel.CRITICAL,
        status=OperationStatus.SUCCEEDED,
        created_at=now,
        approved_at=now,
        started_at=now,
        finished_at=now,
        rollback_status="NOT_REQUIRED",
    )
    database.add(stage)
    container = PortalContainer(
        managed_user_id=managed.id,
        owner_managed_user_id=managed.id,
        name=f"gpu-dev-{username}",
        image_digest="sha256:" + "a" * 64,
        ssh_port=uid + 2021,
        desired_state="STOPPED",
        observed_state="STOPPED",
        development_profile=development_profile,
        gpu_count=1 if development_profile == "GPU_1_8CPU_32GB" else 0,
        safe_spec={
            "gpu": ("SLURM_ALLOCATED_1" if development_profile == "GPU_1_8CPU_32GB" else "NONE"),
            "privileged": False,
            "host_network": False,
            "host_pid": False,
            "host_ipc": False,
            "docker_socket": False,
            "munge": False,
            "cpus": 8,
            "memory_gb": 32,
            "pids_limit": 4096,
            "host_authorized_keys": "ABSENT",
            "container_authorized_keys": "ABSENT",
            "authorized_keys": "ABSENT",
            "lease_state": "NOT_STARTED",
        },
    )
    storage = PortalStorageResource(
        owner_managed_user_id=managed.id,
        root_path=f"/storage/users/{uid}",
        quota_bytes=300 * 1024**3,
        state="STAGED",
    )
    database.add_all([container, storage])
    key: PortalSshKey | None = None
    if key_scope is not None:
        enrollment = PortalOperation(
            operation_type="ssh_key.enroll",
            target_type="ssh_public_key",
            target_id=str(uuid.uuid4()),
            requested_by=user.id,
            owner_managed_user_id=managed.id,
            approved_by=user.id,
            request_summary="validated Container public key fixture",
            validated_payload={"public_material_only": True},
            idempotency_key=f"key:{uuid.uuid4()}",
            risk_level=RiskLevel.MEDIUM,
            status=OperationStatus.SUCCEEDED,
            created_at=now,
            approved_at=now,
            started_at=now,
            finished_at=now,
        )
        database.add(enrollment)
        database.flush()
        key_id = uuid.uuid4()
        key = PortalSshKey(
            id=key_id,
            managed_user_id=managed.id,
            owner_managed_user_id=managed.id,
            key_type="ssh-ed25519",
            fingerprint_sha256=f"SHA256:ActivationOwner{uid}",
            public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestActivationOwner",
            comment="activation fixture",
            scope=key_scope,
            state="VALIDATED",
            host_install_state="NOT_INSTALLED",
            container_install_state="NOT_INSTALLED",
            generation_method="IMPORTED",
            created_by=user.id,
            enrollment_operation_id=enrollment.id,
            staging_file_name=f"{key_id}.pub",
            content_sha256="b" * 64,
            approved_by=user.id,
            approved_at=now,
            validated_at=now,
            active=True,
            created_at=now,
        )
        database.add(key)
    database.commit()
    return StagedActivation(user, managed, compute_request, plan, stage, container, storage, key)


def _login(client: Any, origin_headers: dict[str, str], login: str) -> dict[str, str]:
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        headers={**origin_headers, "X-CSRF-Token": csrf},
        json={"username": login, "password": PASSWORD},
    )
    assert response.status_code == 200
    return {**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]}


def _worker_success(calls: list[tuple[str, dict[str, Any]]]):
    def worker(operation: str, **kwargs: Any) -> dict[str, Any]:
        payload = kwargs["payload"]
        calls.append((operation, kwargs))
        if operation == "compute.activate.self.rollback":
            return {
                "status": "SUCCEEDED",
                "handler": operation,
                "rollback_status": "ROLLED_BACK",
            }
        assert operation == "compute.activate.self"
        started = utcnow()
        expires = started + timedelta(seconds=LEASE_SECONDS)
        return {
            "status": "SUCCEEDED",
            "handler": operation,
            "activation_operation_id": payload["activation_operation_id"],
            "managed_user_id": payload["managed_user_id"],
            "username": payload["username"],
            "container_name": payload["container_name"],
            "container_state": "RUNNING",
            "container_gpu": payload["expected_gpu"],
            "gpu_allocation_job_id": (
                701 if payload["development_profile"] == "GPU_1_8CPU_32GB" else None
            ),
            "gpu_allocation_uuid": (
                "GPU-11111111-2222-3333-4444-555555555555"
                if payload["development_profile"] == "GPU_1_8CPU_32GB"
                else None
            ),
            "container_cpus": 8,
            "container_memory_gb": 32,
            "container_pids_limit": 4096,
            "container_privileged": False,
            "docker_socket": "ABSENT",
            "munge": "ABSENT",
            "host_namespaces": "DISABLED",
            "host_authorized_keys": "ABSENT",
            "host_shell": "/usr/sbin/nologin",
            "host_password": "LOCKED",
            "container_key_fingerprints": payload["ssh_key_fingerprints"],
            "lease_starts_at": started.isoformat(),
            "lease_expires_at": expires.isoformat(),
            "deployment_version": payload["deployment_version"],
            "worker_request_id": "activation-worker-test",
            "idempotent_replay": False,
        }

    return worker


def _activate(client: Any, headers: dict[str, str], key: uuid.UUID | None = None):
    return client.post(
        "/api/v1/self/compute/activate",
        headers=headers,
        json={"idempotency_key": str(key or uuid.uuid4())},
    )


def test_owner_can_activate_staged_compute_once_without_users_write(
    client: Any,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _staged_activation(database)
    headers = _login(client, origin_headers, target.user.normalized_login)
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(activation_routes, "call_worker", _worker_success(calls))

    response = _activate(client, headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ACTIVE"
    assert body["environment_state"] == "ACTIVE"
    assert body["container_state"] == "RUNNING"
    assert body["ssh_state"] == "READY"
    starts_at = ensure_utc(datetime.fromisoformat(body["lease"]["starts_at"]))
    expires_at = ensure_utc(datetime.fromisoformat(body["lease"]["expires_at"]))
    assert expires_at - starts_at == timedelta(seconds=LEASE_SECONDS)
    assert body["lease"]["duration_seconds"] == LEASE_SECONDS
    assert [item[0] for item in calls] == ["compute.activate.self"]
    worker_kwargs = calls[0][1]
    assert worker_kwargs["requested_by"] == target.user.normalized_login
    assert worker_kwargs["approved_by"] == target.user.normalized_login
    assert worker_kwargs["dry_run"] is False
    payload = worker_kwargs["payload"]
    assert payload["managed_user_id"] == str(target.managed.id)
    assert payload["portal_account_id"] == str(target.user.id)
    assert payload["request_id"] == str(target.request.id)
    assert payload["plan_id"] == str(target.plan.id)
    assert payload["container_name"] == target.container.name
    assert payload["ssh_key_record_ids"] == ([str(target.key.id)] if target.key is not None else [])
    assert set(payload).isdisjoint({"private_key", "command", "argv"})

    database.expire_all()
    managed = database.get(PortalManagedUser, target.managed.id)
    compute_request = database.get(PortalComputeResourceRequest, target.request.id)
    plan = database.get(PortalProvisionPlan, target.plan.id)
    container = database.get(PortalContainer, target.container.id)
    storage = database.get(PortalStorageResource, target.storage.id)
    key = database.get(PortalSshKey, target.key.id) if target.key else None
    assert managed is not None and managed.onboarding_state == OnboardingState.ACTIVE
    assert managed.compute_environment_state == "ACTIVE"
    assert managed.shell == "/usr/sbin/nologin"
    assert managed.host_access_state == "DISABLED_BY_PLATFORM_POLICY"
    assert compute_request is not None and compute_request.status == "ACTIVE"
    assert plan is not None and plan.state == "STAGED"
    assert plan.lease_state == "NOT_STARTED"
    assert container is not None and container.observed_state == "RUNNING"
    assert container.safe_spec["gpu"] == "NONE"
    assert container.safe_spec["host_authorized_keys"] == "ABSENT"
    assert storage is not None and storage.state == "ACTIVE"
    assert key is not None and key.container_install_state == "INSTALLED"
    assert key.host_install_state == "NOT_INSTALLED"
    operation = database.scalar(
        select(PortalOperation).where(PortalOperation.operation_type == "compute.activate.self")
    )
    assert operation is not None and operation.status == OperationStatus.SUCCEEDED
    assert operation.owner_managed_user_id == target.managed.id
    assert operation.worker_execution_id == "activation-worker-test"
    audit = database.scalar(
        select(PortalAuditEvent).where(PortalAuditEvent.event_type == "COMPUTE_SELF_ACTIVATED")
    )
    assert audit is not None and audit.actor == target.user.normalized_login


def test_owner_activation_persists_gpu_development_allocation_coordinates(
    client: Any,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _staged_activation(
        database,
        login="gpu-activation-user",
        username="gpu-activation-user",
        uid=20012,
        development_profile="GPU_1_8CPU_32GB",
    )
    headers = _login(client, origin_headers, target.user.normalized_login)
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(activation_routes, "call_worker", _worker_success(calls))

    response = _activate(client, headers)
    assert response.status_code == 200
    payload = calls[0][1]["payload"]
    assert payload["development_profile"] == "GPU_1_8CPU_32GB"
    assert payload["container_gpu"] == 1
    assert payload["expected_gpu"] == "SLURM_ALLOCATED_1"
    assert payload["workspace_path"] == "/storage/users/20012"

    database.expire_all()
    container = database.get(PortalContainer, target.container.id)
    assert container is not None
    assert container.development_profile == "GPU_1_8CPU_32GB"
    assert container.gpu_count == 1
    assert container.observed_state == "RUNNING"
    assert container.gpu_allocation_job_id == 701
    assert container.gpu_allocation_uuid == "GPU-11111111-2222-3333-4444-555555555555"


def test_double_activate_is_idempotent_and_never_extends_lease(
    client: Any,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _staged_activation(database)
    headers = _login(client, origin_headers, target.user.normalized_login)
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(activation_routes, "call_worker", _worker_success(calls))
    key = uuid.uuid4()

    first = _activate(client, headers, key)
    second = _activate(client, headers, uuid.uuid4())

    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "ALREADY_ACTIVE"
    assert second.json()["idempotent_replay"] is True
    assert second.json()["lease"]["starts_at"] == first.json()["lease"]["starts_at"]
    assert second.json()["lease"]["expires_at"] == first.json()["lease"]["expires_at"]
    assert [item[0] for item in calls] == ["compute.activate.self"]
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 1
    assert (
        database.scalar(
            select(func.count())
            .select_from(PortalOperation)
            .where(PortalOperation.operation_type == "compute.activate.self")
        )
        == 1
    )


def test_concurrent_replay_returns_existing_operation_without_second_worker_write(
    client: Any,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _staged_activation(database)
    headers = _login(client, origin_headers, target.user.normalized_login)
    now = utcnow()
    operation = PortalOperation(
        operation_type="compute.activate.self",
        target_type="compute_identity",
        target_id=str(target.managed.id),
        requested_by=target.user.id,
        owner_managed_user_id=target.managed.id,
        approved_by=target.user.id,
        request_summary="in-flight owner activation fixture",
        validated_payload={"activation_operation_id": str(uuid.uuid4())},
        idempotency_key=f"compute-activate-self:{target.managed.id}:{uuid.uuid4()}",
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.RUNNING,
        created_at=now,
        approved_at=now,
        started_at=now,
    )
    database.add(operation)
    database.commit()
    monkeypatch.setattr(
        activation_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("a fresh in-flight replay must not reach Worker"),
    )

    response = _activate(client, headers)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ACTIVATING",
        "operation_id": str(operation.id),
        "idempotent_replay": True,
        "environment_state": "STAGED",
        "container_state": "ACTIVATING",
        "ssh_state": "INSTALLING",
        "lease": None,
    }
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 0
    assert (
        database.scalar(
            select(func.count())
            .select_from(PortalOperation)
            .where(PortalOperation.operation_type == "compute.activate.self")
        )
        == 1
    )


@pytest.mark.parametrize("key_scope", [None, "HOST"])
def test_activation_requires_active_container_scope_key_before_operation(
    client: Any,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    key_scope: str | None,
) -> None:
    target = _staged_activation(database, key_scope=key_scope)
    headers = _login(client, origin_headers, target.user.normalized_login)
    monkeypatch.setattr(
        activation_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("invalid key scope must not reach Root Worker"),
    )

    response = _activate(client, headers)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SSH_KEY_REQUIRED"
    assert (
        database.scalar(
            select(func.count())
            .select_from(PortalOperation)
            .where(PortalOperation.operation_type == "compute.activate.self")
        )
        == 0
    )
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 0


def test_browser_cannot_select_another_users_compute_or_key(
    client: Any,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _staged_activation(database)
    attacker = _account(
        database,
        login="activation-attacker",
        onboarding_state=OnboardingState.DRAFT,
    )
    database.commit()
    headers = _login(client, origin_headers, attacker.normalized_login)
    monkeypatch.setattr(
        activation_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("cross-user request must not reach Root Worker"),
    )

    selected = client.post(
        "/api/v1/self/compute/activate",
        headers=headers,
        json={
            "idempotency_key": str(uuid.uuid4()),
            "managed_user_id": str(owner.managed.id),
            "container_name": owner.container.name,
            "ssh_key_record_ids": [str(owner.key.id)] if owner.key else [],
        },
    )
    implicit = _activate(client, headers)

    assert selected.status_code == 422
    assert implicit.status_code == 404
    assert implicit.json()["detail"]["code"] == "MANAGED_IDENTITY_NOT_FOUND"
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 0


def test_activation_failure_rolls_back_and_does_not_start_lease(
    client: Any,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _staged_activation(database)
    headers = _login(client, origin_headers, target.user.normalized_login)
    calls: list[str] = []

    def worker(operation: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(operation)
        if operation == "compute.activate.self":
            return {
                "status": "ERROR",
                "error": {"code": "CONTAINER_START_FAILED", "message": "safe failure"},
                "rollback_status": "ROLLED_BACK",
            }
        return {
            "status": "SUCCEEDED",
            "handler": "compute.activate.self.rollback",
            "rollback_status": "ROLLED_BACK",
        }

    monkeypatch.setattr(activation_routes, "call_worker", worker)

    response = _activate(client, headers)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONTAINER_START_FAILED"
    assert calls == ["compute.activate.self"]
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 0
    database.expire_all()
    managed = database.get(PortalManagedUser, target.managed.id)
    plan = database.get(PortalProvisionPlan, target.plan.id)
    container = database.get(PortalContainer, target.container.id)
    assert managed is not None and managed.onboarding_state == OnboardingState.STAGED
    assert managed.compute_environment_state == "STAGED"
    assert plan is not None and plan.lease_state == "NOT_STARTED"
    assert container is not None and container.observed_state == "STOPPED"
    operation = database.scalar(
        select(PortalOperation).where(PortalOperation.operation_type == "compute.activate.self")
    )
    assert operation is not None and operation.status == OperationStatus.FAILED
    assert operation.rollback_status == "ROLLED_BACK"


def test_preflight_failure_with_no_worker_mutation_does_not_run_redundant_rollback(
    client: Any,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _staged_activation(database)
    headers = _login(client, origin_headers, target.user.normalized_login)
    calls: list[str] = []

    def worker(operation: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(operation)
        assert operation == "compute.activate.self"
        return {
            "status": "ERROR",
            "error": {"code": "SCRIPT_INTEGRITY_FAILED", "message": "safe failure"},
            "rollback_status": "NOT_REQUIRED",
        }

    monkeypatch.setattr(activation_routes, "call_worker", worker)

    response = _activate(client, headers)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SCRIPT_INTEGRITY_FAILED"
    assert calls == ["compute.activate.self"]
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 0
    database.expire_all()
    plan = database.get(PortalProvisionPlan, target.plan.id)
    container = database.get(PortalContainer, target.container.id)
    operation = database.scalar(
        select(PortalOperation).where(PortalOperation.operation_type == "compute.activate.self")
    )
    assert plan is not None and plan.lease_state == "NOT_STARTED"
    assert container is not None and container.observed_state == "STOPPED"
    assert operation is not None and operation.status == OperationStatus.FAILED
    assert operation.rollback_status == "NOT_REQUIRED"


def test_lease_persistence_failure_compensates_worker_activation(
    client: Any,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _staged_activation(database)
    headers = _login(client, origin_headers, target.user.normalized_login)
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(activation_routes, "call_worker", _worker_success(calls))

    def fail_lease(**_kwargs: Any) -> PortalComputeLease:
        raise SQLAlchemyError("simulated Lease persistence failure")

    monkeypatch.setattr(activation_routes, "create_lease", fail_lease)

    response = _activate(client, headers)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ACTIVATION_PERSISTENCE_FAILED"
    assert [item[0] for item in calls] == [
        "compute.activate.self",
        "compute.activate.self.rollback",
    ]
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 0
    database.expire_all()
    managed = database.get(PortalManagedUser, target.managed.id)
    plan = database.get(PortalProvisionPlan, target.plan.id)
    container = database.get(PortalContainer, target.container.id)
    key = database.get(PortalSshKey, target.key.id) if target.key else None
    assert managed is not None and managed.onboarding_state == OnboardingState.STAGED
    assert managed.compute_activated_at is None
    assert plan is not None and plan.lease_state == "NOT_STARTED"
    assert container is not None and container.observed_state == "STOPPED"
    assert key is not None and key.container_install_state == "NOT_INSTALLED"


def test_invalid_worker_lease_contract_compensates_before_database_activation(
    client: Any,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _staged_activation(database)
    headers = _login(client, origin_headers, target.user.normalized_login)
    calls: list[tuple[str, dict[str, Any]]] = []
    successful_worker = _worker_success(calls)

    def invalid_lease_worker(operation: str, **kwargs: Any) -> dict[str, Any]:
        result = successful_worker(operation, **kwargs)
        if operation == "compute.activate.self":
            starts_at = datetime.fromisoformat(str(result["lease_starts_at"]))
            result["lease_expires_at"] = (starts_at + timedelta(hours=95)).isoformat()
        return result

    monkeypatch.setattr(activation_routes, "call_worker", invalid_lease_worker)

    response = _activate(client, headers)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ACTIVATION_LEASE_CONTRACT_FAILED"
    assert [item[0] for item in calls] == [
        "compute.activate.self",
        "compute.activate.self.rollback",
    ]
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 0
    database.expire_all()
    managed = database.get(PortalManagedUser, target.managed.id)
    plan = database.get(PortalProvisionPlan, target.plan.id)
    container = database.get(PortalContainer, target.container.id)
    assert managed is not None and managed.onboarding_state == OnboardingState.STAGED
    assert managed.compute_activated_at is None
    assert plan is not None and plan.lease_state == "NOT_STARTED"
    assert container is not None and container.observed_state == "STOPPED"


def test_self_activation_requires_csrf_and_is_not_users_write(
    client: Any,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _staged_activation(database)
    headers = _login(client, origin_headers, target.user.normalized_login)
    monkeypatch.setattr(
        activation_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("CSRF rejection must happen before Worker"),
    )

    response = client.post(
        "/api/v1/self/compute/activate",
        headers=origin_headers,
        json={"idempotency_key": str(uuid.uuid4())},
    )

    assert response.status_code == 403
    role = database.scalar(select(PortalRole).where(PortalRole.name == "user"))
    assert role is not None
    assert "self.compute.activate" in role.permissions
    assert "users.write" not in role.permissions
    assert headers["X-CSRF-Token"]
