"""Independent control/member databases, explicit trust and uncertain delivery recovery."""
import asyncio
import json
import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from pliqra.main import create_app
from pliqra.fleet import clean_snapshot
from fastapi import HTTPException
from tests.test_app import connect, call
from tests.test_architecture import swap_upstream


def initialize(client, name):
    response = client.post('/api/v1/setup', json={'name': name, 'email': name.lower() + '@example.test', 'password': 'synthetic-long-password'})
    assert response.status_code == 200, response.text
    client.headers['X-Pliqra-CSRF'] = response.json()['csrf']
    return response.json()['user']['id']


@pytest.fixture(params=['sqlite','postgres'])
def fleet(tmp_path,request):
    options=[{},{}]
    if request.param=='postgres':
        url=os.getenv('PLIQRA_TEST_POSTGRES_URL')
        if not url:pytest.skip('Real PostgreSQL integration URL is not configured')
        from cryptography.fernet import Fernet
        options=[{'database_url':url,'database_schema':'fleet_'+uuid.uuid4().hex,'encryption_key':Fernet.generate_key()} for _ in range(2)]
    source, target = create_app(tmp_path / 'controller',**options[0]), create_app(tmp_path / 'member',**options[1])
    with TestClient(source) as controller, TestClient(target) as member:
        author = initialize(controller, 'Author')
        initialize(member, 'Owner')
        reviewer = controller.post('/api/v1/users', json={'name': 'Reviewer', 'email': 'reviewer@example.test', 'password': 'synthetic-long-password', 'role': 'admin', 'project_ids': []})
        assert reviewer.status_code == 201, reviewer.text
        service = connect(member, endpoint='https://models.test/v1', provider='openai', model='synthetic')['services'][0]
        grant = member.post('/api/v1/fleet/trusts', json={'name': 'Controller', 'scopes': ['status', 'policy']}).json()
        state = {'lose_response': False, 'apply_calls': 0, 'redirect': False}
        transport = httpx.ASGITransport(app=target)

        async def forward(request):
            if state['redirect']:
                return httpx.Response(307, headers={'location': 'https://untrusted.test/steal'})
            response = await transport.handle_async_request(request)
            if request.url.path.endswith('/apply'):
                state['apply_calls'] += 1
                if state['lose_response']:
                    state['lose_response'] = False
                    await response.aclose()
                    raise httpx.ReadError('Synthetic response loss after the member committed', request=request)
            return response

        mock = httpx.AsyncClient(transport=httpx.MockTransport(forward))
        source.state.client = mock
        paired = controller.post('/api/v1/fleet/members', json={'name': 'Research instance', 'endpoint': 'http://127.0.0.1:9999', 'token': grant['token']})
        assert paired.status_code == 201, paired.text
        yield controller, member, source, target, service, grant, paired.json(), state, author
        asyncio.run(mock.aclose())
    if request.param=='postgres':
        import psycopg
        with psycopg.connect(url,autocommit=True) as db:
            for option in options:db.execute('DROP SCHEMA "'+option['database_schema']+'" CASCADE')


def propose(fleet, limit=3):
    controller, _, _, _, service, _, member, _, _ = fleet
    response = controller.post('/api/v1/fleet/proposals', json={'name': 'Shared gateway rules',
        'document': {'name': 'Team AI', 'slug': 'fleet-team', 'config': {'services': ['chat'], 'limits': [{'metric': 'requests', 'scope': 'gateway', 'window_seconds': 60, 'limit': limit}]}},
        'targets': [{'member_id': member['id'], 'mapping': {'services': {'chat': service['id']}}}]})
    assert response.status_code == 201, response.text
    return response.json()


def approve(controller, proposal, email='reviewer@example.test'):
    response = controller.post('/api/v1/login', json={'email': email, 'password': 'synthetic-long-password'})
    assert response.status_code == 200
    controller.headers['X-Pliqra-CSRF'] = response.json()['csrf']
    decision = controller.post('/api/v1/fleet/proposals/' + proposal['id'] + '/decision', json={'fingerprint': proposal['fingerprint']})
    assert decision.status_code == 200, decision.text
    return decision.json()


def test_fleet_scoped_credentials_namespaces_approval_and_idempotence(fleet):
    controller, member, source, target, _, grant, paired, state, author = fleet
    serialized = json.dumps(controller.get('/api/v1/fleet').json())
    assert grant['token'] not in serialized and 'provider-secret-value' not in serialized
    encrypted = source.state.store.one('SELECT secret FROM fleet_members')['secret']
    assert encrypted != grant['token'] and source.state.vault.decrypt(encrypted) == grant['token']
    assert member.get('/api/v1/settings', headers={'Authorization': 'Bearer ' + grant['token']}).status_code == 401
    proposal = propose(fleet)
    path = '/api/v1/fleet/proposals/' + proposal['id']
    assert controller.post(path + '/deploy', json={}).status_code == 409
    assert controller.post(path + '/decision', json={'fingerprint': proposal['fingerprint']}).status_code == 403
    approved = approve(controller, proposal)
    assert approved['author'] == author and approved['approved_by'] != author
    result = controller.post(path + '/deploy', json={})
    assert result.status_code == 200, result.text
    assert result.json()['targets'][0]['status'] == 'applied'
    assert controller.post(path + '/deploy', json={}).json()['targets'][0]['status'] == 'applied'
    assert state['apply_calls'] == 1
    profile = target.state.store.one('SELECT * FROM gateway_profiles')
    assert profile['version'] == 1
    assert json.loads(profile['config'])['limits'][0]['limit'] == 3
    controller.post('/api/v1/fleet/members/' + paired['id'] + '/sync', json={})
    snapshot = controller.get('/api/v1/fleet').json()['members'][0]['snapshot']
    assert snapshot['managed_gateways'][0]['drifted'] is False
    assert target.state.store.one("SELECT COUNT(*) AS n FROM audit WHERE action='fleet.policy_applied'")['n'] == 1


def test_fleet_metadata_whitelist_discards_content_and_rejects_invalid_totals(fleet):
    _,member,_,_,_,grant,_,_,_=fleet
    snapshot=member.get('/api/v1/fleet-agent/status',headers={'Authorization':'Bearer '+grant['token']}).json()
    snapshot['prompt']='never retained'
    snapshot['services'][0]['credential']='never retained'
    cleaned=clean_snapshot(snapshot)
    assert 'never retained' not in json.dumps(cleaned)
    snapshot['totals']['requests']=-1
    with pytest.raises(HTTPException) as error:clean_snapshot(snapshot)
    assert error.value.status_code==502


def test_lost_commit_response_is_recovered_without_duplicate_application(fleet):
    controller, _, _, target, _, _, _, state, _ = fleet
    proposal = approve(controller, propose(fleet))
    state['lose_response'] = True
    path = '/api/v1/fleet/proposals/' + proposal['id'] + '/deploy'
    assert controller.post(path, json={}).json()['targets'][0]['status'] == 'unknown'
    assert target.state.store.one('SELECT version FROM gateway_profiles')['version'] == 1
    retry = controller.post(path, json={}).json()['targets'][0]
    assert retry['status'] == 'applied' and retry['receipt']['idempotent_replay'] is True
    assert target.state.store.one('SELECT version FROM gateway_profiles')['version'] == 1
    assert state['apply_calls'] == 2


def test_service_drift_after_planning_blocks_delivery(fleet):
    controller, member, _, target, service, _, _, _, _ = fleet
    proposal = approve(controller, propose(fleet))
    port = target.state.gateway.port(service['id'])
    changed = member.patch('/api/v1/ports/' + service['id'], json={**port, 'max_output_tokens': 64})
    assert changed.status_code == 200, changed.text
    result = controller.post('/api/v1/fleet/proposals/' + proposal['id'] + '/deploy', json={})
    assert result.json()['targets'][0]['status'] == 'conflict'
    assert not target.state.store.one('SELECT id FROM gateway_profiles')


def test_pairing_cannot_overwrite_local_gateway_or_escape_prefix(fleet):
    _, member, _, _, service, grant, _, _, _ = fleet
    headers = {'Authorization': 'Bearer ' + grant['token']}
    local = {'name': 'Local owner', 'slug': 'fleet-team', 'config': {'services': [service['id']]}}
    assert member.post('/api/v1/gateways', json=local).status_code == 201
    body = {'document': {**local, 'config': {'services': ['chat']}}, 'mapping': {'services': {'chat': service['id']}}}
    assert member.post('/api/v1/fleet-agent/plan', headers=headers, json=body).status_code == 409
    body['document']['slug'] = 'unrelated'
    assert member.post('/api/v1/fleet-agent/plan', headers=headers, json=body).status_code == 403


def test_revocation_redirect_and_stale_snapshots_are_visible_and_excluded(fleet):
    controller, member, source, _, _, grant, paired, state, _ = fleet
    assert controller.get('/api/v1/fleet/summary?team_id=foreign').status_code == 422
    assert controller.get('/api/v1/fleet/summary').json()['included_members'] == [paired['id']]
    state['redirect'] = True
    assert controller.post('/api/v1/fleet/members/' + paired['id'] + '/sync', json={}).status_code == 502
    assert controller.get('/api/v1/fleet/summary').json()['excluded_members'] == [paired['id']]
    state['redirect'] = False
    assert member.delete('/api/v1/fleet/trusts/' + grant['id']).status_code == 200
    assert controller.post('/api/v1/fleet/members/' + paired['id'] + '/sync', json={}).status_code == 502
    assert source.state.store.one('SELECT last_error FROM fleet_members')['last_error']


def test_status_aggregates_metadata_without_prompt_or_credential_content(fleet):
    controller, member, _, target, service, _, paired, _, _ = fleet
    mock = swap_upstream(target, lambda _: httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'Private response must stay local'}}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 2}}))
    assert call(member, service['name']).status_code == 200
    result = controller.post('/api/v1/fleet/members/' + paired['id'] + '/sync', json={})
    assert result.status_code == 200, result.text
    serialized = json.dumps(result.json())
    assert 'Private prompt' not in serialized and 'Private response' not in serialized and 'provider-secret-value' not in serialized
    summary = controller.get('/api/v1/fleet/summary').json()
    assert summary['totals']['requests'] == 1 and summary['totals']['input_tokens'] == 5
    assert summary['teams'][0]['member_id'] == paired['id']
    asyncio.run(mock.aclose())


def test_readonly_pairing_and_expired_or_wrong_workspace_plans_cannot_write(fleet):
    controller,member,_,target,service,grant,_,_,_=fleet
    readonly=member.post('/api/v1/fleet/trusts',json={'name':'Read only'}).json()
    body={'document':{'name':'Test','slug':'fleet-test','config':{'services':['chat']}},'mapping':{'services':{'chat':service['id']}}}
    assert member.post('/api/v1/fleet-agent/plan',headers={'Authorization':'Bearer '+readonly['token']},json=body).status_code==403
    proposal=propose(fleet)
    planned=proposal['targets'][0]['plan']
    headers={'Authorization':'Bearer '+grant['token']}
    for change in ({'workspace_id':'foreign-workspace'},{'expires':0}):
        response=member.post('/api/v1/fleet-agent/apply',headers=headers,json={'operation_id':'synthetic-expired','plan':{**planned,**change},'author':'author','approver':'reviewer'})
        assert response.status_code==409,response.text
    assert not target.state.store.one('SELECT id FROM gateway_profiles')


def test_local_drift_is_visible_and_rollback_requires_a_new_second_approval(fleet):
    controller,member,_,target,_,_,paired,_,_=fleet
    proposal=approve(controller,propose(fleet))
    response=controller.post('/api/v1/fleet/proposals/'+proposal['id']+'/deploy',json={})
    assert response.json()['targets'][0]['status']=='applied'
    gateway=member.get('/api/v1/gateways').json()[0]
    update=member.put('/api/v1/gateways/'+gateway['id'],json={'name':gateway['name'],'slug':gateway['slug'],'config':{**gateway['config'],'max_output_tokens':123}})
    assert update.status_code==200,update.text
    controller.post('/api/v1/fleet/members/'+paired['id']+'/sync',json={})
    assert controller.get('/api/v1/fleet').json()['members'][0]['snapshot']['managed_gateways'][0]['drifted'] is True
    rollback=controller.post('/api/v1/fleet/proposals/'+proposal['id']+'/rollback-proposal',json={})
    assert rollback.status_code==200,rollback.text
    rollback=rollback.json()
    path='/api/v1/fleet/proposals/'+rollback['id']
    assert controller.post(path+'/deploy',json={}).status_code==409
    approve(controller,rollback,'author@example.test')
    assert controller.post(path+'/deploy',json={}).json()['targets'][0]['status']=='applied'
    assert json.loads(target.state.store.one('SELECT config FROM gateway_profiles')['config'])['enabled'] is False


def test_same_workspace_cannot_be_counted_twice_through_another_replica_url(fleet):
    controller,_,_,_,_,grant,_,_,_=fleet
    duplicate=controller.post('/api/v1/fleet/members',json={'name':'Another ingress','endpoint':'http://127.0.0.1:9998','token':grant['token']})
    assert duplicate.status_code==409,duplicate.text
    assert len(controller.get('/api/v1/fleet').json()['members'])==1
