# Slurm version decision and Debian package build gate

- Run ID: `20260804-050357`
- Decision date: `2026-08-04`
- Host OS: Ubuntu 26.04 LTS (`resolute`), `amd64`
- Kernel after the approved reboot: `7.0.0-28-generic`
- GNU libc: `2.43`
- Secure Boot: disabled
- Current state: Slurm, MUNGE, and MariaDB are not installed
- Gate status: `SLURM SOURCE BUILD APPROVAL REQUIRED`

## Sources checked

- Ubuntu 26.04 APT metadata on the target host
- SchedMD download page: <https://www.schedmd.com/download-slurm/>
- SchedMD release/news archive: <https://slurm.schedmd.com/news.html>
- SchedMD cgroup v2 documentation: <https://slurm.schedmd.com/cgroup_v2.html>
- SchedMD GRES/NVML documentation: <https://slurm.schedmd.com/gres.html>
- SchedMD configless documentation: <https://slurm.schedmd.com/configless_slurm.html>
- NVIDIA Pyxis releases: <https://github.com/NVIDIA/pyxis/releases>
- NVIDIA Pyxis `v0.24.0`: <https://github.com/NVIDIA/pyxis/releases/tag/v0.24.0>

The selected upstream archive is:

`https://download.schedmd.com/slurm/slurm-25.11.7.tar.bz2`

Recorded SHA-256:

`8f71a55b755e41f07d0fc790e9d0c94bd810dc1b324cd308545b9a0fd5f66df6`

The archive will be downloaded again immediately before a build and its digest
must match this value. A mismatch is a hard stop.

## Observed Ubuntu package state

All of these packages are currently uninstalled and have Ubuntu candidate
version `25.11.2-1ubuntu2`:

- `slurm-wlm`
- `slurmctld`
- `slurmd`
- `slurmdbd`
- `libslurm-dev`
- `slurm-client`
- `slurm-wlm-basic-plugins`
- `slurm-wlm-basic-plugins-dev`

No Ubuntu Slurm binary package will be installed before this gate is approved.
Slurm packages from different sources or versions will not be mixed.

## Decision

Recommend SchedMD `25.11.7`, packaged locally as Debian packages with a local
revision such as `25.11.7-1h100.1`.

Do not use Ubuntu's current `25.11.2-1ubuntu2` binaries on this host. The target
requires `jobacct_gather/cgroup` on glibc 2.43, cgroup v2 device enforcement,
NVML GRES discovery, slurmdbd accounting, and Pyxis. The 25.11 maintenance
updates after 25.11.2 include fixes relevant to these exact paths, including a
glibc 2.43+ JobAcctGather failure, cgroup v2 reconfiguration, NVML GPU-name
handling, and controller/database/accounting stability. Deploying 25.11.2
would knowingly retain fixes already available in the same maintenance line.

SchedMD `26.05.2` is the newer feature series checked during this audit, but it
is not selected for this bootstrap. None of the required features needs the
26.05 series, while using it would increase the delta from Ubuntu's 25.11
packaging and the compatibility surface for the current Pyxis stable release.
Selecting the latest 25.11 maintenance release keeps that delta narrow while
including the required maintenance fixes.

## Proposed Debian package build

Source preparation is in progress under the approved gate. Compilation and
Slurm package installation have not started at the time of this revision.

1. Download the SchedMD `25.11.7` archive from the URL above into a dedicated
   build directory. Verify the pinned SHA-256 before extraction.
2. Use the `slurm-smd` Debian packaging shipped inside the verified SchedMD
   `25.11.7` archive. Its source changelog is `25.11.7-1`, its maintainer is
   SchedMD Support, and its binary packages explicitly conflict with Ubuntu's
   `slurm-wlm` family. Do not overlay Ubuntu's `slurm-wlm` packaging.
3. Add a local changelog revision such as `25.11.7-1h100.1`. Build all Slurm
   binary packages from one source tree and one build invocation. Do not use
   `make install`.
4. Derive build dependencies from the actual SchedMD `debian/control` and
   `dpkg-checkbuilddeps` output. Install build dependencies only from the
   configured Ubuntu, NVIDIA, and other already approved official repositories.
   Do not guess or silently disable a feature to satisfy a missing dependency.
5. Ensure the build enables the required MUNGE, MariaDB, JWT, hwloc, cgroup v2,
   and NVIDIA NVML integrations. Do not bundle an NVIDIA driver or CUDA Toolkit.
6. Build unprivileged with Debian tooling and unsigned local packages
   (`dpkg-buildpackage`, no `make install`). Preserve the source digest, build
   log, package list, package metadata, and SHA-256 for every resulting `.deb`.
7. Inspect package contents and linked libraries. The exact paths depend on the
   generated package metadata, but validation must demonstrate the presence of
   the required controller, daemon, client, development headers, slurmdbd,
   cgroup v2, MUNGE, MySQL/MariaDB accounting, JWT, and NVML GRES plugins.
8. Confirm every Slurm package has the same upstream and local revision. Confirm
   no Ubuntu `25.11.2` Slurm binary would be co-installed.
9. Run an APT simulation against the explicit local package set. Stop on any
    removal, downgrade, package-source mixture, or unexpected NVIDIA, kernel,
    SSH, or networking change.
10. Only after the simulation passes, install the explicit package set and add
    an APT version guard so a normal upgrade cannot replace only part of Slurm.
    Record the exact rollback package set before enabling services.
11. Verify versions, `PluginDir`, shared-library resolution, headers, and plugin
    loading. Run `slurmd -C` and `slurmd -G` before creating or starting the
    single-node configuration.
12. Build Pyxis `v0.24.0` later against the exact installed
    `25.11.7-1h100.1` headers and libraries. A plugin-version mismatch is a hard
    stop.

### Packaging discovery during the approved preparation

The verified SchedMD archive includes a complete `debian/` directory for source
package `slurm-smd`. An initial dry-run that attempted to overlay Ubuntu's
`slurm-wlm` packaging stopped before any package installation because the two
source names conflict. The failed staging tree is retained under the ignored
build directory as an audit artifact. The revised build uses only the SchedMD
program source and SchedMD-shipped Debian metadata, which is a smaller and more
auditable source delta than the original plan.

## Required post-build checks before configuration

- All installed Slurm components report `25.11.7` and the same package revision.
- `spank.h` comes from the locally built development package.
- `AutoDetect=nvml` can load against the installed NVIDIA/NVML stack.
- cgroup v2 and `ConstrainDevices` prerequisites remain available.
- MariaDB accounting and JWT plugins are present; secrets are not created or
  printed during the build step.
- `slurmctld`, `slurmd`, and `slurmdbd` are not started with an unvalidated
  configuration.
- The node is never resumed as part of package installation.

## Rollback boundary

Before package installation, preserve the package manifest, generated `.deb`
files, APT simulation, and any existing `/etc/slurm` directory backup. If package
validation or installation fails, stop services, retain logs and packages for
analysis, and do not fall through to Ubuntu 25.11.2 or an ad-hoc source install.

## Approval

The operator explicitly approved the following on `2026-08-04`:

`允许从 SchedMD 25.11.7 官方源码构建并安装 Debian 包`
