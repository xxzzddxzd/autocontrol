"""The animated running icon is uploaded once per bot and cached privately."""

import json
import stat

import pytest

from autocontrol.lark import assets
from autocontrol.lark.client import DeliveryError


class Client:
    def __init__(self, app='bot-a', key='img_v3_spinner'):
        self.app_fingerprint = app
        self.key = key
        self.uploads = []

    def upload_image(self, data, filename, content_type):
        self.uploads.append((len(data), filename, content_type))
        if isinstance(self.key, Exception):
            raise self.key
        return self.key


def test_the_spinner_is_uploaded_once_and_reused(tmp_path):
    client = Client()
    assert assets.spinner_key(tmp_path, client) == 'img_v3_spinner'
    assert assets.spinner_key(tmp_path, client) == 'img_v3_spinner'
    assert client.uploads == [(assets.SPINNER.stat().st_size, 'spinner.webp', 'image/webp')]
    cache = tmp_path / assets.CACHE_NAME
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    saved = json.loads(cache.read_text())['spinner']
    assert saved['app'] == 'bot-a' and saved['key'] == 'img_v3_spinner'


def test_another_bot_uploads_its_own_copy(tmp_path):
    assets.spinner_key(tmp_path, Client(app='bot-a', key='img_v3_a'))
    other = Client(app='bot-b', key='img_v3_b')
    assert assets.spinner_key(tmp_path, other) == 'img_v3_b' and len(other.uploads) == 1


def test_a_failed_upload_is_raised_and_nothing_is_cached(tmp_path):
    with pytest.raises(DeliveryError):
        assets.spinner_key(tmp_path, Client(key=DeliveryError('permission_denied')))
    assert not (tmp_path / assets.CACHE_NAME).exists()


def test_the_asset_is_an_animated_webp():
    data = assets.SPINNER.read_bytes()
    assert data[:4] == b'RIFF' and data[8:12] == b'WEBP' and b'ANIM' in data[:64]
