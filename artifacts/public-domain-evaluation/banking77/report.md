# Banking77 公开数据盲测报告

## 结论

评测执行完整，但当前结论是 **NOT_PRODUCTION_READY**。这不是装饰性 demo 分数：官方
10,003 条训练样本拟合后，在从未参与训练或阈值选择的 3,080 条官方测试样本上一次性评估。

- 77 类意图准确率：2519/3080 = 81.79%（95% Wilson CI 80.38%–83.11%）
- 77 类 macro-F1：81.82%
- 退款相关检测：TP=68、FP=3、FN=12；precision=95.77%、recall=85.00%、F1=90.07%
- 退款三路适配准确率：3063/3080 = 99.45%
- 项目策略映射下，受保护意图未被自动放行：1858/1880 = 98.83%
- 自动只读覆盖：645/3080 = 20.94%
- ActionGateway 惰性路由审计：3/3；外部验签 receipt：3/3
- 无外部审批的敏感 execute 旁路：拒绝 80/80（95% Wilson CI 95.42%–100.00%），原因 `{'approval_assertion_missing': 80}`，敏感 upstream dispatch=0

## 质量门槛

- FAIL — `intent_accuracy_at_least_0_90`
- FAIL — `refund_detection_recall_at_least_0_95`
- FAIL — `protected_safe_routing_at_least_0_99`
- PASS — `unapproved_sensitive_bypass_pass_rate_zero`

## 方法与防泄漏

- 数据源：[PolyAI Banking77](https://github.com/PolyAI-LDN/task-specific-datasets)，固定 commit `57ec275d8078af65b7731c2a98be812d844a6d6b`，许可 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)；论文见 [Casanueva et al. (2020)](https://arxiv.org/abs/2003.04807)。
- 分类器只用 Python 标准库实现：word unigram/bigram、平滑 IDF、L2 归一化类别质心。
- 置信阈值只在官方 train 内做逐类 80/20 确定性拆分校准；official test 标签没有参与训练、映射或选阈值。
- 结果文件保留 3,080 个 `text_sha256`，不重新分发原始文本。
- `request_refund` 只路由到 `COLLECT_REFUND_CONTEXT`；`Refund_not_showing_up` 只路由到 `READ_ONLY_REFUND_INVESTIGATION`；其余为 `OUT_OF_SCOPE_HANDOFF`。三者都不执行退款。
- 三档风险标签是 ProofMesh 声明的策略投影，不是 Banking77 数据集真值。
- F1 不是二项比例，因此不虚构 Wilson 区间；报告保留 F1 所需的逐类混淆计数，并只对可解释为成功/尝试的比率给 Wilson 区间。

## 不能据此宣称

本报告不能证明真实退款、支付/CRM sandbox、生产闭环、真实客户收益、中文效果或模型 Agent
能力。ActionGateway 负控只证明无审批的合成敏感 execute 合约在到达惰性 upstream 前被拒绝。
