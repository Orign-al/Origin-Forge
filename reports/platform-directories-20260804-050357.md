# Platform data-directory creation

- Run ID: `20260804-050357`
- Filesystem: `/srv/gpu-platform` (independent XFS)
- Existing unknown target paths: none
- Additional user directories created: none

Created with the required ownership and modes:

- `/srv/gpu-platform/users`: `root:root`, `0711`
- `/srv/gpu-platform/datasets`: `root:gpu-platform-admin`, `2770`
- `/srv/gpu-platform/models`: `root:gpu-platform-admin`, `2770`
- `/srv/gpu-platform/scratch`: `root:root`, `1777`
- `/srv/gpu-platform/enroot` and initial subdirectories: `root:root`, `0755`
- `/srv/gpu-platform/container-data`: `root:root`, `0755`
- `/srv/gpu-platform/backup-staging`: `root:root`, `0700`
- `/srv/gpu-platform/platform`: `root:gpu-platform-admin`, `2770`
- `/srv/gpu-platform/platform/secrets`: `codexops:gpu-platform-admin`, `0700`

No employee account, employee workspace, or employee container was created.
