"""Delivery tests use injected transports and temporary private directories only.

Ported from yslzm/tests/test_daily_board_delivery.py and test_daily_board_resilient.py.
"""

from functools import partial
import json
import os
import stat
import threading
import time

import pytest

from autocontrol.lark.client import DeliveryError, FeishuClient, load_config
from autocontrol.lark.publisher import CardPublisher
from autocontrol.lark.store import BoardStore, sanitize_snapshot
from autocontrol.projects.yslzm.catalog import CATALOG

CONFIG = {'app_id': 'cli_test', 'app_secret': 'test_only_secret', 'chat_id': 'oc_test'}
SANITIZE = partial(
    sanitize_snapshot, count_keys=CATALOG.count_keys, phases=frozenset(CATALOG.phases)
)


def snapshot(current=1, day='2026-11-17'):
    return {
        'schema': 1, 'project': 'yslzm', 'service_day': day, 'discovered': True, 'running': True,
        'tasks': [{'task_id': 'cardbook', 'title': '事件簿', 'phase': 'cardbook', 'status': 'running',
                   'current': current, 'total': 12, 'unit': '次'}],
        'summary': {'completed': 0, 'running': 1},
    }  # fmt: skip


def render(value):
    return {'config': {'update_multi': True},
            'header': {'title': {'tag': 'plain_text', 'content': 'yslzm · ' + value['service_day']}},
            'elements': [{'tag': 'markdown', 'content': str(value['tasks'][0]['current'])}]}  # fmt: skip


class FakeClient:
    def __init__(self):
        self.sent, self.updated = [], []

    def send_card(self, chat_id, card, uuid):
        self.sent.append((chat_id, card, uuid))
        return 'om_test'

    def update_card(self, message_id, card):
        self.updated.append((message_id, card))


def write_config(private, **overrides):
    path = private / 'lark_app.json'
    path.write_text(json.dumps({**CONFIG, **overrides}))
    path.chmod(0o600)
    return path


def publisher(private, client, **options):
    options.setdefault('min_interval', 0)
    return CardPublisher(private, render, SANITIZE, client_factory=lambda config: client, **options)


def store(private):
    return BoardStore(private / 'daily_board.sqlite3', sanitize=SANITIZE)


# -- store and client ---------------------------------------------------------


def test_store_is_private_and_drops_raw_reports(tmp_path):
    path = tmp_path / 'private' / 'board.sqlite3'
    value = snapshot()
    value['raw_response'] = {'session_key': 'sensitive-value'}
    value['tasks'][0]['session_key'] = 'sensitive-value'
    value['tasks'][0]['counts'] = {'confirmed_actions': 1, 'access_token': 23, 'role_id': 1234}
    BoardStore(path, sanitize=SANITIZE).save(
        'yslzm', value['service_day'], 'oc_test', snapshot=value, message_id='om_test'
    )
    row = BoardStore(path, sanitize=SANITIZE).get('yslzm', value['service_day'], 'oc_test')
    assert row['message_id'] == 'om_test'
    assert row['snapshot']['tasks'][0]['counts'] == {'confirmed_actions': 1}
    assert 'raw_response' not in row['snapshot']
    assert b'sensitive-value' not in path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        BoardStore.latest_snapshot(path, 'yslzm', value['service_day'])['tasks'][0]['current'] == 1
    )
    assert BoardStore.latest_snapshot(tmp_path / 'missing.sqlite3', 'yslzm', '2026-11-17') is None


def test_config_security_and_host_allowlist(tmp_path):
    path = write_config(tmp_path)
    assert load_config(path)['app_id'] == 'cli_test'
    path.chmod(0o644)
    with pytest.raises(DeliveryError, match='config_permissions'):
        load_config(path)
    with pytest.raises(DeliveryError, match='config_missing'):
        load_config(tmp_path / 'nope.json')
    with pytest.raises(DeliveryError, match='config_url'):
        FeishuClient({**CONFIG, 'base_url': 'https://example.com'})


def test_client_caches_token_and_uses_same_message_for_patch():
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, body))
        if '/auth/' in url:
            return {'code': 0, 'tenant_access_token': 'memory_only_token', 'expire': 7200}
        assert headers['Authorization'] == 'Bearer memory_only_token'
        return {'code': 0, 'data': {'message_id': 'om_new'}}

    client = FeishuClient(CONFIG, request_json=transport)
    card = render(snapshot())
    assert client.send_card('oc_test', card, 'stable-uuid') == 'om_new'
    client.update_card('om_new', card)
    assert len([call for call in calls if '/auth/' in call[1]]) == 1
    assert calls[1][3]['uuid'] == 'stable-uuid' and calls[1][3]['msg_type'] == 'interactive'
    assert json.loads(calls[1][3]['content']) == card
    assert calls[2][0] == 'PATCH' and calls[2][1].endswith('/im/v1/messages/om_new')


def test_client_refreshes_only_definitively_invalid_token_and_keeps_uuid():
    calls, auths = [], []

    def transport(method, url, headers, body, timeout):
        if '/auth/' in url:
            auths.append(1)
            return {'code': 0, 'tenant_access_token': f'token{len(auths)}', 'expire': 7200}
        calls.append(body)
        if len(calls) == 1:
            return {'code': 99991663, 'msg': 'raw error text must not escape'}
        return {'code': 0, 'data': {'message_id': 'om_new'}}

    assert (
        FeishuClient(CONFIG, request_json=transport).send_card(
            'oc_test', render(snapshot()), 'same-uuid'
        )
        == 'om_new'
    )
    assert len(auths) == 2
    assert [body['uuid'] for body in calls] == ['same-uuid', 'same-uuid']


def test_client_errors_do_not_expose_response_text():
    def transport(method, url, headers, body, timeout):
        if '/auth/' in url:
            return {'code': 0, 'tenant_access_token': 'memory-only', 'expire': 7200}
        return {'code': 230027, 'msg': 'sensitive-server-detail'}

    with pytest.raises(DeliveryError) as failure:
        FeishuClient(CONFIG, request_json=transport).send_card(
            'oc_test', render(snapshot()), 'uuid'
        )
    assert (failure.value.code, failure.value.api_code) == ('permission_denied', 230027)
    assert 'sensitive-server-detail' not in str(failure.value)


def test_chats_are_paginated_and_return_only_selection_fields():
    seen = []

    def transport(method, url, headers, body, timeout):
        if '/auth/' in url:
            return {'code': 0, 'tenant_access_token': 'memory-only', 'expire': 7200}
        seen.append(url)
        if len(seen) == 1:
            return {
                'code': 0,
                'data': {
                    'items': [{'chat_id': 'oc_1', 'name': 'one', 'owner': 'x'}],
                    'has_more': True,
                    'page_token': 'cursor',
                },
            }
        return {
            'code': 0,
            'data': {'items': [{'chat_id': 'oc_2', 'name': 'two'}], 'has_more': False},
        }

    assert FeishuClient(CONFIG, request_json=transport).list_chats() == [
        {'chat_id': 'oc_1', 'name': 'one'},
        {'chat_id': 'oc_2', 'name': 'two'},
    ]
    assert 'page_token=cursor' in seen[1]


def test_find_card_matches_only_this_apps_card_with_the_exact_title():
    card = render(snapshot())

    def transport(method, url, headers, body, timeout):
        if '/auth/' in url:
            return {'code': 0, 'tenant_access_token': 'memory-only', 'expire': 7200}
        content = json.dumps(card)
        return {'code': 0, 'data': {'has_more': False, 'items': [
            {'message_id': 'om_mine', 'msg_type': 'interactive', 'sender': {'id': 'cli_test'}, 'body': {'content': content}},
            {'message_id': 'om_other', 'msg_type': 'interactive', 'sender': {'id': 'cli_x'}, 'body': {'content': content}},
            {'message_id': 'om_old', 'msg_type': 'interactive', 'sender': {'id': 'cli_test'}, 'deleted': True,
             'body': {'content': content}}]}}  # fmt: skip

    assert (
        FeishuClient(CONFIG, request_json=transport).find_card('oc_test', card, since=time.time())
        == 'om_mine'
    )


# -- publisher --------------------------------------------------------------------


def test_publisher_reuses_daily_message_across_restart(private):
    write_config(private)
    client = FakeClient()
    first = publisher(private, client)
    assert first.submit(snapshot())
    assert first.close()
    second = publisher(private, client)
    assert second.submit(snapshot(2))
    assert second.close()
    assert len(client.sent) == len(client.updated) == 1
    assert client.updated[0][0] == 'om_test'
    assert (
        store(private).get('yslzm', '2026-11-17', 'oc_test')['snapshot']['tasks'][0]['current'] == 2
    )


def test_publisher_deduplicates_unchanged_card_and_separates_days(private):
    write_config(private)
    client = FakeClient()
    card = publisher(private, client)
    card.submit(snapshot())
    assert card.flush()
    card.submit(snapshot())
    assert card.flush()
    assert card.status['status'] == 'unchanged'
    card.submit(snapshot(day='2026-11-18'))
    assert card.close()
    assert len(client.sent) == 2
    assert client.sent[0][2] != client.sent[1][2]
    assert not client.updated


def test_latest_progress_replaces_queued_intermediate_updates(private):
    write_config(private)
    entered, release = threading.Event(), threading.Event()

    class SlowClient(FakeClient):
        def send_card(self, chat_id, card, uuid):
            entered.set()
            assert release.wait(5)
            return super().send_card(chat_id, card, uuid)

    client = SlowClient()
    card = publisher(private, client)
    card.submit(snapshot(1))
    assert entered.wait(5)
    for current in range(2, 10):
        card.submit(snapshot(current))
    release.set()
    assert card.close()
    assert len(client.sent) == 1 and len(client.updated) == 1
    assert client.updated[0][1]['elements'][0]['content'] == '9'


def test_uncertain_create_recovers_from_server_without_second_send(private):
    write_config(private)

    class UncertainClient(FakeClient):
        def send_card(self, chat_id, card, uuid):
            super().send_card(chat_id, card, uuid)
            raise DeliveryError('timeout', uncertain=True)

        def find_card(self, chat_id, card, *, since):
            return 'om_recovered'

    client = UncertainClient()
    card = publisher(private, client)
    card.submit(snapshot(1))
    assert card.flush()
    assert card.status['status'] == 'uncertain'
    card.submit(snapshot(2))
    assert card.close()
    assert len(client.sent) == 1
    assert client.updated[0][0] == 'om_recovered'


def test_uncertain_create_retries_same_uuid_inside_the_idempotency_window(private):
    write_config(private)

    class RetryClient(FakeClient):
        def send_card(self, chat_id, card, uuid):
            result = super().send_card(chat_id, card, uuid)
            if len(self.sent) == 1:
                raise DeliveryError('timeout', uncertain=True)
            return result

    client = RetryClient()
    card = publisher(private, client)
    card.submit(snapshot())
    assert card.flush()
    card.submit(snapshot(2))
    assert card.close()
    assert len(client.sent) == 2
    assert client.sent[0][2] == client.sent[1][2]


def test_uuid_recovery_patches_latest_content_before_committing_digest(private):
    write_config(private)

    class DeduplicatingClient(FakeClient):
        def find_card(self, *args, **kwargs):
            raise DeliveryError('permission_denied', api_code=230027)

        def send_card(self, chat_id, card, uuid):
            result = super().send_card(chat_id, card, uuid)
            if len(self.sent) == 1:
                self.server_card = card
                raise DeliveryError('timeout', uncertain=True)
            return result  # a UUID replay returns the original message unchanged

        def update_card(self, message_id, card):
            self.server_card = card
            super().update_card(message_id, card)

    client = DeduplicatingClient()
    card = publisher(private, client)
    try:
        card.submit(snapshot(1))
        assert card.flush()
        card.submit(snapshot(9))
        assert card.flush()
        assert len(client.sent) == 2 and client.sent[0][2] == client.sent[1][2]
        assert len(client.updated) == 1
        assert client.server_card['elements'][0]['content'] == '9'
        card.submit(snapshot(9))
        assert card.flush()
        assert len(client.updated) == 1
    finally:
        card.close()


def test_failed_patch_after_uuid_recovery_retains_binding_for_patch_only_retry(private):
    write_config(private)

    class RecoveryClient(FakeClient):
        def find_card(self, *args, **kwargs):
            raise DeliveryError('permission_denied')

        def send_card(self, chat_id, card, uuid):
            result = super().send_card(chat_id, card, uuid)
            if len(self.sent) == 1:
                raise DeliveryError('timeout', uncertain=True)
            return result

        def update_card(self, message_id, card):
            super().update_card(message_id, card)
            if len(self.updated) == 1:
                raise DeliveryError('network_error', uncertain=True)

    client = RecoveryClient()
    card = publisher(private, client)
    try:
        card.submit(snapshot(1))
        assert card.flush()
        card.submit(snapshot(9))
        assert card.flush()
        row = store(private).get('yslzm', '2026-11-17', 'oc_test')
        assert (row['message_id'], row['content_hash']) == ('om_test', None)
        assert card.status['status'] == 'uncertain'
        card.submit(snapshot(9))
        assert card.flush()
        assert len(client.sent) == 2 and len(client.updated) == 2
        assert store(private).get('yslzm', '2026-11-17', 'oc_test')['content_hash']
    finally:
        card.close()


def test_expired_uncertain_create_does_not_blindly_send_again(private):
    write_config(private)
    store(private).save('yslzm', '2026-11-17', 'oc_test', create_uuid='existing-uuid',
                        create_started_at=time.time() - 4000, delivery_status='create_uncertain')  # fmt: skip
    client = FakeClient()
    card = publisher(private, client)
    card.submit(snapshot())
    card.close()
    assert card.status['status'] == 'uncertain'
    assert not client.sent


def test_failed_notification_does_not_raise_into_submitter(private):
    write_config(private)

    class DeniedClient(FakeClient):
        def send_card(self, chat_id, card, uuid):
            raise DeliveryError('permission_denied', api_code=230027)

    card = publisher(private, DeniedClient())
    assert card.submit(snapshot())
    card.close()
    assert card.status['error_code'] == 'permission_denied'
    assert store(private).get('yslzm', '2026-11-17', 'oc_test')['snapshot']['tasks']


def test_missing_config_recovers_and_sends_only_the_latest_snapshot(private):
    client = FakeClient()
    card = publisher(private, client, retry_interval=0.05)
    try:
        for current in (1, 2, 3):
            assert card.submit(snapshot(current))
        card.flush(1)
        assert card.status['error_code'] == 'config_missing'
        write_config(private)
        deadline = time.monotonic() + 5
        while not client.sent and time.monotonic() < deadline:
            time.sleep(0.02)
        assert card.flush(5)
        assert [entry[1]['elements'][0]['content'] for entry in client.sent] == ['3']
        assert card.delivered
    finally:
        card.close()


def test_missing_chat_is_a_notification_status_only(private):
    write_config(private, chat_id=None)
    card = publisher(private, FakeClient())
    assert card.submit(snapshot())
    card.flush(2)
    assert card.status['error_code'] == 'chat_id_missing'
    card.close()


def test_config_change_moves_the_card_to_the_new_chat(private):
    write_config(private)
    clients = {}

    def factory(config):
        return clients.setdefault(config['chat_id'], FakeClient())

    card = CardPublisher(private, render, SANITIZE, client_factory=factory, min_interval=0)
    try:
        card.submit(snapshot())
        assert card.flush()
        time.sleep(0.01)
        write_config(private, chat_id='oc_other')
        os.utime(private / 'lark_app.json', ns=(time.time_ns(), time.time_ns()))
        card.submit(snapshot(2))
        assert card.flush()
        assert [entry[0] for entry in clients['oc_test'].sent] == ['oc_test']
        assert [entry[0] for entry in clients['oc_other'].sent] == ['oc_other']
    finally:
        card.close()


def test_network_failure_retries_newest_snapshot_without_another_submit(private):
    write_config(private)

    class FlakyClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.failures = 2

        def send_card(self, chat_id, card, uuid):
            if self.failures:
                self.failures -= 1
                raise DeliveryError('network_error')
            return super().send_card(chat_id, card, uuid)

    client = FlakyClient()
    card = publisher(private, client, retry_interval=0.05, max_retry_interval=0.1)
    try:
        card.submit(snapshot(5))
        deadline = time.monotonic() + 5
        while not client.sent and time.monotonic() < deadline:
            time.sleep(0.02)
        assert [entry[1]['elements'][0]['content'] for entry in client.sent] == ['5']
        assert card.flush(2) and card.delivered
    finally:
        card.close()


def test_two_publishers_serialize_create_and_share_the_binding(private):
    write_config(private)
    entered, release = threading.Event(), threading.Event()

    class SlowClient(FakeClient):
        def send_card(self, chat_id, card, uuid):
            entered.set()
            assert release.wait(5)
            return super().send_card(chat_id, card, uuid)

    client = SlowClient()
    first, second = publisher(private, client), publisher(private, client)
    try:
        first.submit(snapshot(1))
        assert entered.wait(5)
        second.submit(snapshot(2))
        time.sleep(0.1)
        release.set()
        assert first.flush() and second.flush()
        assert len(client.sent) == 1
        assert len(client.updated) == 1 and client.updated[0][0] == 'om_test'
    finally:
        first.close()
        second.close()


def test_invalid_snapshots_are_refused_without_raising(private):
    card = publisher(private, FakeClient())
    assert card.submit(None) is False
    assert card.submit({'project': 'yslzm'}) is False
    card.close()


def test_client_uploads_a_card_image_as_multipart():
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, body))
        if '/auth/' in url:
            return {'code': 0, 'tenant_access_token': 'memory_only_token', 'expire': 7200}
        return {'code': 0, 'data': {'image_key': 'img_v3_spinner'}}

    client = FeishuClient(CONFIG, request_json=transport)
    assert client.upload_image(b'RIFF-webp-bytes', 'spinner.webp', 'image/webp') == 'img_v3_spinner'
    method, url, headers, body = calls[1]
    assert (method, url) == ('POST', 'https://open.feishu.cn/open-apis/im/v1/images')
    assert headers['Content-Type'].startswith('multipart/form-data; boundary=')
    assert headers['Authorization'] == 'Bearer memory_only_token'
    assert b'name="image_type"\r\n\r\nmessage' in body and b'RIFF-webp-bytes' in body
    assert client.app_fingerprint != CONFIG['app_id'] and len(client.app_fingerprint) == 16
    with pytest.raises(DeliveryError):
        client.upload_image(b'', 'empty.webp', 'image/webp')


class SpinnerClient(FakeClient):
    app_fingerprint = 'bot-test'

    def __init__(self, fail=False):
        super().__init__()
        self.fail, self.uploads = fail, 0

    def upload_image(self, data, filename, content_type):
        self.uploads += 1
        if self.fail:
            raise DeliveryError('permission_denied', api_code=99991672)
        return 'img_v3_spinner'


def test_publisher_renders_with_the_uploaded_spinner_or_without_it(private):
    seen = []

    def render_icons(value, icons=None):
        seen.append(icons)
        return render(value)

    write_config(private)
    client = SpinnerClient()
    sender = CardPublisher(private, render_icons, SANITIZE, client_factory=lambda config: client,
                           min_interval=0)  # fmt: skip
    assert sender.submit(snapshot()) and sender.submit(snapshot(2))
    assert sender.close()
    assert seen and all(icons == {'spinner': 'img_v3_spinner'} for icons in seen)
    assert client.uploads == 1  # cached in the private directory afterwards
    failing = SpinnerClient(fail=True)
    seen.clear()
    other = tmp = private.parent / 'other'
    tmp.mkdir(mode=0o700)
    write_config(other)
    sender = CardPublisher(other, render_icons, SANITIZE, client_factory=lambda config: failing,
                           min_interval=0)  # fmt: skip
    assert sender.submit(snapshot())
    assert sender.close()
    assert seen == [None] and failing.sent  # the card still goes out, with the static icon


# -- the tenant's monthly quota ---------------------------------------------------------


def test_next_month_is_just_after_midnight_on_the_first_in_beijing():
    from datetime import datetime

    from autocontrol.lark.publisher import next_month
    from autocontrol.schedule import BEIJING

    def at(*parts):
        return datetime(*parts, tzinfo=BEIJING).timestamp()

    assert next_month(at(2026, 11, 25, 19, 0)) == at(2026, 12, 1, 0, 5)
    assert next_month(at(2026, 12, 31, 23, 59)) == at(2027, 1, 1, 0, 5)


def test_a_used_up_quota_pauses_every_card_until_next_month(private, tmp_path):
    from autocontrol.lark.publisher import cards_state, cards_switch_path, next_month, set_cards

    console = tmp_path / 'console'

    class Exhausted(FakeClient):
        calls = 0

        def send_card(self, chat_id, card, uuid):
            Exhausted.calls += 1
            raise DeliveryError('quota_exhausted', api_code=99991403)

    write_config(private)
    card = publisher(private, Exhausted(), switch_path=cards_switch_path(console))
    try:
        assert card.submit(snapshot(1)) and card.flush(5)
        assert card.status['status'] == 'quota_paused' and card.delivered
        state = cards_state(console)
        assert state['on'] and abs(state['paused_until'] - next_month()) < 5
        assert card.submit(snapshot(2)) and card.flush(5)
        assert Exhausted.calls == 1  # paused: no more calls this month
        row = store(private).get('yslzm', '2026-11-17', 'oc_test')
        assert (
            row['delivery_status'] == 'quota_paused' and row['snapshot']['tasks'][0]['current'] == 2
        )
    finally:
        card.close()
    # A second scheduler sees the shared pause and does not call Feishu either.
    other = publisher(private, Exhausted(), switch_path=cards_switch_path(console))
    try:
        assert other.submit(snapshot(3)) and other.flush(5)
        assert Exhausted.calls == 1 and other.status['status'] == 'quota_paused'
    finally:
        other.close()
    # The 1st has come (or someone switched cards on): the newest display goes out.
    set_cards(True, console)
    assert cards_state(console)['paused_until'] is None
    client = FakeClient()
    resumed = publisher(private, client, switch_path=cards_switch_path(console))
    try:
        assert resumed.submit(snapshot(4)) and resumed.flush(5)
        assert resumed.status['status'] == 'sent' and client.sent
    finally:
        resumed.close()


def test_without_a_bot_the_board_still_gets_every_display(private):
    card = publisher(private, FakeClient())
    try:
        assert card.submit(snapshot(5))
        card.flush(1)
        assert card.status['status'] == 'local' and card.status['error_code'] == 'config_missing'
    finally:
        card.close()
    row = store(private).get('yslzm', '2026-11-17', 'local')
    assert row['snapshot']['tasks'][0]['current'] == 5


def test_http_429_tells_a_used_up_quota_from_a_rate_limit(monkeypatch):
    import io
    from urllib import error

    from autocontrol.lark import client as lark_client

    def opener_for(body):
        class Opener:
            def open(self, req, timeout):
                raise error.HTTPError(req.full_url, 429, 'Too Many Requests', {}, io.BytesIO(body))

        return lambda *handlers: Opener()

    monkeypatch.setattr(lark_client.request, 'build_opener',
                        opener_for(b'{"code":99991403,"msg":"quota exceeded"}'))  # fmt: skip
    with pytest.raises(DeliveryError) as raised:
        lark_client.request_json('POST', 'https://open.feishu.cn/x', {}, {}, 5)
    assert (raised.value.code, raised.value.api_code) == ('quota_exhausted', 99991403)
    monkeypatch.setattr(lark_client.request, 'build_opener', opener_for(b'{"code":99991400}'))
    with pytest.raises(DeliveryError) as raised:
        lark_client.request_json('POST', 'https://open.feishu.cn/x', {}, {}, 5)
    assert raised.value.code == 'rate_limited'
