from unittest.mock import MagicMock, Mock, patch

import pytest


HOST = "example-host"
PORT = 9223
WS_URL = f"ws://{HOST}:{PORT}/devtools/browser/abc123"
HTTP_URL = f"http://{HOST}:{PORT}"
VERSION_URL = f"{HTTP_URL}/json/version"


class TestResolveCdpOverride:
    def test_keeps_full_devtools_websocket_url(self):
        from tools.browser_tool import _resolve_cdp_override

        assert _resolve_cdp_override(WS_URL) == WS_URL

    def test_resolves_http_discovery_endpoint_to_websocket(self):
        from tools.browser_tool import _resolve_cdp_override

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = _resolve_cdp_override(HTTP_URL)

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)

    def test_resolves_bare_ws_hostport_to_discovery_websocket(self):
        from tools.browser_tool import _resolve_cdp_override

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = _resolve_cdp_override(f"ws://{HOST}:{PORT}")

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)

    def test_falls_back_to_raw_url_when_discovery_fails(self):
        from tools.browser_tool import _resolve_cdp_override

        with patch("tools.browser_tool.requests.get", side_effect=RuntimeError("boom")):
            assert _resolve_cdp_override(HTTP_URL) == HTTP_URL

    def test_normalizes_provider_returned_http_cdp_url_when_creating_session(self, monkeypatch):
        import tools.browser_tool as browser_tool

        provider = Mock()
        provider.create_session.return_value = {
            "session_name": "cloud-session",
            "bb_session_id": "bu_123",
            "cdp_url": "https://cdp.browser-use.example/session",
            "features": {"browser_use": True},
        }

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        monkeypatch.setattr(browser_tool, "_active_sessions", {})
        monkeypatch.setattr(browser_tool, "_session_last_activity", {})
        monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
        monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task_id: None)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "")
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            session_info = browser_tool._get_session_info("task-browser-use")

        assert session_info["cdp_url"] == WS_URL
        provider.create_session.assert_called_once_with("task-browser-use")
        mock_get.assert_called_once_with(
            "https://cdp.browser-use.example/session/json/version",
            timeout=10,
        )


class TestPlaywrightCdpBackend:
    def test_get_cdp_execution_backend_defaults_to_playwright_cdp(self, monkeypatch):
        from tools.browser_tool import _get_cdp_execution_backend

        monkeypatch.delenv("BROWSER_CDP_BACKEND", raising=False)

        assert _get_cdp_execution_backend() == "playwright-cdp"

    def test_get_cdp_execution_backend_prefers_config_when_env_missing(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.delenv("BROWSER_CDP_BACKEND", raising=False)

        class FakeConfigModule:
            @staticmethod
            def read_raw_config():
                return {"browser": {"cdp_execution_backend": "agent-browser"}}

        import sys
        monkeypatch.setitem(sys.modules, "hermes_cli.config", FakeConfigModule)

        assert browser_tool._get_cdp_execution_backend() == "agent-browser"

    def test_run_browser_command_routes_cdp_override_to_playwright_backend(self, monkeypatch):
        import tools.browser_tool as browser_tool

        session_info = {
            "session_name": "cdp-session",
            "bb_session_id": None,
            "cdp_url": WS_URL,
            "features": {"cdp_override": True, "cdp_backend": "playwright-cdp"},
        }
        routed = {}

        def fake_run(command, args, *, cdp_url, session_store, task_id):
            routed.update({
                "command": command,
                "args": args,
                "cdp_url": cdp_url,
                "session_store": session_store,
                "task_id": task_id,
            })
            return {"success": True, "data": {"title": "Example", "url": "https://example.com"}}

        monkeypatch.setattr(browser_tool, "_get_session_info", lambda task_id: session_info)
        monkeypatch.setattr(browser_tool, "_get_cdp_execution_backend", lambda: browser_tool._PLAYWRIGHT_CDP_BACKEND)
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
        monkeypatch.setattr(browser_tool, "_playwright_sessions", {})

        with patch("tools.browser_playwright_cdp.run_playwright_cdp_command", side_effect=fake_run) as mock_run:
            result = browser_tool._run_browser_command("task-pw", "navigate", ["https://example.com"])

        assert result == {"success": True, "data": {"title": "Example", "url": "https://example.com"}}
        assert routed == {
            "command": "navigate",
            "args": ["https://example.com"],
            "cdp_url": WS_URL,
            "session_store": browser_tool._playwright_sessions,
            "task_id": "task-pw",
        }
        mock_run.assert_called_once()

    def test_run_browser_command_surfaces_playwright_backend_errors(self, monkeypatch):
        import tools.browser_tool as browser_tool

        session_info = {
            "session_name": "cdp-session",
            "bb_session_id": None,
            "cdp_url": WS_URL,
            "features": {"cdp_override": True, "cdp_backend": "playwright-cdp"},
        }

        monkeypatch.setattr(browser_tool, "_get_session_info", lambda task_id: session_info)
        monkeypatch.setattr(browser_tool, "_get_cdp_execution_backend", lambda: browser_tool._PLAYWRIGHT_CDP_BACKEND)
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

        with patch("tools.browser_playwright_cdp.run_playwright_cdp_command", side_effect=RuntimeError("playwright failed")):
            result = browser_tool._run_browser_command("task-pw", "snapshot", [])

        assert result == {"success": False, "error": "playwright failed"}

    def test_cleanup_browser_closes_cached_playwright_session(self, monkeypatch):
        import tools.browser_tool as browser_tool

        closed = []
        fake_session = type("FakePlaywrightSession", (), {"close": lambda self: closed.append("closed")})()

        monkeypatch.setattr(browser_tool, "_active_sessions", {
            "task-pw": {
                "session_name": "cdp-session",
                "bb_session_id": None,
                "cdp_url": WS_URL,
                "features": {"cdp_override": True, "cdp_backend": "playwright-cdp"},
            }
        })
        monkeypatch.setattr(browser_tool, "_session_last_activity", {"task-pw": 1.0})
        monkeypatch.setattr(browser_tool, "_playwright_sessions", {"task-pw": fake_session})
        monkeypatch.setattr(browser_tool, "_run_browser_command", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not call _run_browser_command for Playwright CDP cleanup")))
        monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(browser_tool, "_maybe_stop_recording", lambda task_id: None)
        monkeypatch.setattr(browser_tool, "_socket_safe_tmpdir", lambda: "/tmp/hermes-tests")
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: None)

        browser_tool.cleanup_browser("task-pw")

        assert closed == ["closed"]
        assert browser_tool._playwright_sessions == {}
        assert browser_tool._active_sessions == {}
        assert browser_tool._session_last_activity == {}

    def test_cleanup_browser_skips_close_command_for_playwright_backend_without_cached_session(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setattr(browser_tool, "_active_sessions", {
            "task-pw": {
                "session_name": "cdp-session",
                "bb_session_id": None,
                "cdp_url": WS_URL,
                "features": {"cdp_override": True, "cdp_backend": "playwright-cdp"},
            }
        })
        monkeypatch.setattr(browser_tool, "_session_last_activity", {"task-pw": 1.0})
        monkeypatch.setattr(browser_tool, "_playwright_sessions", {})
        monkeypatch.setattr(browser_tool, "_run_browser_command", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not call _run_browser_command when session is marked as Playwright CDP backend")))
        monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(browser_tool, "_maybe_stop_recording", lambda task_id: None)
        monkeypatch.setattr(browser_tool, "_socket_safe_tmpdir", lambda: "/tmp/hermes-tests")
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: None)

        browser_tool.cleanup_browser("task-pw")

        assert browser_tool._active_sessions == {}
        assert browser_tool._session_last_activity == {}


class TestGetCdpOverride:
    def test_prefers_env_var_over_config(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setenv("BROWSER_CDP_URL", HTTP_URL)
        monkeypatch.setattr(
            browser_tool,
            "read_raw_config",
            lambda: {"browser": {"cdp_url": "http://config-host:9222"}},
            raising=False,
        )

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = browser_tool._get_cdp_override()

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)

    def test_uses_config_browser_cdp_url_when_env_missing(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("hermes_cli.config.read_raw_config", return_value={"browser": {"cdp_url": HTTP_URL}}), \
             patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = browser_tool._get_cdp_override()

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)


class TestBrowserPlaywrightCdpModule:
    def test_pick_best_page_prefers_exact_url_match(self):
        from tools.browser_playwright_cdp import _pick_best_page

        page_blank = MagicMock()
        page_blank.is_closed.return_value = False
        page_blank.url = "about:blank"
        page_blank.title.return_value = ""

        page_target = MagicMock()
        page_target.is_closed.return_value = False
        page_target.url = "https://x.com/home"
        page_target.title.return_value = "(2) 主页 / X"

        context = MagicMock()
        context.pages = [page_blank, page_target]

        session = MagicMock()
        session.context = context
        session.page = page_blank

        picked = _pick_best_page(session, "https://x.com/home")

        assert picked is page_target
        assert session.page is page_target

    def test_pick_best_page_prefers_non_blank_when_no_target_match(self):
        from tools.browser_playwright_cdp import _pick_best_page

        page_blank = MagicMock()
        page_blank.is_closed.return_value = False
        page_blank.url = "about:blank"
        page_blank.title.return_value = ""

        page_existing = MagicMock()
        page_existing.is_closed.return_value = False
        page_existing.url = "https://example.com/app"
        page_existing.title.return_value = "Example App"

        context = MagicMock()
        context.pages = [page_blank, page_existing]

        session = MagicMock()
        session.context = context
        session.page = page_blank

        picked = _pick_best_page(session, "https://x.com/home")

        assert picked is page_existing
        assert session.page is page_existing

    def test_run_playwright_cdp_command_reuses_cached_session(self):
        from tools.browser_playwright_cdp import run_playwright_cdp_command

        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://example.com/next"
        page.title.return_value = "Example"

        session = MagicMock()
        session.page = page
        session.context.pages = [page]

        store = {"task-1": session}
        result = run_playwright_cdp_command(
            "navigate",
            ["https://example.com/next"],
            cdp_url=WS_URL,
            session_store=store,
            task_id="task-1",
        )

        page.goto.assert_not_called()
        page.bring_to_front.assert_called_once()
        assert result == {"success": True, "data": {"title": "Example", "url": "https://example.com/next"}}
        assert store["task-1"] is session

    def test_run_playwright_cdp_command_reuses_matching_page_without_goto(self):
        from tools.browser_playwright_cdp import run_playwright_cdp_command

        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://x.com/home"
        page.title.return_value = "(2) 主页 / X"

        session = MagicMock()
        session.page = page
        session.context.pages = [page]

        store = {"task-1": session}
        result = run_playwright_cdp_command(
            "navigate",
            ["https://x.com/home"],
            cdp_url=WS_URL,
            session_store=store,
            task_id="task-1",
        )

        page.goto.assert_not_called()
        page.bring_to_front.assert_called_once()
        assert result == {"success": True, "data": {"title": "(2) 主页 / X", "url": "https://x.com/home"}}

    def test_run_playwright_cdp_command_creates_session_on_first_use(self, monkeypatch):
        import tools.browser_playwright_cdp as browser_playwright_cdp

        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://example.com"
        page.title.return_value = "Example"

        session = MagicMock()
        session.page = page
        session.context.pages = [page]

        monkeypatch.setattr(browser_playwright_cdp, "PlaywrightCdpSession", lambda cdp_url: session)
        store = {}

        result = browser_playwright_cdp.run_playwright_cdp_command(
            "navigate",
            ["https://example.com"],
            cdp_url=WS_URL,
            session_store=store,
            task_id="task-2",
        )

        page.goto.assert_not_called()
        page.bring_to_front.assert_called_once()
        assert result["success"] is True
        assert store == {"task-2": session}

    def test_run_playwright_cdp_command_snapshot_returns_compact_refs(self):
        from tools.browser_playwright_cdp import run_playwright_cdp_command

        page = MagicMock()
        page.is_closed.return_value = False
        page.evaluate.return_value = {
            "snapshot": "[@e1] button Submit",
            "refs": {"@e1": {"role": "button", "text": "Submit", "tag": "button"}},
        }

        session = MagicMock()
        session.page = page
        session.context.pages = [page]

        result = run_playwright_cdp_command(
            "snapshot",
            [],
            cdp_url=WS_URL,
            session_store={"task-3": session},
            task_id="task-3",
        )

        assert result == {
            "success": True,
            "data": {
                "snapshot": "[@e1] button Submit",
                "refs": {"@e1": {"role": "button", "text": "Submit", "tag": "button"}},
            },
        }

    @pytest.mark.parametrize(
        ("command", "args", "expected_call"),
        [
            ("click", ["@e1"], "click"),
            ("fill", ["@e1", "hello"], "fill"),
            ("press", ["Enter"], "press"),
        ],
    )
    def test_run_playwright_cdp_command_dispatches_interactions(self, command, args, expected_call):
        from tools.browser_playwright_cdp import run_playwright_cdp_command

        locator = MagicMock()
        page = MagicMock()
        page.is_closed.return_value = False
        page.locator.return_value.first = locator

        session = MagicMock()
        session.page = page
        session.context.pages = [page]

        result = run_playwright_cdp_command(
            command,
            args,
            cdp_url=WS_URL,
            session_store={"task-4": session},
            task_id="task-4",
        )

        assert result == {"success": True, "data": {}}
        if expected_call == "click":
            locator.click.assert_called_once_with(timeout=10000)
        elif expected_call == "fill":
            locator.fill.assert_called_once_with("hello", timeout=10000)
        elif expected_call == "press":
            page.keyboard.press.assert_called_once_with("Enter")

    def test_run_playwright_cdp_command_close_removes_cached_session(self):
        from tools.browser_playwright_cdp import run_playwright_cdp_command

        session = MagicMock()
        page = MagicMock()
        page.is_closed.return_value = False
        session.page = page
        session.context.pages = [page]
        store = {"task-5": session}

        result = run_playwright_cdp_command("close", [], cdp_url=WS_URL, session_store=store, task_id="task-5")

        session.close.assert_called_once_with()
        assert result == {"success": True, "data": {}}
        assert store == {}

    def test_run_playwright_cdp_command_rejects_unsupported_commands(self):
        from tools.browser_playwright_cdp import run_playwright_cdp_command

        page = MagicMock()
        page.is_closed.return_value = False
        session = MagicMock()
        session.page = page
        session.context.pages = [page]

        result = run_playwright_cdp_command(
            "errors",
            [],
            cdp_url=WS_URL,
            session_store={"task-6": session},
            task_id="task-6",
        )

        assert result["success"] is False
        assert "does not support command 'errors'" in result["error"]

