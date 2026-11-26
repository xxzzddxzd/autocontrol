"""The web board: settings, Feishu sign-in, access, pages, the card switch, helpers.

Feishu is never contacted: sign-in runs against an injected fake ``fetch``.
"""

from datetime import datetime
from functools import partial
import json
import signal
import stat
from urllib.parse import parse_qs, urlsplit

import pytest

from autocontrol import registry
from autocontrol.console import FIRST_RETRY, HELPER_ELSEWHERE
from autocontrol.dashboard import auth, config as settings, pages
from autocontrol.dashboard.data import Source
from autocontrol.dashboard.server import LOGIN_COOKIE, SESSION_COOKIE, App
from autocontrol.lark.publisher import CardPublisher, cards_off, cards_switch_path, set_cards
from autocontrol.lark.store import BoardStore, sanitize_snapshot
from autocontrol.projects.yslzm.catalog import CATALOG
from autocontrol.schedule import BEIJING

from .test_console import Harness, item

URL = 'https://board.example.test'
OWNER = 'ou_owner0000001'
OTHER = 'ou_other0000002'


def write_app(path, **overrides):
    path.write_text(json.dumps({'app_id': 'cli_board', 'app_secret': 'test_only_secret',
                                'chat_id': 'oc_test', **overrides}))  # fmt: skip
    path.chmod(0o600)
    return path


class FakeFeishu:
    """Stands in for Feishu's token and user-info endpoints."""

    def __init__(self, open_id=OWNER, name='管理员'):
        self.open_id, self.name, self.calls = open_id, name, []

    def __call__(self, method, url, *, body=None, headers=None, timeout=10.0):
        self.calls.append((method, url, body, headers))
        if url.endswith('/authen/v2/oauth/token'):
            return {
                'code': 0,
                'access_token': 'u-test-token',
                'expires_in': 7200,
                'token_type': 'Bearer',
            }
        if url.endswith('/authen/v1/user_info'):
            return {
                'code': 0,
                'data': {'open_id': self.open_id, 'name': self.name, 'tenant_key': 't1'},
            }
        raise AssertionError(url)


class FakeSource:
    def __init__(self):
        self.state = {'on': True, 'paused_until': None}

    def entries(self):
        return [{'name': 'alpha', 'manifest': '/work/alpha/autocontrol.json', 'private': '/cfg/alpha',
                 'enabled': True}], None  # fmt: skip

    def console(self):
        return {'running': True, 'projects': {'alpha': {'state': 'running', 'pid': 7}}}

    def cards(self):
        return self.state

    def board(self, entry, day=None, now=None):
        card = {'config': {'summary': {'content': '下次 21:15 · 3/4'}},
                'header': {'template': 'blue', 'title': {'content': 'Alpha · 11月25日 周三'},
                           'subtitle': {'content': '每日清单 · 已完成 3/4'}, 'text_tag_list': []},
                'body': {'elements': [{'tag': 'markdown', 'content': '<script>x</script> **完成**'}]}}  # fmt: skip
        return {'name': 'alpha', 'title': 'Alpha', 'day': day or '2026-11-25', 'today': '2026-11-25',
                'days': ['2026-11-25', '2026-11-24'], 'card': card, 'error': None, 'project': None,
                'progress': (3, 4)}  # fmt: skip

    def rounds(self, project, limit=12):
        return []

    def log_tail(self, name, lines=80):
        return ['11-25 14:00:00 开始一轮']


@pytest.fixture
def board(tmp_path):
    folder = tmp_path / 'console'
    folder.mkdir(mode=0o700)
    app_file = write_app(tmp_path / 'lark_app.json')
    path = settings.path_for(folder)
    settings.setup(path, public_url=URL, app=str(app_file), now=1000.0)
    clock = [1000.0]
    feishu = FakeFeishu()
    app = App(path, source=FakeSource(), fetch=feishu, clock=lambda: clock[0],
              notifier=Notifier(), background=False)  # fmt: skip
    return app, path, feishu, clock


class Notifier:
    """Records what the board would post to the group bot."""

    def __init__(self):
        self.sent = []

    def __call__(self, hook, card, *, secret=None):
        self.sent.append((hook, card, secret))


def request(app, target, *, cookies=None, host='board.example.test', partial=False):
    headers = {'Host': host}
    if cookies:
        headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
    if partial:
        headers['X-Board-Partial'] = '1'
    return app.handle('GET', target, headers)


def header(response, name):
    return [value for key, value in response.headers if key == name]


def cookie_value(response, name):
    for value in header(response, 'Set-Cookie'):
        key, rest = value.split('=', 1)
        if key == name:
            return rest.split(';', 1)[0]
    return None


def sign_in(app, *, next_path='/'):
    """Walk the whole Feishu sign-in; returns the callback response."""
    start = request(app, '/login?next=' + next_path)
    assert start.status == 302
    location = dict(start.headers)['Location']
    query = parse_qs(urlsplit(location).query)
    state = query['state'][0]
    return request(app, f'/auth/callback?code=code-1234567890&state={state}',
                   cookies={LOGIN_COOKIE: cookie_value(start, LOGIN_COOKIE)})  # fmt: skip


# -- settings ------------------------------------------------------------------------


def test_setup_writes_a_private_file_with_its_own_secret_and_claim_window(tmp_path):
    path = tmp_path / 'console' / 'dashboard.json'
    config = settings.setup(path, public_url='https://Board.Example.test/', now=100.0)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config['public_url'] == 'https://board.example.test'
    assert len(config['secret']) == 64 and config['owner'] is None
    assert config['claim_until'] == 100.0 + settings.CLAIM_WINDOW
    again = settings.setup(path, port=8795, now=200.0)
    assert again['secret'] == config['secret'] and again['port'] == 8795
    assert 'secret' not in '\n'.join(settings.summary(again)) and config['secret'] not in str(
        settings.summary(again)
    )


@pytest.mark.parametrize(
    'url', ['http://board.example.test', 'https://board.example.test/x', 'board']
)
def test_only_a_bare_https_address_is_accepted(url):
    with pytest.raises(settings.ConfigError):
        settings.normalize_url(url)


def test_settings_others_can_read_are_refused(tmp_path):
    path = tmp_path / 'dashboard.json'
    settings.setup(path, now=0.0)
    path.chmod(0o644)
    with pytest.raises(settings.ConfigError):
        settings.load(path)


# -- sign-in pieces -----------------------------------------------------------------------


def test_each_login_state_works_once_and_expires():
    clock = [0.0]
    logins = auth.Logins(clock=lambda: clock[0])
    state, challenge = logins.begin('/p/alpha')
    assert len(challenge) == 43  # S256 of the verifier, base64url without padding
    verifier, target = logins.finish(state)
    assert target == '/p/alpha' and len(verifier) >= 43
    assert logins.finish(state) is None
    late, _ = logins.begin('/')
    clock[0] = auth.STATE_TTL + 1
    assert logins.finish(late) is None


def test_authorize_url_asks_feishu_for_a_pkce_code():
    app = {'app_id': 'cli_board', 'base_url': 'https://open.feishu.cn'}
    url = urlsplit(auth.authorize_url(app, URL + '/auth/callback', 'st', 'ch'))
    query = parse_qs(url.query)
    assert url.netloc == 'accounts.feishu.cn' and url.path == '/open-apis/authen/v1/authorize'
    assert query == {'client_id': ['cli_board'], 'response_type': ['code'],
                     'redirect_uri': [URL + '/auth/callback'], 'state': ['st'],
                     'code_challenge': ['ch'], 'code_challenge_method': ['S256']}  # fmt: skip


def test_sign_in_trades_the_code_for_the_user_with_the_verifier():
    feishu = FakeFeishu()
    app = {'app_id': 'cli_board', 'app_secret': 's', 'base_url': 'https://open.feishu.cn'}
    user = auth.sign_in(app, 'code-1234567890', 'v' * 64, URL + '/auth/callback', fetch=feishu)
    assert user == {'open_id': OWNER, 'name': '管理员', 'tenant_key': 't1'}
    token_call, info_call = feishu.calls
    assert (
        token_call[2]['code_verifier'] == 'v' * 64
        and token_call[2]['grant_type'] == 'authorization_code'
    )
    assert info_call[3] == {'Authorization': 'Bearer u-test-token'}


def test_a_refused_token_stops_the_sign_in():
    def fetch(method, url, **_):
        return {'code': 20003, 'error': 'invalid_grant'}

    app = {'app_id': 'a', 'app_secret': 's', 'base_url': 'https://open.feishu.cn'}
    with pytest.raises(auth.AuthError) as raised:
        auth.sign_in(app, 'code-1234567890', 'v' * 64, URL, fetch=fetch)
    assert raised.value.code == 'token'


def test_sessions_are_signed_and_expire():
    value = auth.make_session({'open_id': OWNER, 'name': '管理员'}, 'k' * 64, now=0, days=1)
    assert auth.read_session(value, 'k' * 64, now=10)['sub'] == OWNER
    assert auth.read_session(value, 'other' * 13, now=10) is None
    body, signature = value.split('.')
    assert auth.read_session(body + 'x.' + signature, 'k' * 64, now=10) is None
    assert auth.read_session(value, 'k' * 64, now=86400 + 1) is None


def test_access_owner_list_and_claim_window():
    config = {'owner': None, 'allow': [OTHER], 'claim_until': 50.0}
    assert auth.access(config, OWNER, now=10) == 'claim'
    assert auth.access(config, OWNER, now=60) == 'denied'
    assert auth.access(config, OTHER, now=60) == 'allowed'
    assert auth.access({**config, 'owner': OWNER}, OWNER, now=60) == 'owner'
    assert auth.access({**config, 'owner': OWNER}, 'ou_stranger00', now=10) == 'denied'


# -- the service ----------------------------------------------------------------------------


def test_pages_need_a_session_and_refreshes_ask_to_sign_in_again(board):
    app, *_ = board
    response = request(app, '/p/alpha?day=2026-11-24')
    assert response.status == 302
    assert dict(response.headers)['Location'] == '/login?next=%2Fp%2Falpha%3Fday%3D2026-11-24'
    assert request(app, '/', partial=True).status == 401
    assert request(app, '/healthz').status == 200


def test_first_sign_in_claims_the_board_and_the_cookie_is_strict(board):
    app, path, feishu, _ = board
    done = sign_in(app, next_path='/p/alpha')
    assert done.status == 302 and dict(done.headers)['Location'] == '/p/alpha'
    session = [
        value for value in header(done, 'Set-Cookie') if value.startswith(SESSION_COOKIE + '=')
    ][0]
    assert all(flag in session for flag in ('Secure', 'HttpOnly', 'SameSite=Lax', 'Path=/'))
    saved = settings.load(path)
    assert saved['owner'] == OWNER and saved['owner_name'] == '管理员' and saved['claim_until'] is None
    page = request(app, '/', cookies={SESSION_COOKIE: cookie_value(done, SESSION_COOKIE)})
    assert page.status == 200 and 'Alpha' in page.body.decode()
    policy = header(page, 'Content-Security-Policy')[0]
    nonce = policy.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in page.body.decode()


def test_once_claimed_others_need_the_white_list(board):
    app, path, feishu, _ = board
    sign_in(app)
    feishu.open_id, feishu.name = OTHER, '同事'
    denied = sign_in(app)
    assert denied.status == 403 and '同事' in denied.body.decode()
    assert OTHER not in denied.body.decode()  # no ids or commands for the visitor
    assert cookie_value(denied, SESSION_COOKIE) is None
    config = settings.load(path)
    config['allow'] = [OTHER]
    settings.save(path, config)
    allowed = sign_in(app)
    assert allowed.status == 302 and cookie_value(allowed, SESSION_COOKIE)


def test_a_removed_person_loses_access_at_once(board):
    app, path, feishu, _ = board
    sign_in(app)
    feishu.open_id = OTHER
    config = settings.load(path)
    config['allow'] = [OTHER]
    settings.save(path, config)
    cookie = cookie_value(sign_in(app), SESSION_COOKIE)
    assert request(app, '/', cookies={SESSION_COOKIE: cookie}).status == 200
    config['allow'] = []
    settings.save(path, config)
    assert request(app, '/', cookies={SESSION_COOKIE: cookie}).status == 302


def test_a_callback_from_another_browser_or_a_replay_is_refused(board):
    app, *_ = board
    start = request(app, '/login')
    state = parse_qs(urlsplit(dict(start.headers)['Location']).query)['state'][0]
    target = f'/auth/callback?code=code-1234567890&state={state}'
    assert request(app, target, cookies={LOGIN_COOKIE: 'someone-else'}).status == 400
    assert request(app, target).status == 400  # no login cookie at all
    # The browser that started the sign-in can still finish it, once.
    assert request(app, target, cookies={LOGIN_COOKIE: state}).status == 302
    assert request(app, target, cookies={LOGIN_COOKIE: state}).status == 400


def test_the_next_page_after_sign_in_stays_on_this_site(board):
    app, *_ = board
    done = sign_in(app, next_path='//evil.example')
    assert dict(done.headers)['Location'] == '/'


def test_unknown_hosts_are_refused(board):
    app, *_ = board
    assert request(app, '/', host='evil.example').status == 421
    assert request(app, '/healthz', host='evil.example').status == 200


def test_login_waits_for_an_address(tmp_path):
    path = tmp_path / 'dashboard.json'
    settings.setup(path, now=0.0)
    app = App(path, source=FakeSource())
    assert request(app, '/login', host='127.0.0.1:8790').status == 503


def test_partial_refresh_returns_only_the_main_part(board):
    app, *_ = board
    cookie = cookie_value(sign_in(app), SESSION_COOKIE)
    page = request(app, '/p/alpha', cookies={SESSION_COOKIE: cookie}, partial=True)
    body = page.body.decode()
    assert page.status == 200 and '<html' not in body and header(page, 'X-Board-Time')
    assert '&lt;script&gt;' in body and '<script>x' not in body  # card text is escaped
    assert request(app, '/p/missing', cookies={SESSION_COOKIE: cookie}).status == 404


def test_the_card_switch_note_shows_when_cards_are_off(board):
    app, *_ = board
    cookie = cookie_value(sign_in(app), SESSION_COOKIE)
    assert '飞书卡片已停发' not in request(app, '/', cookies={SESSION_COOKIE: cookie}).body.decode()
    app.source.state = {'on': False, 'paused_until': None}
    assert '飞书卡片已停发' in request(app, '/', cookies={SESSION_COOKIE: cookie}).body.decode()
    resume = datetime(2026, 12, 1, 0, 5, tzinfo=BEIJING).timestamp()
    app.source.state = {'on': True, 'paused_until': resume}
    page = request(app, '/', cookies={SESSION_COOKIE: cookie}).body.decode()
    assert '额度已用完' in page and '12-01 00:05' in page


def test_dev_preview_needs_no_sign_in_and_no_settings(tmp_path):
    app = App(tmp_path / 'dashboard.json', source=FakeSource(), dev=True)
    assert request(app, '/', host='127.0.0.1:8798').status == 200


# -- pages --------------------------------------------------------------------------------


def test_card_html_draws_the_card_components():
    card = {
        'header': {'template': 'orange', 'title': {'content': 'T'}, 'subtitle': {'content': 'S'},
                   'text_tag_list': [{'text': {'content': '1 项异常'}, 'color': 'red'}]},
        'body': {'elements': [
            {'tag': 'checker', 'checked': True, 'text': {'content': '签到'}},
            {'tag': 'div', 'icon': {'token': 'loading_outlined'}, 'text': {'content': '执行中'}},
            {'tag': 'div', 'icon': {'token': 'warning_outlined', 'color': 'red'}, 'text': {'content': "<font color='red'>失败</font>"}},
            {'tag': 'collapsible_panel', 'element_id': 'done', 'expanded': False,
             'header': {'title': {'content': '已完成 2 项'}}, 'elements': [{'tag': 'markdown', 'content': '~~a~~'}]},
        ]},
    }  # fmt: skip
    html = pages.card_html(card)
    assert 'head h-orange' in html and 'tag c-red' in html
    assert 'todo done' in html and 'mark ring spin' in html
    assert '<span class="c-red">失败</span>' in html and 'mark ring c-red' in html
    assert '<details data-key="done">' in html and '<s>a</s>' in html


def test_overview_rows_carry_progress_and_only_new_tags():
    source = FakeSource()
    entry = source.entries()[0][0]
    row = source.board(entry)
    row['card']['header']['text_tag_list'] = [{'text': {'content': '下次 21:15'}, 'color': 'blue'},
                                             {'text': {'content': '2 项连续失败'}, 'color': 'red'}]  # fmt: skip
    html = pages.overview_main([row], source.console())
    assert 'width:75%' in html and '2 项连续失败' in html
    assert html.count('下次 21:15') == 1  # the summary line already says it


# -- reading real project files -------------------------------------------------------------


def test_source_renders_the_saved_snapshot_with_the_project_card(tmp_path):
    path = tmp_path / 'console' / 'projects.json'
    entry = {'name': 'yslzm', 'project': 'yslzm', 'workdir': str(tmp_path / 'work'),
             'private': str(tmp_path / 'private'), 'enabled': True}  # fmt: skip
    registry.save(path, [entry])
    source = Source(path)
    project = source.project(registry.load(path)[0])
    now = datetime(2026, 11, 25, 15, 0, tzinfo=BEIJING)
    day = project.schedule.service_day(now)
    snapshot = project.new_projection(now, restore=False).snapshot()
    BoardStore(project.board_db, sanitize=project.sanitize).save(
        'yslzm', day, 'oc_test', snapshot=snapshot, delivery_status='local'
    )
    result = source.board(registry.load(path)[0], now=now)
    assert result['day'] == day and result['days'] == [day]
    assert result['card']['header']['title']['content'].startswith(project.catalog.display_name)
    assert result['progress'][1] > 0
    assert source.rounds(project) == [] and source.log_tail('yslzm') == []


# -- the card switch ---------------------------------------------------------------------------


SANITIZE = partial(
    sanitize_snapshot, count_keys=CATALOG.count_keys, phases=frozenset(CATALOG.phases)
)


def card_snapshot(day='2026-11-25'):
    return {'schema': 1, 'project': 'yslzm', 'service_day': day, 'discovered': True, 'running': True,
            'tasks': [{'task_id': 'cardbook', 'title': '事件簿', 'phase': 'cardbook',
                       'status': 'running', 'current': 1, 'total': 12, 'unit': '次'}],
            'summary': {'completed': 0, 'running': 1}}  # fmt: skip


def test_with_cards_off_snapshots_stay_local_and_feishu_is_never_called(tmp_path):
    private, console = tmp_path / 'private', tmp_path / 'console'
    private.mkdir()
    write_app(private / 'lark_app.json')
    set_cards(False, console)
    assert cards_off(console)
    made = []

    def factory(config):
        made.append(config)
        raise AssertionError('Feishu must not be contacted')

    publisher = CardPublisher(private, lambda snap: {}, SANITIZE, client_factory=factory,
                              min_interval=0, switch_path=cards_switch_path(console))  # fmt: skip
    try:
        assert publisher.submit(card_snapshot())
        assert publisher.flush(5)
        assert publisher.status['status'] == 'local' and publisher.delivered
    finally:
        publisher.close()
    row = BoardStore(private / 'daily_board.sqlite3', sanitize=SANITIZE).get(
        'yslzm', '2026-11-25', 'oc_test'
    )
    assert row['delivery_status'] == 'local' and row['snapshot']['tasks'][0]['current'] == 1
    assert made == []


def test_turning_cards_back_on_brings_the_day_to_its_card(tmp_path):
    private, console = tmp_path / 'private', tmp_path / 'console'
    private.mkdir()
    write_app(private / 'lark_app.json')
    set_cards(False, console)

    class Client:
        def __init__(self):
            self.sent = []

        def send_card(self, chat_id, card, uuid):
            self.sent.append(chat_id)
            return 'om_test'

        def update_card(self, message_id, card):
            pass

    client = Client()
    publisher = CardPublisher(private, lambda snap: {'x': snap['tasks'][0]['current']}, SANITIZE,
                              client_factory=lambda config: client, min_interval=0,
                              switch_path=cards_switch_path(console))  # fmt: skip
    try:
        publisher.submit(card_snapshot())
        assert publisher.flush(5) and client.sent == []
        set_cards(True, console)
        publisher.submit(card_snapshot())
        assert publisher.flush(5)
        # The remembered day goes to its card once; the same display again is unchanged.
        assert client.sent == ['oc_test'] and publisher.delivered
    finally:
        publisher.close()


# -- the console keeps the board running ------------------------------------------------------


def test_the_console_runs_the_board_once_it_is_set_up(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    console = harness.console
    console.tend_helpers(harness.now)
    assert not console.helpers  # no dashboard.json: nothing to run
    settings.setup(settings.path_for(harness.path.parent), public_url=URL, now=0.0)
    console.tend_helpers(harness.now)
    board = console.helpers['dashboard'].process
    assert board.command == [
        '/py',
        '-B',
        '/console/run.py',
        'dashboard',
        '--registry',
        str(harness.path),
    ]
    assert console.status()['helpers']['dashboard']['state'] == 'running'
    board.code = 1  # it crashed
    harness.now += 1
    console.tend_helpers(harness.now)
    assert console.helpers['dashboard'].state == 'restarting'
    harness.now += FIRST_RETRY
    console.tend_helpers(harness.now)
    assert (
        console.helpers['dashboard'].process is not board
        and console.helpers['dashboard'].restarts == 1
    )


def test_a_second_copy_waits_while_another_board_runs(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    console = harness.console
    settings.setup(settings.path_for(harness.path.parent), now=0.0)
    console.tend_helpers(harness.now)
    console.helpers['dashboard'].process.code = HELPER_ELSEWHERE
    console.tend_helpers(harness.now + 1)
    assert (
        console.helpers['dashboard'].state == 'elsewhere'
        and console.helpers['dashboard'].restarts == 0
    )


def test_a_tunnel_starts_with_its_own_config_and_restarts_on_change(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    console = harness.console
    path = settings.path_for(harness.path.parent)
    settings.setup(
        path, tunnel={'config': '/cfg/tunnel.yml', 'binary': '/bin/cloudflared'}, now=0.0
    )
    console.tend_helpers(harness.now)
    tunnel = console.helpers['tunnel'].process
    assert tunnel.command == [
        '/bin/cloudflared',
        'tunnel',
        '--no-autoupdate',
        '--config',
        '/cfg/tunnel.yml',
        'run',
    ]
    tunnel.idle = True
    settings.setup(path, tunnel={'config': '/cfg/other.yml', 'binary': '/bin/cloudflared'}, now=0.0)
    console.tend_helpers(harness.now + 1)
    assert tunnel.signals == [signal.SIGTERM]
    console.tend_helpers(harness.now + 2)
    assert console.helpers['tunnel'].process.command[4] == '/cfg/other.yml'


def test_shutdown_stops_the_helpers(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    console = harness.console
    settings.setup(settings.path_for(harness.path.parent), now=0.0)
    console.tend_helpers(harness.now)
    board = console.helpers['dashboard'].process
    board.idle = True
    console.shutdown()
    assert board.signals == [signal.SIGTERM] and console.helpers['dashboard'].state == 'stopped'


# -- access requests and the owner's approval -----------------------------------------------

HOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-0001'


def with_hook(path, secret='sig-secret'):
    config = settings.load(path)
    config['notify'] = {'webhook': HOOK, 'secret': secret}
    settings.save(path, config)


def owner_cookie(app):
    return cookie_value(sign_in(app), SESSION_COOKIE)


def form_fields(html):
    import re

    return dict(re.findall(r'name="([a-z_]+)" value="([^"]*)"', html))


def post(app, target, fields, cookie):
    from urllib.parse import urlencode

    headers = {'Host': 'board.example.test', 'Cookie': f'{SESSION_COOKIE}={cookie}'}
    return app.handle('POST', target, headers, urlencode(fields).encode())


def ask_as_other(app, feishu, name='同事'):
    feishu.open_id, feishu.name = OTHER, name
    return sign_in(app)


def test_someone_turned_away_becomes_a_request_and_the_owner_hears_once_a_day(board):
    app, path, feishu, clock = board
    with_hook(path)
    cookie = owner_cookie(app)
    first = ask_as_other(app, feishu)
    assert first.status == 403 and '已经通知管理员' in first.body.decode()
    ((hook, card, secret),) = app.notifier.sent
    assert hook == HOOK and secret == 'sig-secret'
    links = [b['multi_url']['url'] for b in card['elements'][-1]['actions']]
    request_id = links[0].split('/admin/requests/')[1].split('?')[0]
    assert links[0] == f'{URL}/admin/requests/{request_id}?do=approve'
    assert links[1].endswith('?do=deny') and links[2] == f'{URL}/admin'
    assert OTHER not in json.dumps(card) and '同事' in json.dumps(card, ensure_ascii=False)
    ask_as_other(app, feishu)
    assert len(app.notifier.sent) == 1  # the same person again the same day: quiet
    clock[0] += 86400
    ask_as_other(app, feishu)
    assert len(app.notifier.sent) == 2
    members = request(app, '/admin', cookies={SESSION_COOKIE: cookie})
    assert (
        members.status == 200 and '待批准' in members.body.decode() and '同事' in members.body.decode()
    )


def test_without_a_group_bot_the_request_waits_on_the_members_page(board):
    app, path, feishu, _ = board
    owner_cookie(app)
    denied = ask_as_other(app, feishu)
    assert '「成员」页批准' in denied.body.decode() and app.notifier.sent == []


def test_the_owner_approves_from_the_card_link_with_one_more_tap(board):
    app, path, feishu, _ = board
    with_hook(path)
    cookie = owner_cookie(app)
    ask_as_other(app, feishu)
    link = app.notifier.sent[0][1]['elements'][-1]['actions'][0]['multi_url']['url']
    target = link.replace(URL, '')
    assert request(app, target).status == 302  # the link alone signs in first, changes nothing
    page = request(app, target, cookies={SESSION_COOKIE: cookie})
    body = page.body.decode()
    assert page.status == 200 and '让 同事 查看看板？' in body and '<script' not in body
    fields = form_fields(body)
    assert fields['do'] == 'approve'
    forged = post(app, target.split('?')[0], {**fields, 'csrf': 'x' * 40}, cookie)
    assert forged.status == 403 and OTHER not in settings.load(path)['allow']
    done = post(app, target.split('?')[0], fields, cookie)
    assert done.status == 200 and '已批准 同事' in done.body.decode()
    assert settings.load(path)['allow'] == [OTHER]
    feishu.open_id = OTHER
    assert cookie_value(sign_in(app), SESSION_COOKIE)  # in at the next opening


def test_only_the_owner_manages_members(board):
    app, path, feishu, _ = board
    owner = owner_cookie(app)
    config = settings.load(path)
    config['allow'] = [OTHER]
    settings.save(path, config)
    feishu.open_id, feishu.name = OTHER, '同事'
    colleague = cookie_value(sign_in(app), SESSION_COOKIE)
    assert request(app, '/admin', cookies={SESSION_COOKIE: colleague}).status == 403
    page = request(app, '/admin', cookies={SESSION_COOKIE: owner}).body.decode()
    assert '同事' in page and '移出' in page
    fields = form_fields(page)
    assert (
        post(app, '/admin/people', {**fields, 'do': 'remove', 'open_id': OTHER}, colleague).status
        == 403
    )
    assert request(app, '/', cookies={SESSION_COOKIE: owner}).body.decode().count('/admin') == 1
    assert '/admin' not in request(app, '/', cookies={SESSION_COOKIE: colleague}).body.decode()


def test_removing_and_refusing_take_effect_at_once(board):
    app, path, feishu, clock = board
    with_hook(path)
    owner = owner_cookie(app)
    ask_as_other(app, feishu)
    request_id = next(iter(app.people.snapshot()['requests']))
    page = request(app, f'/admin/requests/{request_id}?do=deny', cookies={SESSION_COOKIE: owner})
    fields = form_fields(page.body.decode())
    assert fields['do'] == 'deny'
    assert '已拒绝 同事' in post(app, f'/admin/requests/{request_id}', fields, owner).body.decode()
    again = ask_as_other(app, feishu)
    assert '没有批准' in again.body.decode() and len(app.notifier.sent) == 1
    clock[0] += 8 * 86400  # a week later they may ask again
    ask_as_other(app, feishu)
    assert len(app.notifier.sent) == 2
    config = settings.load(path)
    config['allow'] = [OTHER]
    settings.save(path, config)
    page = request(app, '/admin', cookies={SESSION_COOKIE: owner}).body.decode()
    removed = post(
        app, '/admin/people', {**form_fields(page), 'do': 'remove', 'open_id': OTHER}, owner
    )
    assert removed.status == 200 and settings.load(path)['allow'] == []


def test_posts_need_a_session_and_stay_small(board):
    app, path, *_ = board
    assert (
        app.handle('POST', '/admin/people', {'Host': 'board.example.test'}, b'do=remove').status
        == 401
    )
    cookie = owner_cookie(app)
    big = app.handle('POST', '/admin/people', {'Host': 'board.example.test',
                     'Cookie': f'{SESSION_COOKIE}={cookie}'}, b'x' * 9000)  # fmt: skip
    assert big.status == 413


# -- the group bot ----------------------------------------------------------------------------


def test_signed_webhook_messages_follow_feishu_rules():
    import base64
    import hashlib
    import hmac

    from autocontrol.dashboard import notify

    expected = base64.b64encode(hmac.new(b'1700000000\nkey', b'', hashlib.sha256).digest()).decode()
    assert notify.sign('key', 1700000000) == expected
    seen = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size):
            return b'{"code":0,"msg":"success","data":{}}'

    def opener(req, timeout):
        seen.append(json.loads(req.data))
        return Reply()

    notify.send(HOOK, {'header': {}}, secret='key', opener=opener, clock=lambda: 1700000000)
    assert seen[0]['msg_type'] == 'interactive' and seen[0]['sign'] == expected
    assert seen[0]['timestamp'] == '1700000000' and seen[0]['card'] == {'header': {}}
    with pytest.raises(notify.NotifyError):
        notify.send('https://example.com/hook', {}, opener=opener)


def test_settings_accept_only_a_feishu_group_bot(tmp_path):
    path = tmp_path / 'dashboard.json'
    settings.setup(path, now=0.0)
    config = settings.load(path)
    config['notify'] = {'webhook': 'https://evil.example/hook'}
    with pytest.raises(settings.ConfigError):
        settings.save(path, config)
    config['notify'] = {'webhook': HOOK}
    settings.save(path, config)
    assert settings.load(path)['notify'] == {'webhook': HOOK, 'secret': None}
    assert HOOK not in '\n'.join(settings.summary(settings.load(path)))


def test_the_webhook_command_saves_quietly_and_sends_a_test(tmp_path, monkeypatch, capsys):
    from autocontrol import cli
    from autocontrol.dashboard import notify

    path = tmp_path / 'dashboard.json'
    settings.setup(path, now=0.0)
    sent = []
    monkeypatch.setattr(
        notify, 'send', lambda hook, payload, secret=None: sent.append((hook, secret))
    )
    answers = iter([HOOK, 'sig'])
    assert cli._dashboard_webhook(path, None, ask=lambda prompt: next(answers)) == 0
    assert settings.load(path)['notify'] == {'webhook': HOOK, 'secret': 'sig'} and sent == [
        (HOOK, 'sig')
    ]
    assert HOOK not in capsys.readouterr().out
    assert cli._dashboard_webhook(path, 'off') == 0 and settings.load(path)['notify'] is None
    bad = iter(['https://example.com/x'])
    assert cli._dashboard_webhook(path, None, ask=lambda prompt: next(bad)) == 1


def test_people_remember_requests_and_names(tmp_path):
    from autocontrol.dashboard.people import People

    clock = [100.0]
    store = People(tmp_path / 'people.json', clock=lambda: clock[0])
    item, tell = store.ask(OTHER, '同事')
    assert (
        tell
        and item['status'] == 'pending'
        and stat.S_IMODE((tmp_path / 'people.json').stat().st_mode) == 0o600
    )
    assert store.ask(OTHER, '同事')[1] is False
    assert store.decide(item['id'], 'approved')['status'] == 'approved'
    store.seen(OTHER, '新名字')
    assert store.name(OTHER) == '新名字'
    assert store.get('missing-id') is None and store.decide('missing-id', 'denied') is None
