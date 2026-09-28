# Synthesia Interactive Avatar — Minimal Quickstart

The smallest useful [Synthesia Interactive Avatar](https://www.synthesia.io/features/avatars/interactive-avatars) app: a Python [LiveKit agent](https://docs.livekit.io/agents/) you can talk to, rendered as a photoreal, lip-synced avatar in your browser. Drop in three API keys and go.

**How it works:** the agent listens with Cartesia Ink-2 STT, thinks with OpenAI Realtime used as a text-only streaming brain, and speaks with Cartesia Sonic TTS. Both Cartesia models run through [LiveKit Inference](https://docs.livekit.io/agents/models/), billed to your LiveKit account, so you don't need a Cartesia key. The Synthesia plugin reroutes that speech to a hosted avatar worker, which joins the LiveKit room as a regular participant publishing lip-synced video. The frontend is a plain LiveKit client with no Synthesia-specific code.

**Latency:** `realtime_preflight.py` hides ~0.3–0.5 s per turn by starting the Realtime reply on the eager end-of-turn transcript. Look for `Adopting realtime preflight speculation` in the logs; disable with `REALTIME_PREFLIGHT_ENABLED=false`.

## Prerequisites

- Python **3.10–3.14**
- **LiveKit Cloud** project (free tier works): `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` from [cloud.livekit.io](https://cloud.livekit.io)
- **Synthesia** API key ([Interactive Avatars](https://www.synthesia.io/features/avatars/interactive-avatars))
- **OpenAI** API key from [platform.openai.com](https://platform.openai.com/api-keys)

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then fill in the three keys

python agent.py dev    # terminal 1: the agent worker
python server.py       # terminal 2: frontend at http://localhost:8080
```

Open <http://localhost:8080>, click **Start**, and allow the microphone. You join the room, the agent connects, the avatar joins and publishes video (cold starts can take longer), and then it greets you. Say hello.

> Don't use `python agent.py console`. It runs a mock room and the avatar will silently never appear. Always test through a real room, like this frontend.

## Optional: Quail Voice Focus

**Quail is disabled by default.** In noisy environments, such as an open office with background conversations, enabling [Quail Voice Focus](https://docs.ai-coustics.com/models/voice-focus/quail-voice-focus) can help isolate your voice, improve transcription, and reduce unwanted interruptions.

This optional setup uses Quail Voice Focus **2.2 L** on the agent worker, before speech detection and transcription. It keeps Silero VAD and requires an ai-coustics SDK key, billed separately from LiveKit.

From this recipe's directory, with its virtual environment activated:

1. Install the optional dependencies:

   ```bash
   pip install -r requirements-quail.txt
   ```

   This uses `ai-coustics-livekit-plugin`. Do not also install `livekit-plugins-ai-coustics`: the two packages share an import path but have different APIs.

2. Download the model once; the command prints the downloaded file's path:

   ```bash
   python -c 'from livekit.plugins import ai_coustics; print(ai_coustics.Model.download("quail-vf-2.2-l-16khz", "./models"))'
   ```

3. Get an SDK key from the [ai-coustics developer portal](https://developers.ai-coustics.com), then update `.env`:

   ```dotenv
   QUAIL_ENABLED=true
   AIC_SDK_KEY=your-sdk-key
   QUAIL_MODEL_PATH=path-printed-by-the-download-command
   ```

   Relative model paths resolve from this recipe's directory. Keep the SDK key server-side.

Restart `python agent.py dev`. The worker logs `Quail Voice Focus configured` when it attaches the processor; the plugin logs `Processor: initialized` when microphone processing starts. If configuration, model loading, or processor construction fails, the agent logs a warning and continues without Quail. The plugin also logs audio initialization or processing failures and passes audio through.

Set `QUAIL_ENABLED=false` and restart to disable it. The normal install and default run do not need the Quail package, SDK key, or model download.

## The integration, in three lines

Everything avatar-specific in `agent.py` is:

```python
from livekit.plugins import synthesia

avatar = synthesia.AvatarSession(synthesia.AvatarConfig(avatar_ids=[AVATAR_ID]))
await avatar.start(session, room=ctx.room)   # before session.start() — order matters
```

The agent produces speech exactly as it always does; the plugin intercepts `session.output.audio` and the avatar lip-syncs it. The plugin ships on PyPI as [`livekit-plugins-synthesia`](https://pypi.org/project/livekit-plugins-synthesia/); see the [LiveKit plugin docs](https://docs.livekit.io/agents/models/avatar/plugins/synthesia/) for its full API. That means you can swap any STT/LLM/TTS providers in `AgentSession` and the avatar lines never change.

## Make it yours

All in `agent.py`:

- **Personality**: edit `INSTRUCTIONS`.
- **Voice**: set `CARTESIA_VOICE_ID` to any [Cartesia library voice](https://play.cartesia.ai/voices) (a free account is enough to browse; usage bills via LiveKit Inference).
- **Custom / cloned voices**: LiveKit Inference only serves Cartesia's public library, so a [voice cloned](https://docs.cartesia.ai/build-with-cartesia/capability-guides/clone-voices) in your own Cartesia account needs the direct plugin instead: get a Cartesia API key, add `CARTESIA_API_KEY=` to `.env`, change `requirements.txt` to `livekit-agents[cartesia,openai,silero]`, and swap the `tts=` line to `cartesia.TTS(model="sonic-3.6", voice=CARTESIA_VOICE_ID)` (adding `cartesia` to the `livekit.plugins` import). TTS then bills to your Cartesia account rather than LiveKit.
- **Avatar**: set `AVATAR_ID` to any avatar your Synthesia workspace has access to.
- **Models**: swap the `stt=` / `llm=` / `tts=` lines for any [LiveKit-supported provider](https://docs.livekit.io/agents/models/). Preflight is a no-op for non-Realtime models.

`server.py` mints room tokens with `sync_streams=True` (keeps the avatar's audio and video in sync in the browser) and a `RoomAgentDispatch` matching the worker's `agent_name`. Dispatch is explicit, so without it the agent never joins, and without `agent_name` the worker would join *every* room in your LiveKit project. Keep both when you build your own token endpoint, and keep `SYNTHESIA_API_KEY` server-side: it's a workspace-bound secret that must never reach frontend code.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `SynthesiaError` with `type` `AUTH` | API key invalid or expired. |
| `SynthesiaError` with `type` `FEATURE_NOT_IN_PLAN` | Your Synthesia workspace's plan doesn't include Interactive Avatars. |
| `SynthesiaError` with `type` `LIVEKIT_CREDENTIALS_REJECTED` | `LIVEKIT_URL` and `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` aren't from the same LiveKit project. |
| `SynthesiaError` with `type` `UNKNOWN_AVATAR` | `AVATAR_ID` isn't available to your workspace. |
| `SynthesiaError` with `type` `QUOTA_EXCEEDED` | Minute or concurrent-session cap hit. |
| `SynthesiaError` with `type` `TIMEOUT` | Cold start took too long. Retry; the agent already waits 60 s and retries transient errors. |
| Avatar never appears, no error | You're in `console` mode, `avatar.start()` ran after `session.start()`, or the token lacks the `RoomAgentDispatch` room config. |
| Avatar joins but doesn't lip-sync | Something reassigned `session.output.audio` after the avatar attached. |
