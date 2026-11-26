"""The board's settings: ``dashboard.json`` in the console directory (0600).

``public_url`` is the HTTPS address people open. Feishu redirects back to
``<public_url>/auth/callback``, which must be listed as a redirect URL of the
app. ``app`` names a private ``lark_app.json`` whose ``app_id`` and
``app_secret`` sign people in. Only the owner and the open_ids in ``allow``
may view the board. While no owner is set, the first person to sign in before
``claim_until`` becomes the owner. ``secret`` signs the session cookies, so
changing it signs everyone out.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

NAME = 'dashboard.json'
DEFAULT_PORT = 8790
CLAIM_WINDOW = 24 * 3600.0
MAX_BYTES = 64 * 1024
OPEN_ID = re.compile(r'ou_[A-Za-z0-9_-]{8,64}')
TUNNEL_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')


class ConfigError(ValueError):
    pass


def path_for(console_dir) -> Path:
    return Path(console_dir) / NAME


def normalize_url(value) -> str:
    """``https://host[:port]`` with no path, or '' when not set yet."""
    if value in (None, ''):
        return ''
    if not isinstance(value, str):
        raise ConfigError('public_url 必须是字符串')
    parts = urlsplit(value.strip())
    if parts.scheme != 'https' or not parts.hostname:
        raise ConfigError('public_url 必须是 https:// 开头的地址')
    if parts.path not in ('', '/') or parts.query or parts.fragment or parts.username:
        raise ConfigError('public_url 只写到域名（和端口），不带路径')
    host = parts.hostname.lower()
    return f'https://{host}' + (f':{parts.port}' if parts.port else '')


def _validate(data) -> dict:
    if not isinstance(data, dict):
        raise ConfigError('dashboard.json 必须是一个 JSON 对象')
    port = data.get('port', DEFAULT_PORT)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ConfigError('port 必须是 1024–65535 之间的整数')
    secret = data.get('secret')
    if not isinstance(secret, str) or len(secret) < 32:
        raise ConfigError('secret 缺失或太短')
    app = data.get('app') or ''
    if not isinstance(app, str):
        raise ConfigError('app 必须是 lark_app.json 的路径')
    owner = data.get('owner')
    if owner is not None and (not isinstance(owner, str) or not OPEN_ID.fullmatch(owner)):
        raise ConfigError('owner 必须是 open_id（ou_ 开头）')
    allow = data.get('allow', [])
    if not isinstance(allow, list) or not all(
        isinstance(item, str) and OPEN_ID.fullmatch(item) for item in allow
    ):
        raise ConfigError('allow 必须是 open_id 列表')
    claim = data.get('claim_until')
    if claim is not None and (type(claim) not in (int, float) or claim < 0):
        raise ConfigError('claim_until 必须是时间戳')
    tunnel = data.get('tunnel')
    if tunnel is not None:
        if not isinstance(tunnel, dict) or not isinstance(tunnel.get('config'), str):
            raise ConfigError('tunnel 必须写明 cloudflared 配置文件 config')
        if tunnel.get('binary') is not None and not isinstance(tunnel['binary'], str):
            raise ConfigError('tunnel.binary 必须是路径')
        if tunnel.get('name') is not None and not TUNNEL_NAME.fullmatch(str(tunnel['name'])):
            raise ConfigError('tunnel.name 只能用字母、数字、- 和 _')
    notify = data.get('notify')
    if notify is not None:
        from .notify import valid_hook

        if not isinstance(notify, dict) or not valid_hook(notify.get('webhook')):
            raise ConfigError('notify.webhook 必须是飞书群机器人的 webhook 地址')
        if notify.get('secret') is not None and not isinstance(notify['secret'], str):
            raise ConfigError('notify.secret 必须是字符串')
        notify = {'webhook': notify['webhook'], 'secret': notify.get('secret') or None}
    owner_name = data.get('owner_name')
    return {
        'enabled': data.get('enabled', True) is not False,
        'port': port,
        'public_url': normalize_url(data.get('public_url')),
        'app': app,
        'owner': owner,
        'owner_name': owner_name if isinstance(owner_name, str) else None,
        'allow': list(dict.fromkeys(allow)),
        'claim_until': claim,
        'secret': secret,
        'tunnel': dict(tunnel) if tunnel else None,
        'notify': notify,
    }


def load(path) -> dict:
    """The validated settings. Raises ConfigError (and FileNotFoundError when missing)."""
    path = Path(path).expanduser()
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as source:
        if os.fstat(source.fileno()).st_mode & 0o077:
            raise ConfigError('dashboard.json 的权限必须是 0600')
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ConfigError('dashboard.json 太大')
    try:
        data = json.loads(raw)
    except ValueError:
        raise ConfigError('dashboard.json 不是合法的 JSON') from None
    return _validate(data)


def save(path, config) -> None:
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = _validate(config)
    for key in ('tunnel', 'notify'):
        if data.get(key) is None:
            data.pop(key)
    temporary = path.with_name(f'.{path.name}.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write('\n')
    os.replace(temporary, path)


def setup(path, *, public_url=None, app=None, port=None, tunnel=None, now=None) -> dict:
    """Create or update the settings; a new file gets its own secret and claim window."""
    now = time.time() if now is None else now
    try:
        config = load(path)
    except FileNotFoundError:
        config = {
            'enabled': True,
            'port': DEFAULT_PORT,
            'public_url': '',
            'app': '',
            'owner': None,
            'allow': [],
            'claim_until': None,
            'secret': secrets.token_hex(32),
            'tunnel': None,
        }
    if public_url is not None:
        config['public_url'] = normalize_url(public_url)
    if app is not None:
        config['app'] = str(Path(app).expanduser())
    if port is not None:
        config['port'] = port
    if tunnel is not None:
        config['tunnel'] = tunnel or None
    if config.get('owner') is None:
        config['claim_until'] = now + CLAIM_WINDOW
    save(path, config)
    return load(path)


def summary(config) -> list[str]:
    """Readable lines for the command line; never the secret."""
    claim = config.get('claim_until')
    owner = config.get('owner')
    if owner:
        who = f"{config.get('owner_name') or '已设置'}（{owner[:6]}…）"
    elif claim and claim > time.time():
        who = '未设置：' + time.strftime('%m-%d %H:%M', time.localtime(claim)) + ' 前第一个登录的人成为管理员'
    else:
        who = '未设置，认领已过期（运行 dashboard setup 重新开放 24 小时）'
    tunnel = config.get('tunnel')
    return [
        f"看板：{'启用' if config['enabled'] else '停用'}，本机端口 {config['port']}",
        f"公网地址：{config['public_url'] or '未设置'}",
        f"飞书应用配置：{config['app'] or '未设置'}",
        f'管理员：{who}',
        f"白名单：{len(config['allow'])} 人",
        f"隧道：{(tunnel.get('name') or tunnel['config']) if tunnel else '未配置'}",
        '申请通知：'
        + (
            ('群机器人（已开签名校验）' if config['notify'].get('secret') else '群机器人')
            if config.get('notify')
            else '未配置（run.py dashboard webhook）'
        ),
    ]
