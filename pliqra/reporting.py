import csv
import io
import json
from typing import Annotated
from fastapi import Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from .access import event_scope, project_ids, service_allowed
from .gateway import decoded_port
from .db import now


def mount(app):
    ctx=app.state.context
    store,user=ctx['store'],ctx['user']

    def filters(days:int=7,search:str='',status:str='',project_id:str='',team_id:str='',user_id:str='',key_id:str='',service_id:str='',model:str='',connection_id:str='',request_kind:str='',start:float|None=None,end:float|None=None,min_latency:float|None=None,max_latency:float|None=None,stream:bool|None=None,unpriced:bool=False):
        return locals()

    def query(p,f):
        scope,args=event_scope(store,p,'e')
        beginning=f['start'] if f['start'] is not None else now()-max(1,min(f['days'],3650))*86400
        if f['start'] is None and f['days']==0:
            first=store.one('SELECT MIN(e.started) AS first FROM events e WHERE '+scope,args)['first']
            beginning=first if first is not None else now()-86400
        ending=f['end'] if f['end'] is not None else now()
        if beginning>=ending:raise HTTPException(422,'The start time must be before the end time')
        where='e.started>=? AND e.started<=? AND '+scope
        args=[beginning,ending,*args]
        for name,column in {'status':'status','project_id':'project_id','team_id':'team_id','user_id':'actor_user_id','key_id':'key_id','service_id':'port_id','model':'model','request_kind':'request_kind'}.items():
            if f[name]=='__unattributed__':where+=' AND e.'+column+' IS NULL'
            elif f[name]:where+=' AND e.'+column+'=?';args.append(f[name])
        if f['connection_id']:
            where+=' AND e.deployment_id IN (SELECT id FROM deployments WHERE connection_id=?)';args.append(f['connection_id'])
        if f['search']:
            where+=' AND (e.model LIKE ? OR e.port_name LIKE ? OR e.id LIKE ? OR e.connection_name LIKE ?)'
            args.extend(['%'+f['search'][:200]+'%']*4)
        for field,operator in [('min_latency','>='),('max_latency','<=')]:
            if f[field] is not None:where+=' AND e.latency_ms'+operator+'?';args.append(f[field])
        if f['stream'] is not None:where+=' AND e.stream=?';args.append(int(f['stream']))
        if f['unpriced']:where+=' AND e.cost_micros IS NULL'
        return where,args,{'start':beginning,'end':ending}

    def aggregate(where,args,group=None):
        totals="COUNT(*) AS requests,COALESCE(SUM(e.status='success'),0) AS successful,COALESCE(SUM(e.status IN ('error','incomplete','interrupted')),0) AS errors,COALESCE(SUM(e.status='pending'),0) AS pending,COALESCE(SUM(e.status='cancelled'),0) AS cancelled,COALESCE(SUM(e.status='rejected'),0) AS rejected,COALESCE(SUM(e.input_tokens),0) AS input_tokens,COALESCE(SUM(e.output_tokens),0) AS output_tokens,COALESCE(SUM(COALESCE(e.cost_micros,e.known_cost_micros)),0) AS cost_micros,COALESCE(SUM(e.cost_micros IS NULL),0) AS unpriced,COALESCE(SUM(e.usage_source IN ('unknown','partial')),0) AS unknown_usage,COALESCE(SUM(e.cache_hit),0) AS cache_hits,AVG(e.latency_ms) AS latency_ms"
        if not group:return store.one('SELECT '+totals+' FROM events e WHERE '+where,args)
        columns={'models':'e.model,e.connection_name','projects':'e.project_id','teams':'e.team_id','users':'e.actor_user_id','keys':'e.key_id','services':'e.port_id,e.port_name'}[group]
        return store.all('SELECT '+columns+','+totals+' FROM events e WHERE '+where+' GROUP BY '+columns+' ORDER BY requests DESC',args)

    @app.get('/api/v1/report-filters',tags=['Usage'])
    def choices(p=Depends(user)):
        allowed=set(project_ids(store,p))
        visible_ports=[decoded_port(r) for r in store.all('SELECT * FROM ports') if service_allowed(store,p,decoded_port(r))]
        teams=store.all('SELECT id,name FROM teams ORDER BY name') if p['role']=='admin' else store.all('SELECT t.id,t.name FROM teams t JOIN team_members tm ON tm.team_id=t.id WHERE tm.user_id=?',(p['id'],))
        users=store.all('SELECT id,name,email FROM users ORDER BY name') if p['role']=='admin' else store.all('SELECT id,name,email FROM users WHERE id=?',(p['id'],))
        return {'projects':[r for r in store.all('SELECT id,name FROM projects ORDER BY name') if r['id'] in allowed],'teams':teams,'users':users,'keys':store.all('SELECT id,name FROM api_keys ORDER BY name') if p['role']=='admin' else store.all('SELECT id,name FROM api_keys WHERE created_by=?',(p['id'],)),'services':[{'id':r['id'],'name':r['name']} for r in visible_ports],'connections':store.all('SELECT id,name FROM connections ORDER BY name') if p['role']=='admin' else [],'models':[r['model'] for r in store.all('SELECT DISTINCT model FROM deployments ORDER BY model')] if p['role']=='admin' else []}

    @app.get('/api/v1/events',tags=['Usage'])
    def events(f=Depends(filters),limit:int=50,offset:int=0,p=Depends(user)):
        where,args,_=query(p,f)
        rows=store.all('SELECT e.*,u.name AS actor_name,t.name AS team_name,k.name AS key_name,p.name AS project_name,(SELECT COUNT(*) FROM attempts a WHERE a.event_id=e.id) AS attempt_count FROM events e LEFT JOIN users u ON u.id=e.actor_user_id LEFT JOIN teams t ON t.id=e.team_id LEFT JOIN api_keys k ON k.id=e.key_id LEFT JOIN projects p ON p.id=e.project_id WHERE '+where+' ORDER BY e.started DESC LIMIT ? OFFSET ?',[*args,max(1,min(limit,200)),max(0,offset)])
        return {'items':[{**r,'decisions':json.loads(r['decisions'])} for r in rows],'total':store.one('SELECT COUNT(*) AS n FROM events e WHERE '+where,args)['n']}

    @app.get('/api/v1/events/{record_id}',tags=['Usage'])
    def event(record_id:str,p=Depends(user)):
        scope,args=event_scope(store,p,'e')
        row=store.one('SELECT e.*,u.name AS actor_name,t.name AS team_name,k.name AS key_name,p.name AS project_name FROM events e LEFT JOIN users u ON u.id=e.actor_user_id LEFT JOIN teams t ON t.id=e.team_id LEFT JOIN api_keys k ON k.id=e.key_id LEFT JOIN projects p ON p.id=e.project_id WHERE e.id=? AND '+scope,[record_id,*args])
        if not row:raise HTTPException(404,'Request not found')
        return {**row,'content_available':bool(store.one('SELECT id FROM event_content WHERE id=?',(record_id,))),'decisions':json.loads(row['decisions']),'attempts':store.all('SELECT * FROM attempts WHERE event_id=? ORDER BY started',(record_id,))}

    @app.get('/api/v1/events/{record_id}/content',tags=['Usage'])
    def content(record_id:str,p=Depends(ctx['admin'])):
        row=store.one('SELECT payload FROM event_content WHERE id=?',(record_id,))
        if not row:raise HTTPException(404,'Content was not captured for this request')
        store.audit(p['id'],'request.content_viewed',record_id)
        return json.loads(ctx['vault'].decrypt(row['payload']))

    @app.delete('/api/v1/events/{record_id}/content',tags=['Usage'])
    def delete_content(record_id:str,p=Depends(ctx['admin'])):
        store.run('DELETE FROM event_content WHERE id=?',(record_id,))
        store.audit(p['id'],'request.content_deleted',record_id)
        return {'ok':True}

    @app.get('/api/v1/usage',tags=['Usage'])
    def usage(f=Depends(filters),p=Depends(user)):
        where,args,interval=query(p,f)
        summary=aggregate(where,args)
        measured=store.one('SELECT COUNT(*) AS n FROM events e WHERE '+where+' AND e.latency_ms IS NOT NULL',args)['n']
        for q in (.5,.95,.99):
            row=store.one('SELECT e.latency_ms FROM events e WHERE '+where+' AND e.latency_ms IS NOT NULL ORDER BY e.latency_ms LIMIT 1 OFFSET ?',[*args,int((measured-1)*q)]) if measured else None
            summary[f'p{int(q*100)}_ms']=row['latency_ms'] if row else None
        granularity='%Y-%m-%dT%H:00:00Z' if interval['end']-interval['start']<=8*86400 else '%Y-%m-%dT00:00:00Z'
        series=store.all("SELECT strftime(?,e.started,'unixepoch') AS bucket,COUNT(*) AS requests,SUM(e.status='success') AS successful,SUM(e.status IN ('error','incomplete','interrupted')) AS errors,COALESCE(SUM(e.input_tokens+e.output_tokens),0) AS tokens FROM events e WHERE "+where+' GROUP BY bucket ORDER BY bucket',[granularity,*args])
        project_rows={r['project_id']:r for r in aggregate(where,args,'projects')}
        allowed=set(project_ids(store,p))
        projects=[{**r,**project_rows.get(r['id'],{'requests':0,'cost_micros':0,'unpriced':0})} for r in store.all('SELECT * FROM projects') if r['id'] in allowed]
        names={kind:{r['id']:r['name'] for r in store.all('SELECT id,name FROM '+table)} for kind,table in [('teams','teams'),('users','users'),('keys','api_keys')]}
        rollups={}
        for dimension,column in [('teams','team_id'),('users','actor_user_id'),('keys','key_id')]:
            rollups[dimension]=[{**r,'name':names[dimension].get(r[column],'Unattributed')} for r in aggregate(where,args,dimension)]
        return {'summary':summary,'series':series,'models':aggregate(where,args,'models'),'projects':projects,**rollups,'services':aggregate(where,args,'services'),'range':interval,'updated_at':now(),'days':f['days'],'currency':'USD','cost_basis':'Configured-price estimates. Unknown attempt charges remain explicit.','content_logging':'Service opt-in; disabled by default'}

    @app.get('/api/v1/usage.csv',tags=['Usage'])
    def export(f=Depends(filters),p=Depends(user)):
        where,args,_=query(p,f)
        columns=['id','started','finished','port_name','port_version','model','connection_name','project_id','team_id','actor_user_id','key_id','status','stream','request_kind','input_tokens','output_tokens','usage_source','cost_micros','known_cost_micros','reserved_micros','latency_ms','ttft_ms','reconciled_by','reconciliation_note','error']
        def generate():
            output=io.StringIO();writer=csv.writer(output);writer.writerow(columns)
            yield output.getvalue();offset=0
            while True:
                rows=store.all('SELECT '+','.join('e.'+c for c in columns)+' FROM events e WHERE '+where+' ORDER BY e.started DESC,e.id DESC LIMIT 500 OFFSET ?',[*args,offset])
                if not rows:break
                output.seek(0);output.truncate(0)
                for row in rows:writer.writerow(["'"+value if isinstance(value,str) and value.startswith(('=','+','-','@')) else value for value in (row[c] for c in columns)])
                yield output.getvalue();offset+=len(rows)
        return StreamingResponse(generate(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="pliqra-usage.csv"'})
