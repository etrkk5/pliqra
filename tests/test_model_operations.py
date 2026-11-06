import asyncio
import base64
import json

import httpx
import pytest

from tests.test_app import workspace,connect
from tests.test_architecture import swap_upstream
from tests.test_fabric import gateway,rail,bound_key,invoke


def capable(client,provider='openai'):
    connection=connect(client,provider=provider)
    deployment=client.get('/api/v1/connections').json()[0]['models'][0]
    response=client.patch('/api/v1/deployments/'+deployment['id'],json={'capabilities':['chat','streaming','tools','embeddings','audio_speech','audio_transcription'],'input_price':2,'output_price':3})
    assert response.status_code==200,response.text
    return connection['services'][0]


def test_embeddings_share_gateway_redaction_limits_and_accounting(workspace):
    client,app,_=workspace;service=capable(client)
    guard=rail(client,config={'rule':'pii','pii_types':['email'],'action':'redact'})
    profile=gateway(client,[service['id']],guardrails=[{'guardrail_id':guard['id'],'stages':['input']}],limits=[{'metric':'requests','scope':'gateway','limit':1,'window_seconds':60}])
    _,auth=bound_key(client,profile)
    captured=[]
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,json={'data':[{'index':0,'embedding':[.1,.2]}],'usage':{'prompt_tokens':5},'model':'unchecked metadata'})
    mock=swap_upstream(app,handler)
    response=client.post('/g/'+profile['slug']+'/v1/embeddings',headers=auth,json={'model':service['name'],'input':'private@example.test'})
    assert response.status_code==200,response.text
    assert captured[0]['input']=='[REDACTED]'
    assert response.json()['model']!='unchecked metadata'
    event=app.state.store.one("SELECT * FROM events WHERE status='success'")
    assert event['input_tokens']==5 and event['output_tokens']==0 and event['delivery']=='embeddings'
    denied=client.post('/v1/embeddings',headers=auth,json={'model':service['name'],'input':'second call'})
    assert denied.status_code==429 and len(captured)==1
    asyncio.run(mock.aclose())


def test_embedding_validation_and_unsupported_output_checks_fail_closed(workspace):
    client,app,_=workspace;service=capable(client)
    mock=swap_upstream(app,lambda _:httpx.Response(200,json={'data':[{'index':0,'embedding':[True]}],'usage':{'prompt_tokens':1}}))
    result=client.post('/v1/embeddings',json={'model':service['name'],'input':'test'})
    assert result.status_code==502
    assert 'embeddings' not in json.loads(app.state.store.one('SELECT verified FROM deployments')['verified'])
    guard=rail(client)
    profile=gateway(client,[service['id']],guardrails=[{'guardrail_id':guard['id'],'stages':['output']}])
    _,auth=bound_key(client,profile)
    assert client.post('/v1/embeddings',headers=auth,json={'model':service['name'],'input':'test'}).status_code==422
    asyncio.run(mock.aclose())


def test_speech_redacts_both_instructions_and_text_without_inventing_usage(workspace):
    client,app,_=workspace;service=capable(client)
    guard=rail(client,config={'rule':'pii','pii_types':['email'],'action':'redact'})
    profile=gateway(client,[service['id']],guardrails=[{'guardrail_id':guard['id'],'stages':['input']}])
    _,auth=bound_key(client,profile);captured=[]
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,content=b'ID3Synthetic audio bytes',headers={'content-type':'audio/mpeg'})
    mock=swap_upstream(app,handler)
    result=client.post('/v1/audio/speech',headers=auth,json={'model':service['name'],'input':'Read a@example.test','instructions':'Style for b@example.test','voice':'alloy'})
    assert result.status_code==200,result.text
    assert result.content==b'ID3Synthetic audio bytes'
    assert '@example.test' not in json.dumps(captured)
    event=app.state.store.one("SELECT * FROM events WHERE status='success'")
    assert event['usage_source']=='unknown' and event['cost_micros'] is None
    assert app.state.store.one('SELECT tokens FROM gateway_requests')['tokens']>=32768
    asyncio.run(mock.aclose())


def test_transcription_multipart_and_output_redaction(workspace):
    client,app,_=workspace;service=capable(client)
    guard=rail(client,config={'rule':'pii','pii_types':['email'],'action':'redact'})
    profile=gateway(client,[service['id']],guardrails=[{'guardrail_id':guard['id'],'stages':['output']}])
    _,auth=bound_key(client,profile)
    mock=swap_upstream(app,lambda request:httpx.Response(200,json={'text':'Contact private@example.test','debug':'private@example.test'}))
    result=client.post('/g/'+profile['slug']+'/v1/audio/transcriptions',headers=auth,data={'model':service['name']},files={'file':('sample.wav',b'RIFF synthetic audio','audio/wav')})
    assert result.status_code==200,result.text
    assert result.json()=={'text':'Contact [REDACTED]'}
    assert 'private@example.test' not in result.text
    asyncio.run(mock.aclose())


def test_bound_embedding_contract_invalidates_after_service_change(workspace):
    client,app,_=workspace;service=capable(client)
    binding=client.post('/protocol/v1/bindings',json={'service':service['name'],'requirements':['embeddings']}).json()
    port=client.get('/api/v1/ports').json()[0]
    client.patch('/api/v1/ports/'+port['id'],json={**port,'max_input_chars':300})
    response=client.post('/protocol/v1/bindings/'+binding['id']+'/operations/embeddings',json={'model':service['name'],'input':'test'})
    assert response.status_code==409,response.text


def test_ollama_tool_results_round_trip_with_ids_and_json_arguments(workspace):
    client,app,_=workspace;service=capable(client,'ollama');captured=[]
    def handler(request):
        payload=json.loads(request.content);captured.append(payload)
        if len(captured)==1:
            return httpx.Response(200,json={'message':{'role':'assistant','content':'','tool_calls':[{'function':{'name':'weather','arguments':{'city':'Istanbul'}}}]},'done':True})
        return httpx.Response(200,json={'message':{'role':'assistant','content':'The tool result was received'},'done':True})
    mock=swap_upstream(app,handler)
    first=invoke(client,service['name'],tools=[{'type':'function','function':{'name':'weather','parameters':{'type':'object'}}}])
    assert first.status_code==200,first.text
    message=first.json()['choices'][0]['message'];call=message['tool_calls'][0]
    assert isinstance(call['function']['arguments'],str) and call['id'].startswith('call_')
    second=client.post('/v1/chat/completions',json={'model':service['name'],'messages':[{'role':'user','content':'weather'},message,{'role':'tool','tool_call_id':call['id'],'content':'22 C'}]})
    assert second.status_code==200,second.text
    assert captured[1]['messages'][-1]['tool_name']=='weather'
    assert captured[1]['messages'][1]['tool_calls'][0]['function']['arguments']=={'city':'Istanbul'}
    asyncio.run(mock.aclose())
