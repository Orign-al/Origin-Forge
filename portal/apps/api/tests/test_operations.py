import pytest
from h100_portal_api.enums import OperationStatus, RiskLevel
from h100_portal_api.operations import can_transition, risk_for
from h100_portal_api.routes.operations import validate_operation_payload


def test_operation_state_machine() -> None:
    assert can_transition(OperationStatus.DRAFT, OperationStatus.PENDING_APPROVAL)
    assert can_transition(OperationStatus.PENDING_APPROVAL, OperationStatus.APPROVED)
    assert can_transition(OperationStatus.APPROVED, OperationStatus.QUEUED)
    assert can_transition(OperationStatus.QUEUED, OperationStatus.RUNNING)
    assert can_transition(OperationStatus.RUNNING, OperationStatus.SUCCEEDED)
    assert not can_transition(OperationStatus.SUCCEEDED, OperationStatus.RUNNING)


def test_risk_classification() -> None:
    assert risk_for("slurm.resume") == RiskLevel.CRITICAL
    assert risk_for("quota.update") == RiskLevel.HIGH
    assert risk_for("user.plan") == RiskLevel.MEDIUM


def test_operation_payload_rejects_arbitrary_fields_and_protected_users() -> None:
    with pytest.raises(ValueError):
        validate_operation_payload("user.stage", {"username": "root"})
    with pytest.raises(ValueError):
        validate_operation_payload("container.start", {"name": "gpu-dev-user", "command": "sh"})
    with pytest.raises(ValueError):
        validate_operation_payload("slurm.resume", {"node_name": "other-node"})


def test_portal3a_plan_requires_separate_compute_username() -> None:
    assert validate_operation_payload("user.plan", {"username": "origin-pilot"}) == {
        "username": "origin-pilot"
    }
    with pytest.raises(ValueError):
        validate_operation_payload("user.plan", {"username": "origin-al"})


def test_quota_is_bounded() -> None:
    assert (
        validate_operation_payload(
            "quota.update", {"username": "example-user", "quota_bytes": 300 * 1024**3}
        )["quota_bytes"]
        == 300 * 1024**3
    )
    with pytest.raises(ValueError):
        validate_operation_payload("quota.update", {"username": "example-user", "quota_bytes": -1})
