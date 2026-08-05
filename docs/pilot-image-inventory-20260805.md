# H100 受控单机 Pilot 镜像清单

采集时间：`2026-08-05T17:54:34+08:00`。`LastTagTime` 来自 Docker image metadata，统一以 UTC 记录。导入/构建执行人为平台部署账号 `codexops`；没有记录或提交任何 Registry 凭据。

| Registry | Repository | Tag | Digest | LastTagTime / 导入记录 | 上传/导入用户 | 用途 |
|---|---|---|---|---|---|---|
| local | `h100-local/dev-container` | `ubuntu24.04-codexops-20260804` | `sha256:0003a26a1bfc1f4109440039e91bb9d28603f8ff1bc56946eed3bfd4cda75577` | `2026-08-04T15:58:08.180494126Z` | `codexops` | codexops 长期开发容器 |
| local | `h100-local/grafana-oss` | `13.1.1-1` | `sha256:3b2767e2f6a2ff25dcbde72350fab8aa1775b776a71982d9f4964fa3418c3138` | `2026-08-04T13:42:19.449320201Z` | `codexops` | Grafana |
| local | `h100-local-runtime-smoke` | `20260804-050357` | `sha256:333df8b4dc63df04f6c8bde25b5d1be0bddfc3c72656159108355fc41698ccc6` | `2026-08-04T06:50:59.119113870Z` | `codexops` | NVIDIA runtime smoke test |
| Public ECR | `public.ecr.aws/ubuntu/ubuntu` | digest-only | `sha256:019e8eb29a85e74d64925745884f2ec79aa27e3feab36353d24656f4d6b89467` | `2026-08-04T14:26:48.270515173Z` | `codexops` | 已缓存的固定 Ubuntu 24.04 基础镜像 |
| Quay | `quay.io/prometheus/prometheus` | digest-only | `sha256:1147c92841726a6fef55fe6124491d6f85480f8de204f7d420304ca5bbd0a8f7` | `2026-08-04T13:02:42.172918957Z` | `codexops` | Prometheus |
| NVIDIA NGC | `nvcr.io/nvidia/k8s/dcgm-exporter` | digest-only | `sha256:b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a` | `2026-08-04T13:02:54.620467254Z` | `codexops` | DCGM Exporter |
| NVIDIA NGC | `nvcr.io/nvidia/cuda` | `13.2.0-base-ubuntu24.04`（本地以 digest 引用） | `sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a` | `2026-08-04T07:08:26.899652411Z` | `codexops` | Slurm/Pyxis GPU 验收 |

Pilot 新增镜像必须来自已批准的 NGC、GHCR、Quay，或由管理员离线导入；必须把 registry、repository、tag、digest、导入时间和上传用户追加到本清单或对应运行报告。不得只记录 tag，不得依赖 Docker Hub 实时拉取。
