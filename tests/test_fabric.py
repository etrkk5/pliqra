import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx

from tests.test_app import workspace, connect, key, call, price
from tests.test_product import local_user, sign_in
from pliqra.db import Store


def gateway(client, service_ids, **config):
    result = client.post('/api/v1/gateways', json={'name': 'Finance gateway', 'slug': 'finance', 'config': {'services': service_ids, **config}})
    assert result.status_code == 201, result.text
    return result.json()


def rail(client, **values):
    result = client.post('/api/v1/guardrails', json={'name': 'Content policy', 'provider': 'builtin',
                         'config': {'rule': 'keywords', 'terms': ['forbidden']}, **values})
    assert result.status_code == 201, result.text
    return result.json()


def bound_key(client, profile):
    token = key(client, gateway_id=profile['id'], rpm=10000)
    return token, {'Authorization': 'Bearer '+token['token']}


def invoke(client, service, headers=None, path='/v1/chat/completions', content='Hello', **options):
    return client.post(path, headers=headers or {}, json={'model': service, 'messages': [{'role': 'user', 'content': content}], **options})


def test_named_gateway_key_cannot_bypass_shared_limits_or_catalog(workspace):
    client, app, captured = workspace
    first, second = connect(client), connect(client)
    service = first['services'][0]
    profile = gateway(client, [service['id']], limits=[{'metric': 'requests', 'scope': 'gateway', 'limit': 2, 'window_seconds': 3600}])
    token, headers = bound_key(client, profile)
    assert [r['id'] for r in client.get('/v1/models', headers=headers).json()['data']] == [service['name']]
    assert invoke(client, second['services'][0]['name'], headers).status_code == 403
    binding = client.post('/protocol/v1/bindings', headers=headers, json={'service': service['name']}).json()
    assert binding['gateway_fingerprint'] == profile['fingerprint']
    assert invoke(client, service['name'], headers).status_code == 200
    assert invoke(client, service['name'], headers, binding['invoke']).status_code == 200
    before = len(captured)
    denied = invoke(client, service['name'], headers, '/g/finance/v1/chat/completions')
    assert denied.status_code == 429 and denied.json()['detail']['metric'] == 'requests'
    assert len(captured) == before
    other = client.post('/api/v1/gateways', json={'name': 'Other', 'slug': 'other', 'config': {'services': [service['id']]}}).json()
    assert invoke(client, service['name'], headers, '/g/other/v1/chat/completions').status_code == 403
    ledger = app.state.store.all('SELECT * FROM gateway_requests')
    assert len(ledger) == 2 and all(r['tokens'] == 19 and r['status'] == 'success' for r in ledger)
    assert client.get('/api/v1/usage', params={'gateway_id': profile['id']}).json()['summary']['successful'] == 2


def test_input_guard_blocks_before_inference_and_releases_reservations(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]; price(client)
    project = client.get('/api/v1/projects').json()[0]
    client.patch('/api/v1/projects/'+project['id'], json={'name': project['name'], 'monthly_budget': 10, 'max_concurrency': 20})
    guard = rail(client)
    profile = gateway(client, [service['id']], project_id=project['id'], guardrails=[{'guardrail_id': guard['id'], 'stages': ['input']}])
    _, headers = bound_key(client, profile)
    before = len(captured)
    result = invoke(client, service['name'], headers, content='forbidden')
    assert result.status_code == 422
    assert len(captured) == before
    request = client.get('/api/v1/events/'+result.json()['detail']['request_id']).json()
    assert request['status'] == 'blocked' and request['cost_micros'] == request['reserved_micros'] == 0
    assert request['usage_source'] == 'not_invoked'
    assert request['model'] is None and not request['attempts']
    assert request['guardrails'][0]['outcome'] == 'blocked'
    assert app.state.store.one('SELECT COUNT(*) AS n FROM events')['n'] == 1
    assert app.state.store.one('SELECT tokens FROM gateway_requests')['tokens'] == 0


def test_output_guard_buffers_sse_and_never_leaks_blocked_text(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    guard = rail(client, config={'rule': 'keywords', 'terms': ['Hello']})
    profile = gateway(client, [service['id']], guardrails=[{'guardrail_id': guard['id'], 'stages': ['output']}])
    _, headers = bound_key(client, profile)
    result = invoke(client, service['name'], headers, stream=True)
    assert result.status_code == 422 and 'Hello!' not in result.text
    assert not json.loads(captured[-1].content)['stream']
    event = client.get('/api/v1/events/'+result.json()['detail']['request_id']).json()
    assert event['delivery'] == 'guarded_buffer' and event['output_tokens'] == 7
    assert event['attempts'][0]['outcome'] == 'success' and event['status'] == 'blocked'
    assert app.state.store.one('SELECT tokens FROM gateway_requests')['tokens'] == 19


def test_redaction_and_observe_chains_preserve_decisions_without_raw_content(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    redactor = rail(client, config={'rule': 'pii', 'pii_types': ['email'], 'action': 'redact'})
    observer = rail(client, name='Observe only')
    profile = gateway(client, [service['id']], guardrails=[
        {'guardrail_id': redactor['id'], 'stages': ['input']},
        {'guardrail_id': observer['id'], 'stages': ['input'], 'mode': 'observe'}])
    _, headers = bound_key(client, profile)
    result = invoke(client, service['name'], headers, content='forbidden private@example.test')
    assert result.status_code == 200
    assert 'private@example.test' not in captured[-1].content.decode()
    assert '[REDACTED]' in captured[-1].content.decode()
    decisions = app.state.store.all('SELECT * FROM guardrail_decisions')
    assert [r['outcome'] for r in decisions] == ['redacted', 'blocked']
    assert 'private@example.test' not in json.dumps(decisions)


def test_gateway_and_guardrail_versions_invalidate_bindings_and_rollback(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    guard = rail(client)
    profile = gateway(client, [service['id']], guardrails=[{'guardrail_id': guard['id']}])
    _, headers = bound_key(client, profile)
    binding = client.post('/protocol/v1/bindings', headers=headers, json={'service': service['name']}).json()
    changed = client.put('/api/v1/guardrails/'+guard['id'], json={'name': guard['name'], 'provider': 'builtin', 'config': {'terms': ['new restriction']}})
    assert changed.status_code == 200
    before = len(captured)
    assert invoke(client, service['name'], headers, binding['invoke']).status_code == 409
    assert len(captured) == before
    assert client.get('/api/v1/getting-started').json()['needs_rebinding'] == 1
    assert client.post('/protocol/v1/bindings', headers=headers, json={'service': service['name']}).status_code == 201
    assert client.get('/api/v1/getting-started').json()['needs_rebinding'] == 0
    revised = client.put('/api/v1/gateways/'+profile['id'], json={'name': profile['name'], 'slug': profile['slug'], 'config': {**profile['config'], 'max_output_tokens': 100}}).json()
    assert revised['version'] == 2
    restored = client.post('/api/v1/gateways/'+profile['id']+'/rollback/1', json={}).json()
    assert restored['version'] == 3 and restored['config']['max_output_tokens'] == 4096
    assert len(client.get('/api/v1/gateways/'+profile['id']+'/versions').json()) == 3


def test_token_admission_is_reserved_and_settled_from_reported_usage(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    profile = gateway(client, [service['id']], limits=[{'metric': 'tokens', 'limit': 100, 'window_seconds': 60}])
    _, headers = bound_key(client, profile)
    before = len(captured)
    blocked = invoke(client, service['name'], headers, max_tokens=10)
    assert blocked.status_code == 429 and len(captured) == before
    profile = client.put('/api/v1/gateways/'+profile['id'], json={'name': profile['name'], 'slug': 'finance', 'config': {**profile['config'], 'limits': [{'metric': 'tokens', 'limit': 10000}]}}).json()
    assert invoke(client, service['name'], headers).status_code == 200
    assert app.state.store.one('SELECT tokens,reservation FROM gateway_requests')['tokens'] == 19
    assert app.state.store.one('SELECT reservation FROM gateway_requests')['reservation'] > 1024


def test_gateway_concurrency_admission_is_atomic(workspace):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    profile = gateway(client, [service['id']], max_concurrency=1)
    _, headers = bound_key(client, profile)
    async def slow(request):
        await asyncio.sleep(.15)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}],
                                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})
    mock = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    app.state.gateway.client = mock
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: invoke(client, service['name'], headers).status_code, range(3)))
    asyncio.run(mock.aclose())
    assert sorted(results) == [200, 429, 429]
    assert app.state.store.one("SELECT COUNT(*) AS n FROM gateway_requests WHERE status='pending'")['n'] == 0


def test_nemo_decision_adapter_and_fail_closed_errors(workspace):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    seen = []
    verdict = {'status': 'passed', 'content': 'Do not persist this provider text'}
    def remote(request):
        seen.append(request)
        if request.url.path == '/v1/checks':
            return httpx.Response(200, json=verdict)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'Safe'}}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 1}})
    mock = httpx.AsyncClient(transport=httpx.MockTransport(remote))
    app.state.gateway.client = mock
    guard = rail(client, provider='nemo', api_key='synthetic-nemo-secret', config={'endpoint': 'https://guard.test/v1/checks', 'config_id': 'company'})
    assert guard['has_secret'] and 'secret' not in guard
    profile = gateway(client, [service['id']], guardrails=[{'guardrail_id': guard['id'], 'stages': ['input', 'output']}])
    _, headers = bound_key(client, profile)
    result = invoke(client, service['name'], headers, stream=True)
    assert result.status_code == 200 and '[DONE]' in result.text and 'Safe' in result.text
    assert result.headers['x-pliqra-delivery'] == 'guarded_buffer'
    checks = [r for r in seen if r.url.path == '/v1/checks']
    assert [json.loads(r.content)['guardrails']['rail_types'] for r in checks] == [['input'], ['output']]
    assert json.loads(checks[0].content)['guardrails']['config_id'] == 'company'
    assert checks[0].headers['authorization'] == 'Bearer synthetic-nemo-secret'
    assert 'synthetic-nemo-secret' not in app.state.store.one('SELECT secret FROM guardrails')['secret']
    verdict['status'] = 'modified'
    assert invoke(client, service['name'], headers).status_code == 503
    assert 'Do not persist' not in json.dumps(app.state.store.all('SELECT * FROM guardrail_decisions'))
    asyncio.run(mock.aclose())


def test_webhook_contract_and_explicit_error_policy(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    guard = rail(client, provider='webhook', config={'endpoint': 'https://offline.test/check'})
    profile = gateway(client, [service['id']], guardrails=[{'guardrail_id': guard['id'], 'on_error': 'block'}])
    _, headers = bound_key(client, profile)
    assert invoke(client, service['name'], headers).status_code == 503
    changed = {**profile['config'], 'guardrails': [{'guardrail_id': guard['id'], 'stages': ['input'], 'mode': 'enforce', 'on_error': 'allow'}]}
    assert client.put('/api/v1/gateways/'+profile['id'], json={'name': profile['name'], 'slug': 'finance', 'config': changed}).status_code == 200
    assert invoke(client, service['name'], headers).status_code == 200
    assert app.state.store.all('SELECT outcome,on_error FROM guardrail_decisions')[-1] == {'outcome': 'error', 'on_error': 'allow'}


def test_gateway_management_and_team_attribution_are_authorized(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    team = client.post('/api/v1/teams', json={'name': 'Finance', 'members': [], 'projects': [{'project_id': 'default', 'role': 'member'}]}).json()
    profile = gateway(client, [service['id']], project_id='default', team_ids=[team['id']])
    assert client.post('/api/v1/keys', json={'name': 'Wrong team', 'gateway_id': profile['id']}).status_code == 422
    token = key(client, gateway_id=profile['id'], team_id=team['id'])
    assert invoke(client, service['name'], {'Authorization': 'Bearer '+token['token']}).status_code == 200
    local_user(client, 'Member'); client.post('/api/v1/logout', json={}); sign_in(client, 'member@example.test')
    assert client.get('/api/v1/gateways').status_code == 403
    assert client.get('/api/v1/guardrails').status_code == 403
    assert client.post('/api/v1/guardrails', json={'name': 'Denied', 'provider': 'builtin', 'config': {'terms': ['x']}}).status_code == 403
    assert invoke(client, service['name'], path='/g/finance/v1/chat/completions').status_code == 403


def test_gateway_preview_has_no_inference_or_quota_side_effects(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    profile = gateway(client, [service['id']])
    before = len(captured)
    result = client.post('/api/v1/gateways/'+profile['id']+'/preview', json={'service': service['name']})
    assert result.status_code == 200 and result.json()['admissible']
    assert len(captured) == before
    assert app.state.store.one('SELECT COUNT(*) AS n FROM gateway_requests')['n'] == 0
    invalid = client.post('/api/v1/guardrails', json={'name': 'Unsafe reference', 'provider': 'builtin', 'config': {'rule': 'json_schema', 'json_schema': {'$ref': 'https://external.test/schema'}}})
    assert invalid.status_code == 422


def test_http_decision_shape_and_output_json_schema_are_enforced(workspace):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    verdict = {'allow': True}
    content = '{"invoice_id":"synthetic-1"}'
    def remote(request):
        if request.url.path == '/check':
            assert json.loads(request.content)['protocol'] == 'pliqra/guardrail-check/1'
            return httpx.Response(200, json=verdict)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': content}}],
                                        'usage': {'prompt_tokens': 1, 'completion_tokens': 2}})
    mock = httpx.AsyncClient(transport=httpx.MockTransport(remote)); app.state.gateway.client = mock
    external = rail(client, provider='webhook', config={'endpoint': 'https://check.test/check'})
    schema = rail(client, name='Invoice structure', config={'rule': 'json_schema', 'json_schema': {
        'type': 'object', 'required': ['invoice_id'], 'properties': {'invoice_id': {'type': 'string'}}}})
    profile = gateway(client, [service['id']], guardrails=[
        {'guardrail_id': external['id'], 'stages': ['input']},
        {'guardrail_id': schema['id'], 'stages': ['output']}])
    _, headers = bound_key(client, profile)
    assert invoke(client, service['name'], headers, stream=True).status_code == 200
    content = '{"other":true}'
    assert invoke(client, service['name'], headers).status_code == 422
    verdict['allow'] = 'true'
    assert invoke(client, service['name'], headers).status_code == 503
    verdict['allow'] = True
    update = client.put('/api/v1/guardrails/'+schema['id'], json={'name': schema['name'], 'provider': 'builtin',
                        'config': {'rule': 'json_schema', 'json_schema': {'$ref': '#/missing'}}})
    assert update.status_code == 200
    assert invoke(client, service['name'], headers).status_code == 503
    assert app.state.store.one("SELECT COUNT(*) AS n FROM gateway_requests WHERE status='pending'")['n'] == 0
    asyncio.run(mock.aclose())


def test_unknown_usage_stays_reserved_until_audited_reconciliation(workspace):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    profile = gateway(client, [service['id']])
    _, headers = bound_key(client, profile)
    mock = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        'choices': [{'message': {'role': 'assistant', 'content': 'Unknown usage'}}]})))
    app.state.gateway.client = mock
    result = invoke(client, service['name'], headers)
    assert result.status_code == 200
    ledger = app.state.store.one('SELECT * FROM gateway_requests')
    assert ledger['tokens'] == ledger['reservation'] > 1024
    reconciled = client.post('/api/v1/events/'+result.headers['x-request-id']+'/reconcile', json={
        'cost_micros': 0, 'input_tokens': 3, 'output_tokens': 2, 'note': 'Synthetic independently reconciled usage'})
    assert reconciled.status_code == 200
    assert app.state.store.one('SELECT tokens FROM gateway_requests')['tokens'] == 5
    asyncio.run(mock.aclose())


def test_user_quota_combines_keys_and_policy_discovery_excludes_secrets(workspace):
    client, app, captured = workspace
    service = connect(client)['services'][0]
    guard = rail(client, provider='webhook', api_key='never-disclose', config={'endpoint': 'https://offline.test/check'})
    profile = gateway(client, [service['id']], limits=[{'metric': 'requests', 'scope': 'user', 'limit': 1, 'window_seconds': 3600}],
                      guardrails=[{'guardrail_id': guard['id'], 'mode': 'observe'}])
    _, first = bound_key(client, profile); _, second = bound_key(client, profile)
    contract = client.get('/protocol/v1/gateway', headers=first).json()['gateway']
    assert contract['fingerprint'] == profile['fingerprint']
    assert 'endpoint' not in json.dumps(contract) and 'never-disclose' not in json.dumps(contract)
    assert invoke(client, service['name'], first).status_code == 200
    assert invoke(client, service['name'], second).status_code == 429
    app.state.store.run('UPDATE gateway_requests SET started=started-3601')
    assert invoke(client, service['name'], second).status_code == 200


def test_version_two_workspace_migrates_without_losing_resources(workspace, tmp_path):
    client, app, _ = workspace
    connect(client)
    directory = tmp_path/'older-workspace'; directory.mkdir()
    previous = sqlite3.connect(directory/'pliqra.sqlite3')
    app.state.store.conn.backup(previous)
    for table in ['gateway_profiles', 'gateway_versions', 'guardrails', 'gateway_requests', 'guardrail_decisions']:
        previous.execute('DROP TABLE '+table)
    for table, columns in [('api_keys',['gateway_id']),('bindings',['gateway_id','gateway_fingerprint']),
                           ('events',['gateway_id','gateway_name','gateway_fingerprint','delivery'])]:
        for column in columns:
            previous.execute('ALTER TABLE '+table+' DROP COLUMN '+column)
    previous.execute('PRAGMA user_version=2'); previous.commit(); previous.close()
    upgraded = Store(directory)
    assert upgraded.one('PRAGMA user_version')['user_version'] == 3
    assert upgraded.one('SELECT COUNT(*) AS n FROM connections')['n'] == 1
    assert upgraded.one('SELECT COUNT(*) AS n FROM gateway_profiles')['n'] == 0
    assert 'gateway_id' in {r['name'] for r in upgraded.all('PRAGMA table_info(api_keys)')}
    upgraded.close()


def test_running_request_keeps_the_guardrail_snapshot_it_was_admitted_with(workspace):
    client, app, _ = workspace
    service = connect(client)['services'][0]
    external = rail(client, provider='webhook', config={'endpoint': 'https://check.test/check'})
    local = rail(client, name='Changing policy')
    profile = gateway(client, [service['id']], guardrails=[
        {'guardrail_id': external['id'], 'stages': ['input']},
        {'guardrail_id': local['id'], 'stages': ['input']}])
    _, headers = bound_key(client, profile)
    changed = False
    def remote(request):
        nonlocal changed
        if request.url.path == '/check':
            if not changed:
                row=app.state.store.one('SELECT * FROM guardrails WHERE id=?',(local['id'],))
                config=json.loads(row['config']);config['terms']=['Hello']
                app.state.store.update('guardrails',local['id'],{'config':json.dumps(config),'version':2})
                changed=True
            return httpx.Response(200, json={'allow': True})
        return httpx.Response(200, json={'choices':[{'message':{'role':'assistant','content':'Safe'}}],
                                        'usage':{'prompt_tokens':1,'completion_tokens':1}})
    mock=httpx.AsyncClient(transport=httpx.MockTransport(remote));app.state.gateway.client=mock
    assert invoke(client,service['name'],headers).status_code == 200
    assert invoke(client,service['name'],headers).status_code == 422
    asyncio.run(mock.aclose())
