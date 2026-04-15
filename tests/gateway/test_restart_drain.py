import asyncio
import shutil
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.platforms.base import MessageEvent, MessageType
from gateway.restart import DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
from gateway.session import build_session_key
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.mark.asyncio
async def test_restart_command_while_busy_requests_drain_without_interrupt(monkeypatch):
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

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "interrupt")
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "interrupt"

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "parallel")
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "parallel"


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
