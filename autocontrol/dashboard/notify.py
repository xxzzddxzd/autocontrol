"""Tell the owner in Feishu through a group custom bot (webhook).

Feishu does not count custom-bot webhook messages against the tenant's monthly
API quota. Such a bot can only open links from its cards, so the buttons open
the board's approval page, where the owner confirms with one more tap. The
optional signing secret is the bot's 签名校验 key. The webhook address is never
logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from urllib import error, request

HOOK = re.compile(
    r'https://open\.(feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[A-Za-z0-9_-]{8,128}'
)


class NotifyError(Exception):
    pass


def valid_hook(value) -> bool:
    return isinstance(value, str) and HOOK.fullmatch(value) is not None


def sign(secret: str, timestamp: int) -> str:
    """Feishu's custom-bot signature: HMAC-SHA256 keyed by "timestamp\\nsecret" over nothing."""
    key = f'{timestamp}\n{secret}'.encode('utf-8')
    return base64.b64encode(hmac.new(key, b'', hashlib.sha256).digest()).decode('ascii')


def request_card(name: str, asked: str, approve_url: str, deny_url: str, members_url: str) -> dict:
    """A card asking the owner to let someone see the board (custom bots open links only)."""

    def button(text, url, kind='default'):
        return {'tag': 'button', 'text': {'tag': 'plain_text', 'content': text}, 'type': kind,
                'multi_url': {'url': url, 'pc_url': url, 'android_url': url, 'ios_url': url}}  # fmt: skip

    return {
        'config': {'wide_screen_mode': True},
        'header': {'template': 'orange', 'title': {'tag': 'plain_text', 'content': '看板访问申请'}},
        'elements': [
            {'tag': 'div', 'text': {'tag': 'lark_md', 'content': f'**{_md(name)}** 想查看自动执行看板。'}},
            {'tag': 'note', 'elements': [{'tag': 'plain_text', 'content': f'申请时间 {asked} · 点按钮后在看板里确认'}]},
            {'tag': 'action', 'actions': [
                button('批准', approve_url, 'primary'),
                button('拒绝', deny_url, 'danger'),
                button('成员管理', members_url),
            ]},
        ],
    }  # fmt: skip


def text_message(text: str) -> dict:
    return {'msg_type': 'text', 'content': {'text': text}}


def _md(text: str) -> str:
    return re.sub(r'([*_~`<>\[\]])', r'\\\1', text or '')


def send(
    hook: str, payload: dict, *, secret=None, timeout=10.0, opener=None, clock=time.time
) -> None:
    """POST one message to the custom bot; raises NotifyError with a short reason."""
    if not valid_hook(hook):
        raise NotifyError('invalid_webhook')
    body = dict(payload)
    if 'card' not in body and 'content' not in body:
        body = {'msg_type': 'interactive', 'card': payload}
    if secret:
        stamp = int(clock())
        body.update(timestamp=str(stamp), sign=sign(secret, stamp))
    req = request.Request(
        hook,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        method='POST',
        headers={'Content-Type': 'application/json; charset=utf-8'},
    )
    try:
        with (opener or request.urlopen)(req, timeout=timeout) as response:
            raw = response.read(64 * 1024)
    except error.HTTPError as exc:
        raise NotifyError(f'http_{exc.code}') from None
    except (OSError, ValueError) as exc:
        raise NotifyError(type(exc).__name__) from None
    try:
        result = json.loads(raw)
    except ValueError:
        raise NotifyError('invalid_response') from None
    code = result.get('code', result.get('StatusCode')) if isinstance(result, dict) else None
    if code != 0:
        raise NotifyError(f'code_{code}')
