# GPU policy framework tests

The production probes live in `scripts/h100-gpu-open-probe` and
`scripts/h100-cuda-context-probe`.

Phase Pilot-1D validates the framework without creating a Pilot account:

- static syntax and policy-pattern checks;
- protected and nonexistent-account negative tests;
- a runtime-only `user-1001.slice DevicePolicy=closed` regression;
- transient probes whose cgroup path is proven under the exact UID slice;
- rollback to `user-1001.slice DevicePolicy=auto`;
- an empty-registry Guard run with the timer disabled.

No test in this directory may create a Linux user, persistent UID drop-in,
Pilot container, or Slurm job.
