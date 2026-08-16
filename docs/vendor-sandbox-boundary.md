# Stripe test + HubSpot developer-test 供应商边界

## 结论与证据边界

`proofmesh.vendor_sandbox.StripeHubSpotSandboxBackend` 是接在既有 `ActionGateway` 后方的真实供应商协议适配器：退款调用 Stripe `v1` test-mode API，工单读取和关单调用 HubSpot CRM `v3` Ticket API。网关仍然负责 Action Passport、精确参数/资源/金额授权、持久化 operation id、总预算、fencing lease、Receipt 和审计链。

仓库内测试只使用独立的本地 HTTP 仿真服务，不使用真实密钥、真实账户、真实支付或客户工单。因此当前可以声明 **sandbox-ready / provider-contract-tested**，不能声明“已在真实供应商账户实跑”“客户试点”“生产 SLA”或“真实资金退款”。只有 `vendor_readiness_probe.py --network` 在用户自己的 test account 返回 `ready=true`，且完成下述受控 smoke run 后，才可另行提交外部运行证据。

## 安全不变量

- Stripe 只接受 `sk_test_` / `rk_test_` key，检测到 live key 时拒绝启动；HubSpot 必须使用 developer test account 的 token。
- credential 只从环境变量读取，不进入工具参数、Passport、Receipt、workflow proof、URL、`repr`、异常或日志。readiness probe 仅输出布尔值以及供应商公开 account/portal id。
- 默认只允许官方 HTTPS host。自定义 HTTP base URL 仅允许 loopback，供本地合同测试；禁止 URL 用户名/密码、query、fragment 和任意远端 host，避免 credential 被 SSRF/重定向带走。HTTP redirect 被禁用。
- HubSpot 只读取白名单自定义属性；不读取 subject、content、联系人姓名、邮件、电话或任意自由文本。`resolution` 只把 SHA-256 摘要写回 HubSpot。
- `tenant_id` 在 Gateway 注入、配置和 HubSpot Ticket 自定义属性三处一致才允许操作。供应商响应必须回绑 workflow、ticket、tenant、operation、金额、币种和资源。
- provider 4xx 被分为授权失败、权限拒绝、参数拒绝、资源缺失、状态/幂等冲突；408/425/429、5xx、timeout、连接中断、畸形/超大响应均视为“不确定”，不能盲目重试。
- Stripe refund 使用 Gateway `operation_id` 作为 `Idempotency-Key`，并把 operation/workflow/ticket/tenant 写入 metadata。租约到期后先按 PaymentIntent + metadata 查账：精确命中恢复成功；确认不存在才允许重发；无法确认或多条命中则冻结为 `UNKNOWN`。
- HubSpot 没有通用 Idempotency-Key；关单前写入 operation/workflow/refund/tenant binding。超时后读取 Ticket：精确命中即恢复；仍开放且没有任何操作痕迹才允许重试；其他状态一律 `UNKNOWN`。
- Stripe refund 是不可逆资金动作，适配器不会把另一笔交易伪装成“补偿”。`payments.compensate_refund` 在供应商模式明确返回 `stripe_refund_irreversible_manual_remediation`。若业务需要可逆 saga，应把“撤销支付/返向入账”建模为新的、另行授权的业务动作。

## 必需的 sandbox 数据契约

### Stripe

使用 test-mode PaymentIntent，订单主键即 `pi_...`。支付必须已成功且 `latest_charge` 可展开。ProofMesh 使用：

- `PaymentIntent.id/status/currency/customer/latest_charge`
- `Charge.amount_captured/amount_refunded`
- `Refund.id/payment_intent/amount/currency/status/metadata`

建议创建只拥有读取 PaymentIntent/Charge、创建与读取 Refund、读取 Account 权限的 restricted **test** key。实际权限名以 Stripe Dashboard 当前界面为准。

### HubSpot

在 developer test account 的 Ticket 对象创建以下自定义属性。除 `hs_pipeline_stage` 外，均使用单行文本或数值且不要放 PII：

| 属性 | 用途 |
|---|---|
| `proofmesh_order_id` | Stripe test PaymentIntent id |
| `proofmesh_customer_id` | 合成/脱敏业务客户标识 |
| `proofmesh_requested_amount_minor` | 最小币种单位整数 |
| `proofmesh_currency` | ISO 4217 大写币种 |
| `proofmesh_reason_code` | 受控原因枚举，不是自由文本 |
| `proofmesh_operation_id` | Gateway 稳定操作号 |
| `proofmesh_workflow_id` | ProofMesh workflow id |
| `proofmesh_refund_id` | 已回查的 Stripe refund id |
| `proofmesh_tenant_id` | credential 固定租户绑定 |
| `proofmesh_resolution_digest` | resolution SHA-256，不含原文 |

配置 `PROOFMESH_HUBSPOT_OPEN_STAGE_ID` / `PROOFMESH_HUBSPOT_CLOSED_STAGE_ID` 为该测试 pipeline 的实际 open / closed stage internal id。其他 stage 一律不可操作，不会被宽松地当成“open”。token 需要读取和写入 Ticket、读取 account info 所需的最小 scopes；不要授予联系人、营销或生产账户权限。

## 私密配置与 readiness

不要把值发到聊天、提交 Git、粘贴进文档或截图。把它们放到本地未跟踪 `.env` 或 secret manager：

```text
PROOFMESH_VENDOR_TENANT_ID=acme-cn
PROOFMESH_STRIPE_SECRET_KEY=<Stripe restricted test key>
PROOFMESH_HUBSPOT_ACCESS_TOKEN=<developer test account token>
PROOFMESH_HUBSPOT_OPEN_STAGE_ID=<open stage internal id>
PROOFMESH_HUBSPOT_CLOSED_STAGE_ID=<closed stage internal id>
PROOFMESH_VENDOR_TIMEOUT_SECONDS=3.0
```

不联网检查配置存在性：

```bash
PYTHONPATH=src python3 scripts/vendor_readiness_probe.py
```

显式联网、只读验证两边账户：

```bash
PYTHONPATH=src python3 scripts/vendor_readiness_probe.py --network
```

输出只含四个配置布尔值、`acct_...`、HubSpot 数字 portal id 与最终 `ready`；任何异常都 fail closed，不输出响应体或 credential。

## 合同验证

```bash
pytest -q tests/test_vendor_sandbox.py
```

本地 HTTP 合同矩阵覆盖：正常成功、同键重复零额外退款、Stripe 提交后超时再对账恢复、对账不可用转 `UNKNOWN` 且不重发、Stripe 拒绝、Stripe 幂等冲突、HubSpot 提交后超时恢复、关单状态冲突、权限拒绝、live key 禁用和 readiness 输出去敏。

真实 test-account smoke run 应固定一个全新 PaymentIntent 与 Ticket，先保存运行前快照，再经 `ActionGateway.call_tool` 执行一次退款、一次关单、一次同键回放，最后保存去敏响应、Receipt、operation 行、供应商事件时间线和 SHA-256。不要直接调用 adapter 作为参赛证据；直接调用绕过了授权网关。
