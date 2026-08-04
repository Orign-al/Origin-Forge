# XFS project-quota fstab configuration

- Run ID: `20260804-050357`
- Filesystems: `/var/lib/docker`, `/srv/gpu-platform`
- Devices: existing `/dev/disk/by-id/dm-uuid-LVM-*` paths preserved
- Filesystem types: XFS preserved
- Added options: `noatime,prjquota`
- Partitioning, formatting, LVM and mount points: unchanged
- Remount/unmount performed: no

## Integrity and backup

- Original `/etc/fstab` SHA-256: `d4beb82b4bbc08558414d60cc674c8211af1a67dfb8e6d233df3e4906d3fd222`
- Installed `/etc/fstab` SHA-256: `5863bcaf349f944e4fc10a0ed9b155a7c8f39683a6f5f6d03d9ca1ea4008beb1`
- Backup: `/srv/gpu-platform/platform/backups/fstab-20260804-050357`
- Repository copy: `/srv/gpu-platform/platform/config/fstab`
- Final `findmnt --verify --verbose`: success, no errors or warnings
- systemd daemon reload: completed; no filesystem was remounted

## Execution notes

- First attempt stopped before any fstab write because comment lines were included in the uniqueness check.
- First failed script backup: `/srv/gpu-platform/platform/backups/03-configure-project-quota-fstab.sh-20260804-052420`
- Second attempt stopped before any fstab write because `index` conflicted with an awk built-in function.
- Second failed script backup: `/srv/gpu-platform/platform/backups/03-configure-project-quota-fstab.sh-20260804-052542`
- The corrected policy was executed locally against the exact candidate before the successful transaction.

## Current state

- fstab target options for both filesystems: `defaults,noatime,prjquota`
- Current live mount options: `relatime,noquota`
- Quota activation is intentionally deferred to the approved combined reboot window.
- Status: `QUOTA REBOOT REQUIRED`
- `PROJECT QUOTA PASSED` must not be declared until post-reboot `findmnt` and `xfs_quota state` validation succeeds.
