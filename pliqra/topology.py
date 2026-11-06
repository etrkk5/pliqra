"""Explicit site placement and runtime observations for a shared-authority topology."""
from typing import Annotated,Literal

from pydantic import BaseModel,ConfigDict,Field,AfterValidator

from .config import env

def site_identifier(value):
    if value in ('shared','unspecified'):raise ValueError('This identifier is reserved for unplaced endpoints')
    return value


SiteID=Annotated[str,Field(pattern=r'^[a-z][a-z0-9-]{0,62}$'),AfterValidator(site_identifier)]


class RuntimeTopology(BaseModel):
    model_config=ConfigDict(frozen=True,extra='forbid')
    site:SiteID='local'
    region:SiteID='local'
    multi_dc:bool=False

    @classmethod
    def from_env(cls):
        return cls(site=env('PLIQRA_SITE','local'),region=env('PLIQRA_REGION','local'),multi_dc=env('PLIQRA_MULTI_DC','0')=='1')


class Placement(BaseModel):
    model_config=ConfigDict(extra='forbid')
    routing:Literal['local_first','local_only','anywhere']='local_first'
    allowed_sites:Annotated[list[SiteID],Field(max_length=50)]=[]
    allowed_regions:Annotated[list[SiteID],Field(max_length=50)]=[]


def placement_reason(deployment,policy,topology):
    site=deployment.get('site') or '';region=deployment.get('region') or ''
    if policy.get('allowed_sites') and (not site or site not in policy['allowed_sites']):return 'Model site is outside the allowed placement boundary'
    if policy.get('allowed_regions') and (not region or region not in policy['allowed_regions']):return 'Model region is outside the allowed placement boundary'
    if policy.get('routing')=='local_only' and site!=topology.site:return 'Local-only service excludes remote and unplaced models'
    return None


def runtime_health(store,site,deployment_id):
    return store.one('SELECT * FROM deployment_health WHERE site=? AND deployment_id=?',(site,deployment_id))


def observe_deployment(store,site,deployment_id,**fields):
    previous=runtime_health(store,site,deployment_id)
    row={'site':site,'deployment_id':deployment_id,'available':1,'failure_count':0,'open_until':0,'updated':store.clock(),**fields}
    if previous:row={**previous,**fields,'updated':store.clock()}
    columns=list(row)
    store.run('INSERT INTO deployment_health ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+') ON CONFLICT(site,deployment_id) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in columns if k not in ('site','deployment_id')),tuple(row[k] for k in columns))


def observe_connection(store,site,connection_id,status,error=None):
    store.run('INSERT INTO connection_health (site,connection_id,status,last_checked,last_error) VALUES (?,?,?,?,?) ON CONFLICT(site,connection_id) DO UPDATE SET status=excluded.status,last_checked=excluded.last_checked,last_error=excluded.last_error',(site,connection_id,status,store.clock(),error))


def model_headers(gateway,deployment):
    return {'X-Pliqra-Site':gateway.cluster.topology.site,'X-Pliqra-Model-Site':deployment.get('site') or 'shared'}


class SiteCreate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:SiteID
    region:SiteID
    mode:Literal['active','draining','standby']='standby'


class SiteMode(BaseModel):
    model_config=ConfigDict(extra='forbid')
    mode:Literal['active','draining','standby']
    version:Annotated[int,Field(ge=1)]


def mount(app):
    from fastapi import Depends,HTTPException
    from .cluster import NODE_TIMEOUT
    store=app.state.store;admin=app.state.context['admin']

    @app.get('/api/v1/sites',tags=['Topology'])
    def sites(p=Depends(admin)):
        instant=store.clock();result=[]
        for site in store.all('SELECT * FROM sites ORDER BY id'):
            nodes=store.all('SELECT id,heartbeat,region FROM runtime_nodes WHERE site=? ORDER BY id',(site['id'],))
            active=store.one("SELECT COUNT(*) AS n FROM runtime_leases l JOIN runtime_nodes n ON n.id=l.node_id WHERE n.site=? AND l.kind='inference'",(site['id'],))['n']
            live=sum(n['heartbeat']>instant-NODE_TIMEOUT for n in nodes)
            result.append({**site,'replicas':nodes,'live_replicas':live,'active_requests':active,'state':'unavailable' if not live else site['mode'],'current':site['id']==app.state.gateway.cluster.topology.site})
        return result

    @app.post('/api/v1/sites',tags=['Topology'],status_code=201)
    def create(body:SiteCreate,p=Depends(admin)):
        with store.transaction():
            if store.one('SELECT COUNT(*) AS n FROM sites')['n']>=50:raise HTTPException(422,'At most 50 sites are supported per workspace')
            row={**body.model_dump(),'version':1,'updated':store.clock()}
            store.insert('sites',row)
            store.audit(p['id'],'site.created',body.id,{'region':body.region,'mode':body.mode})
        return row

    @app.put('/api/v1/sites/{site_id}/mode',tags=['Topology'])
    def change(site_id:str,body:SiteMode,p=Depends(admin)):
        with store.transaction():
            previous=store.one('SELECT * FROM sites WHERE id=?',(site_id,))
            if not previous:raise HTTPException(404,'Site not found')
            if previous['version']!=body.version:raise HTTPException(409,'Site configuration changed; refresh before updating')
            row={'mode':body.mode,'version':previous['version']+1,'updated':store.clock()}
            store.update('sites',site_id,row)
            store.audit(p['id'],'site.mode_changed',site_id,{'before':previous['mode'],'after':body.mode})
        return {**previous,**row}
