# Nginx deployment note

Portal-0/1 不部署额外 Nginx 监听。Next.js Web 的受控 rewrite 将浏览器同源
`/api/v1/*` 请求代理到 `127.0.0.1:18081`，浏览器只访问 `127.0.0.1:18080`。

未来若引入 HTTPS reverse proxy，必须单独审批证书、监听地址、可信代理头、Cookie
`Secure=true` 和 CSP；不得直接复用当前 localhost HTTP 配置开放公网。
