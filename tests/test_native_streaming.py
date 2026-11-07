"""Synthetic native providers, including a transport gate proving incremental delivery."""
import asyncio
import json
import threading
import time

import httpx
import pytest

from pliqra.native_streaming import Upstream

from tests.test_app import workspace, connect, call, key
from tests.test_architecture import swap_upstream
from tests.test_live_conformance import gateway_server


def test_responses_multiple_parts_keep_normalized_separators_and_validate_final_tools():
    parser=Upstream('responses','request','service')
    events=responses_events('Hello')
    final={'type':'message','id':'msg_fixture','role':'assistant','content':[{'type':'output_text','text':'Hello'},{'type':'refusal','refusal':'Second'}]}
    events=events[:-2]+[
        {'type':'response.content_part.added','output_index':0,'content_index':1,'part':{'type':'refusal','refusal':''}},
        {'type':'response.refusal.delta','output_index':0,'content_index':1,'delta':'Second'},
        {'type':'response.output_item.done','output_index':0,'item':final},
        {'type':'response.completed','response':{'status':'completed','output':[final],'usage':{'input_tokens':5,'output_tokens':2}}}]
    chunks=[chunk for event in events for chunk in parser.feed(event)]
    assert ''.join(c['choices'][0]['delta'].get('content','') for c in chunks)=='Hello\nSecond'
    assert parser.terminal
    parser=Upstream('responses','request','service')
    events=responses_events(tool=True)
    for event in events[:-1]:parser.feed(event)
    events[-1]['response']['output'][0]={**events[-1]['response']['output'][0],'call_id':'different'}
    with pytest.raises(ValueError,match='Final native calls'):parser.feed(events[-1])


def test_anthropic_multiple_text_blocks_keep_normalized_separators():
    parser=Upstream('anthropic','request','service')
    events=anthropic_events('First')
    events=events[:-2]+[
        {'type':'content_block_start','index':1,'content_block':{'type':'text','text':''}},
        {'type':'content_block_delta','index':1,'delta':{'type':'text_delta','text':'Second'}},
        {'type':'content_block_stop','index':1},*events[-2:]]
    chunks=[chunk for event in events for chunk in parser.feed(event)]
    assert ''.join(c['choices'][0]['delta'].get('content','') for c in chunks)=='First\nSecond'


def test_anthropic_start_usage_is_never_substituted_for_missing_final_usage():
    parser=Upstream('anthropic','request','service')
    events=anthropic_events()
    for event in events[:-2]:parser.feed(event)
    malformed={**events[-2],'usage':{}}
    with pytest.raises(KeyError):parser.feed(malformed)
    assert not parser.terminal


def responses_events(text='Native response', tool=False):
    item = {'type': 'function_call', 'id': 'fc_fixture', 'call_id': 'call_fixture', 'name': 'weather', 'arguments': ''} if tool else {'type': 'message', 'id': 'msg_fixture', 'role': 'assistant', 'content': []}
    events = [{'type': 'response.created', 'response': {'status': 'in_progress'}},
              {'type': 'response.output_item.added', 'output_index': 0, 'item': item}]
    for value in (['{"city":', '"Izmir"}'] if tool else [text[:4], text[4:]]):
        events.append({'type': 'response.function_call_arguments.delta' if tool else 'response.output_text.delta', 'output_index': 0, 'item_id': item['id'], 'content_index': 0, 'delta': value})
    final = {**item, **({'arguments': '{"city":"Izmir"}'} if tool else {'content': [{'type': 'output_text', 'text': text}]})}
    events += [{'type': 'response.output_item.done', 'output_index': 0, 'item': final},
               {'type': 'response.completed', 'response': {'status': 'completed', 'output': [final], 'usage': {'input_tokens': 5, 'output_tokens': 2}}}]
    return events


def anthropic_events(text='Native response', tool=False):
    block = {'type': 'tool_use', 'id': 'call_fixture', 'name': 'weather', 'input': {}} if tool else {'type': 'text', 'text': ''}
    events = [{'type': 'message_start', 'message': {'id': 'msg_fixture', 'usage': {'input_tokens': 5, 'output_tokens': 0}}},
              {'type': 'content_block_start', 'index': 0, 'content_block': block}]
    for value in (['{"city":', '"Izmir"}'] if tool else [text[:4], text[4:]]):
        events.append({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': value} if tool else {'type': 'text_delta', 'text': value}})
    events += [{'type': 'content_block_stop', 'index': 0},
               {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if tool else 'end_turn'}, 'usage': {'output_tokens': 2}},
               {'type': 'message_stop'}]
    return events


class EventStream(httpx.AsyncByteStream):
    def __init__(self, events, gate=None):
        self.events, self.gate, self.closed = events, gate, False

    async def __aiter__(self):
        for index, event in enumerate(self.events):
            if self.gate and index == 3:
                deadline = time.monotonic() + 4
                while not self.gate.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(.01)
                assert self.gate.is_set(), 'First delta was buffered until the provider finished'
            data = ('event: ' + event['type'] + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()
            for offset in range(0, len(data), 11):
                yield data[offset:offset + 11]

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('provider', ['responses', 'anthropic'])
@pytest.mark.parametrize('interface', ['responses', 'anthropic'])
@pytest.mark.parametrize('tool', [False, True])
def test_native_streaming_translates_text_tools_and_usage(workspace, provider, interface, tool):
    client, app, _ = workspace
    service = connect(client, endpoint='https://native.test/v1', provider=provider, model='synthetic')['services'][0]
    deployment = app.state.store.one('SELECT id FROM deployments')['id']
    client.patch('/api/v1/deployments/' + deployment, json={'capabilities': ['chat', 'streaming', 'tools']})
    stream = EventStream((responses_events if provider == 'responses' else anthropic_events)('Hello 🌍', tool))
    captured = []

    def upstream(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, stream=stream, headers={'content-type': 'text/event-stream'})

    mock = swap_upstream(app, upstream)
    body = {'model': service['name'], 'stream': True, **({'input': 'Synthetic'} if interface == 'responses' else {'messages': [{'role': 'user', 'content': 'Synthetic'}], 'max_tokens': 32})}
    if tool:
        body['tools'] = [{'type': 'function', 'name': 'weather', 'parameters': {'type': 'object'}}] if interface == 'responses' else [{'name': 'weather', 'input_schema': {'type': 'object'}}]
    response = client.post('/v1/' + ('responses' if interface == 'responses' else 'messages'), json=body)
    assert response.status_code == 200, response.text
    assert response.headers['X-Pliqra-Delivery'] == interface + '_live'
    assert captured[0]['stream'] is True and stream.closed
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
    assert not any(e['type'] == 'error' for e in events), events
    assert events[-1]['type'] == ('response.completed' if interface == 'responses' else 'message_stop')
    if tool:
        parts = [e.get('delta') for e in events if e['type'] == 'response.function_call_arguments.delta'] if interface == 'responses' else [e['delta']['partial_json'] for e in events if e['type'] == 'content_block_delta']
        assert json.loads(''.join(parts)) == {'city': 'Izmir'}
    else:
        parts = [e['delta'] for e in events if e['type'] == 'response.output_text.delta'] if interface == 'responses' else [e['delta']['text'] for e in events if e['type'] == 'content_block_delta']
        assert ''.join(parts) == 'Hello 🌍'
    row = app.state.store.one('SELECT * FROM events WHERE id=?', (response.headers['X-Request-ID'],))
    assert row['status'] == 'success' and row['input_tokens'] == 5 and row['output_tokens'] == 2
    assert not app.state.store.one('SELECT id FROM runtime_leases')
    asyncio.run(mock.aclose())


@pytest.mark.parametrize('provider', ['responses', 'anthropic'])
def test_native_missing_terminal_or_provider_error_never_completes(workspace, provider):
    client, app, _ = workspace
    service = connect(client, endpoint='https://native.test/v1', provider=provider, model='synthetic')['services'][0]
    events = (responses_events if provider == 'responses' else anthropic_events)()[:-1]
    mock = swap_upstream(app, lambda _: httpx.Response(200, stream=EventStream(events)))
    response = client.post('/v1/responses', json={'model': service['name'], 'input': 'Synthetic', 'stream': True})
    assert 'event: error\n' in response.text and 'event: response.completed\n' not in response.text
    assert app.state.store.one('SELECT status FROM events WHERE id=?', (response.headers['X-Request-ID'],))['status'] == 'incomplete'
    asyncio.run(mock.aclose())


@pytest.mark.parametrize('provider', ['responses', 'anthropic'])
def test_native_first_text_arrives_before_provider_can_finish_over_real_http(workspace, provider):
    client, app, _ = workspace
    service = connect(client, endpoint='https://native.test/v1', provider=provider, model='synthetic')['services'][0]
    token = key(client, rpm=100)['token']
    gate = threading.Event()
    mock = swap_upstream(app, lambda _: httpx.Response(200, stream=EventStream((responses_events if provider == 'responses' else anthropic_events)(), gate)))
    with gateway_server(app) as url, httpx.Client(timeout=6) as external:
        with external.stream('POST', url + '/v1/responses', headers={'Authorization': 'Bearer ' + token}, json={'model': service['name'], 'input': 'Synthetic', 'stream': True}) as response:
            assert response.status_code == 200
            lines=response.iter_lines()
            for line in lines:
                if line.startswith('data:') and json.loads(line[5:])['type'] == 'response.output_text.delta':
                    assert not gate.is_set()
                    assert app.state.store.one("SELECT status FROM events ORDER BY started DESC LIMIT 1")['status'] == 'pending'
                    gate.set()
                    break
            assert 'response.completed' in '\n'.join(lines)
    asyncio.run(mock.aclose())


def test_native_client_disconnect_closes_provider_and_releases_admission_over_real_http(workspace):
    client, app, _ = workspace
    service = connect(client, endpoint='https://native.test/v1', provider='anthropic', model='synthetic')['services'][0]
    token = key(client, rpm=100)['token']
    gate = threading.Event()
    stream = EventStream(anthropic_events(), gate)
    mock = swap_upstream(app, lambda _: httpx.Response(200, stream=stream))
    with gateway_server(app) as url, httpx.Client(timeout=6) as external:
        with external.stream('POST', url + '/v1/responses', headers={'Authorization': 'Bearer ' + token}, json={'model': service['name'], 'input': 'Synthetic', 'stream': True}) as response:
            request_id = response.headers['X-Request-ID']
            for line in response.iter_lines():
                if line.startswith('data:') and json.loads(line[5:])['type'] == 'response.output_text.delta':
                    break
        deadline = time.monotonic() + 3
        while (not stream.closed or app.state.store.one('SELECT id FROM runtime_leases')) and time.monotonic() < deadline:
            time.sleep(.01)
        assert stream.closed
        assert app.state.store.one('SELECT status FROM events WHERE id=?', (request_id,))['status'] == 'cancelled'
        assert not app.state.store.one('SELECT id FROM runtime_leases')
    asyncio.run(mock.aclose())
