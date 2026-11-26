"""The board's HTTP service: routes, Feishu sign-in, the session cookie, members.

:class:`App` turns a request into a :class:`Response` without touching a
socket (tests drive it directly); :func:`serve` puts it behind a threaded
HTTP server on 127.0.0.1. Everything except ``/healthz`` and the sign-in
routes needs a signed session cookie from a permitted Feishu user.

Someone turned away is recorded as a request, and the owner hears about it in
Feishu through a group custom bot (see :mod:`.notify`). The card's buttons
open ``/admin/requests/<id>``, where the owner confirms with a POST. The
members page ``/admin`` lists requests and everyone who may look. Every
change is a POST that carries a token tied to the owner's session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from . import auth, config as settings, data, notify, pages, people
from ..schedule import BEIJING

SESSION_COOKIE = '__Host-board'
LOGIN_COOKIE = '__Host-board-login'
PROJECT_PATH = re.compile(r'/p/([a-z][a-z0-9_-]{0,31})')
REQUEST_PATH = re.compile(r'/admin/requests/([A-Za-z0-9_-]{8,40})')
DAY = re.compile(r'\d{4}-\d{2}-\d{2}')
MAX_URL = 2048
MAX_BODY = 8192


@dataclass
class Response:
    status: int = 200
    body: bytes = b''
    headers: list = field(default_factory=list)
    content_type: str = 'text/html; charset=utf-8'


class App:
    def __init__(self, config_path, *, source=None, dev=False, fetch=auth.fetch_json,
                 clock=time.time, tz=BEIJING, notifier=notify.send, members=None,
                 background=True):  # fmt: skip
        self.config_path = Path(config_path)
        self.source = source or data.Source(self.config_path.parent / 'projects.json')
        self.dev = dev
        self.fetch = fetch
        self.clock = clock
        self.tz = tz
        self.notifier = notifier
        self.background = background  # tests send notifications inline
        self.people = members or people.People(self.config_path.parent / people.NAME, clock=clock)
        self.logins = auth.Logins()
        self._config = None
        self._stamp = None
        self._dev_secret = secrets.token_hex(32)
        self._lock = threading.Lock()

    # -- settings ----------------------------------------------------------------

    def config(self) -> dict:
        """The settings, re-read when the file changes (so edits need no restart)."""
        try:
            info = self.config_path.stat()
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = None
        with self._lock:
            if stamp != self._stamp or self._config is None:
                try:
                    self._config = settings.load(self.config_path)
                except (OSError, settings.ConfigError) as error:
                    self._config = {'error': str(error)}
                    if self.dev:
                        # A local preview works before the board is set up.
                        self._config = {'port': settings.DEFAULT_PORT, 'public_url': '', 'app': '',
                                        'owner': None, 'allow': [], 'claim_until': None,
                                        'secret': self._dev_secret, 'enabled': True,
                                        'notify': None}  # fmt: skip
                self._stamp = stamp
            return self._config

    def _update(self, change) -> bool:
        """Apply ``change(settings)`` to the file under the lock; True when saved."""
        with self._lock:
            try:
                current = settings.load(self.config_path)
            except (OSError, settings.ConfigError):
                return False
            if change(current) is False:
                return False
            settings.save(self.config_path, current)
            self._stamp = None
        return True

    def _claim(self, user) -> bool:
        """Make the first person to sign in the owner (once, inside the window)."""
        claimed = []

        def change(current):
            verdict = auth.access(current, user['open_id'], now=self.clock())
            if verdict != 'claim':
                claimed.append(verdict in ('owner', 'allowed'))
                return False
            current.update(owner=user['open_id'], owner_name=user['name'], claim_until=None)
            claimed.append(True)

        saved = self._update(change)
        if saved:
            _log(f'看板管理员已设置：{user["name"]}')
        return bool(claimed and claimed[0])

    # -- requests ------------------------------------------------------------------

    def handle(self, method: str, target: str, headers, body: bytes = b'') -> Response:
        if len(target) > MAX_URL:
            return self._text(414, 'URI too long')
        url = urlsplit(target)
        path, query = url.path, parse_qs(url.query, max_num_fields=20)
        if method not in ('GET', 'HEAD', 'POST'):
            return self._text(405, 'method not allowed', [('Allow', 'GET, HEAD, POST')])
        if path == '/healthz':
            return self._text(200, 'ok')
        config = self.config()
        if not self._host_ok(headers.get('Host', ''), config):
            return self._text(421, 'misdirected request')
        if path == '/favicon.ico':
            return Response(204, b'', content_type='image/x-icon')
        if 'error' in config:
            return self._message(
                503, '看板设置有误', [config['error'], '检查 ~/.config/autocontrol/console/dashboard.json。']
            )
        if method == 'POST':
            return self._post(path, headers, body, config)
        if path == '/login':
            return self._login(query, config)
        if path == '/auth/callback':
            return self._callback(query, headers, config)
        if path == '/logout':
            response = self._message(200, '已退出', ['下次打开看板会重新用飞书登录。'], link='/login', label='重新登录')
            response.headers.append(('Set-Cookie', _cookie(SESSION_COOKIE, '', 0)))
            return response
        user = self._user(headers, config)
        partial = headers.get('X-Board-Partial') == '1'
        if user is None:
            if partial:
                return self._text(401, 'sign in again')
            return _redirect('/login?next=' + quote(target, safe=''))
        if path == '/':
            return self._overview(user, partial)
        match = PROJECT_PATH.fullmatch(path)
        if match:
            day = (query.get('day') or [None])[0]
            if day is not None and not DAY.fullmatch(day):
                day = None
            return self._project(match[1], day, user, partial)
        if path == '/admin' or REQUEST_PATH.fullmatch(path):
            if user['role'] != 'owner':
                return self._message(403, '只有管理员能管理成员', [], link='/', label='回到看板')
            if path == '/admin':
                return self._members(user, config)
            verb = (query.get('do') or ['approve'])[0]
            return self._confirm(REQUEST_PATH.fullmatch(path)[1], verb, user)
        return self._message(404, '没有这个页面', ['链接可能已经过期。'], link='/', label='回到看板')

    # -- sign-in -------------------------------------------------------------------

    def _login(self, query, config) -> Response:
        if self.dev:
            return _redirect('/')
        if not config.get('public_url') or not config.get('app'):
            return self._message(503, '看板还没配置好', ['需要先设置公网地址和飞书应用（run.py dashboard setup）。'])
        try:
            from ..lark.client import load_config

            app = load_config(config['app'])
        except Exception as error:
            return self._message(503, '飞书应用配置不可用', [type(error).__name__ + ': ' + str(error)])
        state, challenge = self.logins.begin(_safe_next((query.get('next') or ['/'])[0]))
        url = auth.authorize_url(app, config['public_url'] + '/auth/callback', state, challenge)
        response = _redirect(url)
        response.headers.append(('Set-Cookie', _cookie(LOGIN_COOKIE, state, int(auth.STATE_TTL))))
        return response

    def _callback(self, query, headers, config) -> Response:
        state = (query.get('state') or [''])[0]
        if (query.get('error') or [None])[0]:
            return self._message(403, '没有完成授权', ['你在飞书授权页取消了登录。'], link='/login', label='重新登录')
        browser = _cookies(headers).get(LOGIN_COOKIE)
        pending = (
            self.logins.finish(state)
            if browser and secrets.compare_digest(browser, state)
            else None
        )
        if pending is None:
            return self._message(400, '登录已过期', ['请重新打开看板登录。'], link='/login', label='重新登录')
        verifier, next_path = pending
        try:
            from ..lark.client import load_config

            app = load_config(config['app'])
            user = auth.sign_in(
                app, (query.get('code') or [''])[0], verifier, config['public_url'] + '/auth/callback',
                fetch=self.fetch,
            )  # fmt: skip
        except auth.AuthError as error:
            _log(f'飞书登录失败：{error.code}')
            return self._message(
                502, '飞书登录失败', [f'步骤：{error.code}', '稍后再试一次。'], link='/login', label='重新登录'
            )
        except Exception as error:
            return self._message(503, '飞书应用配置不可用', [type(error).__name__])
        verdict = auth.access(config, user['open_id'], now=self.clock())
        if verdict == 'claim':
            verdict = 'owner' if self._claim(user) else 'denied'
        if verdict == 'denied':
            response = self._turned_away(user, self.config())
            response.headers.append(('Set-Cookie', _cookie(LOGIN_COOKIE, '', 0)))
            return response
        try:
            self.people.seen(user['open_id'], user['name'])
        except OSError:
            pass  # names on the members page are a convenience
        secret = self.config()['secret']
        response = _redirect(next_path)
        response.headers.append(('Set-Cookie', _cookie(LOGIN_COOKIE, '', 0)))
        response.headers.append(
            ('Set-Cookie', _cookie(SESSION_COOKIE, auth.make_session(user, secret, now=self.clock()),
                                   auth.SESSION_DAYS * 86400))  # fmt: skip
        )
        return response

    def _turned_away(self, user, config) -> Response:
        """Record the request, tell the owner (at most daily), and say what happens next."""
        try:
            item, tell = self.people.ask(user['open_id'], user['name'])
        except OSError:
            item, tell = {'status': 'pending'}, False
        _log(f'看板拒绝登录：{user["name"]}（已记为申请）')
        if item.get('status') == 'denied':
            lines = [f'{user["name"]}，管理员没有批准你查看看板。']
        else:
            if tell and config.get('notify') and config.get('public_url'):
                self._tell_owner(item, config)
            lines = [
                f'{user["name"]}，你还没有查看权限。',
                '已经通知管理员，批准后重新打开看板就行。'
                if config.get('notify')
                else '已经记下你的申请，管理员在看板「成员」页批准后，重新打开看板就行。',
            ]
        return self._message(403, '没有查看权限', lines)

    def _tell_owner(self, item, config) -> None:
        base = config['public_url']
        card = notify.request_card(
            item.get('name') or '未命名',
            pages._when(item.get('asked'), self.tz),
            f'{base}/admin/requests/{item["id"]}?do=approve',
            f'{base}/admin/requests/{item["id"]}?do=deny',
            f'{base}/admin',
        )
        hook, secret = config['notify']['webhook'], config['notify'].get('secret')

        def send():
            try:
                self.notifier(hook, card, secret=secret)
                _log(f'已在飞书提醒管理员：{item.get("name")} 申请查看')
            except notify.NotifyError as error:
                _log(f'飞书提醒发送失败：{error}')
            except Exception as error:
                _log(f'飞书提醒发送失败：{type(error).__name__}')

        if self.background:
            threading.Thread(target=send, name='board-notify', daemon=True).start()
        else:
            send()

    def _user(self, headers, config) -> dict | None:
        """The signed-in, still permitted user: ``{'sub', 'name', 'exp', 'role'}``, or None."""
        if self.dev:
            return {'sub': 'dev', 'name': '本机预览', 'exp': 0, 'role': 'owner'}
        session = auth.read_session(
            _cookies(headers).get(SESSION_COOKIE), config['secret'], now=self.clock()
        )
        if session is None:
            return None
        verdict = auth.access(config, session['sub'], now=self.clock())
        if verdict not in ('owner', 'allowed'):
            return None
        return {**session, 'name': session.get('name') or '已登录', 'role': verdict}

    # -- members ---------------------------------------------------------------------

    def _csrf(self, user, config) -> str:
        message = f'csrf|{user["sub"]}|{user.get("exp", 0)}'.encode('utf-8')
        return hmac.new(config['secret'].encode('utf-8'), message, hashlib.sha256).hexdigest()[:40]

    def _members(self, user, config) -> Response:
        snapshot = self.people.snapshot()
        requests = sorted(
            (
                dict(item, id=key)
                for key, item in snapshot['requests'].items()
                if isinstance(item, dict)
            ),
            key=lambda item: item.get('last') or item.get('asked') or 0,
            reverse=True,
        )
        allowed = [
            {'open_id': open_id, 'name': self.people.name(open_id) or f'{open_id[:8]}…'}
            for open_id in config.get('allow', ())
        ]
        main = pages.members_main(
            owner=config.get('owner_name'), allowed=allowed, requests=requests,
            notify=bool(config.get('notify')), csrf=self._csrf(user, config), tz=self.tz,
        )  # fmt: skip
        return self._plain_page('成员 · 自动执行看板', main, user)

    def _confirm(self, key, verb, user) -> Response:
        item = self.people.get(key)
        if item is None:
            return self._message(404, '没有这条申请', ['可能已经过期，去「成员」页看看。'], link='/admin', label='成员管理')
        verb = 'deny' if verb == 'deny' else 'approve'
        main = pages.confirm_main(item, verb, self._csrf(user, self.config()))
        return self._plain_page('审批 · 自动执行看板', main, user)

    def _post(self, path, headers, body, config) -> Response:
        user = self._user(headers, config)
        if user is None:
            return self._message(401, '登录已过期', ['请重新打开看板登录。'], link='/login', label='重新登录')
        if user['role'] != 'owner':
            return self._message(403, '只有管理员能管理成员', [], link='/', label='回到看板')
        if len(body) > MAX_BODY:
            return self._text(413, 'too large')
        form = {key: values[0] for key, values in parse_qs(body.decode('utf-8', 'replace'),
                                                             max_num_fields=10).items()}  # fmt: skip
        if not secrets.compare_digest(form.get('csrf', ''), self._csrf(user, config)):
            return self._message(403, '页面已过期', ['请回到「成员」页重新操作。'], link='/admin', label='成员管理')
        match = REQUEST_PATH.fullmatch(path)
        if match and form.get('do') in ('approve', 'deny'):
            return self._decide(match[1], form['do'])
        if path == '/admin/people' and form.get('do') == 'remove':
            return self._remove(form.get('open_id', ''))
        return self._message(404, '没有这个操作', [], link='/admin', label='成员管理')

    def _decide(self, key, verb) -> Response:
        item = self.people.get(key)
        if item is None:
            return self._message(404, '没有这条申请', [], link='/admin', label='成员管理')
        open_id, name = item.get('open_id', ''), item.get('name') or '未命名'
        if verb == 'approve':

            def allow(current):
                if open_id == current.get('owner'):
                    return False
                current['allow'] = (
                    [*current['allow'], open_id]
                    if open_id not in current['allow']
                    else current['allow']
                )

            self._update(allow)
            self.people.decide(key, 'approved')
            _log(f'看板批准：{name}')
            return self._message(
                200, f'已批准 {name}', ['对方重新打开看板就能看到了。'], link='/admin', label='成员管理'
            )

        def refuse(current):
            if open_id not in current['allow']:
                return False
            current['allow'] = [item for item in current['allow'] if item != open_id]

        self._update(refuse)
        self.people.decide(key, 'denied')
        _log(f'看板拒绝申请：{name}')
        return self._message(200, f'已拒绝 {name}', ['七天内对方再申请也不会提醒你。'], link='/admin', label='成员管理')

    def _remove(self, open_id) -> Response:
        name = self.people.name(open_id) or '这个人'

        def change(current):
            if open_id not in current['allow']:
                return False
            current['allow'] = [item for item in current['allow'] if item != open_id]

        removed = self._update(change)
        if removed:
            _log(f'看板移出：{name}')
        lines = ['已经立即生效，对方再打开会重新申请。'] if removed else ['白名单里没有这个人。']
        return self._message(
            200, f'已移出 {name}' if removed else '没有变化', lines, link='/admin', label='成员管理'
        )

    # -- pages ---------------------------------------------------------------------

    def _overview(self, user, partial) -> Response:
        entries, problem = self.source.entries()
        console = self.source.console()
        now = datetime.now(self.tz)
        boards = [self.source.board(entry, now=now) for entry in entries]
        main = pages.overview_main(boards, console)
        if problem:
            main = f'<div class="note">登记表无法读取：{pages.escape(problem)}</div>' + main
        return self._page('自动执行看板', main, user, console, partial)

    def _project(self, name, day, user, partial) -> Response:
        entries, _ = self.source.entries()
        entry = next((item for item in entries if item['name'] == name), None)
        if entry is None:
            return self._message(404, '没有这个项目', [name], link='/', label='回到看板')
        console = self.source.console()
        board = self.source.board(entry, day, now=datetime.now(self.tz))
        project = board.get('project')
        rounds = self.source.rounds(project) if project is not None else []
        slots = console.get('projects') if isinstance(console.get('projects'), dict) else {}
        main = pages.project_main(board, rounds=rounds, log=self.source.log_tail(name),
                                  slot=slots.get(name) if console.get('running') else None, tz=self.tz)  # fmt: skip
        return self._page(f'{board["title"]} · 自动执行看板', main, user, console, partial)

    def _page(self, title, main, user, console, partial) -> Response:
        stamp = datetime.now(self.tz).strftime('%H:%M:%S')
        if partial:
            response = Response(200, main.encode('utf-8'))
            response.headers.append(('X-Board-Time', stamp))
            return response
        nonce = secrets.token_urlsafe(16)
        note = ''
        try:
            cards = self.source.cards()
            if not cards['on']:
                note = '<div class="note">飞书卡片已停发，进度只在这里更新。</div>'
            elif cards['paused_until']:
                until = datetime.fromtimestamp(cards['paused_until'], self.tz).strftime(
                    '%m-%d %H:%M'
                )
                note = f'<div class="note">飞书本月额度已用完，卡片暂停到 {until} 自动恢复；' '这里照常更新。</div>'
        except Exception:
            pass
        html = pages.page(title, main, nonce=nonce, stamp=stamp, console=console, user=user['name'],
                          note=note, admin=user['role'] == 'owner')  # fmt: skip
        response = Response(200, html.encode('utf-8'))
        response.headers.append(('Content-Security-Policy', _csp(nonce)))
        return response

    def _plain_page(self, title, main, user) -> Response:
        """A page without auto-refresh (forms must not be swapped out from under a tap)."""
        html = pages.page(title, main, nonce='', user=user['name'], admin=user['role'] == 'owner')
        response = Response(200, html.encode('utf-8'))
        response.headers.append(('Content-Security-Policy', _csp(None)))
        return response

    def _message(self, status, title, lines, *, link=None, label=None) -> Response:
        response = Response(
            status, pages.message(title, lines, link=link, label=label).encode('utf-8')
        )
        response.headers.append(('Content-Security-Policy', _csp(None)))
        return response

    @staticmethod
    def _text(status, text, headers=None) -> Response:
        return Response(
            status, text.encode('utf-8'), list(headers or ()), 'text/plain; charset=utf-8'
        )

    def _host_ok(self, host, config) -> bool:
        """Only this machine's address or the public name (no DNS-rebinding tricks)."""
        host = (host or '').lower()
        port = config.get('port', settings.DEFAULT_PORT)
        allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
        public = config.get('public_url') or ''
        if public:
            netloc = urlsplit(public).netloc.lower()
            allowed |= {netloc, f'{netloc}:443'} if ':' not in netloc else {netloc}
        if self.dev:
            return host.startswith(('127.0.0.1:', 'localhost:')) or host in allowed
        return host in allowed


def _now() -> str:
    return datetime.now(BEIJING).strftime('%m-%d %H:%M:%S')


def _log(text: str) -> None:
    print(f'{_now()} {text}', file=sys.stderr, flush=True)


def _csp(nonce) -> str:
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    return (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
        f"script-src {script}; connect-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'self' https://*.feishu.cn https://*.larksuite.com"
    )


def _cookie(name, value, max_age) -> str:
    return f'{name}={value}; Max-Age={max_age}; Path=/; Secure; HttpOnly; SameSite=Lax'


def _cookies(headers) -> dict:
    jar = http_cookies.SimpleCookie()
    try:
        jar.load(headers.get('Cookie', '') or '')
    except http_cookies.CookieError:
        return {}
    return {key: morsel.value for key, morsel in jar.items()}


def _safe_next(target) -> str:
    """Only a path on this site: never an absolute or protocol-relative URL."""
    if (
        not isinstance(target, str)
        or not target.startswith('/')
        or target.startswith('//')
        or '\\' in target
    ):
        return '/'
    return target[:512]


def _redirect(location) -> Response:
    return Response(302, b'', [('Location', location)], 'text/plain; charset=utf-8')


SECURITY_HEADERS = (
    ('X-Content-Type-Options', 'nosniff'),
    ('Referrer-Policy', 'no-referrer'),
    ('Cache-Control', 'no-store'),
)


class _Handler(BaseHTTPRequestHandler):
    server_version = 'autocontrol-board'
    sys_version = ''
    protocol_version = 'HTTP/1.1'
    timeout = 30

    def do_GET(self):
        self._dispatch('GET')

    def do_HEAD(self):
        self._dispatch('HEAD')

    def do_POST(self):
        self._dispatch('POST')

    def _dispatch(self, method):
        body = b''
        if method == 'POST':
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self._send(
                    Response(413, b'too large', content_type='text/plain; charset=utf-8'), method
                )
                self.close_connection = True
                return
            body = self.rfile.read(length)
        try:
            response = self.server.app.handle(method, self.path, self.headers, body)
        except Exception as error:  # a broken page never takes the service down
            _log(f'看板出错：{type(error).__name__}: {str(error)[:200]}')
            response = Response(
                500, '看板出错了，稍后刷新'.encode('utf-8'), content_type='text/plain; charset=utf-8'
            )
        self._send(response, method)

    def _send(self, response, method):
        self.send_response(response.status)
        self.send_header('Content-Type', response.content_type)
        self.send_header('Content-Length', str(len(response.body)))
        for key, value in (*SECURITY_HEADERS, *response.headers):
            self.send_header(key, value)
        self.end_headers()
        if method != 'HEAD':
            self.wfile.write(response.body)

    def log_request(self, code='-', size='-'):
        # Page loads and failures only (not the 30-second refreshes), and the
        # path only: a query may carry an authorization code.
        path = urlsplit(self.path).path if isinstance(self.path, str) else '-'
        quiet = self.headers.get('X-Board-Partial') == '1' or path == '/healthz'
        if quiet and str(code).isdigit() and int(code) < 400:
            return
        _log(f'{self.command} {path[:120]} {code}')

    def log_message(self, format, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, port):
        self.app = app
        super().__init__(('127.0.0.1', port), _Handler)


def serve(app, port, stop: threading.Event) -> int:
    server = Server(app, port)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={'poll_interval': 0.5}, daemon=True
    )
    thread.start()
    _log(f'看板启动：http://127.0.0.1:{port}' + ('（本机预览，不登录）' if app.dev else ''))
    try:
        stop.wait()
    finally:
        server.shutdown()
        server.server_close()
        _log('看板停止')
    return 0
