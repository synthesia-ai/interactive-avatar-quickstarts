"""Hello-world LiveKit agent with a Synthesia interactive avatar.

Run: python agent.py dev  (`console` mode uses a mock room — no avatar)
"""

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    inference,
    metrics,
    room_io,
)
from livekit.plugins import openai, silero, synthesia

from realtime_preflight import install_realtime_preflight_support

# Module-level on purpose: LiveKit plugins must register on the main thread.
try:
    from livekit.plugins import ai_coustics
except (ImportError, OSError):
    ai_coustics = None

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

AVATAR_ID = "8788bef1-8020-46e0-a8f4-510ea9989b25"  # Jenny
CARTESIA_VOICE_ID = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"  # Skylar (play.cartesia.ai/voices)

INSTRUCTIONS = """\
You are Jenny, a friendly assistant rendered as a real-time Synthesia avatar.
This is a spoken conversation: the user is talking to you and your replies
are spoken aloud, so never refer to typing, reading, or chat. Always speak
English unless the user explicitly asks for another language. Answer in one
to three short sentences, and never use bullet points or markdown or read
out URLs. If asked what you are, explain that you are a LiveKit voice agent
with a Synthesia interactive avatar, and that your creator can make you say
anything by editing one prompt in agent.py."""


def quail_audio_input() -> room_io.AudioInputOptions:
    """Enable optional Voice Focus using the room's LiveKit Cloud credentials."""
    if os.getenv("QUAIL_ENABLED", "false").strip().lower() != "true":
        return room_io.AudioInputOptions()

    if ai_coustics is None:
        logging.getLogger("quail").warning(
            "Quail unavailable; install requirements-quail.txt. Continuing without Quail."
        )
        return room_io.AudioInputOptions()

    try:
        processor = ai_coustics.audio_enhancement(
            model=ai_coustics.EnhancerModel.QUAIL_VF_L,
        )
    except Exception as exc:  # noqa: BLE001 — optional cleanup must not stop the agent
        logging.getLogger("quail").warning(
            "Quail unavailable (%s); install requirements-quail.txt. "
            "Continuing without Quail.",
            type(exc).__name__,
        )
        return room_io.AudioInputOptions()

    logging.getLogger("quail").info("Quail Voice Focus configured via LiveKit Cloud.")
    # Match Voice Focus's native 16 kHz / 240-sample processing blocks.
    return room_io.AudioInputOptions(
        sample_rate=16000, frame_size_ms=15, noise_cancellation=processor,
    )


def prewarm(proc) -> None:
    # Starts the Realtime reply on the eager end-of-turn transcript (~0.3s sooner);
    # LiveKit only does this for cascaded LLMs. Delete once livekit/agents#6537 lands.
    install_realtime_preflight_support()
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    session = AgentSession(
        stt=inference.STT(model="cartesia/ink-2"),
        # Text-only: audio out belongs to Cartesia; also required for preflight.
        llm=openai.realtime.RealtimeModel(
            model="gpt-realtime-1.5",
            modalities=["text"],
            turn_detection=None,
        ),
        tts=inference.TTS(model="cartesia/sonic-3.6", voice=CARTESIA_VOICE_ID),
        vad=ctx.proc.userdata["vad"],
        turn_handling={
            "turn_detection": "stt",
            "preemptive_generation": {"enabled": True},
            "interruption": {"min_duration": 0.75, "min_words": 1, "false_interruption_timeout": 2.0},
        },
    )

    @session.on("metrics_collected")
    def on_metrics(ev) -> None:
        metrics.log_metrics(ev.metrics)

    # Must attach before session.start().
    avatar = synthesia.AvatarSession(
        synthesia.AvatarConfig(avatar_ids=[AVATAR_ID]),
        join_timeout=60.0,
    )
    # Retry only errors the plugin marks retryable (timeout, connection, rate limit).
    for attempt in range(3):
        try:
            await avatar.start(session, room=ctx.room)
            break
        except synthesia.SynthesiaError as e:
            if not e.retryable or attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))

    # Tools disable preflight speculation.
    await session.start(
        agent=Agent(instructions=INSTRUCTIONS),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=quail_audio_input(),
        ),
    )

    session.generate_reply(
        instructions="Greet the user in one short sentence and invite them to chat.",
        allow_interruptions=False,
    )


if __name__ == "__main__":
    # agent_name makes dispatch explicit: the worker only joins rooms whose token
    # requests it (server.py's RoomAgentDispatch) instead of every room in the project.
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="avatar-quickstart-minimal",
        )
    )
