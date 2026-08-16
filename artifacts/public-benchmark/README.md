# AgentDojo benchmark artifact boundary

`cases.jsonl` is a deterministic export of the public AgentDojo v1 benchmark shipped with `agentdojo==0.1.35`. It is a benchmark fixture, not ProofMesh customer, partner, pilot, or production data.

The fixture contains fictional task values such as email addresses, verification codes, reset URLs, and tool arguments because those strings are part of the upstream benchmark scenarios. They must not be interpreted as live credentials or personal records. Provenance, version, license, upstream authorship, and the installed-license hash are recorded in [`data/benchmarks/THIRD_PARTY_AGENTDOJO.md`](../../data/benchmarks/THIRD_PARTY_AGENTDOJO.md).

The committed export supports deterministic offline replay. It does not contain model runs, does not measure prompt-injection ASR or task utility, and does not prove customer or production behavior. The exact scope and metrics are in [`authorization-contract-replay.md`](authorization-contract-replay.md).
