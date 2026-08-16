# Public evidence replay

This directory is the source for the GitHub Pages demo. It is intentionally static and read-only.

The page replays three frozen reference workflows and links to the corresponding proof bundles, external verification reports, AgentDojo authorization replay, and independent-process HTTP fault experiment. The deployment workflow copies only those selected evidence files into the Pages artifact.

It does **not** run the FastAPI service, call a model, use payment or CRM accounts, accept customer data, or create side effects. For the real local reference workflow, follow the repository root README.

Local preview:

```bash
python3 scripts/build_public_demo.py --output /tmp/proofmesh-pages
python3 -m http.server 4173 --directory /tmp/proofmesh-pages
```

The GitHub Pages deployment uses `.github/workflows/pages.yml`.
