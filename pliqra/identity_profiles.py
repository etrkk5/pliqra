"""Configuration profiles and explicit acceptance status, not vendor certification."""
import json
from fastapi import Depends,HTTPException
from pydantic import BaseModel
from .db import encode

PROFILES={
    'entra':{'name':'Microsoft Entra ID','subject_claim':'oid','groups_claim':'groups','notes':'Map SCIM externalId to the same tenant object identifier. Configure group claims explicitly; group-overage Graph fetching is not automatic.','documentation':'https://learn.microsoft.com/en-us/entra/identity/app-provisioning/use-scim-to-provision-users-and-groups'},
    'okta':{'name':'Okta','subject_claim':'sub','groups_claim':'groups','notes':'Configure the application subject and SCIM externalId to agree. Add an application groups claim.','documentation':'https://developer.okta.com/docs/guides/scim-provisioning-integration-connect/main/'},
    'keycloak':{'name':'Keycloak','subject_claim':'sub','groups_claim':'groups','notes':'Add a groups mapper when using group grants. Use the same stable user ID for SCIM externalId.','documentation':'https://www.keycloak.org/docs/latest/server_admin/index.html'},
    'generic':{'name':'Generic OIDC / SAML / SCIM','subject_claim':'sub','groups_claim':'groups','notes':'Select stable subject and group claims for your own provider. Verify your actual issuer, claims and provisioning mappings.','documentation':'https://openid.net/specs/openid-connect-core-1_0.html'},
}


class Apply(BaseModel):
    provider_id:str
    acknowledge_subject_change:bool=False


def mount(app):
    store=app.state.store;admin=app.state.context['admin']

    @app.get('/api/v1/identity-profiles',tags=['Identity'])
    def profiles(p=Depends(admin)):
        return [{'id':key,**value,'customer_tenant_acceptance':'required','enterprise_scim':True} for key,value in PROFILES.items()]

    @app.post('/api/v1/identity-profiles/{profile_id}/apply',tags=['Identity'])
    def apply(profile_id:str,body:Apply,p=Depends(admin)):
        profile=PROFILES.get(profile_id)
        row=store.one("SELECT * FROM identity_providers WHERE id=? AND protocol='oidc'",(body.provider_id,))
        if not profile or not row:raise HTTPException(404,'OIDC provider or configuration profile not found')
        config=json.loads(row['config'])
        if config.get('subject_claim','sub')!=profile['subject_claim'] and not body.acknowledge_subject_change:
            raise HTTPException(409,'Changing the stable subject requires acknowledgement and matching SCIM externalId configuration')
        config.update(subject_claim=profile['subject_claim'],groups_claim=profile['groups_claim'],compatibility_profile=profile_id)
        with store.transaction():
            store.update('identity_providers',row['id'],{'config':encode(config)})
            store.run('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE issuer=?)',(row['id'],))
            store.audit(p['id'],'identity.profile_applied',row['id'],{'profile':profile_id,'subject_claim':profile['subject_claim']})
        return {'applied':True,'subject_claim':profile['subject_claim'],'groups_claim':profile['groups_claim'],'sessions_invalidated':True,'tenant_acceptance_required':True}
