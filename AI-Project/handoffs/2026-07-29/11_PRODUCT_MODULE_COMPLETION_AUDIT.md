# 产品功能完成度审计

- 审计对象：当前 Worktree 源码与测试
- 审计日期：2026-07-30
- 报告目标：为“实现网站全部功能”的后续执行提供可落地的缺口清单
- 写入范围：仅本文件
- 明确未执行：代码修改、提交、推送、部署、数据库迁移、生产配置变更

## 0. 审计口径与结论

### 0.1 状态定义

- `Implemented`：当前源码存在完整的核心路径，且测试覆盖了主要合同。它仍不自动等于生产验证。
- `Partial`：存在实现，但有未接入正式路径、关键边界缺失、测试只覆盖模拟环境或生产阻塞项。
- `Missing`：正式运行路径不存在，或者现有配置会直接破坏目标能力。

### 0.2 总结

| # | 模块 | 状态 | 是否有生产 P0 blocker |
|---|---|---|---|
| 1 | 代理机制 | Partial | 是 |
| 2 | C 端邀请、首充返积分、防恶意注册 | Partial | 是 |
| 3 | 积分充值、扣费、退款、对账、支付回调 | Partial | 是 |
| 4 | 防盗图、防破解图库 | Partial | 是 |
| 5 | 数据后台与权限 | Partial | 是 |
| 6 | 图片缓存与 AI 资产复用入库 | Partial | 否，但会阻塞规模化与多实例 |
| 7 | 精细改图、版本、导出 | Partial | 是，若作为正式付费能力上线 |
| 8 | 并发、Redis、Worker、PostgreSQL/outbox、服务拆分 | Partial | 是 |
| 9 | 40 品类、近似菜名、套餐识别、真实准确率 | Partial | 是 |
| 10 | Render blueprint 与客户网站保留 | Missing | 是 |

当前没有任何一个被审计模块可以仅凭现有 `pytest` 结果宣称“已经完成生产验证”。代码中已有不少可复用基础，但支付、后台鉴权、资产访问、正式持久化链路、Render 服务结构和真实图片正确率仍存在明确阻塞。

### 0.3 Worktree 基线

审计开始前 Worktree 已有大量未提交修改和未跟踪文件。本次没有回退、覆盖或整理这些既有改动。测试在 `/tmp/waimai-product-audit.z1QkHj` 的隔离副本运行，并禁用了 Python bytecode 和 pytest cache，避免污染当前 Worktree。

报告写入后的第二次 `git status` 比起始快照额外出现了 `data/library/uploaded_1785403977_082760000/style-upload/宫保鸡丁.jpg` 和 `tests/test_postgres_menu_app_integration.py`。本次审计没有写入这两个路径，按并行任务改动保留且未触碰。后者不在冻结的隔离测试副本中，因此本报告的 `663 passed, 1 failed` 不包含该并行新增测试。

---

## 1. 代理机制

**状态：Partial**

### 当前实现证据

- `growth_rules.py`
  - `agent_commission()`：按支付金额计算佣金。
  - `validate_agent_commission_depth()`：当前明确限制为一层。
  - `AGENT_FIRST_ORDER_RATE_BPS = 2000`。
  - `AGENT_REPEAT_ORDER_RATE_BPS = 2000`。
  - `MAX_AGENT_RELATION_DEPTH = 1`。
- `growth_service.py`
  - `create_agent_profile()`：创建代理资料，默认可直接成为 active。
  - `bind_agent_customer()`：只建立直接代理与客户关系。
  - `record_payment_growth()`、`_create_agent_commission()`：生成直接代理佣金。
  - 退款时有对应 clawback 路径。
- `settlement_service.py`
  - 佣金结算单创建、释放、状态更新和查询。
- `withdrawal_service.py`
  - 只允许从已支付结算余额发起提现，存在最低提现金额和提现状态。
- `app.py`
  - 普通登录用户可自行创建 active 代理资料。
  - 代理绑定、结算状态、提现状态等部分管理操作存在 admin/finance 角色检查和审计记录。
- 测试证据：
  - `tests/test_growth_rules.py`
  - `tests/test_growth_service.py`
  - `tests/test_growth_api.py`
  - `tests/test_settlement_service.py`
  - `tests/test_settlement_api.py`
  - `tests/test_withdrawal_service.py`

### 规则冲突登记

| 规则项 | `decisions.md` 当前规则 | 当前代码 | 用户后续最新表达 | 结论 |
|---|---|---|---|---|
| 代理层级 | 二级代理目标 | `MAX_AGENT_RELATION_DEPTH = 1`，只算直接关系 | 希望做二级 | 明确冲突 |
| 首单佣金 | 20% | 20% | 后续提出统一 20% | 代码数值一致 |
| 复购佣金 | 10% | 20% | 后续提出不分首单/复购，统一 20% | `decisions.md` 与代码/后续表达冲突 |
| 二级分配 | 未定义清晰拆分 | 不支持二级 | “二级、统一 20%” | “总计 20%”还是“每层 20%”未明确，不能擅自实现 |
| 提现 | 有代理提现方向 | 本地账本和提现状态已存在 | 用户曾询问代理是否能提现 | 产品、税务、KYC 和真实出款规则仍未冻结 |

本审计只记录冲突，不选择规则。尤其“二级、统一 20%”缺少上下级分配公式，直接编码可能造成超额返佣及合规风险。

### 已实现部分

- 直接代理关系、直接佣金、退款冲销、结算状态和提现申请的数据与服务基础。
- 部分管理动作有角色校验与审计事件。
- 核心计算存在单元测试。

### 真实上线 blocker

1. 决策文档、当前代码和用户后续规则不一致。
2. 没有二级关系图、循环关系防护、二级佣金拆分和对应退款分摊。
3. 普通用户可自助创建 active 代理，缺少审核、合同、实名、税务和风控门槛。
4. 提现只是应用内状态机，没有真实出款渠道、收款账户校验、KYC、税务处理和财务复核闭环。
5. 当前主要依赖本地 SQLite，无法证明多实例结算并发、幂等和账务一致性。
6. 中国境内多层分销是否合规不能由代码测试判定，需要律师基于实际招募、计酬、商品和宣传方式审查。

### 最小后续任务

1. 先冻结一份法律审核后的代理规则：层级、总佣金上限、每层比例、归属期、退款冲销、提现条件。
2. 将冻结规则写成版本化纯函数和合同测试，再扩展关系表与结算表。
3. 增加代理申请审核、实名/KYC、合同版本、税务资料、冻结/解冻和人工复核。
4. 在 PostgreSQL 事务中完成佣金记账、结算和 outbox，并接真实出款沙箱。

### 外部验证要求

- 必须：法律合规、税务、KYC、真实出款服务、财务对账。
- `pytest` 通过仍不能证明：二级分销合法、真实出款成功、税务正确或并发结算无重复。

---

## 2. C 端邀请积分、首充返积分、防恶意注册

**状态：Partial**

### 当前实现证据

- `growth_rules.py`
  - 邀请注册当前为邀请人 50 积分、被邀请人 50 积分。
  - 首次充值奖励为直接邀请人获得充值积分的 10%。
  - 普通后续充值不再返该 10%。
  - 存在每设备和每 IP 的注册数量阈值。
- `growth_service.py`
  - 注册邀请奖励、首次充值奖励、幂等事件和退款冲销路径。
- `auth_service.py`
  - `request_otp()`：随机验证码、哈希保存、TTL 和发送频率检查。
  - `verify_otp()`：一次性消费、过期和尝试次数检查，并签发哈希会话 token。
  - `build_registration_context()`：形成注册风控上下文。
- `risk_rules.py`
  - 独立的注册风险规则计算函数。
- `sms_service.py`
  - 支持 disabled、local mock 和 generic webhook；没有已验证的腾讯云短信正式适配。
- 测试证据：
  - `tests/test_auth_service.py`
  - `tests/test_auth_api.py`
  - `tests/test_risk_rules.py`
  - `tests/test_sms_service.py`
  - `tests/test_growth_rules.py`
  - `tests/test_growth_service.py`

### 规则冲突登记

- `decisions.md` 仍写邀请人 100 积分、被邀请人 20 积分。
- 当前代码是 50/50。
- 用户后续曾提出“注册赠送 50 积分”，但没有再次明确邀请人与被邀请人的最终拆分。
- 用户后续明确 C 端只在首充获得 10% 积分，不是每单返；当前首充逻辑与此方向一致。
- 在规则冻结前，不应把 50/50 视为已确认产品规则。

### 已实现部分

- 手机 OTP 的基本请求、校验、过期、尝试次数和会话机制。
- 直接邀请关系、注册奖励、首充奖励及部分幂等/退款逻辑。
- 设备/IP 数量阈值的规则函数。

### 真实上线 blocker

1. 正式邀请注册积分拆分未冻结。
2. `build_registration_context()` 当前直接将 `human_verified=True`、`risk_blocked=False`，手机号 OTP 被等同于真人验证，没有 CAPTCHA、活体或可信设备证明。
3. 设备标识主要来自 User-Agent，容易伪造，不能作为稳定设备指纹。
4. 请求 IP 可直接受 `X-Forwarded-For` 影响，缺少可信代理边界。
5. SQLite 计数器无法证明多 Web 实例下限流原子性；没有 Redis 分布式限流。
6. 没有正式短信渠道送达、频控、黑名单、号码风险、接码平台识别和人工风控处置闭环。
7. 邀请奖励记账与产品数据库事件跨数据库，不是一个事务，失败时可能出现关系已建立但积分未入账或反向情况。

### 最小后续任务

1. 冻结邀请人/被邀请人注册奖励和首充口径，并补版本化规则测试。
2. 接入真实短信沙箱、CAPTCHA/真人校验和可信代理 IP 解析。
3. 使用 Redis 原子限流和更可靠的设备标识，增加手机号、设备、IP、支付账户关联风控。
4. 将邀请事件与积分账本接入 PostgreSQL 事务/outbox，提供补偿和人工复核队列。

### 外部验证要求

- 必须：真实短信供应商、CAPTCHA/设备风控服务、真实手机号和攻击样本。
- `pytest` 通过仍不能证明：真人注册、短信可达、接码平台可防、跨实例限流有效或邀请作弊率可接受。

---

## 3. 积分充值、扣费、退款、对账、支付回调

**状态：Partial**

### 当前实现证据

- `billing.py`
  - SQLite 钱包、积分流水、充值订单和退款分配。
  - `BEGIN IMMEDIATE`、幂等键和非负余额保护。
  - 当前价格：普通出图 10、精修出图 20、水印移除 50、额外平台 100。
  - 当前充值套餐：49 元/500 积分、99 元/1040 积分、299 元/3190 积分。
  - 支持部分退款和累计退款分配。
- `payment_service.py`
  - 支付订单、支付事件、回调签名/状态/金额校验和幂等。
  - fake 本地支付和支付宝网页支付基础。
  - 微信支付明确未实现。
  - `_record_payment_event()` 在产品数据库中提交支付事件和订单状态。
- `app.py`
  - 支付下单、fake/支付宝回调、积分扣费、退款和财务对账接口。
  - `apply_payment_callback_effects()` 在支付事件提交后再操作独立积分数据库。
- 测试证据：
  - `tests/test_billing.py`
  - `tests/test_payment_service.py`
  - `tests/test_payment_api.py`
  - `tests/test_reconciliation_service.py`
  - `tests/test_reconciliation_api.py`

### 已实现部分

- 本地钱包、流水、扣费、退款和支付事件幂等基础。
- 服务端有标准/精修出图价格常量，不完全依赖前端显示。
- 支付回调具备基础签名与金额检查。

### 真实上线 blocker

1. **支付订单创建接口缺少登录约束，并接受请求方提交的 `userId`。**
2. **非现金套餐路径接受请求方独立提交 `amountCents` 和 `points`，没有绑定服务端套餐目录。**
3. 隔离探针实际得到：

   ```text
   HTTP 200
   {"userId":"victim-user","amountCents":1,"points":999999,"status":"pending"}
   ```

   这意味着攻击者可以构造 1 分钱对应 999999 积分的待支付订单；若网关允许该金额并完成回调，服务端会按订单积分值入账。
4. 支付事件先在产品数据库提交，积分再写入独立 billing SQLite。若中间失败，重复回调因支付事件已幂等消费而可能不再补积分，只能依赖人工对账。
5. 扣费、任务创建、Redis 入队和失败退款不是一个耐久事务，崩溃窗口可能造成扣费无任务、任务无扣费或重复补偿。
6. 没有生产级微信支付、真实支付宝证书轮换、退款网关、拒付/争议、日切账单下载和自动差异处理证据。
7. SQLite 无法证明多实例资金账本的串行化与灾难恢复。

### 最小后续任务

1. 支付下单强制使用认证主体，删除客户端 `userId` 信任。
2. 客户端只提交服务端套餐 ID；金额和积分全部从版本化服务端目录推导并签入订单。
3. 将支付事件、积分账本、增长奖励和 outbox 放入同一个 PostgreSQL 事务，设计可重复执行的补账任务。
4. 对接真实网关沙箱：支付、重复回调、乱序回调、退款、部分退款、拒付和日账单对账。

### 外部验证要求

- 必须：支付宝/微信支付沙箱或测试商户、真实回调入口、财务账单和财务人员验收。
- `pytest` 通过仍不能证明：真实资金到账、回调公网可达、退款成功、拒付处理正确或账实一致。

---

## 4. 防盗图与防破解图库

**状态：Partial**

### 当前实现证据

- `object_storage_service.py`
  - 本地/COS 存储抽象。
  - staging/production readiness 要求远程私有存储。
  - `create_signed_access()` 生成带用户、用途、variant、过期时间和 nonce 的签名访问参数。
- `asset_security.py`
  - HMAC 签名、预览 TTL 和一次性下载策略。
- `download_guard.py`
  - 可校验 owner/order/job/purpose/variant，并在调用方提供消费记录集合时防重放。
- `app.py`
  - `/api/objects/sign`、`/objects/<key>`、`/download/<name>`。
  - 正式生成任务与精细改图任务的资产接口具备 owner/manifest 校验基础。
  - 存在可选水印和导出设置。
- 测试证据：
  - `tests/test_asset_security.py`
  - `tests/test_download_guard.py`
  - `tests/test_object_storage_service.py`
  - `tests/test_asset_access_routes.py`
  - `tests/test_asset_access_api.py`

### 已实现部分

- 私有对象存储目标、签名 URL、用途/用户绑定数据和正式任务 owner 校验基础。
- 下载守卫函数支持一次性消费语义。
- 水印能力存在。

### 真实上线 blocker

1. `/objects/<key>` 只验证签名和对象 key，没有把签名用户绑定到当前认证会话，也没有持久消费 nonce。
2. `/download/<name>` 调用守卫时没有传入持久化 `consumed_token_keys`，所谓一次性 token 在真实路由中不会被消费。
3. 隔离 staging 探针用同一导出 token 连续请求两次，结果为：

   ```text
   first request: 200
   replay request: 200
   ```

4. `/media/<path>` 可匿名读取 `LIBRARY_DIR` 下内容，存在源图库直接暴露面。
5. `/model-inputs/<24hex>.jpg` 可匿名读取模型输入图。
6. 当前用户识别可受 `X-User-Id` 或查询参数影响，不能作为互联网边界上的可信身份。
7. 签名接口虽然在非本地模式要求服务 token，但调用者仍可指定 object key、user、purpose 和 base URL；缺少最小授权对象清单。
8. 没有持久化下载次数、用户/IP 配额、批量抓取检测、CDN/WAF 限速、异常告警和跨用户渗透证据。
9. 水印是可选的产品功能，不是强制的预览防盗水印策略。

### 最小后续任务

1. 关闭匿名 `/media` 和 `/model-inputs`，所有资产统一走认证主体与 owner 校验。
2. nonce/下载次数写入 Redis 或 PostgreSQL，使用原子消费实现一次性/限次下载。
3. 签名服务只允许基于已授权 manifest 生成 URL，不接受任意对象 key/user。
4. 增加预览强制水印、下载/IP/账号限流、抓取行为检测、告警和安全测试。

### 外部验证要求

- 必须：真实私有 COS、CDN/WAF、跨账号渗透测试和抓取压力测试。
- `pytest` 通过仍不能证明：对象桶确实私有、URL 无法重放、源图不泄漏或批量抓取可被阻断。

---

## 5. 数据后台与运营、财务、风控权限

**状态：Partial**

### 当前实现证据

- `admin_panel.py`
  - 后台首页、dashboard、图库样本、菜单审计、AI 资产和 users/orders/risk-events/withdrawals 等列表。
- `app.py`
  - admin、finance、risk、asset 等角色辅助函数。
  - 部分结算、提现、风控和资产变更接口有角色校验及审计写入。
  - `/api/admin/queue-snapshot` 和 `/api/ops/readiness`。
- `product_database.py`
  - 用户、订单、任务、风险、审计、提现等后台查询基础。
- 测试证据：
  - `tests/test_admin_panel.py`
  - `tests/test_admin_api.py`
  - `tests/test_admin_mutation_auth.py`
  - `tests/test_roles_and_audit.py`

### 已实现部分

- 后台页面与多类运营数据查询已有基础。
- 多个敏感写操作存在角色约束和审计记录。

### 真实上线 blocker

1. `admin_panel.py` 注册的 `/admin`、dashboard、users、orders、risk-events、withdrawals 等读取路由没有统一鉴权装饰器。
2. `/api/admin/queue-snapshot` 和 `/api/ops/readiness` 也可匿名读取。
3. 隔离 staging 探针在未提供认证时实际得到：

   ```text
   /admin                              200
   /api/admin/dashboard                200
   /api/admin/lists/users              200
   /api/admin/lists/orders             200
   /api/admin/lists/risk-events        200
   /api/admin/lists/withdrawals        200
   /api/admin/queue-snapshot           200
   /api/ops/readiness                  200
   protected admin mutation            403
   ```

   说明“写接口部分受保护”不能弥补后台 PII、订单、财务和风控读取暴露。
4. `admin_panel.py` 与 `app.py` 存在重复/相邻管理路由，权限策略不统一。
5. 当前角色来自用户 metadata、环境变量或静态 admin token，没有正式 RBAC 表、MFA/SSO、权限变更审批和会话撤销。
6. 未看到字段级脱敏、导出审批、审计防篡改和数据留存策略。

### 最小后续任务

1. 给整个 admin blueprint 增加默认拒绝的认证/RBAC 门禁，逐路由声明最小角色。
2. 移除重复的未保护管理路由，敏感字段默认脱敏。
3. 引入数据库 RBAC、MFA、会话撤销、权限变更审计和高风险操作双人复核。
4. 为匿名读取、越权、跨角色、CSRF 和批量导出增加合同与 E2E 测试。

### 外部验证要求

- 必须：真实身份提供方或正式账号体系、权限矩阵业务确认、安全渗透测试。
- `pytest` 通过仍不能证明：真实管理员身份安全、最小权限、MFA、审计不可篡改或隐私合规。

---

## 6. 图片缓存与 AI 资产复用入库

**状态：Partial**

### 当前实现证据

- `ai_asset_repository.py`
  - 本地 JSONL 资产清单。
  - 保存 category、style、product、keywords、match names、provider、quality、object/local path、SHA-256 和状态。
  - 支持去重、approved/rejected/disabled 和 `find_reusable()`。
- `app.py`
  - `persist_ai_generated_asset()`：本地复制、技术质量报告、可选 COS 上传、写入清单。
  - `library_images()`：`lru_cache(maxsize=1)` 加载 approved 且本地文件存在的产品资产。
- 测试证据：
  - `tests/test_ai_asset_repository.py`
  - `tests/test_ai_asset_ingestion.py`
  - `tests/test_ai_asset_api.py`
  - `tests/test_product_generation_app.py`

### 已实现部分

- AI 图入库字段、SHA 去重、状态和本地复用基础。
- 可选写入 COS，并有基础图片尺寸/文件质量门禁。

### 真实上线 blocker

1. 元数据清单是本地 JSONL，多实例、重启、并发写和容器临时磁盘下不可靠。
2. `library_images()` 只加载有 `localPath` 的 approved 记录，COS-only 资产不能被正式匹配复用。
3. 入库后没有统一清理 `library_images()` 缓存；部分上传路径会清理，但自动生成入库路径不会稳定刷新。
4. 仓库级 `find_reusable()` 没有直接接入客户正式匹配主路径；当前更多是把本地 approved 资产展平成旧图库匹配。
5. 技术质量门禁不能判断“菜名是否正确、背景是否符合品类、套餐组件是否完整”。
6. 缺少租户隔离、授权来源、提示词版本、模型版本、人工审核、过期/下架和版权追溯完整闭环。

### 最小后续任务

1. 将资产元数据迁入 PostgreSQL，原图和派生图进入私有 COS。
2. 建立可查询的 category/product alias/style/prompt/model/quality/tenant/provenance 索引。
3. 统一复用选择服务并接入正式生成路径，使用事件驱动缓存失效。
4. 增加语义审核、人工抽检和错误资产隔离，防止错误图片被重复放大。

### 外部验证要求

- 必须：真实 COS、多实例环境、人工标注质量集和版权/授权确认。
- `pytest` 通过仍不能证明：容器重启后资产可复用、跨实例一致、语义正确或版权可商用。

---

## 7. 精细改图、版本与导出

**状态：Partial**

### 当前实现证据

- `shared/refinement_contract.py`
  - 不可变输入 SHA/object 快照和服务端价格。
  - rework 普通 10、精修 20，refine 10，并支持免费额度合同。
- `image_edit_provider.py`
  - Gemini Interactions 适配，默认 `gemini-3.1-flash-image`，失败关闭。
- `refinement_pipeline.py`
  - 计算源图/背景差分蒙版，并锁定非菜品区域。
- `worker/product_revision_handler.py`
  - 校验合同和输入 SHA。
  - 调用编辑模型、拒绝未变化结果、合成锁定背景、写私有 PNG/manifest。
- `app.py`
  - 创建、状态、资产、取消、扣费/退款和导出选择接口。
  - 支持基于前一个 revision job 继续修改。
- 测试证据：
  - `tests/test_refinement_contract.py`
  - `tests/test_refinement_pipeline.py`
  - `tests/test_image_edit_provider.py`
  - `tests/test_product_revision_handler.py`
  - `tests/test_refinement_api_integration.py`
  - `tests/test_refinement_queue_integration.py`

### 已实现部分

- 精细改图合同、不可变输入验证、Redis 任务、背景锁定和私有输出基础。
- 任务可串联形成版本来源，并可选择完成版本导出。
- 扣费失败退款和任务状态测试较完整。

### 真实上线 blocker

1. 测试使用 fake HTTP/模型结果，没有真实 Gemini 网络调用、配额、超时、内容安全和输出质量证据。
2. 质量判断主要验证“发生了变化”和“锁定区未改变”，不能证明菜品、配料、文字指令和审美结果正确。
3. 没有独立版本表、版本列表 UI、命名、比较、回滚和保留策略；目前是通用 job/manifest 串联。
4. 免费额度扫描和部分锁是进程内语义，多实例竞争仍需验证。
5. 扣费、任务持久化和 Redis 入队没有接入同一 PostgreSQL/outbox 事务。
6. 导出前缺少平台尺寸、色彩、压缩、透明度、水印和最终可下载性的一体化真实 E2E。

### 最小后续任务

1. 将 revision 任务接入 PostgreSQL/outbox，并建立正式版本模型。
2. 增加版本列表、对比、设为当前版本、回滚和删除/保留规则。
3. 建立真实 Gemini staging 测试集和人工语义评分，失败时不扣费或自动退款。
4. 对每个平台执行最终导出合同和像素级/视觉验收。

### 外部验证要求

- 必须：真实 Gemini 凭据/配额、人工视觉评审、平台上传验证。
- `pytest` 通过仍不能证明：模型真的可用、菜品编辑正确、背景一致、用户指令满足或平台审核通过。

---

## 8. 并发、Redis、Worker、PostgreSQL/outbox 与服务拆分

**状态：Partial**

### 当前实现证据

- `shared/redis_queue.py`
  - Lua 原子入队、幂等键、claim lease/fencing、heartbeat、cancel、recover、TTL 和 dead-letter。
- `worker/worker.py`
  - 独立 worker 进程入口、prompt/product 模式、超时、重试、lease 和服务心跳。
- `worker/product_batch_handler.py`
  - 校验批任务合同后执行正式生成；当前仍导入 monolith `app`。
- `shared/product_job_store.py`
  - PostgreSQL job/outbox/result 存储实现。
- `worker/outbox_dispatcher.py`
  - PostgreSQL outbox 到 Redis 的 dispatcher。
- `shared/postgres_runtime.py`
  - PostgreSQL 运行时配置。
- `migrations/001_product_generation_postgres.sql`
  - generation jobs、outbox、results、settlement。
- `migrations/002_product_wallet_postgres.sql`
  - wallet 和 ledger。
- `migrations/003_menu_uploads_postgres.sql`
  - menu uploads。
- `api-server/app.py`
  - 独立 API 服务，只负责健康检查、入队和查询状态，不执行 AI。
- 测试证据：
  - Redis 使用 `RedisTestDouble`/`AtomicFakeRedis`。
  - PostgreSQL 使用脚本化 fake connection。
  - `tests/test_redis_queue_reliability.py`
  - `tests/test_product_redis_integration.py`
  - `tests/test_product_job_store.py`
  - `tests/test_product_outbox_dispatcher.py`
  - `tests/test_postgres_runtime.py`
  - `tests/test_saas_runtime.py`
  - `tests/test_generation_batch_transaction.py`

### 已实现部分

- Redis 队列合同和可靠性机制代码较完整。
- API 可在不执行 AI 的情况下独立启动。
- Worker 有独立进程入口。
- PostgreSQL schema、job store 和 outbox dispatcher 已有代码基础。

### 真实上线 blocker

1. 正式 Web 请求路径没有使用 `ProductJobStore`；它目前主要被 dispatcher 和测试引用。
2. `app.py` readiness 明确将 `durable_job_store_integrated=False`、`transactional_outbox_integrated=False` 作为 staging blocker。
3. Worker 产品批处理仍导入 monolith `app`，服务边界没有完全解耦。
4. 当前测试没有连接真实 Redis/PostgreSQL，不能验证网络故障、并发 worker、进程崩溃、lease 过期、主从切换和容器重启。
5. `render.yaml` 没有声明 Redis、PostgreSQL、Worker 或 outbox dispatcher。
6. 全量测试当前有一个真实失败：

   ```text
   tests/test_product_job_store.py::test_claim_outbox_uses_skip_locked_and_returns_fence
   ```

   测试要求 claim SQL 包含直接的 `fence = j.fence + 1`；当前实现使用条件式 fence 增量。无论最终选择哪种语义，当前门禁是红色，且必须先冻结并发合同再修正代码或测试。

### 最小后续任务

1. 冻结 job/outbox/fence 状态机合同，修复当前失败测试。
2. 把客户生成请求、扣费、job 和 outbox 接入同一 PostgreSQL 事务。
3. 由 dispatcher 唯一负责投递 Redis，Worker 幂等写结果和结算。
4. 在真实 Redis/PostgreSQL 容器中运行多 worker、kill/restart、超时、重复投递和恢复测试。
5. 在 Render 测试环境声明独立 Web、API、Worker、dispatcher、Redis 和 PostgreSQL。

### 外部验证要求

- 必须：真实 Redis/PostgreSQL、Render 多服务、故障注入、负载和恢复测试。
- `pytest` 通过仍不能证明：真实网络并发、进程崩溃恢复、Redis/PostgreSQL 高可用或 Render 多服务运行正确。

---

## 9. 40 品类、近似菜名、套餐识别与真实准确率

**状态：Partial**

### 当前实现证据

- `matching_engine.py`
  - `TAXONOMY_RULES` 当前有 40 个叶子品类。
  - taxonomy version 为 `2026-07-30.v1`。
  - 菜名归一化、别名、品类 guard、保守相似度阈值。
  - 套餐 fingerprint 和 components 提取/比较。
  - `match_menu_to_library()` 提供 40 品类约束下的候选匹配。
- `menu_parser.py`
  - 多 sheet/header 识别。
  - 为菜单项写入 taxonomy/version/combo/components。
- `image_pipeline.py`
  - AI-first 正式生成会把菜名和套餐组件写入提示词。
- 测试证据：
  - `tests/test_menu_taxonomy_and_combo.py`
  - `tests/test_menu_parser.py`
  - `test_matching_engine.py`
  - 本组隔离测试：17 passed。

### 正式路径接入缺口

1. `match_menu_to_library()` 只在自身模块和测试中被引用，没有接入当前客户网站 `app.py` 的正式候选选择路径。
2. 客户网站仍使用 `app.py::top_candidates()` 和 `strict_match_allowed()`，主要依赖粗粒度 food/beverage/soup 以及字面 bigram，不强制 40 叶子品类相等，也没有严格使用套餐 fingerprint。
3. `app.py` 的 `CATEGORY_PROMPTS` 只有 3 个粗品类：
   - 轻食
   - 盖码饭
   - 炒菜/川湘
4. 背景提示词因此不是按 40 品类分别生成，无法满足“每一个品类对应合适背景”的要求。
5. 当前生成提示词包含菜名/组件，不等于模型输出真的与菜名和套餐一致。

### 真实菜单只读审计结果

对 `/Users/guiguixiaxia/Documents/menus` 运行当前 parser：

```text
Excel files: 24
parsed: 24
failed: 0
menu items: 3038
observed leaf taxonomies: 35 / 40
unknown taxonomy: 951 / 3038 = 31.3%
combo-tagged items: 782 / 3038 = 25.74%
combo without components: 5
combo with only one component: 104
```

这些数字只证明解析覆盖情况，不是准确率。没有人工标注真值时，31.3% unknown、25.74% combo 既可能是真实长尾，也可能包含漏识别或误识别，不能据此宣称正确。

### 已实现部分

- 40 叶子品类定义、别名归一化、套餐组件和保守匹配引擎存在。
- 24 个真实 Excel 均能解析，无 parser 崩溃。
- AI-first prompt 会携带具体菜名和套餐组件。

### 真实上线 blocker

1. 40 品类匹配引擎未接入正式客户生成主路径。
2. 背景生成仍只有 3 个粗品类提示词。
3. 3038 条真实菜单没有人工标注 taxonomy、近似名、套餐 components 和期望图片真值。
4. 没有 precision/recall、top-k 命中率、混淆矩阵、套餐完整率或按品类图片正确率。
5. 没有逐菜生成后视觉模型/人工审核，无法证明“所有菜名都对应正确产品”。
6. 未验证 40 品类每类都有足量合格背景和正式产品资产。

### 最小后续任务

1. 将统一 taxonomy service 接入 parser、候选图库、背景提示词和正式菜品生成四条路径。
2. 为 40 品类建立版本化背景模板/提示词和不适用约束。
3. 从真实 3038 条菜单分层抽样并人工标注，建立近似菜名、套餐组件和预期品类基准集。
4. 设置可验收指标：taxonomy、套餐、top-k 匹配和最终图片语义正确率；不达标时强制 AI 生成或人工复核。
5. 运行真实混元生图并由人工/视觉模型双重检查菜名、组件、背景和版式。

### 外部验证要求

- 必须：人工标注数据、真实菜单、真实混元服务、真实生成图片和业务人员视觉验收。
- `pytest` 通过仍不能证明：近似菜名真实准确率、套餐组件完整、背景适配或最终图片语义正确。

---

## 10. Render blueprint、启动结构与客户网站保留

**状态：Missing**

### 当前实现证据

- `render.yaml`
  - 当前只声明一个 `web` service。
  - start command 为：

    ```text
    gunicorn --chdir api-server app:app ...
    ```

  - 未声明 Worker、outbox dispatcher、Redis 或 PostgreSQL。
  - 未完整声明产品 worker、数据库、Gemini、短信等正式环境变量。
- `Procfile`
  - 同样把 web 指向 API-only 服务，另有 worker 命令，但 Render blueprint 不会因此自动创建 worker。
- `api-server/app.py`
  - 实际路由只有：

    ```text
    /healthz
    /generate
    /status/<task_id>
    /static/<path>
    ```

  - 隔离启动探针：`/` 返回 404，`/healthz` 返回 200。
- `AI-Project/decisions/decisions.md`
  - 明确要求现有 Render Web 保留客户网站。
  - 在创建独立服务前，不应把现有 `gunicorn app:app` 替换为 API-only 入口。

### 判断

若按当前 `render.yaml` 应用到现有客户 Web，**不会保留客户网站**；它会启动只有 API 路由的 `api-server/app.py`，根路径为 404。这直接违反当前 decisions 中的保留客户站约束。

Render Dashboard 可能存在手工覆盖，但本次没有部署或读取线上配置，因此不能用“可能覆盖”抵消源码 blueprint 的明确风险。

### 真实上线 blocker

1. 当前 blueprint 会把客户网站替换为 API-only 服务。
2. blueprint 没有完整运行拓扑，生成任务不会因仅部署 API 而自动获得 Worker、dispatcher、Redis/PostgreSQL。
3. 环境变量声明不足，真实混元/Gemini/COS/短信/支付链路无法由 blueprint 复现。
4. 没有 staging smoke/E2E 证明 Excel 上传、6 张背景、选择、全菜单生成、改图和导出。

### 最小后续任务

1. 保留现有客户 Web 的 monolith 启动入口。
2. 以独立 service 新增 API、Worker 和 dispatcher，而不是覆盖客户 Web。
3. 声明并绑定测试环境 Redis/PostgreSQL 和所需 secret/env。
4. blueprint review 通过后，才在获授权的测试环境执行部署和端到端验收。

### 外部验证要求

- 必须：Render 测试环境、多服务日志、真实环境变量、真实 Excel 到图片的浏览器 E2E。
- `pytest` 通过仍不能证明：Render blueprint 正确、客户网站仍可访问、Worker 正在消费或真实 AI 服务已配置。

---

## 11. 可执行 Backlog

### P0：上线前必须完成

1. **冻结商业规则与合规边界**
   - 解决代理一层/二层、首单/复购、统一 20% 的冲突。
   - 明确二级佣金是总池 20% 还是每层 20%，以及各层拆分。
   - 明确邀请注册积分拆分。
   - 完成中国境内分销、提现、税务和宣传口径法律审核。

2. **修复支付订单信任边界**
   - 强制认证用户。
   - 仅接受服务端套餐 ID。
   - 禁止客户端提供任意 user/amount/points 组合。
   - 增加 1 分钱高积分、跨用户、重放和篡改测试。

3. **统一支付、积分、增长奖励和任务事务**
   - 接入 PostgreSQL wallet/ledger/job/outbox。
   - 完成回调后可重复执行的积分入账和补账。
   - 解决扣费、任务、入队、退款之间的崩溃窗口。

4. **关闭后台匿名读取**
   - admin blueprint 默认拒绝。
   - users/orders/risk/withdrawals/readiness/queue 等按最小角色授权并脱敏。
   - 增加真实会话、MFA/RBAC 和越权 E2E。

5. **关闭资产匿名与可重放访问**
   - 禁止匿名 `/media` 和 `/model-inputs`。
   - 所有对象绑定认证主体和 manifest owner。
   - Redis/PostgreSQL 原子消费 nonce，落实一次性/限次下载。
   - 增加跨用户、重放和批量抓取测试。

6. **修正 Render 拓扑，保留客户网站**
   - 现有 Web 继续启动客户站。
   - 独立创建 API、Worker、dispatcher、Redis 和 PostgreSQL。
   - 不用 API-only blueprint 覆盖客户 Web。

7. **接通 PostgreSQL/outbox 正式路径**
   - 冻结 fence 合同并修复当前失败测试。
   - 正式生成请求改走 durable job + transactional outbox。
   - 真实 Redis/PostgreSQL 下运行多 worker 故障恢复测试。

8. **把 40 品类与套餐识别接入正式生成路径**
   - 替换/统一 `app.py` 的粗粒度匹配。
   - 背景提示词从 3 类扩展为版本化 40 品类体系。
   - 套餐 fingerprint 参与正式匹配和生成。

9. **完成真实测试环境端到端验收**
   - 配置真实混元、Gemini、私有 COS、短信和支付沙箱。
   - 用电脑 `menus` 文件夹的 Excel 执行上传、6 张背景、选择、全菜单生图、精细改图和导出。
   - 保存每个品类的输入、输出、耗时、失败原因和人工正确率证据。

### P1：P0 后完成

1. 二级代理关系、循环防护、分层退款冲销、KYC、合同、税务和真实出款。
2. Redis 分布式 OTP/注册限流、可信设备、CAPTCHA、黑名单和风控案件工作台。
3. AI 资产元数据迁 PostgreSQL，COS-only 复用、缓存失效、审核和来源追踪。
4. 精细改图正式版本表、比较、回滚、保留策略和语义质量门禁。
5. 真实支付宝/微信支付、部分退款、拒付、日账单和自动对账。
6. CDN/WAF、下载配额、反抓取告警、管理端字段脱敏和导出审批。
7. 建立人工标注准确率基准、每品类最小样本量和发布阈值。

### P2：稳定性与规模化

1. 压测容量模型、自动扩缩容、SLO、告警和可观测性 dashboard。
2. PostgreSQL/Redis 备份恢复演练、跨区故障方案和数据留存策略。
3. 资产语义向量检索、热门品类预生成、成本/时延优化。
4. 管理后台 SSO、细粒度 ABAC、审计归档和定期权限复核。
5. 模型/提示词 A/B、质量漂移检测和按品类自动回归。

---

## 12. 即使 pytest 全绿也不能声称生产验证的项目

以下项目必须保留“未生产验证”标记，直到获得真实环境证据：

1. 中国境内二级代理、返佣、提现和税务合规。
2. 真实支付宝/微信资金、退款、拒付和日账单对账。
3. 手机短信到达率、真人识别、接码平台和注册攻击防护。
4. COS 桶私有性、签名 URL、一次性下载、跨用户隔离和反抓取。
5. Render 多服务、Redis/PostgreSQL 网络、进程崩溃与恢复。
6. 混元/Gemini 的凭据、配额、延迟、限流、内容安全和图片质量。
7. 40 品类、近似菜名、套餐组件和最终图片的真实准确率。
8. 美团、饿了么、京东实际上传后的尺寸、压缩、审核和显示效果。
9. 管理后台真实身份、MFA、最小权限、隐私合规和渗透测试。
10. 高并发下积分不超扣、不漏扣、任务不丢失和结算不重复。

---

## 13. 实际只读命令与结果

### 13.1 基线与源码检索

```bash
git status --short --untracked-files=all
rg -n "AGENT_|agent_commission|consumer_referral|registration" growth_rules.py growth_service.py AI-Project/decisions/decisions.md
rg -n "ProductJobStore|durable_job_store_integrated|transactional_outbox_integrated" .
rg -n "match_menu_to_library|top_candidates|strict_match_allowed|CATEGORY_PROMPTS" .
rg -n "api/admin|queue-snapshot|ops/readiness|/media|model-inputs|objects/sign" app.py admin_panel.py
```

结果：确认既有 dirty baseline；确认代理规则冲突、PostgreSQL/outbox 未接正式请求路径、40 品类 matcher 未接客户主路径、后台和资产公开路由风险。

### 13.2 全量测试

测试在隔离副本 `/tmp/waimai-product-audit.z1QkHj` 运行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

结果：

```text
663 passed, 1 failed in 9.55s
FAILED tests/test_product_job_store.py::test_claim_outbox_uses_skip_locked_and_returns_fence
```

### 13.3 分组复核

```text
product job store group:                         22 passed, 1 failed
security/growth/admin group:                    180 passed
taxonomy/parser/matching group:                  17 passed
refinement group:                               127 passed
Redis/SaaS/product/outbox/PostgreSQL group:      146 passed
AI asset/product app group:                     102 passed
```

这些分组仍使用 fake Redis、脚本化数据库连接或 mock AI，不是外部服务验证。

### 13.4 前端语法

```bash
node --check static/app.js
```

结果：exit 0。

### 13.5 真实菜单解析

```bash
PYTHONDONTWRITEBYTECODE=1 python3 menu_parser.py /Users/guiguixiaxia/Documents/menus
```

结果：24/24 Excel 解析成功，0 失败，共 3038 个菜单项。补充只读统计见第 9 节；该统计不是准确率。

### 13.6 隔离安全与启动探针

使用临时 SQLite、临时对象目录和 staging/development 环境执行 Flask test client 探针，没有访问或修改生产服务：

```text
支付订单参数探针：HTTP 200，可创建 1 分钱 / 999999 积分订单
后台匿名读取探针：主要 admin/list/readiness/queue 路由均 HTTP 200
一次性下载重放探针：同一 token 连续两次均 HTTP 200
API-only 启动探针：/ 返回 404，/healthz 返回 200
```

这些结果是当前源码行为的直接证据，也是本报告 P0 判定的依据。

---

## 14. 最终判断

当前项目不是“完全未实现”，而是已经形成了较多模块骨架和局部合同测试；但“规划过”“存在文件”“mock 测试通过”均不能等价为“网站全部功能已完成”。

后续执行应先处理 P0 信任边界和运行拓扑，再接真实外部服务，最后用带人工真值的菜单与图片数据验收准确率。若跳过此顺序，继续增加界面或生成策略会掩盖支付、权限、资产泄漏和任务耐久性问题，并且无法形成可信的生产验收结论。
