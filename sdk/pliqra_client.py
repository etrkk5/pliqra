"""Dependency-free client for the Pliqra discovery/binding protocol.

This module deliberately does not import the gateway implementation.
"""
import argparse
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class PliqraError(Exception):
    def __init__(self,status,detail):
        self.status,self.detail=status,detail
        super().__init__(f'HTTP {status}: {detail}')


class ContractChanged(PliqraError):
    pass


class Pliqra:
    def __init__(self,base_url,api_key,timeout=90):
        parsed=urlsplit(base_url)
        if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Use an HTTP(S) base URL without credentials, query, or fragment')
        self.base_url,self.api_key,self.timeout=base_url.rstrip('/'),api_key,timeout
        self.opener=build_opener(NoRedirect())
        self.protocol=None

    def request(self,path,body=None):
        if not path.startswith('/') or path.startswith('//') or '://' in path:raise ValueError('Server returned a non-local protocol path')
        data=json.dumps(body).encode() if body is not None else None
        request=Request(self.base_url+path,data=data,headers={'Authorization':'Bearer '+self.api_key,'Content-Type':'application/json'},method='POST' if data is not None else 'GET')
        try:return self.opener.open(request,timeout=self.timeout)
        except HTTPError as exc:
            try:detail=json.loads(exc.read(200000)).get('detail','Protocol request failed')
            except ValueError:detail='Protocol request failed'
            cls=ContractChanged if exc.code==409 else PliqraError
            raise cls(exc.code,detail) from None

    def services(self):
        with self.request(self.metadata()['discovery']) as response:return json.load(response)['services']

    def metadata(self):
        if self.protocol is None:
            with self.request('/.well-known/pliqra') as response:self.protocol=json.load(response)
            if self.protocol.get('protocol')!='pliqra/0.1-draft':raise PliqraError(422,'Unsupported Pliqra protocol version')
        return self.protocol

    def connect(self,service,requirements=None,ttl_seconds=3600):
        with self.request(self.metadata()['bind'],{'service':service,'requirements':requirements or ['chat'],'ttl_seconds':ttl_seconds}) as response:
            return Connection(self,json.load(response))


class Connection:
    def __init__(self,client,binding):
        self.client,self.binding=client,binding

    def complete(self,messages,**parameters):
        body={**parameters,'model':self.binding['service'],'messages':messages,'stream':False}
        with self.client.request(self.binding['invoke'],body) as response:return json.load(response)

    def stream(self,messages,**parameters):
        body={**parameters,'model':self.binding['service'],'messages':messages,'stream':True}
        terminal=False
        with self.client.request(self.binding['invoke'],body) as response:
            for raw in response:
                line=raw.decode('utf-8').strip()
                if not line.startswith('data:'):continue
                data=line[5:].strip()
                if data=='[DONE]':terminal=True;break
                if not data:continue
                item=json.loads(data)
                if item.get('error'):raise PliqraError(502,item['error'].get('message','Streaming failed'))
                yield item
        if not terminal:raise PliqraError(502,'Stream ended without a completion marker')


def main():
    parser=argparse.ArgumentParser(description='Discover, bind and invoke Pliqra services')
    parser.add_argument('--url',default='http://localhost:8088')
    parser.add_argument('--key-env',default='PLIQRA_API_KEY')
    parser.add_argument('command',choices=['services','chat','conformance'])
    parser.add_argument('--service')
    parser.add_argument('--prompt',default='Reply with OK.')
    args=parser.parse_args()
    token=os.getenv(args.key_env)
    if not token:parser.error('Set '+args.key_env+' to a scoped access key')
    client=Pliqra(args.url,token)
    if args.command=='services':print(json.dumps(client.services(),indent=2));return
    if not args.service:parser.error('--service is required')
    bound=client.connect(args.service,['chat'])
    if args.command=='conformance':
        assert bound.binding['fingerprint'] and bound.binding['expires']
        try:client.connect(args.service,['unsupported-capability'])
        except PliqraError as exc:assert exc.status==422
        else:raise AssertionError('Unknown capability was silently accepted')
        print('Discovery and binding conformance passed. No inference was performed.');return
    result=bound.complete([{'role':'user','content':args.prompt}])
    print(result['choices'][0]['message'].get('content',''))


if __name__=='__main__':main()
