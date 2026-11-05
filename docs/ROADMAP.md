# Product scope after 1.2

The single-container product includes project/team access, OIDC/SAML/SCIM, operational recovery, per-attempt accounting, capability probes, evaluations, portable service import/export, an independent client and an independent reference implementation. These are implemented, not placeholder roadmap items. Version 1.1 adds named gateways, composable rolling quotas, policy-bound keys and ordered local/NeMo-check/custom-HTTP guardrails with UI/API controls.

The core direction remains endpoint-first, platform-independent, offline-capable, with UI/API parity and a small independently implementable protocol. Declared capabilities, verified behavior, reported usage, estimates and unknowns remain distinct.

Future additions require their own authorization, API/UI controls, observable execution and conformance evidence:

- Real-vendor identity compatibility matrix, vendor provisioning extensions, workload identity, optional secret-manager integration and SAML single logout.
- PostgreSQL, optional analytics storage, multiple replicas, distributed admission limits and separate organizations.
- Model quality regression gates, controlled canary publication, evaluations with external judges and protocol extension negotiation.
- Embeddings, audio, provider-native APIs, complete Ollama tool-loop adaptation, prompt versioning and batch workloads.
- Additional language SDKs, formal third-party protocol conformance and ecosystem adoption.

None of these future capabilities or an industry-standard status is implied by release 1.2.

Version 1.2 hardens the existing scope: typed inference boundaries, atomic admission/settlement, guarded response envelopes, bounded provider IO and isolated schema validation. See [the technical review](TECHNICAL_REVIEW.md) for findings and regression evidence.
