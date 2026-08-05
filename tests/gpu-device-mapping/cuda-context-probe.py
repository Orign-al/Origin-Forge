#!/usr/bin/env python3
"""Create and briefly hold one real CUDA Driver API context."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import sys
import time
from pathlib import Path


CUDA_SUCCESS = 0


class CUuuid(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]


def uuid_text(value: CUuuid) -> str:
    raw = bytes(value.bytes)
    groups = [raw[0:4], raw[4:6], raw[6:8], raw[8:10], raw[10:16]]
    return "GPU-" + "-".join(group.hex() for group in groups)


def cgroup_text() -> str:
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()
    except OSError as exc:
        return f"ERROR errno={exc.errno} {exc.strerror}"


class Driver:
    def __init__(self) -> None:
        library_name = ctypes.util.find_library("cuda") or "libcuda.so.1"
        self.library_name = library_name
        self.lib = ctypes.CDLL(library_name)

        self.lib.cuInit.argtypes = [ctypes.c_uint]
        self.lib.cuInit.restype = ctypes.c_int
        self.lib.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
        self.lib.cuDeviceGetCount.restype = ctypes.c_int
        self.lib.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        self.lib.cuDeviceGet.restype = ctypes.c_int
        self.lib.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        self.lib.cuDeviceGetName.restype = ctypes.c_int
        self.lib.cuCtxSynchronize.argtypes = []
        self.lib.cuCtxSynchronize.restype = ctypes.c_int

        self.ctx_create_name, self.ctx_create = self._symbol(
            ["cuCtxCreate_v2", "cuCtxCreate"]
        )
        self.ctx_create.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint,
            ctypes.c_int,
        ]
        self.ctx_create.restype = ctypes.c_int

        self.ctx_destroy_name, self.ctx_destroy = self._symbol(
            ["cuCtxDestroy_v2", "cuCtxDestroy"]
        )
        self.ctx_destroy.argtypes = [ctypes.c_void_p]
        self.ctx_destroy.restype = ctypes.c_int

        self.uuid_name, self.device_get_uuid = self._symbol(
            ["cuDeviceGetUuid_v2", "cuDeviceGetUuid"]
        )
        self.device_get_uuid.argtypes = [ctypes.POINTER(CUuuid), ctypes.c_int]
        self.device_get_uuid.restype = ctypes.c_int

        self.get_error_name = getattr(self.lib, "cuGetErrorName", None)
        if self.get_error_name is not None:
            self.get_error_name.argtypes = [
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_char_p),
            ]
            self.get_error_name.restype = ctypes.c_int
        self.get_error_string = getattr(self.lib, "cuGetErrorString", None)
        if self.get_error_string is not None:
            self.get_error_string.argtypes = [
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_char_p),
            ]
            self.get_error_string.restype = ctypes.c_int

    def _symbol(self, names: list[str]):
        for name in names:
            symbol = getattr(self.lib, name, None)
            if symbol is not None:
                return name, symbol
        raise RuntimeError(f"none of the CUDA symbols are available: {names}")

    def describe_error(self, code: int) -> str:
        parts = [f"code={code}"]
        for label, function in [
            ("name", self.get_error_name),
            ("message", self.get_error_string),
        ]:
            if function is None:
                continue
            value = ctypes.c_char_p()
            if function(code, ctypes.byref(value)) == CUDA_SUCCESS and value.value:
                parts.append(f"{label}={value.value.decode(errors='replace')}")
        return " ".join(parts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expect",
        choices=["success", "denied", "any"],
        default="any",
    )
    parser.add_argument("--hold-seconds", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(f"PID={os.getpid()} UID={os.getuid()} GID={os.getgid()}")
    print(f"SLURM_JOB_ID={os.environ.get('SLURM_JOB_ID', '')}")
    print(f"CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES', '')}")
    print("CGROUP_BEGIN")
    print(cgroup_text())
    print("CGROUP_END")

    context_created = False
    context = ctypes.c_void_p()
    failure_stage = ""
    failure_detail = ""
    driver: Driver | None = None

    try:
        driver = Driver()
        print(f"CUDA_LIBRARY={driver.library_name}")
        print(f"CUDA_CTX_CREATE_SYMBOL={driver.ctx_create_name}")
        print(f"CUDA_CTX_DESTROY_SYMBOL={driver.ctx_destroy_name}")
        print(f"CUDA_UUID_SYMBOL={driver.uuid_name}")

        code = driver.lib.cuInit(0)
        print(f"CUINIT={driver.describe_error(code)}")
        if code != CUDA_SUCCESS:
            failure_stage = "cuInit"
            failure_detail = driver.describe_error(code)
            raise RuntimeError(failure_stage)

        count = ctypes.c_int()
        code = driver.lib.cuDeviceGetCount(ctypes.byref(count))
        print(f"CUDEVICEGETCOUNT={driver.describe_error(code)} count={count.value}")
        if code != CUDA_SUCCESS or count.value < 1:
            failure_stage = "cuDeviceGetCount"
            failure_detail = driver.describe_error(code) + f" count={count.value}"
            raise RuntimeError(failure_stage)

        device = ctypes.c_int()
        code = driver.lib.cuDeviceGet(ctypes.byref(device), 0)
        print(f"CUDEVICEGET={driver.describe_error(code)} device={device.value}")
        if code != CUDA_SUCCESS:
            failure_stage = "cuDeviceGet"
            failure_detail = driver.describe_error(code)
            raise RuntimeError(failure_stage)

        name_buffer = ctypes.create_string_buffer(256)
        code = driver.lib.cuDeviceGetName(name_buffer, len(name_buffer), device.value)
        print(
            f"CUDEVICEGETNAME={driver.describe_error(code)} "
            f"name={name_buffer.value.decode(errors='replace')}"
        )
        if code != CUDA_SUCCESS:
            failure_stage = "cuDeviceGetName"
            failure_detail = driver.describe_error(code)
            raise RuntimeError(failure_stage)

        device_uuid = CUuuid()
        code = driver.device_get_uuid(ctypes.byref(device_uuid), device.value)
        print(
            f"CUDEVICEGETUUID={driver.describe_error(code)} "
            f"uuid={uuid_text(device_uuid) if code == CUDA_SUCCESS else ''}"
        )
        if code != CUDA_SUCCESS:
            failure_stage = driver.uuid_name
            failure_detail = driver.describe_error(code)
            raise RuntimeError(failure_stage)

        code = driver.ctx_create(ctypes.byref(context), 0, device.value)
        print(f"CUCTXCREATE={driver.describe_error(code)} context={context.value}")
        if code != CUDA_SUCCESS:
            failure_stage = driver.ctx_create_name
            failure_detail = driver.describe_error(code)
            raise RuntimeError(failure_stage)
        context_created = True

        code = driver.lib.cuCtxSynchronize()
        print(f"CUCTXSYNCHRONIZE={driver.describe_error(code)}")
        if code != CUDA_SUCCESS:
            failure_stage = "cuCtxSynchronize"
            failure_detail = driver.describe_error(code)
            raise RuntimeError(failure_stage)

        print(f"CUDA_CONTEXT_HOLD_SECONDS={args.hold_seconds}")
        time.sleep(max(0.0, args.hold_seconds))
    except (OSError, RuntimeError) as exc:
        if not failure_stage:
            failure_stage = "driver_load_or_symbol"
            failure_detail = str(exc)
    finally:
        if context_created and driver is not None:
            destroy_code = driver.ctx_destroy(context)
            print(f"CUCTXDESTROY={driver.describe_error(destroy_code)}")
            if destroy_code != CUDA_SUCCESS and not failure_stage:
                failure_stage = driver.ctx_destroy_name
                failure_detail = driver.describe_error(destroy_code)

    succeeded = context_created and not failure_stage
    if succeeded:
        print("CUDA_CONTEXT_RESULT=PASSED")
    else:
        print(
            "CUDA_CONTEXT_RESULT=BLOCKED "
            f"stage={failure_stage} detail={failure_detail}"
        )

    if args.expect == "success":
        return 0 if succeeded else 1
    if args.expect == "denied":
        return 1 if succeeded else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
