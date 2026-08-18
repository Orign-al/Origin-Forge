# Nginx deployment note

Portal-0/1 不部署额外 Nginx 监听。Next.js Web 的受控 rewrite 将浏览器同源
`/api/v1/*` 请求代理到 `127.0.0.1:18081`。Web 精确监听批准的私有 `tun0` 地址
`10.10.10.220:18080`；API 不暴露到该网络。SSH Tunnel 的 `127.0.0.1:18080` 入口仍可
作为回退。

未来若引入 HTTPS reverse proxy，必须单独审批证书、监听地址、可信代理头、Cookie
`Secure=true` 和 CSP；不得直接复用当前私有 HTTP 配置开放公网。
