import pytest
from pliqra.main import create_app
from pliqra.topology import RuntimeTopology,Placement,placement_reason
from tests.test_app import workspace,connect,call


def test_multidc_rejects_sqlite_and_unverified_remote_database_before_connect(tmp_path):
    topology=RuntimeTopology(site='dc-west',region='eu',multi_dc=True)
    with pytest.raises(RuntimeError,match='PostgreSQL'):create_app(tmp_path/'sqlite',database_url='',runtime_topology=topology)
    with pytest.raises(RuntimeError,match='verify-full'):create_app(tmp_path/'pg',database_url='postgresql://user:synthetic@db.example/test',runtime_topology=topology)
    assert not (tmp_path/'pg').exists()


def test_placement_is_explicit_and_unknown_locations_cannot_satisfy_restrictions():
    topology=RuntimeTopology(site='west',region='eu')
    assert placement_reason({'site':'west','region':'eu'},Placement(routing='local_only').model_dump(),topology) is None
    assert placement_reason({'site':'east','region':'us'},Placement(allowed_regions=['eu']).model_dump(),topology)
    assert placement_reason({'site':'','region':''},Placement(routing='local_only').model_dump(),topology)
    assert placement_reason({'site':'','region':''},{'allowed_sites':['shared']},topology)
    assert placement_reason({'site':'','region':''},{'allowed_regions':['unspecified']},topology)
    assert placement_reason({'site':'west','region':''},Placement(allowed_regions=['eu']).model_dump(),topology)


def test_site_modes_versioning_and_placement_contracts(workspace):
    client,app,_=workspace;service=connect(client)['services'][0]
    registered=client.post('/api/v1/sites',json={'id':'remote','region':'eu','mode':'active'})
    assert registered.status_code==201,registered.text
    assert client.get('/api/v1/sites').json()[0]['id']=='local'
    bound=client.post('/protocol/v1/bindings',json={'service':service['name'],'required_extensions':['pliqra.placement/1']}).json()
    assert bound['extensions']==['pliqra.placement/1'] and bound['placement']['routing']=='local_first'
    current=app.state.gateway.port(service['id'])
    updated=client.patch('/api/v1/ports/'+service['id'],json={**current,'placement':{'routing':'local_only'}})
    assert updated.status_code==200,updated.text
    assert client.post(bound['invoke'],json={'model':service['name'],'messages':[{'role':'user','content':'Synthetic'}]}).status_code==409
    # Unplaced/cloud endpoints cannot silently satisfy a local-only service.
    assert call(client,service['name']).status_code==422
    drained=client.put('/api/v1/sites/local/mode',json={'mode':'draining','version':1})
    assert drained.status_code==200,drained.text
    assert client.get('/livez').status_code==200
    assert client.get('/readyz').status_code==503
    assert client.get('/api/v1/sites').status_code==200
    assert client.put('/api/v1/sites/local/mode',json={'mode':'active','version':1}).status_code==409
    assert client.put('/api/v1/sites/local/mode',json={'mode':'active','version':2}).status_code==200
    assert client.get('/readyz').status_code==200
