# 部署、评审复现与生产化路径

## 1. 最短评审路径

要求 Python 3.10+。以下流程不需要模型 API Key，也不访问外部企业系统：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
make clean-runtime
pytest -q

# 低风险完整闭环
PYTHONPATH=src python3 -m proofmesh.cli --home . demo-refund TKT-LOW-001

# 下游故障、退款补偿、独立回查与证明封存
PYTHONPATH=src python3 -m proofmesh.cli --home . demo-refund TKT-SAGA-001
```

高风险案件会停在 `WAITING_APPROVAL`：

```bash
PYTHONPATH=src python3 -m proofmesh.cli --home . demo-refund TKT-HIGH-001
PYTHONPATH=src python3 -m proofmesh.cli --home . approval-challenge <WORKFLOW_ID> \
  --output /tmp/proofmesh-approval-challenge.json
# 由独立审批服务读取 challenge 并返回 assertion.jws；参考脚本的私钥目录必须在 ProofMesh home 之外。
PYTHONPATH=src python3 -m proofmesh.cli --home . approve <WORKFLOW_ID> \
  --approver separation-of-duties-reviewer \
  --reason '已复核冻结计划、金额、范围与补偿条件' \
  --approval-assertion-file /path/from/external-approval-service/assertion.jws
PYTHONPATH=src python3 -m proofmesh.cli --home . resume <WORKFLOW_ID>
PYTHONPATH=src python3 -m proofmesh.cli --home . verify-proof \
  artifacts/workflows/<WORKFLOW_ID>/workflow-proof.json
```

高风险审批即使在开发模式也必须提交外部 approval-service assertion；`PROOFMESH_ENV=production` 还会禁用本地 CLI，只允许已认证 API。`scripts/reference_approval_service.py` 是 synthetic 协议演示器，私钥目录不进入 `build_runtime` 或 ProofMesh 容器；它不证明企业 SSO / MFA。生产必须替换为真实 IdP / 审批服务。

CLI 的 `demo-refund` 是本地确定性 smoke runner；它调用同一控制面，但不证明 AgentTeams Worker 已认领任务。真实 AgentTeams 路径必须逐步调用角色 MCP。

## 2. 评委控制台

API 默认 fail closed。先复制 `.env.example`，将每个 `replace-with-*` 替换为不同的随机值，再加载环境：

```bash
cp .env.example .env
# 编辑 .env；不要提交该文件
set -a
source .env
set +a
PROOFMESH_HOME=. PYTHONPATH=src uvicorn proofmesh.api:app \
  --host 127.0.0.1 --port 8000 --no-server-header
```

打开 `http://127.0.0.1:8000`，将七个 Agent 角色、职责分离的 Approver 与 Auditor 共九个 token 粘贴到“角色凭证”。高风险门禁还要求粘贴由外部审批服务根据 `GET .../approval-challenge` 签发的 assertion；Bearer subject 与 assertion subject 必须一致。凭证只保存在当前浏览器 `sessionStorage`，页面源码不内置 token。

检查服务状态：

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
```

`/health` 只代表进程存活；`/ready` 还要求 orchestrator、approver、auditor、六个下游 Worker 角色和 gateway 身份全部配置。可选 operator 仅用于只读运维，不参与案件协议。

## 3. Docker

```bash
cp .env.example .env
# 替换所有占位 token
docker compose --env-file .env config --quiet
docker compose --env-file .env up --build
```

默认只发布到 `127.0.0.1:8000`。镜像以 UID/GID 10001 运行；Compose 使用只读根文件系统、移除 capabilities、启用 `no-new-privileges`，并用独立卷保存业务状态、证明和开发信任材料。

`.env.example` 不是生产秘密方案。生产应由编排器或秘密代理注入短期身份，不应把 token 固化在 Compose、镜像或 Worker 包中。

## 4. AgentTeams v1.2.0-beta.1

静态交付锁定官方 `v1.2.0-beta.1` 与 commit `78d0ceda...`：

```bash
python3 scripts/package_agentteams.py
python3 scripts/validate_agentteams.py
pytest -q tests/test_agentteams.py
```

针对该 beta Manager REST 未映射 `workerMembers` 的已复现缺口，交付包含最小上游补丁与离线复验：

```bash
AGENTTEAMS_SOURCE=/path/to/AgentTeams-v1.2.0-beta.1
python3 agentteams/upstream/verify_patch.py \
  --source "$AGENTTEAMS_SOURCE"
```

它会先核对锁定 commit、三个上游文件和补丁 SHA-256，再仅在临时副本应用补丁，确认解耦 Create 合同与旧 inline 合同同时保留。2026-08-13 的运行快照中，三个 Go 合同测试已通过；锁定源码编译的 controller binary 被放入官方 embedded 父镜像的单层兼容镜像，七成员 Team 已 `Active`，direct MCP 七步 project/task lifecycle 已跨 Leader / Worker 容器完成。该镜像是本地兼容构建，不是上游正式 release；模型仍为 placeholder，不得据此声称模型自主协同。

官方 beta CLI 是 `hiclaw`。Manager 与 ProofMesh 角色 MCP 可达、模型网关已配置后：

```bash
./agentteams/bootstrap.sh prepare
./agentteams/bootstrap.sh status
./agentteams/bootstrap.sh team
./agentteams/bootstrap.sh status
```

`prepare` 上传七个官方结构 Worker ZIP，应用 qwenpaw Worker overlay 并创建 Human；`team` 只有在 Human/Workers 达到要求状态后才创建 Team。官方 AgentTeams gateway 应为每个 Worker 注入独立 consumer key，并在 ProofMesh 侧映射为唯一角色和 tenant scope。不要把 Bearer token 写入 YAML、ZIP 或 Skill。

资源 `Running/Active`、TeamHarness health/直接 MCP 调用、完整 project/task 生命周期和模型驱动自主协同是不同证据。运行报告必须明确区分；详见 `agentteams/README.md`。

## 5. MCP 接口

角色面：

```text
/mcp/roles/orchestrator
/mcp/roles/intake
/mcp/roles/investigator
/mcp/roles/policy
/mcp/roles/executor
/mcp/roles/verifier
/mcp/roles/memory
```

每个端点只列出该角色的高层工作流工具。原始数据面 `/mcp` 包含 `payments.*`、`crm.*` 等业务工具，只允许受信 gateway 身份，并要求调用元数据携带内部签发的 Action Passport 与 idempotency key。Agent/浏览器不应直接访问它。

## 6. Kubernetes 强制边界

参考清单位于 `deploy/kubernetes/`：

1. 使用 `/ready` 输出的 canonical policy digest 配置 `PROOFMESH_ACTIVE_POLICY_DIGESTS`；
2. 为 webhook Service 配置真实证书并替换 `caBundle`；
3. 保持 `failurePolicy: Fail`；
4. 先验证 admission 服务 ready，再给保护命名空间添加 `proofmesh.io/agent-policy=enforced`；
5. 应用默认拒绝 NetworkPolicy；
6. 从真实 Agent Pod 验证只能访问 DNS 与 gateway，不能直连受保护上游。

Admission 会检查 containers、initContainers、ephemeralContainers；生产镜像必须为 `@sha256`；active policy digest 必须精确命中白名单。NetworkPolicy 不是身份系统，上游还必须只接受 gateway 的 mTLS/SPIFFE 身份。

## 7. 生产模式

设置 `PROOFMESH_ENV=production` 后，运行时强制启用 OIDC/JWKS、PostgreSQL GatewayStore 和 mTLS 远程签名代理；静态 bearer、SQLite GatewayStore 与文件私钥均会启动失败。部署前必须提供：

```text
config/trust/action-issuers.json
data/policies/refund_policy.json
HTTPS OIDC issuer + audience + JWKS URI
PostgreSQL DSN
HTTPS signing-proxy URL + CA/client certificate/client key
```

五把 ProofMesh 内部密钥用途完全分离：策略只签 Action Passport，Gateway 只签执行回执，Workflow 只签任务转换，Proof Sealer 只签完整证明，商务上游只签新鲜终态快照。生产控制面只向签名代理发送 canonical payload；代理返回的 JWS 会在本地重新核对 key id、token type、payload 与签名，任何替换均失败关闭。第六类 Human approval 私钥仍必须位于独立审批服务。trust bundle 必须由包外流程发布，并用 `approval_issuers` 严格列出允许的企业 issuer。

基础 `docker-compose.yml` 是本地评审模式。生产形态必须显式挂载外部材料，不能依赖镜像或空 named volume 自举：

```bash
export PROOFMESH_TRUST_BUNDLE=/absolute/approved/action-issuers.json
export PROOFMESH_SIGNER_MTLS_DIR=/absolute/secret/signer-mtls
export PROOFMESH_SIGNER_BASE_URL=https://signer.internal.example
export PROOFMESH_DATABASE_URL=postgresql://proofmesh@postgres.internal/proofmesh
export PROOFMESH_OIDC_ISSUER=https://idp.example
export PROOFMESH_OIDC_AUDIENCE=proofmesh-api
export PROOFMESH_OIDC_JWKS_URI=https://idp.example/.well-known/jwks.json
docker compose -f docker-compose.yml -f docker-compose.production.yml up --build
```

`RemoteEd25519Signer` 是已实现的 KMS/HSM 签名代理协议适配器，不包含任何私钥；真正上线仍需把代理端接到五把独立 key policy，并运营轮换、吊销和审计。`PostgresGatewayStore` 已实现 operation row lock、预算原子累计、lease generation fence、reconcile-before-retry 与 UNKNOWN 队列语义；当前工作流主状态和商务靶场仍是单机 SQLite，不能把这一 Gateway 数据层适配器描述成全系统 HA。商务快照 attestation 在靶场由远程签名协议产生；接真实支付/CRM 时必须改为上游自身的签名响应或可信审计代理。

同时必须补齐：

- OIDC/JWKS 已实现严格签名、issuer、audience、时效、role、tenant 验证；仍需企业 IdP 实例、撤销/会话事件、工作负载身份、API mTLS 与速率限制；
- 将当前 synthetic reference approval issuer 替换为企业 IdP / 审批服务，真实提供 auth_time、ACR / AMR，并由独立发布流程维护 issuer allowlist；
- 为 Gateway 接入独立风险事实 / 决策服务；当前能独立执行金额阈值，但无法在控制面失陷时重算“低金额、高风险分”的人工审批要求；
- Workflow/Commerce/Evidence 其余 SQLite 状态迁移到 PostgreSQL 或等价高可用存储，补迁移、备份恢复与人工对账 UI；

发布链包含确定性 CycloneDX SBOM、哈希锁定 CI 依赖、commit-pinned Actions、in-toto/SLSA predicate 与 GitHub OIDC build provenance。未在公开 GitHub 运行前，本地 predicate 明确标记 `UNVERIFIED-LOCAL-SOURCE`，不得宣称已有公开 Rekor/attestation 记录。
- 上游稳定 operation id、幂等、查询/对账 API、超时、熔断和限流；
- WORM 证明存储、外部链头锚定、可信时间与审计导出；
- PII 脱敏、保留期限、访问审批、数据主体与法务保全流程；
- 性能容量测试、故障演练、第三方安全评估和生产回滚 Runbook。

## 8. 清理与重复演示

固定演示工单不能被重复退款。切换到一套全新演示状态前先停止服务，再运行：

```bash
make clean-runtime
```

该命令只删除本项目的运行数据库、开发私钥、运行工作流证明和临时状态，不删除公开 benchmark 与静态提交材料。删除运行材料会丢失本地审计历史，因此不要在需要保全的环境中使用。
