"""Bounded Feishu / Lark application-bot API client.

Only fixed error categories and numeric API codes leave this module; response
bodies, tokens and server messages are never logged, saved or raised.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
from pathlib import Path
import stat
import threading
import time
from urllib import error, parse, request

API_BASES = frozenset({'https://open.feishu.cn', 'https://open.larksuite.com'})
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CARD_BYTES = 29 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
TOKEN_ERRORS = frozenset({99991661, 99991663, 99991664, 99991668})
PERMISSION_ERRORS = frozenset({99991672, 99991676, 99991679, 230027})
RATE_LIMIT_ERRORS = frozenset({230020})
# The tenant's monthly API quota is used up: nothing helps until the 1st.
QUOTA_ERRORS = frozenset({99991403})


def _api_code(raw) -> int | None:
    try:
        code = json.loads(raw).get('code')
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    return code if type(code) is int else None


class DeliveryError(Exception):
    """A fixed error category, an optional API code and whether a write may have landed."""

    def __init__(self, code, *, api_code=None, uncertain=False):
        self.code = code
        self.api_code = api_code
        self.uncertain = bool(uncertain)
        super().__init__(code)


def load_config(path) -> dict:
    """Read the private bot config (must be a regular file not readable by others)."""
    path = Path(path).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise DeliveryError('config_permissions')
            data = source.read(16 * 1024 + 1)
        if len(data) > 16 * 1024:
            raise DeliveryError('config_format')
        config = json.loads(data)
    except DeliveryError:
        raise
    except FileNotFoundError:
        raise DeliveryError('config_missing') from None
    except (OSError, ValueError, TypeError):
        raise DeliveryError('config_format') from None
    return validate_config(config)


def validate_config(config) -> dict:
    if not isinstance(config, dict):
        raise DeliveryError('config_format')
    result = {key: config.get(key) for key in ('app_id', 'app_secret', 'chat_id')}
    if any(
        not isinstance(result[key], str) or not result[key].strip()
        for key in ('app_id', 'app_secret')
    ):
        raise DeliveryError('config_credentials')
    base_url = config.get('base_url', 'https://open.feishu.cn')
    if not isinstance(base_url, str) or base_url.rstrip('/') not in API_BASES:
        raise DeliveryError('config_url')
    result['base_url'] = base_url.rstrip('/')
    if result['chat_id'] is not None and not isinstance(result['chat_id'], str):
        raise DeliveryError('config_chat')
    return result


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(method, url, headers, body, timeout) -> dict:
    """One HTTPS JSON exchange; writes that time out are marked uncertain."""
    write = method in ('POST', 'PATCH')
    encoded = None
    if isinstance(body, bytes):
        encoded = body  # an already encoded body (multipart upload)
    elif body is not None:
        encoded = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    req = request.Request(url, data=encoded, headers=headers, method=method)
    try:
        with request.build_opener(_NoRedirect).open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except error.HTTPError as exc:
        try:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
        finally:
            exc.close()
        if exc.code == 429:
            code = _api_code(raw)
            if code in QUOTA_ERRORS:
                raise DeliveryError('quota_exhausted', api_code=code) from None
            raise DeliveryError('rate_limited', api_code=code) from None
        if exc.code >= 500:
            raise DeliveryError('server_error', uncertain=write) from None
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DeliveryError('http_error') from None
        if (
            not isinstance(result, dict)
            or not isinstance(result.get('code'), int)
            or result['code'] == 0
        ):
            raise DeliveryError('http_error') from None
        return result
    except TimeoutError:
        raise DeliveryError('timeout', uncertain=write) from None
    except error.URLError as exc:
        timed_out = isinstance(exc.reason, TimeoutError)
        raise DeliveryError('timeout' if timed_out else 'network_error', uncertain=write) from None
    except (OSError, http.client.HTTPException):
        raise DeliveryError('network_error', uncertain=write) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DeliveryError('invalid_response', uncertain=write)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise DeliveryError('invalid_response', uncertain=write) from None
    if not isinstance(result, dict):
        raise DeliveryError('invalid_response', uncertain=write)
    return result


def card_title(card) -> str | None:
    if not isinstance(card, dict):
        return None
    header = card.get('header')
    title = header.get('title') if isinstance(header, dict) else None
    content = title.get('content') if isinstance(title, dict) else None
    return content if isinstance(content, str) and content else None


class FeishuClient:
    def __init__(self, config, *, request_json=request_json, timeout=12, clock=time.monotonic):
        self._config = validate_config(config)
        self._request_json = request_json
        self.timeout = timeout
        self._clock = clock
        self._token = None
        self._token_expires = 0
        self._lock = threading.RLock()

    @property
    def chat_id(self):
        return self._config['chat_id']

    @property
    def app_fingerprint(self) -> str:
        """A short digest naming the bot (the app id itself is never stored)."""
        return hashlib.sha256(self._config['app_id'].encode('utf-8')).hexdigest()[:16]

    def _request(self, method, path, body=None, *, token=None, content_type=None) -> dict:
        write = method in ('POST', 'PATCH')
        headers = {'Content-Type': content_type or 'application/json; charset=utf-8'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            result = self._request_json(
                method, self._config['base_url'] + '/open-apis' + path, headers, body, self.timeout
            )
        except DeliveryError:
            raise
        except Exception:
            raise DeliveryError('transport_error', uncertain=write) from None
        if not isinstance(result, dict) or not isinstance(result.get('code'), int):
            raise DeliveryError('invalid_response', uncertain=write)
        code = result['code']
        if code:
            if code in TOKEN_ERRORS:
                category = 'token_invalid'
            elif code in PERMISSION_ERRORS:
                category = 'permission_denied'
            elif code in RATE_LIMIT_ERRORS:
                category = 'rate_limited'
            elif code in QUOTA_ERRORS:
                category = 'quota_exhausted'
            else:
                category = 'api_error'
            raise DeliveryError(category, api_code=code)
        return result

    def _get_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and now < self._token_expires:
                return self._token
            result = self._request(
                'POST',
                '/auth/v3/tenant_access_token/internal',
                {'app_id': self._config['app_id'], 'app_secret': self._config['app_secret']},
            )
            token, expire = result.get('tenant_access_token'), result.get('expire')
            if (
                not isinstance(token, str)
                or not token
                or not isinstance(expire, (int, float))
                or expire <= 0
            ):
                raise DeliveryError('invalid_token_response')
            self._token, self._token_expires = token, now + max(1, expire - 60)
            return self._token

    def _api(self, method, path, body=None, *, content_type=None) -> dict:
        for attempt in range(2):
            token = self._get_token()
            try:
                return self._request(method, path, body, token=token, content_type=content_type)
            except DeliveryError as exc:
                if exc.code != 'token_invalid' or attempt:
                    raise
                with self._lock:
                    self._token = None
                    self._token_expires = 0
        raise DeliveryError('token_invalid')

    def _pages(self, path, query, *, limit=100):
        """Iterate the ``items`` of a paginated listing, guarding the cursor."""
        page_token, seen = None, set()
        while True:
            params = dict(query)
            if page_token:
                params['page_token'] = page_token
            data = self._api('GET', path + '?' + parse.urlencode(params)).get('data')
            if not isinstance(data, dict) or not isinstance(data.get('items', []), list):
                raise DeliveryError('invalid_response')
            yield from data.get('items', [])
            if not data.get('has_more'):
                return
            page_token = data.get('page_token')
            if not isinstance(page_token, str) or not page_token or page_token in seen:
                raise DeliveryError('invalid_pagination')
            seen.add(page_token)
            if len(seen) > limit:
                raise DeliveryError('pagination_limit')

    def upload_image(self, data: bytes, filename: str, content_type: str) -> str:
        """Upload a message image; return its ``image_key`` (usable in cards)."""
        if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
            raise DeliveryError('invalid_image')
        boundary = secrets.token_hex(16)
        body = b''.join([
            f'--{boundary}\r\nContent-Disposition: form-data; name="image_type"\r\n\r\n'
            'message\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode(),
            data,
            f'\r\n--{boundary}--\r\n'.encode(),
        ])  # fmt: skip
        result = self._api(
            'POST', '/im/v1/images', body, content_type=f'multipart/form-data; boundary={boundary}'
        )
        data_ = result.get('data')
        key = data_.get('image_key') if isinstance(data_, dict) else None
        if not isinstance(key, str) or not key.startswith('img_') or len(key) > 128:
            raise DeliveryError('invalid_response')
        return key

    def list_chats(self) -> list[dict]:
        """Chats the bot belongs to: only ``chat_id`` and a bounded ``name``."""
        return [
            {'chat_id': item['chat_id'], 'name': str(item.get('name', ''))[:200]}
            for item in self._pages('/im/v1/chats', {'page_size': 100})
            if isinstance(item, dict) and isinstance(item.get('chat_id'), str)
        ]

    @staticmethod
    def _content(card) -> str:
        if not isinstance(card, dict):
            raise DeliveryError('invalid_card')
        content = json.dumps(card, ensure_ascii=False, separators=(',', ':'))
        if len(content.encode('utf-8')) > MAX_CARD_BYTES:
            raise DeliveryError('card_too_large')
        return content

    def send_card(self, chat_id, card, uuid) -> str:
        if not isinstance(chat_id, str) or not chat_id.startswith('oc_'):
            raise DeliveryError('config_chat')
        if not isinstance(uuid, str) or not uuid or len(uuid) > 50:
            raise DeliveryError('invalid_uuid')
        result = self._api(
            'POST',
            '/im/v1/messages?receive_id_type=chat_id',
            {
                'receive_id': chat_id,
                'msg_type': 'interactive',
                'content': self._content(card),
                'uuid': uuid,
            },
        )
        data = result.get('data')
        message_id = data.get('message_id') if isinstance(data, dict) else None
        if not isinstance(message_id, str) or not message_id.startswith('om_'):
            raise DeliveryError('invalid_response', uncertain=True)
        return message_id

    def update_card(self, message_id, card) -> None:
        if not isinstance(message_id, str) or not message_id.startswith('om_'):
            raise DeliveryError('invalid_message_id')
        self._api(
            'PATCH',
            '/im/v1/messages/' + parse.quote(message_id, safe=''),
            {'content': self._content(card)},
        )

    def find_card(self, chat_id, card, *, since) -> str | None:
        """Recover an uncertain create from live chat history, never by a new UUID.

        Daily cards carry a unique date-bearing title. Only this application's
        cards with exactly that title count; ambiguous matches fail.
        """
        title = card_title(card)
        if title is None:
            raise DeliveryError('card_lookup_unavailable')
        query = {
            'container_id_type': 'chat',
            'container_id': chat_id,
            'start_time': str(max(0, int(since) - 60)),
            'sort_type': 'ByCreateTimeDesc',
            'page_size': 50,
            'card_msg_content_type': 'user_card_content',
        }
        matches = set()
        for item in self._pages('/im/v1/messages', query, limit=20):
            if (
                not isinstance(item, dict)
                or item.get('deleted')
                or item.get('msg_type') != 'interactive'
            ):
                continue
            sender = item.get('sender')
            if not isinstance(sender, dict) or sender.get('id') != self._config['app_id']:
                continue
            body = item.get('body')
            content = body.get('content') if isinstance(body, dict) else None
            try:
                original = json.loads(content) if isinstance(content, str) else content
            except (ValueError, TypeError):
                continue
            message_id = item.get('message_id')
            if (
                card_title(original) == title
                and isinstance(message_id, str)
                and message_id.startswith('om_')
            ):
                matches.add(message_id)
        if len(matches) > 1:
            raise DeliveryError('card_lookup_ambiguous')
        return next(iter(matches), None)
