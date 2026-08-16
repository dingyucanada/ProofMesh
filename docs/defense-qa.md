# 决赛答辩高压 Q&A

## 1. 这不还是一个退款 Demo 吗？

不是。退款是第一个验证靶场；第二个已实现的业务域是生产运维变更。退款域有自动、审批、补偿 3 条参考路径；运维域有低风险完成、高风险外部签名审批完成、健康失败补偿、对账不明 `UNKNOWN_MANUAL` 4 条路径。两域复用 Action Passport、AuthorizedToolCaller、GatewayStore、Execution Receipt 与包外 Verifier，但各自拥有独立 policy、业务状态和补偿语义。因此可迁移性已有运行证据，不只是把字段换名字；它仍是 reference workflow，不等于真实企业运维平台已接入。

## 2. 为什么说不是玩具？

因为它真的改变事务状态，并对生产故障语义负责：余额与退款记录原子提交；并发 revision 使用 CAS；长调用续租；旧进程用 fencing 失去提交权；崩溃接管先上游对账；结果未知停止自动重放；CRM 失败后真实补偿；独立 Verifier 回查余额、退款和工单。界面只是这些机制的观察窗。

## 3. 没有真实模型，怎么叫多 Agent？

控制面能力与模型推理能力必须分开。七个身份、角色工具、状态机、task receipt 和 AgentTeams 资源是确定的；模型驱动 QwenPaw 自主认领仍必须有真实模型凭证和原始 TeamHarness/Matrix 记录。当前 L1/L2 已验证，L3 已有 Team Active 与跨 Leader/Worker 的 direct MCP 生命周期实证，但不声明 L4。这样避免把控制面直调冒充模型自主协同。

## 4. AgentTeams 是不是只写进了 PPT？

不是。交付锁定官方 v1.2.0-beta.1，包含 Human、7 qwenpaw Workers、Team 定义、退款 DAG、7 个官方根目录 ZIP、bootstrap 和严格 validator；每个 Worker 绑定唯一角色 MCP 与工具。我们针对 beta Manager REST 未映射 `workerMembers` 的缺口构建了最小补丁 Controller；补丁镜像上 Team 已 `Active`，Leader 与六个 Worker ready，7/7 Worker Running。

我们没有用旧 inline Team 绕过，因为那会重复创建 Worker，破坏解耦身份。补丁只让 Create REST DTO 接收 `workerMembers`，非空时走既有解耦控制器，空时完全保留 legacy 校验/映射；三个 Go 合同测试已通过，镜像已从锁定源码构建并运行。随后 direct MCP 在同一 Project 下跨 Leader 与 ticket-intake 两个真实容器完成 `create → plan → delegate → ack → submit → check → accept`。这证明控制面生命周期，不证明 placeholder provider 下的模型自主协同。

## 5. 审批是不是万能放行？

不是。审批只能在 `WAITING_APPROVAL` 状态记录，要求与请求者不同的已认证主体、当前 revision、足够理由、精确 scope、plan digest 与 policy digest 一致。成功后只进入 `AUTHORIZED`，不会执行副作用。Executor 还要单独调用；网关在执行瞬间再次验证 approval digest。

高风险审批现在必须携带包外 approval-service Ed25519 JWS。断言精确绑定 issuer、subject、workflow / project / tenant、当前 revision、context / policy / plan、理由摘要、两项具体动作参数、金额、币种与短时窗口；API Bearer subject 与 assertion subject 必须一致。Workflow、Gateway 和包外 Verifier 三处分别验签，Gateway 对超过固定自动金额阈值却伪标 `AUTOMATIC` 的 Passport 直接拒绝。审批服务 issuer 必须出现在包外 trust bundle 的独立 allowlist；控制面和 `build_runtime` 不持有审批私钥。

参考证据中的断言由独立脚本/目录中的 synthetic key 签发，`acr=urn:proofmesh:reference:synthetic-approval`、`amr=[reference-script]`，不冒充企业 SSO 或 MFA。生产必须替换为真实企业 IdP / 审批服务及其 assurance claim。当前 Gateway 能独立重算金额门槛，但没有独立风险评分事实源；“低金额但因风险分要求人工”的控制面攻陷绕过仍是待生产化的残余边界。

## 6. 为什么不承诺 exactly-once？

跨进程和外部系统无法只靠本地数据库数学保证 exactly-once。ProofMesh 提供可实现的语义：稳定 logical operation id、上游幂等、fenced owner、租约、对账和 UNKNOWN 停止重放。只有上游也支持按 operation id 去重/查询时，才能把重复副作用风险压到协议边界内；否则诚实地进入人工对账。

## 7. 进程在扣预算后、上游执行后、写 receipt 前崩溃怎么办？

新的等价凭证接管同一逻辑操作，不再次扣预算。过期租约后先用稳定 operation id 查询上游：已成功则用查询结果完成封存；确认不存在才重派；无法判断则 `UNKNOWN`。finalize 还需 owner/generation CAS，旧进程恢复也不能覆盖新 owner。

## 8. 攻击者替换 Proof 包里的公钥，再把整包重签呢？

Verifier 不使用包内根。trust bundle 是命令行/部署侧外部输入，并严格验证 public key、issuer、token type、有效期和撤销字段。攻击者能重签自己的包，但无法让外部 pinned key 接受。

## 9. 如果每个签名都有效，但把金额或工单拼错呢？

这正是语义 Verifier 与“只验签”的差别。Verifier 跨 request、plan、approval、task receipt、gateway receipt 和由 Commerce 独立 key 签名的最终 snapshot，核对 tenant、workflow、tool、resource、amount、currency、order/ticket/refund 关系与余额公式。Proof Sealer 无权签 Commerce attestation；签名正确但语义不一致仍然失败。

## 10. 拒绝请求也有签名证明吗？

当前没有。成功执行产生签名 execution receipt；拒绝写入 `gateway_denials` SQLite audit row。报告明确区分，未把拒绝行包装成外部不可抵赖回执。生产版可以增加独立 denial signer/透明日志，但必须考虑拒绝洪泛和存储成本。

## 11. AgentDojo 的 100% 是不是“安全率 100%”？

不是。100% 指在 2,159 个固定 user ground-truth 合同上全部接受，并对工具/参数/上下文错配全部拒绝；它不运行模型，不测提示注入 ASR 或 task utility。报告给出 Wilson 区间，并将 placeholder 保留为符号值。我们证明授权网关符合合同，不证明模型不会生成危险计划。

## 12. 为什么用 TraceBackend，不执行 AgentDojo 真实外部工具？

本实验测的是控制面授权一致性和幂等副作用计数。TraceBackend 是确定性、无外部账号的效果接收器，能精确判断是否重复派发。要测端到端 utility/ASR，必须在独立环境运行官方模型 runner，并与本实验分开报告。

## 13. SQLite 怎么能叫企业级？

SQLite 是可审计参考靶场，不是最终 HA 存储。项目现已增加 PostgreSQL GatewayStore 参考实现，覆盖事务锁、租约/fencing、reconcile 与 `UNKNOWN`；生产容器依赖也已分层。但它尚未连接真实 PostgreSQL 集群，工作流/业务/ledger 仍有 SQLite 边界，也未完成迁移、备份恢复和 HA 演练。因此只能说“生产迁移接口已实现”，不能说“生产数据库已验证”。

## 14. Saga compensation 就等于 rollback 吗？

不是所有外部副作用都能物理回滚。这里的补偿是新的受控业务动作：将退款标记为 `COMPENSATED`、恢复可退款余额、确保工单开放。它有独立 Passport、receipt 和 postcondition。若补偿失败，系统 `BLOCKED` 并升级人工，而不是写一条“已回滚”日志。

## 15. 为什么不用 RAG？是否不满足赛题？

官方要求在记忆、知识库 RAG、共享状态、轨迹可观测四项中至少实现两项。ProofMesh 实现共享状态、脱敏记忆和可观测回执/事件/指标，因此不需要为了 Logo 强行加 RAG。真实客服知识检索可在 Investigator Skill 后接，但授权控制面不应依赖非确定性检索结论。

## 16. Kubernetes NetworkPolicy 就能防 Worker 旁路吗？

不能单独防。NetworkPolicy 限制网络路径；Admission 防止工作负载自行改策略摘要/镜像；上游工具还必须验证 gateway 的 mTLS/SPIFFE 身份并拒绝 Agent ServiceAccount。三者共同形成边界，标签本身不是凭证。

## 17. Skills 的复用在哪里？

七个 Skill 都有闭合输入/输出 schema、调用状态、依赖 MCP、失败策略、安全边界、证据要求和错误码；Worker 包使用官方目录结构，能独立版本和分发。退款与运维两个业务域已复用同一授权、恢复和验真协议，证明它们不是一次性 prompt；当前仍缺外部使用者和云端实跑证据。

## 18. 商业价值如何量化？

当前能直接测的工程 KPI 是重复副作用、非法错误通过、人工门禁、补偿恢复和审计复算时间。商业 KPI 需在真实历史工单盲测：自动闭环率、人工触达率、重复退款率、平均处理时长、异常恢复时长和审计取证时间。PPT 中应将目标值标为待试点验证，不能把工程回放推成财务收益。

统一定义、数据源、公式与两周试点流程已写入 `docs/pilot-value-scorecard.md`。年度风险避免 / 审计效率公式只在企业提供工单量、基线异常率、损失和人力成本后代入；不能凭空填一个“节省 XX%”。

## 19. 最大已知风险是什么？

三个：一是上游缺少稳定幂等/对账接口时无法消除未知副作用；二是真实模型驱动 AgentTeams 协同尚缺模型凭证和端到端业务 trace；三是尚无真实 sandbox、授权历史工单和独立复现形成外部证据。我们分别用 `UNKNOWN` fail-closed、`modelDriven=false` 和六项 Claim 全部 `PENDING` 约束宣传口径。

## 20. 为什么这个项目可能赢？

它不是把七个 Agent 排成流水线，而是补上赛题“Demo → Production”最难的执行层：谁能做什么、这一次能做多少、重试如何不重复、失败如何恢复、系统之外如何验证。每个主张都有可运行代码、负向测试和诚实边界，评委可以当场篡改并看到系统拒绝。

## 21. 为什么没有直接堆阿里云官方 Skills？

官网明确推荐项目和云产品不按使用数量评分。ProofMesh 只选择一个与证据链直接相关的官方 Skill 接入点：`alibabacloud-sls-query`，用于对脱敏 workflow / gateway / verifier 事件做只读检索；它只需要 GetIndex / GetLogsV2 权限，无退款写权限，也不能决定 Proof 是否有效。当前只有采用契约，没有阿里云账号与运行日志，因此不声称云端实跑。权限和复赛验收材料见 `docs/aliyun-skill-adoption.md`。

## 22. 如果你是评委，现在会给多少分？

工程准备度内部自评约 88/100：场景 22/25、多 Agent 20/25、Skill 22/25、工程 20/20、开源 4/5。依据是两个业务域、162 项测试、Team Active/direct lifecycle、生产迁移参考适配与供应链门禁。这不是主办方评分：`modelDriven=false`，production adapters 未连接真实设施，Stripe/HubSpot 仅 sandbox-ready，公开仓库、授权试点和独立复现尚未完成，六项外部 Claim 全部 `PENDING`。所以当前不能严谨自称 95+、前三或冠军。见 `docs/judge-scorecard.md`。

## 23. 生产适配器和 Stripe/HubSpot 代码不是已经解决外部证据了吗？

没有。OIDC/JWKS、PostgreSQL GatewayStore、remote signer 和供应链 provenance 是生产边界的参考实现，尚未连接真实企业 IdP/KMS/数据库；Stripe 与 HubSpot adapter 已实现真实 API 语义和密钥护栏，但没有测试账户 run。只有保存去敏环境标识、请求/对账摘要、上游回执、失败样本、commit 和独立验证结果，才可把对应 Claim 从 `PENDING` 升级。完整步骤见 `docs/95-plus-evidence-runbook.md`。
