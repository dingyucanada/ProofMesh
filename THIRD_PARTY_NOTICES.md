# Third-Party Notices

ProofMesh 自有代码采用 Apache License 2.0。第三方组件保留各自版权与许可证；使用者应以锁定版本的上游发布为准。

## 运行与测试依赖

- FastAPI — MIT — https://github.com/fastapi/fastapi
- Uvicorn — BSD-3-Clause — https://github.com/encode/uvicorn
- Pydantic — MIT — https://github.com/pydantic/pydantic
- PyYAML — MIT — https://github.com/yaml/pyyaml
- cryptography — Apache-2.0 OR BSD-3-Clause — https://github.com/pyca/cryptography
- pytest — MIT — https://github.com/pytest-dev/pytest
- HTTPX — BSD-3-Clause — https://github.com/encode/httpx

## AgentTeams

项目提供面向 AgentTeams `v1.2.0-beta.1` 的 Human/Worker/Team 资源、Skill 包和 bootstrap，但不复制 AgentTeams 源码或容器镜像到本发布包。官方仓库：https://github.com/agentscope-ai/AgentTeams 。实际部署须遵守该版本的上游许可证与镜像条款。

## AgentDojo

`artifacts/public-benchmark/cases.jsonl` 是由 AgentDojo `0.1.35` / benchmark `v1` ground-truth 元数据确定性导出的规范化 artifact，用于授权合同一致性回放。AgentDojo 使用 MIT License；具体来源、版本、导出方法、边界与归属见 `data/benchmarks/THIRD_PARTY_AGENTDOJO.md`。

## Banking77 与 CFPB 公开数据

- Banking77 来自 PolyAI `task-specific-datasets`，锁定 commit `57ec275d8078af65b7731c2a98be812d844a6d6b`，采用 CC BY 4.0。发布包不重新分发原始问句，只包含来源锁、归属说明、不可逆文本摘要和聚合评测结果；详见 `artifacts/public-domain-evaluation/banking77/ATTRIBUTION.md` 与 `docs/public-dataset-benchmark.md`。
- CFPB Consumer Complaint Database 是美国 Consumer Financial Protection Bureau 的公开投诉数据。评测只通过官方 API 在内存处理公开 narrative，发布包不保存 narrative、投诉 ID、公司、州、邮编或其他身份字段，只保存摘要和受控枚举；官方 API 元数据返回 CC0。该数据不是统计代表性样本，叙述未由 CFPB 核实；详见 `artifacts/public-domain-evaluation/cfpb/provenance.json` 与官方 [data-use 页面](https://www.consumerfinance.gov/complaint/data-use/)。

两组公开数据只用于输入适配、意图分流与 no-write shadow 评测，不能替代客户授权数据、企业试点、真实退款结果或生产准确率。

项目不捆绑模型权重、真实企业数据或第三方 API Key。AgentTeams 清单中的模型名称只是可替换配置，不表示本发布包提供模型服务或许可。
