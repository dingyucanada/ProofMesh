# Contributing

欢迎提交 Action Passport、网关恢复协议、ToolBackend、Verifier、AgentTeams Skill、部署和文档改进。

1. 保持改动单一聚焦，并说明行为、数据模型或威胁边界变化。
2. 运行 `PYTHONDONTWRITEBYTECODE=1 pytest -q`。
3. 修改 AgentTeams/Skills 时运行 `make package-agentteams && make validate-agentteams`。
4. 修改网关合同或 benchmark 时运行 `make validate-agentdojo && make replay-agentdojo`，并提交确定性报告差异及原因。
5. 新增有副作用的工具时，必须定义最小 scope、资源键、closed input schema、幂等/对账、预算、审批条件、补偿和 postcondition 测试。
6. 修改 proof schema 时，必须同时增加有效签名但语义错误的负向测试；不能只测试字节篡改。
7. 不得提交 `.env`、私钥、token、管理员口令、模型 key、个人数据或许可证不兼容材料。
8. PR 需给出复现命令、失败模式、向后兼容性和生产迁移影响。

安全问题请走私密报告渠道，不要在公开 Issue 提供可直接利用的凭证或攻击细节。

