"""Check optional Quail setup with the real plugin, without processing billed audio."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from livekit import plugins, rtc
from livekit.agents import room_io


@pytest.fixture(params=["minimal", "rag", "tools"])
def agent(request, monkeypatch):
    monkeypatch.delenv("QUAIL_ENABLED", raising=False)
    monkeypatch.setenv("KB_SOURCE", "wikipedia")
    monkeypatch.setattr("dotenv.load_dotenv", lambda **_kwargs: False)
    path = Path(__file__).parents[1] / request.param / "agent.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location(f"{request.param}_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("enabled", [None, "false"])
def test_default_input_needs_no_quail(agent, monkeypatch, caplog, enabled):
    if enabled is not None:
        monkeypatch.setenv("QUAIL_ENABLED", enabled)
    monkeypatch.delattr(plugins, "ai_coustics", raising=False)
    monkeypatch.setitem(sys.modules, "livekit.plugins.ai_coustics", None)

    assert agent.quail_audio_input() == room_io.AudioInputOptions()
    assert not caplog.records


def test_enabled_input_creates_a_real_processor_per_session(agent, monkeypatch):
    # The official plugin defers authentication and native processing until audio arrives.
    monkeypatch.setenv("QUAIL_ENABLED", " TRUE ")
    first = agent.quail_audio_input()
    second = agent.quail_audio_input()

    assert first.sample_rate == 16000
    assert first.frame_size_ms == 15
    assert first.num_channels == 1
    assert isinstance(first.noise_cancellation, rtc.FrameProcessor)
    assert first.noise_cancellation.enabled
    assert first.noise_cancellation is not second.noise_cancellation


@pytest.mark.parametrize("package", [None, SimpleNamespace()])
def test_missing_or_incompatible_plugin_falls_back(agent, monkeypatch, caplog, package):
    # Covers an absent optional install and the other package sharing this import path.
    monkeypatch.setenv("QUAIL_ENABLED", "true")
    monkeypatch.delattr(plugins, "ai_coustics", raising=False)
    monkeypatch.setitem(sys.modules, "livekit.plugins.ai_coustics", package)

    assert agent.quail_audio_input() == room_io.AudioInputOptions()
    assert "install requirements-quail.txt" in caplog.text
    assert "Continuing without Quail" in caplog.text


def test_plugin_setup_failure_falls_back_without_logging_credentials(
    agent, monkeypatch, caplog
):
    from livekit.plugins import ai_coustics

    monkeypatch.setenv("QUAIL_ENABLED", "true")

    def fail(**_kwargs):
        raise RuntimeError("Plugin error containing secret-livekit-key")

    monkeypatch.setattr(ai_coustics, "audio_enhancement", fail)

    assert agent.quail_audio_input() == room_io.AudioInputOptions()
    assert "Continuing without Quail" in caplog.text
    assert "secret-livekit-key" not in caplog.text
