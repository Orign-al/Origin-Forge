# Docker Engine and NVIDIA Container Toolkit

- Run ID: `20260804-050357`
- OS: Ubuntu 26.04 LTS (`resolute`), amd64
- Docker official documentation: <https://docs.docker.com/engine/install/ubuntu/>
- Docker official repository: <https://download.docker.com/linux/ubuntu>
- Docker signing-key fingerprint: `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`

## Docker package versions

- Docker Engine / CLI: `29.7.1`
- `docker-ce`: `5:29.7.1-1~ubuntu.26.04~resolute`
- `docker-ce-cli`: `5:29.7.1-1~ubuntu.26.04~resolute`
- `containerd.io`: `2.2.6-1~ubuntu.26.04~resolute`
- Buildx plugin: `0.36.0-1~ubuntu.26.04~resolute`
- Compose plugin: `5.4.0-1~ubuntu.26.04~resolute`

The installation simulation added seven packages and removed none. There was no Snap Docker or conflicting distribution Docker package. `codexops` is not a member of the `docker` group and cannot access the daemon without sudo. `/run/docker.sock` is `root:docker` mode `0660`.

## Docker daemon configuration

- Data root: `/var/lib/docker`
- Backing mount: independent XFS with `ftype=1` and active `prjquota`
- Log driver: `local`
- Log rotation: `max-size=100m`, `max-file=5`
- Live restore: enabled
- Cgroup driver/version: systemd / v2
- Default runtime: `runc`
- NVIDIA named runtime: configured
- User namespace remapping: not enabled
- Docker TCP API: not enabled; no listeners on 2375/2376
- `codexops` docker-group membership: no

The configuration passed both `jq empty` and `dockerd --validate`. The original pre-Toolkit daemon config is backed up at `/srv/gpu-platform/platform/backups/daemon.json-before-nvidia-ctk-20260804-050357`.

## Engine tests and Docker Hub observation

A locally built immutable scratch test image ran successfully with `--network none`, read-only root, all capabilities dropped, and no-new-privileges. Image ID:

`sha256:333df8b4dc63df04f6c8bde25b5d1be0bddfc3c72656159108355fc41698ccc6`

The Docker Hub API and Registry endpoints repeatedly timed out using the system-provided DNS answers. Therefore the requested Docker Official Image `hello-world` could not be pulled and was not falsely reported as executed. DNS, proxy, TLS, firewall, and mirror configuration were not changed. Engine and image-pull functionality were independently demonstrated by the local test and the successful NVIDIA NGC pull below. The Docker Hub item remains part of the existing P0 network-access investigation.

## NVIDIA Container Toolkit

- Official guide: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>
- Stable release: `v1.19.1`
- Package version: `1.19.1-1`
- Git commit reported by `nvidia-ctk`: `09ceee5dde66ba9ce25c7cc69b1ebd5e6e3266fa`
- Official package source used for all four package URIs: <https://nvidia.github.io/libnvidia-container/stable/deb/amd64/>
- Repository signing-key fingerprint: `C95B 321B 61E8 8C18 09C4 F759 DDCA E044 F796 ECB0`
- Experimental repository: disabled
- CDI devices: four indices, four UUIDs, and `all`

Installed fixed versions:

- `nvidia-container-toolkit=1.19.1-1`
- `nvidia-container-toolkit-base=1.19.1-1`
- `libnvidia-container-tools=1.19.1-1`
- `libnvidia-container1=1.19.1-1`

## Pinned GPU test image and results

- Source tag used only to select the release: `nvcr.io/nvidia/cuda:13.2.0-base-ubuntu24.04`
- Runtime reference: `nvcr.io/nvidia/cuda@sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a`
- Platform: linux/amd64
- `--gpus all`: exactly four H100 GPUs and expected UUIDs
- `--gpus device=0`: exactly GPU 0, UUID `GPU-c8377945-df2c-5761-8798-66385611808b`
- Container network: none
- Privileged: not used
- Host network: not used
- Capabilities: all dropped
- GPU processes after tests: none

Raw GPU test: `/srv/gpu-platform/platform/reports/docker-gpu-test-20260804-050357.txt`

Status: `DOCKER PASSED`

Status: `DOCKER GPU PASSED`
