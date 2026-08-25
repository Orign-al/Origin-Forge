# Portal-5A Workspace Mount Contract

Status: implementation contract, not yet deployed

Platform: Portal-5A / H100 single-node cloud

Contract version: `workspace-v1`

## Outcome

Every managed Linux UID has one persistent workspace. Portal, the development
container, and Slurm refer to the same bytes; none of these paths is a copy or
synchronization target.

| Coordinate               | Contract                                                            |
| ------------------------ | ------------------------------------------------------------------- |
| Stable owner key         | managed Linux UID/GID                                               |
| Host and Slurm path      | `/storage/users/{uid}`                                              |
| Container path           | `/workspace`                                                        |
| Default job workdir      | `/storage/users/{uid}/projects`                                     |
| User-visible output root | `/storage/users/{uid}/outputs`                                      |
| Backing filesystem       | `/srv/gpu-platform/users/{username}/workspace` on quota-enabled XFS |

The username is display and Slurm-account metadata. It never selects a
workspace path. `workspace_path(uid)` is the single application-level path
derivation function.

```text
PortalManagedUser.uid
        |
        v
/srv/gpu-platform/users/{username}/workspace
        |
        +-- per-user systemd bind alias (same device and inode)
        v
/storage/users/{uid}  (canonical owner-bound path)
        |
        +-- bind mount --> development container:/workspace
        |
        +-- Slurm --chdir --> /storage/users/{uid}/projects
        |
        +-- Portal storage view/usage --> /storage/users/{uid}
```

## Directory layout

Provision creates the following directories as `{uid}:{gid}`. The workspace
root and all managed internal directories use mode `0700`.

```text
/storage/users/{uid}/
├── projects/
├── datasets/
├── outputs/
└── .portal/
    ├── job-scripts/
    └── jobs/
```

`.portal` is reserved for Portal artifacts. Users edit projects under
`/workspace/projects`; job stdout and stderr are written under `outputs` so the
development container sees results immediately.

## Container contract

The fixed Compose definition bind-mounts exactly the canonical UID workspace:

```text
/storage/users/{uid}:/workspace:rw,rprivate
```

The worker rejects the container unless all of the following remain true:

- the host workspace is a real directory, not a symlink;
- owner and group equal the managed UID/GID and root mode is exactly `0700`;
- image labels bind the same UID/GID and username;
- the bind source is the canonical UID path, or the inventoried backing path
  only when it has the exact same device and inode, and destination is
  `/workspace`;
- the only other writable binds are the owner's container home, preserved
  owner-only `/shared`, and persistent SSH host-key directory;
- `/`, the Docker socket, MUNGE, and other host paths are absent;
- privileged and host network/PID/IPC modes are disabled;
- no capabilities or direct host devices are added.

The container home and existing owner-only `/shared` mount remain separate for
compatibility. New project data uses `/workspace`.

## Slurm contract

Portal accepts script text, stages an immutable owner-bound artifact under
`.portal/job-scripts`, and submits it using a fixed argv with `shell=False`.
`setpriv` enters the managed UID/GID, clears supplementary groups and all
capability sets, and only then invokes `sbatch`.

Each job receives:

```text
WORKSPACE=/storage/users/{uid}
--chdir=/storage/users/{uid}/projects
--output=/storage/users/{uid}/outputs/{portal_job_id}.out
--error=/storage/users/{uid}/outputs/{portal_job_id}.err
```

Containerized Slurm jobs receive only two aliases of the same owner root:
the canonical host path at itself and at `/workspace`. Host home mounting is
disabled. GPU requests remain `0` or `1`; `2` is rejected before Worker
execution.

All path traversal uses descriptor-relative `O_NOFOLLOW` opens and validates
file type plus UID/GID at each component. A request carrying another UID's
workspace path is rejected as `WORKSPACE_BINDING_REJECTED`.

## Lifecycle

| Lease state   | Workspace                 | Development container | Slurm access                    |
| ------------- | ------------------------- | --------------------- | ------------------------------- |
| `ACTIVE`      | mounted and preserved     | may run               | enabled within entitlement      |
| `RECYCLE_BIN` | preserved in place        | stopped               | jobs cancelled, new work denied |
| restored      | same inode tree remounted | restarted             | new lease controls access       |

Recycle and restore never copy workspace data. Permanent deletion remains a
separate typed-confirmation administrative path and validates the exact UID
workspace before removal.

## Portal file view

Portal currently reports quota and usage but does not expose a file browser.
Any future file API must root descriptor-relative traversal at
`/storage/users/{uid}`, apply the same owner checks, and must not introduce a
second storage tree.

## Security invariants

RBAC, session CSRF, recent reauthentication for high-risk administration,
owner-scoped queries, the Unix-socket Root Worker boundary, fixed handlers,
`shell=False`, Slurm account/QOS restrictions, max GPU `1`, and Lease checks
remain mandatory. This contract does not authorize host shell access,
privileged containers, the Docker socket, MUNGE mounts, or arbitrary host
filesystem access.
