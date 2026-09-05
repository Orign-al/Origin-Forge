from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parents[2]
POLICY = PLATFORM_ROOT / "scripts/h100-multigpu-qos-policy"

FAKE_SACCTMGR = r"""#!/usr/bin/env python3
import json
import os
import sys

path = os.environ["FAKE_SLURM_STATE"]
with open(path, encoding="utf-8") as handle:
    state = json.load(handle)
args = [arg for arg in sys.argv[1:] if arg not in {"-nP", "-i"}]

def csv_add(value, item):
    values = [part for part in value.split(",") if part]
    if item not in values:
        values.append(item)
    return ",".join(values)

def csv_remove(value, item):
    return ",".join(part for part in value.split(",") if part and part != item)

def value(prefix):
    return next((item.split("=", 1)[1] for item in args if item.startswith(prefix + "=")), "")

if args[:2] == ["show", "qos"]:
    name = value("Name")
    if not name and len(args) > 2 and not args[2].startswith("format="):
        name = args[2]
    maximum = state["qos"].get(name)
    if maximum is not None:
        print(f"{name}|{maximum}|")
elif args[:2] == ["show", "assoc"]:
    requested_user = value("User")
    output_format = next(item for item in args if item.startswith("format="))[7:]
    if output_format.startswith("Account,User"):
        print(f"company||{state['account_qos']}|||")
        for user, assoc in sorted(state["users"].items()):
            print(
                f"company|{user}|{assoc['qos']}|{assoc['default_qos']}|"
                f"{assoc['max_tres']}|{assoc['group_tres']}|"
            )
    elif requested_user:
        assoc = state["users"].get(requested_user)
        if assoc is not None:
            print(
                f"{requested_user}|company|{assoc['qos']}|{assoc['default_qos']}|"
                f"{assoc['max_tres']}|{assoc['group_tres']}|"
            )
elif args[:2] == ["add", "qos"]:
    state["qos"][args[2]] = "gres/gpu=4"
elif args[:2] == ["delete", "qos"]:
    state["qos"].pop(value("Name"), None)
elif args[:2] == ["modify", "qos"]:
    name = value("Name")
    for item in args[args.index("set") + 1:]:
        if item.startswith("MaxTRESPU="):
            state["qos"][name] = item.split("=", 1)[1]
elif args[:2] == ["modify", "account"]:
    for item in args[args.index("set") + 1:]:
        if item.startswith("QOS+="):
            state["account_qos"] = csv_add(state["account_qos"], item[5:])
        elif item.startswith("QOS-="):
            state["account_qos"] = csv_remove(state["account_qos"], item[5:])
elif args[:2] == ["modify", "user"]:
    user = value("Name")
    assoc = state["users"][user]
    for item in args[args.index("set") + 1:]:
        if item.startswith("QOS+="):
            assoc["qos"] = csv_add(assoc["qos"], item[5:])
        elif item.startswith("QOS="):
            assoc["qos"] = item[4:]
        elif item.startswith("DefaultQOS="):
            assoc["default_qos"] = item.split("=", 1)[1]
        elif item.startswith("MaxTRES="):
            maximum = item.split("=", 1)[1]
            assoc["max_tres"] = "" if maximum == "gres/gpu=-1" else maximum
        elif item.startswith("GrpTRES="):
            maximum = item.split("=", 1)[1]
            assoc["group_tres"] = "" if maximum == "gres/gpu=-1" else maximum
else:
    raise SystemExit(f"unsupported fake sacctmgr invocation: {args!r}")

with open(path, "w", encoding="utf-8") as handle:
    json.dump(state, handle, sort_keys=True)
"""


def _association(
    qos: str = "general", max_tres: str = "gres/gpu=1", group_tres: str = ""
) -> dict[str, str]:
    return {
        "qos": qos,
        "default_qos": "general",
        "max_tres": max_tres,
        "group_tres": group_tres,
    }


def _state_file(root: Path, user: str, max_gpus: int | None) -> None:
    maximum = [] if max_gpus is None else [f"MAX_GPUS={max_gpus}"]
    (root / f"{user}.state").write_text(
        "\n".join(
            [
                "VERSION=2",
                f"USERNAME={user}",
                "SLURM_ACCOUNT=company",
                "SLURM_QOS=general",
                *maximum,
                "",
            ]
        )
    )


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "policy"
    user_state_dir = tmp_path / "users"
    bin_dir.mkdir()
    user_state_dir.mkdir()
    sacctmgr = bin_dir / "sacctmgr"
    sacctmgr.write_text(FAKE_SACCTMGR)
    sacctmgr.chmod(0o755)
    squeue = bin_dir / "squeue"
    squeue.write_text(
        '#!/bin/sh\n[ -z "${FAKE_ACTIVE_QOS:-}" ] || printf \'%s\\n\' "$FAKE_ACTIVE_QOS"\n'
    )
    squeue.chmod(0o755)
    database = tmp_path / "slurm.json"
    database.write_text(
        json.dumps(
            {
                "qos": {"general": "gres/gpu=1"},
                "account_qos": "general",
                "users": {
                    "alice": _association(),
                    "bob": _association(max_tres="gres/gpu=0"),
                    "origin-pilot": _association(max_tres=""),
                    "outside-user": _association(),
                },
            }
        )
    )
    _state_file(user_state_dir, "alice", 1)
    _state_file(user_state_dir, "bob", 0)
    _state_file(user_state_dir, "origin-pilot", None)
    env = {
        **os.environ,
        "FAKE_SLURM_STATE": str(database),
        "H100_MULTIGPU_POLICY_STATE_DIR": str(state_dir),
        "H100_MULTIGPU_POLICY_USER_STATE_DIR": str(user_state_dir),
        "H100_MULTIGPU_POLICY_SACCTMGR": str(sacctmgr),
        "H100_MULTIGPU_POLICY_SQUEUE": str(squeue),
    }
    return env, database, state_dir, user_state_dir


def _run(env: dict[str, str], action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(POLICY), action],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.skipif(os.geteuid() != 0, reason="policy script intentionally requires root")
def test_multigpu_qos_apply_is_scoped_idempotent_and_reversible(tmp_path: Path) -> None:
    env, database, state_dir, user_state_dir = _fixture(tmp_path)

    applied = _run(env, "apply")
    assert applied.returncode == 0, applied.stderr
    assert "MULTIGPU_QOS_POLICY=PASS USERS=3" in applied.stdout
    state = json.loads(database.read_text())
    assert state["qos"]["portal-approved-multigpu"] == "gres/gpu=4"
    assert state["account_qos"] == "general,portal-approved-multigpu"
    for user in ("alice", "bob", "origin-pilot"):
        assert state["users"][user] == _association(
            "general,portal-approved-multigpu", "gres/gpu=4", "gres/gpu=4"
        )
    assert state["users"]["outside-user"] == _association()
    snapshot = state_dir / "multigpu-qos-policy.snapshot"
    snapshot_before = snapshot.read_text()

    replay = _run(env, "apply")
    assert replay.returncode == 0, replay.stderr
    assert snapshot.read_text() == snapshot_before

    _state_file(user_state_dir, "carol", 1)
    state["users"]["carol"] = _association(
        "general,portal-approved-multigpu", "gres/gpu=4", "gres/gpu=4"
    )
    database.write_text(json.dumps(state))
    verified = _run(env, "verify")
    assert verified.returncode == 0, verified.stderr
    assert "USERS=4" in verified.stdout

    rolled_back = _run(env, "rollback")
    assert rolled_back.returncode == 0, rolled_back.stderr
    state = json.loads(database.read_text())
    assert "portal-approved-multigpu" not in state["qos"]
    assert state["account_qos"] == "general"
    assert state["users"]["alice"] == _association()
    assert state["users"]["bob"] == _association(max_tres="gres/gpu=0")
    assert state["users"]["origin-pilot"] == _association(max_tres="")
    assert state["users"]["carol"] == _association()
    assert state["users"]["outside-user"] == _association()
    assert not snapshot.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="policy script intentionally requires root")
def test_multigpu_qos_verify_rejects_unmanaged_user_exposure(tmp_path: Path) -> None:
    env, database, _state_dir, _user_state_dir = _fixture(tmp_path)
    assert _run(env, "apply").returncode == 0
    state = json.loads(database.read_text())
    state["users"]["outside-user"] = _association(
        "general,portal-approved-multigpu", "gres/gpu=4", "gres/gpu=4"
    )
    database.write_text(json.dumps(state))

    result = _run(env, "verify")
    assert result.returncode != 0
    assert "unmanaged Slurm user outside-user unexpectedly has dedicated QoS" in result.stderr


@pytest.mark.skipif(os.geteuid() != 0, reason="policy script intentionally requires root")
def test_multigpu_qos_apply_recovers_owned_partial_application(tmp_path: Path) -> None:
    env, database, state_dir, _user_state_dir = _fixture(tmp_path)
    assert _run(env, "apply").returncode == 0
    snapshot_before = (state_dir / "multigpu-qos-policy.snapshot").read_text()

    state = json.loads(database.read_text())
    state["qos"]["portal-approved-multigpu"] = "gres/gpu=2"
    state["account_qos"] = "general"
    state["users"]["bob"] = _association(max_tres="gres/gpu=0")
    database.write_text(json.dumps(state))

    recovered = _run(env, "apply")
    assert recovered.returncode == 0, recovered.stderr
    assert "MULTIGPU_QOS_POLICY=PASS USERS=3" in recovered.stdout
    assert (state_dir / "multigpu-qos-policy.snapshot").read_text() == snapshot_before
    state = json.loads(database.read_text())
    assert state["qos"]["portal-approved-multigpu"] == "gres/gpu=4"
    assert state["account_qos"] == "general,portal-approved-multigpu"
    assert state["users"]["bob"] == _association(
        "general,portal-approved-multigpu", "gres/gpu=4", "gres/gpu=4"
    )


@pytest.mark.skipif(os.geteuid() != 0, reason="policy script intentionally requires root")
def test_multigpu_qos_apply_rejects_corrupt_owned_snapshot_before_write(tmp_path: Path) -> None:
    env, database, state_dir, _user_state_dir = _fixture(tmp_path)
    state_dir.mkdir()
    snapshot = state_dir / "multigpu-qos-policy.snapshot"
    snapshot.write_text("VERSION|1\nUSER|alice|company|general|general|gres/gpu=9||\n")
    before = database.read_text()

    result = _run(env, "apply")
    assert result.returncode != 0
    assert "unexpected original GPU ceiling for alice" in result.stderr
    assert database.read_text() == before
    assert snapshot.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="policy script intentionally requires root")
def test_multigpu_qos_rollback_blocks_while_approved_job_is_active(tmp_path: Path) -> None:
    env, database, state_dir, _user_state_dir = _fixture(tmp_path)
    assert _run(env, "apply").returncode == 0
    before = database.read_text()
    env["FAKE_ACTIVE_QOS"] = "portal-approved-multigpu"

    result = _run(env, "rollback")
    assert result.returncode != 0
    assert "cannot rollback while a dedicated multi-GPU QoS Job is active" in result.stderr
    assert database.read_text() == before
    assert (state_dir / "multigpu-qos-policy.snapshot").exists()
