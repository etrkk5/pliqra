"""The small, permission-scoped path from an endpoint to an application."""
import json
from typing import Annotated

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .access import event_scope, service_allowed
from .db import now
from .gateway import CAPABILITIES, decoded_port


class ApplicationConnection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    service: str
    name: Annotated[str, Field(min_length=1, max_length=100)]
    project_id: str | None = None
    gateway_id: str | None = None
    team_id: str | None = None
    requirements: Annotated[list[str], Field(max_length=8)] = ['chat']
    required_extensions:Annotated[list[str],Field(max_length=20)]=[]
    optional_extensions:Annotated[list[str],Field(max_length=20)]=[]
    ttl_seconds: Annotated[int, Field(ge=60, le=86400)] = 3600

    @field_validator('requirements')
    @classmethod
    def capabilities(cls, values):
        if set(values) - CAPABILITIES:
            raise ValueError('Unknown capability')
        return sorted(set(values))


def mount(app):
    ctx = app.state.context
    store, user, admin = ctx['store'], ctx['user'], ctx['admin']

    @app.get('/api/v1/getting-started', tags=['Connections'])
    def getting_started(service_id: str = '', p=Depends(user)):
        gateway = app.state.gateway
        ports = [decoded_port(row) for row in store.all('SELECT * FROM ports ORDER BY created DESC')]
        ports = [port for port in ports if service_allowed(store, p, port)]
        if service_id:
            port = next((item for item in ports if item['id'] == service_id), None)
            if not port:
                raise HTTPException(404, 'AI service not found')
        else:
            port = next((item for item in ports if gateway.candidates(item)[0]), ports[0] if ports else None)
        if not port:
            return {'service': None, 'has_successful_call': False, 'current_bindings': 0,
                    'needs_rebinding': 0, 'changes': [], 'evidence': [], 'ready': False}

        fingerprint = gateway.fingerprint(port)
        scope, args = event_scope(store, p, 'e')
        success = store.one("SELECT e.id FROM events e WHERE e.port_id=? AND e.status='success' "
                            "AND e.request_kind='inference' AND " + scope + ' LIMIT 1', [port['id'], *args])
        identity_scope = '1=1' if p['role'] == 'admin' else 'b.principal=?'
        identity_args = [] if p['role'] == 'admin' else [p['id']]
        instant = now()
        active = """b.port_id=? AND b.expires>? AND (
            EXISTS(SELECT 1 FROM api_keys k WHERE k.id=b.principal AND k.active=1 AND (k.expires IS NULL OR k.expires>?)
                AND (? IS NULL OR k.project_id=?) AND (k.allowed_ports='[]' OR EXISTS(SELECT 1 FROM json_each(k.allowed_ports) a WHERE a.value=b.port_id)))
            OR EXISTS(SELECT 1 FROM users u WHERE u.id=b.principal AND u.active=1 AND
                (u.issuer='local' OR EXISTS(SELECT 1 FROM identity_providers i WHERE i.id=u.issuer AND i.active=1))
                AND (? IS NULL OR u.role='admin' OR EXISTS(SELECT 1 FROM memberships m WHERE m.user_id=u.id AND m.project_id=?)
                    OR EXISTS(SELECT 1 FROM team_members tm JOIN team_projects tp ON tp.team_id=tm.team_id WHERE tm.user_id=u.id AND tp.project_id=?))))"""
        active=active.replace('? IS NULL','CAST(? AS TEXT) IS NULL')
        if store.backend=='postgresql':active=active.replace('json_each(k.allowed_ports) a','json_array_elements_text(k.allowed_ports::json) a(value)')
        active_args = [port['id'], instant, instant, *([port['project_id']] * 5)]
        bindings = store.all('SELECT b.* FROM bindings b WHERE ' + active + ' AND ' + identity_scope,
                             [*active_args, *identity_args])
        current, stale_principals, fresh_principals = 0, set(), set()
        for binding in bindings:
            key = store.one('SELECT * FROM api_keys WHERE id=?', (binding['principal'],))
            account = store.one('SELECT * FROM users WHERE id=?', (binding['principal'],)) if not key else None
            actor = {'id': binding['principal'], 'kind': 'key' if key else 'user',
                     'role': 'consumer' if key else account['role'], **({'key': key} if key else {})}
            try:
                policy = gateway.policies.resolve(actor, port, None if key else binding['gateway_id'])
            except HTTPException:
                continue
            matches = (binding['fingerprint'] == fingerprint and
                       binding['gateway_id'] == (policy['id'] if policy else None) and
                       binding['gateway_fingerprint'] == (policy['fingerprint'] if policy else None))
            if matches:
                current += 1
                fresh_principals.add(binding['principal'])
            else:
                stale_principals.add(binding['principal'])
        # A newly negotiated binding resolves the notice for the same application identity.
        stale = len(stale_principals - fresh_principals)
        deployments = store.all('SELECT d.*,c.enabled AS connection_enabled FROM deployments d '
                                'JOIN connections c ON c.id=d.connection_id WHERE d.id IN (' +
                                ','.join('?' for _ in port['deployments']) + ')', port['deployments'])
        changes = []
        if p['role'] == 'admin':
            connections = {row['connection_id'] for row in deployments}
            if connections:
                changes = store.all("SELECT id,message,created FROM notifications WHERE resolved=0 AND kind='model-drift' "
                                    'AND target IN (' + ','.join('?' for _ in connections) +
                                    ') ORDER BY created DESC LIMIT 5', list(connections))
        return {'service': {'id': port['id'], 'name': port['name'], 'version': port['version'],
                            'fingerprint': fingerprint, 'project_id': port['project_id']},
                'ready': bool(gateway.candidates(port)[0]), 'has_successful_call': bool(success),
                'current_bindings': current, 'needs_rebinding': stale, 'changes': changes,
                'evidence': [{'model': row['model'], 'declared': json.loads(row['capabilities']),
                              'verified': json.loads(row['verified'])} for row in deployments]}

    @app.post('/api/v1/application-connections', tags=['Connections'], status_code=201)
    def connect_application(body: ApplicationConnection, p=Depends(admin)):
        port = app.state.gateway.port(body.service)
        project_id = body.project_id or port['project_id'] or store.one('SELECT id FROM projects ORDER BY created LIMIT 1')['id']
        if port['project_id'] and project_id != port['project_id']:
            raise HTTPException(422, 'Use the project assigned to this service')
        with store.transaction():
            key = ctx['create_key'](ctx['KeyInput'](name=body.name, project_id=project_id,
                                  allowed_ports=[port['id']], rpm=60, expires_days=90,
                                  gateway_id=body.gateway_id, team_id=body.team_id), p)
            credential = store.one('SELECT * FROM api_keys WHERE id=?', (key['id'],))
            principal = {'id': key['id'], 'kind': 'key', 'role': 'consumer', 'key': credential}
            binding = ctx['bind'](ctx['BindingInput'](service=port['name'], requirements=body.requirements,
                                  ttl_seconds=body.ttl_seconds,required_extensions=body.required_extensions,
                                  optional_extensions=body.optional_extensions), principal)
        return {'binding': binding, 'key': key, 'key_expires': credential['expires'],
                'note': 'Store the key now. It is restricted to this service and shown only once.'}
