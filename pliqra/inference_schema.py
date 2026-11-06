"""The supported inference boundary, separate from routing and HTTP management."""
import copy
import json
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

INFERENCE_CONTRACT_REVISION = 'pliqra-inference/3'


class Message(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['system', 'developer', 'user', 'assistant', 'tool', 'function']
    content: str | list[dict] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict] | None = None
    function_call: dict | None = None
    refusal: str | None = None
    reasoning_content: str | None = None

    @field_validator('content')
    @classmethod
    def content_parts(cls, value):
        if isinstance(value, list):
            for part in value:
                if part.get('type') not in ('text', 'image_url', 'input_image', 'refusal'):
                    raise ValueError('Unsupported chat content part')
        return value


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    include_usage: bool = True


class PromptSelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str
    version:Annotated[int,Field(ge=1)]
    variables:dict[str,Annotated[str,Field(max_length=20000)]]={}


class ChatInput(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    model: Annotated[str, Field(min_length=1, max_length=256)]
    messages: Annotated[list[Message], Field(max_length=500)] = []
    prompt:PromptSelection|None=None
    stream: bool = False
    stream_options: StreamOptions | None = None
    max_tokens: Annotated[int | None, Field(ge=1, le=131072)] = None
    max_completion_tokens: Annotated[int | None, Field(ge=1, le=131072)] = None
    temperature: Annotated[float | None, Field(ge=0, le=2)] = None
    top_p: Annotated[float | None, Field(ge=0, le=1)] = None
    frequency_penalty: Annotated[float | None, Field(ge=-2, le=2)] = None
    presence_penalty: Annotated[float | None, Field(ge=-2, le=2)] = None
    seed: int | None = None
    stop: str | list[str] | None = None
    logit_bias: dict[str, float] | None = None
    logprobs: bool | None = None
    top_logprobs: Annotated[int | None, Field(ge=0, le=20)] = None
    user: Annotated[str | None, Field(max_length=256)] = None
    n: Literal[1] = 1
    best_of: Literal[1] = Field(default=1, exclude=True)
    response_format: dict | None = None
    tools: Annotated[list[dict] | None, Field(max_length=128)] = None
    tool_choice: str | dict | None = None
    parallel_tool_calls: bool | None = None
    functions: Annotated[list[dict] | None, Field(max_length=128)] = None
    function_call: str | dict | None = None
    pliqra: dict | None = Field(default=None, validation_alias=AliasChoices('pliqra', 'modelport'))

    @model_validator(mode='before')
    @classmethod
    def attribution_aliases(cls, value):
        try:
            json.dumps(value,allow_nan=False)
        except (ValueError,TypeError,RecursionError):
            raise ValueError('Inference requests must contain finite, serializable JSON')
        if isinstance(value,dict) and 'pliqra' in value and 'modelport' in value:
            return {k:v for k,v in value.items() if k!='modelport'}
        return value

    @model_validator(mode='after')
    def unambiguous_output_limit(self):
        if not self.messages and self.prompt is None:raise ValueError('Messages or an explicit prompt version are required')
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError('Use either max_tokens or max_completion_tokens, not both')
        return self


def requests_tools(body):
    return bool(body.get('tools') or body.get('functions')) or any(
        body.get(name) not in (None, 'none') for name in ('tool_choice', 'function_call'))


def valid_completion(data):
    if not isinstance(data, dict) or not isinstance(data.get('choices'), list) or len(data['choices']) != 1:
        raise ValueError('Expected one chat completion')
    choice = data['choices'][0]
    message = choice.get('message') if isinstance(choice, dict) else None
    if not isinstance(message, dict) or message.get('role', 'assistant') != 'assistant':
        raise ValueError('Invalid completion message')
    if not any(message.get(k) is not None for k in ('content', 'tool_calls', 'function_call', 'refusal', 'reasoning_content')):
        raise ValueError('Completion message has no content or call')
    if message.get('content') is not None and not isinstance(message['content'], (str, list)):
        raise ValueError('Invalid completion content')
    if message.get('tool_calls') is not None and not isinstance(message['tool_calls'], list):
        raise ValueError('Invalid completion tool calls')
    for field in ('name', 'refusal', 'reasoning_content'):
        if message.get(field) is not None and not isinstance(message[field], str):
            raise ValueError('Invalid completion text field')
    if isinstance(message.get('content'), list):
        for part in message['content']:
            if not isinstance(part, dict) or not isinstance(part.get('type'), str):
                raise ValueError('Invalid completion content part')
            for field in ('text', 'refusal'):
                if field in part and not isinstance(part[field], str):
                    raise ValueError('Invalid completion content text')
    functions = [message.get('function_call')]
    for call in message.get('tool_calls') or []:
        if not isinstance(call, dict) or not isinstance(call.get('id', ''), str):
            raise ValueError('Invalid completion tool call')
        functions.append(call.get('function'))
        if call.get('function') is None:
            raise ValueError('Missing completion tool function')
    for function in functions:
        if function is not None and (not isinstance(function, dict) or
                not all(isinstance(function.get(k), str) for k in ('name', 'arguments'))):
            raise ValueError('Invalid completion function')
    return data


def validate_tool_choice(data,body):
    choice=body.get('tool_choice',body.get('function_call'))
    if choice in (None,'auto'):return
    message=data['choices'][0]['message']
    names=[call['function']['name'] for call in message.get('tool_calls') or []]
    if message.get('function_call'):names.append(message['function_call']['name'])
    if choice=='none' and names:raise ValueError('Provider ignored the no-tools constraint')
    if choice=='required' and not names:raise ValueError('Provider did not return a required tool call')
    if isinstance(choice,dict):
        selected=choice.get('name') or choice.get('function',{}).get('name')
        if not names or any(name!=selected for name in names):raise ValueError('Provider did not return the selected function')


def map_message_text(messages, transform):
    """Transform data fields, preserving roles, type tags and protocol structure."""
    result = copy.deepcopy(messages)
    for message in result:
        for field in ('name', 'refusal', 'reasoning_content'):
            if isinstance(message.get(field), str):
                message[field] = transform(message[field])
        content = message.get('content')
        if isinstance(content, str):
            message['content'] = transform(content)
        elif isinstance(content, list):
            for part in content:
                for field in ('text', 'refusal'):
                    if isinstance(part.get(field), str):
                        part[field] = transform(part[field])
                image = part.get('image_url')
                if isinstance(image, dict) and isinstance(image.get('url'), str):
                    image['url'] = transform(image['url'])
                elif isinstance(image, str):
                    part['image_url'] = transform(image)
        functions = [message.get('function_call')]
        for call in message.get('tool_calls') or []:
            if isinstance(call.get('id'), str):
                call['id'] = transform(call['id'])
            functions.append(call.get('function'))
        for function in functions:
            if isinstance(function, dict):
                for field in ('name', 'arguments'):
                    if isinstance(function.get(field), str):
                        function[field] = transform(function[field])
    return result


def checked_completion(data, request_id, model):
    """Only checked message data and locally controlled metadata may be delivered."""
    valid_completion(data)
    source = data['choices'][0]
    message = source['message']
    message = {k: copy.deepcopy(v) for k, v in message.items()
               if k in ('role', 'content', 'tool_calls', 'function_call', 'refusal', 'reasoning_content')}
    message['role'] = 'assistant'
    # Content-part and tool envelopes have fixed keys; provider extensions cannot hide data in keys.
    if isinstance(message.get('content'), list):
        parts = []
        for part in message['content']:
            if not isinstance(part, dict) or part.get('type') not in ('text', 'refusal'):
                raise ValueError('Unsupported checked output content part')
            field = 'text' if part['type'] == 'text' else 'refusal'
            if not isinstance(part.get(field), str):
                raise ValueError('Invalid checked output text')
            parts.append({'type': part['type'], field: part[field]})
        message['content'] = parts
    for call in message.get('tool_calls') or []:
        function = call.get('function')
        if not isinstance(call.get('id',''),str) or not isinstance(function, dict) or not isinstance(function.get('name'), str) or not isinstance(function.get('arguments'), str):
            raise ValueError('Invalid checked tool call')
    if message.get('tool_calls'):
        message['tool_calls'] = [{'id': c.get('id', ''), 'type': 'function',
                                 'function': {k: c['function'][k] for k in ('name', 'arguments')}} for c in message['tool_calls']]
    if message.get('function_call'):
        message['function_call'] = {k: message['function_call'][k] for k in ('name', 'arguments')}
    reason = source.get('finish_reason') or 'stop'
    if reason not in ('stop', 'length', 'tool_calls', 'function_call', 'content_filter'):
        raise ValueError('Unsupported finish reason')
    result = {'id': request_id, 'object': 'chat.completion', 'model': model,
              'choices': [{'index': 0, 'message': message, 'finish_reason': reason}]}
    usage = data.get('usage')
    if isinstance(usage, dict):
        safe = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens')
                if isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0}
        if len(safe) == 2:
            safe['total_tokens'] = sum(safe.values())
        if safe:
            result['usage'] = safe
    return result
