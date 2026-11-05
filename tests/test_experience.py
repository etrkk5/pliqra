import json

from tests.test_app import workspace, connect, call
from tests.test_product import local_user, sign_in


def test_journey_uses_retained_service_evidence_and_current_contracts(workspace):
    client, app, _ = workspace
    assert client.get('/api/v1/getting-started').json()['service'] is None
    result = connect(client)
    service = result['services'][0]
    journey = client.get('/api/v1/getting-started').json()
    assert journey['service']['id'] == service['id']
    assert journey['ready'] and not journey['has_successful_call']
    assert journey['current_bindings'] == 0
    assert call(client, service['name']).status_code == 200
    journey = client.get('/api/v1/getting-started', params={'days': 1}).json()
    assert journey['has_successful_call']
    connected = client.post('/api/v1/application-connections', json={'service': service['name'], 'name': 'Invoice app'})
    assert connected.status_code == 201, connected.text
    data = connected.json()
    assert data['key']['token'].startswith('plq_')
    key = app.state.store.one('SELECT * FROM api_keys WHERE id=?', (data['key']['id'],))
    assert json.loads(key['allowed_ports']) == [service['id']]
    assert key['rpm'] == 60 and key['expires']
    assert client.get('/api/v1/getting-started').json()['current_bindings'] == 1
    port = client.get('/api/v1/ports').json()[0]
    assert client.patch('/api/v1/ports/' + port['id'], json={**port, 'description': 'New contract'}).status_code == 200
    journey = client.get('/api/v1/getting-started').json()
    assert journey['needs_rebinding'] == 1 and journey['current_bindings'] == 0
    headers = {'Authorization': 'Bearer ' + data['key']['token']}
    assert client.post(data['binding']['invoke'], headers=headers, json={'model': service['name'], 'messages': [{'role': 'user', 'content': 'test'}]}).status_code == 409
    assert client.post('/protocol/v1/bindings', headers=headers, json={'service': service['name']}).status_code == 201
    assert client.get('/api/v1/getting-started').json()['needs_rebinding'] == 0


def test_application_connection_is_atomic_and_service_scoped(workspace):
    client, app, _ = workspace
    first = connect(client); second = connect(client)
    failed = client.post('/api/v1/application-connections', json={'service': first['services'][0]['name'], 'name': 'Invalid requirements', 'requirements': ['tools']})
    assert failed.status_code == 422
    assert app.state.store.one('SELECT COUNT(*) AS n FROM api_keys')['n'] == 0
    assert app.state.store.one('SELECT COUNT(*) AS n FROM bindings')['n'] == 0
    assert app.state.store.one("SELECT COUNT(*) AS n FROM audit WHERE action='key.created'")['n'] == 0
    connected = client.post('/api/v1/application-connections', json={'service': first['services'][0]['name'], 'name': 'Scoped application'}).json()
    headers = {'Authorization': 'Bearer ' + connected['key']['token']}
    available = client.get('/v1/models', headers=headers).json()['data']
    assert [m['id'] for m in available] == [first['services'][0]['name']]
    assert client.post('/v1/chat/completions', headers=headers, json={'model': second['services'][0]['name'], 'messages': [{'role': 'user', 'content': 'blocked'}]}).status_code == 403
    client.delete('/api/v1/keys/' + connected['key']['id'])
    assert client.get('/api/v1/getting-started').json()['current_bindings'] == 0


def test_getting_started_respects_visibility_and_key_creation_role(workspace):
    client, app, _ = workspace
    result = connect(client)
    private = client.post('/api/v1/projects', json={'name': 'Restricted'}).json()['id']
    port = client.get('/api/v1/ports').json()[0]
    client.patch('/api/v1/ports/' + port['id'], json={**port, 'project_id': private})
    local_user(client, 'Member')
    client.post('/api/v1/logout', json={}); sign_in(client, 'member@example.test')
    assert client.get('/api/v1/getting-started').json()['service'] is None
    assert client.get('/api/v1/getting-started', params={'service_id': port['id']}).status_code == 404
    assert client.post('/api/v1/application-connections', json={'service': result['services'][0]['name'], 'name': 'Unauthorized'}).status_code == 403


def test_application_contract_counts_exclude_removed_project_access(workspace):
    client, app, _ = workspace
    result = connect(client)
    service = result['services'][0]
    created = client.post('/api/v1/application-connections', json={'service': service['name'], 'name': 'Formerly shared application'}).json()
    assert client.get('/api/v1/getting-started').json()['current_bindings'] == 1
    project = client.post('/api/v1/projects', json={'name': 'Now private'}).json()['id']
    port = client.get('/api/v1/ports').json()[0]
    client.patch('/api/v1/ports/' + port['id'], json={**port, 'project_id': project})
    journey = client.get('/api/v1/getting-started').json()
    assert journey['current_bindings'] == 0 and journey['needs_rebinding'] == 0
    headers = {'Authorization': 'Bearer ' + created['key']['token']}
    assert client.get('/v1/models', headers=headers).json()['data'] == []


def test_renamed_or_tightened_service_rejects_stale_binding_before_upstream(workspace):
    client, app, captured = workspace
    result = connect(client)
    service = result['services'][0]
    connected = client.post('/api/v1/application-connections', json={'service': service['name'], 'name': 'Version-aware app'}).json()
    port = client.get('/api/v1/ports').json()[0]
    client.patch('/api/v1/ports/' + port['id'], json={**port, 'name': 'renamed-service', 'max_input_chars': 1})
    before = len(captured)
    response = client.post(connected['binding']['invoke'], headers={'Authorization': 'Bearer ' + connected['key']['token']}, json={'model': service['name'], 'messages': [{'role': 'user', 'content': 'Old application contract'}]})
    assert response.status_code == 409,response.text
    assert len(captured) == before
    assert client.get('/api/v1/getting-started').json()['needs_rebinding'] == 1
