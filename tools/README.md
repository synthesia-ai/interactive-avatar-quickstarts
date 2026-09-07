# Synthesia Interactive Avatar — Tools Quickstart

A [Synthesia Interactive Avatar](https://www.synthesia.io/features/avatars/interactive-avatars) that **acts, not just talks**: it fills in a booking form on the page as you speak to it, and reacts out loud when you type into the form yourself. Built on the [minimal quickstart](../minimal/README.md): a Python [LiveKit agent](https://docs.livekit.io/agents/) rendered as a photoreal, lip-synced avatar in your browser.

**The pattern this demonstrates** is LLM tool calling plus shared UI state, synced both ways over LiveKit data channels:

- **agent → browser**: the LLM calls a `@function_tool` (`update_field`), which publishes the change on a data topic; the browser renders it. The avatar visibly *does* something.
- **browser → agent**: your manual edits (and the Submit click) publish back on the same topic; the agent updates its state and the avatar acknowledges out loud.

The form is a stand-in for any state your app shares with the avatar — a booking, an onboarding flow, a support ticket, a shopping cart.

## Prerequisites

- Python **3.10–3.13** (3.13 recommended; the LiveKit plugins require < 3.14)
- **LiveKit Cloud** project (free tier works): `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` from [cloud.livekit.io](https://cloud.livekit.io)
- **Synthesia** API key with [Interactive Avatar access](https://www.synthesia.io/features/avatars/interactive-avatars)
- **Cartesia** API key from [play.cartesia.ai](https://play.cartesia.ai)
- **OpenAI** API key from [platform.openai.com](https://platform.openai.com/api-keys)

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then fill in the four keys

python agent.py dev    # terminal 1: the agent worker
python server.py       # terminal 2: frontend at http://localhost:8080
```

Open <http://localhost:8080>, click **Start**, and allow the microphone. Once the avatar joins, the form unlocks. Try both directions:

1. **Speak**: "I'd like to book something — I'm Ada Lovelace, next Tuesday, about analytical engines." Watch the fields flash blue as the avatar fills them.
2. **Type**: put your email straight into the form — the avatar notices and confirms it out loud.
3. Confirm verbally (or click **Submit**) and the agent validates, freezes the form, and logs the record.

> Don't use `python agent.py console` — it runs a mock room and the avatar will silently never appear. Always test through a real room, like this frontend.

## How it works

One data topic (`form`), three message shapes, and server-side state as the single source of truth:

```
you speak ─► STT ─► LLM calls update_field(field, value)
                        └─► agent state updated ─► publish_data ─► the form fills in

you type  ─► browser publishData ─► data_received handler
                        └─► agent state updated ─► generate_reply ─► the avatar reacts aloud
```

Everything tools-specific in `agent.py` is two seams. The tool (agent → browser):

```python
@function_tool
async def update_field(self, field: str, value: str) -> dict:
    self.form[field] = value.strip()
    await self._publish({"type": "field", "field": field, "value": self.form[field]})
    return {"status": "ok"}
```

And the data handler (browser → agent):

```python
def on_data(packet):
    msg = json.loads(packet.data)
    agent.form[msg["field"]] = msg["value"]
    session.generate_reply(instructions=f"The visitor typed {msg['field']} themselves — acknowledge it.")
```

Because the authoritative form lives in `FormAgent.form` — not in the LLM's conversation memory and not in the DOM — neither side can drift: the model reads it back with `get_form`, and `submit_form` validates against it, so a value you typed counts exactly like one the avatar heard.

Two deliberate choices worth copying:

- **Preemptive generation is off** (`turn_handling` in `agent.py`). The framework can speculatively start a reply on an eager transcript; with side-effecting tools, that speculation could call `update_field` with a half-heard value and then be discarded. Speculation and side effects don't mix.
- **Browser input is untrusted.** The handler whitelists field names and caps value length before touching state. Keep that shape when you extend the protocol.

## Make it yours

- **The form** — edit `FIELDS` in `agent.py` and the matching `<input>` ids in `index.html` (the two lists must stay in sync), and mention the new fields in `build_instructions()`.
- **What Submit does** — `submit_form` currently logs the record; replace the `logger.info` line with a POST to your booking API or CRM.
- **More tools** — any `@function_tool` on `FormAgent` becomes something the avatar can *do*: fetch availability before offering dates, look up the visitor by email, navigate the page. Same seam, richer app.
- **Voice / Avatar** — `CARTESIA_VOICE_ID` ([Cartesia voices](https://play.cartesia.ai/voices)) and `SYNTHESIA_AVATAR_ID` (any avatar your workspace can access), via `.env`.
- **Models** — swap the `stt=` / `llm=` / `tts=` lines for any [LiveKit-supported provider](https://docs.livekit.io/agents/models/); tool calling is provider-independent.

`server.py` mints room tokens with `sync_streams=True` — keep that when you build your own token endpoint; it's what keeps the avatar's audio and video in sync in the browser. Keep `SYNTHESIA_API_KEY` server-side: it's a workspace-bound secret that must never reach frontend code.

## Security & production

> **Demo only — do not deploy as-is.** This is a local quickstart, not a production template.

- **The `/token` endpoint is unauthenticated.** The demo server binds to localhost and mints 15-minute, room-scoped tokens, so exposure is limited to your machine — but anyone who can reach the endpoint can dispatch an agent worker (which costs money), so a production endpoint must sit behind your app's authentication.
- **Make agent dispatch explicit in production.** The worker dispatches to *every* new room in the LiveKit project, so anything that creates a room burns avatar minutes. Set `agent_name` in `WorkerOptions` and request the agent per-token via `RoomAgentDispatch` in the room config so dispatch is opt-in.
- **Data-channel messages come from the browser** — treat them as untrusted user input on the agent side (the handler already whitelists fields and caps lengths; keep doing that as you extend it).
- **Form contents are PII** in a real deployment — the demo logs the submitted record; route it somewhere appropriate before collecting real data.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| Avatar talks but the form never fills | Field ids in `index.html` don't match `FIELDS` in `agent.py`, or the topics differ — both sides must use `form`. |
| The date field stays blank | `<input type="date">` silently ignores anything that isn't `YYYY-MM-DD` — the instructions tell the model this; check what `update_field` received in the agent log. |
| Typing in the form does nothing | Edits only send on blur (the `change` event) — click out of the field. Also confirm the avatar has joined; the form is disabled until then. |
| A field updates with a half-heard value mid-sentence | Preemptive generation got re-enabled — it must stay off with side-effecting tools (see `turn_handling`). |
| `SynthesiaAuthError` | API key invalid, or the workspace doesn't have Interactive Avatar access. |
| `UnknownAvatarError` | `SYNTHESIA_AVATAR_ID` isn't available to your workspace. |
| `QuotaExceededError` | Minute or concurrent-session cap hit. |
| Avatar never appears, no error | You're in `console` mode, or `avatar.start()` ran after `session.start()`. |
| Avatar joins but doesn't lip-sync | Something reassigned `session.output.audio` after the avatar attached. |
