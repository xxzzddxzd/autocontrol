"""Feishu web login (authorization code with PKCE) and the board's session cookie.

Only authentication APIs are called: the authorization page, the
user_access_token exchange (v2) and the signed-in user's info. Feishu does not
count these against a tenant's monthly API quota. Inside the Feishu client
the authorization page goes straight back without asking.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib import error, parse, request

from .config import OPEN_ID

ACCOUNTS = {
    'https://open.feishu.cn': 'https://accounts.feishu.cn',
    'https://open.larksuite.com': 'https://accounts.larksuite.com',
}
STATE_TTL = 600.0
MAX_PENDING = 256
MAX_RESPONSE_BYTES = 256 * 1024
SESSION_DAYS = 14


class AuthError(Exception):
    """A login step failed; ``code`` names the step, never a secret."""

    def __init__(self, code: str, detail=None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f'{code}: {detail}')


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


class Logins:
    """Sign-ins in progress: state → (code_verifier, path to return to), ten minutes each."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._items: dict[str, tuple[str, str, float]] = {}
        self._lock = threading.Lock()

    def begin(self, next_path: str) -> tuple[str, str]:
        """A new ``(state, code_challenge)``."""
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode('ascii')).digest())
        now = self._clock()
        with self._lock:
            for key in [k for k, (_, _, until) in self._items.items() if until <= now]:
                del self._items[key]
            while len(self._items) >= MAX_PENDING:
                del self._items[next(iter(self._items))]
            self._items[state] = (verifier, next_path, now + STATE_TTL)
        return state, challenge

    def finish(self, state) -> tuple[str, str] | None:
        """``(code_verifier, next_path)`` for a known, unexpired state; each state works once."""
        if not isinstance(state, str):
            return None
        with self._lock:
            item = self._items.pop(state, None)
        if item is None or item[2] <= self._clock():
            return None
        return item[0], item[1]


def authorize_url(app, redirect_uri: str, state: str, challenge: str) -> str:
    accounts = ACCOUNTS.get(app['base_url'].rstrip('/'))
    if accounts is None:
        raise AuthError('unsupported_base_url')
    query = {
        'client_id': app['app_id'],
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }
    return f'{accounts}/open-apis/authen/v1/authorize?{parse.urlencode(query)}'


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(method, url, *, body=None, headers=None, timeout=10.0) -> dict:
    """One JSON exchange with Feishu; HTTP errors still return their JSON body."""
    data = None if body is None else json.dumps(body, separators=(',', ':')).encode('utf-8')
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={'Content-Type': 'application/json; charset=utf-8', **(headers or {})},
    )
    try:
        with request.build_opener(_NoRedirect).open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except error.HTTPError as exc:
        try:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
        finally:
            exc.close()
        if exc.code == 429:
            raise AuthError('rate_limited') from None
    except (OSError, ValueError) as exc:
        raise AuthError('network', type(exc).__name__) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AuthError('response_too_large')
    try:
        result = json.loads(raw)
    except ValueError:
        raise AuthError('invalid_response') from None
    if not isinstance(result, dict):
        raise AuthError('invalid_response')
    return result


def sign_in(app, code: str, verifier: str, redirect_uri: str, *, fetch=fetch_json) -> dict:
    """Trade the authorization code for the signed-in user: open_id, name, tenant."""
    if not isinstance(code, str) or not 8 <= len(code) <= 512:
        raise AuthError('invalid_code')
    base = app['base_url'].rstrip('/')
    token = fetch(
        'POST',
        base + '/open-apis/authen/v2/oauth/token',
        body={
            'grant_type': 'authorization_code',
            'client_id': app['app_id'],
            'client_secret': app['app_secret'],
            'code': code,
            'redirect_uri': redirect_uri,
            'code_verifier': verifier,
        },
    )
    access = token.get('access_token')
    if token.get('code') != 0 or not isinstance(access, str) or not access:
        raise AuthError('token', token.get('error') or token.get('code'))
    info = fetch(
        'GET',
        base + '/open-apis/authen/v1/user_info',
        headers={'Authorization': 'Bearer ' + access},
    )
    data = info.get('data') if info.get('code') == 0 else None
    if not isinstance(data, dict):
        raise AuthError('user_info', info.get('code'))
    open_id = data.get('open_id')
    if not isinstance(open_id, str) or not OPEN_ID.fullmatch(open_id):
        raise AuthError('user_info', 'open_id')
    name = data.get('name') if isinstance(data.get('name'), str) else ''
    return {
        'open_id': open_id,
        'name': name.strip()[:64] or '未命名',
        'tenant_key': data.get('tenant_key') if isinstance(data.get('tenant_key'), str) else None,
    }


def make_session(user: dict, secret: str, *, now=None, days=SESSION_DAYS) -> str:
    now = time.time() if now is None else now
    payload = {
        'sub': user['open_id'],
        'name': user.get('name', '')[:64],
        'exp': int(now + days * 86400),
    }
    body = _b64(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    signature = _b64(
        hmac.new(secret.encode('utf-8'), body.encode('ascii'), hashlib.sha256).digest()
    )
    return f'{body}.{signature}'


def read_session(value, secret: str, *, now=None) -> dict | None:
    """The session's ``{'sub', 'name', 'exp'}``, or None when missing, forged or expired."""
    if not isinstance(value, str) or len(value) > 2048 or value.count('.') != 1:
        return None
    body, signature = value.split('.')
    expected = _b64(hmac.new(secret.encode('utf-8'), body.encode('ascii'), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, UnicodeDecodeError):
        return None
    now = time.time() if now is None else now
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get('sub'), str)
        or type(payload.get('exp')) is not int
        or payload['exp'] <= now
    ):
        return None
    return payload


def access(config: dict, open_id: str, *, now=None) -> str:
    """'owner', 'allowed', 'claim' (no owner yet, inside the claim window) or 'denied'."""
    now = time.time() if now is None else now
    if config.get('owner') and open_id == config['owner']:
        return 'owner'
    if open_id in config.get('allow', ()):
        return 'allowed'
    claim = config.get('claim_until')
    if not config.get('owner') and claim is not None and now < claim:
        return 'claim'
    return 'denied'
