# Product experience delivery — 2.1 RC3

This delivery uses isolated synthetic customer environments. It does not claim a real customer reference, third-party certification, production capacity or geographic disaster-recovery acceptance.

## Acceptance tracks

- [x] Native live streaming in both directions: Responses and Anthropic text/tool events, bounded parsing, terminal/error/cancellation behavior, accounting and output-check buffering.
- [x] Installable Python and JavaScript/TypeScript SDK artifacts with clean-install tests, release assets, version checks and registry publication instructions/workflow.
- [x] Independent-instance fleet management: explicit scoped pairing, encrypted controller credentials, metadata-only health/usage, portable gateway-policy mapping, immutable proposals, separate approval, optimistic concurrency, idempotent application and drift detection, console/API controls.
- [x] Synthetic customer acceptance using independent applications and installed SDKs; usability/onboarding observations labelled as scripted, not human feedback.
- [x] Repeatable load/soak and interruption/recovery exercises with machine-readable results, measured environment/limits and no unsupported SLO claims.
- [x] Current English guides, short README, license-bearing multi-platform candidate image, commit/push, all release checks, local acceptance and Docker shutdown.

Published: [gateway RC3](https://github.com/eturkok/pliqra/releases/tag/v2.1.0-rc.3), [Python SDK](https://pypi.org/project/pliqra-client/0.1.0rc1/) and [npm SDK](https://www.npmjs.com/package/@eturkok/pliqra/v/0.1.0-rc.1). Both registry downloads match the tested release assets; canonical clean installs and synthetic gateway calls passed. PyPI OIDC publication and npm trusted-publisher validation are recorded in [acceptance evidence](ACCEPTANCE.md#published-distribution-acceptance). The intentional npm duplicate-version check was refused rather than overwriting published code. Local disposable containers/volumes and transient CLI credentials were removed; Docker Desktop was stopped. GitHub package artifacts remain directly installable without registry credentials. No credentials or model traffic are seeded into an existing user workspace.
