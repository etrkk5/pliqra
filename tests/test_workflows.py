import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from pliqra.main import create_app
from tests.test_app import workspace,connect,call
from tests.test_architecture import swap_upstream


def wait_for(client,path):
    deadline=time.monotonic()+6
    while time.monotonic()<deadline:
        response=client.get(path)
        assert response.status_code==200,response.text
        if response.json()['status'] not in ('queued','running'):return response.json()
        time.sleep(.02)
    pytest.fail('Asynchronous operation did not finish')


def test_prompt_versions_are_immutable_and_render_as_literal_data(workspace):
    client,app,captured=workspace;service=connect(client)['services'][0]
    prompt=client.post('/api/v1/prompts',json={'name':'Versioned instructions','messages':[{'role':'system','content':'Hello {{person}}'}]}).json()
    first={'model':service['name'],'prompt':{'id':prompt['id'],'version':1,'variables':{'person':'{{unexpanded}}'}}}
    response=client.post('/v1/chat/completions',json=first)
    assert response.status_code==200,response.text
    assert json.loads(captured[-1].content)['messages'][0]['content']=='Hello {{unexpanded}}'
    assert 'prompt' not in json.loads(captured[-1].content)
    record=app.state.store.one("SELECT prompt_id,prompt_version FROM events WHERE status='success'")
    assert record=={'prompt_id':prompt['id'],'prompt_version':1}
    assert client.post('/api/v1/prompts/'+prompt['id']+'/versions',json={'messages':[{'role':'system','content':'Changed {{person}}'}]}).status_code==201
    assert client.post('/v1/chat/completions',json=first).status_code==200
    assert json.loads(captured[-1].content)['messages'][0]['content']=='Hello {{unexpanded}}'
    missing=client.post('/v1/chat/completions',json={**first,'prompt':{'id':prompt['id'],'version':1,'variables':{}}})
    assert missing.status_code==422


def test_batch_execution_saves_encrypted_results_and_audits_reads(workspace):
    client,app,_=workspace;service=connect(client)['services'][0]
    response=client.post('/api/v1/batches',json={'name':'Batch example','items':[{'custom_id':str(i),'body':{'model':service['name'],'messages':[{'role':'user','content':'Private batch prompt'}]}} for i in range(3)]})
    assert response.status_code==202,response.text
    batch=wait_for(client,'/api/v1/batches/'+response.json()['id'])
    assert batch['status']=='completed' and all(item['status']=='success' for item in batch['items'])
    stored=app.state.store.one('SELECT request,result FROM batch_items')
    assert 'Private batch prompt' not in stored['request'] and 'Hello!' not in stored['result']
    details=client.get('/api/v1/batches/'+batch['id']+'?outputs=true').json()
    assert details['items'][0]['result']['response']['choices'][0]['message']['content']=='Hello!'
    assert app.state.store.one('SELECT action FROM audit ORDER BY created DESC')['action']=='batch.outputs_viewed'
    assert all(row['request_kind']=='batch' for row in app.state.store.all("SELECT request_kind FROM events WHERE status='success'"))


def test_queued_batch_survives_restart_without_replaying_finished_items(tmp_path,monkeypatch):
    from pliqra import batches
    original=batches.worker
    async def paused(_):await asyncio.sleep(3600)
    monkeypatch.setattr(batches,'worker',paused)
    app=create_app(tmp_path)
    with TestClient(app) as client:
        response=client.post('/api/v1/setup',json={'name':'Owner','email':'owner@example.test','password':'long-local-password'})
        client.headers['X-Pliqra-CSRF']=response.json()['csrf']
        mock=swap_upstream(app,lambda _:httpx.Response(200,json={'data':[{'id':'batch-model'}]}))
        service=connect(client)['services'][0]
        batch=client.post('/api/v1/batches',json={'name':'Durable queue','items':[{'body':{'model':service['name'],'messages':[{'role':'user','content':'Synthetic'}]}}]}).json()
        assert batch['status']=='queued'
        asyncio.run(mock.aclose())
    async def start_with_transport(app):
        while not isinstance(app.state.gateway.client._transport,httpx.MockTransport):await asyncio.sleep(.01)
        await original(app)
    monkeypatch.setattr(batches,'worker',start_with_transport)
    restarted=create_app(tmp_path)
    calls=[]
    with TestClient(restarted) as client:
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'Restarted result'}}]})
        mock=swap_upstream(restarted,handler)
        login=client.post('/api/v1/login',json={'email':'owner@example.test','password':'long-local-password'})
        client.headers['X-Pliqra-CSRF']=login.json()['csrf']
        finished=wait_for(client,'/api/v1/batches/'+batch['id'])
        assert finished['status']=='completed' and finished['items'][0]['status']=='success' and len(calls)==1
        asyncio.run(mock.aclose())


def evaluation(client,stable,candidate,judge_id=None):
    dataset=client.post('/api/v1/evaluation-sets',json={'name':'Release cases','cases':[{'name':str(i),'prompt':'Synthetic case','contains':'Hello'} for i in range(3)]}).json()
    response=client.post('/api/v1/evaluations',json={'name':'Candidate quality','dataset_id':dataset['id'],'services':[stable['name'],candidate['name']],'judge_id':judge_id})
    assert response.status_code==202,response.text
    result=wait_for(client,'/api/v1/evaluations/'+response.json()['id'])
    assert result['status']=='completed',result
    return result,dataset


def test_quality_gates_canary_allocation_promotion_and_rollback(workspace):
    client,app,_=workspace
    stable=connect(client)['services'][0];candidate=connect(client)['services'][0]
    evidence,_=evaluation(client,stable,candidate)
    old_binding=client.post('/protocol/v1/bindings',json={'service':stable['name']}).json()
    response=client.post('/api/v1/releases',json={'service':stable['name'],'candidate':candidate['name'],'evaluation_id':evidence['id'],'percent':20})
    assert response.status_code==201,response.text
    release=response.json()
    assert client.post(old_binding['invoke'],json={'model':stable['name'],'messages':[{'role':'user','content':'Synthetic'}]}).status_code==409
    lanes=[]
    for _ in range(100):
        result=call(client,stable['name']);assert result.status_code==200,result.text
        lanes.append(result.headers['X-Pliqra-Lane'])
    assert lanes.count('canary')==20 and lanes.count('stable')==80
    assert client.post('/api/v1/releases/'+release['id']+'/advance',json={'percent':100}).status_code==200
    assert client.post('/api/v1/releases/'+release['id']+'/promote',json={}).status_code==200
    assert app.state.gateway.port(stable['id'])['deployments']==app.state.gateway.port(candidate['id'])['deployments']
    assert client.post('/api/v1/releases/'+release['id']+'/rollback',json={}).status_code==200
    assert app.state.gateway.port(stable['id'])['deployments']!=app.state.gateway.port(candidate['id'])['deployments']


def test_changed_dataset_and_failed_quality_cannot_publish(workspace):
    client,app,_=workspace
    stable=connect(client)['services'][0];candidate=connect(client)['services'][0]
    evidence,dataset=evaluation(client,stable,candidate)
    app.state.store.run('UPDATE evaluations SET results=? WHERE id=?',(json.dumps([{**r,'passed':False} if r['service']==candidate['name'] else r for r in evidence['results']]),evidence['id']))
    body={'service':stable['name'],'candidate':candidate['name'],'evaluation_id':evidence['id']}
    assert client.post('/api/v1/releases',json=body).status_code==409
    client.patch('/api/v1/evaluation-sets/'+dataset['id'],json={'name':'Changed cases','cases':[{'name':'Changed','prompt':'Changed','contains':'Changed'}]})
    assert client.post('/api/v1/releases',json=body).status_code==409
    assert not app.state.store.all('SELECT id FROM releases')


def test_external_judge_score_is_evidence_and_configuration_changes_invalidate_it(workspace):
    client,app,_=workspace
    stable=connect(client)['services'][0];candidate=connect(client)['services'][0]
    judge_config={'name':'HTTP quality judge','provider':'http','endpoint':'https://judge.test/check','rubric':'Return a quality score','api_key':'synthetic-judge-key'}
    judge=client.post('/api/v1/judges',json=judge_config)
    assert judge.status_code==201,judge.text
    def handler(request):
        if request.url.host=='judge.test':return httpx.Response(200,json={'score':.95,'reason':'Synthetic judgement'})
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'Hello!'}}]})
    mock=swap_upstream(app,handler)
    evidence,_=evaluation(client,stable,candidate,judge.json()['id'])
    assert all(r['passed'] and r['judge']['score']==.95 and 'reason' not in r['judge'] for r in evidence['results'])
    assert client.put('/api/v1/judges/'+judge.json()['id'],json={**judge_config,'rubric':'Changed criteria'}).status_code==200
    result=client.post('/api/v1/releases',json={'service':stable['name'],'candidate':candidate['name'],'evaluation_id':evidence['id']})
    assert result.status_code==409
    asyncio.run(mock.aclose())
