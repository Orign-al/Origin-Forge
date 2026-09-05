# Multi-GPU approval deployment and rollback

This runbook applies to the not-yet-deployed
`PORTAL-5A-USER-MULTIGPU-APPROVAL-1` candidate. It does not authorize a
production deployment.

## Deployment order

1. Verify the immutable Git bundle, Web artifact, manifest, candidate commit,
   tree, and checksums before any production write.
2. Capture a new rollback point containing Git/tree, database revision and
   backup, Portal service state, current Slurm queue/GPU allocations, exact QoS,
   account and per-user association output, and the managed-user state-file
   inventory.
3. Require no active or pending Job using `portal-approved-multigpu`. Existing
   `general` Jobs do not need to be cancelled or modified.
4. Install the reviewed runtime and systemd units without starting the new API.
5. Run `h100-multigpu-qos-policy apply`, then `verify`. The root-owned snapshot
   at `/var/lib/h100-portal/multigpu-qos-policy.snapshot` must exist. A partial
   first application is resumed idempotently from that owned snapshot.
6. Upgrade PostgreSQL from `d2e3f4a5b6c7` to `e3f4a5b6c7d8` with Alembic and
   verify the exact head.
7. Start/reload only required Portal services. API startup is ordered after and
   requires the successful QoS policy unit.
8. Verify API, Worker, Web, timers, private CLI ingress, loopback-only API,
   EasyTier Web, and existing-container CLI behavior before acceptance.

The policy keeps `general` at one GPU, creates the dedicated approved policy at
four GPUs, gives it only to managed Portal users, sets each managed association
to a four-GPU per-Job ceiling and a four-GPU aggregate ceiling, and preserves
`general` as the default. Future Portal-provisioned users receive the same
association contract through the reviewed provisioning lifecycle.

## Rollback order

Rollback is explicit; the systemd unit intentionally has no automatic
`ExecStop` because stopping a service must never mutate scheduling policy.

1. Stop the candidate API so no new approval can be submitted or decided.
2. Confirm with authoritative Slurm state that no active or pending Job uses
   `portal-approved-multigpu`. Never cancel a user Job automatically. If one
   exists, rollback is blocked until it terminates normally or an independently
   authorized action resolves it.
3. Run `h100-multigpu-qos-policy rollback`. It restores every snapshotted
   association, restores any later managed user from its lifecycle state,
   removes the dedicated QoS from the account, deletes the dedicated QoS, and
   removes only its owned snapshot.
4. Downgrade Alembic to `d2e3f4a5b6c7`. Downgrade intentionally fails closed if
   any multi-GPU approval row or any Job incompatible with the legacy
   constraints exists. Do not delete or rewrite those rows manually.
5. Restore the previous immutable runtime/Web release and previous systemd
   units, then start the previous services.
6. Verify the original `general` QoS and each association, API/Worker/Web,
   timers, CLI ingress, queue, GPU allocations, Lease/storage counts, and user
   data against the rollback manifest.

Do not weaken QoS, edit Slurm accounting rows manually, force an Alembic
downgrade, cancel Jobs, or expose host Slurm/MUNGE to work around a failed gate.
