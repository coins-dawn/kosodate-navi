import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from backend import (barrier, gtfs, indoor, profile, spots, stages, transit, walk)  # noqa: E402


@pytest.fixture(scope="session")
def all_stages():
    return stages.load_all()


@pytest.fixture(scope="session")
def every_stage():
    """まだ使えない舞台（enabled: false）も含めた全部。画面の一覧のテストで使う。"""
    return stages.load_all(include_disabled=True)


@pytest.fixture(scope="session")
def nagareyama(all_stages):
    return all_stages["nagareyama"]


@pytest.fixture(scope="session")
def minato(all_stages):
    return all_stages["minato"]


@pytest.fixture(scope="session")
def nagareyama_con(nagareyama):
    return gtfs.connect(nagareyama.db_path)


@pytest.fixture(scope="session")
def client():
    from backend.app import app
    app.config["TESTING"] = True
    return app.test_client()
