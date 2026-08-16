# Public demo boundary

The public URL combines an **interactive browser decision lab** with a **frozen evidence replay**. It is more useful than a slide animation, while remaining safe to expose anonymously.

## Interactive decision lab

Visitors can choose a refund or production-change action, set its risk level and approval state, inject an upstream outcome, and mutate the approved tool, arguments, or context. Client-side logic then explains whether the Gateway would:

- wait for a signed Human approval;
- reject a mismatched contract before dispatch;
- complete a normal action;
- reconcile a committed action after a lost response;
- compensate or roll back a confirmed partial execution;
- stop in `UNKNOWN_MANUAL` when upstream state cannot be established.

The lab uses deterministic rules in `demo/app.js`. It does not execute the Python workflow or assert that a remote control plane ran.

## Frozen evidence replay

The second part reads `demo/data/scenarios.json`, replays three reference workflows, exposes selected frozen proofs and verification reports, and verifies each proof SHA-256 in the browser.

## Deliberate safety limits

The page cannot execute a refund, close a ticket, deploy software, accept arbitrary prompts, store customer data, invoke an LLM, or connect to Stripe, HubSpot, a customer network, or a production identity system.

The local FastAPI console does mutate an isolated reference database and requires separate role credentials. Hosting that API for anonymous users would need per-visitor isolation, quotas, request limits, TTL cleanup, abuse controls, TLS, and a safe external approval service. GitHub Pages cannot provide those controls or run Python.

The deployment workflow publishes only selected proofs, verification reports, benchmark summaries, and redacted AgentTeams runtime evidence. It excludes private keys, bearer tokens, databases, `.env` files, customer records, raw public narratives, and runtime workflow directories.
