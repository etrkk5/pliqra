from tests.test_app import workspace


def test_organization_storage_keys_sessions_and_disable_are_isolated(workspace):
    client,app,_=workspace
    created=client.post('/api/v1/organizations',json={'name':'Research','slug':'research','owner_email':'owner@example.test'})
    assert created.status_code==201,created.text
    organization=created.json()
    assert client.get('/org/research/api/v1/session').json()['user'] is None
    assert client.get('/org/research/api/v1/connections').status_code==401
    login=client.post('/org/research/api/v1/login',json={'email':organization['owner_email'],'password':organization['initial_password']})
    assert login.status_code==200,login.text
    csrf=login.json()['csrf']
    child=app.state.organizations.apps[organization['id']]
    assert child.state.vault.key!=app.state.vault.key
    assert child.state.store.directory!=app.state.store.directory
    assert client.get('/org/research/api/v1/session').json()['user']['id']!=client.get('/api/v1/session').json()['user']['id']
    response=client.post('/org/research/api/v1/projects',json={'name':'Private organization project'},headers={'X-Pliqra-CSRF':csrf,'Origin':'http://testserver'})
    assert response.status_code==201,response.text
    assert any(p['name']=='Private organization project' for p in client.get('/org/research/api/v1/projects').json())
    assert all(p['name']!='Private organization project' for p in client.get('/api/v1/projects').json())
    html=client.get('/org/research/').text
    assert 'content="/org/research"' in html and 'src="/org/research/static/app.js"' in html
    assert client.get('/org/research/static/app.js').status_code==200
    disabled=client.patch('/api/v1/organizations/'+organization['id'],json={'enabled':False})
    assert disabled.status_code==200
    assert client.get('/org/research/api/v1/session').status_code==404


def test_organization_routes_reject_traversal_and_uninitialized_access(workspace):
    client,app,_=workspace
    invalid=client.post('/api/v1/organizations',json={'name':'Invalid','slug':'../other','owner_email':'owner@example.test'})
    assert invalid.status_code==422
    assert client.get('/org/missing/api/v1/session').status_code==404
    assert not app.state.store.all('SELECT * FROM organizations')
