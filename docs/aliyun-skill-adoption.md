# 阿里云官方 Skill 采用契约

## 决策

ProofMesh 不以云产品数量作为得分点。当前最有必要的官方能力是只读观测：在将 Gateway deny audit、workflow event 和 verifier report 投递到阿里云 SLS 后，由官方 `alibabacloud-sls-query` Skill 查询一次 workflow 的授权、执行、补偿和验真链。

选择它的理由：

- 与 ProofMesh 的核心证据链直接相关，而不是装饰性 Logo；
- 仅需 SLS `GetIndex` 与 `GetLogsV2` 读权限，不获得退款或业务写权限；
- 官方 Skill 明确要求先检查索引、使用 `get-logs-v2`、为每次 API 调用附带可追踪 user-agent，并在凭证缺失时停止；
- 查询层可替换为本地 SQLite / OpenSearch / 其他日志后端，Proof 的密码学有效性仍由独立 Verifier 决定。

## 角色与边界

| 项目 | 合同 |
|---|---|
| 调用角色 | `outcome-verifier` 或只读 `auditor`，不能由 `action-executor` 复用凭证 |
| 官方 Skill | `alibabacloud-sls-query` |
| 最小 RAM 权限 | `log:GetIndex`、`log:GetLogStoreLogs` |
| 数据范围 | 单个租户的脱敏 Trace / Log / Metric 索引；不上传 Passport、私钥、Bearer token、原始客户 PII |
| 查询键 | `tenant_id` + `workflow_id`；时间窗必须显式 |
| 输出 | 查询结果摘要、缺失序号、异常原因；不得把 SLS 查询成功当作 Proof 有效 |
| 失败 | 无凭证、无索引、越权、查询不完整或网络错误均停止；不切换账户，不扩大权限 |
| 审计 | 保留官方 Skill user-agent / session id 与 ProofMesh workflow id 的映射 |

## 推荐索引

```text
tenant_id, workflow_id, event_type, tool, decision, reason,
revision, call_id, operation_id, receipt_digest, proof_digest,
gen_ai.agent.name, gen_ai.operation.name, timestamp
```

## 示例只读问题

```text
给定 tenant_id=acme-cn、workflow_id=<id> 和明确时间窗：
1. 先读取 Logstore 索引配置；
2. 查询 workflow.received 到 proof.sealed 的事件并按 revision/时间排序；
3. 聚合 gateway denied reason；
4. 检查 call_id、operation_id、receipt_digest 是否缺失或重复；
5. 返回证据缺口，不执行任何退款、补偿或审批动作。
```

## 当前验证等级

本文件是**采用与权限契约**，不是云端运行证据。当前提交没有阿里云账号 / RAM 凭证，也没有把真实日志上传 SLS；因此不得声称官方 Skill 已安装、已调用或已产生云端效果。复赛验收必须附：官方 Skill pin、脱敏导出 schema、RAM policy、查询命令、带 user-agent 的日志、查询结果和 Proof 包外复验报告。

官方来源：

- https://skills.aliyun.com/skills/alibabacloud-sls-query
- https://skills.aliyun.com/
- https://www.goaihz.com/tracks?track=infra
