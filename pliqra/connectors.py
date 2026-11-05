import json
import math
import hashlib

import httpx
from fastapi import HTTPException

from .security import endpoint_url

MAX_RESPONSE_BYTES = 16_000_000
MAX_STREAM_LINE_BYTES = 1_000_000


def strict_json(value):
    def invalid_constant(_):
        raise ValueError('Non-finite JSON number')
    def finite_float(number):
        result=float(number)
        if not math.isfinite(result):
            raise ValueError('Non-finite JSON number')
        return result
    try:
        return json.loads(value, parse_constant=invalid_constant, parse_float=finite_float)
    except RecursionError:
        raise ValueError('Endpoint JSON nesting exceeded the parser limit') from None


async def response_json(response, limit=MAX_RESPONSE_BYTES):
    chunks, size = [], 0
    async for chunk in response.aiter_bytes(chunk_size=65536):
        size += len(chunk)
        if size > limit:
            raise ValueError('Endpoint response exceeded its size limit')
        chunks.append(chunk)
    return strict_json(b''.join(chunks))


async def bounded_lines(response):
    pending, size = bytearray(), 0
    # Preserve transport chunks: chunk_size buffering would delay the first SSE token.
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError('Stream exceeded its size limit')
        pending.extend(chunk)
        while (ending := pending.find(b'\n')) >= 0:
            if ending > MAX_STREAM_LINE_BYTES:
                raise ValueError('Stream frame exceeded its size limit')
            line = bytes(pending[:ending]).removesuffix(b'\r')
            del pending[:ending+1]
            yield line.decode('utf-8')
        if len(pending) > MAX_STREAM_LINE_BYTES:
            raise ValueError('Stream frame exceeded its size limit')
    if pending:
        yield bytes(pending).removesuffix(b'\r').decode('utf-8')


def headers(secret, name="Authorization", prefix="Bearer"):
    if name.lower() in {"host", "content-length", "connection", "cookie", "transfer-encoding"} or name.lower().startswith("x-forwarded-"):
        raise HTTPException(422, "Choose an authentication header, not a transport or forwarding header")
    return {name: (prefix.strip() + " " if prefix.strip() else "") + secret} if secret else {}


async def discover(client, endpoint, secret="", provider="auto", auth_header="Authorization", auth_prefix="Bearer"):
    endpoint = endpoint_url(endpoint)
    for ending in ("/chat/completions", "/models", "/api/tags", "/api/chat"):
        if endpoint.endswith(ending):
            endpoint = endpoint[:-len(ending)]
            break
    candidates = []
    if provider in ("auto", "openai"):
        candidates.extend([("openai", endpoint + "/models"), ("openai", endpoint + "/v1/models")])
    if provider in ("auto", "ollama"):
        root = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
        candidates.append(("ollama", root + "/api/tags"))
    errors = []
    for kind, url in candidates:
        try:
            async with client.stream('GET', url, headers=headers(secret, auth_header, auth_prefix), timeout=8) as response:
                if response.status_code in (401, 403):
                    errors.append("Endpoint rejected the credentials")
                    continue
                if response.status_code != 200:
                    errors.append(f"Discovery returned HTTP {response.status_code}")
                    continue
                payload = await response_json(response, 2_000_000)
            entries = payload.get("data" if kind == "openai" else "models")
            if not isinstance(entries, list):
                continue
            models = []
            for item in entries[:200]:
                if not isinstance(item, dict):
                    continue
                name = item.get("id" if kind == "openai" else "name")
                if isinstance(name, str) and 0 < len(name) <= 256:
                    metadata = {key: item[key] for key in ("id", "name", "created", "owned_by", "digest", "details", "context_length") if key in item}
                    fingerprint = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
                    models.append({"model": name, "capabilities": ["chat", "streaming"], "metadata_hash": fingerprint, "evidence": "adapter-declared; inference not yet tested"})
            if models:
                base = url[:-len("/models")] if kind == "openai" else url[:-len("/api/tags")]
                return {"provider": kind, "endpoint": base, "models": models}
        except (httpx.HTTPError, ValueError, AttributeError):
            errors.append("Endpoint is unreachable or did not return a supported model list")
    raise HTTPException(422, {"message": "Could not discover models. Check the endpoint and credentials, or enter the model name manually.", "reasons": list(dict.fromkeys(errors))})


def upstream_request(connection, body, model, secret):
    payload = dict(body)
    payload["model"] = model
    if connection["provider"] == "openai":
        if payload.get("stream"):
            payload["stream_options"] = {**payload.get("stream_options", {}), "include_usage": True}
        return connection["endpoint"] + "/chat/completions", payload, headers(secret, connection.get("auth_header", "Authorization"), connection.get("auth_prefix", "Bearer"))
    allowed = {"model": model, "messages": body["messages"], "stream": bool(body.get("stream"))}
    options = {}
    for key, dest in (("temperature", "temperature"), ("top_p", "top_p"), ("max_tokens", "num_predict"), ("max_completion_tokens", "num_predict"), ("seed", "seed")):
        if key in body:
            options[dest] = body[key]
    if options:
        allowed["options"] = options
    if body.get("tools"):
        allowed["tools"] = body["tools"]
    fmt = body.get("response_format", {})
    if fmt.get("type") == "json_object":
        allowed["format"] = "json"
    elif fmt.get("type") == "json_schema":
        allowed["format"] = fmt.get("json_schema", {}).get("schema", {})
    return connection["endpoint"] + "/api/chat", allowed, headers(secret, connection.get("auth_header", "Authorization"), connection.get("auth_prefix", "Bearer"))


def ollama_result(data, request_id, model):
    message = data.get("message", {})
    result = {"id": request_id, "object": "chat.completion", "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": message.get("content", ""), **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {})}, "finish_reason": "stop"}]}
    if "prompt_eval_count" in data and "eval_count" in data:
        result["usage"] = {"prompt_tokens": data["prompt_eval_count"], "completion_tokens": data["eval_count"], "total_tokens": data["prompt_eval_count"] + data["eval_count"]}
    return result


def sse(data):
    return "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n"
