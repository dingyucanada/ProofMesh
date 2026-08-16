# Public action decision lab

This directory powers the GitHub Pages demo. It has two deliberately separate layers:

1. a browser-side decision simulator for exploring policy, approval, contract mismatch, reconciliation, compensation, and `UNKNOWN_MANUAL` outcomes;
2. a replay of three frozen reference workflows with SHA-256 verification and links to their machine-readable proofs.

The simulator is interactive but deterministic. It implements the documented decision rules in client-side JavaScript and never claims to be a hosted ProofMesh backend. The frozen proofs were produced by the local reference runtime.

The page does not accept free text, credentials, workflow IDs, approval assertions, or customer records. It does not call a model, payment provider, CRM, deployment platform, or FastAPI service.

Local preview:

```bash
python3 scripts/build_public_demo.py --output /tmp/proofmesh-pages
python3 -m http.server 4173 --directory /tmp/proofmesh-pages
```

The GitHub Pages workflow is `.github/workflows/pages.yml`.
