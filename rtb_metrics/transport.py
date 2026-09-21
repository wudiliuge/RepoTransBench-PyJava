"""Validated, opt-in adaptation for OpenAI-compatible chat providers."""
import json
import math
import os
from urllib.parse import urlsplit, urlunsplit


def normalize_endpoint(base_url, endpoint_kind='auto'):
    """Resolve a server root or an explicitly supplied complete chat endpoint."""
    if endpoint_kind not in ('auto', 'root', 'full'):
        raise ValueError('EndpointKind 必须为 auto、root 或 full。')
    if not isinstance(base_url, str) or not base_url or any(c.isspace() or ord(c) < 32 for c in base_url):
        raise ValueError('API 地址必须为不含空白字符的 HTTP(S) URL。')
    try:
        parts = urlsplit(base_url)
        _ = parts.port  # Validate malformed ports before making a request.
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or '?' in base_url or '#' in base_url or '\\' in base_url):
            raise ValueError
    except ValueError:
        raise ValueError('API 地址必须为 HTTP(S) URL，且不能包含凭据、查询参数或片段。') from None
    path = parts.path if endpoint_kind == 'full' else parts.path.rstrip('/')
    if endpoint_kind == 'root' or (endpoint_kind == 'auto' and not path.endswith('/chat/completions')):
        path += '/chat/completions' if path.endswith('/v1') else '/v1/chat/completions'
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def validate_options(options):
    """Return a copy of supported generation options; never accept credentials."""
    if not isinstance(options, dict):
        raise ValueError('模型参数必须为 JSON 对象。')
    allowed = {'max_tokens', 'max_completion_tokens', 'temperature', 'top_p', 'seed',
               'stop', 'presence_penalty', 'frequency_penalty', 'reasoning_effort', 'enable_thinking'}
    if any(key not in allowed for key in options):
        raise ValueError('模型参数中包含不支持或保留的参数。')
    if 'max_tokens' in options and 'max_completion_tokens' in options:
        raise ValueError('max_tokens 与 max_completion_tokens 不能同时设置，请只选择一个。')
    for key, value in options.items():
        valid = False
        if key in ('max_tokens', 'max_completion_tokens'):
            valid = type(value) is int and value > 0
        elif key == 'seed':
            valid = type(value) is int
        elif key == 'enable_thinking':
            valid = type(value) is bool
        elif key == 'reasoning_effort':
            valid = isinstance(value, str) and value in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh')
        elif key == 'stop':
            valid = isinstance(value, str) or (isinstance(value, list) and 1 <= len(value) <= 4
                                               and all(isinstance(item, str) for item in value))
        elif type(value) in (int, float):
            lower, upper = {'temperature': (0, 2), 'top_p': (0, 1),
                            'presence_penalty': (-2, 2), 'frequency_penalty': (-2, 2)}[key]
            valid = math.isfinite(value) and lower <= value <= upper
        if not valid:
            raise ValueError('模型参数值无效：' + key)
    return {key: list(value) if isinstance(value, list) else value for key, value in options.items()}


def prepare_request(url, kwargs):
    """Apply environment overrides without mutating callers' payloads."""
    endpoint = os.getenv('RTB_CHAT_URL')
    raw_options = os.getenv('RTB_MODEL_OPTIONS')
    plain_text = os.getenv('RTB_PLAIN_TEXT_MESSAGES') == '1'
    if endpoint:
        url = normalize_endpoint(endpoint, 'full')
    options = {}
    if raw_options:
        try:
            options = validate_options(json.loads(raw_options))
        except (ValueError, TypeError):
            raise ValueError('RTB_MODEL_OPTIONS 必须包含受支持且有效的 JSON 生成参数。') from None
    if options or plain_text:
        payload = kwargs.get('json')
        if not isinstance(payload, dict):
            raise ValueError('请求参数覆盖要求请求正文为 JSON 对象。')
        payload = dict(payload)
        payload.update(options)
        if plain_text and isinstance(payload.get('messages'), list):
            messages = []
            for message in payload['messages']:
                if isinstance(message, dict):
                    message = dict(message)
                    content = message.get('content')
                    if isinstance(content, list) and content and all(
                            isinstance(part, dict) and part.get('type') == 'text'
                            and isinstance(part.get('text'), str) for part in content):
                        message['content'] = ''.join(part['text'] for part in content)
                messages.append(message)
            payload['messages'] = messages
        kwargs = dict(kwargs, json=payload)
    return url, kwargs
