import asyncio
import shutil
import subprocess
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.restart import DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
from gateway.session import SessionEntry, SessionSource, build_session_key
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.mark.asyncio
async def test_restart_command_while_busy_requests_drain_without_interrupt(monkeypatch):
    # Ensure INVOCATION_ID is NOT set — systemd sets this in service mode,
    # which changes the restart call signature.
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m1",
    )
    session_key = build_session_key(event.source)
    running_agent = MagicMock()
    runner._running_agents[session_key] = running_agent

    result = await runner._handle_message(event)

    assert result == "⏳ Draining 1 active agent(s) before restart..."
    running_agent.interrupt.assert_not_called()
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)


@pytest.mark.asyncio
async def test_drain_queue_mode_queues_follow_up_without_interrupt():
    runner, adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"

    event = MessageEvent(
        text="follow up",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m2",
    )
    session_key = build_session_key(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter.handle_message(event)

    assert session_key in adapter._pending_messages
    assert adapter._pending_messages[session_key].text == "follow up"
    assert not adapter._active_sessions[session_key].is_set()
    assert any("queued for the next turn" in message for message in adapter.sent)


@pytest.mark.asyncio
async def test_draining_rejects_new_session_messages():
    runner, _adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True

    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=make_restart_source("fresh"),
        message_id="m3",
    )

    result = await runner._handle_message(event)

    assert result == "⏳ Gateway is restarting and is not accepting new work right now."


@pytest.mark.asyncio
async def test_parallel_busy_mode_starts_detached_reply_without_queueing():
    runner, adapter = make_restart_runner()
    runner._busy_input_mode = "parallel"
    runner._start_parallel_reply_for_busy_session = AsyncMock(return_value="parallel-started")

    event = MessageEvent(
        text="follow up",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m-parallel",
    )
    session_key = build_session_key(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter.handle_message(event)

    runner._start_parallel_reply_for_busy_session.assert_awaited_once_with(event)
    assert session_key not in adapter._pending_messages
    assert not adapter._active_sessions[session_key].is_set()
    assert adapter.sent[-1] == "parallel-started"


@pytest.mark.asyncio
async def test_start_parallel_reply_for_busy_session_schedules_background_reply(monkeypatch):
    runner, _adapter = make_restart_runner()

    class FakeTask:
        def __init__(self):
            self.callbacks = []

        def add_done_callback(self, cb):
            self.callbacks.append(cb)

    fake_task = FakeTask()
    created = {}

    runner._run_background_task = AsyncMock(return_value=None)

    def fake_create_task(coro):
        created["coro"] = coro
        coro.close()
        return fake_task

    monkeypatch.setattr(gateway_run.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(gateway_run.os, "urandom", lambda n: b"abc")

    event = MessageEvent(
        text="Please continue with this parallel request",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m4",
    )

    message = await runner._start_parallel_reply_for_busy_session(event)

    runner._run_background_task.assert_called_once()
    _, kwargs = runner._run_background_task.call_args
    assert kwargs["reply_to_message_id"] == "m4"
    assert kwargs["completion_label"] == "Parallel reply complete"
    assert fake_task in runner._background_tasks
    assert runner._background_tasks.discard in fake_task.callbacks
    assert "started this in parallel" in message
    assert "Task ID: parallel_" in message
    assert "Please continue with this parallel request" in message


class _FeishuBusyAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.FEISHU)
        self.sent: list[str] = []

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "group"}


def _make_feishu_busy_event(text: str = "follow up", message_id: str = "om_topic_1") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.FEISHU,
            chat_id="oc_feishu_group",
            chat_type="group",
            user_id="ou_user_1",
        ),
        message_id=message_id,
    )


@pytest.mark.asyncio
async def test_parallel_busy_mode_uses_feishu_topic_branch_without_queueing():
    runner, _adapter = make_restart_runner()
    adapter = _FeishuBusyAdapter()
    adapter.set_message_handler(AsyncMock(return_value=None))
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    runner.adapters = {Platform.FEISHU: adapter}
    runner._busy_input_mode = "parallel"
    runner._start_feishu_topic_branch_for_busy_session = AsyncMock(return_value="topic-started")
    runner._start_parallel_reply_for_busy_session = AsyncMock(return_value="parallel-started")

    event = _make_feishu_busy_event()
    session_key = build_session_key(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter.handle_message(event)

    runner._start_feishu_topic_branch_for_busy_session.assert_awaited_once_with(event)
    runner._start_parallel_reply_for_busy_session.assert_not_called()
    assert session_key not in adapter._pending_messages
    assert not adapter._active_sessions[session_key].is_set()
    assert adapter.sent[-1] == "topic-started"


@pytest.mark.asyncio
async def test_start_feishu_topic_branch_binds_background_task_to_topic_session(monkeypatch):
    runner, _adapter = make_restart_runner()
    event = _make_feishu_busy_event(
        text="Please track this in a Feishu topic",
        message_id="om_topic_branch",
    )
    branch_source = SessionSource(
        platform=Platform.FEISHU,
        chat_id=event.source.chat_id,
        chat_type=event.source.chat_type,
        user_id=event.source.user_id,
        thread_id=event.message_id,
    )
    branch_entry = SessionEntry(
        session_key=build_session_key(branch_source),
        session_id="sess_feishu_topic_1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=branch_source,
        platform=Platform.FEISHU,
        chat_type="group",
    )
    runner.session_store.get_or_create_session = MagicMock(return_value=branch_entry)

    class FakeTask:
        def __init__(self):
            self.callbacks = []

        def add_done_callback(self, cb):
            self.callbacks.append(cb)

    fake_task = FakeTask()
    runner._run_background_task = AsyncMock(return_value=None)

    def fake_create_task(coro):
        coro.close()
        return fake_task

    monkeypatch.setattr(gateway_run.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(gateway_run.os, "urandom", lambda n: b"abc")

    message = await runner._start_feishu_topic_branch_for_busy_session(event)

    runner.session_store.get_or_create_session.assert_called_once()
    called_source = runner.session_store.get_or_create_session.call_args.args[0]
    assert called_source.thread_id == event.message_id

    runner._run_background_task.assert_called_once()
    args = runner._run_background_task.call_args.args
    kwargs = runner._run_background_task.call_args.kwargs
    assert args[0] == "Please track this in a Feishu topic"
    assert args[1].thread_id == event.message_id
    assert kwargs["session_id"] == "sess_feishu_topic_1"
    assert kwargs["reply_to_message_id"] == event.message_id
    assert kwargs["completion_label"] == "Topic reply complete"
    assert fake_task in runner._background_tasks
    assert runner._background_tasks.discard in fake_task.callbacks
    assert "new Feishu topic" in message
    assert "Keep replying in that topic" in message


def test_load_busy_input_mode_prefers_env_then_config_then_default(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_INPUT_MODE", raising=False)

    assert gateway_run.GatewayRunner._load_busy_input_mode() == "interrupt"

    (tmp_path / "config.yaml").write_text(
        "display:\n  busy_input_mode: queue\n", encoding="utf-8"
    )
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "queue"

    (tmp_path / "config.yaml").write_text(
        "display:\n  busy_input_mode: parallel\n", encoding="utf-8"
    )
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "parallel"

    (tmp_path / "config.yaml").write_text(
        "display:\n  busy_input_mode: steer\n", encoding="utf-8"
    )
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "steer"

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "interrupt")
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "interrupt"

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "parallel")
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "parallel"

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "steer")
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "steer"

    # Unknown values fall through to the safe default
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "bogus")
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "interrupt"


def test_load_restart_drain_timeout_prefers_env_then_config_then_default(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_RESTART_DRAIN_TIMEOUT", raising=False)

    assert (
        gateway_run.GatewayRunner._load_restart_drain_timeout()
        == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    )

    (tmp_path / "config.yaml").write_text(
        "agent:\n  restart_drain_timeout: 12\n", encoding="utf-8"
    )
    assert gateway_run.GatewayRunner._load_restart_drain_timeout() == 12.0

    monkeypatch.setenv("HERMES_RESTART_DRAIN_TIMEOUT", "7")
    assert gateway_run.GatewayRunner._load_restart_drain_timeout() == 7.0

    monkeypatch.setenv("HERMES_RESTART_DRAIN_TIMEOUT", "invalid")
    assert (
        gateway_run.GatewayRunner._load_restart_drain_timeout()
        == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    )
    assert "Invalid restart_drain_timeout" in caplog.text


@pytest.mark.asyncio
async def test_request_restart_is_idempotent():
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()

    assert runner.request_restart(detached=True, via_service=False) is True
    first_task = next(iter(runner._background_tasks))
    assert runner.request_restart(detached=True, via_service=False) is False

    await first_task

    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=True, service_restart=False
    )


@pytest.mark.asyncio
async def test_launch_detached_restart_command_uses_setsid(monkeypatch):
    runner, _adapter = make_restart_runner()
    popen_calls = []

    monkeypatch.setattr(gateway_run, "_resolve_hermes_bin", lambda: ["/usr/bin/hermes"])
    monkeypatch.setattr(gateway_run.os, "getpid", lambda: 321)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/setsid" if cmd == "setsid" else None)

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    await runner._launch_detached_restart_command()

    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    assert cmd[:2] == ["/usr/bin/setsid", "bash"]
    assert "gateway restart" in cmd[-1]
    assert "kill -0 321" in cmd[-1]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


# ── Shutdown notification tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_notification_sent_to_active_sessions():
    """Active sessions receive a notification when the gateway starts shutting down."""
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="999", chat_type="dm")
    session_key = f"agent:main:telegram:dm:999"
    runner._running_agents[session_key] = MagicMock()

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent) == 1
    assert "shutting down" in adapter.sent[0]
    assert "interrupted" in adapter.sent[0]


@pytest.mark.asyncio
async def test_shutdown_notification_says_restarting_when_restart_requested():
    """When _restart_requested is True, the message says 'restarting' and mentions /retry."""
    runner, adapter = make_restart_runner()
    runner._restart_requested = True
    session_key = "agent:main:telegram:dm:999"
    runner._running_agents[session_key] = MagicMock()

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent) == 1
    assert "restarting" in adapter.sent[0]
    assert "resume" in adapter.sent[0]


@pytest.mark.asyncio
async def test_shutdown_notification_deduplicates_per_chat():
    """Multiple sessions in the same chat only get one notification."""
    runner, adapter = make_restart_runner()
    # Two sessions (different users) in the same chat
    runner._running_agents["agent:main:telegram:group:chat1:u1"] = MagicMock()
    runner._running_agents["agent:main:telegram:group:chat1:u2"] = MagicMock()

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_shutdown_notification_skipped_when_no_active_agents():
    """No notification is sent when there are no active agents."""
    runner, adapter = make_restart_runner()

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent) == 0


@pytest.mark.asyncio
async def test_shutdown_notification_ignores_pending_sentinels():
    """Pending sentinels (not-yet-started agents) don't trigger notifications."""
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner, adapter = make_restart_runner()
    runner._running_agents["agent:main:telegram:dm:999"] = _AGENT_PENDING_SENTINEL

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent) == 0


@pytest.mark.asyncio
async def test_shutdown_notification_send_failure_does_not_block():
    """If sending a notification fails, the method still completes."""
    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(side_effect=Exception("network error"))
    session_key = "agent:main:telegram:dm:999"
    runner._running_agents[session_key] = MagicMock()

    # Should not raise
    await runner._notify_active_sessions_of_shutdown()


@pytest.mark.asyncio
async def test_shutdown_notification_uses_persisted_origin_for_colon_ids():
    """Shutdown notifications should route from persisted origin, not reparsed keys."""
    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock()
    source = make_restart_source(chat_id="!room123:example.org", chat_type="group")
    source.platform = gateway_run.Platform.MATRIX
    session_key = build_session_key(source)
    runner._running_agents[session_key] = MagicMock()
    runner.session_store._entries = {
        session_key: SessionEntry(
            session_key=session_key,
            session_id="sess-1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            platform=source.platform,
            chat_type=source.chat_type,
        )
    }
    runner.adapters = {gateway_run.Platform.MATRIX: adapter}

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.send.await_count == 1
    assert adapter.send.await_args.args[0] == "!room123:example.org"
