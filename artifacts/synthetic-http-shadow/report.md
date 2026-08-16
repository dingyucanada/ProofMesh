# 独立进程 HTTP 合成沙箱盲测报告

> **数据声明：本报告只使用确定性生成的合成数据。不是企业历史工单，不是客户试点，不是第三方支付接入，不代表生产 SLA 或财务 ROI。**

## 实验边界

- 样本：240 个合成案例；冻结 seed `20260813`。
- 进程边界：支付服务、CRM 服务、ProofMesh runner 是独立 OS 进程，通过回环 HTTP 通信。
- 状态边界：支付与 CRM 各自拥有独立 SQLite 文件；ProofMesh Gateway 另有自己的操作账本。
- 盲测：案例计划先冻结，全部运行后才聚合 KPI；没有企业现网基线。
- 参数漂移负测：全部 240 个案例各执行一次无副作用的 passport 参数漂移；UNKNOWN 样本也包含在分母内。
- 租约到期注入：仅测试 runner 直接推进隔离 GatewayStore 的租约时间；不是生产 API，不修改支付或 CRM 状态。
- 审批边界：本专项只使用 `AUTOMATIC` 合成低金额 passport，未启用 pinned approval policy，不覆盖 Human approval assertion。

## 故障矩阵

| 故障 | 案例数 | 注入点 | 预期终态 |
|---|---:|---|---|
| `normal` | 60 | none | COMPLETED |
| `crm_close_error` | 60 | CRM close before commit | COMPENSATED |
| `payment_timeout_after_commit` | 60 | payment response after committed refund | RECOVERED_COMPLETED |
| `reconcile_unknown_after_commit` | 60 | payment reconcile visibility after committed refund | UNKNOWN_MANUAL |

## KPI

| 指标 | 结果 | 95% 边界 / 口径 |
|---|---:|---|
| 重复副作用率 | 0 / 240 | Wilson [0.0, 0.015754] |
| 错误放行率 | 0 / 240 | Wilson [0.0, 0.015754] |
| 可恢复终态率 | 180 / 180 | 只以可判定上游状态为分母；Wilson [0.979105, 1.0] |
| UNKNOWN fail-closed | 60 / 60 | 不把 UNKNOWN 伪装为成功 |
| HTTP 调用 p50 / p95 / p99 | 5.073 / 12.214 / 20.198 ms | 本机回环 HTTP；非生产 SLA |
| 审计读回 p50 / p95 | 0.003 / 0.01 s | 程序化 snapshot+receipt 读取；非人工审计耗时 |

终态计数：`COMPLETED=60`、`RECOVERED_COMPLETED=60`、`COMPENSATED=60`、`UNKNOWN_MANUAL=60`。

## 可证伪边界

- 这证明两个独立 HTTP 合成服务上的调用、幂等、对账、补偿与 UNKNOWN 行为可重放。
- 它不证明真实支付机构、真实 CRM、客户数据、业务收益、生产吞吐、外部上游签名或人工审批断言。
- 零观察失败不等于真实失败率为零；报告保留 Wilson 95% 区间。
- 审计指标是程序读取时间，不应当作企业审计人员节省时间。
