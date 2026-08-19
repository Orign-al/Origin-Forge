# Nginx deployment note

Portal-0/1 不部署额外 Nginx 监听。Next.js Web 的受控 rewrite 将浏览器同源
`/api/v1/*` 请求代理到 `127.0.0.1:18081`。Next.js Web 负责 IPv4 用户入口，systemd
网络策略只允许批准的 VPN、EasyTier 和管理 LAN 网段；API 不暴露到这些网络。
用户正式访问地址是 `http://20.10.10.3:18080/`，SSH Tunnel 的 `127.0.0.1:18080`
入口仍可作为回退。旧 EasyTier 地址的兼容入口由精确绑定
`10.10.10.2:80` 的 systemd socket proxy 提供，不增加 Nginx，也不把 80 绑定到其他
接口；`10.10.10.2:18080` 仍由同一个 Web listener 直接提供。

未来若引入 HTTPS reverse proxy，必须单独审批证书、监听地址、可信代理头、Cookie
`Secure=true` 和 CSP；不得直接复用当前私有 HTTP 配置开放公网。
