"""Exercise the first Quail import without inheriting another test's plugin cache."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("recipe", ["minimal", "rag", "tools"])
def test_quail_setup_on_worker_thread_after_fresh_start(recipe):
    # Windows uses thread workers; an earlier import can hide registration failures.
    path = Path(__file__).parents[1] / recipe / "agent.py"
    script = """
import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from livekit import rtc

path = Path(sys.argv[1])
sys.path.insert(0, str(path.parent))
spec = importlib.util.spec_from_file_location("agent_under_test", path)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

with ThreadPoolExecutor(max_workers=1) as executor:
    options = executor.submit(agent.quail_audio_input).result(timeout=15)

assert isinstance(options.noise_cancellation, rtc.FrameProcessor), (
    "Quail fell back to unprocessed audio in the worker thread"
)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        env={**os.environ, "QUAIL_ENABLED": "true", "KB_SOURCE": "wikipedia"},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
