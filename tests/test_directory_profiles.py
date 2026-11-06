from tests.test_app import workspace
from pliqra.identity import ENTERPRISE_SCHEMA


def test_enterprise_scim_attributes_and_patch_do_not_grant_roles(workspace):
    client,app,_=workspace
    provider=client.post('/api/v1/identity-providers',json={'name':'Directory','issuer':'https://identity.test','client_id':'client','client_secret':'synthetic-secret'}).json()['id']
    token=client.post('/api/v1/identity-providers/'+provider+'/scim-token',json={}).json()['token']
    headers={'Authorization':'Bearer '+token};root='/scim/v2/'+provider
    schemas=client.get(root+'/Schemas',headers=headers).json()
    assert any(row['id']==ENTERPRISE_SCHEMA for row in schemas['Resources'])
    response=client.post(root+'/Users',headers=headers,json={'userName':'directory-user','externalId':'stable-user-id','active':True,ENTERPRISE_SCHEMA:{'department':'Engineering','employeeNumber':'E123','manager':{'value':'manager-id'}}})
    assert response.status_code==201,response.text
    user=response.json()
    assert user[ENTERPRISE_SCHEMA]['department']=='Engineering'
    path=ENTERPRISE_SCHEMA+':department'
    changed=client.patch(root+'/Users/'+user['id'],headers=headers,json={'Operations':[{'op':'replace','path':path,'value':'Platform'},{'op':'replace','path':ENTERPRISE_SCHEMA+':manager.displayName','value':'Example Manager'}]})
    assert changed.status_code==200,changed.text
    assert changed.json()[ENTERPRISE_SCHEMA]['department']=='Platform'
    assert app.state.store.one('SELECT role FROM users WHERE id=?',(user['id'],))['role']=='viewer'
    assert client.patch(root+'/Users/'+user['id'],headers=headers,json={'Operations':[{'op':'replace','path':ENTERPRISE_SCHEMA+':role','value':'admin'}]}).status_code==400


def test_vendor_profile_requires_acknowledgement_for_subject_mapping_changes(workspace):
    client,app,_=workspace
    profile=client.get('/api/v1/identity-profiles').json()
    assert {p['id'] for p in profile}=={'entra','okta','keycloak','generic'}
    provider=client.post('/api/v1/identity-providers',json={'name':'Entra','issuer':'https://identity.test','client_id':'client','client_secret':'synthetic-secret'}).json()['id']
    assert client.post('/api/v1/identity-profiles/entra/apply',json={'provider_id':provider}).status_code==409
    applied=client.post('/api/v1/identity-profiles/entra/apply',json={'provider_id':provider,'acknowledge_subject_change':True})
    assert applied.status_code==200 and applied.json()['subject_claim']=='oid'
