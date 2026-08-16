# 获奖方案总纲：ProofMesh Agent Action Control Plane

## 核心命题

企业不会因为 Agent 能写出正确答案，就允许它直接退款、封号、改配置或发布代码。真正缺失的基础设施是：让每个副作用同时具备可验证授权、可恢复执行语义、失败恢复和 Proof 包外验真。

ProofMesh 的产品定义：

> 面向多 Agent 企业动作的 Proof-Carrying Execution 控制面。

AgentTeams 承载角色协同；ProofMesh 位于协同系统与企业工具之间，把 Agent 的计划变成短期、精确范围的 Action Passport，并将任务、执行、补偿和终态封存为可独立复算的 Proof。当前退款域三路径与生产运维变更域四路径已复用同一控制协议；AgentTeams Team 已 `Active`，direct MCP project/task 生命周期已跨两个 Worker 容器跑通，但 `modelDriven=false`，模型自主协同仍未验证。

## 两个可运行业务域

智能客服退款闭环：工单进入后，由 Orchestrator、Intake、Investigator、Policy、Executor、Verifier、Memory 七角色协议通过 L2 确定性角色 API 协作；高风险计划暂停等待外部 Human；执行涉及支付与 CRM；下游失败触发补偿；最终必须重新读取退款、余额和工单状态。

选择这一场景有三层价值：

- 官方赛题明确把退款列为智能客服闭环示例；
- 金额、审批、跨系统和补偿让安全机制可被客观验证；
- 控制协议可迁移到理赔、授信、账户变更、运维修复与研发发布。

为避免“可迁移”停留在口号，项目已加入第二域 `production-operations-change`：低风险变更完成、高风险签名审批完成、健康检查失败补偿、对账不明 `UNKNOWN_MANUAL`。运维域使用独立 policy、健康检查和回滚语义，但复用 AuthorizedToolCaller、Action Passport、GatewayStore、Execution Receipt 与包外 Verifier。它是确定性 reference workflow，不冒充真实企业运维平台。

## 六个决定性创新

### 1. 模型不可见的 Action Passport

Passport 由控制面内部签发，Worker 只看到高层角色工具。它精确绑定 tenant、subject、workflow、tool、resource、args digest、context、policy、approval、金额、预算、次数和 TTL；网关在执行瞬间使用外部 trust bundle 验证。

### 2. 审批与执行物理分步

Human approval 只能把 `WAITING_APPROVAL` 原子转为 `AUTHORIZED`，必须绑定当前 revision、plan digest、policy digest 和完整 scope。它不会调用任何业务工具；Executor 必须单独领取任务。

### 3. 逻辑操作而非 token 的幂等

logical operation 与 credential attempt 分离，使用 owner UUID、fencing generation、lease heartbeat 和 finalize CAS。旧 token 过期后可用新的等价 token 恢复，但合同任一字段变化都会被拒绝。

### 4. 对账优先的崩溃恢复

租约过期后先用稳定 operation id 向上游查询。成功则封存原结果，确认不存在才重派；不确定进入 `UNKNOWN`，永不自动赌第二次。这是对“exactly once”边界的工程化诚实回答。

### 5. 可验证 Saga，而不是写日志式回滚

支付成功、CRM 失败时执行独立授权的 compensation，恢复余额并保持工单开放；Verifier 再读取业务数据库确认 postconditions。补偿失败会安全阻断，不会把事件名写成“rollback completed”。

### 6. 外部信任锚 + 跨证据语义 Verifier

Verifier 不信任 Proof 包里的根；外部 trust bundle 与 pinned policy 是独立输入。除验签和链外，还核对 approval、plan、tool args、金额、币种、订单版本、余额公式、退款/工单关系和 memory 状态，能拒绝“签名正确但业务拼错”。

## 可运行证据

- 三条事务业务路径：自动退款、高风险审批、CRM 故障补偿，最终分别 `COMPLETED / COMPLETED / COMPENSATED` 且 Proof Valid；
- 四条生产运维变更路径：`COMPLETED / COMPLETED / COMPENSATED / UNKNOWN_MANUAL`；三条已知终态 Proof 可包外复验，未知终态严格 fail closed；
- 自动化负向测试：角色越权、跨租户、并发审批、revision 过期、key usage、撤销/时效、预算拆分、长调用租约、崩溃对账、UNKNOWN、语义篡改；
- AgentDojo 0.1.35/v1：629 cases、2,159 user calls 的授权合同回放；合法接受 2,159/2,159，参数/工具/上下文错配错误通过均 0/2,159，副作用重复 0；
- AgentTeams：1 Human manifest、7 个 qwenpaw Worker manifests、1 个 Team manifest、7 个官方结构 Skill ZIP、bootstrap 与严格 validator；运行证据为 Human Active、7/7 Worker Running、7/7 health、Team=`Active`，以及跨 Leader / Worker 的 direct MCP 七步 lifecycle；modelDriven=false；
- 工程证据：完整测试 `162 passed`；FastAPI + MCP、Prometheus、Kubernetes fail-closed admission；OIDC/JWKS、PostgreSQL GatewayStore、remote Ed25519 signer、SBOM/provenance 为生产迁移参考实现；
- Vendor 边界：Stripe 测试退款与 HubSpot 开发者测试 Ticket adapter 已 sandbox-ready，但没有真实账户运行，不能称外部 sandbox 已验证。

## 比赛叙事

PPT 不从架构图开始，而按以下顺序：

1. 一次重复退款/错范围审批如何发生；
2. 为什么多 Agent 协同本身不能解决执行可信；
3. ProofMesh 的“一张 Passport + 一个 logical operation + 一份外部可验 Proof”；
4. 高风险停门、审批不执行、Saga 补偿三段实跑；
5. 629 case 与负向测试；
6. AgentTeams/Skill 的真实映射；
7. 生产迁移与证据边界；
8. 开源与试点计划。

## 复赛到决赛路线

### 复赛前

- 使用真实模型凭证完成 QwenPaw Leader/Workers 的 TeamHarness 自主任务，保留 Matrix、project/task、Worker 日志与 ProofMesh 双账本关联；
- 接入一个真实沙箱业务 API，验证稳定 operation id、幂等与 reconciliation；
- 取得数据所有方授权，用 200–500 条严格脱敏历史工单做预注册 shadow/sandbox 盲测；
- 发布公开 GitHub tag 并由至少一位独立复现者从干净环境验收；
- 录制 4 分钟 Demo，并提供一键验真与断网备用路径。

### 决赛前

- 将现有 remote signer/PostgreSQL adapter 接到真实 KMS/数据库，完成迁移、备份恢复和 HA 演练；
- 至少一家团队的历史工单盲测，分开报告自动闭环率、误放行、误阻断、人工触达、MTTR 和审计耗时；
- 第三方安全审查、故障演练和链头外部锚定；
- 将 Action Passport schema、Verifier 和 reference gateway 做成独立开源组件。

## 不可夸大的边界

当前业务域是 reference workflow，不是生产支付或生产运维；AgentDojo 结果不是模型 ASR/utility；production adapters 尚未连接真实 IdP/KMS/PostgreSQL；Stripe/HubSpot 仅 sandbox-ready；AgentTeams Team Active/direct 7/7 不等于模型自主协同。模型实跑、真实 vendor sandbox、授权试点、独立复现、公开仓库/身份、生产设施六项外部 Claim 当前全部 `PENDING`。约 88/100 是 22/20/22/20/4 的工程准备度内部自评，不是官方成绩；95+ 只能靠上述外部证据。
