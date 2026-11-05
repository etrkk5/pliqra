# Capability reference

[← Pliqra](../README.md)

The implemented product surface. For measured evidence and compatibility boundaries, see [release validation](RELEASE.md).

## Features and APIs

| Area | Supported behavior |
|---|---|
| Connections | Discovery, manual registration, custom authentication header/prefix, credential rotation, enable/disable, scheduled health checks and inventory drift alerts |
| AI services | Stable names, priority/fallback and round-robin routing, capability requirements, project access, input/output limits, exact encrypted cache, configuration preview, version history and rollback |
| Evidence | Declared capabilities separate from actual verification; explicit chat, streaming, function-call and JSON Schema probes; metadata drift resets verification |
| Named gateways | Versioned service catalogs, project/team restrictions, rolling request/token quotas, six quota scopes, concurrency, inference controls, clone/export/import, preview and policy-aware keys |
| Guardrails | Ordered input/output chains; local phrase/PII/schema checks; NeMo check-API and custom HTTP decision providers; enforce/observe; explicit error policy; per-request verdicts; guarded SSE delivery |
| Applications | OpenAI-compatible model list and chat completions, streaming, scoped/expiring/revocable keys, key rotation, RPM, project concurrency and budget admission |
| Access | Local accounts; workspace administrators; direct project memberships; teams with member/manager grants; OIDC with PKCE; SAML 2.0; provider-scoped SCIM user/group provisioning and deprovisioning |
| Reporting | Workspace totals and team/project/user/key/service/model breakdowns; custom time, status, latency, transport, connection and request-type filters; paginated requests; complete filtered CSV export |
| Request details | Routing decisions, each fallback attempt, HTTP status, timing, first-token latency, reported tokens, known/unknown cost and operator reconciliation |
| Content | Off by default. Per-service opt-in encrypted request/response capture, administrator-only inspection, audited reads and deletion, 64 KB limit per side |
| Evaluations | Saved task examples, expected text/JSON structure/latency checks, comparisons across services, explicit execution, optional saved outputs, cancellation and deletion |
| Operations | Runtime limits, retention, drift notifications, encrypted backup/restore, offline recovery commands, protected Prometheus metrics and audit history |
| Portability | Credential-free export/import with local deployment mapping, fingerprinted discovery/binding/invoke protocol, dependency-free Python client and independent reference server |

All supported configuration is available through the interface and `/api/v1/*`. `/openapi.json` describes the API; **Settings & API** displays operations offline and offers the Python client and protocol specification for download. The dashboard requires no external fonts, scripts, analytics, or CDN.

For automation, `GET /api/v1/getting-started?service_id=...` exposes the same scoped onboarding state. Administrators can use `POST /api/v1/application-connections` to create a service-restricted key and binding atomically. Failed capability negotiation leaves no orphan key. The generated key permits 60 requests/minute, expires after 90 days, and is returned only once; the binding lifetime is independently configurable.
