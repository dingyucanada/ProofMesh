# Vendor sandbox integration quickstart

权威安全/数据边界和全部配置说明见 [vendor-sandbox-boundary.md](vendor-sandbox-boundary.md)。本页只列集成入口，便于 release gate 和独立复现者发现。

- 适配器：`src/proofmesh/vendor_sandbox.py`
- readiness probe：`PYTHONPATH=src python3.11 scripts/vendor_readiness_probe.py`
- 显式只读联网 probe：`PYTHONPATH=src python3.11 scripts/vendor_readiness_probe.py --network`
- 本地 provider HTTP 合同：`pytest -q tests/test_vendor_sandbox.py`
- 环境变量模板：`.env.example`

这组文件证明真实 Stripe / HubSpot 协议的 sandbox readiness 与本地合同测试，不证明真实外部账户已执行。真实 test-account evidence 必须由账户持有人在私密环境配置 secret 后另行产生。
