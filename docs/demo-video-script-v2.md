# ProofMesh 3–5 分钟录屏与材料升级门槛（v2）

> 目的：用一条可复现的业务证据链回答“企业为什么敢让 Agent 退款”，并规定未来何时才允许升级 PPT / 白皮书主张。本文是录屏执行稿和材料变更门禁，不代表下列未来证据已经完成。

## 1. 结论与成片规格

- **建议主版：4 分 20 秒**。当前 `demo-script.md` 的 4 分 30 秒结构可压缩；删除单独的重试原理页，把幂等、fencing 和 `UNKNOWN` 合并到 Saga 讲解中。
- **必须保留的三个画面**：高风险案件停在 `WAITING_APPROVAL`；审批后仍停在 `AUTHORIZED`；¥90 案件到 `COMPENSATED` 且 `PROOF VALID`。
- **只能作为短证据卡的内容**：当前验证清单、AgentDojo 629 cases / 2,159 calls、AgentTeams 当前运行边界。不要现场滚终端日志或朗读指标表。
- 画面 16:9、1080p、浏览器 100% 缩放；鼠标路径预演，敏感字段区域不进入画面。全片只用当前控制台、两张证据卡和结尾页，不切代码编辑器。
- 每个结论出现时，至少同时展示一个可核对字段；禁止用旁白替代证据。

## 2. 录制前门禁（不进入成片）

1. 从干净状态启动服务并预跑完整流程；固定浏览器窗口大小与 100% 缩放。
2. 九个本地角色 token 已加载，但折叠“角色凭证”；画面中不得出现 `.env`、模型 key、AgentTeams 管理员口令或 gateway consumer key。
3. `/health`、`/ready` 正常；运行提交前的最新全量测试并记录 `162 passed`；退款域三条 reference Proof 与运维域三条已知终态 Proof 能用包外 trust bundle 与 pinned policy 复验；运维 `UNKNOWN_MANUAL` 路径必须拒绝伪造成功 Proof。
4. 为录屏准备三张全屏证据卡：
   - `artifacts/public-benchmark/authorization-contract-replay.md` 的四个数字与“非模型评测”声明；
   - `agentteams/runtime-evidence/` 的去敏状态摘要，必须与录制当日事实一致。
   - 两域与 Claim 摘要：`refund 3 paths · operations 4 paths · 162 passed · external claims 0/6 VERIFIED`。
5. 固定业务工单只能使用一次。正式录制前若需重置，停止服务后执行 `make clean-runtime`；需要保全的运行证据不得清理。
6. 录制一次彩排并计时。若任一步未达到本文“必须看到”的字段，停止录制并排障，不能后期用字幕伪造状态。

## 3. 4:20 主版逐秒脚本

### 0:00–0:25｜问题：¥90 退款到底完成了吗？

**画面**：控制台首页，已选“下游故障与补偿”，暂不点击。

**旁白**：

> AI 会调用退款工具，不等于企业敢让它退款。支付成功、CRM 失败时，盲重试可能退两次，不重试又会留下永久错账。ProofMesh 不替 Agent 做决定；它把每次副作用变成一份精确授权、一个可恢复操作和一份可由 Proof 包外信任材料复验的证明。

**必须看到**：页面标题“每个副作用，先有许可证，再有证明”；左侧 ¥90 Saga 用例；右上 `external trust` 运行状态。

### 0:25–1:28｜高风险先停门：审批不等于执行

**画面动作**：

1. 切换“高风险人工审批”，点击“创建隔离案件”；
2. 点击“推进至下一门禁”；
3. 在 `WAITING_APPROVAL` 停留约 12 秒，指向状态、`next task`、金额、Plan digest 和 Scope；
4. 将 synthetic reference approval service 根据当前 challenge 签发的 JWS 粘贴到断言框，点击“记录限定审批”；
5. 在 `AUTHORIZED` 停留约 8 秒，不继续执行。

**旁白**：

> Orchestrator、Intake、Investigator 和 Policy 各自提交带 revision 的任务回执。策略冻结金额、计划和完整 scope 后，项目真的停在 `WAITING_APPROVAL`：这时没有退款或关单写副作用。审批者与请求者职责分离，审批只把当前 digest-bound 计划原子改成 `AUTHORIZED`；Executor 仍要单独领取任务。

**必须看到**：

- `WAITING_APPROVAL`、`next task = record_human_approval`；
- 审批前退款为空或等价的“无写副作用”证据；事件链 `VALID`；
- 审批后 `AUTHORIZED`、`next task = execute_authorized`；
- 点击审批后没有自动跳到 `EXECUTED / COMPLETED`。

**当前诚实边界字幕**（2 秒即可）：

> approval origin = EXTERNAL_SIGNED_ASSERTION；当前 issuer 为 synthetic reference service，不是企业 IdP / MFA 证据。

### 1:28–2:40｜真实失败后补偿：先恢复，再验真

**画面动作**：清空当前视图，切换“下游故障与补偿”，创建并推进至终态；点击“独立语义验真”。

**旁白**：

> 现在看更难的路径。系统先创建 ¥90 退款并扣减可退余额，随后注入 CRM 关单故障。Executor 不能盲重试原退款，而是用另一张精确许可证执行补偿。逻辑操作绑定稳定 operation id；租约过期后先 reconcile，结果不确定就进入 `UNKNOWN` 并停止自动重放。Verifier 再读取新鲜退款、余额和工单快照：退款必须是 `COMPENSATED`，余额恢复，工单保持 `OPEN`，Memory 才能封存 Proof。

**必须看到**：

- 最终 `COMPENSATED`、`next task = terminal`；
- `saga.executed`、`postconditions.verified`、`workflow.sealed` 或界面中的等价证据；
- 网关回执、任务回执、独立 actor 计数均非零；
- 点击验真后出现 `PROOF VALID` 与“外部信任 / 跨证据语义一致”。

**禁止旁白**：不要说“绝对 exactly-once”“生产支付已接入”或“所有故障都能回滚”。

### 2:40–3:12｜公开回放：测网关，不冒充测模型

**画面**：切到准备好的 AgentDojo 结果卡；只展示摘要，不滚动 JSON。

**旁白**：

> 我们还把 AgentDojo v1 的 629 个公开组合、2,159 个 ground-truth 调用送入真实 Action Gateway 做授权合同回放。合法合同全部接受；工具、参数和上下文错配错误通过为零；同键重放没有第二次副作用。它只证明网关符合合同，不运行模型，也不代表模型 ASR、utility 或提示注入检测率。

**必须看到**：`629 cases`、`2,159 calls`、`0 duplicate side effects`，以及同屏的 `does_not_measure: model ASR / task utility`。

### 3:12–3:48｜多 Agent 与当前边界

**画面**：七角色 DAG / 官方 Identity 八字段矩阵，再切当日 AgentTeams 证据卡。

**旁白（当前版本）**：

> 七个 Agent 不是七段提示词，而是七个最小权限合同。官方 AgentTeams v1.2.0-beta.1 的 Manager、Controller、Human 和 7/7 Worker 已运行；七成员 Team 已 Active。同一 Project 下，Leader 与 ticket-intake 两个真实容器直接通过 MCP 完成 create、plan、delegate、ack、submit、check、accept。这里证明的是控制面生命周期；模型仍是 placeholder，所以不冒充模型自主协同。

**必须看到**：`Human Active`、`7/7 Worker Running`、`Team Active`、`direct lifecycle 7/7 passed`、`modelDriven=false` 同屏出现；若展示 Matrix artifact，必须同时标出 ATB-007 未验证。

### 3:48–4:20｜跨域复用与诚实边界

**画面**：结尾页；显示 `refund 3 paths · operations 4 paths · 162 passed`，角落固定 `Team Active · direct 7/7 · modelDriven=false · external claims 0/6 VERIFIED`。

**旁白**：

> ProofMesh 开源的不是退款页面，而是 Action Passport、可恢复 Gateway、七个 Skill 合同和包外 Verifier。同一协议已经跑过退款三路径和生产运维变更四路径，其中未知状态会停止自动执行。162 项测试证明工程准备度，不替代真实模型、真实 vendor sandbox、授权试点、公开仓库和独立复现；这六类外部 Claim 目前全部待完成。每个副作用，先有许可证，再有证明。

## 4. 3:00 压缩版

| 时间 | 保留内容 | 删除 / 合并 |
|---|---|---|
| 0:00–0:18 | ¥90 冲突与一句话方案 | 删除七角色总览 |
| 0:18–1:03 | `WAITING_APPROVAL → AUTHORIZED`，明确审批不执行 | 不跑高风险终态 |
| 1:03–2:05 | Saga 到 `COMPENSATED + PROOF VALID` | 把 logical operation / reconcile 压成一句 |
| 2:05–2:28 | 629 / 2,159 结果卡与非模型评测边界 | 不念各类 Wilson 区间 |
| 2:28–2:45 | AgentTeams 当日边界卡 | 只说已证 / 未证各一句 |
| 2:45–3:00 | 两域、162 passed、model false、Claim 0/6 与结束语 | 删除路线图 |

不得压掉 `AUTHORIZED` 停留、`COMPENSATED` 业务终态、`PROOF VALID` 和 AgentTeams 未验证边界；否则视频会重新退化成“点按钮成功”的 Demo。

## 5. 后期与验收清单

- 只做裁切空等候、放大关键字段、加章节条；不得改写运行值、拼接不存在的状态或遮盖失败。
- 字幕中的数字、版本和状态必须来自同一次冻结证据；不要把之后新增的结果贴到旧录像上。
- 高风险段必须有两个独立停帧：`WAITING_APPROVAL` 与 `AUTHORIZED`。
- Saga 段必须同时交代业务结果和证明结果：`refund=COMPENSATED`、余额恢复、ticket=`OPEN`、Proof=`VALID`。
- 不展示 token、私钥、`.env`、真实客户 PII、管理员 URL 或可复用审批 assertion。
- 成片末尾保留 2 秒边界卡：`production adapters = reference, not connected · vendor adapters = sandbox-ready, not run · modelDriven=false · external claims 0/6 VERIFIED`；当对应实证升级后再按第 6 节替换。
- 导出后从头观看一次，检查字幕遮挡、光标误触、敏感信息、静音、跳帧和状态跨案件混用。

## 6. 下一阶段材料更新门禁

以下三类证据互相独立。某一类达标，只能更新它对应的页和主张；不能因为 Team 跑通就声称外部业务试点或企业审批来源也已完成。

### 6.1 AgentTeams Team / 生命周期 / 模型协同

#### 严格主张层级

| 可升级主张 | 必须同时满足的证据 | 仍然不能推出 |
|---|---|---|
| `Team created / Active` | 官方版本/commit 与构建镜像 digest；Team CR `resourceCreated=true` 且 phase=`Active`；Human/7 Worker UID 与 Team roster 一致；时间戳和去敏原始响应 | 不等于 TeamHarness lifecycle 跑通；不等于模型自主协同 |
| `TeamHarness 完整生命周期已实测` | 同一 Project 下 `create → plan → delegate → ack → submit → check → accept` 全部真实执行；Project/Task ID、状态、时间、角色、失败重试与最终 accept 可关联；无手工改 DB / 假事件；ProofMesh `workflow_id/task_id/revision/trace_id/digest` 能与 TeamHarness 双账本一一核对 | 若是脚本直调，只能称“direct MCP lifecycle”，不能称模型自主 |
| `模型驱动七角色协同已验证` | 非 placeholder provider；模型/版本/推理参数与调用时间有去敏记录；Leader/Worker 由真实 assignment / room 驱动并实际调用各自 MCP；至少成功、Human pause/resume、CRM 故障补偿、篡改拒绝四条路径；保存 Matrix/room、project/task、Worker 日志与 Proof；至少一次从干净环境复跑 | 不等于模型安全率、生产 SLA 或所有任务泛化；仍需报告失败率与人工干预 |

出现任一证据断链、跨 run 拼接、只读 health、静态 manifest 或 `welcomeSent=false` 时，不得升级到下一层。

#### 达标后替换材料

- **PPT 第 3 页“当前控制台”**：可增加 Team / Project / Task 与 ProofMesh workflow 的同屏关联，但不得删掉业务终态与包外验真。
- **PPT 第 4 页“动作控制平面”**：把“AgentTeams 目标映射”改为已运行协作面，图中标出官方 Team / TeamHarness 与 ProofMesh 控制面的真实边界。
- **PPT 第 5 页“七个 Agent”**：以真实 roster、assignment、handoff/receipt 取代静态映射图；官方八字段 Identity 矩阵继续保留。
- **PPT 第 8 页“可复算证据”**：新增 Team/Task 双账本证据格；只在模型路径满足上表第三层时增加模型 run 数、成功/失败和人工干预统计。
- **PPT 第 11 页“自评分与边界”**：仅移除已经被真实证据消除的扣分项，按同一评分规则重新自评；不得只加分不重列剩余扣分。
- **白皮书第 4 页（03 · JUDGE LENS）**：重算多 Agent 分项与总分，写清直接生命周期与模型生命周期的区别。
- **白皮书第 6 页（05 · ARCHITECTURE）**、**第 7 页（06 · MULTI-AGENT）**：更新运行架构、项目/任务状态映射、角色 handoff 与失败恢复证据。
- **白皮书第 18 页（15 · AGENTTEAMS）**：整页换成新的 L1–L4 证据梯，列出 Team UID、Project/Task 关联、模型 provider 状态、成功与失败样本；旧兼容缺口降为修复历史。
- **白皮书第 19 页（16 · OPEN SOURCE + ROADMAP）**：把已完成 Team 项从路线图移入成果，保留真实模型/社区采用等未完成项。
- **白皮书第 21 页（18 · CLAIM REGISTER）**：逐条把 `禁止声称 / 限定声称` 改为有证据的窄主张；不要删除非模型 ASR、非生产支付等其他边界。
- **录屏**：只有 Team Active 时，替换 3:12–3:48 的资源边界卡；只有 direct lifecycle 时，旁白必须带“控制面直调”；只有完整模型层达标，才可展示真实 room/assignment 片段并说“模型驱动协同已验证”。

### 6.2 外部支付 / CRM 沙箱与真实试点

#### 严格主张门槛

当前 Stripe/HubSpot adapter 已 sandbox-ready，但没有真实测试账户运行证据。要说“已接外部沙箱”，至少需要：

1. 连接的确是项目进程之外、由独立凭证和账户控制的支付 / CRM sandbox；报告供应方、API 环境标识、adapter 版本、时间窗，凭证去敏；
2. 每个写调用携带稳定 operation id / 上游 idempotency key，保存请求摘要、HTTP/业务回执和查询/对账结果；ProofMesh 不能自签冒充上游 attestation；
3. 真实跑通低风险成功、高风险审批、支付成功/CRM 失败补偿、超时未知、重复请求、参数漂移六类路径；`UNKNOWN` 不能被自动写成成功；
4. 独立 Verifier 使用包外 trust/policy 和上游签名响应或可信审计代理复算终态；支付流水、CRM 状态、Proof 能按 operation/workflow 对齐；
5. 从干净账户或可重置沙箱复跑，公开失败样本与限制。只有 API mock、录制响应或内置 SQLite adapter 时，仍只能叫“隔离事务靶场”。

要进一步说“企业试点产生价值”，还必须预注册样本与口径，使用 200–500 条脱敏历史工单做 shadow / sandbox 盲测，并由非开发者复核：重复副作用率、错误放行率、审批前写副作用、可恢复终态率、人工触达率、恢复时长、验真成功率和审计取证时长。ROI 只能使用客户提供的基线、时间窗、样本量和成本输入；629-case 回放与本机延迟不得外推为客户收益或 SLA。

#### 达标后替换材料

- **PPT 第 2 页**：保留 ¥90 冲突，但用外部 sandbox 的支付/CRM 回执与补偿对账截图替换内置靶场示意。
- **PPT 第 3 页**：三大结果改为同一外部 run 的“审批前零写、补偿后余额恢复、包外 VALID”，同时显示外部 operation / ticket 关联。
- **PPT 第 4 页**：架构图把 Commerce sandbox 改为真实 adapter、上游 idempotency / reconcile API 与独立 attestation 来源。
- **PPT 第 6 页**：用外部故障时间线和 `UNKNOWN → reconcile / 人工` 证据替换当前本地 Saga 截图。
- **PPT 第 8 页**：增加外部 run 样本量、失败类型、独立验真率；不要删除 AgentDojo 的“仅授权合同回放”免责声明。
- **PPT 第 10 页**：只有盲测试点达标后才填 KPI 实测值、基线、样本量和时间窗；未达标前继续只展示定义。
- **PPT 第 11 页**：外部试点达标后重算场景价值/工程分；“生产支付、HA、KMS、SLA”仍需各自证据，不能一并宣称。
- **白皮书第 2–3 页（01–02）**：将业务故事和当前控制台换为同一外部 run 的可追溯事实。
- **白皮书第 4–5 页（03–04）**：重算自评，并把 KPI 定义表升级为“定义 + 基线 + 结果 + 样本量 + 置信区间 / 限制”。
- **白皮书第 6 页（05）**、**第 10–11 页（09–10）**、**第 13 页（12）**：更新 adapter、reconcile、补偿、业务快照与 reference evidence。
- **白皮书第 17 页（14 · ENGINEERING）**：记录外部身份、网络、限流、超时、上游 SLA 和仍未完成的 KMS/HA 边界。
- **白皮书第 19、21 页（16、18）**：更新路线图与 claim register；仅在盲测和外部复核完成后写“试点结果”，不能把 sandbox 集成写成“客户采用”。
- **录屏**：用外部 sandbox 专用账户录制，必须遮挡账号和凭证，但保留环境标识、operation id 尾部、支付/CRM 查询结果与 Proof 关联；不能拼接不同 run 的成功画面。

### 6.3 企业 IdP / 审批服务 assertion

#### 严格主张门槛

当前已达到“外部可验证 synthetic 审批来源”的协议与密码学门槛；升级为企业身份 assurance 仍必须满足：

1. assertion 由 ProofMesh 控制面之外的企业 IdP、WebAuthn 服务或审批服务签发；Verifier 的 issuer/key/policy 来自包外 pinned trust，不从 Proof 内自举；
2. 断言至少绑定 issuer、subject、audience、tenant、workflow、revision、plan digest、policy digest、完整 scope、decision、issued/expiry、nonce/challenge；MFA / assurance level 只能在上游真实提供时记录；
3. 控制面校验时效、audience、tenant、nonce 一次性、撤销/密钥轮换和 requester/approver 职责分离，成功后仍只做 `WAITING_APPROVAL → AUTHORIZED`；
4. Proof 保留原始 assertion 或可验证封装及其 digest；包外 Verifier 独立验签并与 task/gateway receipt、计划、策略和最终业务结果交叉绑定；
5. 负测至少覆盖伪造签名、错误 issuer/audience/tenant、旧 revision、plan/policy/scope 漂移、过期、重放、撤销 key、低 assurance、不符合职责分离；全部 fail closed 并留审计；
6. 生产模式继续禁用本地任填 approver CLI；必须将 reference-script ACR / AMR 替换为企业上游真实提供的身份 assurance。

满足后，Verifier 应输出窄而明确的 assurance 枚举（例如 `EXTERNAL_ASSERTION_VERIFIED`，以最终 schema 为准），而不是笼统的“独立 Human 签名”。

#### 达标后替换材料

- **PPT 第 3 页**：高风险参考结果增加“外部 assertion 验证通过”，但仍与业务执行、Proof 终态分开显示。
- **PPT 第 6 页**：用 challenge → IdP/审批服务 assertion → 控制面授权 → Executor 领取的真实序列替换当前本地审批说明。
- **PPT 第 7–8 页**：将 approval issuer 加入包外信任输入与可复算证据；如果它使用第六类独立 key，必须同步修改“五钥”表述、trust schema、代码和全部证据后才能改成“六钥”，不能只改图。
- **PPT 第 11 页**：删除已解决的审批来源缺口，保留真实上游、KMS/HA、第三方评估等剩余缺口并重算自评。
- **白皮书第 4 页（03）**、**第 7 页（06）**、**第 11 页（10）**：更新 assurance 枚举、assertion 字段、职责分离与负测结果。
- **白皮书第 12 页（11 · EXTERNAL PROOF）**：将 assertion issuer / key 纳入外部 trust 和跨证据检查；同步解释它与 task receipt 的不同职责。
- **白皮书第 17 页（14）**、**第 21 页（18）**：把生产缺口和 claim register 改成有证据的窄主张；不能因此声称全套企业 IAM、不可否认性或合规认证已完成。
- **录屏**：替换 0:25–1:28 的当前边界字幕，短暂展示去敏 assertion 字段和 Verifier assurance；不展示可重放的 assertion、cookie、QR、设备标识或用户 PII。

## 7. 三类证据同时完成后的重构顺序

1. 先冻结证据目录、时间窗、版本、测试和负测，生成 claim register；
2. 再更新 `docs/demo-video-script-v2.md` 的旁白边界与镜头；
3. 重录视频，确保 Team、外部 sandbox、assertion 来自同一或明确关联的 run；
4. 更新白皮书的证据页、KPI、架构和 claim register；
5. 最后更新 PPT 第 2–11 页及来源备注，重新做逐页渲染、溢出、文本和禁词扫描；
6. 二进制材料冻结后，才重建 release、清单与 SHA256SUMS。

任何一步只有截图、口头确认或单个 happy path 时，都不满足材料升级门槛。

工程准备度约 88/100 只是 22/20/22/20/4 的内部自评，不是官方赛事成绩。模型实跑、真实 vendor sandbox、授权历史试点、独立复现、公开仓库/身份、生产设施六项 Claim 当前均为 `PENDING`；任何一项升级必须通过 `scripts/validate_external_evidence.py` 的 fail-closed 校验。
