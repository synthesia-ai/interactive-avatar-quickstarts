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

## Deploy

`agent.py` is a worker: it dials out to LiveKit over a WebSocket and takes jobs from there, so it needs no inbound traffic, just a container that keeps running. The `Dockerfile` builds that container (`python agent.py start` is the production form of `dev`). `server.py`, the demo frontend, is not part of it and belongs behind your app's auth.

```bash
docker build -t avatar-agent .
docker run --rm --env-file .env avatar-agent   # secrets injected at run time
```

Docker's `--env-file` passes values verbatim, so keep them unquoted and free of trailing comments in `.env`, as `.env.example` does. Stop any local `python agent.py dev` first: both workers register under the same `agent_name`, and LiveKit may hand the job to either.

`.dockerignore` keeps `.env` out of the image on purpose. Baking it in hands your keys to anyone with the image. Pass the variables from your platform's secret store instead, and don't carry local-only settings across: a corporate proxy's `SSL_CERT_FILE`, for instance, names a path that doesn't exist in the container and breaks TLS there.

**LiveKit Cloud** is the zero-ops option: run `lk agent create` in this directory and it builds the image, uploads `.env` as secrets, and runs the worker on LiveKit's infrastructure. See [Deploying to LiveKit Cloud](https://docs.livekit.io/deploy/agents/).

**Google Cloud Run** needs a *worker pool*, not a service. A service throttles CPU whenever no HTTP request is in flight, and this worker never receives one, so it starves: process prewarm times out and jobs never start. A worker pool keeps CPU allocated, needs no port, and runs a fixed number of instances:

```bash
gcloud run worker-pools deploy avatar-agent --source . --ignore-file .dockerignore --region <region> \
  --instances 1 --memory 2Gi \
  --set-env-vars LIVEKIT_URL=wss://<your-project>.livekit.cloud \
  --set-secrets LIVEKIT_API_KEY=livekit-api-key:latest,LIVEKIT_API_SECRET=livekit-api-secret:latest,SYNTHESIA_API_KEY=synthesia-api-key:latest,OPENAI_API_KEY=openai-api-key:latest
```

Create the four secrets in Secret Manager first. `--ignore-file` applies `.dockerignore` to the source upload as well, so `.env` and `.venv` never reach the build bucket. `rag/` and `tools/` have more variables in their `.env.example`; add each the same way (the Bedrock path needs explicit `AWS_*` key secrets, since there is no AWS role to fall back on here).

The same rule applies to AWS Fargate and Azure Container Apps: run the worker as an always-on background task with CPU allocated, not as a request-scaled service. If you must use a request-scaled service, set CPU to always allocated, keep at least one instance warm, and set the container port to `8081`: the worker's health server listens there and ignores `$PORT`.

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
| `FileNotFoundError` from `_create_ssl_context` at startup in a container | `SSL_CERT_FILE` / `SSL_CERT_DIR` reached the container's environment (via `--env-file`, platform secrets, or a copied `.env`) and points at a path that only exists on your machine. Unset it for the container. |
| `failed to connect to livekit` with `%22` in the error, in a container | The URL carries literal quotes: `docker run --env-file` passes values verbatim. Remove the quotes around values in `.env`. |
| Worker starts in Cloud Run but jobs never run; `initializing process` times out | CPU is throttled because the worker runs as a Cloud Run *service*. Deploy it as a worker pool instead (see [Deploy](#deploy)). |
