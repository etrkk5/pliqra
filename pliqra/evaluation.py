import asyncio
import json
from typing import Annotated, Literal

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .db import encode, now, uid
from .gateway import decoded_deployment
from .schema_validation import validate_schema_definition


class Case(BaseModel):
    name:Annotated[str,Field(min_length=1,max_length=100)]
    prompt:Annotated[str,Field(min_length=1,max_length=20000)]
    contains:Annotated[str,Field(max_length=2000)]=""
    json_schema:dict|None=None
    max_latency_ms:Annotated[int|None,Field(ge=1,le=600000)]=None

    @field_validator('json_schema')
    @classmethod
    def schema(cls,value):
        if value is None:return value
        return validate_schema_definition(value)


class Dataset(BaseModel):
    name:Annotated[str,Field(min_length=1,max_length=100)]
    cases:Annotated[list[Case],Field(min_length=1,max_length=20)]


class Run(BaseModel):
    name:Annotated[str,Field(min_length=1,max_length=100)]
    dataset_id:str
    services:Annotated[list[str],Field(min_length=1,max_length=10)]
    max_tokens:Annotated[int,Field(ge=16,le=4096)]=256
    store_outputs:bool=False


class Probe(BaseModel):
    capability:Literal['chat','streaming','structured_output','tools']='chat'


def mount(app):
    ctx=app.state.context
    store,admin=ctx['store'],ctx['admin']
    app.state.evaluation_tasks={}

    @app.get('/api/v1/evaluation-sets',tags=['Evaluations'])
    def datasets(p=Depends(admin)):
        return [{**row,'cases':json.loads(row['cases'])} for row in store.all('SELECT * FROM evaluation_sets ORDER BY created DESC')]

    @app.post('/api/v1/evaluation-sets',tags=['Evaluations'],status_code=201)
    def create_dataset(body:Dataset,p=Depends(admin)):
        record_id=uid('dataset_')
        store.insert('evaluation_sets',{'id':record_id,'name':body.name,'cases':encode([case.model_dump() for case in body.cases]),'created':now()})
        store.audit(p['id'],'evaluation_set.created',record_id,{'name':body.name,'case_count':len(body.cases)})
        return {'id':record_id}

    @app.patch('/api/v1/evaluation-sets/{record_id}',tags=['Evaluations'])
    def edit_dataset(record_id:str,body:Dataset,p=Depends(admin)):
        if not store.one('SELECT id FROM evaluation_sets WHERE id=?',(record_id,)):raise HTTPException(404,'Dataset not found')
        store.update('evaluation_sets',record_id,{'name':body.name,'cases':encode([case.model_dump() for case in body.cases])})
        store.audit(p['id'],'evaluation_set.updated',record_id,{'case_count':len(body.cases)})
        return {'ok':True}

    async def result_checks(case,text,event):
        checks=[{'name':'Valid completion','passed':True}]
        if case.get('contains'):checks.append({'name':'Expected text','passed':case['contains'].casefold() in text.casefold()})
        if case.get('json_schema') is not None:
            passed=False
            try:
                value=json.loads(text)
                passed=await app.state.gateway.policies.schemas.validate(case['json_schema'],value)
            except (ValueError,RecursionError,TimeoutError):pass
            checks.append({'name':'JSON Schema','passed':passed})
        if case.get('max_latency_ms'):checks.append({'name':'Latency target','passed':(event.get('latency_ms') or 10**12)<=case['max_latency_ms']})
        return checks

    async def evaluate(record_id,body,principal,cases):
        results=[]
        store.update('evaluations',record_id,{'status':'running'})
        try:
            for service in body.services:
                for case in cases:
                    if app.state.gateway.maintenance:raise RuntimeError('Workspace entered maintenance')
                    result={'service':service,'case':case['name'],'passed':False,'checks':[]}
                    try:
                        response=await app.state.gateway.chat({'model':service,'messages':[{'role':'user','content':case['prompt']}],'max_tokens':body.max_tokens,'temperature':0,'stream':False},principal,request_kind='evaluation')
                        data=json.loads(response.body)
                        request_id=response.headers.get('X-Request-ID')
                        event=store.one('SELECT * FROM events WHERE id=?',(request_id,)) or {}
                        text=data['choices'][0]['message'].get('content') or ''
                        checks=await result_checks(case,text,event)
                        result.update(request_id=request_id,checks=checks,passed=all(check['passed'] for check in checks),latency_ms=event.get('latency_ms'),cost_micros=event.get('cost_micros'),input_tokens=event.get('input_tokens'),output_tokens=event.get('output_tokens'),contract_fingerprint=app.state.gateway.fingerprint(app.state.gateway.port(service)),quality_assertions=bool(case.get('contains') or case.get('json_schema')))
                        if body.store_outputs:result['output']=text[:20000]
                        store.run("UPDATE events SET request_kind='evaluation' WHERE id=?",(request_id,))
                    except HTTPException as exc:result['error']='Request failed with HTTP '+str(exc.status_code)
                    results.append(result)
                    store.update('evaluations',record_id,{'results':encode(results)})
            store.update('evaluations',record_id,{'status':'completed','finished':now()})
        except asyncio.CancelledError:
            store.update('evaluations',record_id,{'status':'cancelled','finished':now(),'results':encode(results)})
            raise
        except Exception:
            store.update('evaluations',record_id,{'status':'error','finished':now(),'error':'Evaluation stopped unexpectedly; inspect request records','results':encode(results)})

    @app.post('/api/v1/evaluations',tags=['Evaluations'],status_code=202)
    async def run_evaluation(body:Run,p=Depends(admin)):
        dataset=store.one('SELECT * FROM evaluation_sets WHERE id=?',(body.dataset_id,))
        if not dataset:raise HTTPException(404,'Dataset not found')
        if store.one("SELECT id FROM evaluations WHERE status IN ('queued','running') LIMIT 1"):raise HTTPException(409,'One evaluation may run at a time')
        for service in body.services:app.state.gateway.port(service)
        record_id=uid('eval_')
        cases=json.loads(dataset['cases'])
        store.insert('evaluations',{'id':record_id,'name':body.name,'dataset_id':body.dataset_id,'services':encode(body.services),'status':'queued','created':now(),'actor':p['id'],'max_tokens':body.max_tokens,'store_outputs':int(body.store_outputs)})
        # Schedule on the application's loop even though management functions execute in a worker thread.
        def launch():
            task=asyncio.create_task(evaluate(record_id,body,p,cases))
            app.state.jobs.add(task)
            app.state.evaluation_tasks[record_id]=task
            task.add_done_callback(app.state.jobs.discard)
            task.add_done_callback(lambda _:app.state.evaluation_tasks.pop(record_id,None))
        app.state.loop.call_soon_threadsafe(launch)
        store.audit(p['id'],'evaluation.started',record_id,{'services':body.services,'case_count':len(cases),'outputs_saved':body.store_outputs})
        return {'id':record_id,'request_count':len(cases)*len(body.services)}

    @app.get('/api/v1/evaluations',tags=['Evaluations'])
    def evaluations(p=Depends(admin)):
        return [{**row,'services':json.loads(row['services']),'results':[{k:v for k,v in result.items() if k!='output'} for result in json.loads(row['results'])]} for row in store.all('SELECT * FROM evaluations ORDER BY created DESC LIMIT 100')]

    @app.get('/api/v1/evaluations/{record_id}',tags=['Evaluations'])
    def evaluation(record_id:str,p=Depends(admin)):
        row=store.one('SELECT * FROM evaluations WHERE id=?',(record_id,))
        if not row:raise HTTPException(404,'Evaluation not found')
        return {**row,'services':json.loads(row['services']),'results':json.loads(row['results'])}

    @app.delete('/api/v1/evaluations/{record_id}',tags=['Evaluations'])
    def delete_evaluation(record_id:str,p=Depends(admin)):
        row=store.one('SELECT status FROM evaluations WHERE id=?',(record_id,))
        if not row:raise HTTPException(404,'Evaluation not found')
        if row['status'] in ('queued','running'):raise HTTPException(409,'Stop the evaluation first')
        store.run('DELETE FROM evaluations WHERE id=?',(record_id,))
        store.audit(p['id'],'evaluation.deleted',record_id)
        return {'ok':True}

    @app.delete('/api/v1/evaluation-sets/{record_id}',tags=['Evaluations'])
    def delete_dataset(record_id:str,p=Depends(admin)):
        store.run('DELETE FROM evaluation_sets WHERE id=?',(record_id,))
        store.audit(p['id'],'evaluation_set.deleted',record_id)
        return {'ok':True}

    @app.post('/api/v1/evaluations/{record_id}/cancel',tags=['Evaluations'])
    async def cancel_evaluation(record_id:str,p=Depends(admin)):
        task=app.state.evaluation_tasks.get(record_id)
        if task and not task.done():task.cancel()
        else:raise HTTPException(409,'Evaluation is not running')
        store.audit(p['id'],'evaluation.cancelled',record_id)
        return {'ok':True}

    @app.post('/api/v1/deployments/{record_id}/probe',tags=['Evaluations'])
    async def capability_probe(record_id:str,body:Probe,p=Depends(admin)):
        row=store.one('SELECT d.*,c.name AS connection_name,c.endpoint,c.provider,c.secret,c.enabled AS connection_enabled,c.auth_header,c.auth_prefix FROM deployments d JOIN connections c ON c.id=d.connection_id WHERE d.id=?',(record_id,))
        if not row:raise HTTPException(404,'Deployment not found')
        if not row['enabled'] or not row['connection_enabled']:raise HTTPException(409,'Enable the connection and deployment first')
        deployment=decoded_deployment(row)
        port={'id':'probe:'+record_id,'name':'Capability probe','version':1,'strategy':'priority','deployments':[record_id],'requirements':[],'enabled':True,'cache_ttl':0}
        payload={'model':row['model'],'messages':[{'role':'user','content':'Reply with the single word OK.'}],'max_tokens':96,'temperature':0,'stream':body.capability=='streaming'}
        if body.capability=='structured_output':
            payload['messages']=[{'role':'user','content':'Return a JSON object with ok equal to true.'}]
            payload['response_format']={'type':'json_schema','json_schema':{'name':'pliqra_probe','strict':True,'schema':{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}}}
        if body.capability=='tools':
            payload['messages']=[{'role':'user','content':'Call pliqra_probe with value equal to ok.'}]
            payload['tools']=[{'type':'function','function':{'name':'pliqra_probe','parameters':{'type':'object','properties':{'value':{'type':'string'}},'required':['value']}}}]
            payload['tool_choice']={'type':'function','function':{'name':'pliqra_probe'}}
        passed=False;request_id=None;response=None;detail='Endpoint did not satisfy the capability probe'
        try:
            response=await app.state.gateway.execute(payload,p,port,[deployment],[{'eligible':True,'reason':'Explicit administrator capability probe'}],kind='probe')
            request_id=response.headers.get('X-Request-ID')
            if body.capability=='streaming':
                chunks=[]
                async for chunk in response.body_iterator:
                    chunks.append(chunk.decode() if isinstance(chunk,bytes) else chunk)
                    if sum(map(len,chunks))>100000:raise ValueError('Probe response exceeded its size limit')
                text=''.join(chunks)
                passed='data: [DONE]' in text and 'incomplete_stream' not in text and 'upstream_stream_error' not in text
            else:
                data=json.loads(response.body)
                message=data['choices'][0]['message']
                if body.capability=='structured_output':passed=json.loads(message.get('content',''))=={'ok':True}
                elif body.capability=='tools':
                    calls=message.get('tool_calls') or []
                    passed=any(call.get('function',{}).get('name')=='pliqra_probe' and json.loads(call['function'].get('arguments','{}')).get('value')=='ok' for call in calls)
                else:passed=isinstance(message.get('content'),str) and bool(message['content'].strip())
            detail='Probe passed' if passed else detail
        except (HTTPException,ValueError,KeyError,TypeError):pass
        finally:
            if isinstance(response,StreamingResponse):
                await response.body_iterator.aclose()
        if passed:
            current=store.one('SELECT capabilities FROM deployments WHERE id=?',(record_id,))
            store.update('deployments',record_id,{'capabilities':encode(sorted(set(json.loads(current['capabilities']))|{body.capability}))})
            app.state.gateway.mark_verified(deployment,body.capability)
        else:
            current=store.one('SELECT verified FROM deployments WHERE id=?',(record_id,))
            verified=set(json.loads(current['verified']))-{body.capability}
            store.update('deployments',record_id,{'verified':encode(sorted(verified))})
        probe_id=uid('probe_')
        store.insert('probes',{'id':probe_id,'deployment_id':record_id,'capability':body.capability,'passed':int(passed),'request_id':request_id,'detail':detail,'created':now()})
        store.audit(p['id'],'capability.probed',record_id,{'capability':body.capability,'passed':passed,'request_id':request_id})
        return {'id':probe_id,'passed':passed,'capability':body.capability,'request_id':request_id,'detail':detail}
