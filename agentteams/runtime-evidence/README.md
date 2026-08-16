# Runtime evidence boundary

本目录保存 2026-08-13 对官方 AgentTeams `v1.2.0-beta.1` 控制面、最小本地 REST 兼容镜像与 ProofMesh 七成员 Team 的去敏运行快照。

- `control-plane.json`：锁定补丁/二进制/镜像 provenance，以及 Manager、Human、7 Workers 与 Team=`Active` 的选定状态；
- `compatibility-findings.json`：七项真实 beta 兼容发现；`ATB-006` 已通过本地补丁实跑缓解，`ATB-007` 的可选 Matrix 文件发布仍开放；
- `teamharness-direct.json`：完整 direct MCP project/task 生命周期已运行，包含步骤归属、关联 ID、同步布尔值和状态文件 SHA-256。

本目录不保存原始 Human JSON、HTTP headers、容器环境、Manager 配置、对象存储配置、密码、token、bearer key 或其他凭证。Team 房间仅保留 SHA-256，不保存原始 room ID。`scripts/validate_agentteams.py` 检查证据结构和常见敏感字段，但不会查询 Docker，因此其静态输出始终保持 `runtime_verified: false`。

## 证据等级与边界

- L1：补丁锁、官方父镜像摘要、本地二进制与兼容镜像摘要；
- L2：Manager=`Running`、Human=`Active`、7/7 Worker=`Running`；
- L3：真实 Team UID、`Active`、唯一 Leader、6 个 ready Workers 与七成员 roster；
- L3：Leader 与 `ticket-intake` 两个真实容器之间完成 `create → plan → delegate → ack → submit → check → accept`，委派/确认/提交/检查均保留同步或拉取结果；
- 非 L4：全部 lifecycle 调用均为 direct MCP control-plane invocation。Manager 使用 placeholder provider 且 `welcomeSent=false`，所以 `model-driven`/自主多 Agent 协同仍未验证。

`submit_task` 的任务状态和 result 已同步成功；其可选 Matrix artifact publication 因 workspace `shared` 符号链接路径校验失败。这一失败记录为 `ATB-007`，不作为 lifecycle 成功证据，也不影响 Leader 随后的 `pulled=true/effective=true` 检查和 `completed` 验收。

回滚边界：本地镜像只在官方 embedded 摘要之上替换 `/usr/local/bin/hiclaw-controller`。原官方 Controller 容器被停止并保留；持久卷、Manager、Workers 均未删除。私有重建参数只存在于运行主机的 `0600` 临时文件，不进入本证据目录。
