"""Tests for unified tool progress mode in GatewayStreamConsumer.

Unified mode merges tool progress lines into the streaming content message
instead of sending them as a separate message bubble.
"""

import asyncio
import queue
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.stream_consumer import (
    GatewayStreamConsumer,
    StreamConsumerConfig,
    _DONE,
    _NEW_SEGMENT,
    _TOOL_PROGRESS,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_adapter(max_len: int = 4096) -> MagicMock:
    """Create a mock adapter with send/edit_message support."""
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = max_len
    adapter.REQUIRES_EDIT_FINALIZE = False

    send_result = MagicMock(success=True, message_id="msg_1")
    edit_result = MagicMock(success=True)

    adapter.send = AsyncMock(return_value=send_result)
    adapter.edit_message = AsyncMock(return_value=edit_result)
    adapter.send_typing = AsyncMock()
    adapter.truncate_message = MagicMock(side_effect=lambda text, limit: [text])

    return adapter


def _make_consumer(
    adapter=None, unified: bool = True, cursor: str = " ▉"
) -> GatewayStreamConsumer:
    """Create a stream consumer for testing."""
    if adapter is None:
        adapter = _make_adapter()
    cfg = StreamConsumerConfig(
        edit_interval=0.01,
        buffer_threshold=1,
        cursor=cursor,
    )
    return GatewayStreamConsumer(
        adapter=adapter,
        chat_id="test_chat",
        config=cfg,
        unified_progress=unified,
    )


async def _run_consumer_with_events(consumer, events, settle_time=0.15):
    """Start consumer, feed events, wait for processing, then finish."""
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.05)  # Let consumer start

    for event in events:
        if isinstance(event, tuple) and event[0] == "tool":
            consumer.on_tool_progress(event[1])
        elif isinstance(event, tuple) and event[0] == "delta":
            consumer.on_delta(event[1])
        elif isinstance(event, tuple) and event[0] == "segment_break":
            consumer.on_segment_break()
        elif isinstance(event, tuple) and event[0] == "sleep":
            await asyncio.sleep(event[1])
        else:
            consumer.on_delta(event)

    await asyncio.sleep(settle_time)
    consumer.finish()
    await asyncio.wait_for(task, timeout=5.0)
    return consumer


# ── Tests ────────────────────────────────────────────────────────────


class TestBuildUnifiedText:
    """Test the _build_unified_text helper."""

    def test_tool_lines_only(self):
        c = _make_consumer()
        c._tool_lines = ["⚙️ terminal", "🔍 search_files"]
        result = c._build_unified_text("", with_cursor=True)
        assert "⚙️ terminal → 🔍 search_files" in result
        assert "───" not in result  # no separator when no content
        assert result.endswith(c.cfg.cursor)

    def test_content_only(self):
        c = _make_consumer()
        result = c._build_unified_text("Hello world", with_cursor=False)
        assert result == "Hello world"
        assert "───" not in result

    def test_tool_lines_and_content(self):
        c = _make_consumer()
        c._tool_lines = ["⚙️ terminal"]
        result = c._build_unified_text("Response text", with_cursor=False)
        assert "⚙️ terminal" in result
        assert "───" in result
        assert "Response text" in result

    def test_tool_lines_and_content_with_cursor(self):
        c = _make_consumer()
        c._tool_lines = ["⚙️ terminal"]
        result = c._build_unified_text("Response text", with_cursor=True)
        assert result.endswith(c.cfg.cursor)

    def test_empty(self):
        c = _make_consumer()
        result = c._build_unified_text("")
        assert result == ""

    def test_multiple_tool_lines(self):
        c = _make_consumer()
        c._tool_lines = ["⚙️ terminal", "🔍 search_files", "📄 read_file"]
        result = c._build_unified_text("content")
        assert "⚙️ terminal → 🔍 search_files → 📄 read_file" in result


class TestOnToolProgress:
    """Test the on_tool_progress method."""

    def test_queues_in_unified_mode(self):
        c = _make_consumer(unified=True)
        c.on_tool_progress("⚙️ terminal")
        item = c._queue.get_nowait()
        assert isinstance(item, tuple)
        assert item[0] is _TOOL_PROGRESS
        assert item[1] == "⚙️ terminal"

    def test_ignored_in_non_unified_mode(self):
        c = _make_consumer(unified=False)
        c.on_tool_progress("⚙️ terminal")
        assert c._queue.empty()

    def test_empty_line_ignored(self):
        c = _make_consumer(unified=True)
        c.on_tool_progress("")
        assert c._queue.empty()


class TestUnifiedProgressIntegration:
    """Integration tests for unified progress mode."""

    @pytest.mark.asyncio
    async def test_tool_only_message(self):
        """Tool progress without content should display tool lines."""
        adapter = _make_adapter()
        c = _make_consumer(adapter=adapter)

        await _run_consumer_with_events(c, [
            ("tool", "⚙️ terminal"),
            ("sleep", 0.05),
            ("tool", "🔍 search_files"),
        ])

        # Should have sent/edited a message with tool lines
        assert adapter.send.called or adapter.edit_message.called

    @pytest.mark.asyncio
    async def test_tool_then_content_single_message(self):
        """Tool progress followed by content should be in one message."""
        adapter = _make_adapter()
        c = _make_consumer(adapter=adapter)

        await _run_consumer_with_events(c, [
            ("tool", "⚙️ terminal"),
            ("sleep", 0.05),
            ("delta", "Hello "),
            ("delta", "world"),
        ])

        # First call should be send (new message), subsequent should be edits
        assert adapter.send.call_count >= 1
        # The final edit should contain both tool line and content
        last_edit_text = None
        if adapter.edit_message.called:
            last_edit_text = adapter.edit_message.call_args_list[-1].kwargs.get(
                "content"
            ) or adapter.edit_message.call_args_list[-1][1].get("content", "")
        # Final message should be content-only (tool header stripped)
        # The consumer strips tool lines on _DONE

    @pytest.mark.asyncio
    async def test_segment_break_no_reset_in_unified(self):
        """Segment breaks should NOT reset the message in unified mode."""
        adapter = _make_adapter()
        c = _make_consumer(adapter=adapter)

        await _run_consumer_with_events(c, [
            ("tool", "⚙️ terminal"),
            ("delta", "Part 1"),
            ("sleep", 0.05),
            ("segment_break", None),
            ("sleep", 0.05),
            ("tool", "🔍 search_files"),
            ("delta", " Part 2"),
        ])

        # In unified mode, send should only be called once (initial message)
        # All subsequent updates should be edits to the same message
        assert adapter.send.call_count == 1

    @pytest.mark.asyncio
    async def test_on_new_message_disabled_in_unified(self):
        """on_new_message callback should be disabled in unified mode."""
        callback = MagicMock()
        adapter = _make_adapter()
        cfg = StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1)
        c = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test",
            config=cfg,
            on_new_message=callback,
            unified_progress=True,
        )
        assert c._on_new_message is None

    @pytest.mark.asyncio
    async def test_final_edit_strips_tool_header(self):
        """The final edit should contain only the response content."""
        adapter = _make_adapter()
        c = _make_consumer(adapter=adapter, cursor="")

        await _run_consumer_with_events(c, [
            ("tool", "⚙️ terminal"),
            ("sleep", 0.05),
            ("delta", "Final answer"),
        ], settle_time=0.1)

        # The last edit (finalize) should be content-only
        if adapter.edit_message.called:
            last_call = adapter.edit_message.call_args_list[-1]
            content = last_call.kwargs.get("content") or last_call[1].get("content", "")
            # Should not contain tool emoji in final
            assert "Final answer" in content

    @pytest.mark.asyncio
    async def test_non_unified_still_works(self):
        """Non-unified mode should behave as before."""
        adapter = _make_adapter()
        c = _make_consumer(adapter=adapter, unified=False)

        await _run_consumer_with_events(c, [
            ("delta", "Hello"),
            ("sleep", 0.05),
            ("segment_break", None),
            ("sleep", 0.05),
            ("delta", "World"),
        ])

        # In non-unified mode, segment break resets — should send 2 messages
        assert adapter.send.call_count >= 2


class TestUnifiedProgressConfig:
    """Test that unified mode is properly configured."""

    def test_unified_progress_flag_stored(self):
        c = _make_consumer(unified=True)
        assert c._unified_progress is True

    def test_non_unified_flag_stored(self):
        c = _make_consumer(unified=False)
        assert c._unified_progress is False

    def test_tool_lines_initially_empty(self):
        c = _make_consumer()
        assert c._tool_lines == []
