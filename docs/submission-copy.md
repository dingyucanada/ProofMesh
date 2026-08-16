# 初赛提交文案

## 项目名称（20 字以内）

ProofMesh：可信动作控制面

## 作品简介（500 字以内）

ProofMesh 面向企业客服与运维团队，是多 Agent 高风险动作可信控制面。它把计划冻结为 Action Passport，绑定主体、租户、工具、参数、策略、审批和时效；以租约、幂等及上游对账避免盲目重试，未知即停。7 个 Agent 与 7 个 Skill 以 AgentTeams 完成拆解、交接、审批、退款/补偿、验真和脱敏记忆；退款 3 路径与运维 4 路径复用 Gateway、签名回执和包外 Verifier。已完成 Team Active、direct lifecycle 7/7、162 项测试、629 组授权回放、240 条故障盲测、Banking77 3,080 条分流和 CFPB 240 条 no-write shadow；`modelDriven=false`，公开数据不冒充客户试点。不用 RAG，已实现共享状态、脱敏记忆、轨迹可观测；官方 SLS Skill 仅完成采用契约。Apache-2.0 开源，生产设施与外部 Claim 待验证。

## 一句话定位

AgentTeams 承载多角色协作；ProofMesh 让退款与运维变更等副作用在精确授权下可恢复执行，并由 Proof 包外信任材料独立复验。

## 三项创新点

1. **Proof-carrying action**：执行时精确验签的 Action Passport，不把底层权限交给模型。
2. **Reconcile-before-retry**：logical operation 与 credential attempt 分离，崩溃后先对账，不确定即停止重放。
3. **Semantic Proof Verifier**：使用包外信任锚，跨审批、计划、回执与业务终态做语义验真。

## 当前进展

- 代码与本地三条业务路径：已完成；
- 第二业务域生产运维变更四条路径：已完成；
- 完整测试：`162 passed`；
- 7 个 AgentTeams Worker manifests、1 个 Team manifest、1 个 Human manifest、7 个 Skill ZIP 与 bootstrap：已完成；
- AgentDojo 629 case 授权回放与报告：已完成；
- 上下文增强：明确不使用 RAG；共享状态、脱敏记忆与轨迹可观测已实现，满足四项能力至少实现两项的要求；
- 阿里云官方 Skill：`alibabacloud-sls-query` 的只读采用、最小 RAM 权限、脱敏与失败边界契约已完成，尚无阿里云账号运行日志，不声称云端实跑；
- 官方 AgentTeams 控制面运行证据：已实跑可复现基线为 `v1.2.0-beta.1` / commit `78d0ced...`；Human Active、7/7 Worker Running、7/7 health、Team Active，以及跨 Leader / Worker 的 direct MCP `create_project → plan_dag → delegate_task → ack_task → submit_task → check_task → accept_task_result`；该证据不等于模型自主协同；当前 stable `v1.2.2` 仅为迁移目标，尚未验证；
- 真实模型驱动 QwenPaw 自主协同：尚未运行，不与静态/控制面证据混淆；
- OIDC/JWKS、PostgreSQL GatewayStore、远程 signer 与供应链 provenance：参考实现已完成，尚未接真实企业设施；
- Stripe/HubSpot adapter：sandbox-ready，尚无真实测试账户 run；
- 外部证据：模型、vendor sandbox、授权历史试点、独立复现、公开仓库/身份、生产设施六项 Claim 全部 `PENDING`；
- 工程准备度内部自评约 88/100（22/20/22/20/4），不是主办方评分；95+ 只能由真实外部证据推动。

## 提交链接

- 代码仓库：`[提交前填写]`
- Demo 视频：`[提交前填写]`
- 在线体验：`[可选，提交前填写]`
- 开源协议：Apache-2.0
