# Portal 前端设计系统

## 目标与边界

Portal 使用面向基础设施运维的紧凑中文界面。视觉层服务于状态辨识和审计，不使用渐变、
玻璃拟态、霓虹、超大圆角、远程字体、第三方 CDN、分析脚本或装饰性动态图表。页面顶部
固定显示 `LOCAL SSH TUNNEL MODE`，不得暗示当前 HTTP 入口是公开 HTTPS 服务。

## 视觉令牌

令牌定义于 `apps/web/app/globals.css`：

- 背景使用中性灰 `--bg`，内容面板使用白色 `--panel`；正文使用深灰 `--ink`。
- 主操作色为克制的蓝色 `--accent`；成功、警告、危险色只表达状态。
- 圆角统一为 4–8px，默认 `--radius` 为 6px；阴影仅为一像素弱阴影。
- 字体为系统字体栈，基础字号 14px；不下载字体。
- 间距遵循 4px/8px 倍数，表格正文为 13px，状态 Badge 为 12px。

状态不能只由颜色表达。Badge 同时显示 `OK`、`UNKNOWN`、`DRAIN`、`FAILED` 等文字；
危险按钮使用红色边框和文本，但页面不使用大面积红色。

## 页面骨架

- 左侧 212px 导航：总览、用户、容器、Slurm、作业、GPU、存储、镜像、监控、审批、
  审计和系统。
- 顶栏：环境、Tunnel 模式、全局模块搜索、告警数、当前登录人及退出。
- 主区：面包屑、标题、说明、主要操作和数据区。
- 首页只放四个紧凑核心指标，其余使用状态表、作业表和告警表。

布局必须在 1366×768 和 1920×1080 下可用。宽表通过容器横向滚动，不得扩张整个页面。

## 数据状态

每个数据模块必须区分：

- `Loading`：正在读取，不展示假数据。
- `Empty`：数据源成功但记录为空。
- `Error`：请求失败，明确说明没有用 0 伪装正常。
- `Unauthorized`：后端返回 403。
- `Stale`：后台刷新中或展示最近一次成功结果。
- `Partial failure` / `UNKNOWN`：保留失败数据源身份和错误码。

不得把网络异常、Worker 异常或解析异常折叠成健康状态或数值 0。

## 组件规则

- 表格优先于大卡片；核心数值卡只用于首页 4–6 个指标。
- 表单必须有可见标签或 `aria-label`，错误与帮助文本需和控件关联。
- 所有按钮和链接必须支持键盘操作，并保留清晰的 `:focus-visible` 轮廓。
- 危险操作必须显示对象名确认、风险说明和重新认证要求。
- 容器页面不得提供 arbitrary argv、mount、capability、privileged、host network 或 GPU
  开关。
- GPU 页面不提供 MIG、GPU reset 或 Compute Mode 修改入口。
- Slurm 页面持续显示 Portal-0/1 禁止 RESUME。

## 测试门槛

提交前运行 `pnpm lint`、`pnpm typecheck`、`pnpm test`、`pnpm build` 和
`pnpm test:e2e`。Playwright 的 fixture 必须与真实 Worker 稳定 schema 一致；不能用仅在
mock 中存在的字段让生产页面测试假通过。视觉扫描同时拒绝渐变、玻璃拟态、外部字体、
“AI 助手”和虚构数据。
