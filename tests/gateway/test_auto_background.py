"""Tests for the auto-background task routing feature.

When ``agent.auto_background`` is enabled in config, messages that match
long-running task patterns (research, bulk analysis, etc.) are automatically
routed to a background session instead of blocking the main conversation.
"""

import os
import re
import pytest
from unittest.mock import patch, MagicMock

# Minimal stub so we can instantiate GatewayRunner._should_auto_background
# without a full gateway setup.


class _FakeRunner:
    """Lightweight stand-in that borrows the classifier methods."""

    from gateway.run import GatewayRunner

    _AUTO_BG_DEFAULT_PATTERNS = GatewayRunner._AUTO_BG_DEFAULT_PATTERNS
    _load_auto_background_config = staticmethod(GatewayRunner._load_auto_background_config)
    _should_auto_background = GatewayRunner._should_auto_background


@pytest.fixture
def runner():
    return _FakeRunner()


# ── Config loading ────────────────────────────────────────────────


def test_auto_bg_disabled_by_default(runner):
    """With no config, auto-background is off."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_AUTO_BACKGROUND", None)
        # Mock config to return disabled (default when no config key exists)
        with patch.object(type(runner), '_load_auto_background_config',
                          staticmethod(lambda: {"enabled": False, "extra_patterns": [], "min_length": 10})):
            assert runner._should_auto_background("深度调研 AI 模型") is False


def test_auto_bg_env_override_on(runner):
    """HERMES_AUTO_BACKGROUND=1 enables the feature."""
    with patch.dict(os.environ, {"HERMES_AUTO_BACKGROUND": "1"}):
        assert runner._should_auto_background("深度调研 AI 模型") is True


def test_auto_bg_env_override_off(runner):
    """HERMES_AUTO_BACKGROUND=0 disables even if config says true."""
    with patch.dict(os.environ, {"HERMES_AUTO_BACKGROUND": "0"}):
        assert runner._should_auto_background("深度调研 AI 模型") is False


# ── Pattern matching ──────────────────────────────────────────────


@pytest.fixture
def enabled_runner(runner):
    """Runner with auto-background force-enabled via env."""
    os.environ["HERMES_AUTO_BACKGROUND"] = "1"
    yield runner
    os.environ.pop("HERMES_AUTO_BACKGROUND", None)


class TestChinesePatterns:
    def test_research(self, enabled_runner):
        assert enabled_runner._should_auto_background("帮我调研一下目前主流的 AI 模型") is True

    def test_deep_analysis(self, enabled_runner):
        assert enabled_runner._should_auto_background("深度分析这个项目的架构") is True

    def test_batch_scan(self, enabled_runner):
        assert enabled_runner._should_auto_background("批量扫描所有渠道的价格") is True

    def test_collect_and_write(self, enabled_runner):
        assert enabled_runner._should_auto_background("收录这个项目到 wiki 知识库") is True

    def test_write_report(self, enabled_runner):
        assert enabled_runner._should_auto_background("写一篇关于 GRPO 的技术报告") is True

    def test_read_source_code(self, enabled_runner):
        assert enabled_runner._should_auto_background("学习这个 repo 的源码") is True

    def test_comprehensive_review(self, enabled_runner):
        assert enabled_runner._should_auto_background("全面评测这几个模型的效果") is True


class TestEnglishPatterns:
    def test_deep_research(self, enabled_runner):
        assert enabled_runner._should_auto_background("Do a deep research on GRPO training methods") is True

    def test_comprehensive_analysis(self, enabled_runner):
        assert enabled_runner._should_auto_background("comprehensive analysis of the codebase") is True

    def test_batch_collect(self, enabled_runner):
        assert enabled_runner._should_auto_background("batch collect all pricing data from providers") is True

    def test_write_report(self, enabled_runner):
        assert enabled_runner._should_auto_background("write a report on the current AI landscape") is True

    def test_analyze_source_code(self, enabled_runner):
        assert enabled_runner._should_auto_background("analyze the source code of this project") is True


class TestNonMatching:
    """Messages that should NOT trigger auto-background."""

    def test_simple_question(self, enabled_runner):
        assert enabled_runner._should_auto_background("今天天气怎么样") is False

    def test_short_message(self, enabled_runner):
        assert enabled_runner._should_auto_background("好的") is False

    def test_greeting(self, enabled_runner):
        assert enabled_runner._should_auto_background("你好") is False

    def test_simple_command(self, enabled_runner):
        assert enabled_runner._should_auto_background("帮我改一下这个文件的第三行") is False

    def test_empty(self, enabled_runner):
        assert enabled_runner._should_auto_background("") is False

    def test_none(self, enabled_runner):
        assert enabled_runner._should_auto_background(None) is False


class TestMinLength:
    def test_below_min_length(self, enabled_runner):
        """Even if pattern matches, messages below min_length are skipped."""
        # "调研" alone is only 2 chars — below default min_length of 10
        assert enabled_runner._should_auto_background("调研") is False

    def test_at_min_length(self, enabled_runner):
        # "调研一下这个项目" is 8 chars — still below 10
        assert enabled_runner._should_auto_background("调研一下这个项") is False

    def test_above_min_length(self, enabled_runner):
        assert enabled_runner._should_auto_background("调研一下这个项目的情况") is True
