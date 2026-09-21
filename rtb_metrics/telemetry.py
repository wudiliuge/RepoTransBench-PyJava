"""Optional, secret-free accounting of every attempted API request."""
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .transport import prepare_request


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def append_event(path, event):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(event, ensure_ascii=False, allow_nan=False)+'\n')
        f.flush()
        os.fsync(f.fileno())


class FatalAPIError(SystemExit):
    """Stop legacy generators whose ordinary exception handler retries forever."""
    def __init__(self, api_error_code):
        self.api_error_code = api_error_code
        explanations = {
            'authentication_error': 'API 身份验证失败，请检查密钥。',
            'insufficient_balance': 'API 账户余额不足或欠费，请检查账户余额。',
            'permission_denied': 'API 访问被拒绝，请检查账户和模型权限。',
            'invalid_request': 'API 请求参数或模型配置无效，请检查配置。',
        }
        super().__init__(explanations[api_error_code] + ' 已停止运行。')


def fatal_api_code(status, body):
    """Classify only known structured identifiers; never expose response text."""
    identifiers = []
    if isinstance(body, dict):
        containers = [body]
        if isinstance(body.get('error'), dict):
            containers.append(body['error'])
        for container in containers:
            for field in ('code', 'type', 'error_code'):
                value = container.get(field)
                if isinstance(value, str):
                    identifiers.append(''.join(c for c in value.lower() if c.isalnum()))
    known = {
        'arrearage': 'insufficient_balance',
        'insufficientbalance': 'insufficient_balance',
        'insufficientquota': 'insufficient_balance',
        'invalidapikey': 'authentication_error',
        'authenticationerror': 'authentication_error',
        'permissiondenied': 'permission_denied',
        'permissiondeniederror': 'permission_denied',
        'model400': 'invalid_request',
        'invalidrequesterror': 'invalid_request',
        'invalidparameters': 'invalid_request',
    }
    for identifier in identifiers:
        if identifier in known:
            return known[identifier]
    return {400: 'invalid_request', 401: 'authentication_error',
            402: 'insufficient_balance', 403: 'permission_denied',
            404: 'invalid_request'}.get(status)


def request_json(post, url, **kwargs):
    """Optional logging and fatal-error stop, independently enabled by environment."""
    url, kwargs = prepare_request(url, kwargs)
    log = os.getenv('RTB_REQUEST_LOG')
    stop_on_error = os.getenv('RTB_STOP_ON_API_ERROR') == '1'
    if not log and not stop_on_error:
        return post(url, **kwargs).json()
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    if log:
        append_event(log, dict(event='start', request_id=request_id, timestamp=utc_now()))
    usage, status, error = None, None, None
    api_error_code = None
    try:
        response = post(url, **kwargs)
        status = response.status_code
        try:
            body = response.json()
        except Exception:
            if stop_on_error:
                api_error_code = fatal_api_code(status, None)
                if api_error_code:
                    raise FatalAPIError(api_error_code) from None
            raise
        if isinstance(body, dict) and isinstance(body.get('usage'), dict):
            allowed = ('prompt_tokens','completion_tokens','total_tokens','input_tokens','output_tokens')
            usage = {k:v for k,v in body['usage'].items() if k in allowed}
        if stop_on_error:
            api_error_code = fatal_api_code(status, body)
            if api_error_code:
                raise FatalAPIError(api_error_code)
        return body
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        if log:
            event = dict(event='finish', request_id=request_id, timestamp=utc_now(),
                         elapsed_seconds=time.perf_counter()-started, http_status=status,
                         error_type=error, usage=usage)
            if stop_on_error:
                event.update(api_error_code=api_error_code, fatal_api_error=bool(api_error_code))
            append_event(log, event)
