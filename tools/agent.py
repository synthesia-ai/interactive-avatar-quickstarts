"""Tool-calling LiveKit agent with a Synthesia interactive avatar.

The avatar fills in a booking form on the page as you talk to it — and notices
when you type into the form yourself. Two directions, one data topic:

  - agent -> browser: LLM function tools mutate page state via publish_data
  - browser -> agent: manual edits flow back on data_received and the avatar
    acknowledges them out loud

Run: python agent.py dev  (`console` mode uses a mock room — no avatar)
"""

import asyncio
import datetime
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    function_tool,
    inference,
    metrics,
)
from livekit.plugins import cartesia, openai, silero, synthesia

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logger = logging.getLogger("tools-avatar")

# --- Avatar & voice (any avatar/voice your Synthesia + Cartesia accounts can use) ---
AVATAR_ID = os.getenv("SYNTHESIA_AVATAR_ID", "4b638067-6319-46e1-b634-371a4bdfceb9")  # Mei
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4")  # Skylar

# The form. One entry per <input> id in index.html — keep the two in sync.
FIELDS = ("name", "email", "date", "topic")

# Agent and browser exchange form messages on this LiveKit data topic, as JSON:
#   {"type": "field", "field": ..., "value": ...}   either direction: one field changed
#   {"type": "submit"}                              browser -> agent: Submit clicked
#   {"type": "submitted"}                           agent -> browser: form accepted, freeze it
FORM_TOPIC = "form"


def build_instructions() -> str:
    today = datetime.date.today().isoformat()
    return f"""\
You are Mei, a friendly booking assistant rendered as a real-time Synthesia
avatar, standing next to an appointment form the visitor can see. This is a
spoken conversation: your replies are spoken aloud, so never refer to typing,
reading, chat, bullet points, markdown, or URLs. Answer in one to three short
sentences.

Your job is to fill in the form by chatting: the visitor's name, email,
preferred date, and the topic of the appointment. Ask for one or two fields at
a time. The moment the visitor gives you a value, call update_field — they
watch the form fill in live, so never save updates for later. Today is
{today}; resolve spoken dates like "next Tuesday" to YYYY-MM-DD yourself.
Email addresses are easy to mishear: read the address back to confirm, and
mention they can also type it straight into the form. The visitor may edit any
field directly — those values are authoritative; call get_form if you are
unsure of the current state. Once every field is filled, read all the details
back, and only after the visitor confirms call submit_form."""


class FormAgent(Agent):
    """An agent whose tools read and write a form the browser renders live."""

    def __init__(self, room: rtc.Room) -> None:
        super().__init__(instructions=build_instructions())
        self._room = room
        # Single source of truth — the LLM (tools) and the browser (data topic) both read and write it.
        self.form: dict[str, str] = dict.fromkeys(FIELDS, "")
        self.submitted = False

    async def _publish(self, msg: dict) -> None:
        await self._room.local_participant.publish_data(
            json.dumps(msg).encode(), reliable=True, topic=FORM_TOPIC
        )

    @function_tool
    async def update_field(self, field: str, value: str) -> dict:
        """Set one field of the appointment form the visitor is looking at.

        Args:
            field: One of: name, email, date, topic.
            value: The value to show. Dates must be YYYY-MM-DD.
        """
        if field not in FIELDS:
            return {"status": "error", "detail": f"unknown field; valid: {', '.join(FIELDS)}"}
        if self.submitted:
            return {"status": "error", "detail": "the form is already submitted"}
        self.form[field] = value.strip()
        await self._publish({"type": "field", "field": field, "value": self.form[field]})
        return {"status": "ok"}

    @function_tool
    async def get_form(self) -> dict:
        """Read the current form state, including values the visitor typed themselves."""
        return {"fields": self.form, "submitted": self.submitted}

    @function_tool
    async def submit_form(self) -> dict:
        """Submit the completed form. Call only after the visitor confirms the details."""
        missing = [f for f in FIELDS if not self.form[f]]
        if missing:
            return {"status": "error", "missing": missing}
        self.submitted = True
        await self._publish({"type": "submitted"})
        # This is where a real app hands off — POST to your booking API / CRM here.
        logger.info("form submitted: %s", json.dumps(self.form))
        return {"status": "submitted", "form": self.form}


def prewarm(proc) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    session = AgentSession(
        stt=inference.STT(model="cartesia/ink-2"),
        # Tool calling works the same with any LiveKit-supported LLM provider.
        llm=openai.LLM(model="gpt-4o"),
        tts=cartesia.TTS(model="sonic-3.5", voice=CARTESIA_VOICE_ID),
        vad=ctx.proc.userdata["vad"],
        turn_handling={
            "turn_detection": "stt",
            # OFF on purpose: a speculative reply from an eager transcript could
            # call update_field with a half-heard value. Tools + speculation don't mix.
            "preemptive_generation": {"enabled": False},
            "interruption": {"min_duration": 0.75, "min_words": 1, "false_interruption_timeout": 2.0},
        },
    )

    @session.on("metrics_collected")
    def on_metrics(ev) -> None:
        metrics.log_metrics(ev.metrics)

    agent = FormAgent(room=ctx.room)

    def on_data(packet: rtc.DataPacket) -> None:
        if packet.topic != FORM_TOPIC:
            return
        try:
            msg = json.loads(packet.data)
        except (ValueError, UnicodeDecodeError):
            return
        # Browser data is untrusted input: whitelist the field, coerce and cap the value.
        if msg.get("type") == "field" and msg.get("field") in FIELDS and not agent.submitted:
            field, value = msg["field"], str(msg.get("value", ""))[:200].strip()
            agent.form[field] = value
            session.generate_reply(
                instructions=(
                    f"The visitor just typed into the form themselves: {field} is now "
                    f"{value!r}. Acknowledge it in one short sentence and carry on."
                )
            )
        elif msg.get("type") == "submit":
            session.generate_reply(
                instructions=(
                    "The visitor clicked Submit on the form. Call submit_form; if it "
                    "reports missing fields, ask for them instead."
                )
            )

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

    await session.start(agent=agent, room=ctx.room)
    # After session.start(): the handler calls generate_reply, which needs a running session.
    ctx.room.on("data_received", on_data)

    session.generate_reply(
        instructions="Greet the visitor in one short sentence and offer to book them an appointment.",
        allow_interruptions=False,
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
