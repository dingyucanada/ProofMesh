# Banking77 公开数据评测说明

## 为什么要加这组评测

AgentDojo 授权合约回放证明的是 ActionGateway 能否接受合法合约并拒绝越权合约，不能回答客服
自然语言是否能被正确理解。Banking77 提供 77 类、10,003 条官方训练样本和 3,080 条官方测试
样本，适合补充“意图识别与安全分流”的外部数据证据。

## 可复现命令

先将 PolyAI 官方仓库固定在 commit
`57ec275d8078af65b7731c2a98be812d844a6d6b`，然后从项目根目录运行：

```bash
python3 scripts/benchmarks/run_banking77_routing.py \
  --dataset-root ../../work/public-datasets/task-specific-datasets
```

运行器会校验 train、test 与 LICENSE 的 SHA-256；任何文件漂移都会失败。它只用 official train
拟合透明 TF-IDF 类别质心分类器，在 train 内校准保守阈值，再一次性评估 official test。

## 输出

- `report.json`：分子、分母、比率、95% Wilson 区间、质量门槛与不宣称项；
- `report.md`：人类可读结论；
- `case-results.jsonl`：3,080 条逐样本结果，只含文本 SHA-256，不含原始话术；
- `source-lock.json`：来源、commit、许可和文件摘要。

## 路由边界

- `request_refund` → `COLLECT_REFUND_CONTEXT`，只收集上下文；
- `Refund_not_showing_up` → `READ_ONLY_REFUND_INVESTIGATION`，只读调查；
- 其余 → `OUT_OF_SCOPE_HANDOFF`，交给人工或其他已验证流程。

三条路由都不会执行退款。三档风险标签是 ProofMesh 的公开策略映射，不是 Banking77 自带真值。
评测只让少量代表性路由进入惰性审计 sink；对 official test 的 80 条退款相关样本各做一次
合成敏感 execute 负控，用真实 ActionGateway 验证缺少外部审批断言时在 upstream 前拒绝。
负控只携带文本 SHA-256；支付和 CRM 后端对该评测不可达。

## 数据许可与限制

Banking77 来自 [PolyAI task-specific-datasets](https://github.com/PolyAI-LDN/task-specific-datasets)，
采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)，应引用
[Casanueva et al., Efficient Intent Detection with Dual Sentence Encoders](https://arxiv.org/abs/2003.04807)。
本项目只提交派生指标、标签和不可逆文本摘要，不重新分发原始话术。

该评测不能证明真实支付/CRM 接入、真实退款、生产业务结果、中文效果或模型 Agent 能力。

## CFPB 公开投诉 no-write shadow

第二层使用 CFPB Consumer Complaint Database 官方 API，在固定 `2023-01-01` 至 `2024-01-01` 时间窗按四个产品各取 60 条公开 narrative。选择规则为官方 API `created_date_asc` 的前 60 条，无人工挑样。运行器只在内存读取 narrative，随后仅保留不可逆摘要、产品枚举、人工复核队列和置信桶；投诉 ID、公司、州、邮编、精确日期与 narrative 均不落盘。

```bash
PYTHONPATH=src python3 scripts/benchmarks/run_cfpb_shadow.py
PYTHONPATH=src python3 scripts/benchmarks/run_cfpb_shadow.py --validate-only
```

最终运行 240/240 成功：敏感人工复核 148、一般人工复核 92；原文 artifact=0、身份字段=0、写工具调用=0、不安全写入=0。CFPB issue 不是授权真值，投诉不是统计代表性样本，narrative 也未由 CFPB 核实，因此这一层不报告 accuracy，不证明客户试点或生产业务结果。

发布包包含 `artifacts/public-domain-evaluation/cfpb/{manifest,provenance,report,case-results}.json*`，但不包含远程 JSON 响应或 narrative。来源与使用边界见 [CFPB 官方数据使用说明](https://www.consumerfinance.gov/complaint/data-use/)；机器完整性由 manifest SHA-256 和 release gate 验证。
