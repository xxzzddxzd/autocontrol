from pathlib import Path

import pytest

from autocontrol.projects.yslzm import YslzmProject


@pytest.fixture
def private(tmp_path) -> Path:
    path = tmp_path / 'private'
    path.mkdir(mode=0o700)
    (path / 'state').mkdir(mode=0o700)
    return path


@pytest.fixture
def project(tmp_path, private) -> YslzmProject:
    workdir = tmp_path / 'workdir'
    workdir.mkdir()
    return YslzmProject(workdir=workdir, private=private)
