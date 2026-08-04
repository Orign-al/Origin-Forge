# XFS project-quota post-reboot acceptance

- Run ID: `20260804-050357`
- Boot ID: `3b8b3af2-d2bd-4040-886e-e9129ac6c005`
- Running kernel: `7.0.0-28-generic`
- Reboot completed: 2026-08-04 14:19:52 CST

## `/var/lib/docker`

- Source: `/dev/mapper/vg_h100-lv_docker`
- Filesystem: XFS
- Live options include: `noatime,prjquota`
- Project quota accounting: ON
- Project quota enforcement: ON

## `/srv/gpu-platform`

- Source: `/dev/mapper/vg_h100-lv_platform`
- Filesystem: XFS
- Live options include: `noatime,prjquota`
- Project quota accounting: ON
- Project quota enforcement: ON

No remount, unmount, formatting, partitioning, RAID, or LVM operation was performed. The fstab backup remains at `/srv/gpu-platform/platform/backups/fstab-20260804-050357`.

Raw acceptance record: `/srv/gpu-platform/platform/reports/project-quota-post-reboot-20260804-050357.txt`

Raw record SHA-256: `e2874af49d0d11d37ceedcdc8480006c1e9f5d251d2d8f4538c4ad96cffbb827`

Status: `PROJECT QUOTA PASSED`
