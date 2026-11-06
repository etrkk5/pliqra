import asyncio
import json

import httpx
import pytest

from tests.test_app import workspace,connect,call
from tests.test_architecture import swap_upstream
from tests.test_fabric import rail,gateway,bound_key


def test_anthropic_upstream_adapter_translates_tools_and_authentication(workspace):
    client,app,_=workspace
    service=connect(client,endpoint='https://claude.test/v1',provider='anthropic',model='synthetic-claude')['services'][0]
    captured=[]
    def handler(request):
        captured.append(request)
        return httpx.Response(200,json={'content':[{'type':'text','text':'Native response'}],'stop_reason':'end_turn','usage':{'input_tokens':5,'output_tokens':2}})
    mock=swap_upstream(app,handler)
    result=call(client,service['name'],max_tokens=64)
    assert result.status_code==200,result.text
    assert result.json()['choices'][0]['message']['content']=='Native response'
    assert captured[0].url.path=='/v1/messages'
    assert captured[0].headers['x-api-key']=='provider-secret-value'
    assert captured[0].headers['anthropic-version']=='2023-06-01'
    assert json.loads(captured[0].content)['max_tokens']==64
    asyncio.run(mock.aclose())


def test_responses_upstream_uses_stateless_bounded_buffer_for_chat_stream(workspace):
    client,app,_=workspace
    service=connect(client,endpoint='https://responses.test/v1',provider='responses',model='synthetic-response')['services'][0]
    captured=[]
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'Native response'}]}],'usage':{'input_tokens':5,'output_tokens':2}})
    mock=swap_upstream(app,handler)
    result=call(client,service['name'],stream=True,max_tokens=64)
    assert result.status_code==200,result.text
    assert '[DONE]' in result.text and 'Native response' in result.text
    assert captured[0]['store'] is False and captured[0]['stream'] is False
    assert captured[0]['max_output_tokens']==64
    assert result.headers['X-Pliqra-Delivery']=='adapter_buffer'
    assert 'streaming' not in json.loads(app.state.store.one('SELECT verified FROM deployments')['verified'])
    asyncio.run(mock.aclose())


@pytest.mark.parametrize('path,extra',[('/v1/responses',{'input':'Synthetic'}),('/v1/messages',{'messages':[{'role':'user','content':'Synthetic'}],'max_tokens':64}),('/api/chat',{'messages':[{'role':'user','content':'Synthetic'}]}),('/api/generate',{'prompt':'Synthetic'})])
@pytest.mark.parametrize('stream',[False,True])
def test_native_entry_points_cannot_bypass_output_checks_or_shared_gateway_limits(workspace,path,extra,stream):
    client,app,_=workspace;service=connect(client)['services'][0]
    guard=rail(client,config={'rule':'pii','pii_types':['email'],'action':'redact'})
    profile=gateway(client,[service['id']],guardrails=[{'guardrail_id':guard['id'],'stages':['output']}],limits=[{'metric':'requests','scope':'gateway','window_seconds':60,'limit':1}])
    _,auth=bound_key(client,profile)
    mock=swap_upstream(app,lambda _:httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'Contact private@example.test'}}],'provider_debug':'private@example.test'}))
    result=client.post(path,headers=auth,json={'model':service['name'],'stream':stream,**extra})
    assert result.status_code==200,result.text
    assert '[REDACTED]' in result.text and 'private@example.test' not in result.text
    second=client.post('/v1/chat/completions',headers=auth,json={'model':service['name'],'messages':[{'role':'user','content':'Another call'}]})
    assert second.status_code==429
    asyncio.run(mock.aclose())


def test_native_unsupported_stateful_controls_are_rejected(workspace):
    client,app,captured=workspace;service=connect(client)['services'][0];before=len(captured)
    response=client.post('/v1/responses',json={'model':service['name'],'input':'Synthetic','previous_response_id':'external-state'})
    assert response.status_code==422 and len(captured)==before
    assert client.post('/v1/responses',json={'model':service['name'],'input':'Synthetic','tools':[{'type':'web_search'}]}).status_code==422
