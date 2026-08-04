# Phase P0-B Docker Hub 正式出口方案

状态：**尚未选择或部署任何方案。**

当前系统 DNS 为 `114.114.114.114`、`223.5.5.5`。10 轮当前路径查询中，正确的 `production.cloudfront.docker.com` 得到 CloudFront CNAME 和 AWS 地址；Registry、Auth、docker.io、hub.docker.com 则得到可疑且随测试变化的地址。Registry/Auth 的 IPv4 TCP/TLS 超时，而 CloudFront TCP 与 TLS 1.3 可达。按 Phase P0-B 证据标准，管理员尚未提供 Resolver B，因此只能记录为高度可疑，不能最终打印 `DNS PATH CONFIRMED AS ROOT CAUSE`。

不得同时部署多个方案。先完成可信 Resolver B 的非持久对比，再由管理员选择。

## 方案 A：公司批准的干净 DNS

适用条件：使用 Resolver B 后，Registry/Auth/CloudFront 的 `curl --resolve` 均能建立经验证的 TLS，且公司允许该服务器长期使用该解析服务。

管理员需提供：

- DNS IP 和所有者；
- 是否为公司内部服务；
- 是否支持 TCP 53/DoT；如为 DoT，提供可验证证书名称；
- SLA；
- 内部域名、search domain 和 split DNS 要求。

正式配置前需备份 netplan、resolved 配置和 `/etc/resolv.conf` 链接状态，先运行 `netplan generate`，输出完整 diff，并等待原文批准：`允许修改 H100 DNS 配置`。管理流量依赖故障 Mellanox 时，必须有 BMC/KVM/现场回滚保障。不得把 CDN IP写入 `/etc/hosts`。

## 方案 B：公司批准的 HTTPS 代理

适用条件：可信 DNS 仍不能解决 Registry/Auth 出口，且公司已有允许 Registry/Auth/CloudFront 的正式 HTTP CONNECT 代理。

管理员需提供：

- scheme、host、port；
- 是否认证；
- NO_PROXY 范围；
- 凭据保存和轮换要求。

凭据不得出现在聊天、报告、Git 或 daemon.json 的公开模板中。正式变更需要安全合并 Docker 既有 JSON，保留 data-root、NVIDIA runtime、日志和 live-restore；通过 `jq empty` 与 `dockerd --validate`，输出脱敏 diff，并等待原文批准：`允许配置 Docker daemon HTTPS 代理并重启 Docker`。重启前必须确认重要容器任务为空。

## 方案 C：内部 Harbor/Registry pull-through cache

适用条件：公司不允许 H100 直接可靠访问 Docker Hub，且已有或准备建设长期受管的内部制品服务。

要求包括 TLS、受信 CA、审计、扫描、保留策略、容量、上游凭据安全、rate limit 与高可用。缓存服务必须位于能够正常访问 Registry、Auth、CloudFront 的网络。本阶段若没有现成服务，只做设计，不在 H100 上临时部署，也不配置 insecure registry 或第三方 mirror。

管理员需提供内部 Registry FQDN、证书/CA 管理方式、认证方法、proxy-cache project/命名规则、HA/SLA 和运维责任人，之后再制定 H100 侧最小变更。

## 当前决策顺序

1. 现场按单变量完成 Mellanox 整改；
2. 提供批准的 Resolver B；
3. 用 `dig` UDP/TCP 和 `curl --resolve` 区分 DNS 与 ACL/SNI；
4. 根据结果只选择 A、B、C 中一个；
5. 输出 diff 并取得相应明确批准；
6. 按 DNS → TCP → TLS → Registry 401 → Auth → manifest → blob/CDN → 固定 digest pull/run 完成验收。

禁止 `/etc/hosts` 固定 IP、未知公共 DNS/DoH、个人 VPN/代理、`--insecure`、insecure registry、未知 mirror、盲改 MTU或全局禁用 IPv6。
