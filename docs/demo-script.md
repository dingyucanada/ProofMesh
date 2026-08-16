# ProofMesh 现场 Demo 脚本（4 分 30 秒）

## 演示前 10 分钟

1. 停止旧服务，运行 `make clean-runtime`；
2. 加载七个 Agent 角色、职责分离的 Approver 与 Auditor 共九个不同 token，启动 `127.0.0.1:8000`；
3. 确认 `/health` 与 `/ready` 成功；
4. 预跑 `pytest -q` 和 629 case 报告校验；
5. 浏览器保持 100% 缩放，先折叠角色凭证；
6. 另开终端准备证明验真命令，断网也能运行。

不要在录屏或投屏中展示 `.env`、AgentTeams 管理员口令、模型 key 或 gateway consumer key。

## 0:00–0:35｜痛点与主张

画面：控制台首页。

讲述：

> 多 Agent 会调用退款、工单和账户工具，但企业真正担心的是：模型临时改了参数怎么办？重试会不会退两次？审批是不是顺手就执行了？日志能不能被整包重签？ProofMesh 给每个副作用发一张短期、精确范围的 Action Passport，并在执行后生成可由系统之外复算的证明。

指出页面右上角 `mcp-action-gateway · external trust` 与七角色 DAG。

## 0:35–1:35｜高风险审批不是执行按钮

1. 选择“高风险人工审批”；
2. 点击“创建隔离案件”；
3. 点击“推进至下一门禁”。

必须看到：

- `WAITING_APPROVAL`；
- `next task = record_human_approval`；
- 推进按钮禁用；
- 计划摘要与 scope 展示；
- 事件链 `VALID`；
- 此时没有 `saga.executed`。

讲述：

> Orchestrator、Intake、Investigator 和 Policy 分别提交带 revision 的签名任务回执。策略冻结了金额、订单版本和动作范围，然后项目真正暂停。审批绑定 plan digest 与完整 scope，而且请求者不能审批。

点击“记录限定审批”。必须停在 `AUTHORIZED`，`next task = execute_authorized`。

> 注意，审批只改变授权状态，没有隐藏执行；Executor 仍需单独领取任务。

再点击“推进至下一门禁”，到 `COMPLETED`，点击“离线语义验真”，必须显示 `PROOF VALID`。

## 1:35–2:40｜故障发生在真实副作用之后

点击“开始另一案件”，选择“下游故障与补偿”，创建并推进。

必须看到：

- 最终 `COMPENSATED`；
- `next task = terminal`；
- 网关回执数量增加；
- 事件包含 `saga.executed`、`postconditions.verified`、`workflow.sealed`；
- 点击验真得到 `PROOF VALID`。

讲述：

> 这不是把 JSON 里的 `passed` 改成 true。系统先真实扣减订单可退款余额并创建退款，随后 CRM 关单故障。Executor 使用另一张补偿许可证恢复余额；Verifier 重新读取退款、订单和工单，确认退款为 COMPENSATED、余额回到原值、工单仍开放，Memory Worker 才能封存证明。

## 2:40–3:30｜为什么重试不会退两次

画面：架构/PPT 的 logical operation 图或 `docs/architecture.md`。

讲述：

> ProofMesh 把逻辑操作和本次 token 分开。逻辑操作唯一绑定 tenant、workflow、tool 和 idempotency key；尝试拥有 owner、generation、lease 和 heartbeat。进程崩溃后，新凭证先向上游按稳定 operation id 对账。确认成功就复用结果，确认未发生才重派；结果不确定直接进入 UNKNOWN，绝不自动赌一次。

补一句验证证据：旧 token 换新等价 token、长调用超过租约、并发重试和 UNKNOWN 均有自动化测试。

## 3:30–4:05｜公开 629 case，不冒充模型评测

画面：`artifacts/public-benchmark/authorization-contract-replay.md`。

讲述：

> 我们固定了 AgentDojo 0.1.35/v1 的 97 个用户任务、27 个注入目标、629 个组合，共 2,159 个用户 ground-truth 调用。合法合同全部接受，同键重放没有第二次副作用；参数、工具和上下文错配各 0 次错误通过，2,159 个 receipt 全部通过外部验签和链验证。

必须立刻说明边界：

> 这是授权合同一致性回放，不是模型 ASR、AgentDojo utility 或提示注入检测率。我们没有运行模型，也没有把这个数字包装成模型安全率。

## 4:05–4:30｜AgentTeams 与结束语

画面：7 Worker/Team 图或运行证据页。

讲述：

> 七个 qwenpaw Worker、一个 Human、一个 Team 定义和七个官方结构 Skill ZIP 已锁定 AgentTeams v1.2.0-beta.1。我们为 beta Manager REST 未映射 `workerMembers` 的缺口提供了最小兼容补丁：patched Controller 已运行，Team=`Active`，Human Active、7/7 Worker Running、7/7 TeamHarness health，并跨 Leader/Worker 完成 direct MCP `create → plan → delegate → ack → submit → check → accept`。这只证明控制面生命周期；当前仍为 `modelDriven=false`，不冒充模型自主协同。

结束语：

> AgentTeams 让多个 Agent 把事情做完；ProofMesh 证明这件事被正确的人、在正确范围内、只做了一次，失败后恢复到了可验证状态。

## 断网/页面故障备用

```bash
make clean-runtime
PYTHONPATH=src python3 -m proofmesh.cli --home . demo-refund TKT-SAGA-001
PYTHONPATH=src python3 -m proofmesh.cli --home . verify-proof \
  artifacts/workflows/<WORKFLOW_ID>/workflow-proof.json
PYTHONPATH=src python3 scripts/benchmarks/run_authorization_contract_replay.py
```

若现场时间只剩 2 分钟：只演示高风险停门、审批只到 `AUTHORIZED`、Saga `COMPENSATED + PROOF VALID`，629 case 用一页结果带过。
