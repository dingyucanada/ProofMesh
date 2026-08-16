# ProofMesh × AgentTeams v1.2.0-beta.1

本目录同时包含退款闭环的 **AgentTeams 静态部署包** 和一份 **去敏真实控制面快照**。资源、构建源和验证规则锁定到官方 `v1.2.0-beta.1`、提交 `78d0ceda336befa6e62bf89fc1a6b08b965e128d`。

- 1 个 `Human`：`proofmesh-approver`；
- 7 个独立 `Worker`：全部使用 `runtime: qwenpaw`；
- 1 个解耦式 `Team` 清单：通过 `workerMembers` 引用 Workers，仅 `case-orchestrator` 为 `team_leader`；
- 7 个退款阶段：`create → normalize → context → policy → execute → verify → memory`；
- 7 个 Worker ZIP：官方包根目录为 `manifest.json`、`config/SOUL.md`、`config/AGENTS.md`、`skills/<name>/...`；
- 每个 Skill 都有版本合同、Draft 2020-12 输入/输出 JSON Schema、失败策略、安全边界、验证证据和错误码。

`schema-lock.yaml` 固定了 Human、Worker、Team CRD 和官方 QwenPaw TeamHarness E2E 的审计来源。静态校验不跟随浮动的 `main` 分支。

`upstream/` 另含一个可提交给 AgentTeams 上游的最小 REST 修复资产：补丁、Issue/PR 文稿、release commit 与三个基线文件 SHA-256 锁，以及不改源码树的离线合同验证器。本地兼容镜像已对该补丁完成测试、构建和运行验收，但它仍不是上游发布。

## 当前可核验的运行证据

2026-08-13 已在本机官方 beta 控制面之上部署只替换 Controller 二进制的最小兼容镜像，并完成 Team 与 direct MCP 生命周期验收。证据位于 `runtime-evidence/`，只含选定字段、摘要和布尔型检查，不含原始 Human JSON、HTTP headers、容器环境或任何凭证。

| 层级 | 实测结果 | 可声明范围 |
|---|---|---|
| Controller / Manager | patched Controller=`running`；Manager=`Running`、runtime=`copaw` | 官方父镜像加单二进制兼容层已实跑；非上游发布 |
| Human | `proofmesh-approver=Active` | Human 资源已提交并 reconcile |
| Workers | 7/7 `Running`，容器均为 `running` | 七个 QwenPaw Worker 已提交并 reconcile |
| Worker 资产 | 7/7 自定义 Skill 存在；角色 MCP URL、HTTP transport、Authorization 投影通过布尔检查 | 包分发和最小权限端点投影已验证，不披露 bearer value |
| TeamHarness 插件 | 7/7 Worker 的直接 MCP `health` 返回 `ok=true/tool=health` | 七个 MCP server 均可调用 |
| Team | `Active`；唯一 Leader + 6 Workers，`leaderReady=true`、`readyWorkers=6/6` | 解耦 `workerMembers` Team 已通过 REST 创建并 reconcile |
| 直接 TeamHarness MCP 生命周期 | 7/7 passed | `create_project → plan_dag → delegate_task → ack_task → submit_task → check_task → accept_task_result` 跨 Leader/Worker 完成；证据步骤简称 `accept_task` |
| 模型驱动协同 | 未验证 | Manager 使用明确 placeholder provider，`welcomeSent=false` |

静态 validator 会校验这些证据的结构、版本一致性和去敏边界，但不会查询 Docker，因此输出始终保持 `runtime_verified: false`。

## 角色与最小权限 MCP

| DAG | Worker | Skill | ProofMesh MCP URL | 角色可见工具 |
|---|---|---|---|---|
| create | `case-orchestrator` | `orchestrate-refund-case` | `/mcp/roles/orchestrator` | `create_case`, `get_state` |
| normalize | `ticket-intake` | `normalize-refund-case` | `/mcp/roles/intake` | `normalize_case` |
| context | `context-investigator` | `collect-refund-context` | `/mcp/roles/investigator` | `gather_context` |
| policy | `risk-policy-sentinel` | `evaluate-refund-policy` | `/mcp/roles/policy` | `evaluate_policy` |
| execute | `action-executor` | `execute-refund-saga` | `/mcp/roles/executor` | `execute_authorized` |
| verify | `outcome-verifier` | `verify-refund-outcome` | `/mcp/roles/verifier` | `verify_outcome` |
| memory | `case-memory-curator` | `curate-refund-memory` | `/mcp/roles/memory` | `curate_memory` |

静态包与当前严格协议一致：`create_case` 只接受 `ticket_id`、`tenant_id`；其余六个角色步骤都且只接受 `workflow_id`、整数 `expected_revision`、非空 `task_id`。主路径为 `RECEIVED → NORMALIZED → CONTEXT_READY → AUTHORIZED → EXECUTED → VERIFIED → COMPLETED`；需人工审批时在 `CONTEXT_READY` 与 `AUTHORIZED` 之间进入 `WAITING_APPROVAL`。独立补偿验证路径最终为 `COMPENSATED`，任一 fail-closed 分支为 `BLOCKED`。

`workers.yaml` 故意不写 `spec.package`。`hiclaw apply worker --zip` 先创建 Worker，脚本再把同一 ZIP 发布到该角色可读取的对象前缀并只更新 package；最后应用无 package 的 overlay。宿主机 `file://` URI 不会被 Manager 分发，validator 会直接拒绝。

## 静态构建与校验

从解压后的 ProofMesh 项目根目录执行：

```bash
python3 scripts/package_agentteams.py
python3 scripts/validate_agentteams.py
pytest -q tests/test_agentteams.py
```

打包器会清理旧角色 ZIP，确定性生成七个包和 `dist/SHA256SUMS`。校验覆盖固定 beta schema lock、字段白名单、QwenPaw 枚举、Human/Team 引用、唯一 Leader、角色 MCP URL、DAG/状态映射、Skill 合同与 schema、ZIP 根目录、校验和，以及 `runtime-evidence/` 的诚实性和去敏规则。

## 官方镜像与 beta 兼容层

官方 release tag 的 QwenPaw Worker 需要先从锁定源码构建，再叠加本目录的最小兼容层。AgentTeams 上游源码不随本发布包分发；先将锁定源码检出到你选择的独立目录，并保存 ProofMesh 根目录：

```bash
PROOFMESH_ROOT="$PWD"
AGENTTEAMS_SOURCE=/path/to/AgentTeams-v1.2.0-beta.1
cd "$AGENTTEAMS_SOURCE"
make build-qwenpaw-worker VERSION=v1.2.0-beta.1

cd "$PROOFMESH_ROOT"
docker build \
  -f agentteams/qwenpaw-compat.Dockerfile \
  -t proofmesh/agentteams-qwenpaw-worker:v1.2.0-beta.1-compat2 \
  .
```

兼容层只处理两个已复现问题：把 `/opt/hiclaw/qwenpaw-builtin` 对齐到运行器解析的 `/opt/agentteams/qwenpaw-builtin`，并把 `agent-client-protocol` 固定到最新已验证兼容的 `0.10.1`。另外，`bootstrap.sh` 对 beta 的 QwenPaw readiness probe 和包对象权限做可审计兼容处理。完整发现记录见 `runtime-evidence/compatibility-findings.json`。

## 两阶段引导

前置条件：官方 `v1.2.0-beta.1` Manager/Controller 已启动；兼容镜像存在；ProofMesh 角色 MCP 在 AgentTeams 网络内可达。AgentTeams 会把每个 Worker 的 gateway consumer key 注入 MCP `Authorization` header；部署时必须在 ProofMesh 侧把七个 key 分别映射为对应角色与 tenant，或由受信网关做等价身份换发。不要把 bearer key 写入 YAML、ZIP 或证据目录。

```bash
./agentteams/bootstrap.sh prepare
./agentteams/bootstrap.sh status

# 仅当所用 Manager API 已支持 spec.workerMembers 时才会成功
./agentteams/bootstrap.sh team
./agentteams/bootstrap.sh status
```

如容器名或运行器不同：

```bash
AGENTTEAMS_CONTAINER_CMD=podman \
AGENTTEAMS_MANAGER_CONTAINER=my-agentteams-manager \
./agentteams/bootstrap.sh prepare
```

`prepare` 执行静态打包/校验、逐包上传、角色前缀重发布、Worker overlay、Human 创建和 QwenPaw beta readiness 兼容。它支持从部分成功中恢复，并且 `status` 只输出去敏字段。`team` 会先要求 Human=`Active`、七个 Worker=`Running`，再尝试 Team；命令提交成功也不会被表述为 TeamHarness 成功。

## 已缓解 BLOCKER：beta Manager REST 不支持解耦 Team 提交

原始 `team.yaml` 提交曾返回 `HTTP 400: leader.name is required`。固定版本 CRD/Controller 支持 `workerMembers`，但 beta REST DTO 会丢失该字段。脚本仍会在未打补丁的官方环境识别这一诊断并 fail closed。

已在 `upstream/agentteams-v1.2.0-beta.1-manager-worker-members.patch` 准备最小上游补丁：`CreateTeamRequest` 接收 `workerMembers`；列表非空时跳过旧 `leader.name` 前置校验并原样写入 `TeamSpec.WorkerMembers`；列表为空时保留原校验和完整 legacy 映射。补丁还附三个 Go handler test，分别覆盖解耦创建、旧校验和混合迁移语义。离线复验：

```bash
AGENTTEAMS_SOURCE=/path/to/AgentTeams-v1.2.0-beta.1
python3 agentteams/upstream/verify_patch.py \
  --source "$AGENTTEAMS_SOURCE"
```

该命令校验 commit/文件/补丁哈希，把补丁应用到临时副本并检查新旧合同。本轮又在隔离 Go 1.25 builder 中运行三个新增 handler test，全部 PASS；随后编译静态 Controller 二进制，并以官方 embedded 摘要 `sha256:446703…8447` 为父镜像只替换该二进制。兼容镜像摘要为 `sha256:989e50…f067`。`hiclaw apply` 已创建 Team=`Active`，七成员 roster 与 UID 见去敏证据。

没有改用旧 inline Team 绕过，因为它会创建另一组内联 Worker并与既有七个独立 Worker 冲突。补丁只修复 REST transport，Team controller 仍负责 roster 校验、房间和 runtime reconcile。

## Direct MCP 实测与剩余边界

真实 Leader `case-orchestrator` 创建/规划/委派，真实 Worker `ticket-intake` 拉取/确认/提交，再由 Leader 拉取、检查并调用 `accept_task_result` 验收。`delegate/ack/submit/check` 的同步或拉取结果均为 true，最终 `effective=true`、`validationErrors=[]`、node=`completed`。这证明 direct MCP 控制面闭环，不证明模型自主协同。

`submit_task` 的状态和 result 文件已同步，但可选 Matrix artifact publication 因 workspace `shared` 符号链接路径校验失败（`ATB-007`）。因此不声称完整 artifact publication；该失败没有被计入七步 lifecycle 成功。

placeholder provider 仍未产生 welcome，故七角色模型自主协同依然是下一阶段的真实 blocker。

## 仍需完成的运行验收

1. 修复 `ATB-007` 并验证 Matrix artifact publication；
2. 一次真实模型驱动的退款协同，分别覆盖低风险自动退款与高风险外部 Human digest-bound approval；
3. 在模型协同中重复验证幂等、revision 冲突、非空 task receipt、CRM 补偿和 Verifier 拒绝篡改；
4. 将业务状态与 TeamHarness task/project 状态在同一模型驱动运行中关联并验收。

固定版本参考：

- [AgentTeams v1.2.0-beta.1 release](https://github.com/agentscope-ai/AgentTeams/releases/tag/v1.2.0-beta.1)
- [Worker CRD](https://github.com/agentscope-ai/AgentTeams/blob/78d0ceda336befa6e62bf89fc1a6b08b965e128d/helm/hiclaw/crds/workers.agentteams.io.yaml)
- [Team CRD](https://github.com/agentscope-ai/AgentTeams/blob/78d0ceda336befa6e62bf89fc1a6b08b965e128d/helm/hiclaw/crds/teams.agentteams.io.yaml)
- [QwenPaw TeamHarness E2E](https://github.com/agentscope-ai/AgentTeams/blob/78d0ceda336befa6e62bf89fc1a6b08b965e128d/tests/test-26-qwenpaw-teamharness-plugin-mode.sh)

## 上游补丁后续门槛

测试、构建、Team=`Active` 和 direct MCP lifecycle 已完成。后续仍需：

1. 向上游提交补丁并等待 review/合入；
2. 在上游 CI 中运行完整 `go test ./internal/server`，而非仅三个新增 handler test；
3. 修复并回归 `ATB-007`；
4. 使用真实模型凭证单独获取 L4 协同证据。
