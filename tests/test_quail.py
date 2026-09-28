"""Check each copyable recipe without contacting the licensed ai-coustics service."""

import importlib.util
import logging
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from livekit import plugins, rtc
from livekit.agents import cli, room_io


@pytest.fixture(params=["minimal", "rag", "tools"])
def quail(request, monkeypatch):
    for name in ("QUAIL_ENABLED", "AIC_SDK_KEY", "QUAIL_MODEL_PATH"):
        monkeypatch.delenv(name, raising=False)
    path = Path(__file__).parents[1] / request.param / "quail.py"
    spec = importlib.util.spec_from_file_location(f"{request.param}_quail", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sdk(monkeypatch):
    # Only the licensed SDK boundary is replaced; use real LiveKit input options.
    class Model:
        @staticmethod
        def from_file(path):
            return SimpleNamespace(path=path)

    class Processor(rtc.FrameProcessor):
        enabled = True

        def __init__(self, *, model, license_key):
            self.model = model
            self.license_key = license_key

        def _process(self, frame):
            return frame

        def _close(self):
            pass

    sdk = SimpleNamespace(Model=Model, Processor=Processor)
    monkeypatch.setattr(plugins, "ai_coustics", sdk, raising=False)
    monkeypatch.setitem(sys.modules, "livekit.plugins.ai_coustics", sdk)
    return sdk


@pytest.mark.parametrize("enabled", [None, "false"])
def test_default_input_needs_no_quail(quail, monkeypatch, caplog, enabled):
    # Configuring credentials alone must never opt a user in.
    monkeypatch.setenv("AIC_SDK_KEY", "unused-key")
    monkeypatch.setenv("QUAIL_MODEL_PATH", "unused-model")
    if enabled is not None:
        monkeypatch.setenv("QUAIL_ENABLED", enabled)
    monkeypatch.delattr(plugins, "ai_coustics", raising=False)
    monkeypatch.setitem(sys.modules, "livekit.plugins.ai_coustics", None)

    quail.prepare_quail_model()
    model = quail.load_quail_model()

    assert model is None
    assert quail.quail_audio_input(model) == room_io.AudioInputOptions()
    assert not caplog.records


@pytest.mark.parametrize(
    "license_key, model_path", [("", ""), ("unused-key", "models/pinned.aicmodel")]
)
def test_preparation_skips_download_without_key_or_with_explicit_path(
    quail, monkeypatch, caplog, license_key, model_path
):
    monkeypatch.setenv("QUAIL_ENABLED", "true")
    monkeypatch.setenv("AIC_SDK_KEY", license_key)
    monkeypatch.setenv("QUAIL_MODEL_PATH", model_path)
    monkeypatch.delattr(plugins, "ai_coustics", raising=False)
    monkeypatch.setitem(sys.modules, "livekit.plugins.ai_coustics", None)

    quail.prepare_quail_model()

    assert os.environ["QUAIL_MODEL_PATH"] == model_path
    assert not caplog.records


@pytest.mark.parametrize("arguments", [["dev"], ["start"], ["--help"], ["dev", "--help"]])
def test_cli_prepares_model_before_starting_workers(
    quail, sdk, monkeypatch, tmp_path, arguments
):
    monkeypatch.setenv("QUAIL_ENABLED", "true")
    monkeypatch.setenv("AIC_SDK_KEY", "test-sdk-key")
    monkeypatch.setenv("KB_SOURCE", "wikipedia")
    recipe_dir = Path(quail.__file__).parent
    model_path = str(tmp_path / "quail-vf-2.2-l-build.aicmodel")
    events = []

    def download(model_id, download_dir):
        assert model_id == "quail-vf-2.2-l-16khz"
        assert download_dir == recipe_dir / "models"
        events.append("download")
        return model_path

    def start_workers(_options):
        events.append("cli")
        if "--help" not in arguments:
            assert quail.load_quail_model().path == model_path
            # Child workers receive the resolved path through their environment.
            assert os.environ["QUAIL_MODEL_PATH"] == model_path
        else:
            assert "QUAIL_MODEL_PATH" not in os.environ

    monkeypatch.setattr(sdk.Model, "download", download, raising=False)
    monkeypatch.setattr(cli, "run_app", start_workers)
    monkeypatch.setattr("dotenv.load_dotenv", lambda **_kwargs: False)
    monkeypatch.syspath_prepend(str(recipe_dir))
    monkeypatch.setitem(sys.modules, "quail", quail)
    monkeypatch.setattr(sys, "argv", [str(recipe_dir / "agent.py"), *arguments])

    runpy.run_path(str(recipe_dir / "agent.py"), run_name="__main__")

    assert events == (["cli"] if "--help" in arguments else ["download", "cli"])


@pytest.mark.parametrize("failure", ["download", "plugin"])
def test_preparation_failure_falls_back_without_logging_credentials(
    quail, sdk, monkeypatch, caplog, failure
):
    monkeypatch.setenv("QUAIL_ENABLED", "true")
    monkeypatch.setenv("AIC_SDK_KEY", "secret-sdk-key")

    def fail(*_args, **_kwargs):
        raise RuntimeError("Download error containing secret-sdk-key")

    if failure == "download":
        monkeypatch.setattr(sdk.Model, "download", fail, raising=False)
    else:
        monkeypatch.delattr(plugins, "ai_coustics", raising=False)
        monkeypatch.setitem(sys.modules, "livekit.plugins.ai_coustics", None)

    quail.prepare_quail_model()

    assert "QUAIL_MODEL_PATH" not in os.environ
    assert quail.quail_audio_input(quail.load_quail_model()) == room_io.AudioInputOptions()
    assert "Quail model preparation failed" in caplog.text
    assert "secret-sdk-key" not in caplog.text


def test_requested_without_config_falls_back(quail, monkeypatch, caplog):
    monkeypatch.setenv("QUAIL_ENABLED", "true")

    assert quail.load_quail_model() is None
    assert "AIC_SDK_KEY or QUAIL_MODEL_PATH is missing" in caplog.text


def test_requested_without_plugin_falls_back(quail, monkeypatch, caplog):
    monkeypatch.setenv("QUAIL_ENABLED", "true")
    monkeypatch.setenv("AIC_SDK_KEY", "unused-key")
    monkeypatch.setenv("QUAIL_MODEL_PATH", "unused-model")
    monkeypatch.delattr(plugins, "ai_coustics", raising=False)
    monkeypatch.setitem(sys.modules, "livekit.plugins.ai_coustics", None)

    assert quail.load_quail_model() is None
    assert "install requirements-quail.txt" in caplog.text


def test_enabled_input_uses_a_processor_per_session(quail, sdk, monkeypatch, tmp_path):
    monkeypatch.setenv("QUAIL_ENABLED", "true")
    monkeypatch.setenv("AIC_SDK_KEY", "test-sdk-key")
    monkeypatch.setenv("QUAIL_MODEL_PATH", "models/quail.aicmodel")
    monkeypatch.chdir(tmp_path)  # Running from elsewhere must still find the recipe's model.

    model = quail.load_quail_model()
    first = quail.quail_audio_input(model)
    second = quail.quail_audio_input(model)

    assert model.path == str(Path(quail.__file__).parent / "models/quail.aicmodel")
    assert first.sample_rate == 16000
    assert first.frame_size_ms == 15
    assert first.num_channels == 1
    assert isinstance(first.noise_cancellation, rtc.FrameProcessor)
    assert first.noise_cancellation is not second.noise_cancellation
    assert first.noise_cancellation.model is second.noise_cancellation.model is model
    assert first.noise_cancellation.license_key == "test-sdk-key"


@pytest.mark.parametrize("stage", ["model", "processor"])
def test_sdk_failure_falls_back_without_logging_credentials(
    quail, sdk, monkeypatch, caplog, stage
):
    monkeypatch.setenv("QUAIL_ENABLED", "true")
    monkeypatch.setenv("AIC_SDK_KEY", "secret-sdk-key")
    monkeypatch.setenv("QUAIL_MODEL_PATH", "models/quail.aicmodel")

    def fail(*args, **kwargs):
        raise RuntimeError("SDK error containing secret-sdk-key")

    if stage == "model":
        monkeypatch.setattr(sdk.Model, "from_file", fail)
    else:
        monkeypatch.setattr(sdk, "Processor", fail)

    with caplog.at_level(logging.WARNING):
        options = quail.quail_audio_input(quail.load_quail_model())

    assert options == room_io.AudioInputOptions()
    assert "Continuing without Quail" in caplog.text
    assert "secret-sdk-key" not in caplog.text
