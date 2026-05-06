from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from mcmaster_navigator_mcp import server


def test_dispatch_retries_recoverable_browser_error_without_closing_new_session(monkeypatch):
    class FakeNavigator:
        instances = []

        def __init__(self):
            self.closed = False
            self.index = len(self.instances)
            self.instances.append(self)

        def doctor(self):
            if self.index == 0:
                raise RuntimeError("chrome not reachable")
            return {"ok": True, "index": self.index}

        def close(self):
            self.closed = True

    monkeypatch.setattr(server, "McMasterNavigator", FakeNavigator)
    monkeypatch.setattr(server, "_navigator", None)

    result = server._dispatch("doctor", {})

    assert result == {"ok": True, "index": 1}
    assert [navigator.closed for navigator in FakeNavigator.instances] == [True, False]

    server._reset_navigator()


def test_call_worker_timeout_replaces_executor_and_closes_browser(monkeypatch):
    class FakeNavigator:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    old_executor = ThreadPoolExecutor(max_workers=1)
    old_navigator = FakeNavigator()

    def slow_dispatch(action, payload):
        time.sleep(0.2)
        return {"late": True}

    monkeypatch.setenv("MCMASTER_NAV_TOOL_TIMEOUT", "0.01")
    monkeypatch.setattr(server, "_executor", old_executor)
    monkeypatch.setattr(server, "_navigator", old_navigator)
    monkeypatch.setattr(server, "_dispatch", slow_dispatch)

    with pytest.raises(TimeoutError, match="exceeded 0.01s"):
        asyncio.run(server._call_worker("slow", {}))

    assert old_navigator.closed is True
    assert server._executor is not old_executor

    server._executor.shutdown(wait=False, cancel_futures=True)
