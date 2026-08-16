.PHONY: install test demo-low demo-high demo-compensation serve export-agentdojo validate-agentdojo replay-agentdojo banking77-routing cfpb-shadow validate-public-data synthetic-http-shadow vendor-readiness vendor-readiness-network reference-evidence issue-reference-approval package-agentteams validate-agentteams validate-skills validate-external-evidence sbom clean-runtime release

PYTHON ?= python3
AGENTDOJO_PYTHON ?= $(PYTHON)

install:
	$(PYTHON) -m pip install -e '.[dev]'

test:
	pytest -q

demo-low:
	PYTHONPATH=src $(PYTHON) -m proofmesh.cli --home . demo-refund TKT-LOW-001

demo-high:
	PYTHONPATH=src $(PYTHON) -m proofmesh.cli --home . demo-refund TKT-HIGH-001

demo-compensation:
	PYTHONPATH=src $(PYTHON) -m proofmesh.cli --home . demo-refund TKT-SAGA-001

serve:
	PROOFMESH_HOME=. PYTHONPATH=src uvicorn proofmesh.api:app --host 127.0.0.1 --port 8000

export-agentdojo:
	$(AGENTDOJO_PYTHON) scripts/benchmarks/export_agentdojo.py

replay-agentdojo:
	PYTHONPATH=src $(PYTHON) scripts/benchmarks/run_authorization_contract_replay.py

banking77-routing:
	@test -n "$(BANKING77_DATASET_ROOT)" || (echo "Set BANKING77_DATASET_ROOT to the locked PolyAI task-specific-datasets checkout" && exit 2)
	PYTHONPATH=src $(PYTHON) scripts/benchmarks/run_banking77_routing.py --dataset-root "$(BANKING77_DATASET_ROOT)"

cfpb-shadow:
	PYTHONPATH=src $(PYTHON) scripts/benchmarks/run_cfpb_shadow.py

validate-public-data:
	PYTHONPATH=src $(PYTHON) scripts/benchmarks/run_cfpb_shadow.py --validate-only
	PYTHONPATH=src $(PYTHON) -m pytest -q -p no:cacheprovider tests/benchmarks/test_banking77_routing.py tests/benchmarks/test_cfpb_shadow.py

synthetic-http-shadow:
	PYTHONPATH=src $(PYTHON) scripts/benchmarks/run_synthetic_http_shadow.py

vendor-readiness:
	PYTHONPATH=src $(PYTHON) scripts/vendor_readiness_probe.py

vendor-readiness-network:
	PYTHONPATH=src $(PYTHON) scripts/vendor_readiness_probe.py --network

validate-agentdojo:
	$(PYTHON) scripts/benchmarks/validate_agentdojo_export.py

reference-evidence:
	PYTHONPATH=src $(PYTHON) scripts/generate_reference_artifacts.py

issue-reference-approval:
	@echo "Use scripts/reference_approval_service.py with a private-key path outside PROOFMESH_HOME; see docs/deployment.md"

package-agentteams:
	$(PYTHON) scripts/package_agentteams.py

validate-agentteams:
	$(PYTHON) scripts/validate_agentteams.py

validate-skills:
	$(PYTHON) scripts/validate_skills.py

sbom:
	$(PYTHON) scripts/generate_sbom.py

validate-external-evidence:
	$(PYTHON) scripts/validate_external_evidence.py

clean-runtime:
	$(PYTHON) scripts/clean_runtime.py

release:
	PYTHONPATH=src $(PYTHON) scripts/build_release.py
