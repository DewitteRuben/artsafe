import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from artsafe import logs


class Captured:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)

        class R:
            def close(self):
                pass
        return R()


@pytest.fixture
def reporter(monkeypatch):
    cap = Captured()
    monkeypatch.setattr(logs.urllib.request, "urlopen", cap)
    r = logs.DiscordReporter("https://discord.test/hook")
    yield r, cap


def wait_for(cap, n, timeout=5):
    end = time.time() + timeout
    while len(cap.requests) < n and time.time() < end:
        time.sleep(0.05)
    return cap.requests


def test_error_is_sent_without_her_user_name(reporter):
    r, cap = reporter
    home = str(Path.home())
    try:
        raise RuntimeError(f"could not open {home}\\Desktop\\dragon.psd")
    except RuntimeError:
        rec = logging.getLogger("t").makeRecord("t", logging.ERROR, __file__, 1, "Backup failed", (), sys.exc_info())
    r.emit(rec)
    [req] = wait_for(cap, 1)
    content = json.loads(req.data)["content"]
    assert "Backup failed" in content and "RuntimeError" in content
    assert "~\\Desktop\\dragon.psd" in content and home not in content


def test_same_error_is_not_sent_again_and_again(reporter):
    r, cap = reporter
    rec = logging.getLogger("t").makeRecord("t", logging.ERROR, __file__, 7, "Same problem", (), None)
    for _ in range(10):
        r.emit(rec)
    time.sleep(0.5)
    assert len(cap.requests) == 1


def test_daily_cap(reporter):
    r, cap = reporter
    for i in range(logs.MAX_REPORTS_PER_DAY + 10):
        r.post("x", f"different error {i}")
    time.sleep(1)
    assert len(wait_for(cap, logs.MAX_REPORTS_PER_DAY)) == logs.MAX_REPORTS_PER_DAY


def test_manual_report_attaches_log(reporter, tmp_path):
    r, cap = reporter
    f = tmp_path / "artsafe.log"
    f.write_text(f"line one\nsomething in {Path.home()}\\Pictures\n")
    assert r.post("Report", "it broke", attach=f)
    [req] = wait_for(cap, 1)
    body = req.data.decode()
    assert "multipart/form-data" in req.headers["Content-type"]
    assert 'filename="artsafe.log"' in body and "~\\Pictures" in body and str(Path.home()) not in body


def test_reports_can_be_switched_off(monkeypatch):
    cap = Captured()
    monkeypatch.setattr(logs.urllib.request, "urlopen", cap)
    r = logs.DiscordReporter("https://discord.test/hook", enabled=lambda: False)
    assert r.post("x", "y") is False
    time.sleep(0.3)
    assert cap.requests == []
