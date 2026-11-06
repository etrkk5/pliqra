# Pliqra 2.1.0 — active/active multi-DC validation

Validated 6 November 2026. The [2.0 validation record](RELEASE_2_0.md) and [1.2 record](RELEASE_1_2.md) remain available as history. The deployment contract, including infrastructure responsibilities, is in [MULTI_DC.md](MULTI_DC.md).

## Delivered behavior

- Active gateway sites share one writable PostgreSQL authority for request/token quotas, concurrency, policies, identities and usage. Database time governs admission windows and node leases.
- Typed service placement supports local preference, local-only routing and allowed model sites/regions. Constraints apply to fallback, canary and cached model selection, participate in fingerprints and can be negotiated as `pliqra.placement/1`.
- Health observations and circuit breakers are isolated by observing site. A failed path in one DC does not poison another DC's reachability.
- Versioned, audited active/draining/standby controls are available in the console and API. Admitted calls finish during drain; new calls and selection of models at that site stop.
- Separate liveness/readiness probes and opt-in supervised restart let fenced gateways rejoin with a fresh process/node identity. Started inference is never automatically replayed.
- Ingress/model-site filters, CSV fields, detailed events and per-attempt model locations make routing visible to administrators. TypeScript and Go binding types expose placement metadata.

The default stays one platform-independent Docker image with SQLite. Optional [multi-DC Compose](../compose.multidc.yaml) runs separately in each DC against your existing verified-TLS PostgreSQL HA service. It does not provision independent writers or a global traffic manager.

## Automated evidence

**159 Python tests pass** on host Python 3.14 with real PostgreSQL, including **23 PostgreSQL integration cases** (16 multi-DC and seven existing storage/scale cases). Without the test database URL, those integration cases skip explicitly. The Linux/Python 3.13 Docker suite exercises the same tests. JavaScript and Go client checks remain part of CI.

The new cases cover concurrent global request admission; pending token reservations in gateway/key/user/project/team/service scopes; global concurrency; local model selection; drain while an admitted call runs; per-site health/circuits; strict placement and canary boundaries; database-clock admission; multi-host writable selection; manual endpoint recovery without discovery; and isolated storage-connection loss. Unit cases additionally reject SQLite/unverified remote databases in multi-DC mode and check placement/traffic-mode contract behavior.

These tests caught and corrected PostgreSQL token-quota edge cases: numeric aggregate values must be normalized before JSON error serialization, and nullable quota scopes require portable null-safe equality. Candidate and contract-inventory reads were batched to avoid per-model WAN round trips.

No assertion is suppressed. Existing Authlib/httpx and SAML deprecation warnings remain.

## Real container acceptance

Two actual gateway containers and two synthetic HTTP model endpoints used a real PostgreSQL 18 instance on one local host. Shared sessions, local selection from both sites, draining one site while the other served, and termination of one gateway's database connection passed. The fenced gateway terminated gracefully and its configured container policy restarted it with a fresh node identity; authenticated access and inference then resumed. [Machine-readable record](evidence/multidc-container.json).

The console was checked against this separate synthetic workspace: both active data centers and live replicas, traffic-mode controls, model-location selection and model-placement forms. Ingress/model-site request filters and details use the same API. The English interface and charcoal/orange styling remain. No existing user workspace was started or seeded.

## Acceptance boundaries

- The container exercise simulates two sites on one host. Geographically separate DCs, WAN failure/latency, real model capacity and PostgreSQL primary promotion require deployment acceptance.
- Exact quota continuity depends on **one writer preserving acknowledged state**. The database platform owns replication, promotion, former-primary fencing and durability. Asynchronous replication may lose recent quota/policy records during promotion. Multi-host connection selection is not leader election.
- Disconnected gateways stop admitting calls. There are no offline quota allowances, independent writable DC copies or inference replay. Unknown usage remains reserved until reconciled or its documented window expires.
- No production RTO/RPO, capacity or availability SLO is claimed. Shared database latency and serialized workspace writes affect throughput.
- Model location labels are administrator declarations. Routing constraints do not certify physical/legal residency of databases, guards, judges, analytics, identities or backups.
- Previously documented native API subsets, buffered native streaming, batch/archive limits and real-vendor acceptance boundaries remain. The prior Keycloak evidence is from the 2.0 validation; it was not presented as a new 2.1 vendor certification.

2.1 uses schema **5**, inference contract revision **4** and wire protocol draft **0.1**. Stop old replicas, back up, upgrade every site and renew application bindings. See [the upgrade procedure](MULTI_DC.md#evidence-and-upgrade).
