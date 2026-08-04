# Enroot version decision

- Stable release selected: v4.2.1
- Release date: 2026-06-09
- Tag commit: ead3a25ed974948235d28f99fc387ac82435ce62
- Official release: https://github.com/NVIDIA/enroot/releases/tag/v4.2.1
- Package flavor: standard plus capabilities
- Selection reason: official standard HPC flavor; +caps enables unprivileged image import/conversion
- Hardened flavor selected: no; its documented overhead is not required for this trusted internal HPC model
- AppArmor sysctl changed: no
- AppArmor plan: use the persistent /etc/apparmor.d/enroot profile shipped in the release asset
- Artifact checksum manifest: /srv/gpu-platform/platform/reports/enroot-preflight-finalize-20260804-050357.d/EXPECTED_SHA256SUMS
- APT transaction: zero upgrades and zero removals

Status: ENROOT PREFLIGHT PASSED
