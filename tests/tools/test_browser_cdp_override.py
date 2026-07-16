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

    def test_redacts_secret_query_params_in_success_log(self):
        from tools.browser_tool import _resolve_cdp_override

        raw = "https://cdp.example/json/version?access_token=super-secret-token-123456"
        resolved_ws = "wss://cdp.example/devtools/browser/abc?token=super-secret-token-123456"

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": resolved_ws}

        with patch("tools.browser_tool.requests.get", return_value=response), \
                patch("tools.browser_tool.logger.info") as mock_info:
            resolved = _resolve_cdp_override(raw)

        assert resolved == resolved_ws
        mock_info.assert_called_once()
        _, logged_raw, logged_ws = mock_info.call_args.args
        assert "super-secret-token-123456" not in logged_raw
        assert "super-secret-token-123456" not in logged_ws
        assert "access_token=***" in logged_raw
        assert "token=***" in logged_ws

    def test_redacts_secret_query_params_in_failure_log(self):
        from tools.browser_tool import _resolve_cdp_override

        raw = "https://cdp.example?access_token=super-secret-token-123456"
        secret_error = RuntimeError(
            "upstream rejected https://cdp.example/json/version?access_token=super-secret-token-123456"
        )

        with patch("tools.browser_tool.requests.get", side_effect=secret_error), \
                patch("tools.browser_tool.logger.warning") as mock_warning:
            resolved = _resolve_cdp_override(raw)

        assert resolved == raw
        mock_warning.assert_called_once()
        _, logged_raw, logged_version_url, logged_error = mock_warning.call_args.args
        assert "super-secret-token-123456" not in logged_raw
        assert "super-secret-token-123456" not in logged_version_url
        assert "super-secret-token-123456" not in logged_error
        assert "access_token=***" in logged_raw
        assert "access_token=***" in logged_version_url
        assert "access_token=***" in logged_error
        assert logged_version_url.startswith("https://cdp.example")

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

    def test_camofox_yields_to_config_cdp_override(self, monkeypatch):
        """CAMOFOX_URL + a persistent browser.cdp_url config override must NOT
        report camofox mode: the CDP browser takes precedence so navigation is
        not routed through Camofox, and the CDP backend stays non-local for SSRF
        checks. Regression for the env-only suppression gap (config CDP was
        ignored, so CAMOFOX_URL + config CDP still dispatched to Camofox)."""
        import tools.browser_camofox as bc

        monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

        # No CDP anywhere -> camofox mode is on.
        with patch("hermes_cli.config.read_raw_config", return_value={}):
            assert bc.is_camofox_mode() is True

        # A config-only CDP override suppresses camofox.
        with patch("hermes_cli.config.read_raw_config",
                   return_value={"browser": {"cdp_url": HTTP_URL}}):
            assert bc.is_camofox_mode() is False

        # The env override still suppresses camofox.
        monkeypatch.setenv("BROWSER_CDP_URL", HTTP_URL)
        with patch("hermes_cli.config.read_raw_config", return_value={}):
            assert bc.is_camofox_mode() is False

class TestCreateCdpSession:
    """_create_cdp_session() must sanitize the CDP URL before logging.

    PR #54851 added _sanitize_url_for_logs() and wired it into the three log
    sites inside _resolve_cdp_override(). This test guards the fourth site
    that was missed: the logger.info call inside _create_cdp_session(), which
    receives the already-resolved CDP URL and could contain a query-string
    token (e.g. wss://provider.example/session?token=secret).
    """

    def test_redacts_token_in_session_creation_log(self):
        from tools.browser_tool import _create_cdp_session

        cdp_url_with_token = "wss://cdp.example/devtools/browser/abc?token=super-secret-token-999"

        with patch("tools.browser_tool.logger.info") as mock_info:
            result = _create_cdp_session("task-1", cdp_url_with_token)

        assert result["cdp_url"] == cdp_url_with_token, "raw URL must be stored unmodified"

        mock_info.assert_called_once()
        logged_args = " ".join(str(a) for a in mock_info.call_args.args)
        assert "super-secret-token-999" not in logged_args
        assert "token=***" in logged_args

    def test_plain_url_without_secrets_passes_through(self):
        from tools.browser_tool import _create_cdp_session

        plain_url = "ws://localhost:9222/devtools/browser/abc123"

        with patch("tools.browser_tool.logger.info") as mock_info:
            _create_cdp_session("task-2", plain_url)

        logged_args = " ".join(str(a) for a in mock_info.call_args.args)
        assert "localhost:9222" in logged_args


class TestCDPSupervisorTimeoutRedaction:
    """CDPSupervisor.start() TimeoutError must not expose raw CDP credentials.

    The supervisor raises TimeoutError(f"... (cdp_url={self.cdp_url[:80]}...)")
    when attach times out.  A URL with a query-string token (e.g.
    wss://provider.example/session?token=secret) would embed the raw secret
    in the exception message, which propagates to caller logs and tracebacks.
    """

    def _make_timed_out_supervisor(self, cdp_url: str):
        """Return a CDPSupervisor whose start() will time out immediately."""
        import threading
        from tools.browser_supervisor import CDPSupervisor

        sup = CDPSupervisor.__new__(CDPSupervisor)
        sup.task_id = "test-task"
        sup.cdp_url = cdp_url
        sup._start_error = None
        sup._stop_requested = False
        sup._loop = None
        # _thread = None so the is_alive() early-return guard is skipped.
        sup._thread = None
        # _ready_event that never fires so wait() always returns False.
        never_ready = threading.Event()
        sup._ready_event = never_ready
        return sup

    def test_timeout_error_redacts_query_token(self):
        cdp_url = "wss://cdp.example/devtools/browser/abc?token=super-secret-999"
        sup = self._make_timed_out_supervisor(cdp_url)

        with patch("threading.Thread") as mock_thread_cls, patch.object(sup, "stop"):
            mock_thread_cls.return_value = Mock()
            try:
                sup.start(timeout=0.001)
            except TimeoutError as exc:
                msg = str(exc)
                assert "super-secret-999" not in msg, (
                    "raw token must not appear in TimeoutError message"
                )
                assert "cdp_url=" in msg
            else:
                raise AssertionError("TimeoutError was not raised")

    def test_timeout_error_preserves_plain_url(self):
        plain_url = "ws://127.0.0.1:9222/devtools/browser/abc"
        sup = self._make_timed_out_supervisor(plain_url)

        with patch("threading.Thread") as mock_thread_cls, patch.object(sup, "stop"):
            mock_thread_cls.return_value = Mock()
            try:
                sup.start(timeout=0.001)
            except TimeoutError as exc:
                assert "127.0.0.1:9222" in str(exc)
            else:
                raise AssertionError("TimeoutError was not raised")


class TestCDPSupervisorStartErrorRedaction:
    """CDPSupervisor.start() must not leak the CDP URL via the connect-error path.

    The more common failure mode than attach-timeout: the first
    websockets.connect(self.cdp_url) raises (bad URI, refused, TLS), the raw
    exception is stashed as self._start_error, and start() re-raises it. Those
    websockets exceptions embed the full raw cdp_url -- token and userinfo --
    in their message. start() must re-raise a REDACTED error and must not leak
    the secret via the exception message or the traceback cause chain.
    """

    def _run_start_hitting_error(self, cdp_url: str, start_error: BaseException):
        """Invoke start() so it takes the _start_error re-raise branch.

        start() clears _ready_event / _start_error and launches a thread, so we
        can't pre-seed them. Instead we stub threading.Thread: the fake thread's
        start() synchronously populates _start_error and sets the ready event,
        exactly as the real supervisor loop does on a first-connect failure.
        """
        import threading
        from tools.browser_supervisor import CDPSupervisor

        sup = CDPSupervisor.__new__(CDPSupervisor)
        sup.task_id = "test-task"
        sup.cdp_url = cdp_url
        sup._start_error = None
        sup._stop_requested = False
        sup._loop = None
        sup._thread = None
        sup._ready_event = threading.Event()

        def _fake_thread(*args, **kwargs):
            fake = Mock()

            def _start():
                sup._start_error = start_error
                sup._ready_event.set()

            fake.start.side_effect = _start
            fake.is_alive.return_value = False
            return fake

        with patch("threading.Thread", side_effect=_fake_thread), patch.object(sup, "stop"):
            sup.start(timeout=5.0)

    def test_start_error_redacts_query_token(self):
        # A realistic websockets-style error embedding the raw URL + token.
        raw = "wss://cdp.example/devtools/browser/abc?token=super-secret-999"
        err = ValueError(f"{raw} isn't a valid URI: hostname isn't provided")
        try:
            self._run_start_hitting_error(raw, err)
        except Exception as exc:  # noqa: BLE001 - asserting on the surface
            msg = str(exc)
            assert "super-secret-999" not in msg, (
                "raw token must not appear in the re-raised error message"
            )
            # The raw cause must be suppressed so it can't leak via traceback.
            assert exc.__cause__ is None
            assert getattr(exc, "__suppress_context__", False) is True
        else:
            raise AssertionError("start() did not re-raise the start error")

    def test_start_error_redacts_userinfo_password(self):
        raw = "wss://user:p4ssw0rd@cdp.example/devtools/browser/x"
        err = ValueError(f"{raw} isn't a valid URI: hostname isn't provided")
        try:
            self._run_start_hitting_error(raw, err)
        except Exception as exc:  # noqa: BLE001
            assert "p4ssw0rd" not in str(exc)
        else:
            raise AssertionError("start() did not re-raise the start error")


class TestRedactCdpErrorText:
    """The supervisor's error-text chokepoint masks credentials, keeps context."""

    def test_masks_query_token_in_exception(self):
        from tools.browser_supervisor import _redact_cdp_error_text

        err = ConnectionError("connect wss://h/x?token=leak-me failed")
        out = _redact_cdp_error_text(err)
        assert "leak-me" not in out

    def test_preserves_non_secret_context(self):
        from tools.browser_supervisor import _redact_cdp_error_text

        err = ConnectionError("connect ws://127.0.0.1:9222/x failed: refused")
        out = _redact_cdp_error_text(err)
        assert "127.0.0.1:9222" in out
        assert "refused" in out
