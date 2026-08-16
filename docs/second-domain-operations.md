# 第二业务域：生产运维配置变更参考实现

## 1. 结论与边界

ProofMesh 已不再只有退款域。`production-operations-change` reference workflow 使用同一套 Action Passport、Action Gateway、GatewayStore、外部审批断言、签名 Execution Receipt、证据链和包外 Verifier，完成四条确定性路径：

| 路径 | Fixture | 终态 | 核心不变量 |
|---|---|---|---|
| 低风险自动执行 | `CHG-LOW-001` | `COMPLETED` | 精确 patch + expected version；健康门通过 |
| 高风险外部审批 | `CHG-HIGH-001` | `COMPLETED` | 审批前无 execution；JWS 绑定 service / patch / revision / risk units |
| 执行后健康失败 | `CHG-ROLLBACK-001` | `COMPENSATED` | 恢复精确 before config；版本单调递增而非倒退 |
| 提交状态不可判定 | `CHG-UNKNOWN-001` | `UNKNOWN_MANUAL` | Gateway 持久化 `UNKNOWN`；不自动重派，不猜测提交结果 |

这是本地 SQLite reference adapter 与确定性故障注入证据，**不是生产 Kubernetes 集群、客户数据、云厂商账号、SLA 或试点**。UNKNOWN 路径为缩短测试时间，在隔离 fixture 内直接推进 Gateway lease；该 hook 不是公开或生产 API。

## 2. 复用而非复制

共享协议实现位于：

- `src/proofmesh/domain_protocol.py`：`AuthorizedToolCaller` 统一 mint Passport、调用 Gateway、持久化 receipt/tool execution；退款工作流已改用同一实现；
- `src/proofmesh/gateway.py`：同一 Gateway 可以钉住多个 policy digest，各策略只提供自己的自动审批阈值；
- `src/proofmesh/capabilities.py`：同一 `ActionPassportClaims` 与外部 Human approval assertion；
- `src/proofmesh/gateway.py` / `GatewayStore`：同一逻辑 operation、lease、fencing、幂等、对账与 UNKNOWN 语义；
- `src/proofmesh/ledger.py`：同一 append-only hash chain。

第二域仅新增真正的业务差异：

- `OperationsSandbox`：change / service config / execution adapter；
- `operations_change_policy.json`：使用无财务含义的 `risk_units`，JWS 中以 ISO 4217 测试币种 `XTS` 承载有界数值；
- `OperationsChangeControlPlane`：冻结 patch、expected version、blast radius，定义健康验证与恢复不变量；
- `operations_verifier.py`：从包外 trust bundle 与 pinned operations policy 复验终态。

退款域仍使用 CNY 金额语义；运维域 `risk_units` 不是金额，也不得在材料中解释为成本或收益。

## 3. 可复验入口

```bash
pytest -q tests/test_operations_change_workflow.py tests/test_operations_reference_artifacts.py
PYTHONPATH=src python scripts/generate_operations_reference.py \
  --output artifacts/operations-reference
```

冻结证据入口：`artifacts/operations-reference/report.json`。三个已知终态有完整 signed proof 与外部验证报告；UNKNOWN 只有 fail-closed evidence，刻意不伪造一个可证明的业务终态。

## 4. 迁移到真实平台

替换 `OperationsSandbox` 时必须保持以下 adapter 合同：

1. `ops.apply_config` 接收稳定 workflow / idempotency identity，或提供可按该 identity 查询的 change execution；
2. 写入响应丢失后，`reconcile` 只能返回 `SUCCEEDED`、`SAFE_TO_RETRY` 或 `UNKNOWN`，不能以本地缓存猜测；
3. `ops.restore_config` 必须以 execution 保存的 before state 和当前 version 作 fencing；发现后续变更必须拒绝自动恢复；
4. `workflow_snapshot` 由业务系统独立身份签名，Proof Sealer 不能替它证明真实终态；
5. 生产环境禁用 reference lease hook，并将 GatewayStore、signer、身份与上游连接迁到 HA Postgres、远程 KMS/HSM、mTLS/SPIFFE 等实际信任边界。

接入真实 Kubernetes、Argo Rollouts 或云配置平台前，还需补 provider sandbox、真实 dry-run/canary 健康指标、RBAC 映射、限流和独立复现记录。
