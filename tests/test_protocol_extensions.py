from tests.test_app import workspace,connect


def test_extension_negotiation_is_explicit_and_unknown_required_extensions_fail(workspace):
    client,app,_=workspace;service=connect(client)['services'][0]
    metadata=client.get('/.well-known/pliqra').json()
    assert metadata['conformance_profile']=='pliqra-core/1'
    assert 'pliqra.operations/1' in metadata['extensions']
    denied=client.post('/protocol/v1/bindings',json={'service':service['name'],'required_extensions':['unimplemented/1']})
    assert denied.status_code==422
    assert not app.state.store.all('SELECT id FROM bindings')
    accepted=client.post('/protocol/v1/bindings',json={'service':service['name'],'required_extensions':['pliqra.operations/1'],'optional_extensions':['pliqra.prompts/1','unimplemented/1']})
    assert accepted.status_code==201,accepted.text
    assert accepted.json()['extensions']==['pliqra.operations/1','pliqra.prompts/1']
    assert app.state.store.one('SELECT extensions FROM bindings')['extensions']=='["pliqra.operations/1","pliqra.prompts/1"]'
