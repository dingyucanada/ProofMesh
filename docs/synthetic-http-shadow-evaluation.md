# 独立进程 HTTP 合成沙箱与盲测方法

> 本评测只使用确定性生成的合成工单。它不是企业历史数据、客户试点、第三方支付接入、真实 CRM、外部上游签名或生产 SLA 证据。

## 为什么增加这一层

原有 `CommerceSandbox` 在同一 Python 进程内验证事务不变量，适合快速测试，但不能证明网关面对网络超时和分离状态所有权时的恢复行为。本评测保留现有 `ActionGateway`，新增两个独立 OS 进程：

- `synthetic-payment`：独立 SQLite，负责订单、退款、补偿和按 Gateway `operation_id` 对账；
- `synthetic-crm`：独立 SQLite，负责工单、关单和脱敏 resolution memory；
- runner：只经回环 HTTP 访问两项服务，Gateway 自己的 operation/receipt store 仍为第三个独立状态域。

这里的“独立”仅指本机进程与数据库边界，不代表独立企业、第三方机构或生产系统。

## 可重放 workload

运行：

```bash
PYTHONPATH=src python3 scripts/benchmarks/run_synthetic_http_shadow.py \
  --cases 240 \
  --seed 20260813 \
  --output artifacts/synthetic-http-shadow
```

输出：

| 文件 | 用途 |
|---|---|
| `workload.json` | 冻结 seed、全部合成案例、预期终态与显式 provenance |
| `case-results.jsonl` | 每案例故障、终态、receipt 数与时延 |
| `report.json` | 机器可读 KPI、故障矩阵、Wilson 区间与方法边界 |
| `report.md` | 人类可读报告 |
| `manifest.json` | 文件大小和 SHA-256 清单 |

Schema 明确固定：

```json
{
  "synthetic": true,
  "customer_data": false,
  "enterprise_historical_data": false,
  "third_party_provider": false,
  "blind_workload": true,
  "shadow_write_mode": "isolated-local-http-sandbox"
}
```

## 故障注入矩阵

| profile | 注入位置 | 期望行为 |
|---|---|---|
| `normal` | 无 | 退款与关单完成 |
| `crm_close_error` | CRM 提交前返回合成 503 | 退款后执行补偿，余额恢复，工单保持开放 |
| `payment_timeout_after_commit` | 支付已提交、HTTP 响应晚于客户端 timeout | Gateway 租约过期后按 operation id 对账，恢复结果而不重复退款 |
| `reconcile_unknown_after_commit` | 支付已提交但对账可见性故意返回 UNKNOWN | Gateway fail closed，持久化 UNKNOWN，禁止自动重派 |

全部案例（包括预期 UNKNOWN 的案例）都先注入一次无副作用的金额参数漂移，验证 passport 参数绑定在 HTTP dispatch 前拒绝；可判定终态案例再重复同一幂等键，验证零重复副作用。

### 两个必须保留的实验边界

1. 为避免每个超时案例等待 Gateway 的 15 秒生产形态租约，runner 使用 `_expire_operation` 直接推进其**隔离测试 GatewayStore** 的租约时间。这只是确定性故障注入钩子，不是生产或公开 API，也不修改支付、CRM 服务状态；随后仍通过正常 `call_tool` 进入真实 reconcile-before-retry 路径。
2. 这是 Gateway 授权、HTTP dispatch、幂等、恢复与补偿专项。案例使用 `AUTOMATIC` 合成低金额 passport，未启用 pinned approval policy，**不覆盖 Human approval assertion**；人工审批完整性应由另一组专项测试证明。

## KPI 口径

| KPI | 定义 | 重要边界 |
|---|---|---|
| 重复副作用率 | 额外支付/CRM 写效果数 ÷ 合成案例数 | 零观察失败仍需报告 Wilson 上界 |
| 错误放行率 | 参数漂移后仍到达合成服务的次数 ÷ 漂移尝试数 | 不等于模型攻击成功率 |
| 可恢复终态率 | 完成、对账恢复或补偿案例 ÷ 上游状态可判定案例 | UNKNOWN 不进入“恢复成功”分母 |
| UNKNOWN fail-closed | 停止自动重派的模糊对账案例 ÷ 全部模糊对账案例 | UNKNOWN 是人工处置状态，不是成功 |
| Gateway HTTP 时延 | Gateway 调用两个本地 HTTP 服务的 p50/p95/p99 | 本机回环画像，不是生产 SLA |
| 审计读回耗时 | 读取合成业务 snapshot 加 Gateway receipt 的程序耗时 | 不是人工审计取证时间或客户改善量 |

## 不能推出什么

- 不能推出真实支付、真实 CRM 的可用性、幂等语义或签名可信度；
- 不能推出企业历史异常率、人工触达率、财务损失或 ROI；
- 不能替代客户盲测、第三方安全评估、容量测试或生产演练；
- 不能把合成 200–500 条 workload 称为“客户历史工单”。
- 不能据此宣称 Human approval assertion 或高风险人工门禁已经在本 workload 中得到覆盖。
