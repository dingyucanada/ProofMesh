# ProofMesh：从工程强度到 95+ 证据的行动手册

> 版本口径：本手册把“代码能力”“可复现证据”“真实外部证据”分开。任何账号、密钥、客户数据、合作方名称都不得发到聊天、Issue、截图或 Git 仓库。评委分数无法由参赛者保证；“88 / 95+”是内部冲刺目标，不是官方承诺。

## 0. 先理解评分差距

| 层级 | 评委真正要看的东西 | 可由开发者单独完成 | 必须由你或外部主体完成 |
|---|---|---:|---:|
| 工程竞争力目标约 88 | 真实模型接入通道、第二业务域、生产身份/数据/密钥接口、自动化测试、SBOM、可复现包 | 是 | 模型服务凭证用于最终实跑 |
| 95+ 竞争力 | 支付与 CRM 的真实 sandbox trace、200–500 条经授权盲测、公开仓库历史、外部复现签名、真实参赛身份 | 否 | 是 |
| 生产采用 | 企业 IdP/KMS、正式数据处理协议、小流量试点、生产 SLA、安全评估 | 否 | 是 |

最短路径不是继续堆功能，而是把同一冻结版本依次通过：`模型自主轨迹 → sandbox 双写与故障恢复 → 授权数据盲测 → 独立机器复现`。

## 1. 你今天要完成的五件事

完成一项后只把“已配置/链接/非敏感 ID”告诉开发者；**永远不要发送 secret、token、客户原文或身份证明扫描件**。

### 1.1 模型服务：优先百炼，Ollama 作离线备份

#### 路径 A：阿里云百炼 / Model Studio（推荐）

1. 用你的阿里云账号开通 Model Studio，选择与后续数据合规策略一致的地域和 Workspace。
2. 在 Workspace 中创建**仅用于本项目、可随时撤销**的 API Key；不要复用个人主账号长期密钥。
3. 在官方 Playground 先完成一次最小对话，记录非敏感的：地域、Base URL、Workspace ID、模型 ID。
4. 在本机私密环境中设置以下变量；值不要写入 `.env.example`、日志或截图：

   ```text
   PROOFMESH_MODEL_PROVIDER=aliyun-model-studio
   PROOFMESH_MODEL_BASE_URL=<你的地域对应 OpenAI-compatible URL>
   PROOFMESH_MODEL_NAME=<实际可用模型 ID>
   PROOFMESH_MODEL_API_KEY=<仅在本机秘密存储>
   ```

5. 回复开发者：`百炼已配置；region=...；model=...`。只给 region/model，不给 key。
6. 设每日费用上限与告警；评测完成后轮换或撤销该 key。

百炼官方说明其 API 提供 OpenAI-compatible 接入；Base URL 与 Key 需按地域和 Workspace 配置：[Model Studio 概览](https://www.alibabacloud.com/help/en/model-studio/what-is-model-studio)、[首次调用 Qwen](https://www.alibabacloud.com/help/en/model-studio/first-api-call-to-qwen)。

#### 路径 B：本地 Ollama（无云端数据外发的备份）

1. 从 [Ollama 官方下载页](https://ollama.com/download) 安装与你机器匹配的版本。
2. 拉取一个支持工具调用、机器内存可承受的模型，并确认本地 `/v1/chat/completions` 可用。
3. 设置：

   ```text
   PROOFMESH_MODEL_PROVIDER=ollama
   PROOFMESH_MODEL_BASE_URL=http://127.0.0.1:11434/v1
   PROOFMESH_MODEL_NAME=<本地模型名>
   PROOFMESH_MODEL_API_KEY=ollama
   ```

4. 回复开发者：`Ollama 已配置；model=...`。

Ollama 的 OpenAI-compatible 接口与工具调用能力见[官方兼容性文档](https://docs.ollama.com/api/openai-compatibility)。

#### 模型验收门槛

同一冻结版本、固定 seed/temperature（模型支持时）至少运行 30 次，覆盖四条路径：

- 低风险成功；
- 高风险暂停并等待 Human assertion；
- 支付成功、CRM 失败后的补偿；
- 参数/工具/上下文篡改被 Gateway 拒绝。

必须导出模型原始 tool-call trace、Proof workflow id、失败原因和 token/延迟；不得把 direct MCP 脚本轨迹冒充模型自主轨迹。

### 1.2 支付 sandbox：Stripe 测试环境

1. 注册 Stripe 账号，切换到 **Sandbox / test environment**；禁止使用真实银行卡或 live key。
2. 创建项目专用 sandbox。用官方测试支付方式先创建一笔模拟支付，再做一次退款。
3. 创建**受限测试密钥**：只允许读取 PaymentIntent/Charge、创建/读取 Refund、读取 Event；不要给客户、转账、账户管理等无关权限。
4. 如有固定出口 IP，再限制密钥来源 IP；启用请求日志。
5. 在本机秘密存储：

   ```text
   PROOFMESH_VENDOR_TENANT_ID=acme-cn
   PROOFMESH_STRIPE_SECRET_KEY=<sandbox restricted key>
   PROOFMESH_STRIPE_API_BASE=https://api.stripe.com
   ```

6. 生成 20–50 个 sandbox 案例，至少覆盖：成功、重复幂等请求、退款失败/异步失败、网络在上游成功后中断、对账为 UNKNOWN。
7. 回复开发者：`Stripe sandbox 已配置；account_id=...`，不要发送 key。

Stripe 官方明确测试环境不会移动真实资金，并要求测试调用使用测试密钥：[Testing use cases](https://docs.stripe.com/testing-use-cases)、[Test card numbers](https://docs.stripe.com/testing)。密钥应放在 secret manager/环境变量并使用最小权限的 restricted key：[API key best practices](https://docs.stripe.com/keys-best-practices)。ProofMesh 的 operation id 应映射到 Stripe idempotency key；同一 key 不得改变参数，规则见[Idempotent requests](https://docs.stripe.com/api/idempotent_requests)。

### 1.3 CRM sandbox：HubSpot 开发测试账户

1. 注册 HubSpot 开发者账号。
2. 进入 `Development → Testing → Test Accounts → Create`，创建隔离的 configurable test account。
3. 建立仅安装到该测试账户的 private/static app；只申请 `tickets` scope，不申请 `tickets.sensitive` 或 `tickets.highly_sensitive`。
4. 创建字段：`proofmesh_workflow_id`、`refund_state`、`refund_amount_bucket`、`proof_digest`。不要创建姓名、电话、地址等演示无关字段。
5. 在本机秘密存储：

   ```text
   PROOFMESH_HUBSPOT_ACCESS_TOKEN=<测试账户 app token>
   PROOFMESH_HUBSPOT_API_BASE=https://api.hubapi.com
   PROOFMESH_HUBSPOT_OPEN_STAGE_ID=<测试 pipeline 的 open stage ID>
   PROOFMESH_HUBSPOT_CLOSED_STAGE_ID=<测试 pipeline 的 closed stage ID>
   PROOFMESH_VENDOR_TIMEOUT_SECONDS=3.0
   ```

6. 建 20–50 个 ticket，覆盖更新成功、版本冲突、超时后对账成功、权限拒绝和可重试 5xx。
7. 回复开发者：`HubSpot test account 已配置；account_id=...`，不要发送 token。

HubSpot 官方说明开发测试账户用于隔离测试，且可从 Development 页面创建：[Account types](https://developers.hubspot.com/docs/getting-started/account-types)、[Configurable test accounts](https://developers.hubspot.com/docs/developer-tooling/local-development/configurable-test-accounts)。Ticket API 的最小 scope 是 `tickets`：[Scopes](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/scopes)。

### 1.4 公开 GitHub 仓库与参赛身份

1. 登录参赛者本人或团队组织的 GitHub；开启 2FA。
2. 新建公开仓库，建议名称 `proofmesh-action-control-plane`，描述写清 Agent Infra / AgentTeams / Apache-2.0。
3. **不要初始化 README/LICENSE**，避免与本地发布树冲突；先给开发者空仓库 URL，之后再由你明确授权推送。
4. 提供以下非敏感信息，写入提交材料：

   - 团队名称；
   - 参赛成员姓名与角色；
   - 单位/学校（如允许公开）；
   - 公开联系邮箱；
   - GitHub URL；
   - 比赛报名编号（如有）。

5. 首次发布使用冻结 tag（例如 `v1.0.0-competition`），上传源码、release ZIP、SHA256SUMS、SBOM、PPT PDF 和复现说明。
6. 开启 Issues、Discussions、Actions；保护 `main`；Actions 默认 `GITHUB_TOKEN` 只读；任何云服务密钥只进入 Actions secrets/environment，不能进入代码或日志。
7. 邀请独立复现者为只读/triage；如果他要提交报告，优先用 Issue/PR，不给管理员权限。

GitHub 的官方建仓步骤见[Creating a new repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)，secret 设置见[Using secrets in GitHub Actions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)，最小权限和 fork 安全边界见[Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use)。

### 1.5 200–500 条工单与一位独立复现者

#### 数据：先授权，再匿名化，再搬出数据方环境

1. 找一家具备真实退款/售后流程的小型电商、SaaS 或校内服务团队；明确这里只做离线 shadow，不触达生产支付。
2. 由数据负责人签署本仓 `docs/templates/data-authorization-and-anonymization-checklist.md`；写明目的、字段白名单、数量、保留期、删除日和可公开聚合指标。
3. **在数据所有方环境内**先删自由文本和直接标识符；不要先导出再脱敏。
4. 推荐只保留：风险分层、金额区间、币种、商品类别、订单年龄区间、是否重复请求、当前状态、期望动作、最终标签、故障类别。时间只保留相对区间。
5. 工单/订单 ID 用项目专用随机映射，不用可逆的明文哈希；映射表留在数据方且不交付。
6. 由不参与开发的人抽查重识别风险；发现姓名、手机号、邮箱、地址、证件、账号、聊天原文即停止导入。
7. 冻结数据集摘要与 schema；只把匿名数据文件放到本机受限目录，不上传聊天。公开仓库只提交数据字典、聚合统计、数据集 hash 和评测报告。
8. 先用 50 条 dry run，再盲跑剩余 150–450 条；不得根据测试标签临时改策略。报告全部失败样本和置信区间。

仓内已经提供严格导入器。它要求 case 与 blind label 分离、200–500 条、closed enum schema、随机 `pmc_` 标识、分层覆盖和授权引用；报告只输出计数与 hash，不回显工单内容：

```text
PYTHONPATH=src python scripts/pilot/validate_pilot_dataset.py \
  --cases /受限目录/cases.jsonl \
  --labels /受限目录/blind-labels.jsonl \
  --origin AUTHORIZED_HISTORICAL \
  --authorization-ref AUTH-2026-PM-001 \
  --reviewed-by reviewer@example.org \
  --output /受限目录/pilot-dataset-report.json
```

这份报告仍只证明 schema/hash/覆盖；“客户试点”只有在外部 claim gate 同时绑定授权记录和独立评审后才能升级为 VERIFIED。

中国《个人信息保护法》要求处理个人信息遵循合法、正当、必要、最小范围；“去标识化”仍不同于“匿名化”，只有无法识别且不能复原才符合其匿名化定义。操作前应由数据方负责人/法务确认授权与处理基础，本手册不替代法律意见。官方全文见[工业和信息化部转载文本](https://www.miit.gov.cn/jgsj/zfs/fl/art/2022/art_515a4b20c12f430eab54bb4f56d89f56.html)。

#### 独立复现：必须与开发过程分离

复现者最好是未参与代码开发的安全、平台、审计或业务同学。把以下材料交给他：

- 冻结 release URL、tag、commit 和 SHA256；
- 一页 quickstart，不提供开发机数据库/私钥；
- 预期只描述“不变量”，不要提前告诉具体输出 hash；
- 本仓 `docs/templates/independent-reproduction-attestation.md`。

他应在干净机器完成：校验 ZIP hash、安装、全量测试、三条 reference proof 包外复验、629 授权回放、240 合成故障盲测、至少一个篡改负例。开发者不能远程操作其机器；如需帮助，先记录原始失败，再另开修复版本。最终提交签名报告、机器环境、开始/结束时间、命令结果、差异和公开 Issue URL。

## 2. 两周执行节奏

| 时间 | 你负责 | 开发侧负责 | 完成定义 |
|---|---|---|---|
| D0–D1 | 模型、Stripe、HubSpot、GitHub 建号 | 冻结 adapter 合同与 secret 边界 | 四项均只在本机配置，仓库无 secret |
| D2 | 运行模型 4 路径 | 固化 tool trace / ablation / Proof 关联 | ≥30 次模型轨迹，可复算、失败全量披露 |
| D3–D4 | 提供 sandbox 非敏感 ID | 接通支付/CRM、故障注入与对账 | 无真实资金；重复副作用为 0；UNKNOWN 停机 |
| D5 | 确认公开身份与仓库 | 发布 tag、CI、SBOM、复现包 | 外部从 URL 可下载并验 hash |
| D6–D8 | 获得书面数据授权并在数据方环境匿名化 | 导入器、schema 校验、冻结策略 | 200–500 条；无直接标识符；dataset hash 固定 |
| D9–D10 | 安排独立评审 | 只响应公开缺陷，不代跑 | 独立报告含原始失败与最终结果 |
| D11–D12 | 确认允许公开的聚合结果 | 更新 PPT/白皮书/视频 | 所有数字可追到 artifact，不夸大 sandbox/试点 |
| D13–D14 | 完成报名与最终签字 | 重建 release + SHA + submission gate | 提交物一致、链接有效、身份完整 |

## 3. 给开发者的安全回执格式

你可以复制下列格式回复；未完成的行留空。不要附密钥或客户文件。

```text
模型：provider=百炼/OpenAI/Ollama；region=...；model=...；本机已配置=yes/no
Stripe：sandbox account id=...；restricted test key 本机已配置=yes/no
HubSpot：test account id=...；tickets-only token 本机已配置=yes/no
GitHub：公开空仓库 URL=...
参赛身份：团队名=...；成员/角色=...；单位=...；公开邮箱=...；报名号=...
数据：数据方书面授权=yes/no；预计匿名工单数=...；文件仅在本机受限目录=yes/no
复现者：角色/单位（可匿名描述）=...；预计复现日期=...
```

## 4. 95+ 的严格 Claim Gate

只有下表全通过，材料才可升级措辞：

| 拟声称内容 | 必需证据 | 不足时的准确措辞 |
|---|---|---|
| “模型自主多 Agent 协同” | 模型服务原始 trace + TeamHarness 7 步 + 4 业务路径 + 多次重复统计 | “direct MCP lifecycle 已验证，模型通道待外部凭证实跑” |
| “真实支付/CRM 集成” | 供应商 sandbox request id、幂等 key、对账日志、无 live 数据证明 | “独立 HTTP 合成 sandbox” |
| “客户试点” | 数据方授权、真实业务样本、冻结方案、盲测报告 | “合成/公开基准评测” |
| “独立复现” | 外部人员、独立机器、冻结 hash、签名报告和公开链接 | “开发者本机复验” |
| “生产就绪” | 企业 IdP/KMS/数据库、容量与故障演练、安全评估、SLA | “生产边界接口与部署参考已实现” |

任何一项不满足，都保留右栏表述。这样做不是保守，而是让评委无法用一个追问击穿整套可信度。
