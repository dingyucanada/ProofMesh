# Public demo boundary

The public URL is a **read-only evidence replay**, not a hosted ProofMesh control plane.

## What the page does

- reads a small, deterministic scenario summary from `demo/data/scenarios.json`;
- replays the automatic, externally approved, and compensated reference paths;
- exposes selected frozen proofs and external-verification reports;
- links each metric to its machine-readable artifact;
- makes the experimental boundaries visible next to the results.

## What the page cannot do

- execute a refund, close a ticket, or call any other write-capable tool;
- accept arbitrary prompts, credentials, workflow IDs, or approval assertions;
- invoke an LLM or prove model-driven collaboration;
- connect to Stripe, HubSpot, customer infrastructure, or production identity systems;
- demonstrate a customer pilot, production SLA, ROI, or real-user outcome.

## Why the live API is not exposed

The local FastAPI console mutates an isolated reference database and requires separate role credentials. Publishing it as a shared anonymous service would require per-visitor isolation, quotas, rate limits, payload limits, TTL cleanup, abuse controls, TLS, and a safe external approval path. GitHub Pages does not run Python services. A static replay therefore gives judges a stable, inspectable URL without pretending that a browser animation is a live backend.

## Evidence copied to Pages

The deployment workflow publishes only:

- the three reference workflow proofs and their external-verification reports;
- the AgentDojo authorization-contract replay report;
- the synthetic independent-process HTTP shadow report;
- the AgentTeams redacted runtime evidence.

No private key, bearer token, database, `.env`, customer record, raw public narrative, or runtime workflow directory is included.
