# 产品化模块状态

本文档记录当前 worktree 的产品化状态。这里的“已完成”只表示本地 MVP 子能力可用，不代表生产上线；当前没有任何产品化模块已经完整上线生产。

状态口径：

- 已完成（本地 MVP 级）：本地代码和测试覆盖了清晰子能力，可在开发环境独立使用。
- 部分完成：已有规则、schema、接口或本地实现，但还缺生产外部服务、全链路接入、风控/审计闭环或运营流程。
- 未完成：尚未实现，或只在文档中规划。

## 产品模块状态总表

| 产品模块 | 当前状态 | 已有本地能力 | 生产化仍缺 |
|---|---|---|---|
| 数据库 | 部分完成 | SQLite MVP schema，覆盖生成任务、生成图、导出包、积分、代理、佣金、邀请、风控、资产访问 | PostgreSQL 迁移、迁移脚本、备份、索引复核、生产数据权限 |
| 对象存储 | 部分完成 | 本地对象存储抽象、签名访问元数据、生产 readiness 评估；已接入腾讯 COS runtime backend，可通过 `OBJECT_STORAGE_PROVIDER=cos` 走私有桶逻辑对象 key 读写 | 菜单、原图、生成图、导出包、AI 资产全量迁到私有 COS，并在 Render 配置 `OBJECT_STORAGE_PROVIDER=cos`、bucket、region、SecretId/SecretKey、签名 secret；OSS/R2/S3 adapter 仍未接入 |
| 积分/支付 | 部分完成 | 本地积分账本、扣费/退款、幂等订单、fake payment provider、回调验签抽象、支付 provider readiness scaffold、生产禁用 fake provider 防误用保护；支付宝电脑网站支付已支持 RSA2 签名下单链接、异步通知验签、订单 paid 状态流转和积分入账；后台财务人工支付对账可推进 paid/refunded/closed/failed 并写账本/审计；微信 provider 仍 fail-closed；后台人工积分调整已要求财务角色并写账本/审计 | 支付宝真实商户联调、退款回调/API、补单和异常订单运营流程；微信支付 adapter 仍未接入 |
| 账号/门店 | 部分完成 | 本地手机号 OTP/session/store schema、local/mock 短信 provider、webhook 短信 provider、生产无 provider 503 guard | 正式短信服务商账号/签名模板/回执、微信登录、正式会话、角色权限、门店归属全链路 |
| 生成队列 | 部分完成 | 旧 monolith 已有内存队列、并发 worker、job id、状态/进度、取消、入队限流、stale/timeout；新 SaaS 骨架已新增 `api-server/`、`worker/`、`shared/`，`POST /generate` 写 Redis 队列，`GET /status/<task_id>` 查 Redis 状态，独立 Worker 消费并最多 retry 2 次 | 把旧 `/api/generation-jobs`、样图、背景图和正式出图全部迁到 Redis Worker；接入真实生产 Redis、任务恢复、失败重试策略、队列监控接口/告警、跨进程定时 sweep |
| AI 图库沉淀 | 部分完成 | JSONL AI asset repository，支持去重、筛选、复用匹配、状态标记；生成成功后写 manifest；本地质量评估可拒绝低质资产；后台已有 AI 资产 approve/reject/disable 状态 API 和审计记录；disable 已限制为 admin/super_admin/owner 角色 | 品类背景图、免费样图、正式菜品图全量沉淀到对象存储；完整人工审核队列、质量分层、复用策略生产化 |
| 防盗图/签名 URL | 部分完成 | HMAC 签名 token、过期校验、用途/变体策略、下载守卫、一次性 token 消费键/replay 判定、可选强制签名下载；导出下载和对象读取会写资产访问审计 | 私有桶全链路、Redis/DB 原子消费记录、低清/水印预览、Redis 限流、泄露追踪 |
| 防刷风控 | 部分完成 | 注册、短信、邀请、代理佣金、下载频率的规则判断；风控事件 allow/review/deny 可写审计，deny 处置已限制为 risk/security/admin/owner 等角色 | 设备指纹、验证码、IP/账号滑动窗口、人工审核队列、封禁/解封后台 |
| 代理/邀请 | 部分完成 | 一级直推规则、代理档案/客户归属/邀请关系服务，支付成功后自动生成代理 20% 待结算佣金，支持 T+7 释放、结算批次、标记已支付，退款后可重算/取消未结算佣金并追回首充返利积分，C 端注册双方 50 积分和直接邀请首充 10% 积分已接入本地账本；代理提现申请本地 MVP 服务已支持最低金额、active agent 校验、保守余额、状态流转、后台审批审计和 paid 财务角色限制；佣金结算 paid 也已限制为财务角色 | 代理协议、实名/主体认证、真实财务打款、归属争议处理、完整后台操作、已打款后财务追索和月度自动结算 |
| 管理后台 | 部分完成 | 只读后台、运营概览、最近任务、佣金、风控、资产访问、AI 资产库概览；后台 lists 明细 API 已覆盖任务、资产访问、佣金结算、提现、订单等明细；前端运营台已接入 lists；部分后台操作审计 helper、AI 资产状态操作 API、提现状态审批审计 API；提现 approved/rejected/canceled 与 paid 已有角色分级；佣金结算 paid 已有财务角色分级；AI 资产审核与禁用已有角色分级；风控 deny 处置已有角色分级；后台人工积分调整已有财务角色分级和审计；`/api/ops/deployment-config` 已输出脱敏生产环境变量清单；live/runtime 已禁用 local demo admin fallback | 用户/门店/订单/积分/代理/佣金/AI 资产审核/风控处置完整 CRUD，完整运营权限分级 |
| 审计 | 部分完成 | 风控审计、后台操作审计 schema/helper；导出下载和对象读取的允许/拒绝记录已落入 `asset_access_logs`；asset-access 明细支持 `status=denied/allowed` 等筛选 | 生产日志聚合、审计查询后台、长期留存、权限分级、异常告警 |
| 运营指标 | 部分完成 | 本地 dashboard 聚合：任务、积分、支付、佣金、邀请、风控、资产访问 | 生产监控、模型成本、生成成功率、渠道转化、收入/毛利报表、告警和 BI |

## 已完成的本地 MVP 子能力

这些能力已经在当前 worktree 中落地，但只算本地 MVP 子能力：

| 子能力 | 状态 | 主要文件 |
|---|---|---|
| AI 资产仓储 | 已完成（本地 MVP 级） | `ai_asset_repository.py` |
| 下载签名核心 | 已完成（本地 MVP 级） | `asset_security.py`, `download_guard.py` |
| 一次性 token replay 规则 | 已完成（本地 MVP 级） | `asset_security.asset_token_consumption_key`, `download_guard.authorize_download` |
| 资产访问审计落库 | 已完成（本地 MVP 级） | `/download`, `/objects/*`, `admin_actions.record_asset_access`, `tests/test_download_route.py`, `tests/test_product_api_integration.py` |
| 内存生成队列 | 已完成（本地 MVP 级） | `generation_queue.py`, `job_rules.py`, `queue.snapshot()`, `/api/generation-jobs/*` timeout/stale payload |
| 队列拥塞错误映射 | 已完成（本地 MVP 级） | `/api/generation-jobs`, `generation_queue_full` 429, `generation_queue_unavailable` 503 |
| SaaS Redis API/Worker 骨架 | 已完成（本地 MVP 级） | `api-server/app.py`, `worker/worker.py`, `shared/redis_queue.py`, `tests/test_saas_runtime.py`, `render.yaml`, `Procfile`, `Dockerfile` |
| 本地对象存储抽象、COS backend 与 readiness 评估 | 已完成（本地 MVP 级） | `object_storage_service.py`, `storage_db.py`, `tests/test_object_storage_service.py` |
| 支付订单规则骨架 | 已完成（本地 MVP 级） | `payment_rules.py`, `payment_service.py` |
| fake 支付防误用、provider readiness 与真实 provider fail-closed | 已完成（本地 MVP 级） | `payment_service.fake_payment_provider_enabled`, `payment_service.assess_payment_provider_readiness`, `payment_service.ensure_payment_checkout_available`, `/api/payments/orders`, `/api/payments/fake-callback` |
| 支付宝电脑网站支付下单与通知验签 | 已完成（本地 MVP 级） | `payment_service.create_payment_checkout`, `/api/payments/alipay/notify`, `tests/test_payment_service.py`, `tests/test_product_api_integration.py` |
| 财务人工支付对账 | 已完成（本地 MVP 级） | `payment_service.reconcile_payment_event`, `/api/admin/actions/payments/reconcile`, `admin_audit_logs`, `tests/test_payment_service.py`, `tests/test_product_api_integration.py` |
| 后台人工积分调整 | 已完成（本地 MVP 级） | `/api/admin/actions/points-adjustments`, `admin_audit_logs`, `billing.ledger` |
| 生产部署配置清单 | 已完成（本地 MVP 级） | `/api/ops/deployment-config`, `tests/test_product_api_integration.py` |
| 代理/邀请规则函数 | 已完成（本地 MVP 级） | `growth_rules.py`, `auth_rules.py` |
| 代理/邀请服务闭环 | 已完成（本地 MVP 级） | `growth_service.py`, `/api/growth/*`, `tests/test_growth_service.py`, `tests/test_growth_api_integration.py` |
| 佣金结算批次 | 已完成（本地 MVP 级） | `commission_settlement_service.py`, `/api/admin/actions/commission-settlements*`, `admin_commission_settlement_status_authorized`, `tests/test_commission_settlement_service.py`, `tests/test_commission_settlement_api.py` |
| 代理提现申请服务 | 已完成（本地 MVP 级） | `agent_withdrawal_requests`, `withdrawal_service.py`, `/api/growth/withdrawals*`, `/api/admin/actions/withdrawals/<withdrawal_id>/status`, `admin_withdrawal_status_authorized`, `tests/test_withdrawal_service.py`, `tests/test_product_api_integration.py` |
| 短信 provider 抽象 | 已完成（本地 MVP 级） | `sms_service.py`, `/api/auth/request-otp`, `tests/test_sms_service.py`, `tests/test_product_api_integration.py` |
| 退款增长追回 | 已完成（本地 MVP 级） | `growth_service.record_payment_refund`, `tests/test_growth_service.py`, `tests/test_growth_api_integration.py` |
| 风控规则函数 | 已完成（本地 MVP 级） | `risk_rules.py` |
| 后台数据聚合 | 已完成（本地 MVP 级） | `admin_data.py` |
| 后台 lists 明细 API | 已完成（本地 MVP 级） | `/api/admin/lists/*`, `admin_data.py`, `static/admin.js` |
| 后台操作审计 helper | 已完成（本地 MVP 级） | `admin_actions.py`, 佣金状态、提现状态和 AI 资产状态相关测试 |
| AI 资产审核动作 API | 已完成（本地 MVP 级） | `/api/admin/actions/ai-assets/<asset_id>/status`, `admin_ai_asset_status_authorized`, `ai_asset_repository.py`, `admin_actions.py` |
| asset-access 状态筛选 | 已完成（本地 MVP 级） | `/api/admin/lists/asset-access?status=denied`, `admin_data.py` |
| 平台导出规则 | 已完成（本地 MVP 级） | `platform_rules.py`, `image_pipeline.py` |
| 前端/后台契约测试保护 | 已完成（本地 MVP 级） | `tests/test_customer_ui_contract.py`, `tests/test_product_api_integration.py`, `tests/test_security_regressions.py` |

## 明确未完成或未启用

- 未上线生产：当前文档和本地代码都不能表述为已经商用上线。
- 未完成真实登录：已有短信 provider 抽象和 webhook 接入，但还缺正式短信服务商账号、签名模板、发送回执处理、微信登录、正式会话和权限接入。
- 未完成真实支付：已有 provider readiness scaffold，会在生产或关闭 `ENABLE_LOCAL_DEMO_BILLING` 时拒绝 fake/no provider；支付宝电脑网站支付本地 MVP 已支持 RSA2 签名下单链接和异步通知验签入账，财务人工支付对账已可本地使用；但还未做真实商户联调、退款 API、补单和异常订单运营流程；微信支付仍未接入 adapter，继续 fail-closed。
- 未完成生产对象存储全链路：已有配置 readiness 评估和 COS runtime backend，生产或关闭本地 demo 时会要求私有远程 provider 和签名 secret；但 Render 尚未切换 `OBJECT_STORAGE_PROVIDER=cos`，所有客户资产和 AI 资产还未统一进入私有桶并强制签名访问，OSS/R2/S3 SDK adapter 仍未接入。
- 未完成生产一次性 token 消费存储：已有本地 replay 判定规则，但还未接 Redis/DB 原子写入和跨进程消费记录。
- 未完成生产队列迁移：SaaS Redis API/Worker 骨架已完成，但旧 monolith 的 `/api/generation-jobs`、背景图、样图和正式出图路径仍未全部迁到 Redis Worker；还缺生产 Redis 实例、任务恢复、监控告警和跨进程定时 sweep。
- 未完成生产代理提现/佣金打款：本地提现申请、保守余额校验、状态流转、后台审批审计、提现 paid 财务角色限制和佣金结算 paid 财务角色限制已完成；实名/主体认证、税务信息、真实财务打款流水、财务复核流程和完整后台权限矩阵仍需运营和财务流程。
- 未完成后台完整 CRUD：当前以只读、明细列表和局部操作为主，不能替代完整客服/运营后台。
- 未完成 AI 质量审核生产闭环：已有本地质量评估、AI 资产状态 API、审核/禁用角色分级，但错品图、低质图、侵权/水印图还没有完整人工审核队列、抽检策略和处置流程。
- 未启用多级分销：代理和 C 端邀请默认只做一级直推；多级分销必须法务确认后另开。

## 图库与合规口径

- 图库策略：新生成的品类背景图、免费样图、正式菜品图都沉淀到服务器目录或对象存储；AI asset manifest 打标签，供未来复用。
- 前台口径：不宣传“真实图库”，只展示 AI 生成、样图预览、历史生成资产复用等产品能力。
- 代理策略：默认一级直推；代理统一按直接订单实付净额 20% 现金返佣。
- C 端邀请：邀请人注册奖励 50 积分，被邀请人注册奖励 50 积分；仅直接邀请首充返 10% 积分，不返现金、不提现。
- 合规边界：多级分销暂不启用；如需启用，必须先完成法务确认并另开方案。

## 下一轮 Worker

| Worker | 目标 |
|---|---|
| Auth | 手机号登录、短信验证码接口、本地 mock provider、账号/角色表 |
| Billing | 支付订单模型、回调验签抽象、本地 fake pay provider、provider readiness scaffold、生产 fake 防误用保护、积分账本闭环 |
| Storage | 对象存储抽象统一替换本地文件访问，下载/预览全链路鉴权 |
| Worker Runtime | Redis/RQ/Celery 适配层，把内存队列替换为生产队列 |
| Admin CRUD | 用户/门店/订单/积分/代理/佣金/资产审核/风控日志的完整后台操作页和权限 |
| QA | Playwright 后台/客户前台端到端流程测试 |
