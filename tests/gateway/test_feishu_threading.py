import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _install_fake_lark_modules() -> None:
    if "lark_oapi" in sys.modules:
        return

    fake_lark = types.ModuleType("lark_oapi")
    fake_api = types.ModuleType("lark_oapi.api")
    fake_im = types.ModuleType("lark_oapi.api.im")
    fake_v1 = types.ModuleType("lark_oapi.api.im.v1")
    fake_ws = types.ModuleType("lark_oapi.ws")

    class _DummyBuilder:
        def __init__(self):
            self.kwargs = {}

        def content(self, value):
            self.kwargs["content"] = value
            return self

        def msg_type(self, value):
            self.kwargs["msg_type"] = value
            return self

        def reply_in_thread(self, value):
            self.kwargs["reply_in_thread"] = value
            return self

        def uuid(self, value):
            self.kwargs["uuid"] = value
            return self

        def message_id(self, value):
            self.kwargs["message_id"] = value
            return self

        def request_body(self, value):
            self.kwargs["request_body"] = value
            return self

        def receive_id(self, value):
            self.kwargs["receive_id"] = value
            return self

        def build(self):
            return SimpleNamespace(**self.kwargs)

    class _ReplyMessageRequestBody:
        @staticmethod
        def builder():
            return _DummyBuilder()

    class _ReplyMessageRequest:
        @staticmethod
        def builder():
            return _DummyBuilder()

    fake_v1.ReplyMessageRequestBody = _ReplyMessageRequestBody
    fake_v1.ReplyMessageRequest = _ReplyMessageRequest
    fake_v1.CreateMessageRequestBody = object
    fake_v1.CreateMessageRequest = object
    fake_ws.Client = object

    sys.modules["lark_oapi"] = fake_lark
    sys.modules["lark_oapi.api"] = fake_api
    sys.modules["lark_oapi.api.im"] = fake_im
    sys.modules["lark_oapi.api.im.v1"] = fake_v1
    sys.modules["lark_oapi.ws"] = fake_ws


_install_fake_lark_modules()

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.feishu.adapter import FeishuAdapter


@pytest.mark.asyncio
async def test_feishu_thread_message_uses_current_message_as_reply_anchor(monkeypatch):
    adapter = FeishuAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter.get_chat_info = AsyncMock(return_value={"name": "Test Chat"})
    adapter._resolve_sender_profile = AsyncMock(
        return_value={"user_id": "u1", "user_name": "七哥", "user_id_alt": None}
    )
    adapter._extract_message_content = AsyncMock(
        return_value=("hello", MessageType.TEXT, [], [], [])
    )
    adapter._fetch_message_text = AsyncMock(return_value="quoted")

    captured = {}

    async def _capture(event):
        captured["event"] = event

    adapter._dispatch_inbound_event = _capture

    message = SimpleNamespace(
        chat_id="oc_xxx",
        thread_id="omt-thread-1",
        parent_id="om_parent",
        upper_message_id="om_upper",
        message_type="text",
        content='{"text":"hello"}',
    )

    await adapter._process_inbound_message(
        data=SimpleNamespace(),
        message=message,
        sender_id=SimpleNamespace(open_id="ou_xxx"),
        chat_type="group",
        message_id="om_current",
    )

    event = captured["event"]
    assert event.reply_to_message_id == "om_current"
    assert event.source.thread_id == "omt-thread-1"
    # Quoted text is fetched from the original parent_id, not the anchored reply_to
    adapter._fetch_message_text.assert_awaited_once_with("om_parent")


@pytest.mark.asyncio
async def test_feishu_thread_message_prefers_root_message_id_for_thread_identity():
    adapter = FeishuAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter.get_chat_info = AsyncMock(return_value={"name": "Test Chat"})
    adapter._resolve_sender_profile = AsyncMock(
        return_value={"user_id": "u1", "user_name": "七哥", "user_id_alt": None}
    )
    adapter._extract_message_content = AsyncMock(
        return_value=("hello", MessageType.TEXT, [], [], [])
    )
    adapter._fetch_message_text = AsyncMock(return_value="quoted")

    captured = {}

    async def _capture(event):
        captured["event"] = event

    adapter._dispatch_inbound_event = _capture

    message = SimpleNamespace(
        chat_id="oc_xxx",
        thread_id="omt-thread-1",
        root_id="om_thread_root",
        parent_id="om_parent",
        upper_message_id="om_upper",
        message_type="text",
        content='{"text":"hello"}',
    )

    await adapter._process_inbound_message(
        data=SimpleNamespace(),
        message=message,
        sender_id=SimpleNamespace(open_id="ou_xxx"),
        chat_type="group",
        message_id="om_current",
    )

    event = captured["event"]
    assert event.reply_to_message_id == "om_current"
    assert event.source.thread_id == "om_thread_root"
    adapter._fetch_message_text.assert_awaited_once_with("om_parent")


@pytest.mark.asyncio
async def test_feishu_send_raw_message_replies_in_thread_when_thread_metadata_present():
    adapter = FeishuAdapter(PlatformConfig(enabled=True, token="fake"))
    reply_calls = []

    class _FakeReplyAPI:
        def reply(self, request):
            reply_calls.append(request)
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="msg1"))

    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=_FakeReplyAPI()))
    )

    await adapter._send_raw_message(
        chat_id="oc_xxx",
        msg_type="text",
        payload='{"text":"ok"}',
        reply_to="om_current",
        metadata={"thread_id": "omt-thread-1"},
    )

    assert len(reply_calls) == 1
    request = reply_calls[0]
    assert request.message_id == "om_current"
    assert request.request_body.reply_in_thread is True


@pytest.mark.asyncio
async def test_feishu_quote_reply_without_thread_creates_thread():
    """When user quotes a message without an existing thread, thread_id should
    be set to the quoted message id so the reply opens a new Feishu topic."""
    adapter = FeishuAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter.get_chat_info = AsyncMock(return_value={"name": "Test Chat"})
    adapter._resolve_sender_profile = AsyncMock(
        return_value={"user_id": "u1", "user_name": "七哥", "user_id_alt": None}
    )
    adapter._extract_message_content = AsyncMock(
        return_value=("hello", MessageType.TEXT, [], [], [])
    )
    adapter._fetch_message_text = AsyncMock(return_value="quoted text")

    captured = {}

    async def _capture(event):
        captured["event"] = event

    adapter._dispatch_inbound_event = _capture

    # Message with parent_id but NO thread_id — a plain quote reply
    message = SimpleNamespace(
        chat_id="oc_xxx",
        thread_id=None,
        message_thread_id=None,
        root_id=None,
        parent_id="om_quoted",
        upper_message_id=None,
        message_type="text",
        content='{"text":"hello"}',
    )

    await adapter._process_inbound_message(
        data=SimpleNamespace(),
        message=message,
        sender_id=SimpleNamespace(open_id="ou_xxx"),
        chat_type="p2p",
        message_id="om_current",
    )

    event = captured["event"]
    # thread_id should be set to the quoted message (auto-thread)
    assert event.source.thread_id == "om_quoted"
    # reply_to anchored to quoted message (thread root) — the bot's reply
    # will use reply_in_thread=True which opens a topic under this message.
    assert event.reply_to_message_id == "om_quoted"
    # Quoted text fetched from the original parent
    adapter._fetch_message_text.assert_awaited_once_with("om_quoted")


@pytest.mark.asyncio
async def test_feishu_send_raw_anchors_to_thread_without_reply_to():
    """When sending with thread metadata but no explicit reply_to, the message
    should still land in the thread by replying to the thread root."""
    adapter = FeishuAdapter(PlatformConfig(enabled=True, token="fake"))
    reply_calls = []

    class _FakeReplyAPI:
        def reply(self, request):
            reply_calls.append(request)
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="msg1"))

        def create(self, request):
            # Should NOT be called
            raise AssertionError("create() should not be called when thread context exists")

    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=_FakeReplyAPI()))
    )

    await adapter._send_raw_message(
        chat_id="oc_xxx",
        msg_type="text",
        payload='{"text":"progress update"}',
        reply_to=None,  # No explicit reply_to
        metadata={"thread_id": "om_thread_root"},
    )

    # Should have used reply API with the thread root, not create
    assert len(reply_calls) == 1
    request = reply_calls[0]
    assert request.message_id == "om_thread_root"
    assert request.request_body.reply_in_thread is True


@pytest.mark.asyncio
async def test_feishu_send_raw_message_falls_back_to_create_for_topic_id_without_reply_to():
    """Topic ids (omt_...) are not valid open_message_id reply anchors."""
    adapter = FeishuAdapter(PlatformConfig(enabled=True, token="fake"))
    reply_calls = []
    create_calls = []

    class _FakeReplyAPI:
        def reply(self, request):
            reply_calls.append(request)
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="msg_reply"))

        def create(self, request):
            create_calls.append(request)
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="msg_create"))

    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=_FakeReplyAPI()))
    )

    result = await adapter._send_raw_message(
        chat_id="oc_xxx",
        msg_type="text",
        payload='{"text":"background update"}',
        reply_to=None,
        metadata={"thread_id": "omt-thread-1"},
    )

    assert result.data.message_id == "msg_create"
    assert reply_calls == []
    assert len(create_calls) == 1
