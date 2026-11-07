"""Official client libraries against the synthetic Pliqra lab, with verified fixture TLS."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import ssl

import anthropic
import httpx2
import openai

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('state',type=Path)
args=parser.parse_args()
state=json.loads((args.state/'.secrets.json').read_text())
context=ssl.create_default_context(cafile=str(args.state/'pki/ca.pem'))
results=[]
with openai.OpenAI(api_key=state['key'],base_url='https://127.0.0.1:18091/v1',http_client=httpx2.Client(verify=context,trust_env=False),max_retries=0) as client:
    for service in (state['responses_service'],state['messages_service']):
        text=''.join(event.delta for event in client.responses.create(model=service,input='Synthetic official OpenAI client',stream=True,max_output_tokens=64) if event.type=='response.output_text.delta')
        assert 'Synthetic customer acceptance' in text and '🌍' in text
        results.append({'client':'openai','service':service,'live_text':'passed'})
    events=list(client.responses.create(model=state['responses_service'],input='Use a synthetic weather tool',stream=True,max_output_tokens=64,tools=[{'type':'function','name':'weather','parameters':{'type':'object','properties':{'city':{'type':'string'}},'required':['city']}}]))
    arguments=''.join(event.delta for event in events if event.type=='response.function_call_arguments.delta')
    assert json.loads(arguments)=={'city':'Izmir'}
    assert events[-1].type=='response.completed'
    results.append({'client':'openai','tool_call':'passed'})
with anthropic.Anthropic(api_key=state['key'],base_url='https://127.0.0.1:18091',http_client=httpx2.Client(verify=context,trust_env=False),max_retries=0) as client:
    for service in (state['responses_service'],state['messages_service']):
        with client.messages.stream(model=service,messages=[{'role':'user','content':'Synthetic official Anthropic client'}],max_tokens=64) as stream:
            text=''.join(stream.text_stream)
            final=stream.get_final_message()
        assert 'Synthetic customer acceptance' in text and final.usage.output_tokens==24
        results.append({'client':'anthropic','service':service,'live_text':'passed'})
    with client.messages.stream(model=state['messages_service'],messages=[{'role':'user','content':'Use a synthetic weather tool'}],max_tokens=64,tools=[{'name':'weather','input_schema':{'type':'object','properties':{'city':{'type':'string'}},'required':['city']}}]) as stream:
        final=stream.get_final_message()
    assert final.content[0].type=='tool_use' and final.content[0].input=={'city':'Izmir'}
    results.append({'client':'anthropic','tool_call':'passed'})
report={'synthetic':True,'real_vendor_model_called':False,'official_clients':{name:importlib.metadata.version(name) for name in ('openai','anthropic')},'results':results,'passed':True}
(args.state/'official-clients.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
