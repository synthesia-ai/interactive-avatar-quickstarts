# Synthesia Interactive Avatar — Tools Quickstart

A [Synthesia Interactive Avatar](https://www.synthesia.io/features/avatars/interactive-avatars) that fills in a booking form on the page as you speak to it, and reacts out loud when you type into the form yourself. Built on the [minimal quickstart](../minimal/README.md): a Python [LiveKit agent](https://docs.livekit.io/agents/) rendered as a photoreal, lip-synced avatar in your browser.

The pattern is LLM tool calling plus shared UI state, synced both ways over LiveKit data channels:

- **agent → browser**: the LLM calls a `@function_tool` (`update_field`), which publishes the change on a data topic, and the browser renders it.
- **browser → agent**: your manual edits (and the Submit click) publish back on the same topic; the agent updates its state and the avatar acknowledges out loud.

The form is a stand-in for any state your app shares with the avatar: a booking, an onboarding flow, a support ticket, a shopping cart.

## Prerequisites

- Python **3.10–3.14**
- **LiveKit Cloud** project (free tier works) from [cloud.livekit.io](https://cloud.livekit.io)
- [**LiveKit CLI**](https://github.com/livekit/livekit-cli#installation) (`brew install livekit-cli`)
- **Synthesia** API key ([Interactive Avatars](https://www.synthesia.io/features/avatars/interactive-avatars))
- **OpenAI** API key from [platform.openai.com](https://platform.openai.com/api-keys)

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

lk cloud auth                            # link your LiveKit project
lk app env --write --destination .env    # writes LiveKit credentials, prompts for the rest

python agent.py dev    # terminal 1: the agent worker
python server.py       # terminal 2: frontend at http://localhost:8080
```

Without the CLI: create an API key under **Settings → API Keys** in your LiveKit project, `cp .env.example .env`, and fill in the values.

Open <http://localhost:8080>, click **Start**, and allow the microphone. Once the avatar joins, the form unlocks. Try both directions:

1. **Speak**: "I'd like to book something. I'm Ada Lovelace, next Tuesday, about analytical engines." Watch the fields flash blue as the avatar fills them.
2. **Type**: put your email straight into the form; the avatar notices and confirms it out loud.
3. Confirm verbally (or click **Submit**) and the agent validates, freezes the form, and logs the record.

> Don't use `python agent.py console`. It runs a mock room and the avatar will silently never appear. Always test through a real room, like this frontend.

## Optional: Quail Voice Focus

**Quail is disabled by default.** In noisy environments, such as an open office with background conversations, enabling [Quail Voice Focus](https://docs.ai-coustics.com/models/voice-focus/quail-voice-focus) can help isolate your voice, improve transcription, and reduce unwanted interruptions.

This option uses the official `livekit-plugins-ai-coustics` plugin with `QUAIL_VF_L` (Voice Focus **2.2 L**, bundled in version 0.3.2). It runs on the agent worker before the existing Silero VAD and transcription. The plugin includes the models and uses your existing LiveKit Cloud credentials; no separate ai-coustics key or model download is needed. Voice isolation incurs an [additional LiveKit charge](https://docs.livekit.io/transport/media/noise-cancellation/#voice-isolation).

From this recipe's directory, with its virtual environment activated:

1. Install the optional dependencies:

   ```bash
   pip install -r requirements-quail.txt
   ```

2. Enable it in `.env`:

   ```dotenv
   QUAIL_ENABLED=true
   ```

Restart `python agent.py dev`. The agent logs `Quail Voice Focus configured via LiveKit Cloud` when it attaches the processor. If the optional plugin cannot be imported or configured, the agent logs a warning and continues without Quail. The plugin also logs authentication or processing failures and passes audio through.

Set `QUAIL_ENABLED=false` and restart to disable it. The normal install and default run do not need the Quail package.

## How it works

One data topic (`form`), three message shapes, and server-side state as the single source of truth:

```
you speak ─► STT ─► LLM calls update_field(field, value)
                        └─► agent state updated ─► publish_data ─► the form fills in

you type  ─► browser publishData ─► data_received handler
                        └─► agent state updated ─► generate_reply ─► the avatar reacts aloud
```

Everything tools-specific in `agent.py` is two functions. The tool (agent → browser):

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

The authoritative form lives in `FormAgent.form`, not in the LLM's conversation memory or the DOM, so neither side can drift. The model reads it back with `get_form`, and `submit_form` validates against it, so a value you typed counts exactly like one the avatar heard.

Two choices worth copying:

- **Preemptive generation is off** (`turn_handling` in `agent.py`). The framework can speculatively start a reply on an eager transcript; with side-effecting tools, that speculation could call `update_field` with a half-heard value and then be discarded.
- **Browser input is untrusted.** The handler whitelists field names and caps value length before touching state. Keep that shape when you extend the protocol.

## Make it yours

- **The form**: edit `FIELDS` in `agent.py` and the matching `<input>` ids in `index.html` (the two lists must stay in sync), and mention the new fields in `build_instructions()`.
- **What Submit does**: `submit_form` currently logs the record; replace the `logger.info` line with a POST to your booking API or CRM.
- **More tools**: any `@function_tool` on `FormAgent` becomes something the avatar can do, like fetching availability before offering dates, looking up the visitor by email, or navigating the page.
- **Voice**: `CARTESIA_VOICE_ID` ([Cartesia voices](https://play.cartesia.ai/voices)), via `.env`.
- **Avatar**: `SYNTHESIA_AVATAR_ID`, via `.env`, must be the Interactive ID of a synthetic or Personal avatar your workspace can use. In Synthesia Studio, open the avatar's `•••` menu and choose **Copy Interactive ID**. Stock actor-based avatars such as Ryan or Ada can't be used as interactive avatars on any plan.
- **Models**: swap the `stt=` / `llm=` / `tts=` lines for any [LiveKit-supported provider](https://docs.livekit.io/agents/models/); tool calling is provider-independent.

`server.py` mints room tokens with `sync_streams=True` (avatar audio/video sync) and a `RoomAgentDispatch` matching the worker's `agent_name` (see Security & production). Keep both when you build your own token endpoint, and keep `SYNTHESIA_API_KEY` server-side: it's a workspace-bound secret that must never reach frontend code.

## Security & production

> **Demo only.** This is a local quickstart. Don't deploy it as-is.

- **The `/token` endpoint is unauthenticated.** The demo server binds to localhost and mints 15-minute, room-scoped tokens, so exposure is limited to your machine. But anyone who can reach the endpoint can dispatch an agent worker, which costs money, so a production endpoint must sit behind your app's authentication.
- **Agent dispatch is explicit.** The worker sets `agent_name` and only joins rooms whose token requests it via `RoomAgentDispatch` (`server.py`), so keep the two names in sync. Don't remove `agent_name`: an unnamed worker auto-joins *every* new room in the LiveKit project.
- **Data-channel messages come from the browser.** Treat them as untrusted user input on the agent side. The handler already whitelists fields and caps lengths; keep doing that as you extend it.
- **Form contents are PII** in a real deployment. The demo logs the submitted record; route it somewhere appropriate before collecting real data.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| Avatar talks but the form never fills | Field ids in `index.html` don't match `FIELDS` in `agent.py`, or the topics differ; both sides must use `form`. |
| The date field stays blank | `<input type="date">` silently ignores anything that isn't `YYYY-MM-DD`. The instructions tell the model this; check what `update_field` received in the agent log. |
| Typing in the form does nothing | Edits only send on blur (the `change` event), so click out of the field. Also confirm the avatar has joined; the form is disabled until then. |
| A field updates with a half-heard value mid-sentence | Preemptive generation got re-enabled. It must stay off with side-effecting tools (see `turn_handling`). |
| `SynthesiaError` with `type` `AUTH` | API key invalid or expired. |
| `SynthesiaError` with `type` `FEATURE_NOT_IN_PLAN` | Your Synthesia workspace's plan doesn't include Interactive Avatars. |
| `SynthesiaError` with `type` `LIVEKIT_CREDENTIALS_REJECTED` | `LIVEKIT_URL` and `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` aren't from the same LiveKit project. Re-run `lk app env --write --destination .env`. |
| `SynthesiaError` with `type` `UNKNOWN_AVATAR` | `SYNTHESIA_AVATAR_ID` isn't a usable Interactive ID: it's a stock actor-based avatar, it hasn't finished converting to an interactive avatar, or it isn't in your plan. |
| `SynthesiaError` with `type` `QUOTA_EXCEEDED` | Minute or concurrent-session cap hit. |
| Avatar never appears, no error | You're in `console` mode, `avatar.start()` ran after `session.start()`, or the token lacks the `RoomAgentDispatch` room config. |
| Avatar joins but doesn't lip-sync | Something reassigned `session.output.audio` after the avatar attached. |
