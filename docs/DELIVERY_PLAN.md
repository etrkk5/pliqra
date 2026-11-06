# Roadmap delivery

The roadmap is authorized for implementation. Default installation remains one image with SQLite; enterprise components are optional.

| Track | Delivered in 2.0 | Acceptance evidence |
|---|---|---|
| Storage and scale | PostgreSQL backend, replica leases, shared admission, isolated organizations, analytics export | Real PostgreSQL tests: concurrent shared quota/concurrency, restore, reconnect, analytics, workflows and organization isolation; runnable Caddy topology |
| Model interfaces | Embeddings, speech, transcription, provider-native routes, complete Ollama tool message adaptation | Interface guardrail/accounting and adapter tests; Model lab browser run; [documented subsets](MODEL_INTERFACES.md) |
| Delivery workflows | Immutable prompts, durable batches, judges, quality gates, canary publication | Prompt/batch/evaluation/release tests and console; [workflow guide](WORKFLOWS.md) |
| Identity and secrets | Workload JWT identities, external secret references, SAML logout, provisioning profiles | Signed workload/SAML and provisioning tests, rotation fixtures, real HTTPS Keycloak acceptance; [matrix](IDENTITY_COMPATIBILITY.md) |
| Protocol and clients | Extension negotiation, JavaScript and Go clients, executable conformance suite | Executable core profile over real HTTP against both servers; JavaScript/Go tests in CI; [guide](CONFORMANCE.md) |
| Release | Console controls, operating guides, concise README, CI | [Release record](RELEASE.md), console screenshots, default and optional Docker installations |

Real customer IdPs/models, third-party conformance attestations and ecosystem adoption require external participants. Implementations and executable compatibility/conformance tools can be delivered locally; external acceptance will be recorded separately instead of claimed.
