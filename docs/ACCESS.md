# Identity, access and administrator visibility

[← Pliqra](../README.md)

Configure company identity, grant scoped access and inspect retained usage.

## Administrator visibility

Administrators can inspect and manage all workspace resources and all retained request history, including cumulative totals and each team's traffic. Filters apply consistently to the dashboard, request list, rollups and CSV. **All retained history** removes the rolling reporting window; runtime retention determines how much history is stored.

Project managers see requests belonging to their managed projects and their own calls. Project members see their own calls and the services granted to their projects. Workspace administration, identity configuration, provider credentials, keys, backups, raw captured content and billing corrections remain administrator functions. Incoming SSO claims never grant workspace administrator status automatically.

Application keys have a fixed project, optional team, and optional service allow-list. Requests cannot override key billing attribution. User/playground requests can choose an accessible project and team; project-scoped services enforce their project automatically. User/key-owner breakdowns identify the key creator, not an inferred downstream human.

## Identity setup

**OIDC:** connect the issuer URL, client ID and secret in **Identity & access**, and register the callback shown by the interface. ID tokens are validated; accounts are keyed by connector and stable subject, not merged by email. Use the provider editor for the stable-subject and groups claim names. New federated accounts are viewers.

**SAML:** configure the IdP entity ID, SSO URL and signing certificate. Register Pliqra's generated SP metadata with the IdP. The SP signs login requests and requires signed assertions, valid audience/destination/time, a matching request, a browser-bound relay cookie and replay protection. Use an **HTTPS public URL** and secure cookies for SAML's cross-site response. Unsolicited IdP-initiated login is unsupported. Signed SP/IdP single logout is available when an SLO URL is configured; register the updated SP metadata. See the [identity matrix](IDENTITY_COMPATIBILITY.md).

**SCIM:** enable a provisioning token on the intended connector and register its displayed `/scim/v2/{provider_id}` URL. Match SCIM `externalId` to the configured stable SSO subject. Enabling SCIM requires provisioning before sign-in and revokes existing sessions. Tokens are provider-scoped. Users, groups, equality filtering, pagination, replace/update, group membership and disabling users are supported. Bulk operations, sorting, ETags and vendor-specific extensions are not. Map trusted SSO/SCIM groups to project member/manager grants explicitly.

Implementations follow [SCIM's protocol](https://www.rfc-editor.org/rfc/rfc7644.html) and use the [SAML Python toolkit](https://github.com/SAML-Toolkits/python3-saml) for signature and assertion validation. OIDC and SAML are verified with controlled signed fixtures; compatibility with an organization's actual IdP must be checked using its configuration. There is no claim that every vendor/extension has been tested.

## Organizations and machine identities

An administrator sees all cumulative and team-filtered details within their workspace, with audited access to explicitly captured content. Root administrators manage the organization directory, while each organization has separate accounts and history; access is not implicitly inherited across organizations. See [organizations](PLATFORM.md#organizations), [workload identity and secret references](IDENTITY_COMPATIBILITY.md).
