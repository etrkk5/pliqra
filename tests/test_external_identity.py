import asyncio
import json

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from pliqra.db import now,uid
from tests.test_app import workspace,connect,call
from tests.test_architecture import swap_upstream


def provider(client,service):
    config={'name':'Workload trust','issuer':'https://workload.test','audience':'pliqra','jwks_uri':'https://workload.test/jwks','subjects':['build-agent'],'allowed_ports':[service['id']]}
    response=client.post('/api/v1/workload-providers',json=config)
    assert response.status_code==201,response.text
    return response.json(),config


def assertion(key,**values):
    claims={'iss':'https://workload.test','aud':'pliqra','sub':'build-agent','iat':int(now()),'exp':int(now())+120,'jti':uid(),**values}
    return jwt.encode({'alg':'RS256','kid':'workload-test'},claims,key)


def test_workload_exchange_replay_scoping_and_immediate_revocation(workspace):
    client,app,_=workspace;service=connect(client)['services'][0]
    profile,config=provider(client,service)
    key=RSAKey.generate_key(2048,parameters={'kid':'workload-test'})
    def handler(request):
        if request.method=='GET':return httpx.Response(200,json={'keys':[key.as_dict(private=False)]})
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'Workload response'}}]})
    mock=swap_upstream(app,handler)
    encoded=assertion(key)
    exchange=client.post('/auth/workload/token',json={'provider_id':profile['id'],'assertion':encoded})
    assert exchange.status_code==200,exchange.text
    token=exchange.json()['access_token']
    assert 0<exchange.json()['expires_in']<=120
    assert client.post('/auth/workload/token',json={'provider_id':profile['id'],'assertion':encoded}).status_code==401
    response=client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+token},json={'model':service['name'],'messages':[{'role':'user','content':'Synthetic'}]})
    assert response.status_code==200,response.text
    assert token not in json.dumps(client.get('/api/v1/audit').json())
    assert client.put('/api/v1/workload-providers/'+profile['id'],json={**config,'enabled':False}).status_code==200
    assert client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+token},json={'model':service['name'],'messages':[{'role':'user','content':'Synthetic'}]}).status_code==401
    asyncio.run(mock.aclose())


@pytest.mark.parametrize('change',[{'aud':'other-app'},{'iss':'https://other-issuer.test'},{'sub':'untrusted-agent'},{'exp':1},{'iat':4102444800},{'exp':4102444800}])
def test_workload_claim_failures_never_issue_credentials(workspace,change):
    client,app,_=workspace;service=connect(client)['services'][0];profile,_=provider(client,service)
    key=RSAKey.generate_key(2048,parameters={'kid':'workload-test'})
    mock=swap_upstream(app,lambda _:httpx.Response(200,json={'keys':[key.as_dict(private=False)]}))
    response=client.post('/auth/workload/token',json={'provider_id':profile['id'],'assertion':assertion(key,**change)})
    assert response.status_code==401,response.text
    assert not app.state.store.all('SELECT id FROM api_keys')
    asyncio.run(mock.aclose())


def test_environment_secret_references_rotate_without_storing_values(workspace,monkeypatch):
    client,app,_=workspace
    profile=client.post('/api/v1/secret-providers',json={'name':'Runtime keys','kind':'env','keys':{'model':'TEST_MODEL_CREDENTIAL'}})
    assert profile.status_code==201,profile.text
    reference='secret://'+profile.json()['id']+'/model'
    service=connect(client,model='manual',api_key=reference)['services'][0]
    sent=[]
    def handler(request):
        sent.append(request.headers['Authorization'])
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'Synthetic'}}]})
    mock=swap_upstream(app,handler)
    for value in ('synthetic-first-key','synthetic-rotated-key'):
        monkeypatch.setenv('TEST_MODEL_CREDENTIAL',value)
        assert call(client,service['name']).status_code==200
    assert sent==['Bearer synthetic-first-key','Bearer synthetic-rotated-key']
    assert app.state.vault.decrypt(app.state.store.one('SELECT secret FROM connections')['secret'])==reference
    denied=client.post('/api/v1/secret-providers',json={'name':'Invalid','kind':'env','keys':{'master':'PLIQRA_ENCRYPTION_KEY'}})
    assert denied.status_code==422
    asyncio.run(mock.aclose())


def test_vault_reference_prefix_and_token_boundary(workspace):
    client,app,_=workspace
    profile=client.post('/api/v1/secret-providers',json={'name':'Vault','kind':'vault','endpoint':'https://vault.test','allowed_prefixes':['apps/allowed'],'token':'synthetic-vault-token'})
    assert profile.status_code==201,profile.text
    sent=[]
    def handler(request):
        sent.append(request)
        assert request.headers['X-Vault-Token']=='synthetic-vault-token'
        return httpx.Response(200,json={'data':{'data':{'api_key':'synthetic-model-key'}}})
    mock=swap_upstream(app,handler);app.state.gateway.secrets.client=mock
    prefix='secret://'+profile.json()['id']
    good=client.post('/api/v1/secret-providers/test',json={'reference':prefix+'/apps/allowed/model#api_key'})
    assert good.status_code==200 and good.json()=={'resolved':True,'value_included':False}
    denied=client.post('/api/v1/secret-providers/test',json={'reference':prefix+'/apps/other/model#api_key'})
    assert denied.status_code==503 and len(sent)==1
    assert 'synthetic-vault-token' not in client.get('/api/v1/secret-providers').text
    asyncio.run(mock.aclose())
