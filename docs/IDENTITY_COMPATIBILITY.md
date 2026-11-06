# Identity and external secrets

Pliqra supports standards-based OIDC/PKCE, signed SAML 2.0 and provider-scoped SCIM, plus local recovery accounts. Identity provider compatibility is configuration-specific: a vendor name alone does not prove every tenant policy works.

| Provider/surface | Delivered | Acceptance evidence |
|---|---|---|
| Keycloak 26.8.2 OIDC | Discovery, PKCE S256, nonce, signed ID token, safe viewer default | Real disposable local Keycloak over verified HTTPS/custom CA; callback replay rejected ([record](evidence/keycloak-26.8.2.json)) |
| Entra ID / Okta / generic OIDC | Console claim profiles and stable-subject configuration | Standards fixtures; actual customer tenant acceptance required |
| SCIM enterprise users | Core + enterprise schemas, discovery, POST/PATCH lifecycle, department/manager/cost center/etc. | Automated provisioning/mapping tests; vendor tenant provisioning acceptance required |
| SAML login and logout | Signed requests/assertions, SP- and IdP-initiated signed SLO, request matching/freshness/replay checks | Actual signed XML/Redirect fixtures; customer IdP logout acceptance required |
| Workload JWT | Exact issuer/audience/subject, bounded public JWKS, RSA/EC signatures, expiry/replay checks | Valid/invalid signed assertion, grant and revocation tests |
| Environment / file / Vault KV v2 | Versioned providers, explicit allowlists/prefixes, value-free access test | Rotation/path-boundary/HTTP fixtures; actual Vault deployment acceptance required |

**Workspace & identity → Directory profiles** applies documented claim mappings to a configured OIDC provider. Changing the stable subject requires explicit acknowledgment and revokes existing sessions. Match SCIM `externalId` to that same stable subject before enabling provisioning. Enterprise attributes never grant an administrator role. See [access](ACCESS.md).

## Workloads

Configure a trust in **Workspace & identity → Workload identity**: issuer, audience, public JWKS URI, exact allowed subjects, signing algorithms, project/team/gateway and allowed services. Exchange a fresh signed JWT:

```http
POST /auth/workload/token
Content-Type: application/json

{"provider_id":"workload_ID","assertion":"SIGNED_WORKLOAD_JWT"}
```

The returned access token uses normal inference APIs with the configured grants, rate limit and a maximum 15-minute lifetime. Each subject has separate ownership for owner-scoped quotas. Assertion replay is rejected; editing/disabling trust revokes its issued keys. The assertion's claims cannot expand grants. This is workload JWT federation, not a full SPIFFE agent or cloud-provider credential broker.

## Secret references

Configure **Secret sources**, then use `secret://provider_ID/alias` in provider API-key, OIDC client-secret, judge, analytics or guardrail secret fields. Environment/file sources require explicit alias mappings. Files stay below `PLIQRA_SECRET_ROOT` (default `/run/secrets`), including symlink resolution. Vault KV v2 uses a verified HTTPS endpoint, mount and allowed path prefixes; reference a field with `secret://provider_ID/path#field`. Vault credentials are encrypted. Secret tests return availability, never values. Rotation is resolved on the next operation. The encryption master key cannot be fetched through this mechanism.

Mount your internal CA and set `PLIQRA_CA_BUNDLE` for outbound model/identity/secret HTTP connections. Certificate verification remains enabled. This bundle is separate from PostgreSQL's connection-string TLS settings. Configure secure cookies and the exact HTTPS public URL for federation. Preserve at least one local administrator for recovery.

For SAML single logout configure the IdP SLO URL and register updated SP metadata. The normal sign-out control uses federation logout when available; signed inbound messages require matching issuer/destination, current timestamps and replay protection. Unsigned logout cannot revoke arbitrary sessions. Test IdP session-index and cross-application logout behavior in your environment.

Provider references: [Keycloak](https://www.keycloak.org/documentation), [Entra SCIM](https://learn.microsoft.com/en-us/entra/identity/app-provisioning/use-scim-to-provision-users-and-groups), [Okta SCIM](https://developer.okta.com/docs/guides/scim-with-entitlements/main/).
