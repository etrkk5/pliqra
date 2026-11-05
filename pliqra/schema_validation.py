"""Bounded, cancellable JSON Schema execution outside the inference event loop."""
import asyncio
import json
import os
import sys

from jsonschema import Draft202012Validator


def validate_schema_definition(value):
    if len(json.dumps(value))>20000:
        raise ValueError('Schema exceeds 20 KB')
    def walk(node, depth=0):
        if depth>40:
            raise ValueError('Schema nesting exceeds 40 levels')
        if isinstance(node,dict):
            for key,item in node.items():
                if key in ('$ref','$dynamicRef') and (not isinstance(item,str) or not item.startswith('#')):
                    raise ValueError('Only local schema references are permitted')
                walk(item,depth+1)
        elif isinstance(node,list):
            for item in node:
                walk(item,depth+1)
    walk(value)
    try:
        Draft202012Validator.check_schema(value)
    except Exception:
        raise ValueError('Invalid JSON Schema')
    return value


class SchemaExecutor:
    def __init__(self):
        self.slots=asyncio.Semaphore(2)

    async def validate(self, schema, value, timeout=5):
        async with asyncio.timeout(timeout):
            async with self.slots:
                return await self._worker(schema,value)

    async def _worker(self,schema,value):
        spawning=None
        process=None
        try:
            environment={k:v for k,v in os.environ.items() if k in ('PATH','PYTHONPATH','SYSTEMROOT')}
            environment.update(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
            spawning=asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable,'-m','pliqra.schema_worker',stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=environment))
            process=await asyncio.shield(spawning)
            output,_=await process.communicate(json.dumps({'schema':schema,'value':value},allow_nan=False).encode())
            if process.returncode or len(output)>1024:
                raise ValueError('Schema worker failed')
            result=json.loads(output)
            if not isinstance(result.get('valid'),bool):
                raise ValueError('Invalid schema worker result')
            return result['valid']
        finally:
            # A timeout can land while the OS is creating the child. Reap it in both cases.
            if spawning is not None:
                if process is None:
                    process=await spawning
                if process.returncode is None:
                    process.kill()
                await process.wait()
