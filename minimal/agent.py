"""Hello-world LiveKit agent with a Synthesia interactive avatar.

Run: python agent.py dev  (`console` mode uses a mock room — no avatar)
"""

import asyncio
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, inference, metrics
from livekit.plugins import openai, silero, synthesia

from realtime_preflight import install_realtime_preflight_support

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
    # Retry transient errors only; auth/quota/unknown-avatar are permanent.
    for attempt in range(3):
        try:
            await avatar.start(session, room=ctx.room)
            break
        except (synthesia.SynthesiaConnectionError, synthesia.SynthesiaTimeoutError):
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))

    # Tools disable preflight speculation.
    await session.start(agent=Agent(instructions=INSTRUCTIONS), room=ctx.room)

    session.generate_reply(
        instructions="Greet the user in one short sentence and invite them to chat.",
        allow_interruptions=False,
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
