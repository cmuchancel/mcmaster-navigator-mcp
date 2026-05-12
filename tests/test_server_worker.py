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


def test_dispatch_retries_recoverable_structured_error(monkeypatch):
    class FakeNavigator:
        instances = []

        def __init__(self):
            self.closed = False
            self.index = len(self.instances)
            self.instances.append(self)

        def close(self):
            self.closed = True

    calls = []

    def fake_dispatch_once(action, payload, navigator):
        calls.append(navigator.index)
        if len(calls) == 1:
            return {
                "status": "error",
                "diagnostics": {
                    "error": "NoSuchWindowException: target window already closed",
                },
            }
        return {"status": "unique", "part_number": "90696A101"}

    monkeypatch.setattr(server, "McMasterNavigator", FakeNavigator)
    monkeypatch.setattr(server, "_navigator", None)
    monkeypatch.setattr(server, "_dispatch_once", fake_dispatch_once)

    result = server._dispatch("find_exact_part", {"description": "demo"})

    assert result == {"status": "unique", "part_number": "90696A101"}
    assert calls == [0, 1]
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


def test_configure_openai_api_key_from_file(tmp_path, monkeypatch):
    key_file = tmp_path / "openai_key.txt"
    key_file.write_text("test-key\n", encoding="utf-8")
    monkeypatch.setattr(server, "_openai_api_key", None)

    args = server.parse_args(["--openai-api-key-file", str(key_file)])
    server.configure_from_args(args)

    assert server._openai_api_key == "test-key"


def test_configure_rejects_two_openai_key_sources():
    args = server.parse_args(["--openai-api-key", "inline", "--openai-api-key-file", "key.txt"])

    with pytest.raises(SystemExit, match="Use only one"):
        server.configure_from_args(args)


def test_find_exact_part_passes_configured_api_key(monkeypatch):
    from mcmaster_navigator_mcp import schema_resolver

    def fake_resolve(navigator, description, **kwargs):
        return {
            "description": description,
            "api_key": kwargs.get("api_key"),
            "search_query": kwargs.get("search_query"),
        }

    monkeypatch.setattr(server, "_openai_api_key", "configured-key")
    monkeypatch.setattr(schema_resolver, "resolve_exact_part_dynamic", fake_resolve)

    result = server._dispatch_once(
        "find_exact_part",
        {"description": "demo part", "search_query": "demo"},
        object(),
    )

    assert result == {
        "description": "demo part",
        "api_key": "configured-key",
        "search_query": "demo",
    }
