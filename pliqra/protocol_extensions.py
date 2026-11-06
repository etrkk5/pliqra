"""Optional, explicit protocol extensions. Required unknown extensions fail closed."""
from fastapi import HTTPException

EXTENSIONS={
    'pliqra.placement/1':{'description':'Site-aware model placement included in the accepted service contract','path':'/protocol/v1/services'},
    'pliqra.policy/1':{'description':'Named gateway policies and guardrail contracts','path':'/protocol/v1/gateway'},
    'pliqra.operations/1':{'description':'Bound embeddings, speech and transcription','path':'/protocol/v1/bindings/{id}/operations/{operation}'},
    'pliqra.prompts/1':{'description':'Explicit immutable prompt versions','path':'/api/v1/prompts'},
    'pliqra.batches/1':{'description':'Durable batches with optional contract-bound items','path':'/api/v1/batches'},
}


def negotiate(required,optional):
    unknown=sorted(set(required)-set(EXTENSIONS))
    if unknown:raise HTTPException(422,{'code':'unsupported_extensions','unsupported':unknown,'supported':sorted(EXTENSIONS)})
    return sorted(set(required)|(set(optional)&set(EXTENSIONS)))
