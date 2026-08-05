# GPU device identity probes

These probes validate physical NVIDIA device allocation without assuming that
CUDA logical index 0 is `/dev/nvidia0`.

## Sources

- `gpu-open-probe.py` correlates `nvidia-smi` UUIDs with procfs `Device Minor`
  values, then tests every per-GPU, shared/control, and caps node with
  `O_RDONLY`, `O_WRONLY`, and `O_RDWR`.
- `cuda-context-probe.py` uses `ctypes` and `libcuda.so.1` to create,
  synchronize, briefly hold, and destroy a real CUDA Driver API context.
- `gpu-device-context-probe.c` is the no-CUDA-header equivalent for the pinned
  Python-free CUDA base image. Build it with:

  ```bash
  gcc -std=gnu11 -O2 -Wall -Wextra -Werror \
    -o gpu-device-context-probe gpu-device-context-probe.c -ldl
  ```

- `run-bare-gpu-probe.sh`, `run-pyxis-gpu-probe.sh`, and
  `run-bpf-gpu-probe.sh` are bounded Slurm validation wrappers.

## Security assertions

For a one-GPU Slurm job, use:

```bash
python3 gpu-open-probe.py \
  --expect-allocated-count 1 \
  --assert-slurm-isolation

python3 cuda-context-probe.py --expect success --hold-seconds 5
```

For a login session expected to have no physical GPU access, use:

```bash
python3 gpu-open-probe.py --assert-out-of-job-denied
python3 cuda-context-probe.py --expect denied --hold-seconds 0
```

The open assertion is based on the UUID-correlated Linux minor and exact
`O_RDWR` result. `CUDA_VISIBLE_DEVICES`, the logical `nvidia-smi` index, and a
fixed `/dev/nvidia0` path are never treated as physical identity evidence.

The C probe is intended only for the approved pinned local Pyxis image. It
loads NVML and the CUDA Driver API with `dlopen`; it does not require CUDA
Toolkit headers or `nvcc` and does not execute a compute workload.
