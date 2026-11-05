# Pliqra 1.0 — validation record

Validated locally on 5 November 2026. This record describes observed behavior, not a security certification or a claim of every-provider compatibility.

## Automated checks

- 30 tests pass on host Python 3.14 and in the Linux/Python 3.13 container.
- Coverage includes OpenAI-compatible/Ollama chat, fragmented Unicode streaming, incomplete streams, cancellation/accounting paths, pre-stream fallback, service fingerprints, revoked/expired/scoped keys, RPM/budget limits, project/team authorization and admin rollups, encrypted optional content/cache/backup, restore, interrupted-restore crash recovery and session invalidation, SCIM lifecycle, signed/tampered/replayed SAML responses, asynchronous evaluations and positive function/structured-output/streaming capability probes.
- Dependency deprecation warnings remain in Authlib/httpx and the SAML toolkit; no failing assertions are suppressed.
- The Docker runtime runs as UID/GID 10001, persists a named volume and reports healthy. Its SAML XML signature runtime imports successfully (xmlsec 1.3.18 / libxml2 2.14.6 on the tested Linux ARM64 build).

Native HTTPS was exercised in a separate temporary container using a test certificate explicitly trusted by the client, with hostname verification enabled. The container health check also supports native TLS.

## Browser checks

A separate workspace at port 8081, with a clearly synthetic upstream, was used for all sample traffic. The user workspace at port 8088 was not seeded with demo accounts or models.

Verified: administrator sign-in; overview; usage/team/key breakdowns; team-to-request drill-down; individual attempt/timing/usage details; audited captured-content inspection; service policy dialog; application capability-binding wizard and generated invocation; evaluation creation/execution/results; operations; identity configuration entry points; settings/client/specification links; project/team-attributed streaming playground. The sidebar also operated in a narrow viewport. The charcoal/orange theme is preserved.

## Independent interoperability

A Python standard-library client, importing no gateway code, passed discovery, binding, completion, streaming and unsupported-capability rejection against:

1. The Docker gateway connected to a synthetic OpenAI-compatible upstream.
2. `examples/reference_server.py`, a second HTTP implementation with no gateway imports, connected directly to that upstream.

The client's discovery now follows `/.well-known/pliqra`. Contract invalidation and identity checks are additionally exercised in automated tests. Third-party adoption or an established protocol standard is not claimed.

## Local load measurement

A controlled run used one Docker gateway, a durable named SQLite volume, eight concurrent callers, exact caching disabled and encrypted request-content capture enabled. A separate synthetic upstream reported fixed usage and returned short completions. For 160 requests:

- All 160 completed with HTTP 200.
- 1.069 seconds total; approximately 149.6 requests/second.
- Client-observed median 46.09 ms; p95 89.21 ms.

These are local end-to-end synthetic measurements, including Docker networking and the test server. They do not measure added gateway overhead in isolation, GPU performance, long-context capacity, production SLOs or maximum throughput. Machine resources and workload shape affect the result.

## Supported release boundary

One workspace, one process and one persistent volume. Generic OIDC/SAML/SCIM are implemented and tested with controlled standards fixtures; the intended real identity provider and real LLM endpoint still require their own configuration and acceptance test. Inference is the documented chat-completions subset. PostgreSQL/HA, distributed quotas, provider-native APIs, embeddings/audio, full native Ollama tool adaptation and a general guardrail engine are outside release 1.0. The connection protocol remains draft 0.1.

## Pliqra rename verification

The Pliqra package, canonical configuration, API header namespace, SDK and protocol discovery path pass the same inference and identity checks. Four additional upgrade tests cover the old database filename with encrypted credentials, environment fallback precedence, encrypted archive restore and reserved attribution-field isolation. Screenshot assets in `docs/screenshots` were captured from the running Pliqra console using a separate synthetic demo workspace. No private user workspace content is published.
