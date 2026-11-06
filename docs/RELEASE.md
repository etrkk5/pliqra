# Pliqra 2.0.0 — validation and boundaries

Validated 6 November 2026. This record separates implemented behavior, real local interoperability and synthetic provider fixtures. The [1.2 record](RELEASE_1_2.md) is preserved as history.

## Delivered scope

The [roadmap delivery table](ROADMAP.md) now links every engineering track to code, console/API controls, tests and an operating guide. Default installation remains one Docker image with SQLite. The optional topology adds PostgreSQL, shared-state replicas, distributed admission, independent organizations and metadata analytics. Model operations, stateless native interfaces, prompts, batches, external judges, gated canaries, identity/secrets and language clients are included.

## Automated validation

- **140 Python tests pass** on host Python 3.14 and Linux/Python 3.13 in the final Docker image, each with a real PostgreSQL 18 instance. The suite includes seven real database/replica cases; without a database URL those seven integration cases explicitly skip.
- JavaScript client: **8 checks pass**, including fragmented Unicode SSE, incomplete streams, contract changes, extension negotiation, local-path enforcement and operation transport.
- Go client: **2 tests pass**, including discovery/binding/streaming and contract/error boundaries.
- The independent **pliqra-core/1** executable profile passes **9 checks against each** of Pliqra and the separate standard-library reference server over real local HTTP. Provider outputs are synthetic. Reports: [gateway](evidence/gateway-core.json), [reference](evidence/reference-core.json).
- JavaScript/Node 22 and Go 1.25 also passed discovery, binding, completion and terminal streaming against the running synthetic gateway over real HTTP ([record](evidence/language-clients.json)).
- CI runs Python 3.13/3.14, real PostgreSQL integration, JavaScript/Go and a fresh default Docker workspace. Python source compilation and console JavaScript parsing pass.
- Dependency deprecation warnings remain in Authlib/httpx and SAML tooling. No failing assertion is suppressed.

Regression coverage includes shared request/concurrency admission across replicas; fencing/reconnect; encrypted atomic restore with rollback; metadata upsert after reconciliation; organization/account/session isolation; versioned prompts and durable restart; batch authorization/encryption; changed/failed quality evidence; canary allocation/promotion/rollback; native routes sharing guardrails/quotas; vector/audio boundaries; signed workload identity and replay; secret rotation/prefix restrictions; signed SAML logout and SCIM enterprise provisioning.

## Real local integrations

**Keycloak 26.8.2** ran in a disposable realm over HTTPS using an explicitly trusted local CA, with certificate and hostname verification enabled. Discovery, PKCE S256, nonce, signed ID-token login, safe viewer role and callback replay rejection passed. [Machine-readable record](evidence/keycloak-26.8.2.json). Custom CA support was added without disabling certificate verification.

**Caddy + two gateway containers + PostgreSQL 18** passed fresh setup, shared authenticated sessions over 20 requests, both-replica status, synthetic upstream invocation and populated onboarding/API views. After one gateway was stopped and active health checks converged, 20 subsequent metadata requests succeeded through the survivor. Proxy inference retries are disabled. This does not establish in-flight request recovery or database failover. [Record](evidence/two-replica-topology.json).

The default non-root Docker image starts from an empty workspace and exposes health/discovery/initial setup and all client downloads ([record](evidence/fresh-docker.json)). Linux/Python 3.13 runs the same test suite with real PostgreSQL. Separate test storage and ports were used; the existing user workspace was never started or seeded.

## Console acceptance

The charcoal/orange console was checked in a separate synthetic workspace: Model lab embedding execution; result/download/request inspection; prompt selection/render/run; batch creation and completed item inspection; quality release/judge views; workload trust form; organization/secret/analytics/directory tabs; SDK links and shared runtime controls. Existing admin totals/team drill-downs, gateway policies and guardrail flows remain covered by the earlier record and regression tests. [Screenshots and tour](CONSOLE.md).

## Practical limits

- SQLite is one process per volume. PostgreSQL serializes workspace writes with one schema lock; production capacity and database HA require operator testing. Replica fencing relies on synchronized clocks and a 60-second lease timeout.
- Organizations have separate application accounts/storage/key contexts but share the host/runtime trust domain. Root administrators manage the directory; their accounts do not inherit organization data access. Up to 50 organizations per root.
- Native Responses/Anthropic/Ollama interfaces are documented **subsets**. Native SSE is buffered and labeled; no server-managed conversations or arbitrary provider tools. Audio is bounded at 32 MB; response-header validation is not acoustic quality validation.
- Portable logical archives are bounded at 32 MB plaintext; online physical SQLite backup is bounded at 32 MB database size. Larger workspaces require native backups. Each organization is a separate recovery unit. PostgreSQL restore requires the matching master key/schema version.
- Batches support 500 items and 100 queued jobs. Unstarted work can resume; started inference is never automatically replayed. Saved outputs require explicit deletion. Provider cancellation is best effort.
- Canary gates check recorded task evidence at publication/advance. They do not autonomously certify production quality. Token/cost estimates are not provider invoices; unknown usage stays visible/reserved. Analytics retention is independent from source retention.
- Entra/Okta customer tenants, actual Vault/NeMo deployments and intended real models still require environment-specific acceptance. Vendor profile/adapter fixtures do not constitute certification. No third-party protocol attestation, ecosystem adoption or industry-standard status is claimed.

See [deployment](PLATFORM.md), [interfaces](MODEL_INTERFACES.md), [identity matrix](IDENTITY_COMPATIBILITY.md) and [workflows](WORKFLOWS.md) before production rollout.
