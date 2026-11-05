import asyncio
import hashlib
import json
import time
import threading
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .connectors import ollama_result, sse, upstream_request
from .db import encode, now, uid
from .access import service_allowed, require_project


CAPABILITIES = {"chat", "streaming", "tools", "structured_output", "vision"}


def decoded_port(row):
    if not row:
        return None
    return {**row, "deployments": json.loads(row["deployments"]), "requirements": json.loads(row["requirements"]), "enabled": bool(row["enabled"])}


def decoded_deployment(row):
    return {**row, "capabilities": json.loads(row["capabilities"]), "verified": json.loads(row["verified"]), "enabled": bool(row["enabled"])}


def cost_micros(usage, deployment):
    if usage[0] is None or usage[1] is None or deployment["input_price"] is None or deployment["output_price"] is None:
        return None
    amount = Decimal(str(deployment["input_price"])) * usage[0] + Decimal(str(deployment["output_price"])) * usage[1]
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def usage_of(data):
    usage = data.get("usage") or {}
    values = (usage.get("prompt_tokens", usage.get("input_tokens")), usage.get("completion_tokens", usage.get("output_tokens")))
    return tuple(value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None for value in values)


class Gateway:
    def __init__(self, store, vault, client):
        self.store, self.vault, self.client = store, vault, client
        self.active=0
        self.maintenance=False
        self.state_lock=threading.RLock()
        self.generation=0

    def port(self, name):
        value = self.store.one("SELECT * FROM ports WHERE name=? OR id=?", (name, name))
        if not value:
            raise HTTPException(404, "AI service not found")
        return decoded_port(value)

    def candidates(self, port, required=None):
        requirements = set(port["requirements"]) | set(required or [])
        candidates, decisions = [], []
        for deployment_id in port["deployments"]:
            row = self.store.one("SELECT d.*,c.name AS connection_name,c.endpoint,c.provider,c.secret,c.enabled AS connection_enabled,c.auth_header,c.auth_prefix FROM deployments d JOIN connections c ON c.id=d.connection_id WHERE d.id=?", (deployment_id,))
            if not row:
                decisions.append({"deployment_id": deployment_id, "eligible": False, "reason": "Deployment no longer exists"})
                continue
            deployment = decoded_deployment(row)
            evidence=deployment["verified"] if port.get("verified_only") else deployment["capabilities"]
            missing = sorted(requirements - set(evidence))
            disabled=not deployment["enabled"] or not deployment["connection_enabled"]
            unavailable=not deployment["available"] or deployment["open_until"]>now()
            reason = "Deployment or connection is disabled" if disabled else ("Temporarily unavailable; circuit breaker or inventory check" if unavailable else ("Missing required verified capabilities: " + ", ".join(missing) if missing and port.get("verified_only") else "Missing required capabilities: " + ", ".join(missing) if missing else "Meets all required capabilities"))
            eligible = not disabled and not unavailable and not missing and port["enabled"]
            decisions.append({"deployment_id": deployment_id, "model": deployment["model"], "connection": deployment["connection_name"], "eligible": bool(eligible), "reason": reason if port["enabled"] else "AI service is disabled"})
            if eligible:
                candidates.append(deployment)
        if port["strategy"] == "round_robin" and len(candidates) > 1:
            count = self.store.one("SELECT COUNT(*) AS n FROM events WHERE port_id=?", (port["id"],))["n"]
            offset = count % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        return candidates, decisions

    def fingerprint(self, port):
        deployments = []
        for record_id in port["deployments"]:
            row = self.store.one("SELECT d.id,d.model,d.capabilities,d.enabled,d.input_price,d.output_price,d.metadata_hash,c.endpoint,c.provider,c.enabled AS connection_enabled,c.auth_header,c.auth_prefix FROM deployments d JOIN connections c ON c.id=d.connection_id WHERE d.id=?", (record_id,))
            deployments.append(row)
        content = {"name": port["name"], "version": port["version"], "enabled": port["enabled"], "requirements": port["requirements"], "strategy": port["strategy"], "log_content":port.get("log_content",0),"project_id":port.get("project_id"),"verified_only":port.get("verified_only",0),"cache_ttl":port.get("cache_ttl",0),"max_input_chars":port.get("max_input_chars",200000),"max_output_tokens":port.get("max_output_tokens",32768),"deployments": deployments}
        return hashlib.sha256(json.dumps(content,sort_keys=True,separators=(",",":")).encode()).hexdigest()

    def manifest(self, port):
        candidates, decisions = self.candidates(port)
        return {"protocol": "pliqra/0.1-draft", "id": port["id"], "name": port["name"], "description": port["description"], "version": port["version"], "fingerprint": self.fingerprint(port), "requirements": port["requirements"], "invocation": {"interface": "openai-chat-completions", "path": "/v1/chat/completions", "model": port["name"]}, "deployments": [{"model": item["model"], "capabilities": item["capabilities"], "verified": item["verified"], "evidence": "Declared capabilities; verified lists independently tested capabilities"} for item in candidates], "readiness": "ready" if candidates else "unavailable", "decisions": decisions}

    def authorize_port(self, principal, port):
        if not service_allowed(self.store,principal,port):
            raise HTTPException(403,"You are not permitted to use this AI service")

    def begin(self, principal, port, deployment, body, decisions, reservation_prices=None, kind='inference', attempt_limit=1):
        event_id, started = uid('req_'), now()
        key = principal.get('key')
        context=body.get('pliqra') or {}
        if not isinstance(context,dict):raise HTTPException(422,'pliqra must be an object')
        project_id=key['project_id'] if key else port.get('project_id') or context.get('project_id')
        team_id=key.get('team_id') if key else context.get('team_id')
        if project_id and not key:require_project(self.store,principal,project_id)
        if team_id:
            if not project_id or not self.store.one('SELECT team_id FROM team_projects WHERE team_id=? AND project_id=?',(team_id,project_id)):raise HTTPException(403,'Team is not assigned to this project')
            if not key and principal['role']!='admin' and not self.store.one('SELECT user_id FROM team_members WHERE team_id=? AND user_id=?',(team_id,principal['id'])):raise HTTPException(403,'You are not a member of this team')
        reserve=0
        with self.store.transaction():
            if key:
                current=self.store.one('SELECT * FROM api_keys WHERE id=?',(key['id'],))
                if not current or not current['active'] or (current['expires'] and current['expires']<=started):raise HTTPException(401,'Access key has been revoked or expired')
                used=self.store.one("SELECT COUNT(*) AS n FROM events WHERE key_id=? AND started>=? AND status!='rejected'",(key['id'],started-60))['n']
                if used>=current['rpm']:raise HTTPException(429,'Access key request-per-minute limit reached',headers={'Retry-After':'60'})
            if project_id:
                project=self.store.one('SELECT * FROM projects WHERE id=?',(project_id,))
                if not project:raise HTTPException(422,'Project not found')
                pending=self.store.one("SELECT COUNT(*) AS n FROM events WHERE project_id=? AND status='pending'",(project_id,))['n']
                if pending>=project['max_concurrency']:raise HTTPException(429,'Project concurrent request limit reached')
                if project['monthly_budget'] is not None:
                    pricing=reservation_prices or deployment
                    if pricing['input_price'] is None or pricing['output_price'] is None:raise HTTPException(422,'This project has a budget. Configure input and output pricing before using this model.')
                    reserve=cost_micros((len(encode(body).encode())+1024,body.get('max_completion_tokens',body.get('max_tokens',1024))),pricing)*attempt_limit
                    month=datetime.now(timezone.utc).replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()
                    spent=self.store.one("SELECT COALESCE(SUM(CASE WHEN status='pending' THEN reserved_micros ELSE COALESCE(cost_micros,known_cost_micros+reserved_micros) END),0) AS n FROM events WHERE project_id=? AND started>=?",(project_id,month))['n']
                    if spent+reserve>project['monthly_budget']*1_000_000:raise HTTPException(402,"Project budget cannot cover this request's estimated reservation")
            self.store.insert('events',{'id':event_id,'started':started,'principal':principal['id'],'actor_user_id':key.get('created_by') if key else principal['id'],'project_id':project_id,'team_id':team_id,'key_id':key['id'] if key else None,'port_id':port['id'],'port_name':port['name'],'port_version':port['version'],'deployment_id':deployment['id'],'model':deployment['model'],'connection_name':deployment['connection_name'],'status':'pending','stream':int(bool(body.get('stream'))),'reserved_micros':reserve,'decisions':encode(decisions),'request_kind':kind,'price_snapshot':encode({'input_price':deployment['input_price'],'output_price':deployment['output_price']})})
        if port.get('log_content') and kind=='inference':
            raw=encode(body)
            content={'request':body} if len(raw.encode('utf-8'))<=64000 else {'request_excerpt':raw.encode('utf-8')[:64000].decode('utf-8',errors='ignore'),'request_truncated':True}
            self.store.insert('event_content',{'id':event_id,'payload':self.vault.encrypt(encode(content)),'created':now()})
        return event_id,started

    def capture_response(self,event_id,value):
        row=self.store.one('SELECT payload FROM event_content WHERE id=?',(event_id,))
        if not row:return
        content=json.loads(self.vault.decrypt(row['payload']))
        raw=encode(value)
        if len(raw.encode('utf-8'))<=64000:content['response']=value
        else:content.update(response_excerpt=raw.encode('utf-8')[:64000].decode('utf-8',errors='ignore'),response_truncated=True)
        self.store.update('event_content',event_id,{'payload':self.vault.encrypt(encode(content))})

    def finish_attempt(self, attempt_id, deployment, outcome, data=None, http_status=None):
        usage=usage_of(data or {})
        with self.store.transaction():
            self.store.update('attempts',attempt_id,{'finished':now(),'outcome':outcome,'http_status':http_status,'input_tokens':usage[0],'output_tokens':usage[1],'cost_micros':cost_micros(usage,deployment)})
            if outcome=='success':self.store.update('deployments',deployment['id'],{'failure_count':0,'open_until':0})
            elif outcome in ('connection_error','upstream_error','error','incomplete'):
                current=self.store.one('SELECT failure_count FROM deployments WHERE id=?',(deployment['id'],))
                if current:
                    count=current['failure_count']+1
                    self.store.update('deployments',deployment['id'],{'failure_count':count,'open_until':now()+30 if count>=3 else 0})

    def finish(self,event_id,started,deployment,status,data=None,ttft=None,error=None):
        pending=self.store.one("SELECT id FROM attempts WHERE event_id=? AND outcome='pending' ORDER BY started DESC LIMIT 1",(event_id,))
        if pending:self.finish_attempt(pending['id'],deployment,status,data,200 if status=='success' else None)
        attempts=self.store.all('SELECT * FROM attempts WHERE event_id=?',(event_id,))
        complete=bool(attempts) and all(a['input_tokens'] is not None and a['output_tokens'] is not None for a in attempts)
        known=bool(attempts) and any(a['input_tokens'] is not None or a['output_tokens'] is not None for a in attempts)
        all_priced=bool(attempts) and all(a['cost_micros'] is not None for a in attempts)
        known_cost=sum(a['cost_micros'] or 0 for a in attempts)
        row=self.store.one('SELECT reserved_micros FROM events WHERE id=?',(event_id,))
        fields={'status':status,'finished':now(),'latency_ms':(time.perf_counter()-started)*1000,'ttft_ms':ttft,'input_tokens':sum(a['input_tokens'] or 0 for a in attempts) if known else None,'output_tokens':sum(a['output_tokens'] or 0 for a in attempts) if known else None,'usage_source':'reported' if complete else 'partial' if known else 'unknown','cost_micros':known_cost if all_priced else None,'known_cost_micros':known_cost,'reserved_micros':0 if all_priced else max(0,(row['reserved_micros'] if row else 0)-known_cost),'error':error}
        self.store.update('events',event_id,fields)

    def rejected(self,body,principal,error):
        key=principal.get('key')
        self.store.insert('events',{'id':uid('req_'),'started':now(),'finished':now(),'principal':principal['id'],'actor_user_id':key.get('created_by') if key else principal['id'],'key_id':key['id'] if key else None,'project_id':key['project_id'] if key else None,'team_id':key.get('team_id') if key else None,'port_name':str(body.get('model',''))[:256],'status':'rejected','stream':int(bool(body.get('stream'))),'cost_micros':0,'usage_source':'not_invoked','error':encode(error.detail) if isinstance(error.detail,dict) else str(error.detail),'decisions':encode([{'eligible':False,'reason':'Rejected before upstream inference','http_status':error.status_code}])})

    async def chat(self,body,principal,binding=None,request_kind='inference'):
        try:return await self.route(body,principal,binding,request_kind)
        except HTTPException as exc:
            if exc.status_code in (402,403,409,422,429,503):self.rejected(body,principal,exc)
            raise

    async def route(self,body,principal,binding=None,request_kind='inference'):
        if binding and (binding['principal']!=principal['id'] or binding['expires']<=now()):
            raise HTTPException(403,'Binding belongs to another identity or has expired')
        port=self.port(binding['port_id'] if binding else body['model'])
        self.authorize_port(principal,port)
        if binding and (body['model'] not in (port['id'],port['name']) or binding['fingerprint']!=self.fingerprint(port)):
            raise HTTPException(409,'Service contract changed. Discover and bind again; no silent model substitution was performed.')
        if not port['enabled']:raise HTTPException(409,'AI service is disabled')
        if len(encode(body['messages']))>port.get('max_input_chars',200000):raise HTTPException(422,'Input exceeds this service\'s configured character limit')
        requested=body.get('max_completion_tokens',body.get('max_tokens',min(1024,port.get('max_output_tokens',32768))))
        if requested>port.get('max_output_tokens',32768):raise HTTPException(422,'Output token limit exceeds this service\'s policy')
        body={**body}
        if 'max_completion_tokens' not in body:body['max_tokens']=requested
        required={'chat'}
        if body.get('stream'):required.add('streaming')
        if body.get('tools'):required.add('tools')
        if body.get('response_format',{}).get('type') in ('json_object','json_schema'):required.add('structured_output')
        for message in body['messages']:
            if isinstance(message.get('content'),list) and any(part.get('type') in ('image_url','input_image') for part in message['content'] if isinstance(part,dict)):required.add('vision')
        if binding:
            required.update(json.loads(binding['requirements']))
        candidates,decisions=self.candidates(port,required)
        if not candidates:raise HTTPException(422,{'message':'No deployment satisfies the required capabilities','decisions':decisions})
        return await self.execute(body,principal,port,candidates,decisions,request_kind)

    async def execute(self,body,principal,port,candidates,decisions,kind='inference'):
        with self.state_lock:
            if self.maintenance:raise HTTPException(503,'Workspace maintenance is in progress')
            if self.active>=self.store.setting('runtime',{}).get('max_concurrency',64):raise HTTPException(429,'Gateway concurrency limit reached')
            self.active+=1
        released=False
        def release():
            nonlocal released
            with self.state_lock:
                if not released:self.active-=1;released=True
        chosen=candidates[0]
        response=None
        event_id=None
        started=time.perf_counter()
        streaming=False
        cache_id=None
        try:
            if port.get('cache_ttl',0)>0 and not body.get('stream') and not body.get('tools') and kind=='inference':
                cache_id=hashlib.sha256((principal['id']+self.fingerprint(port)+json.dumps(body,sort_keys=True)).encode()).hexdigest()
                cached=self.store.one('SELECT payload FROM cache_entries WHERE id=? AND expires>?',(cache_id,now()))
                if cached:
                    event_id,_=self.begin(principal,port,chosen,body,decisions,{'input_price':0,'output_price':0},kind)
                    data=json.loads(self.vault.decrypt(cached['payload']))
                    data['id']=event_id
                    data['pliqra']={'cache_hit':True,'billable_tokens':0}
                    self.capture_response(event_id,data)
                    self.store.update('events',event_id,{'status':'success','finished':now(),'latency_ms':(time.perf_counter()-started)*1000,'usage_source':'cache','input_tokens':0,'output_tokens':0,'cost_micros':0,'cache_hit':1,'reserved_micros':0,'decisions':encode([{'selected':True,'reason':'Encrypted exact-response cache; no upstream call was made'}])})
                    return JSONResponse(data,headers={'X-Request-ID':event_id,'X-Pliqra-Model':chosen['model'],'X-Pliqra-Cache':'HIT'})
            prices={field:max(item[field] for item in candidates) if all(item[field] is not None for item in candidates) else None for field in ('input_price','output_price')}
            event_id,_=self.begin(principal,port,chosen,body,decisions,prices,kind,len(candidates))
            attempts=[]
            for deployment in candidates:
                chosen=deployment
                attempt_id=uid('attempt_')
                self.store.insert('attempts',{'id':attempt_id,'event_id':event_id,'deployment_id':deployment['id'],'model':deployment['model'],'started':now(),'outcome':'pending'})
                url,payload,auth=upstream_request(deployment,body,deployment['model'],self.vault.decrypt(deployment['secret']))
                payload.pop('pliqra',None)
                payload.pop('modelport',None)
                try:
                    response=await self.client.send(self.client.build_request('POST',url,json=payload,headers=auth),stream=True)
                except httpx.HTTPError:
                    self.finish_attempt(attempt_id,deployment,'connection_error')
                    attempts.append({'deployment_id':deployment['id'],'model':deployment['model'],'outcome':'Connection failed before a response'})
                    continue
                if response.status_code>=400:
                    status=response.status_code
                    error_data={}
                    try:
                        chunks=[];size=0
                        async for chunk in response.aiter_bytes():
                            size+=len(chunk)
                            if size>2_000_000:break
                            chunks.append(chunk)
                        if size<=2_000_000:error_data=json.loads(b''.join(chunks))
                    except (httpx.HTTPError,ValueError):pass
                    await response.aclose()
                    self.finish_attempt(attempt_id,deployment,'upstream_error',error_data,status)
                    attempts.append({'deployment_id':deployment['id'],'model':deployment['model'],'outcome':f'HTTP {status}; fallback only before streaming'})
                    response=None
                    if status in (429,502,503,504):continue
                    break
                break
            trail=decisions+attempts+[{'deployment_id':chosen['id'],'model':chosen['model'],'selected':response is not None,'reason':'Selected the first available eligible deployment' if port['strategy']=='priority' else 'Selected by round-robin among eligible deployments'}]
            self.store.update('events',event_id,{'deployment_id':chosen['id'],'model':chosen['model'],'connection_name':chosen['connection_name'],'decisions':encode(trail),'price_snapshot':encode({'attempts':[{k:row.get(k) for k in ('id','model','input_price','output_price')} for row in candidates]})})
            if response is None:
                self.finish(event_id,started,chosen,'error',error='All eligible upstreams failed or rejected this request')
                raise HTTPException(502,{'message':'All eligible upstreams failed or rejected this request','request_id':event_id})
            self.store.update('connections',chosen['connection_id'],{'status':'connected','last_checked':now(),'last_error':None})
            if body.get('stream'):
                async def iterator():
                    try:
                        async for chunk in self.stream(response,chosen,event_id,started):yield chunk
                    finally:release()
                streaming=True
                return StreamingResponse(iterator(),media_type='text/event-stream',headers={'X-Request-ID':event_id,'X-Pliqra-Model':chosen['model'],'Cache-Control':'no-cache','X-Accel-Buffering':'no'},background=BackgroundTask(release))
            chunks=[];size=0
            async for chunk in response.aiter_bytes():
                size+=len(chunk)
                if size>16_000_000:raise ValueError('Response exceeded 16 MB')
                chunks.append(chunk)
            await response.aclose()
            data=json.loads(b''.join(chunks))
            if chosen['provider']=='ollama':data=ollama_result(data,event_id,chosen['model'])
            if not isinstance(data,dict) or not isinstance(data.get('choices'),list) or not data['choices']:raise ValueError('Invalid chat completion')
            self.capture_response(event_id,data)
            self.finish(event_id,started,chosen,'success',data)
            self.mark_verified(chosen,'chat')
            if cache_id:
                self.store.run('INSERT INTO cache_entries VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,expires=excluded.expires',(cache_id,port['id'],self.vault.encrypt(encode(data)),now()+port['cache_ttl']))
            return JSONResponse(data,headers={'X-Request-ID':event_id,'X-Pliqra-Model':chosen['model'],'X-Pliqra-Cache':'MISS'})
        except HTTPException:
            if response:await response.aclose()
            if event_id and self.store.one("SELECT id FROM events WHERE id=? AND status='pending'",(event_id,)):
                self.finish(event_id,started,chosen,'error',error='Upstream request could not be completed')
            raise
        except asyncio.CancelledError:
            if event_id:self.finish(event_id,started,chosen,'cancelled',error='Client disconnected; upstream cancellation requested')
            if response:await response.aclose()
            raise
        except (httpx.HTTPError,ValueError,TypeError,KeyError):
            if response:await response.aclose()
            if event_id:self.finish(event_id,started,chosen,'error',error='Endpoint returned an unreadable or incomplete response')
            raise HTTPException(502,{'message':'Endpoint returned an unreadable or incomplete response','request_id':event_id})
        finally:
            if not streaming:release()

    def mark_verified(self, deployment, capability):
        with self.store.transaction():
            row = self.store.one("SELECT verified FROM deployments WHERE id=?", (deployment["id"],))
            if row:
                verified = set(json.loads(row["verified"])) | {capability}
                self.store.update("deployments", deployment["id"], {"verified": encode(sorted(verified)),"verified_at":now()})

    async def stream(self, response, deployment, event_id, started):
        usage, terminal, ttft, error = {}, False, None, None
        captured=[];capture_size=0
        capture=bool(self.store.one("SELECT id FROM event_content WHERE id=?",(event_id,)))
        try:
            async for line in response.aiter_lines():
                if capture and capture_size<64000:
                    captured.append(line.encode('utf-8')[:max(0,63999-capture_size)].decode('utf-8',errors='ignore'));capture_size+=len(line.encode('utf-8'))+1
                if not line.strip():
                    continue
                if deployment["provider"] == "ollama":
                    obj = json.loads(line)
                    if obj.get("error"):
                        raise ValueError("Upstream reported a streaming error")
                    message = obj.get("message", {})
                    text = message.get("content", "")
                    if ttft is None and (text or message.get("tool_calls")):
                        ttft = (time.perf_counter() - started) * 1000
                    chunk = {"id": event_id, "object": "chat.completion.chunk", "model": deployment["model"], "choices": [{"index": 0, "delta": {"content": text, **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {})}, "finish_reason": "stop" if obj.get("done") else None}]}
                    if obj.get("done"):
                        normalized = ollama_result(obj, event_id, deployment["model"])
                        usage = normalized
                        if "usage" in normalized:
                            chunk["usage"] = normalized["usage"]
                        terminal = True
                    yield sse(chunk)
                    if terminal:
                        yield "data: [DONE]\n\n"
                        break
                else:
                    if line.startswith(":"):
                        yield line + "\n\n"
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        terminal = True
                        yield "data: [DONE]\n\n"
                        break
                    obj = json.loads(payload)
                    if obj.get("error"):
                        raise ValueError("Upstream reported a streaming error")
                    if obj.get("usage"):
                        usage = obj
                    if ttft is None and any(choice.get("delta", {}).get("content") or choice.get("delta", {}).get("tool_calls") for choice in obj.get("choices", [])):
                        ttft = (time.perf_counter() - started) * 1000
                    yield sse(obj)
            if not terminal:
                error = "Upstream stream ended without its completion marker"
                yield sse({"error": {"message": error, "request_id": event_id, "type": "incomplete_stream"}})
        except asyncio.CancelledError:
            self.finish(event_id, started, deployment, "cancelled", usage, ttft, "Client disconnected; upstream cancellation requested")
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            error = "Upstream stream failed; no fallback was attempted after streaming started"
            yield sse({"error": {"message": error, "request_id": event_id, "type": "upstream_stream_error"}})
        finally:
            await response.aclose()
            if capture:self.capture_response(event_id,{"stream_sse":"\n".join(captured),"truncated":capture_size>=64000})
            existing = self.store.one("SELECT status FROM events WHERE id=?", (event_id,))
            if existing and existing["status"] == "pending":
                self.finish(event_id, started, deployment, "success" if terminal and not error else "incomplete", usage, ttft, error)
                if terminal and not error:
                    self.mark_verified(deployment, "chat")
                    self.mark_verified(deployment, "streaming")
