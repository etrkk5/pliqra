import base64
import json
import time
from datetime import datetime,timedelta,timezone
from urllib.parse import parse_qs,urlsplit
import zlib

import pytest
from fastapi.testclient import TestClient
from tests.test_app import workspace,connect,call,key,price


def local_user(client,name):
    response=client.post('/api/v1/users',json={'name':name,'email':name.lower()+'@example.test','password':'long-test-password','role':'viewer'})
    assert response.status_code==201,response.text
    return response.json()['id']


def sign_in(client,email,password='long-test-password'):
    response=client.post('/api/v1/login',json={'email':email,'password':password})
    assert response.status_code==200,response.text
    client.headers['X-Pliqra-CSRF']=response.json()['csrf']
    return response.json()


def test_team_project_scopes_and_administrator_rollups(workspace):
    client,app,_=workspace
    a=connect(client);b=connect(client)
    user=local_user(client,'Alice')
    manager=local_user(client,'Manager')
    project=client.post('/api/v1/projects',json={'name':'Finance'}).json()['id']
    other=client.post('/api/v1/projects',json={'name':'Engineering'}).json()['id']
    team=client.post('/api/v1/teams',json={'name':'Finance team','members':[user,manager],'projects':[{'project_id':project,'role':'member'}]}).json()['id']
    client.post(f'/api/v1/projects/{project}/members',json={'user_id':manager,'role':'manager'})
    ports=client.get('/api/v1/ports').json()
    for item,scope in [(next(p for p in ports if p['id']==a['services'][0]['id']),project),(next(p for p in ports if p['id']==b['services'][0]['id']),other)]:
        item['project_id']=scope
        assert client.patch('/api/v1/ports/'+item['id'],json=item).status_code==200
    token=key(client,project_id=project,team_id=team)['token']
    headers={'Authorization':'Bearer '+token}
    assert client.post('/v1/chat/completions',headers=headers,json={'model':a['services'][0]['name'],'messages':[{'role':'user','content':'Team request'}]}).status_code==200
    assert client.post('/v1/chat/completions',headers=headers,json={'model':b['services'][0]['name'],'messages':[{'role':'user','content':'Blocked request'}]}).status_code==403
    usage=client.get('/api/v1/usage',params={'team_id':team}).json()
    assert usage['summary']['requests']==2  # Includes the explicit authorization rejection.
    assert usage['teams'][0]['name']=='Finance team'
    assert sum(r['requests'] for r in usage['teams'])==usage['summary']['requests']
    event=client.get('/api/v1/events',params={'team_id':team,'status':'success'}).json()['items'][0]
    assert event['team_name']=='Finance team'
    assert event['attempt_count']==1
    client.post('/api/v1/logout',json={})
    sign_in(client,'alice@example.test')
    visible=client.get('/v1/models').json()['data']
    assert [r['id'] for r in visible]==[a['services'][0]['name']]
    assert client.get('/api/v1/events/'+event['id']).status_code==404
    assert client.get('/api/v1/usage').json()['summary']['requests']==0
    assert call(client,b['services'][0]['name']).status_code==403
    client.post('/api/v1/logout',json={})
    sign_in(client,'manager@example.test')
    assert client.get('/api/v1/events/'+event['id']).status_code==200
    assert client.get('/api/v1/events').json()['total']==2


def test_connection_edit_rotation_cache_and_portability(workspace):
    client,app,captured=workspace
    result=connect(client)
    connection=client.get('/api/v1/connections').json()[0]
    changed=client.patch('/api/v1/connections/'+connection['id'],json={**connection,'api_key':'new-secret','auth_header':'x-api-key','auth_prefix':'','provider':'openai','health_interval':0})
    assert changed.status_code==200,changed.text
    port=client.get('/api/v1/ports').json()[0]
    port['cache_ttl']=300
    assert client.patch('/api/v1/ports/'+port['id'],json=port).status_code==200
    first=call(client,port['name'])
    assert first.status_code==200
    assert captured[-1].headers['x-api-key']=='new-secret'
    before=len(captured)
    second=call(client,port['name'])
    assert second.status_code==200
    assert second.headers['X-Pliqra-Cache']=='HIT'
    assert len(captured)==before
    cached=client.get('/api/v1/events/'+second.headers['X-Request-ID']).json()
    assert cached['cache_hit']==1 and cached['cost_micros']==0
    assert 'Hello!' not in app.state.store.one('SELECT payload FROM cache_entries')['payload']
    exported=client.get('/api/v1/ports/'+port['id']+'/export').json()
    assert 'new-secret' not in json.dumps(exported)
    assert connection['endpoint'] not in json.dumps(exported)
    imported=client.post('/api/v1/ports/import',json={'document':exported,'mapping':{'deployment-0':port['deployments'][0]},'name':'portable-copy'})
    assert imported.status_code==201,imported.text
    assert call(client,'portable-copy').status_code==200
    versions=client.get('/api/v1/ports/'+port['id']+'/versions').json()
    assert len(versions)>=2
    assert client.post(f"/api/v1/ports/{port['id']}/rollback/1",json={}).status_code==200
    created=key(client)
    rotated=client.post('/api/v1/keys/'+created['id']+'/rotate',json={}).json()
    assert client.get('/v1/models',headers={'Authorization':'Bearer '+created['token']}).status_code==401
    assert client.get('/v1/models',headers={'Authorization':'Bearer '+rotated['token']}).status_code==200


def test_health_drift_and_verified_capability_routing(workspace):
    client,app,_=workspace
    result=connect(client)
    model=client.get('/api/v1/connections').json()[0]['models'][0]
    probe=client.post('/api/v1/deployments/'+model['id']+'/probe',json={'capability':'chat'})
    assert probe.status_code==200 and probe.json()['passed'] is True
    port=client.get('/api/v1/ports').json()[0]
    port.update(verified_only=True,requirements=['chat'])
    assert client.patch('/api/v1/ports/'+port['id'],json=port).status_code==200
    assert call(client,port['name']).status_code==200
    app.state.store.update('deployments',model['id'],{'metadata_hash':'a-different-model-revision'})
    check=client.post('/api/v1/connections/'+result['id']+'/check',json={})
    assert check.status_code==200 and check.json()['changes']
    assert call(client,port['name']).status_code==422
    assert client.get('/api/v1/operations').json()['notifications']


def test_encrypted_backup_restore_and_accounting_correction(workspace):
    client,app,_=workspace
    result=connect(client)
    response=call(client,result['services'][0]['name'],stream=True,messages=[{'role':'user','content':'truncate'}])
    request_id=response.headers['X-Request-ID']
    corrected=client.post('/api/v1/events/'+request_id+'/reconcile',json={'cost_micros':123,'input_tokens':10,'output_tokens':4,'note':'Confirmed provider invoice'})
    assert corrected.status_code==200
    record=client.get('/api/v1/events/'+request_id).json()
    assert record['cost_micros']==123 and record['usage_source']=='reconciled'
    archived=client.post('/api/v1/backups',json={'password':'backup-password-2026'})
    assert archived.status_code==200 and archived.content.startswith(b'PLQBACK01')
    assert b'provider-secret-value' not in archived.content
    client.post('/api/v1/projects',json={'name':'After backup'})
    restore={'password':'wrong-password-2026','archive':base64.b64encode(archived.content).decode(),'confirmation':'RESTORE'}
    assert client.post('/api/v1/backups/restore',json=restore).status_code==422
    assert client.get('/api/v1/projects').status_code==200
    restore['password']='backup-password-2026'
    restored=client.post('/api/v1/backups/restore',json=restore)
    assert restored.status_code==200,restored.text
    assert client.get('/api/v1/session').json()['user'] is None
    sign_in(client,'owner@example.test','long-local-password')
    assert all(r['name']!='After backup' for r in client.get('/api/v1/projects').json())
    assert call(client,result['services'][0]['name']).status_code==200
    monitoring=client.post('/api/v1/monitoring-token',json={}).json()['token']
    assert client.get('/metrics').status_code==401
    assert 'pliqra_requests_total' in client.get('/metrics',headers={'Authorization':'Bearer '+monitoring}).text


def test_scim_users_groups_scopes_and_deprovisioning(workspace):
    client,app,_=workspace
    provider=client.post('/api/v1/identity-providers',json={'name':'Company','issuer':'https://identity.test','client_id':'client','client_secret':'secret'}).json()['id']
    project=client.post('/api/v1/projects',json={'name':'SCIM project'}).json()['id']
    token=client.post('/api/v1/identity-providers/'+provider+'/scim-token',json={}).json()['token']
    headers={'Authorization':'Bearer '+token,'Content-Type':'application/scim+json'}
    root='/scim/v2/'+provider
    assert client.get(root+'/ServiceProviderConfig',headers=headers).status_code==200
    assert client.get('/api/v1/users',headers=headers).status_code==401
    person=client.post(root+'/Users',headers=headers,json={'userName':'person@example.test','externalId':'stable-subject','displayName':'SCIM Person','emails':[{'value':'person@example.test'}],'active':True})
    assert person.status_code==201,person.text
    user=person.json()['id']
    filtered=client.get(root+'/Users',headers=headers,params={'filter':'userName eq "person@example.test"'}).json()
    assert filtered['totalResults']==1 and filtered['Resources'][0]['externalId']=='stable-subject'
    group=client.post(root+'/Groups',headers=headers,json={'displayName':'Finance','externalId':'group-finance','members':[{'value':user}]}).json()['id']
    client.post('/api/v1/group-rules',json={'provider_id':provider,'group_name':'Finance','project_id':project,'role':'manager'})
    assert app.state.store.one('SELECT * FROM memberships WHERE user_id=? AND project_id=?',(user,project))['role']=='manager'
    removed=client.patch(root+'/Groups/'+group,headers=headers,json={'Operations':[{'op':'remove','path':f'members[value eq "{user}"]'}]})
    assert removed.status_code==200 and not removed.json()['members']
    assert not app.state.store.one('SELECT * FROM memberships WHERE user_id=? AND project_id=?',(user,project))
    assert client.patch(root+'/Users/'+user,headers=headers,json={'Operations':[{'op':'replace','path':'active','value':False}]}).json()['active'] is False
    assert client.get('/scim/v2/wrong-provider/Users',headers=headers).status_code==401


def test_evaluations_and_attempt_history(workspace):
    client,app,_=workspace
    result=connect(client)
    dataset=client.post('/api/v1/evaluation-sets',json={'name':'Task checks','cases':[{'name':'Expected answer','prompt':'Say hello','contains':'Hello!'}]})
    assert dataset.status_code==201
    run=client.post('/api/v1/evaluations',json={'name':'Real comparison','dataset_id':dataset.json()['id'],'services':[result['services'][0]['name']],'store_outputs':False})
    assert run.status_code==202,run.text
    deadline=time.time()+5
    while time.time()<deadline:
        row=client.get('/api/v1/evaluations').json()[0]
        if row['status'] not in ('queued','running'):break
        time.sleep(.02)
    assert row['status']=='completed',row
    assert row['results'][0]['passed'] is True
    assert 'output' not in row['results'][0]
    record=client.get('/api/v1/events/'+row['results'][0]['request_id']).json()
    assert record['request_kind']=='evaluation' and len(record['attempts'])==1
    rejected=client.post('/api/v1/evaluation-sets',json={'name':'No network schemas','cases':[{'name':'Bad','prompt':'x','json_schema':{'$ref':'https://remote.test/schema'}}]})
    assert rejected.status_code==422


def saml_certificate():
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.x509.oid import NameOID
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Test SAML IdP')])
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(days=1)).not_valid_after(datetime.now(timezone.utc)+timedelta(days=30)).sign(key,hashes.SHA256())
    return key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode(),cert.public_bytes(serialization.Encoding.PEM).decode()


def test_signed_saml_login_replay_and_signature_rejection(workspace):
    from lxml import etree
    from onelogin.saml2.utils import OneLogin_Saml2_Utils
    client,app,_=workspace
    private,cert=saml_certificate()
    response=client.post('/api/v1/identity-providers/saml',json={'name':'SAML Company','entity_id':'https://saml.identity.test','sso_url':'https://saml.identity.test/login','certificate':cert})
    assert response.status_code==201,response.text
    provider=response.json()['id']
    app.state.context['public_url']='https://pliqra.example.test'
    assert 'sp_private_key' not in json.dumps(client.get('/api/v1/identity-providers').json())
    metadata=client.get(f'/auth/saml/{provider}/metadata')
    assert metadata.status_code==200 and 'AssertionConsumerService' in metadata.text
    login=client.get(f'/auth/saml/{provider}/login',follow_redirects=False)
    assert login.status_code==302,login.text
    query=parse_qs(urlsplit(login.headers['location']).query)
    assert query.get('Signature')
    request_xml=etree.fromstring(zlib.decompress(base64.b64decode(query['SAMLRequest'][0]),-15))
    request_id=request_xml.get('ID');relay=query['RelayState'][0]
    stamp=lambda offset:(datetime.now(timezone.utc)+timedelta(seconds=offset)).strftime('%Y-%m-%dT%H:%M:%SZ')
    acs=f'https://pliqra.example.test/auth/saml/{provider}/acs'
    audience=f'https://pliqra.example.test/auth/saml/{provider}/metadata'
    assertion=f'''<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_assertion_test" Version="2.0" IssueInstant="{stamp(0)}"><saml:Issuer>https://saml.identity.test</saml:Issuer><saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified">saml-person</saml:NameID><saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData InResponseTo="{request_id}" Recipient="{acs}" NotOnOrAfter="{stamp(300)}"/></saml:SubjectConfirmation></saml:Subject><saml:Conditions NotBefore="{stamp(-30)}" NotOnOrAfter="{stamp(300)}"><saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction></saml:Conditions><saml:AuthnStatement AuthnInstant="{stamp(0)}" SessionIndex="_session"><saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement><saml:AttributeStatement><saml:Attribute Name="email"><saml:AttributeValue>person@example.test</saml:AttributeValue></saml:Attribute><saml:Attribute Name="displayName"><saml:AttributeValue>SAML Person</saml:AttributeValue></saml:Attribute></saml:AttributeStatement></saml:Assertion>'''
    signed=OneLogin_Saml2_Utils.add_sign(assertion,private,cert,sign_algorithm='http://www.w3.org/2001/04/xmldsig-more#rsa-sha256',digest_algorithm='http://www.w3.org/2001/04/xmlenc#sha256')
    envelope=f'''<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_response_test" Version="2.0" IssueInstant="{stamp(0)}" Destination="{acs}" InResponseTo="{request_id}"><saml:Issuer>https://saml.identity.test</saml:Issuer><samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status></samlp:Response>'''
    root=etree.fromstring(envelope.encode());root.append(etree.fromstring(signed))
    encoded=base64.b64encode(etree.tostring(root)).decode()
    headers={'Content-Type':'application/x-www-form-urlencoded','Cookie':'plq_saml='+relay}
    tampered=encoded # Test invalid signatures using a modified signed identity.
    broken=base64.b64encode(etree.tostring(root).replace(b'SAML Person',b'Other Person')).decode()
    invalid=client.post(acs,headers=headers,data={'SAMLResponse':broken,'RelayState':relay},follow_redirects=False)
    assert invalid.status_code==403
    good=client.post(acs,headers=headers,data={'SAMLResponse':encoded,'RelayState':relay},follow_redirects=False)
    assert good.status_code==303,good.text
    session=client.get('https://pliqra.example.test/api/v1/session').json()
    assert session['user']['name']=='SAML Person' and session['user']['role']=='viewer'
    replay=client.post(acs,headers=headers,data={'SAMLResponse':encoded,'RelayState':relay},follow_redirects=False)
    assert replay.status_code==403


def test_optional_content_is_encrypted_audited_and_admin_only(workspace):
    client,app,_=workspace
    result=connect(client)
    port=client.get('/api/v1/ports').json()[0]
    first=call(client,port['name'])
    assert client.get('/api/v1/events/'+first.headers['X-Request-ID']+'/content').status_code==404
    port['log_content']=True
    assert client.patch('/api/v1/ports/'+port['id'],json=port).status_code==200
    response=call(client,port['name'])
    record=response.headers['X-Request-ID']
    stored=app.state.store.one('SELECT payload FROM event_content WHERE id=?',(record,))['payload']
    assert 'Private prompt' not in stored and 'Hello!' not in stored
    detail=client.get('/api/v1/events/'+record).json()
    assert detail['content_available']
    content=client.get('/api/v1/events/'+record+'/content')
    assert content.status_code==200 and content.json()['response']['choices'][0]['message']['content']=='Hello!'
    assert client.get('/api/v1/audit').json()[0]['action']=='request.content_viewed'
    local_user(client,'ReadOnly')
    client.post('/api/v1/logout',json={});sign_in(client,'readonly@example.test')
    assert client.get('/api/v1/events/'+record+'/content').status_code==403
    client.post('/api/v1/logout',json={});sign_in(client,'owner@example.test','long-local-password')
    assert client.delete('/api/v1/events/'+record+'/content').status_code==200
    assert client.get('/api/v1/events/'+record+'/content').status_code==404


def test_invalid_import_and_user_creation_are_atomic(workspace):
    client,app,_=workspace
    initial=len(client.get('/api/v1/users').json())
    assert client.post('/api/v1/users',json={'name':'Invalid','email':'invalid@example.test','password':'long-test-password','project_ids':['missing']}).status_code==422
    assert len(client.get('/api/v1/users').json())==initial
    for service,refs in [(None,[]),([],[]),({},[None]),({},'invalid')]:
        response=client.post('/api/v1/ports/import',json={'document':{'schema':'pliqra/service-export/v1','service':service,'deployment_refs':refs},'mapping':{}})
        assert response.status_code==422,response.text


def test_process_lease_and_content_key_recovery(workspace):
    from pliqra.db import Store
    client,app,_=workspace
    with pytest.raises(RuntimeError,match='already in use'):Store(app.state.store.directory)
    assert client.get('/sdk/pliqra_client.py').status_code==200
    assert client.get('/protocol/specification').status_code==200


def test_admin_all_history_filters_and_unlimited_csv(workspace):
    client,app,_=workspace
    result=connect(client);price(client)
    token=key(client)
    response=client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+token['token']},json={'model':result['services'][0]['name'],'messages':[{'role':'user','content':'Test cumulative history'}]})
    record=response.headers['X-Request-ID']
    app.state.store.update('events',record,{'started':time.time()-120*86400,'port_name':'=spreadsheet()'})
    assert client.get('/api/v1/usage').json()['summary']['requests']==0
    usage=client.get('/api/v1/usage?days=0').json()
    assert usage['summary']['requests']==1
    assert usage['summary']['p95_ms'] is not None
    assert client.get('/api/v1/events',params={'days':0,'key_id':token['id']}).json()['total']==1
    assert client.get('/api/v1/events',params={'days':0,'key_id':'other'}).json()['total']==0
    exported=client.get('/api/v1/usage.csv',params={'days':0,'key_id':token['id']})
    assert exported.status_code==200 and "'=spreadsheet()" in exported.text


def test_fallback_attempt_cost_and_circuit_breaker(workspace):
    client,app,captured=workspace
    a=connect(client,'https://models.test/v1',model='unavailable',provider='openai');b=connect(client)
    services={p['id']:p for p in client.get('/api/v1/ports').json()}
    dep_a=services[a['services'][0]['id']]['deployments'][0];dep_b=services[b['services'][0]['id']]['deployments'][0]
    port=client.post('/api/v1/ports',json={'name':'resilient','deployments':[dep_a,dep_b]}).json()
    for _ in range(3):assert call(client,'resilient').status_code==200
    assert app.state.store.one('SELECT open_until FROM deployments WHERE id=?',(dep_a,))['open_until']>time.time()
    before=len(captured)
    response=call(client,'resilient')
    assert response.status_code==200
    assert len(captured)==before+1
    detail=client.get('/api/v1/events/'+response.headers['X-Request-ID']).json()
    assert len(detail['attempts'])==1
    prior=client.get('/api/v1/events',params={'service_id':port['id']}).json()['items'][1]
    prior=client.get('/api/v1/events/'+prior['id']).json()
    assert len(prior['attempts'])==2 and prior['usage_source']=='partial'
    assert prior['cost_micros'] is None


def test_scim_discovery_documents(workspace):
    client,app,_=workspace
    provider=client.post('/api/v1/identity-providers',json={'name':'Enterprise','issuer':'https://identity.test','client_id':'client','client_secret':'secret'}).json()['id']
    token=client.post('/api/v1/identity-providers/'+provider+'/scim-token',json={}).json()['token']
    headers={'Authorization':'Bearer '+token}
    root='/scim/v2/'+provider
    for resource in ['Schemas','ResourceTypes']:
        response=client.get(root+'/'+resource,headers=headers)
        assert response.status_code==200 and response.json()['totalResults']==(3 if resource=='Schemas' else 2)
        item=response.json()['Resources'][0]
        assert client.get(root+'/'+resource+'/'+item['id'],headers=headers).json()['id']==item['id']
    assert client.get(root+'/Schemas').status_code==401


def test_structured_and_function_capability_evidence(workspace):
    import asyncio,httpx
    from tests.test_app import Fragments
    client,app,_=workspace
    connect(client)
    model=client.get('/api/v1/connections').json()[0]['models'][0]
    def upstream(request):
        body=json.loads(request.content)
        if body.get('stream'):
            return httpx.Response(200,stream=Fragments('data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n'))
        message={'role':'assistant','content':'{"ok":true}'}
        if body.get('tools'):message={'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'pliqra_probe','arguments':'{"value":"ok"}'}}]}
        return httpx.Response(200,json={'choices':[{'message':message}]})
    mock=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app.state.gateway.client=mock
    for capability in ['streaming','structured_output','tools']:
        result=client.post('/api/v1/deployments/'+model['id']+'/probe',json={'capability':capability})
        assert result.status_code==200 and result.json()['passed'] is True,result.text
    current=client.get('/api/v1/connections').json()[0]['models'][0]
    assert {'streaming','structured_output','tools'}<=set(current['verified'])
    assert client.get('/api/v1/events',params={'request_kind':'probe'}).json()['total']==3
    asyncio.run(mock.aclose())


def test_stream_cancellation_releases_capacity_and_records_unknown_usage(workspace):
    import asyncio,httpx
    client,app,_=workspace
    connect(client)
    port=client.get('/api/v1/ports').json()[0]
    closed=[]
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"First token"}}]}\n\n'
            await asyncio.sleep(60)
        async def aclose(self):closed.append(True)
    mock=httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=SlowStream())))
    app.state.gateway.client=mock
    owner=app.state.store.one("SELECT id FROM users WHERE role='admin'")['id']
    async def scenario():
        response=await app.state.gateway.chat({'model':port['name'],'messages':[{'role':'user','content':'Cancellation test'}],'stream':True},{'id':owner,'kind':'user','role':'admin'})
        iterator=response.body_iterator
        assert 'First token' in await iterator.__anext__()
        waiting=asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):await waiting
        assert app.state.gateway.active==0
        await mock.aclose()
        return response.headers['X-Request-ID']
    record=client.portal.call(scenario)
    event=client.get('/api/v1/events/'+record).json()
    assert event['status']=='cancelled' and event['usage_source']=='unknown'
    assert event['cost_micros'] is None and closed


def test_interrupted_restore_recovers_database_and_matching_key(tmp_path):
    import sqlite3
    from pliqra.db import Store
    from pliqra.security import Vault
    store=Store(tmp_path);vault=Vault(tmp_path)
    store.set_setting('recovery-test','original')
    recovery=sqlite3.connect(tmp_path/'pre-restore.sqlite3');store.conn.backup(recovery);recovery.close()
    (tmp_path/'pre-restore.key').write_bytes(vault.key)
    original_key=vault.key
    store.set_setting('recovery-test','partially-restored')
    (tmp_path/'encryption.key').write_bytes(__import__('cryptography.fernet',fromlist=['Fernet']).Fernet.generate_key())
    (tmp_path/'pending-restore').write_text('rollback-on-restart')
    store.close()
    recovered=Store(tmp_path)
    try:
        assert recovered.setting('recovery-test')=='original'
        assert Vault(tmp_path).key==original_key
        assert not (tmp_path/'pending-restore').exists()
    finally:recovered.close()


def test_legacy_database_and_encrypted_credentials_migrate(tmp_path):
    from pliqra.db import Store,now
    from pliqra.security import Vault
    store=Store(tmp_path);vault=Vault(tmp_path)
    store.set_setting('upgrade-proof','retained')
    store.insert('connections',{'id':'old','name':'Existing endpoint','endpoint':'https://models.test/v1','provider':'openai','secret':vault.encrypt('existing-provider-credential'),'status':'connected','created':now()})
    key=vault.key
    store.close()
    (tmp_path/'pliqra.sqlite3').rename(tmp_path/'modelport.sqlite3')
    migrated=Store(tmp_path)
    try:
        assert migrated.setting('upgrade-proof')=='retained'
        assert Vault(tmp_path).key==key
        assert Vault(tmp_path).decrypt(migrated.one('SELECT secret FROM connections')['secret'])=='existing-provider-credential'
        assert not (tmp_path/'modelport.sqlite3').exists()
        assert (tmp_path/'pliqra.sqlite3').exists()
    finally:migrated.close()


def test_legacy_environment_fallback_and_canonical_precedence(monkeypatch):
    from pliqra.config import env
    monkeypatch.delenv('PLIQRA_PUBLIC_URL',raising=False)
    monkeypatch.setenv('MODELPORT_PUBLIC_URL','https://old.example.test')
    assert env('PLIQRA_PUBLIC_URL')=='https://old.example.test'
    monkeypatch.setenv('PLIQRA_PUBLIC_URL','https://new.example.test')
    assert env('PLIQRA_PUBLIC_URL')=='https://new.example.test'


def test_legacy_encrypted_backup_remains_restorable(workspace):
    import hashlib
    from cryptography.fernet import Fernet
    client,app,_=workspace
    connect(client)
    password='upgrade-backup-password'
    archive=client.post('/api/v1/backups',json={'password':password}).content
    assert archive[:9]==b'PLQBACK01'
    salt=archive[9:25]
    cipher=Fernet(base64.urlsafe_b64encode(hashlib.pbkdf2_hmac('sha256',password.encode(),salt,600000)))
    data=json.loads(cipher.decrypt(archive[25:]));data['format']='modelport-backup/1'
    old=b'MPBACKUP1'+salt+cipher.encrypt(json.dumps(data).encode())
    response=client.post('/api/v1/backups/restore',json={'password':password,'archive':base64.b64encode(old).decode(),'confirmation':'RESTORE'})
    assert response.status_code==200,response.text
    sign_in(client,'owner@example.test','long-local-password')
    assert len(client.get('/api/v1/connections').json())==1
    assert app.state.vault.decrypt(app.state.store.one('SELECT secret FROM connections')['secret'])=='provider-secret-value'


def test_attribution_aliases_are_not_forwarded_to_model(workspace):
    client,app,captured=workspace
    result=connect(client)
    service=result['services'][0]['name']
    for metadata in [{'pliqra':{'project_id':'default'}},{'modelport':{'project_id':'default'}},{'pliqra':{'project_id':'default'},'modelport':{'project_id':'ignored'}}]:
        response=call(client,service,**metadata)
        assert response.status_code==200,response.text
        upstream=json.loads(captured[-1].content)
        assert 'pliqra' not in upstream and 'modelport' not in upstream
        event=client.get('/api/v1/events/'+response.headers['X-Request-ID']).json()
        assert event['project_id']=='default'
