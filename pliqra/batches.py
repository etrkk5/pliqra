"""Durable, cancellable batch jobs. Started inference is never automatically replayed."""
import asyncio
import json
from typing import Annotated,Literal

from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field

from .access import current_principal
from .db import encode,now,uid
from .inference_schema import ChatInput
from .model_operations import EmbeddingInput,run_operation


class BatchItem(BaseModel):
    model_config=ConfigDict(extra='forbid')
    operation:Literal['chat','embeddings']='chat'
    body:dict
    custom_id:Annotated[str,Field(max_length=100)]=''
    binding_id:str|None=None


class BatchInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:Annotated[str,Field(min_length=1,max_length=100)]
    items:Annotated[list[BatchItem],Field(min_length=1,max_length=500)]
    store_outputs:bool=True


async def execute(app,batch):
    store=app.state.store;vault=app.state.vault;gateway=app.state.gateway
    try:
        while True:
            current=store.one('SELECT * FROM batches WHERE id=?',(batch['id'],))
            if not current or current['cancel_requested']:raise asyncio.CancelledError()
            gateway.cluster.ensure_live()
            if current['expires']<=now():
                store.run("UPDATE batch_items SET status='expired',error='Batch deadline elapsed before invocation' WHERE batch_id=? AND status='queued'",(batch['id'],))
                break
            with store.transaction():
                gateway.cluster.ensure_live()
                ownership=store.one('SELECT node_id FROM batches WHERE id=?',(batch['id'],))
                if not ownership or ownership['node_id']!=gateway.cluster.node:return
                item=store.one("SELECT * FROM batch_items WHERE batch_id=? AND status='queued' ORDER BY position LIMIT 1",(batch['id'],))
                if not item:break
                if item['next_attempt']>now():
                    store.update('batches',batch['id'],{'status':'queued','node_id':None})
                    return
                store.update('batch_items',item['id'],{'status':'running','attempt_count':item['attempt_count']+1})
            try:
                principal=current_principal(store,json.loads(batch['principal']))
                request=json.loads(vault.decrypt(item['request']))
                body=request['body']
                binding=store.one('SELECT * FROM bindings WHERE id=?',(request['binding_id'],)) if request.get('binding_id') else None
                if request.get('binding_id') and not binding:raise HTTPException(403,'Batch binding is missing')
                if item['operation']=='chat':response=await gateway.chat(body,principal,binding=binding,request_kind='batch')
                else:response=await run_operation(gateway,'embeddings',body,principal,binding=binding,request_kind='batch')
                result={'custom_id':request['custom_id'],'response':json.loads(response.body)}
                with store.transaction():
                    gateway.cluster.ensure_live()
                    ownership=store.one('SELECT node_id FROM batches WHERE id=?',(batch['id'],))
                    if not ownership or ownership['node_id']!=gateway.cluster.node:return
                    store.update('batch_items',item['id'],{'status':'success','event_id':response.headers.get('X-Request-ID'),'result':vault.encrypt(encode(result)) if batch['store_outputs'] else None})
            except HTTPException as exc:
                ownership=store.one('SELECT node_id FROM batches WHERE id=?',(batch['id'],))
                if gateway.cluster.fenced or not ownership or ownership['node_id']!=gateway.cluster.node:return
                if exc.status_code==429 and item['attempt_count']<1000:
                    wait=min(3600,max(1,int((exc.headers or {}).get('Retry-After','10'))))
                    store.update('batch_items',item['id'],{'status':'queued','next_attempt':now()+wait,'error':'Waiting for admission capacity'})
                    store.update('batches',batch['id'],{'status':'queued','node_id':None})
                    return
                event_id=exc.detail.get('request_id') if isinstance(exc.detail,dict) else None
                store.update('batch_items',item['id'],{'status':'error','event_id':event_id,'error':'Request failed with HTTP '+str(exc.status_code)})
                if exc.status_code in (401,403):
                    store.update('batches',batch['id'],{'cancel_requested':1})
            except asyncio.CancelledError:
                if not gateway.cluster.fenced:store.update('batch_items',item['id'],{'status':'interrupted' if getattr(app.state,'batch_stopping',False) else 'cancelled','error':'Stopped while running; inspect request accounting before retrying'})
                raise
            except Exception:
                store.update('batch_items',item['id'],{'status':'error','error':'Batch item failed; no automatic replay was performed'})
        with store.transaction():
            gateway.cluster.ensure_live()
            current=store.one('SELECT node_id FROM batches WHERE id=?',(batch['id'],))
            if current and current['node_id']==gateway.cluster.node:store.update('batches',batch['id'],{'status':'completed','finished':now()})
    except asyncio.CancelledError:
        current=store.one('SELECT * FROM batches WHERE id=?',(batch['id'],))
        if gateway.cluster.fenced or not current or current['node_id']!=gateway.cluster.node:raise
        if getattr(app.state,'batch_stopping',False) and current and not current['cancel_requested']:
            store.update('batches',batch['id'],{'status':'queued','node_id':None})
            raise
        store.run("UPDATE batch_items SET status='cancelled',error='Batch cancelled' WHERE batch_id=? AND status='queued'",(batch['id'],))
        store.update('batches',batch['id'],{'status':'cancelled','finished':now()})
        raise
    except Exception:
        ownership=store.one('SELECT node_id FROM batches WHERE id=?',(batch['id'],))
        if gateway.cluster.fenced or not ownership or ownership['node_id']!=gateway.cluster.node:return
        store.run("UPDATE batch_items SET status='interrupted',error='Batch worker stopped; inspect accounting before retrying' WHERE batch_id=? AND status='running'",(batch['id'],))
        store.update('batches',batch['id'],{'status':'interrupted','finished':now()})


async def worker(app):
    store=app.state.store;tasks={};app.state.batch_tasks=tasks;app.state.batch_stopping=False
    try:
        while True:
            for record_id,task in list(app.state.evaluation_tasks.items()):
                row=store.one('SELECT cancel_requested FROM evaluations WHERE id=?',(record_id,))
                if row and row['cancel_requested'] and not task.done():task.cancel()
            for record_id,task in list(tasks.items()):
                if task.done():tasks.pop(record_id,None)
                elif store.one('SELECT cancel_requested FROM batches WHERE id=?',(record_id,))['cancel_requested']:task.cancel()
            if not app.state.gateway.maintenance and not app.state.gateway.cluster.fenced:
                with store.transaction():
                    running=store.one("SELECT COUNT(*) AS n FROM batches WHERE status='running'")['n']
                    if running<store.setting('runtime',{}).get('max_batch_concurrency',2):
                        batch=store.one("SELECT b.* FROM batches b WHERE b.status='queued' AND (b.cancel_requested=1 OR NOT EXISTS(SELECT 1 FROM batch_items i WHERE i.batch_id=b.id AND i.status='queued' AND i.next_attempt>?)) ORDER BY b.created LIMIT 1",(now(),))
                        if batch:store.update('batches',batch['id'],{'status':'running','node_id':app.state.gateway.cluster.node})
                        else:batch=None
                    else:batch=None
                if batch:
                    task=asyncio.create_task(execute(app,batch));tasks[batch['id']]=task
            await asyncio.sleep(.5)
    finally:
        app.state.batch_stopping=True
        for task in tasks.values():task.cancel()
        await asyncio.gather(*tasks.values(),return_exceptions=True)


def mount(app):
    store=app.state.store;vault=app.state.vault;principal=app.state.context['principal']

    def permitted(record_id,p):
        batch=store.one('SELECT * FROM batches WHERE id=?',(record_id,))
        if not batch or p['role']!='admin' and batch['owner']!=p['id']:raise HTTPException(404,'Batch not found')
        return batch

    def public(batch,outputs=False):
        items=store.all('SELECT id,position,operation,status,event_id,error,attempt_count,result FROM batch_items WHERE batch_id=? ORDER BY position',(batch['id'],))
        return {k:v for k,v in batch.items() if k!='principal'}|{'items':[{k:v for k,v in item.items() if k!='result'}|({'result':json.loads(vault.decrypt(item['result']))} if outputs and item['result'] else {}) for item in items]}

    @app.get('/api/v1/batches',tags=['Batches'])
    def batches(p=Depends(principal)):
        return [public(row) for row in store.all('SELECT * FROM batches'+('' if p['role']=='admin' else ' WHERE owner=?')+' ORDER BY created DESC LIMIT 100',() if p['role']=='admin' else (p['id'],))]

    @app.post('/api/v1/batches',tags=['Batches'],status_code=202)
    def create(body:BatchInput,p=Depends(principal)):
        items=[]
        for item in body.items:
            schema=ChatInput if item.operation=='chat' else EmbeddingInput
            document=schema.model_validate(item.body).model_dump(exclude_none=True)
            if document.get('stream'):raise HTTPException(422,'Batch items use non-streaming delivery')
            port=app.state.gateway.port(document['model'])
            app.state.gateway.authorize_port(p,port)
            if item.binding_id:
                binding=store.one('SELECT * FROM bindings WHERE id=? AND principal=? AND expires>?',(item.binding_id,p['id'],now()))
                if not binding:raise HTTPException(403,'Batch binding is missing or expired')
            items.append({'operation':item.operation,'body':document,'custom_id':item.custom_id,'binding_id':item.binding_id})
        row={'id':uid('batch_'),'name':body.name,'owner':p['id'],'principal':encode({k:p[k] for k in ('id','kind','role')}),
             'status':'queued','store_outputs':int(body.store_outputs),'created':now(),'expires':now()+86400}
        with store.transaction():
            if store.one("SELECT COUNT(*) AS n FROM batches WHERE status IN ('queued','running')")['n']>=100:raise HTTPException(429,'Batch queue is full')
            store.insert('batches',row)
            for index,item in enumerate(items):
                store.insert('batch_items',{'id':uid('item_'),'batch_id':row['id'],'position':index,'operation':item['operation'],'request':vault.encrypt(encode(item)),'status':'queued'})
            store.audit(p['id'],'batch.created',row['id'],{'items':len(items),'outputs_saved':body.store_outputs})
        return public(row)

    @app.get('/api/v1/batches/{record_id}',tags=['Batches'])
    def detail(record_id:str,outputs:bool=False,p=Depends(principal)):
        batch=permitted(record_id,p)
        if outputs:store.audit(p['id'],'batch.outputs_viewed',record_id)
        return public(batch,outputs)

    @app.post('/api/v1/batches/{record_id}/cancel',tags=['Batches'])
    def cancel(record_id:str,p=Depends(principal)):
        batch=permitted(record_id,p)
        if batch['status'] not in ('queued','running'):raise HTTPException(409,'Batch has already finished')
        store.update('batches',record_id,{'cancel_requested':1})
        store.audit(p['id'],'batch.cancel_requested',record_id)
        return {'cancel_requested':True}

    @app.delete('/api/v1/batches/{record_id}',tags=['Batches'])
    def delete(record_id:str,p=Depends(principal)):
        batch=permitted(record_id,p)
        if batch['status'] in ('queued','running'):raise HTTPException(409,'Cancel the batch and wait for it to finish first')
        with store.transaction():
            store.run('DELETE FROM batch_items WHERE batch_id=?',(record_id,))
            store.run('DELETE FROM batches WHERE id=?',(record_id,))
            store.audit(p['id'],'batch.deleted',record_id)
        return {'deleted':True}
