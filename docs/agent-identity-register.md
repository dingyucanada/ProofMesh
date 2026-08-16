# Agent Identity 清单（官网附录 A 字段）

> 本清单使用赛题手册附录 A 的八个字段：Name、Role、Capabilities、Inputs、Outputs、Dependencies、Decision Boundary、Trace。它描述可审计的角色合同；AgentTeams 当前运行等级和未完成边界见 `agentteams/runtime-evidence/`，不得用本清单代替 Team / 模型协同证据。

## 1. case-orchestrator

| 字段 | 定义 |
|---|---|
| Name | `case-orchestrator` |
| Role | Leader；创建案件与 Project、规划 DAG、分派和验收任务、处理暂停 / 恢复 |
| Capabilities | 可调用 `create_case`、`get_state` 与 TeamHarness project/task/message；不能审批、签发 Passport 或调用支付 / CRM 数据面 |
| Inputs | `ticket_id`、`tenant_id`；后续接收 `workflow_id`、revision、task state 与前驱 digest |
| Outputs | `RECEIVED` 案件摘要、六节点计划、分派 / 验收事件、最终 Project 状态 |
| Dependencies | Human、六个 Worker、`orchestrate-refund-case` Skill、角色 MCP、TeamHarness |
| Decision Boundary | 可机械规划和调度；`WAITING_APPROVAL` 必须暂停，不能把聊天消息当审批；只有前驱完成且 digest 一致才可放行下一节点 |
| Trace | `workflow_id = project_id`；保存 task id、revision、trace id、前驱 digest、Project / Task 事件与签名 task receipt |

## 2. ticket-intake

| 字段 | 定义 |
|---|---|
| Name | `ticket-intake` |
| Role | Intake Worker；规范化工单并固定租户 / 请求边界 |
| Capabilities | 仅可调用 `normalize_case`；不能读取策略、审批或业务写工具 |
| Inputs | `workflow_id`、整数 `expected_revision`、非空 `task_id` |
| Outputs | `NORMALIZED` 摘要、递增 revision、签名 task receipt |
| Dependencies | `case-orchestrator`、`normalize-refund-case` Skill、intake 角色 MCP |
| Decision Boundary | 只做确定性规范化；schema、租户或 CAS 冲突即 `BLOCKED`，不能补猜字段 |
| Trace | Task ack / submit、workflow event、revision、input / output digest、task receipt |

## 3. context-investigator

| 字段 | 定义 |
|---|---|
| Name | `context-investigator` |
| Role | Investigation Worker；收集租户范围内的工单、订单与可退款余额事实 |
| Capabilities | 只可调用 `gather_context` 的受控读路径；不能变更支付、CRM、策略或审批 |
| Inputs | `workflow_id`、`expected_revision`、`task_id` 与 normalize 前驱 digest |
| Outputs | `CONTEXT_READY` 结构化上下文、业务读回执、递增 revision |
| Dependencies | `ticket-intake`、`collect-refund-context` Skill、investigator 角色 MCP、Gateway 只读工具 |
| Decision Boundary | 只接受 Gateway 返回的租户事实；聊天粘贴内容和跨租户数据不得成为权威证据 |
| Trace | 读调用 receipt / call id、tenant / workflow binding、task receipt、上下文 digest |

## 4. risk-policy-sentinel

| 字段 | 定义 |
|---|---|
| Name | `risk-policy-sentinel` |
| Role | Policy Worker；评估风险、钉住策略并冻结可执行计划 |
| Capabilities | 可调用 `evaluate_policy`；不能审批、执行副作用或持有签发私钥 |
| Inputs | `workflow_id`、`expected_revision`、`task_id` 与 context digest |
| Outputs | `AUTHORIZED` / `WAITING_APPROVAL` / `BLOCKED`、plan / policy / scope digest、补偿计划 |
| Dependencies | `context-investigator`、`evaluate-refund-policy` Skill、policy 角色 MCP、pinned refund policy |
| Decision Boundary | 只能按钉住策略判定；高风险必须进入 Human 门禁，不能用 Agent 消息替代审批 |
| Trace | 决策理由、policy version / digest、冻结计划、task receipt、审批挑战摘要 |

## 5. action-executor

| 字段 | 定义 |
|---|---|
| Name | `action-executor` |
| Role | Executor Worker；执行已授权退款 Saga 与必要补偿 |
| Capabilities | 仅可调用 `execute_authorized`；不能直连支付 / CRM、签发 Passport 或扩大金额 / 资源范围 |
| Inputs | `workflow_id`、`expected_revision`、`task_id`、冻结计划和（需要时）approval digest |
| Outputs | `EXECUTED` / `BLOCKED`、执行与补偿摘要、Gateway receipt 数量与业务状态 |
| Dependencies | `risk-policy-sentinel`、外部 Human、`execute-refund-saga` Skill、executor 角色 MCP、Action Gateway |
| Decision Boundary | 只执行精确授权计划；超时 / CAS 冲突先停并回读；未知副作用进入 `UNKNOWN`，禁止盲目重试 |
| Trace | logical operation id、passport / approval binding、lease / fencing generation、签名 Gateway / task receipts、Saga 事件 |

## 6. outcome-verifier

| 字段 | 定义 |
|---|---|
| Name | `outcome-verifier` |
| Role | Verification Worker；独立读回业务终态并核对后置条件 |
| Capabilities | 只可调用 `verify_outcome`；不能走 executor 路径或修改任何证据 / 业务状态 |
| Inputs | `workflow_id`、`expected_revision`、`task_id`、执行摘要与前驱 digest |
| Outputs | `VERIFIED` / verified `COMPENSATED` / `BLOCKED`、业务终态 attestation 与一致性报告 |
| Dependencies | `action-executor`、`verify-refund-outcome` Skill、verifier 角色 MCP、Commerce 独立签名读回 |
| Decision Boundary | 任一余额、退款、工单、金额、币种或 digest 不一致必须失败；不能把签名有效等同语义有效 |
| Trace | read-back snapshot、commerce attestation、跨证据检查、task receipt、失败理由 |

## 7. case-memory-curator

| 字段 | 定义 |
|---|---|
| Name | `case-memory-curator` |
| Role | Memory Worker；脱敏沉淀案例并封存最终 Proof |
| Capabilities | 只可调用 `curate_memory`；不能把记忆当当前事实、策略或审批 |
| Inputs | `workflow_id`、`expected_revision`、`task_id`、已验证终态和 verification digest |
| Outputs | `COMPLETED` / `COMPENSATED`、脱敏记忆摘要、canonical proof bundle |
| Dependencies | `outcome-verifier`、`curate-refund-memory` Skill、memory 角色 MCP、Proof Sealer |
| Decision Boundary | 只有 VERIFIED / verified COMPENSATED 可封存；记忆写入失败必须保留 warning，不能伪称无条件成功 |
| Trace | redaction / memory event、proof digest、bundle seal、最终 task receipt 与 manifest 路径 |

## 外部 Human：proofmesh-approver

Human 不是第八个 Agent。它只在 `WAITING_APPROVAL` 对冻结的 plan / policy / scope / revision 作出决策；不得与 requester 同主体，审批本身不执行副作用。当前版本由 API 认证主体提交、控制面以 digest-bound task receipt 封存；生产版仍需企业 IdP / 审批服务的独立签名 assertion，详见 `docs/deployment.md`。

## 证据定位

- 资源身份与角色 MCP：`agentteams/workers.yaml`
- Team / Human：`agentteams/team.yaml`、`agentteams/human.yaml`
- DAG、状态与前驱约束：`agentteams/refund-dag.yaml`
- Worker 操作合同：`agentteams/packages/*/config/AGENTS.md`
- Skill 合同：`skills/*/SKILL.md` 与 `skills/*/references/contract.yaml`
- AgentTeams 可导入包：`agentteams/dist/*.zip` 与 `agentteams/dist/SHA256SUMS`
- 运行等级：`agentteams/runtime-evidence/control-plane.json`、`teamharness-direct.json`
