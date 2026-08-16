# ProofMesh 安全策略与信任边界

ProofMesh 1.0 是可运行的 Agent 动作控制面参考实现。它会在隔离的 SQLite 商务靶场中真实改变退款余额、退款记录、工单状态和补偿状态；它**没有**连接企业生产支付、CRM、身份系统或 KMS。未经适配器加固与独立安全评估，不应直接接入生产账户。

## 安全不变量

1. Worker 只能看到自己的高层角色工具，不能读取或提交原始 Action Passport。
2. 所有有副作用的 MCP `tools/call` 都必须在执行瞬间通过外置公钥验签，并精确绑定 tenant、subject、workflow、tool、resource、参数摘要、policy、approval、金额、预算、次数和时效。
3. 审批者与请求者必须是不同认证主体；高风险审批必须携带独立审批服务签发的 JWS assertion，并绑定 subject、revision、challenge、plan / policy / context digest、完整 scope 与具体 action contract；记录审批只做 `WAITING_APPROVAL → AUTHORIZED`，不触发执行。
4. 同一逻辑操作以 `(tenant, workflow, tool, idempotency_key)` 唯一化。凭证尝试、租约与逻辑操作分离；接管前先向上游对账，结果不确定则进入 `UNKNOWN`，禁止自动重放。
5. 策略签发、网关执行回执、角色任务回执、Proof seal、商务快照 attestation 使用五把 ProofMesh 内部用途隔离的 Ed25519 密钥；第六类 Human approval 私钥只能位于控制面之外的审批服务，ProofMesh 仅持有其包外公钥与 issuer allowlist。工作流事件与网关回执分别形成哈希链。
6. Proof Verifier 不信任证明包内自带的根。它必须读取外部 trust bundle 与外部钉住的策略文件，验证由独立商务系统身份签名的新鲜快照，并跨证据核对租户、策略、审批、计划、工具参数、金额、余额和工单终态。
7. `PROOFMESH_ENV=production` 时强制 OIDC/JWKS、PostgreSQL GatewayStore 与 mTLS 远程签名代理；静态 bearer、SQLite GatewayStore、本地私钥或缺失材料都会启动失败。

## 威胁模型

| 威胁 | 当前控制 | 仍需生产环境承担 |
|---|---|---|
| Worker 越权调用别的工具 | 角色专属 MCP surface；生产 OIDC 校验签名、issuer、audience、时效、role、tenant；数据面 `/mcp` 只允许 gateway 身份 | 企业 IdP 实例、短期工作负载身份、网关 mTLS |
| 模型修改工具或参数 | Action Passport 精确绑定 tool/resource/args digest；执行时验签 | 上游接口保持同样的资源语义，不允许旁路 |
| 偷换信任根后重签整包 | Verifier 只接受命令行/部署侧外部 trust bundle | KMS/HSM、根轮换审批、透明日志和可信时间戳 |
| 签发键用途混淆 | trust bundle 同时限制 issuer 与 token types | 独立 KMS key policy 和硬件隔离 |
| 审批重放或范围漂移 | 外部签名 assertion、issuer allowlist、Bearer/assertion subject 交叉绑定、revision CAS、challenge/action/plan/policy/scope 绑定；Workflow、Gateway、包外 Verifier 三处独立验证 | 生产替换为企业 IdP / 审批服务，接入真实 MFA assurance、撤销、轮换和不可改留痕 |
| 并发重试导致重复退款 | 逻辑操作表、租约心跳、fencing generation、finalize CAS、稳定上游幂等键 | 上游必须支持按 operation id 对账与幂等 |
| 崩溃后副作用未知 | 过期租约先 reconcile；`UNKNOWN` 停止自动重放 | 人工对账队列、告警、SLA 与 Runbook |
| 金额拆分绕过单次上限 | `(tenant, workflow, currency)` 累计预算 | 客户/账户/时间窗级风控与额度服务 |
| 伪造成功日志 | 用途隔离的 task/gateway/proof 签名、双哈希链、商务上游 attestation | WORM 存储、外部审计流、可信时间 |
| Agent Pod 绕过网关 | ValidatingWebhook 精确 policy digest、digest-only image、NetworkPolicy | 支持 NetworkPolicy 的 CNI；上游只接受 gateway SPIFFE/mTLS 身份 |

## 身份与密钥

- API 默认 fail closed：没有配置 Bearer 身份时，状态变更接口返回 `503`。
- Orchestrator、Approver、Auditor、六个下游 Worker 角色和内部 Gateway 必须使用不同令牌；可选 Operator 仅承担只读运维职责，不能创建或推进案件。
- 仓库中的 `config/trust/action-issuers.json` 只用于可复现的开发靶场。`var/keys/` 是运行时私钥目录，已被 `.gitignore` 排除；发布包必须删除运行生成的私钥和数据库。
- 生产控制面不加载签发私钥，只能通过 mTLS 调用远程签名代理；返回 token 必须与请求 canonical payload 完全一致并通过包外公钥验签。密钥轮换需同时维护 `not_before`、`not_after` 与 `revoked` 元数据。
- 文件型实现仅用于开发；五把现存私钥必须是权限 `0600` 的普通文件。trust bundle 对 key entry 使用严格 schema，不接受字符串伪装的布尔值/时间或未知字段。
- `scripts/reference_approval_service.py` 仅是协议与密钥边界的合成参考签发器；其 `acr=urn:proofmesh:reference:synthetic-approval` 与 `amr=[reference-script]` 不代表企业 SSO 或 MFA。审批私钥不得进入 `build_runtime`、ProofMesh 容器、运行卷或发布包。
- `.env.example` 只有占位符。实际 `.env`、AgentTeams gateway consumer key、管理员口令和模型 API key 均不得进入 ZIP、截图、Proof Pack、日志或 Issue。

## 数据与证明材料

- 当前业务数据是合成数据，不含真实个人信息。
- 靶场终态由独立 commerce-sandbox key 签名，能证明“该隔离上游实例返回了这个快照”，但不等于真实支付/CRM 背书；生产适配必须由真实上游或可信审计代理签发。
- Proof 包会保存冻结计划、工具参数摘要、签名回执和最终业务快照。接入真实系统前必须定义字段级脱敏、保留期限、删除/法务保全策略与访问审计。
- 成功执行会生成签名 receipt；被拒请求当前写入 `gateway_denials` 审计表，但**不产生签名拒绝回执**。不要把数据库拒绝行表述为外部不可抵赖证明。
- SQLite 哈希链能发现内容和顺序篡改，不等同于不可删除存储或可信时间戳。生产环境应把链头周期锚定到独立透明日志，并将 Proof 包写入启用保留策略的对象存储。

## 部署要求

- 容器以 UID/GID `10001` 非 root 运行、根文件系统只读、移除 Linux capabilities，并只绑定回环地址作为本地评审默认值。
- Kubernetes webhook 必须保持 `failurePolicy: Fail`，且由集群管理员通过 namespace label 决定保护范围；工作负载不能通过删除自身标签绕过。
- NetworkPolicy 只是网络边界，不是身份。每个受保护上游必须验证 gateway 的工作负载身份并拒绝 Agent ServiceAccount。
- 对外提供服务前增加 TLS、请求体/证明包大小限制、速率限制、审计导出、备份恢复、密钥撤销、数据库高可用和告警。

## 已知边界

- 商务靶场是真实事务副作用，但不是外部支付或 CRM 的等价替代。
- Gateway 可用包外固定策略独立拒绝“高金额却伪标 `AUTOMATIC`”；它尚无独立风险事实源，因此“低金额但高风险”的人工审批要求仍需生产风控/决策服务独立判定。
- AgentDojo 629 组结果是“授权合同一致性回放”，不是模型提示注入 ASR、任务效用或端到端模型评测。
- 官方 AgentTeams 控制面能否运行、QwenPaw Worker 能否加载、TeamHarness 状态流以及模型驱动协同是不同级别的证据；报告必须逐项标注，不得用静态校验替代运行证据。
- 本实现尚未接受第三方渗透测试、形式化验证或生产流量验证。

## 漏洞报告

发现安全问题时，请使用托管仓库的私密安全报告渠道，并附最小复现、影响范围和建议缓解。不要在公开 Issue 中披露可直接利用的令牌、私钥、业务数据或攻击步骤。仓库正式发布后，应在此补充维护者安全联系方式与响应 SLA。
