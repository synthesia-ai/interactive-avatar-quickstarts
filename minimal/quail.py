"""Optional microphone cleanup; kept in each recipe so it can be copied on its own."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from livekit.agents import room_io

if TYPE_CHECKING:
    from aic_sdk import Model

logger = logging.getLogger("quail")


def prepare_quail_model() -> None:
    """Download before LiveKit starts its worker processes."""
    if os.getenv("QUAIL_ENABLED", "false").strip().lower() != "true":
        return
    if not os.getenv("AIC_SDK_KEY", "").strip():
        return
    if os.getenv("QUAIL_MODEL_PATH", "").strip():
        return

    try:
        from livekit.plugins import ai_coustics

        # The SDK reuses an existing, valid model file.
        path = ai_coustics.Model.download(
            "quail-vf-2.2-l-16khz",
            Path(__file__).resolve().parent / "models",
        )
        # Worker processes inherit this path and load the model locally.
        os.environ["QUAIL_MODEL_PATH"] = path
    except Exception as exc:  # noqa: BLE001 — optional setup must not stop the agent
        logger.warning(
            "Quail model preparation failed (%s); install requirements-quail.txt "
            "and check network/cache access. Continuing without Quail.",
            type(exc).__name__,
        )


def load_quail_model() -> Model | None:
    """Load weights during worker prewarm, only when explicitly enabled."""
    if os.getenv("QUAIL_ENABLED", "false").strip().lower() != "true":
        return None

    model_path = os.getenv("QUAIL_MODEL_PATH", "").strip()
    if not os.getenv("AIC_SDK_KEY", "").strip() or not model_path:
        logger.warning(
            "Quail requested but AIC_SDK_KEY or QUAIL_MODEL_PATH is missing; "
            "continuing without Quail."
        )
        return None

    try:
        # Optional dependency: the default quickstart never imports the SDK.
        from livekit.plugins import ai_coustics
    except (ImportError, OSError):
        logger.warning(
            "Quail requested but its plugin is unavailable; "
            "install requirements-quail.txt. Continuing without Quail."
        )
        return None

    try:
        # Relative paths are anchored to the recipe, just like its .env file.
        path = Path(__file__).parent / Path(model_path).expanduser()
        return ai_coustics.Model.from_file(str(path))
    except Exception as exc:  # noqa: BLE001 — optional SDK failures must not stop the agent
        logger.warning(
            "Quail model could not be loaded (%s); check QUAIL_MODEL_PATH. "
            "Continuing without Quail.",
            type(exc).__name__,
        )
        return None


def quail_audio_input(model: Model | None) -> room_io.AudioInputOptions:
    """Give each session its own processor; RoomIO owns its cleanup."""
    if model is None:
        return room_io.AudioInputOptions()

    try:
        from livekit.plugins import ai_coustics

        processor = ai_coustics.Processor(
            model=model,
            license_key=os.environ["AIC_SDK_KEY"].strip(),
        )
    except Exception as exc:  # noqa: BLE001 — optional SDK failures must not stop the agent
        # Quail is optional; an SDK/license failure must not prevent conversation.
        logger.warning(
            "Quail initialization failed (%s); check AIC_SDK_KEY and the model. "
            "Continuing without Quail.",
            type(exc).__name__,
        )
        return room_io.AudioInputOptions()

    logger.info("Quail Voice Focus configured for microphone input (16 kHz).")
    # VF 2.2 L processes 240 samples at 16 kHz; matching its 15 ms blocks avoids buffering.
    return room_io.AudioInputOptions(
        sample_rate=16000, frame_size_ms=15, noise_cancellation=processor,
    )
