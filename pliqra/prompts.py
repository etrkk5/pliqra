"""Immutable prompt versions with literal, bounded variable substitution."""
import hashlib
import json
import re
from typing import Annotated,Literal

from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field

from .access import project_ids,require_project
from .db import encode,now,uid

VARIABLE=re.compile(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,99})\s*\}\}')


class PromptMessage(BaseModel):
    model_config=ConfigDict(extra='forbid')
    role:Literal['system','developer','user','assistant']
    content:Annotated[str,Field(min_length=1,max_length=20000)]


class PromptVersion(BaseModel):
    model_config=ConfigDict(extra='forbid')
    messages:Annotated[list[PromptMessage],Field(min_length=1,max_length=100)]
    note:Annotated[str,Field(max_length=1000)]=''


class PromptCreate(PromptVersion):
    name:Annotated[str,Field(min_length=1,max_length=100)]
    project_id:str='default'


def render(store,principal,selection):
    resource=store.one('SELECT * FROM prompts WHERE id=?',(selection['id'],))
    if not resource:raise HTTPException(404,'Prompt not found')
    require_project(store,principal,resource['project_id'])
    row=store.one('SELECT * FROM prompt_versions WHERE prompt_id=? AND version=?',(selection['id'],selection['version']))
    if not row:raise HTTPException(404,'Prompt version not found')
    document=json.loads(row['document']);variables=selection.get('variables') or {}
    if set(variables)!=set(document['variables']):raise HTTPException(422,{'message':'Prompt variables must match the version exactly','required':document['variables']})
    messages=[{'role':item['role'],'content':VARIABLE.sub(lambda match:variables[match.group(1)],item['content'])} for item in document['messages']]
    if len(encode(messages))>1_000_000:raise HTTPException(422,'Rendered prompt exceeds 1 MB')
    return messages,resource['project_id']


def expand(store,principal,body):
    selection=body.get('prompt')
    if not selection:return body
    messages,project=render(store,principal,selection)
    combined=messages+body.get('messages',[])
    if len(combined)>500:raise HTTPException(422,'Expanded conversation exceeds 500 messages')
    return {**body,'messages':combined,'_prompt_project_id':project}


def mount(app):
    store=app.state.store;admin=app.state.context['admin'];principal=app.state.context['principal']

    def versions(record_id):
        return [{**row,'document':json.loads(row['document'])} for row in store.all('SELECT * FROM prompt_versions WHERE prompt_id=? ORDER BY version DESC',(record_id,))]

    @app.get('/api/v1/prompts',tags=['Prompts'])
    def prompts(p=Depends(principal)):
        allowed=project_ids(store,p)
        return [{**row,'versions':versions(row['id'])} for row in store.all('SELECT * FROM prompts ORDER BY created DESC') if row['project_id'] in allowed]

    def publish(record_id,body,p):
        messages=[item.model_dump() for item in body.messages]
        names=sorted({name for message in messages for name in VARIABLE.findall(message['content'])})
        document={'messages':messages,'variables':names,'note':body.note}
        fingerprint=hashlib.sha256(encode(document).encode()).hexdigest()
        version=store.one('SELECT COALESCE(MAX(version),0) AS n FROM prompt_versions WHERE prompt_id=?',(record_id,))['n']+1
        row={'prompt_id':record_id,'version':version,'document':encode(document),'fingerprint':fingerprint,'actor':p['id'],'created':now()}
        store.insert('prompt_versions',row)
        store.audit(p['id'],'prompt.published',record_id,{'version':version,'fingerprint':fingerprint})
        return {**row,'document':document}

    @app.post('/api/v1/prompts',tags=['Prompts'],status_code=201)
    def create(body:PromptCreate,p=Depends(admin)):
        require_project(store,p,body.project_id)
        resource={'id':uid('prompt_'),'name':body.name,'project_id':body.project_id,'created':now()}
        with store.transaction():
            store.insert('prompts',resource)
            version=publish(resource['id'],body,p)
        return {**resource,'versions':[version]}

    @app.post('/api/v1/prompts/{record_id}/versions',tags=['Prompts'],status_code=201)
    def new_version(record_id:str,body:PromptVersion,p=Depends(admin)):
        if not store.one('SELECT id FROM prompts WHERE id=?',(record_id,)):raise HTTPException(404,'Prompt not found')
        with store.transaction():return publish(record_id,body,p)

    @app.post('/api/v1/prompts/{record_id}/render',tags=['Prompts'])
    def preview(record_id:str,body:dict,p=Depends(principal)):
        from .inference_schema import PromptSelection
        selection=PromptSelection.model_validate({**body,'id':record_id})
        messages,_=render(store,p,selection.model_dump())
        return {'messages':messages,'invoked':False}
