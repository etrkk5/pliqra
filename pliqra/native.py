"""Stateless native message interfaces over the same policy boundary."""
import json
import time
from typing import Literal

from fastapi import Depends,HTTPException,Request
from fastapi.responses import JSONResponse,StreamingResponse
from pydantic import BaseModel,ConfigDict,Field

from .connectors import strict_json,sse,ollama_result
from .inference_schema import ChatInput


def chat_messages_from_anthropic(messages,system=None):
    result=[]
    if system:
        text=system if isinstance(system,str) else '\n'.join(p['text'] for p in system if p.get('type')=='text')
        result.append({'role':'system','content':text})
    for message in messages:
        content=message.get('content','')
        if isinstance(content,str):result.append({'role':message['role'],'content':content});continue
        text=[];calls=[]
        for part in content:
            kind=part.get('type')
            if kind=='text':text.append({'type':'text','text':part['text']})
            elif kind=='image':
                source=part['source']
                url='data:'+source['media_type']+';base64,'+source['data'] if source['type']=='base64' else source['url']
                text.append({'type':'image_url','image_url':{'url':url}})
            elif kind=='tool_use':calls.append({'id':part['id'],'type':'function','function':{'name':part['name'],'arguments':json.dumps(part['input'],allow_nan=False)}})
            elif kind=='tool_result':result.append({'role':'tool','tool_call_id':part['tool_use_id'],'content':part['content'] if isinstance(part['content'],str) else '\n'.join(p['text'] for p in part['content'] if p.get('type')=='text')})
            else:raise HTTPException(422,'Unsupported native content block')
        if text or calls:result.append({'role':message['role'],'content':text or '',**({'tool_calls':calls} if calls else {})})
    return result


def anthropic_request(body,model):
    unsupported={'frequency_penalty','presence_penalty','logit_bias','logprobs','top_logprobs','seed','parallel_tool_calls'}
    if any(body.get(key) is not None for key in unsupported):raise HTTPException(422,'This control is not supported by the Anthropic adapter')
    system=[];messages=[]
    for message in body['messages']:
        role=message['role'];content=message.get('content') or ''
        if role in ('system','developer'):
            if not isinstance(content,str):raise HTTPException(422,'System content must be text')
            system.append(content);continue
        parts=[]
        if role in ('tool','function'):
            parts=[{'type':'tool_result','tool_use_id':message.get('tool_call_id') or 'legacy_'+message.get('name',''),'content':content}];role='user'
        elif isinstance(content,str):
            if content:parts=[{'type':'text','text':content}]
        else:
            for part in content:
                if part['type']=='text':parts.append(part)
                elif part['type'] in ('image_url','input_image'):
                    image=part['image_url'];url=image['url'] if isinstance(image,dict) else image
                    if url.startswith('data:'):
                        media,data=url[5:].split(';base64,',1);source={'type':'base64','media_type':media,'data':data}
                    else:source={'type':'url','url':url}
                    parts.append({'type':'image','source':source})
                else:raise HTTPException(422,'Unsupported native message part')
        calls=message.get('tool_calls') or []
        if message.get('function_call'):calls=[{'id':'legacy_'+message['function_call']['name'],'function':message['function_call']}]
        for call in calls:
            function=call['function'];arguments=strict_json(function['arguments'])
            if not isinstance(arguments,dict):raise HTTPException(422,'Tool arguments must be objects')
            parts.append({'type':'tool_use','id':call['id'],'name':function['name'],'input':arguments})
        if not parts:parts=[{'type':'text','text':''}]
        if messages and messages[-1]['role']==role:messages[-1]['content']+=parts
        else:messages.append({'role':role,'content':parts})
    result={'model':model,'messages':messages,'max_tokens':body.get('max_completion_tokens',body.get('max_tokens',1024)),'stream':False}
    if system:result['system']='\n'.join(system)
    for field in ('temperature','top_p'):
        if field in body:result[field]=body[field]
    if body.get('stop'):result['stop_sequences']=[body['stop']] if isinstance(body['stop'],str) else body['stop']
    tools=body.get('tools') or [{'type':'function','function':f} for f in body.get('functions',[])]
    if tools:result['tools']=[{'name':t['function']['name'],'description':t['function'].get('description',''),'input_schema':t['function'].get('parameters',{'type':'object'})} for t in tools]
    choice=body.get('tool_choice',body.get('function_call'))
    if choice=='none':result.pop('tools',None)
    elif choice=='required':result['tool_choice']={'type':'any'}
    elif isinstance(choice,dict):result['tool_choice']={'type':'tool','name':choice.get('name') or choice['function']['name']}
    fmt=body.get('response_format') or {}
    if fmt.get('type')=='json_schema':result['output_config']={'format':{'type':'json_schema','schema':fmt['json_schema']['schema']}}
    elif fmt.get('type')=='json_object':raise HTTPException(422,'Use an explicit JSON Schema with the Anthropic adapter')
    return result


def responses_request(body,model):
    items=[]
    for message in body['messages']:
        content=message.get('content')
        if message['role'] in ('tool','function'):
            items.append({'type':'function_call_output','call_id':message.get('tool_call_id') or 'legacy_'+message.get('name',''),'output':content or ''});continue
        if content:
            if isinstance(content,list):
                content=[{'type':'input_text','text':p['text']} if p['type']=='text' else {'type':'input_image','image_url':p['image_url']['url'] if isinstance(p['image_url'],dict) else p['image_url']} for p in content]
            items.append({'role':message['role'],'content':content})
        calls=message.get('tool_calls') or []
        if message.get('function_call'):calls=[{'id':'legacy_'+message['function_call']['name'],'function':message['function_call']}]
        for call in calls:items.append({'type':'function_call','call_id':call['id'],'name':call['function']['name'],'arguments':call['function']['arguments']})
    result={'model':model,'input':items,'store':False,'stream':False,'max_output_tokens':body.get('max_completion_tokens',body.get('max_tokens',1024))}
    for field in ('temperature','top_p','parallel_tool_calls'):
        if field in body:result[field]=body[field]
    tools=body.get('tools') or [{'type':'function','function':f} for f in body.get('functions',[])]
    if tools:result['tools']=[{'type':'function',**t['function']} for t in tools]
    choice=body.get('tool_choice',body.get('function_call'))
    if choice:result['tool_choice']={'type':'function','name':choice.get('name') or choice['function']['name']} if isinstance(choice,dict) else choice
    fmt=body.get('response_format') or {}
    if fmt.get('type')=='json_schema':result['text']={'format':{'type':'json_schema',**fmt['json_schema']}}
    elif fmt.get('type')=='json_object':result['text']={'format':{'type':'json_object'}}
    return result


def provider_result(provider,data,request_id,model):
    text=[];calls=[];reasoning=[]
    if provider=='anthropic':
        for part in data['content']:
            if part['type']=='text':text.append(part['text'])
            elif part['type']=='tool_use':calls.append({'id':part['id'],'type':'function','function':{'name':part['name'],'arguments':json.dumps(part['input'],allow_nan=False)}})
            elif part['type']=='thinking':reasoning.append(part['thinking'])
        usage=data.get('usage') or {};reason='tool_calls' if calls else 'length' if data.get('stop_reason')=='max_tokens' else 'stop'
    else:
        if data.get('status') not in ('completed','incomplete'):raise ValueError('Unfinished native response')
        for item in data['output']:
            if item['type']=='message':
                for part in item['content']:
                    if part['type']=='output_text':text.append(part['text'])
                    elif part['type']=='refusal':text.append(part['refusal'])
            elif item['type']=='function_call':calls.append({'id':item['call_id'],'type':'function','function':{'name':item['name'],'arguments':item['arguments']}})
            elif item['type']=='reasoning':reasoning.extend(p['text'] for p in item.get('summary',[]) if p.get('type')=='summary_text')
        usage=data.get('usage') or {};reason='tool_calls' if calls else 'length' if data['status']=='incomplete' else 'stop'
    result={'id':request_id,'object':'chat.completion','model':model,'choices':[{'index':0,'message':{'role':'assistant','content':'\n'.join(text),**({'tool_calls':calls} if calls else {}),**({'reasoning_content':'\n'.join(reasoning)} if reasoning else {})},'finish_reason':reason}]}
    if all(isinstance(usage.get(k),int) and not isinstance(usage[k],bool) and usage[k]>=0 for k in ('input_tokens','output_tokens')):
        result['usage']={'prompt_tokens':usage['input_tokens'],'completion_tokens':usage['output_tokens'],'total_tokens':usage['input_tokens']+usage['output_tokens']}
    return result


class NativeInput(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    model:str
    stream:bool=False
    temperature:float|None=None
    top_p:float|None=None
    pliqra:dict|None=None


class ResponsesInput(NativeInput):
    input:str|list[dict]
    instructions:str|None=None
    max_output_tokens:int|None=None
    tools:list[dict]|None=None
    tool_choice:str|dict|None=None
    parallel_tool_calls:bool|None=None
    text:dict|None=None
    store:Literal[False]=False


class AnthropicInput(NativeInput):
    messages:list[dict]
    system:str|list[dict]|None=None
    max_tokens:int
    tools:list[dict]|None=None
    tool_choice:dict|None=None
    stop_sequences:list[str]|None=None


class OllamaInput(NativeInput):
    messages:list[dict]
    tools:list[dict]|None=None
    options:dict=Field(default_factory=dict)
    format:str|dict|None=None


class GenerateInput(NativeInput):
    prompt:str
    system:str|None=None
    options:dict=Field(default_factory=dict)
    format:str|dict|None=None


def native_to_chat(body,interface):
    result={k:body[k] for k in ('model','temperature','top_p','pliqra') if body.get(k) is not None}
    result['stream']=False
    if interface=='responses':
        messages=[]
        if body.get('instructions'):messages.append({'role':'system','content':body['instructions']})
        for item in ([{'role':'user','content':body['input']}] if isinstance(body['input'],str) else body['input']):
            kind=item.get('type','message')
            if kind=='function_call_output':messages.append({'role':'tool','tool_call_id':item['call_id'],'content':item['output']})
            elif kind=='function_call':messages.append({'role':'assistant','content':None,'tool_calls':[{'id':item['call_id'],'type':'function','function':{'name':item['name'],'arguments':item['arguments']}}]})
            elif kind=='message':
                content=item['content']
                if isinstance(content,list):content=[{'type':'text','text':p['text']} if p['type'] in ('input_text','output_text') else {'type':'image_url','image_url':{'url':p['image_url']}} if p['type']=='input_image' else (_ for _ in ()).throw(ValueError('Unsupported native input part')) for p in content]
                messages.append({'role':item['role'],'content':content})
            else:raise ValueError('Unsupported Responses input item')
        result['messages']=messages
        if body.get('max_output_tokens') is not None:result['max_tokens']=body['max_output_tokens']
        if body.get('tools'):
            if any(t.get('type')!='function' for t in body['tools']):raise ValueError('Only client function tools are supported')
            result['tools']=[{'type':'function','function':{k:v for k,v in tool.items() if k!='type'}} for tool in body['tools']]
        if body.get('tool_choice'):
            choice=body['tool_choice'];result['tool_choice']={'type':'function','function':{'name':choice['name']}} if isinstance(choice,dict) else choice
        if body.get('parallel_tool_calls') is not None:result['parallel_tool_calls']=body['parallel_tool_calls']
        if body.get('text',{}).get('format'):
            fmt=body['text']['format'];result['response_format']={'type':'json_schema','json_schema':{k:v for k,v in fmt.items() if k!='type'}} if fmt['type']=='json_schema' else fmt
    elif interface=='anthropic':
        result['messages']=chat_messages_from_anthropic(body['messages'],body.get('system'));result['max_tokens']=body['max_tokens']
        if body.get('tools'):result['tools']=[{'type':'function','function':{'name':t['name'],'description':t.get('description',''),'parameters':t['input_schema']}} for t in body['tools']]
        choice=body.get('tool_choice')
        if choice:result['tool_choice']={'type':'function','function':{'name':choice['name']}} if choice['type']=='tool' else 'required' if choice['type']=='any' else choice['type']
        if body.get('stop_sequences'):result['stop']=body['stop_sequences']
    else:
        if interface=='generate':result['messages']=([{'role':'system','content':body['system']}] if body.get('system') else [])+[{'role':'user','content':body['prompt']}]
        else:
            result['messages']=body['messages']
            ids={}
            for position,message in enumerate(result['messages']):
                for index,call in enumerate(message.get('tool_calls') or []):
                    call.setdefault('id','ollama_'+str(position)+'_'+str(index)+'_'+call['function']['name'])
                    ids[call['function']['name']]=call['id']
                    if not isinstance(call['function']['arguments'],str):call['function']['arguments']=json.dumps(call['function']['arguments'],allow_nan=False)
                if message.get('tool_name'):
                    message['name']=message.pop('tool_name')
                    if message['name'] not in ids:raise ValueError('Tool result does not match a preceding call')
                    message['tool_call_id']=ids[message['name']]
            if body.get('tools'):result['tools']=body['tools']
        allowed={'num_predict':'max_tokens','temperature':'temperature','top_p':'top_p','seed':'seed'}
        if set(body.get('options',{}))-set(allowed):raise ValueError('Unsupported native generation option')
        for key,value in body.get('options',{}).items():result[allowed[key]]=value
        if body.get('format'):result['response_format']={'type':'json_object'} if body['format']=='json' else {'type':'json_schema','json_schema':{'name':'response','schema':body['format']}}
    return ChatInput.model_validate(result).model_dump(exclude_none=True)


def native_result(data,interface,request_id,model):
    choice=data['choices'][0];message=choice['message'];usage=data.get('usage') or {}
    if interface=='responses':
        output=[]
        if message.get('content'):output.append({'type':'message','id':'msg_'+request_id,'role':'assistant','status':'completed','content':[{'type':'output_text','text':message['content'],'annotations':[]}]})
        for call in message.get('tool_calls') or []:output.append({'type':'function_call','id':call['id'],'call_id':call['id'],'name':call['function']['name'],'arguments':call['function']['arguments'],'status':'completed'})
        return {'id':request_id,'object':'response','created_at':int(time.time()),'status':'incomplete' if choice.get('finish_reason')=='length' else 'completed','model':model,'output':output,'error':None,'usage':{'input_tokens':usage.get('prompt_tokens'),'output_tokens':usage.get('completion_tokens'),'total_tokens':usage.get('total_tokens')}}
    if interface=='anthropic':
        content=[{'type':'text','text':message['content']}] if message.get('content') else []
        for call in message.get('tool_calls') or []:content.append({'type':'tool_use','id':call['id'],'name':call['function']['name'],'input':strict_json(call['function']['arguments'])})
        return {'id':request_id,'type':'message','role':'assistant','model':model,'content':content,'stop_reason':'tool_use' if message.get('tool_calls') else 'max_tokens' if choice.get('finish_reason')=='length' else 'end_turn','stop_sequence':None,'usage':{'input_tokens':usage.get('prompt_tokens'),'output_tokens':usage.get('completion_tokens')}}
    calls=[{'type':'function','function':{'name':c['function']['name'],'arguments':strict_json(c['function']['arguments'])}} for c in message.get('tool_calls') or []]
    return {'model':model,'done':True,'done_reason':'length' if choice.get('finish_reason')=='length' else 'stop',**({'response':message.get('content') or ''} if interface=='generate' else {'message':{'role':'assistant','content':message.get('content') or '',**({'tool_calls':calls} if calls else {})}}),**({'prompt_eval_count':usage['prompt_tokens'],'eval_count':usage['completion_tokens']} if usage.get('prompt_tokens') is not None and usage.get('completion_tokens') is not None else {})}


def mount(app):
    principal=app.state.context['principal']

    async def invoke(body,interface,p):
        try:normalized=native_to_chat(body,interface)
        except (ValueError,TypeError,KeyError,AttributeError):raise HTTPException(422,'Unsupported or malformed native request')
        response=await app.state.gateway.chat(normalized,p)
        data=json.loads(response.body);request_id=response.headers['X-Request-ID']
        try:result=native_result(data,interface,request_id,normalized['model'])
        except (ValueError,TypeError,KeyError,AttributeError):
            with app.state.store.transaction():
                app.state.store.update('events',request_id,{'status':'error','error':'Provider returned an invalid native result'})
                app.state.store.update('gateway_requests',request_id,{'status':'error'})
            raise HTTPException(502,{'message':'Provider returned an invalid native result','request_id':request_id})
        app.state.store.update('events',request_id,{'delivery':interface+'_buffer' if body.get('stream') else interface,'stream':int(bool(body.get('stream')))})
        headers={**dict(response.headers),'X-Pliqra-Delivery':interface+'_buffer' if body.get('stream') else interface}
        headers.pop('content-length',None);headers.pop('content-type',None)
        if not body.get('stream'):return JSONResponse(result,headers=headers)
        async def chunks():
            if interface in ('ollama','generate'):yield encode_line(result);return
            if interface=='responses':
                yield 'event: response.created\n'+sse({'type':'response.created','sequence_number':0,'response':{**result,'status':'in_progress','output':[],'usage':None}})
                sequence=1
                for index,item in enumerate(result['output']):
                    initial={**item,**({'content':[],'status':'in_progress'} if item['type']=='message' else {'arguments':'','status':'in_progress'})}
                    yield 'event: response.output_item.added\n'+sse({'type':'response.output_item.added','sequence_number':sequence,'output_index':index,'item':initial});sequence+=1
                    if item['type']=='message':
                        part=item['content'][0]
                        yield 'event: response.content_part.added\n'+sse({'type':'response.content_part.added','sequence_number':sequence,'item_id':item['id'],'output_index':index,'content_index':0,'part':{**part,'text':''}});sequence+=1
                        yield 'event: response.output_text.delta\n'+sse({'type':'response.output_text.delta','sequence_number':sequence,'item_id':item['id'],'output_index':index,'content_index':0,'delta':item['content'][0]['text']});sequence+=1
                        yield 'event: response.output_text.done\n'+sse({'type':'response.output_text.done','sequence_number':sequence,'item_id':item['id'],'output_index':index,'content_index':0,'text':part['text']});sequence+=1
                        yield 'event: response.content_part.done\n'+sse({'type':'response.content_part.done','sequence_number':sequence,'item_id':item['id'],'output_index':index,'content_index':0,'part':part});sequence+=1
                    else:
                        yield 'event: response.function_call_arguments.delta\n'+sse({'type':'response.function_call_arguments.delta','sequence_number':sequence,'item_id':item['id'],'output_index':index,'delta':item['arguments']});sequence+=1
                        yield 'event: response.function_call_arguments.done\n'+sse({'type':'response.function_call_arguments.done','sequence_number':sequence,'item_id':item['id'],'output_index':index,'arguments':item['arguments']});sequence+=1
                    yield 'event: response.output_item.done\n'+sse({'type':'response.output_item.done','sequence_number':sequence,'output_index':index,'item':item});sequence+=1
                terminal='response.completed' if result['status']=='completed' else 'response.incomplete'
                yield 'event: '+terminal+'\n'+sse({'type':terminal,'sequence_number':sequence,'response':result})
            else:
                yield 'event: message_start\n'+sse({'type':'message_start','message':{**result,'content':[],'stop_reason':None,'usage':{**result['usage'],'output_tokens':0}}})
                for index,part in enumerate(result['content']):
                    yield 'event: content_block_start\n'+sse({'type':'content_block_start','index':index,'content_block':{**part,**({'text':''} if part['type']=='text' else {'input':{}})}})
                    delta={'type':'text_delta','text':part['text']} if part['type']=='text' else {'type':'input_json_delta','partial_json':json.dumps(part['input'])}
                    yield 'event: content_block_delta\n'+sse({'type':'content_block_delta','index':index,'delta':delta})
                    yield 'event: content_block_stop\n'+sse({'type':'content_block_stop','index':index})
                yield 'event: message_delta\n'+sse({'type':'message_delta','delta':{'stop_reason':result['stop_reason'],'stop_sequence':None},'usage':{'output_tokens':result['usage']['output_tokens']}})
                yield 'event: message_stop\n'+sse({'type':'message_stop'})
        return StreamingResponse(chunks(),media_type='application/x-ndjson' if interface in ('ollama','generate') else 'text/event-stream',headers=headers)

    @app.post('/v1/responses',tags=['Native interfaces'])
    async def responses(body:ResponsesInput,p=Depends(principal)):return await invoke(body.model_dump(exclude_none=True),'responses',p)

    @app.post('/v1/messages',tags=['Native interfaces'])
    async def messages(body:AnthropicInput,p=Depends(principal)):return await invoke(body.model_dump(exclude_none=True),'anthropic',p)

    @app.post('/api/chat',tags=['Native interfaces'])
    async def ollama(body:OllamaInput,p=Depends(principal)):return await invoke(body.model_dump(exclude_none=True),'ollama',p)

    @app.post('/api/generate',tags=['Native interfaces'])
    async def generate(body:GenerateInput,p=Depends(principal)):return await invoke(body.model_dump(exclude_none=True),'generate',p)


def encode_line(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n'
