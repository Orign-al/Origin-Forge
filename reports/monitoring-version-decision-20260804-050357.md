# Monitoring version decision and asset preparation

- Run ID: 20260804-050357
- Prometheus: v3.13.2
- Prometheus official release: https://github.com/prometheus/prometheus/releases/tag/v3.13.2
- Prometheus image: quay.io/prometheus/prometheus@sha256:1147c92841726a6fef55fe6124491d6f85480f8de204f7d420304ca5bbd0a8f7
- Node Exporter: v1.12.1
- Node Exporter official release: https://github.com/prometheus/node_exporter/releases/tag/v1.12.1
- Node Exporter archive SHA-256: b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63
- Grafana OSS: v13.1.1
- Grafana official release: https://github.com/grafana/grafana/releases/tag/v13.1.1
- Grafana official archive SHA-256: 0c07116968aea49768af8babd3c3f162d19012655a1a220cd7a9d97efe91da6c
- Grafana binary type: ELF 64-bit LSB executable, x86-64, version 1 (SYSV), statically linked, Go BuildID=M2ZuCRn0xOG8Zh1nuqr6/K00CyfUVSPuq6dEN1bEb/dtLbMy9Wip6fv0UxhAmK/bQ8zEoiS_xr3Neruv1TI, BuildID[sha1]=0ea3bad75d74379c0ae03de78471d0e38e2d8c19, with debug_info, not stripped
- Grafana official Docker Hub image: unreachable because registry-1.docker.io timed out
- Grafana plan: locally build from the checksum-verified official archive using a separately pinned reachable base; do not use a mirror
- DCGM Exporter: 4.6.0-4.8.3
- DCGM Exporter official release: https://github.com/NVIDIA/dcgm-exporter/releases/tag/4.6.0-4.8.3
- DCGM Exporter image: nvcr.io/nvidia/k8s/dcgm-exporter@sha256:b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a
- DCGM runtime included by exporter: 4.6.0
- Host DCGM: 4.6.1
- NVIDIA documented container capability: SYS_ADMIN
- Privileged container required: no
- Services started: no
- Ports opened: no
- Node state: drained

Status: MONITORING ASSETS PREPARED
