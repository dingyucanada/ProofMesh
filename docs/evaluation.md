# 评测设计、结果与可证伪边界

## 1. 评测问题

ProofMesh 的核心主张不是“模型不会受攻击”，而是：给定一份明确的用户工具合同，网关能否只放行精确授权的调用；在重试和崩溃语义下能否不重复副作用；执行结果能否由外部信任材料复算。评测因此分为四层：

1. 角色状态机、认证、租户、审批与业务不变量；
2. 网关凭证、预算、幂等、租约、接管、对账和 `UNKNOWN`；
3. 五类用途隔离签名、商务上游 snapshot attestation、哈希链与跨证据业务语义；
4. 基于公开 AgentDojo v1 数据的授权合同一致性回放。

## 2. 本地业务闭环

固定的三条端到端用例运行在真实 SQLite 事务靶场：

| 用例 | 预期门禁 | 真实副作用 | 独立终态 |
|---|---|---|---|
| `TKT-LOW-001` | 自动策略 | 退款 8000 CNY minor units、工单关闭 | `COMPLETED`，余额准确减少，Proof Valid |
| `TKT-HIGH-001` | `WAITING_APPROVAL` | 审批后退款 12900、工单关闭 | 审批只转 `AUTHORIZED`，Executor 后 `COMPLETED` |
| `TKT-SAGA-001` | 自动策略 | 退款成功、CRM 注入故障、补偿恢复 | `COMPENSATED`，余额恢复、工单开放、Proof Valid |

本地控制台已按实际 HTTP 合同验证三条路径；自动化测试同时检查旧 revision 冲突、并发审批只能成功一次、请求者不能审批、计划/范围漂移拒绝、跨租户读取拒绝和角色工具升级拒绝。

## 3. 网关故障与并发不变量

测试覆盖：

- 同一操作旧 token 过期后，新的等价 Action Passport 只能恢复一次副作用和一次预算扣减；
- 任一 request/context/policy/approval/resource/currency 绑定变化均不得接管；
- 上游调用超过租约时由 heartbeat 续租，并发调用不得二次派发；
- 相同幂等键回放返回同一 result/receipt，不追加链项；
- 不同幂等键复用一次性 Passport 被拒绝；
- 过期租约先向上游对账，`UNKNOWN` 永不自动重放；
- 工作流累计预算按 tenant/workflow/currency 计算，更换 Agent subject 不能绕过；
- passport、gateway receipt、task receipt、proof seal、business snapshot 五类 key usage 分离；错误用途、伪类型、过期、撤销和未生效 key 全部拒绝。

完整测试命令：

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -q
```

## 4. AgentDojo 公开数据锁定

数据来源固定为 AgentDojo `0.1.35`、benchmark `v1`，许可和归属见 `data/benchmarks/THIRD_PARTY_AGENTDOJO.md`。导出文件：

```text
artifacts/public-benchmark/cases.jsonl
bytes: 1,863,974
SHA-256: 20146525d139f83d732bc47e0286b0eccdf67118cdc5eba84f5f079863f5546b
```

| Suite | User tasks | Injection goals | Cases |
|---|---:|---:|---:|
| workspace | 40 | 6 | 240 |
| slack | 21 | 5 | 105 |
| travel | 20 | 7 | 140 |
| banking | 16 | 9 | 144 |
| **总计** | **97** | **27** | **629** |

artifact 保留官方 ground-truth 工具轨迹、placeholder 参数和未解析 token，不编造模型或真实环境才能提供的值。共有 1105 个带 placeholder 参数的调用、794 个未解析 placeholder occurrence。

复现导出与锁校验：

```bash
# 锁校验只需发布包自带数据与项目 Python
make validate-agentdojo

# 仅当需要从官方 AgentDojo 重新导出时，显式指定装有 0.1.35 的解释器
make export-agentdojo AGENTDOJO_PYTHON=/absolute/path/to/agentdojo-0.1.35/bin/python
```

复现者从发布包解压后应先运行 `make validate-agentdojo`；不依赖开发者工作区中的 `work/` 路径。重新导出属于可选溯源步骤，需要单独安装并锁定官方 AgentDojo 0.1.35。

## 5. 授权合同一致性回放

命令：

```bash
PYTHONPATH=src python3 scripts/benchmarks/run_authorization_contract_replay.py
```

实验将 2,159 个 user ground-truth calls 放入固定信封，绑定 case digest、lane、sequence、原函数、原参数与原调用摘要；随后使用真实 `ActionGateway`、文件型临时 SQLite、确定性 TraceBackend 和 case artifact 之外的 Ed25519 trust bundle 执行。TraceBackend 只记录授权效果，不调用原 AgentDojo 外部服务；placeholder 保留为原始符号值，不猜测真实值。

### 核心结果

| 指标 | 结果 | Wilson 95% CI |
|---|---:|---:|
| 合法合同接受 | 2,159 / 2,159 | [0.998224, 1.000000] |
| 同幂等键缓存回放 | 2,159 / 2,159 | [0.998224, 1.000000] |
| 实际 TraceBackend 副作用 | 2,159；重复 0 | 精确计数 |
| 换幂等键复用一次性凭证拒绝 | 2,159 / 2,159 | [0.998224, 1.000000] |
| 同工具参数错配错误通过 | 0 / 2,159 | [0, 0.001776] |
| 工具错配错误通过 | 0 / 2,159 | [0, 0.001776] |
| 上下文错配错误通过 | 0 / 2,159 | [0, 0.001776] |
| 未注册调用错误通过 | 0 / 629 | [0, 0.006070] |
| Receipt 外部验签与链验证 | 各 2,159 / 2,159 | [0.998224, 1.000000] |

拒绝审计共 9,265 行：参数、上下文、工具错配和 `passport_exhausted` 各 2,159，`tool_not_registered` 629。拒绝不会伪装成签名 execution receipt；当前拒绝证据是持久化 SQLite audit row。

### 本机延迟画像

| 阶段 | n | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| 首次合法执行 | 2,159 | 1.132 ms | 2.429 ms | 4.789 ms |
| 同键缓存回放 | 2,159 | 0.404 ms | 0.709 ms | 1.278 ms |
| 参数错配拒绝 | 2,159 | 0.410 ms | 0.704 ms | 1.429 ms |
| 未注册拒绝 | 629 | 0.173 ms | 0.346 ms | 0.670 ms |

这是单机文件型 SQLite 工程画像，不是跨机器性能排名。完整 JSON/Markdown 报告位于 `artifacts/public-benchmark/authorization-contract-replay.*`。

## 6. 不能从这些结果推出什么

- 不能推出模型提示注入攻击成功率（ASR）为 0；实验没有运行模型或攻击提示。
- 不能推出 AgentDojo task utility；TraceBackend 不执行 Gmail、银行、Slack 或旅行服务的真实任务。
- 不能推出端到端 Agent 自主能力；ground-truth 调用来自公开 benchmark，而不是模型自己规划。
- 不能推出生产吞吐、可用性或跨区域延迟；当前是本机 SQLite。
- 不能推出第三方安全认证；项目尚未接受独立渗透测试、形式化验证或生产历史盲测。

这些限制是实验设计的一部分。ProofMesh 本轮证明的是“调用合同与执行控制面是否一致”，不是替代模型安全评测。后续可在独立环境中叠加官方 AgentDojo 模型 runner，分别报告 utility、ASR 与 ProofMesh 拦截后的 counterfactual 结果，三者不得混为一个数字。
