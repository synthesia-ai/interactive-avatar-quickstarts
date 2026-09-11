# Synthesia Interactive Avatar — Minimal Quickstart

The smallest useful [Synthesia Interactive Avatar](https://www.synthesia.io/features/avatars/interactive-avatars) app: a Python [LiveKit agent](https://docs.livekit.io/agents/) you can talk to, rendered as a photoreal, lip-synced avatar in your browser. Drop in three API keys and go.

**How it works:** the agent listens with Cartesia Ink-2 STT, thinks with OpenAI Realtime used as a text-only streaming brain, and speaks with Cartesia Sonic TTS — both Cartesia models run through [LiveKit Inference](https://docs.livekit.io/agents/models/), billed to your LiveKit account, so no Cartesia key is needed. The Synthesia plugin reroutes that speech to a hosted avatar worker, which joins the LiveKit room as a regular participant publishing lip-synced video. The frontend is a plain LiveKit client; it needs zero Synthesia-specific code.

**Latency:** `realtime_preflight.py` hides ~0.3–0.5 s per turn by starting the Realtime reply on the eager end-of-turn transcript. Look for `Adopting realtime preflight speculation` in the logs; disable with `REALTIME_PREFLIGHT_ENABLED=false`.

## Prerequisites

- Python 3.10+
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

Open <http://localhost:8080>, click **Start**, and allow the microphone. Expected sequence: you join the room → the agent connects → the avatar joins and publishes video (cold starts can take longer) → it greets you. Say hello.

> Don't use `python agent.py console` — it runs a mock room and the avatar will silently never appear. Always test through a real room, like this frontend.

## The integration, in three lines

Everything avatar-specific in `agent.py` is:

```python
from livekit.plugins import synthesia

avatar = synthesia.AvatarSession(synthesia.AvatarConfig(avatar_ids=[AVATAR_ID]))
await avatar.start(session, room=ctx.room)   # before session.start() — order matters
```

The agent produces speech exactly as it always does; the plugin intercepts `session.output.audio` and the avatar lip-syncs it. That means you can swap any STT/LLM/TTS providers in `AgentSession` and the avatar lines never change.

## Make it yours

All in `agent.py`:

- **Personality** — edit `INSTRUCTIONS`.
- **Voice** — set `CARTESIA_VOICE_ID` to any [Cartesia library voice](https://play.cartesia.ai/voices) (a free account is enough to browse; usage bills via LiveKit Inference).
- **Custom / cloned voices** — LiveKit Inference only serves Cartesia's public library, so a [voice cloned](https://docs.cartesia.ai/build-with-cartesia/capability-guides/clone-voices) in your own Cartesia account needs the direct plugin instead: get a Cartesia API key, add `CARTESIA_API_KEY=` to `.env`, change `requirements.txt` to `livekit-agents[cartesia,openai,silero]`, and swap the `tts=` line to `cartesia.TTS(model="sonic-3.6", voice=CARTESIA_VOICE_ID)` (adding `cartesia` to the `livekit.plugins` import). TTS then bills to your Cartesia account rather than LiveKit.
- **Avatar** — set `AVATAR_ID` to any avatar your Synthesia workspace has access to.
- **Models** — swap the `stt=` / `llm=` / `tts=` lines for any [LiveKit-supported provider](https://docs.livekit.io/agents/models/). Preflight is a no-op for non-Realtime models.

`server.py` mints room tokens with `sync_streams=True` — keep that when you build your own token endpoint, it's what keeps the avatar's audio and video in sync in the browser. And keep `SYNTHESIA_API_KEY` server-side: it's a workspace-bound secret that must never reach frontend code.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `SynthesiaAuthError` | API key invalid or expired. |
| `UnknownAvatarError` | `AVATAR_ID` isn't available to your workspace. |
| `QuotaExceededError` | Minute or concurrent-session cap hit. |
| `SynthesiaTimeoutError` | Cold start took too long — retry; the agent already waits 60 s and retries transient errors. |
| Avatar never appears, no error | You're in `console` mode, or `avatar.start()` ran after `session.start()`. |
| Avatar joins but doesn't lip-sync | Something reassigned `session.output.audio` after the avatar attached. |
