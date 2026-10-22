"""Mock of the Anthropic Messages API (POST /v1/messages) and token counting (POST /v1/messages/count_tokens).

The mock does not think. It answers with scripted tool calls, but it rejects requests the real API rejects:
  * x-api-key and anthropic-version: 2023-06-01 headers (401 / 400)
  * model must be a current model id (404 not_found_error otherwise), max_tokens a positive integer within
    the model's output limit, messages non empty, starting with "user", roles alternating, no empty content
  * the conversation must end with a user turn: assistant prefill returns 400 on current models
  * temperature, top_p and top_k return 400 on Claude Sonnet 5, Opus 5, Opus 5.5, Opus 4.7/4.8 and Fable 5/5.1
  * thinking budget_tokens returns 400 on those models; thinking cannot be combined with a forced tool;
    thinking {"type": "disabled"} returns 400 on Opus 5.5 and Fable 5 / 5.1 (and on Opus 5 at effort xhigh or max)
  * tool names must match ^[a-zA-Z0-9_-]{1,128}$, input_schema must be a JSON schema object,
    tool_choice {"type": "tool"} must name a defined tool; forced tool_choice returns 400 on
    claude-opus-5-5 and claude-fable-5-1
  * unknown top level fields return 400 "Extra inputs are not permitted"
Every rejected request is also added to mock.anthropic.problems, so a test can assert that list is empty.
A scripted tool input that does not match the request's input_schema is recorded in
mock.anthropic.schema_problems (the answer is still sent, so fallback paths can be tested on purpose).
"""
import json
import re

from .core import json_response

API_KEY = 'mock-anthropic-key'
VERSION = '2023-06-01'
MODELS = {
    'claude-fable-5-1': 128000, 'claude-fable-5': 128000, 'claude-mythos-5-1': 128000, 'claude-opus-5-5': 128000,
    'claude-opus-5': 128000, 'claude-opus-4-8': 128000, 'claude-opus-4-7': 128000, 'claude-opus-4-6': 128000,
    'claude-sonnet-5': 128000, 'claude-sonnet-4-6': 128000, 'claude-haiku-4-5': 64000, 'claude-haiku-4-5-20251001': 64000,
}
NO_SAMPLING = {'claude-fable-5-1', 'claude-fable-5', 'claude-mythos-5-1', 'claude-opus-5-5', 'claude-opus-5',
               'claude-opus-4-8', 'claude-opus-4-7', 'claude-sonnet-5'}
NO_BUDGET_TOKENS = NO_SAMPLING
NO_PREFILL = NO_SAMPLING | {'claude-opus-4-6', 'claude-sonnet-4-6'}
NO_FORCED_TOOL = {'claude-opus-5-5', 'claude-fable-5-1', 'claude-mythos-5-1'}
ALWAYS_THINKING = {'claude-opus-5-5', 'claude-fable-5-1', 'claude-fable-5', 'claude-mythos-5-1'}
TOP_LEVEL = {'model', 'messages', 'max_tokens', 'system', 'tools', 'tool_choice', 'temperature', 'top_p', 'top_k',
             'stop_sequences', 'stream', 'metadata', 'thinking', 'output_config', 'service_tier', 'container',
             'context_management', 'mcp_servers', 'inference_geo', 'cache_control'}
KNOWN_BETAS = {'token-efficient-tools-2025-02-19', 'context-management-2025-06-27', 'compact-2026-01-12',
               'server-side-fallback-2026-07-01', 'mcp-client-2025-11-20'}
TOOL_NAME = re.compile(r'^[a-zA-Z0-9_-]{1,128}$')
BLOCK_TYPES = {'text', 'image', 'document', 'tool_use', 'tool_result', 'thinking', 'redacted_thinking', 'search_result'}


def _error(status, etype, message):
    return json_response(status, {'type': 'error', 'error': {'type': etype, 'message': message},
                                  'request_id': 'req_mock_' + format(abs(hash(message)) % 10 ** 12, '012d')})


class AnthropicMock:
    def __init__(self, server, api_key=API_KEY):
        self.server = server
        self.api_key = api_key
        self.reset()

    def reset(self):
        self.rules = []
        self.queued = []
        self.problems = []
        self.schema_problems = []
        self.calls = []
        self._n = 0

    # ------------------------------------------------------------------ scripting

    def on(self, match=None, tool_input=None, text=None, status=None, error_type=None, times=None, delay=None):
        """Script an answer.

        match       substring of the LAST user message text, a callable(body) -> bool, or None for any request
        tool_input  dict returned as the input of a tool_use block (the forced tool's name is used)
        text        return a plain text answer instead (stop_reason end_turn)
        status      return an HTTP error instead, e.g. 529 (overloaded_error), 500 (api_error), 429 (rate_limit_error)
        times       how many requests this rule answers (None = unlimited)
        Rules are tried in the order they were added; queued answers (queue()) go first.
        """
        self.rules.append({'match': match, 'tool_input': tool_input, 'text': text, 'status': status,
                           'error_type': error_type, 'times': times})

    def queue(self, tool_input=None, text=None, status=None, error_type=None):
        """Answer the next request with this, whatever it contains."""
        self.queued.append({'tool_input': tool_input, 'text': text, 'status': status, 'error_type': error_type})

    def last_user_text(self, body):
        msgs = body.get('messages') or []
        for m in reversed(msgs):
            if m.get('role') == 'user':
                c = m.get('content')
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return '\n'.join(b.get('text', '') for b in c if isinstance(b, dict) and b.get('type') == 'text')
        return ''

    # ------------------------------------------------------------------ routing

    def route(self, path):
        if path in ('/v1/messages', '/v1/messages/count_tokens'):
            return 'anthropic'
        return None

    def annotate(self, req, entry):
        if isinstance(req.json, dict):
            entry['model'] = req.json.get('model')
            tc = req.json.get('tool_choice') or {}
            entry['tool'] = tc.get('name')
            entry['user_text'] = self.last_user_text(req.json)
            entry['operation'] = 'count_tokens' if req.path.endswith('count_tokens') else 'messages'

    def error_body(self, status):
        etype = {400: 'invalid_request_error', 401: 'authentication_error', 403: 'permission_error', 404: 'not_found_error',
                 413: 'request_too_large', 429: 'rate_limit_error', 529: 'overloaded_error'}.get(status, 'api_error')
        msg = {529: 'Overloaded', 429: 'Number of request tokens has exceeded your per-minute rate limit'}.get(status, 'Internal server error')
        return {'type': 'error', 'error': {'type': etype, 'message': msg}}

    # ------------------------------------------------------------------ validation

    def validate(self, headers, body, counting=False):
        """Returns (status, error_type, message) for the first problem, or None."""
        if not headers.get('x-api-key') and not headers.get('authorization'):
            return 401, 'authentication_error', 'x-api-key header is required'
        if headers.get('x-api-key') != self.api_key:
            return 401, 'authentication_error', 'invalid x-api-key'
        version = headers.get('anthropic-version')
        if not version:
            return 400, 'invalid_request_error', 'anthropic-version: header is required'
        if version not in (VERSION, '2023-01-01'):
            return 400, 'invalid_request_error', f'anthropic-version: invalid version {version}'
        betas = [b.strip() for b in (headers.get('anthropic-beta') or '').split(',') if b.strip()]
        bad = [b for b in betas if b not in KNOWN_BETAS]
        if bad:
            return 400, 'invalid_request_error', f"Unexpected value(s) `{', '.join(bad)}` for the `anthropic-beta` header."
        if not isinstance(body, dict):
            return 400, 'invalid_request_error', 'Request body must be a JSON object'
        allowed = TOP_LEVEL - ({'max_tokens', 'stream', 'stop_sequences', 'temperature', 'top_p', 'top_k', 'metadata',
                                'service_tier', 'container', 'inference_geo'} if counting else set())
        for k in body:
            if k not in allowed:
                return 400, 'invalid_request_error', f'{k}: Extra inputs are not permitted'
        model = body.get('model')
        if not isinstance(model, str):
            return 400, 'invalid_request_error', 'model: Field required'
        if model not in MODELS:
            return 404, 'not_found_error', f'model: {model}'
        if not counting:
            mt = body.get('max_tokens')
            if isinstance(mt, bool) or not isinstance(mt, int):
                return 400, 'invalid_request_error', 'max_tokens: Field required' if mt is None else 'max_tokens: Input should be a valid integer'
            if mt < 1:
                return 400, 'invalid_request_error', 'max_tokens: Input should be greater than or equal to 1'
            if mt > MODELS[model]:
                return 400, 'invalid_request_error', (f'max_tokens: {mt} > {MODELS[model]}, which is the maximum allowed number '
                                                      f'of output tokens for {model}')
            if body.get('stream'):
                return 400, 'invalid_request_error', 'stream: this mock answers non-streaming requests only (send stream: false)'
        if model in NO_SAMPLING:
            for k in ('temperature', 'top_p', 'top_k'):
                if k in body:
                    return 400, 'invalid_request_error', f'{k}: This parameter is not supported for {model}. Remove it.'
        system = body.get('system')
        if system is not None:
            if isinstance(system, list):
                if not all(isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str) for b in system):
                    return 400, 'invalid_request_error', 'system: each block must be a text block'
            elif not isinstance(system, str):
                return 400, 'invalid_request_error', 'system: Input should be a valid string or list of text blocks'
        msgs = body.get('messages')
        if not isinstance(msgs, list) or not msgs:
            return 400, 'invalid_request_error', 'messages: at least one message is required'
        for i, m in enumerate(msgs):
            if not isinstance(m, dict) or m.get('role') not in ('user', 'assistant'):
                return 400, 'invalid_request_error', f'messages.{i}.role: Input should be \'user\' or \'assistant\''
            c = m.get('content')
            if isinstance(c, str):
                if not c.strip():
                    return 400, 'invalid_request_error', f'messages.{i}: content must be non-empty'
            elif isinstance(c, list):
                if not c:
                    return 400, 'invalid_request_error', f'messages.{i}: content must be non-empty'
                for j, b in enumerate(c):
                    if not isinstance(b, dict) or b.get('type') not in BLOCK_TYPES:
                        return 400, 'invalid_request_error', f'messages.{i}.content.{j}.type: invalid content block type'
                    if b['type'] == 'text' and not (isinstance(b.get('text'), str) and b['text'].strip()):
                        return 400, 'invalid_request_error', f'messages.{i}.content.{j}.text: text content blocks must be non-empty'
            else:
                return 400, 'invalid_request_error', f'messages.{i}.content: Field required'
        if msgs[0]['role'] != 'user':
            return 400, 'invalid_request_error', 'messages: first message must use the "user" role'
        for a, b in zip(msgs, msgs[1:]):
            if a['role'] == b['role']:
                return 400, 'invalid_request_error', 'messages: roles must alternate between "user" and "assistant"'
        # count_tokens does not apply the generation rules below (checked against the real endpoint in October 2026:
        # prefill, budget_tokens and thinking with a forced tool all count fine there and fail only on /v1/messages)
        if msgs[-1]['role'] == 'assistant' and model in NO_PREFILL and not counting:
            return 400, 'invalid_request_error', ('This model does not support assistant message prefill. '
                                                  'The conversation must end with a user message.')
        tools = body.get('tools')
        names = []
        if tools is not None:
            if not isinstance(tools, list):
                return 400, 'invalid_request_error', 'tools: Input should be a valid list'
            for i, t in enumerate(tools):
                if not isinstance(t, dict):
                    return 400, 'invalid_request_error', f'tools.{i}: Input should be an object'
                if 'type' in t and t['type'] != 'custom':
                    names.append(t.get('name'))
                    continue
                if not isinstance(t.get('name'), str) or not TOOL_NAME.match(t['name']):
                    return 400, 'invalid_request_error', f'tools.{i}.name: String should match pattern \'^[a-zA-Z0-9_-]{{1,128}}$\''
                schema = t.get('input_schema')
                if not isinstance(schema, dict) or schema.get('type') != 'object':
                    return 400, 'invalid_request_error', f'tools.{i}.input_schema: JSON schema must have type "object"'
                extra = set(t) - {'name', 'description', 'input_schema', 'cache_control', 'strict', 'type',
                                  'eager_input_streaming', 'input_examples'}
                if extra:
                    return 400, 'invalid_request_error', f'tools.{i}.{sorted(extra)[0]}: Extra inputs are not permitted'
                if t['name'] in names:
                    return 400, 'invalid_request_error', f'tools: Tool names must be unique ({t["name"]})'
                names.append(t['name'])
        tc = body.get('tool_choice')
        if tc is not None:
            if not isinstance(tc, dict) or tc.get('type') not in ('auto', 'any', 'tool', 'none'):
                return 400, 'invalid_request_error', 'tool_choice.type: Input should be \'auto\', \'any\', \'tool\' or \'none\''
            if tc['type'] in ('any', 'tool') and not names:
                return 400, 'invalid_request_error', 'tool_choice: tools must be provided when tool_choice is any or tool'
            if tc['type'] == 'tool' and tc.get('name') not in names:
                return 400, 'invalid_request_error', f'tool_choice.name: tool {tc.get("name")!r} is not defined in tools'
            if tc['type'] in ('any', 'tool') and model in NO_FORCED_TOOL:
                return 400, 'invalid_request_error', 'tool_choice: type "tool" and "any" are not supported for this model.'
        thinking = body.get('thinking')
        if thinking is not None:
            if not isinstance(thinking, dict) or thinking.get('type') not in ('adaptive', 'enabled', 'disabled'):
                return 400, 'invalid_request_error', 'thinking.type: Input should be \'adaptive\', \'enabled\' or \'disabled\''
            if counting:
                return None
            if thinking['type'] == 'enabled' and model in NO_BUDGET_TOKENS:
                return 400, 'invalid_request_error', f'thinking: budget_tokens is not supported for {model}. Use {{"type": "adaptive"}}.'
            effort = (body.get('output_config') or {}).get('effort') if isinstance(body.get('output_config'), dict) else None
            if thinking['type'] == 'disabled' and (model in ALWAYS_THINKING or (model == 'claude-opus-5' and effort in ('xhigh', 'max'))):
                return 400, 'invalid_request_error', f'thinking: thinking cannot be disabled for {model}. Omit the parameter.'
            if thinking['type'] in ('adaptive', 'enabled') and tc and tc.get('type') in ('any', 'tool'):
                return 400, 'invalid_request_error', 'Thinking may not be enabled when tool_choice forces tool use.'
        return None

    # ------------------------------------------------------------------ handling

    def handle(self, req, entry):
        counting = req.path.endswith('count_tokens')
        if req.method != 'POST':
            return _error(405, 'invalid_request_error', 'Method not allowed')
        if req.json_error is not None:
            problem = (400, 'invalid_request_error', 'Request body is not valid JSON')
        else:
            problem = self.validate(req.headers, req.json, counting)
        if problem:
            self.problems.append({'status': problem[0], 'message': problem[2], 'body': req.json})
            entry['problem'] = problem[2]
            return _error(*problem)
        body = req.json
        tokens = max(1, len(json.dumps(body)) // 4)
        if counting:
            return json_response(200, {'input_tokens': tokens})
        self.calls.append(body)
        answer = self._pick(body)
        if answer is None:
            text = self.last_user_text(body)
            self.problems.append({'status': 500, 'message': 'no scripted answer', 'text': text[:200]})
            return _error(500, 'api_error', f'mock: no scripted answer for this request (last user text starts: {text[:80]!r})')
        if answer.get('status'):
            st = answer['status']
            etype = answer.get('error_type') or {529: 'overloaded_error', 429: 'rate_limit_error', 500: 'api_error',
                                                  400: 'invalid_request_error', 401: 'authentication_error'}.get(st, 'api_error')
            return _error(st, etype, {529: 'Overloaded'}.get(st, 'scripted error'))
        self._n += 1
        tc = body.get('tool_choice') or {}
        if answer.get('text') is not None:
            content = [{'type': 'text', 'text': answer['text']}]
            stop = 'end_turn'
        else:
            tools = [t for t in body.get('tools') or [] if 'input_schema' in t]
            tool_name = tc.get('name') or (tools[0]['name'] if tools else 'tool')
            tool = next((t for t in tools if t['name'] == tool_name), None)
            tool_input = answer['tool_input']
            if callable(tool_input):
                tool_input = tool_input(body)
            if tool is not None:
                issues = schema_issues(tool['input_schema'], tool_input, '$')
                if issues:
                    self.schema_problems.append({'tool': tool_name, 'issues': issues, 'input': tool_input})
                    entry['schema_problems'] = issues
            content = [{'type': 'tool_use', 'id': f'toolu_mock_{self._n:04d}', 'name': tool_name, 'input': tool_input}]
            stop = 'tool_use'
        return json_response(200, {
            'id': f'msg_mock_{self._n:04d}', 'type': 'message', 'role': 'assistant', 'model': body['model'],
            'content': content, 'stop_reason': stop, 'stop_sequence': None, 'stop_details': None,
            'usage': {'input_tokens': tokens, 'output_tokens': 40 + len(json.dumps(content)) // 4,
                      'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'service_tier': 'standard'}})

    def _pick(self, body):
        if self.queued:
            return self.queued.pop(0)
        text = self.last_user_text(body)
        for rule in self.rules:
            if rule['times'] == 0:
                continue
            m = rule['match']
            ok = m is None or (callable(m) and m(body)) or (isinstance(m, str) and m in text)
            if ok:
                if rule['times'] is not None:
                    rule['times'] -= 1
                return rule
        return None


def schema_issues(schema, value, path):
    """A small JSON Schema check: type, enum, required, properties, additionalProperties, items, min/max."""
    issues = []
    types = schema.get('type')
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        ok = any(_is_type(value, t) for t in allowed)
        if not ok:
            return [f'{path}: expected {"/".join(allowed)}, got {type(value).__name__}']
    if 'enum' in schema and value not in schema['enum']:
        issues.append(f'{path}: {value!r} is not one of {schema["enum"]}')
    if isinstance(value, dict):
        props = schema.get('properties', {})
        for r in schema.get('required', []):
            if r not in value:
                issues.append(f'{path}.{r}: required')
        for k, v in value.items():
            if k in props:
                issues += schema_issues(props[k], v, f'{path}.{k}')
            elif schema.get('additionalProperties') is False:
                issues.append(f'{path}.{k}: not allowed (additionalProperties is false)')
    if isinstance(value, list) and 'items' in schema:
        for i, v in enumerate(value):
            issues += schema_issues(schema['items'], v, f'{path}[{i}]')
        if 'minItems' in schema and len(value) < schema['minItems']:
            issues.append(f'{path}: fewer than {schema["minItems"]} items')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            issues.append(f'{path}: below minimum {schema["minimum"]}')
        if 'maximum' in schema and value > schema['maximum']:
            issues.append(f'{path}: above maximum {schema["maximum"]}')
    return issues


def _is_type(v, t):
    return {'object': isinstance(v, dict), 'array': isinstance(v, list), 'string': isinstance(v, str),
            'integer': isinstance(v, int) and not isinstance(v, bool),
            'number': isinstance(v, (int, float)) and not isinstance(v, bool),
            'boolean': isinstance(v, bool), 'null': v is None}.get(t, True)
