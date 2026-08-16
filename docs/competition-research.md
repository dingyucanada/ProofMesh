# GOAI「新智基座 Agent Infra」赛题研究与获奖策略

> 版本基准：2026-08-12。赛程可能调整，提交前应再次核对[官方赛道页](https://www.goaihz.com/tracks?track=infra)与组委会通知。

## 1. 官方要求的本质

官方把赛道目标写得非常明确：面向企业级复杂任务构建多 Agent 基础设施与协同系统，推动 Agent “从 Demo 走向 Production”；作品需设计不少于 3 个不同职能 Agent，并覆盖任务拆解、上下文传递、工具调用、结果验证、执行证据、安全审计、审批/回滚与经验沉淀。

三个不能规避的要求：

- **AgentTeams 必选**：不是在 PPT 写一个名字，而是把角色编排、任务拆解、上下文、协同执行和状态追踪映射到真实框架能力；
- **Skill 必选**：要有输入输出、调用条件、依赖工具、失败处理、安全边界、验证方式、复用、版本和分发；
- **工程证据优先**：复赛要求可执行 AgentTeams 包与可运行 Demo/视频，评审要看日志、Trace、Metrics、运行报告、权限、密钥、审批、回滚、降级和审计。

官方还明确：推荐云产品不按数量加分，关键是必要性、接口契约、可替换性、权限边界、闭环证据与迁移成本。因而“堆产品 Logo”不是获奖路线。

## 2. 为什么选智能客服退款

官方开放选题的“智能客服自主闭环”示例直接包含：多渠道工单、意图分级、退款/换货/账户变更、结果核验、客户确认和知识沉淀。ProofMesh 选择退款不是随便造一个 Demo，而是把官方建议场景中最难、最可审计的一类副作用做深：

- 退款有真实金额与累计预算，错误不能靠重新生成文本弥补；
- 工单、订单、退款跨系统，天然需要上下文一致性；
- 高风险金额需要职责分离与人工审批；
- 支付成功而 CRM 失败是标准分布式 Saga；
- 最终结论必须回查余额和工单，而不是相信 Agent 自报成功。

更重要的是，参赛成果不是“退款机器人”，而是可迁移到换货、账户变更、理赔、授信、运维修复和研发发布的 **Agent Action Control Plane**。退款是第一个苛刻验证靶场；生产运维变更已作为第二业务域运行四条路径，证明协议迁移并非只停留在叙事层。

## 3. 评审权重与 ProofMesh 对位

| 官方维度 | 权重 | ProofMesh 的得分抓手 | 必须展示的证据 |
|---|---:|---|---|
| 场景价值与行业复制 | 25% | 高风险客服副作用；Action Passport/Verifier 可跨行业复用 | 退款事故链、迁移矩阵、[试点 KPI 计分卡](pilot-value-scorecard.md) |
| 多 Agent 协同与闭环 | 25% | 7 角色、CAS 状态机、Human pause/resume、Saga 补偿、独立验真 | AgentTeams DAG、task receipt、异常分支、职责隔离 |
| Skill 工程体系 | 25% | 7 个官方布局 Skill/ZIP，闭合 schema、错误码、安全边界 | 每个 Skill 的合同、版本、校验与分发包 |
| 工程、安全、运行验证 | 20% | MCP 双面、Action Passport、逻辑操作/租约/对账、外部信任锚、K8s admission、629 case 回放 | 实跑 Demo、Proof Valid、测试、报告、容器与部署材料 |
| 开放/开源 | 5% | Apache-2.0、AgentDojo 归属、可复现脚本、接口文档 | LICENSE、THIRD_PARTY、README、公开仓库 |

## 4. 差异化：评委为什么不应把它归为普通 Agent Demo

普通方案常停在“七个角色依次输出文本”。ProofMesh 的差异是把协同结论变成**执行许可与可验证业务结果**：

1. Worker 只调用高层任务工具，底层 Action Passport 对模型不可见；
2. 许可证精确绑定工具、资源、参数、金额、预算、策略与审批；
3. 审批只把状态从 `WAITING_APPROVAL` 变为 `AUTHORIZED`，不会把审批按钮偷偷做成执行按钮；
4. 重试围绕逻辑操作，用 owner/generation/lease/fencing 与上游对账避免重复退款；
5. CRM 失败后执行真实补偿，并由另一个 Agent 回查余额恢复和工单开放；
6. Proof Verifier 不信任包内 trust root，能拒绝“整包换根重签”和“签名正确但金额/工单拼错”；
7. 公开 AgentDojo 629 case 回放提供可复现合同指标，不把合成样例冒充模型 ASR。

一句话获奖主张：

> 目标架构中，AgentTeams 负责多 Agent 协作；ProofMesh 负责证明动作被正确的人、在正确范围内授权并可恢复执行，并且失败后回到了可验证状态。

## 5. 官方技术项覆盖

| 官方项 | 当前实现 | 判断 |
|---|---|---|
| 至少 3 Agent | 7 个 Worker Agents；另有 1 个 Human approver；Team 是编排资源，不计作 Agent | 超额覆盖 |
| Agent Identity 清单 | `docs/agent-identity-register.md`（附录 A 八字段）+ `human.yaml`、`workers.yaml`、`team.yaml`、manifest/SOUL/AGENTS | 已覆盖 |
| AgentTeams 基点 | 已实跑基线锁定 v1.2.0-beta.1 / commit `78d0ced...`；资源、ZIP、bootstrap、TeamHarness DAG；针对 Manager REST 缺失 `workerMembers` 映射提供 commit/hash 锁定的最小兼容补丁 | Team Active 与 direct lifecycle 已证；当前 stable v1.2.2 仅为迁移目标，尚未验证；[五项官方映射与迁移门禁](agentteams-official-mapping.md) |
| Skill | 7 个 Skill，输入/输出 schema、调用条件、依赖、失败、安全、错误码 | 已覆盖 |
| MCP | 7 个角色端点 + 内部动作网关；JSON-RPC initialize/list/call | 已覆盖 |
| 上下文能力至少 2 项 | 共享状态 + 记忆存储 + 轨迹/回执/指标 | 不依赖 RAG 仍满足 |
| 可观测 | 事件链、task/gateway receipt、Prometheus metrics、OTel GenAI 字段 | 已覆盖，生产后端待接 |
| 官方云 Skill | `alibabacloud-sls-query` 的只读采用 / RAM / 脱敏契约 | 设计已覆盖；无云账号与运行日志，不冒充实跑 |
| 审批/回滚 | digest-bound approval + Saga compensation + postcondition verify | 已覆盖 |
| 运行验证 | 业务 E2E、并发/崩溃测试、629 case 合同回放、Verifier 负向测试 | 已覆盖 |
| 开源 | Apache-2.0、第三方归属、构建/测试/部署说明 | 已覆盖 |

## 6. 当前证据分级

为了避免评委发现“包装”，所有主张采用四级标签：

- **L1 静态可审计**：YAML、Skill、schema、ZIP、架构和源代码；
- **L2 本地确定性实跑**：三条真实 SQLite 业务路径、自动化测试、Proof Verifier；
- **L3 官方控制面实跑**：官方 AgentTeams Manager/Controller 进程、资源 CR、Worker/TeamHarness health 与直接 MCP project/task 生命周期；
- **L4 模型驱动协同实跑**：真实模型凭证下由 QwenPaw Leader/Workers 自主认领并完成任务。

L4 没有原始日志时绝不宣称“七 Agent 自主运行成功”。这份诚实边界本身就是安全与工程成熟度证据。

当前 L3 的精确边界是：锁定官方源码的最小补丁已通过三个 Go 合同测试并构建为单层兼容镜像；Human、七个 Worker 与 TeamHarness health 已实测，七成员 Team=`Active`。同一 Project 下的 direct MCP `create → plan → delegate → ack → submit → check → accept` 已跨 Leader / Worker 容器完成。它证明控制面状态流，不证明 placeholder provider 下的模型自主协同；可选 Matrix artifact publication 仍有 ATB-007 open finding。

## 7. 赛程与提交策略

官方赛程（北京时间）：

| 阶段 | 时间 | 提交 |
|---|---|---|
| 初赛 | 7.16–8.16 | 500 字内作品简介、方案 PPT/PDF；AgentTeams 代码包可选 |
| 初赛评审 | 8.17–8.24 | Top 30 进入复赛 |
| 复赛 | 8.25–9.3 | 更新方案、可执行 AgentTeams 包、可运行 Demo/视频 |
| 复赛评审 | 9.4–9.10 | Top 15 进入决赛 |
| 决赛 | 9.22 | 现场路演 PPT、现场 Demo、最终仓库/工程材料 |
| GOAI DAY | 9.23 | 颁奖、展示与生态对接 |

截至 2026 年 8 月 13 日，官网列示初赛于 8 月 16 日截止；具体提交时刻以报名系统和组委会最新通知为准。当前策略不是继续无边界加功能，而是：

1. 先锁定真实、可复现、无夸张的代码与指标；
2. 初赛 PPT 用“行业痛点 → 核心机制 → 三条实跑证据 → 评分映射 → 复赛路线”完成闭环；
3. 可选代码包主动提交，利用超出初赛强制要求的工程完成度拉开差距；
4. 复赛只增加真实模型驱动 AgentTeams 证据、视频、外部存储/网关适配，不推翻架构；
5. 决赛准备断网可演示的本地路径，同时保留 L3/L4 原始日志与一键验真。

## 8. 仍会丢分的地方与行动

| 风险 | 影响 | 行动 |
|---|---|---|
| 缺少真实模型 API 凭证 | 不能完成 L4 QwenPaw 自主协同证据 | 复赛前配置合规模型账号，跑官方 E2E 风格任务并保留 Matrix/TeamHarness 原始记录 |
| placeholder provider 未产生模型协同 | Team 与 direct MCP lifecycle 已实证，但不能证明模型自主规划 / 认领 | 接入合规模型凭证，跑七角色业务 trace，并保留 Matrix/TeamHarness 与 ProofMesh 双账本 |
| 生产 adapters 仍是参考实现 | 评委可能误以为 OIDC/PostgreSQL/remote signer 已连接真实设施 | 明示“代码已实现、环境未接入”；复赛连接企业测试设施并保留去敏部署与故障证据 |
| Stripe/HubSpot adapter 未实跑 | sandbox-ready 不等于已接真实测试账户 | 用最小权限测试凭证跑成功、补偿、UNKNOWN、重复和参数漂移路径，保存上游对账回执 |
| 无第三方安全评估 | 安全主张仍是自证 | 发布威胁模型，邀请独立审查，记录整改前后证据 |
| 价值 KPI 主要是推演 | 25% 场景价值可能不足 | 按[统一计分卡](pilot-value-scorecard.md)找 1 家客服/电商团队做 200–500 条历史工单 shadow / sandbox 盲测 |
| 初赛叙事过技术化 | 评委可能看不见商业价值 | 首页先讲“防错付、可追责、可恢复”，技术细节下沉到证据页 |

## 9. 最终判断

这个方向有获奖潜力，因为它同时命中官方推荐的智能客服场景、AgentTeams/Skill 强制项和“Demo → Production”的赛道主轴，而且选择了多数参赛方案最薄弱的执行安全与证明层。真正的竞争优势不在代码行数，而在于把每一项高分主张都变成评委可以当场复现、篡改、拒绝和验真的证据。

按官网 25/25/25/20/5 维度形成的**工程准备度内部自评约为 88/100（22/20/22/20/4）**：退款三路径与生产运维四路径证明协议跨两个业务域复用；162 项测试、生产迁移参考适配、Team Active 与 direct MCP lifecycle 提升了工程完整度。这不是主办方评分：`modelDriven=false`，生产 adapters 未接真实设施，Stripe/HubSpot 仅 sandbox-ready，且模型实跑、vendor sandbox、授权历史试点、独立复现、公开仓库/身份、生产设施六项外部 Claim 全部 `PENDING`。95+ 只能靠这些外部证据，不能靠继续增加本地代码或合成样本。逐项口径与门槛见 [judge-scorecard.md](judge-scorecard.md) 和 [95+ evidence runbook](95-plus-evidence-runbook.md)。
