# Synthesia Interactive Avatar — RAG Quickstart

A [Synthesia Interactive Avatar](https://www.synthesia.io/features/avatars/interactive-avatars) that answers spoken questions grounded in a **knowledge base** — it speaks *only* from retrieved source material, not the model's training knowledge. Built on the [minimal quickstart](../minimal/README.md): a Python [LiveKit agent](https://docs.livekit.io/agents/) rendered as a photoreal, lip-synced avatar in your browser.

**Runs cold, no cloud setup.** Out of the box it retrieves from the **public Wikipedia API** (`KB_SOURCE=wikipedia`) — no key, any topic — so you can hear grounding work in minutes. When you're ready to ground the avatar in *your own* private corpus, switch to `KB_SOURCE=bedrock` and point it at an AWS Bedrock knowledge base you've built — the worked example in this README grounds **Aristotle** in his own works. See [Grounding your own corpus with Bedrock](#grounding-your-own-corpus-with-bedrock).

## Why grounding matters

An LLM will happily produce plausible-but-invented facts. For a photoreal, talking avatar — especially one wearing a **real person's likeness** — that isn't a small error: it's the avatar stating something the person never said, a reputational and potentially legal risk. So the knowledge base must be the **single source of truth**: substance comes only from retrieved, approved content; the model's job is to *phrase* it, not to *supply* it. The line to hold: *applying* documented principles to a new question is fine; *inventing* specific facts or opinions about people or things not in the corpus is not — even in character.

Note that RAG grounds the model, it never trains it: each answer is assembled live from freshly retrieved passages, so the knowledge base stays the single source of truth — edit it and behaviour changes immediately; nothing is retained between sessions.

Honest caveat: this is prompt-based grounding — strong, but statistical, not a hard guarantee. For real-person likenesses, add a post-generation factual-consistency check (verify each claim against the retrieved passages before it's spoken) as the belt-and-braces bar.

## Prerequisites

- Python **3.10–3.13** (3.13 recommended; the LiveKit plugins require < 3.14)
- **LiveKit Cloud** project (free tier works): `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` from [cloud.livekit.io](https://cloud.livekit.io)
- **Synthesia** API key ([Interactive Avatars](https://www.synthesia.io/features/avatars/interactive-avatars))
- **OpenAI** API key from [platform.openai.com](https://platform.openai.com/api-keys)
- *(Only for `KB_SOURCE=bedrock`)* **AWS credentials** that can call `bedrock-agent-runtime:Retrieve`, and a **Bedrock managed knowledge base** — see [Grounding your own corpus with Bedrock](#grounding-your-own-corpus-with-bedrock)

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in the three keys — no AWS needed for the default

python agent.py dev    # terminal 1: the agent worker
python server.py       # terminal 2: frontend at http://localhost:8080
```

Open <http://localhost:8080>, click **Start**, and allow the microphone. Expected sequence: you join the room → the agent connects → the avatar joins and publishes video (cold starts can take longer) → it greets you. Ask it anything — the default Wikipedia source covers any topic.

> The avatar does not announce that it's AI by default — how (and whether) to declare that is left to you. For real deployments, especially avatars wearing a real person's likeness, add a disclosure (see the greeting in `agent.py`).

> Don't use `python agent.py console` — it runs a mock room and the avatar will silently never appear. Always test through a real room, like this frontend.

## How a turn flows

The pipeline order is owned by the `livekit-agents` framework's `AgentSession` run loop (`livekit/agents/voice/agent_activity.py`), not this repo. This quickstart supplies only the components, one retrieval hook, and one ordering flag. A full turn:

```
mic audio ─► STT (Cartesia Ink-2, streaming)
          ─► turn detection  ── user stops speaking
          ─► on_user_turn_completed(turn_ctx, new_message)   ← the only seam we own
                 ├─ retrieve passages from the KB (Wikipedia / Bedrock)
                 └─ inject them into turn_ctx as system context
          ─► LLM (GPT-4o) generates from the augmented context
          ─► TTS (Cartesia Sonic) speaks the reply
          ─► Synthesia avatar lip-syncs the audio
```

Everything RAG-specific in `agent.py` lives in that one hook:

```python
class GroundedAgent(Agent):
    async def on_user_turn_completed(self, turn_ctx, new_message):
        passages = await self._retrieve(new_message.text_content)   # Wikipedia or Bedrock
        turn_ctx.add_message(role="system", content="Knowledge base context:\n" + passages)
```

Two properties make retrieval **unconditional** — guaranteed to precede generation, not best-effort:

1. **The hook is awaited.** The framework won't start the LLM until `on_user_turn_completed` returns, so retrieved passages are always in context before generation — the model cannot answer without the KB step running first.
2. **Preemptive generation is off** (`turn_handling` in `agent.py`). With it on, the framework would speculatively start the LLM on the partial transcript, racing retrieval and risking an ungrounded reply.

So the sequence is **STT → KB → LLM → TTS → avatar**, with the KB step wedged into the one gap the framework opens between transcription and generation. That ordering has a latency cost: on the Wikipedia path each turn runs a query rewrite (a small-model call) plus the search and page fetches before generation can start — expect roughly a second more per turn than the minimal quickstart. Bedrock's semantic search skips the rewrite step.

`_retrieve` dispatches on `KB_SOURCE`; adding another backend is just another retriever behind the same hook. When retrieval returns nothing, the model is told to say so and decline rather than answer from general knowledge.

## Seeing the tool chain (the status pill)

A small pill in the browser shows what the pipeline is doing each turn: **Listening… → Querying knowledge base… → Thinking… → Speaking…**. It makes the grounding visible — you can watch the KB query fire *before* the model generates, on every turn.

The agent publishes each stage on a LiveKit data topic (`pipeline-status`) and the frontend renders it — no polling, no extra service. The KB and Thinking stages come from the retrieval hook (so they're correctly ordered within a turn); Speaking and Listening come from the session's own `agent_state_changed` events. It's a handy demo affordance and a debugging aid: if you never see "Querying knowledge base…", retrieval isn't wired up.

## Grounding your own corpus with Bedrock

`KB_SOURCE` selects where passages come from. Both sources sit behind the same retrieval hook — only the retriever changes.

| `KB_SOURCE` | Corpus | Setup | Use it for |
| --- | --- | --- | --- |
| `wikipedia` *(default)* | Public Wikipedia, any topic | None — no key, no cloud | Trying the quickstart cold; general-knowledge demos |
| `bedrock` | Your private docs in S3 | AWS account + a Bedrock managed KB | Grounding in a *controlled, private* corpus (real customer PoCs) |

### 1. Build the knowledge base

1. Create an **S3 bucket**; upload your approved source docs under a `material/` prefix. Prefer smaller, semantically-coherent chunks (one per file) over whole documents — it keeps retrieval tight and fast.
2. Create a **Bedrock managed knowledge base** whose data source is scoped to that `material/` prefix only. Let it ingest/sync. Note the KB id.
3. **Verify the source, don't bulk-grab** — the KB *is* the avatar's source of truth, so mis-attributed content corrupts every answer. Strip licence/boilerplate (e.g. Project Gutenberg headers) before ingest, or it gets retrieved as if it were the author.

A worked example to copy: an **Aristotle** avatar grounded in his own works (Nicomachean Ethics, Politics, Poetics, Categories) — public-domain texts, chunked one theme per file, with `PERSONA_NAME=Aristotle` giving first-person answers whose substance comes only from those texts.

### 2. Point the agent at it

The Bedrock retriever is already in the code — switching is **env-only, no code changes**:

```bash
pip install boto3          # optional dep, only the Bedrock path needs it
```
```dotenv
# in .env
KB_SOURCE=bedrock
BEDROCK_KB_ID=your-kb-id       # required — the managed KB you just built
AWS_REGION=eu-west-1           # run the agent in the same region as the KB
AWS_ACCESS_KEY_ID=...          # or set AWS_PROFILE instead of these three
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...          # only for temporary / SSO credentials
```

`boto3` uses the standard credential chain: keys in `.env`, or leave them blank and set `AWS_PROFILE`. SSO / temporary credentials expire every few hours and need refreshing; in a deployed setup attach an IAM role instead. Restart the agent — the persona and greeting adapt automatically (Kenji → Aristotle).

Sanity-check retrieval from the CLI before touching the agent:

```bash
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id "$BEDROCK_KB_ID" \
  --retrieval-query '{"text":"what is virtue?"}' \
  --retrieval-configuration '{"managedSearchConfiguration":{}}' \
  --region "$AWS_REGION"
```

Passages returned = good to go. An expired-token error = refresh AWS credentials first. Note the `managedSearchConfiguration`: a **managed** KB *rejects* the `vectorSearchConfiguration` you'd use for a custom KB — pass the wrong one and retrieval silently returns nothing. This is the single most common RAG-on-Bedrock trap; the agent's `_retrieve_bedrock` already passes the right one.

## Proving it's grounded (the canary test)

A plausible answer doesn't prove retrieval fired — the model may already know it from training. On the **Bedrock** path you can prove grounding by planting a fact the model couldn't know:

1. Add a short document to your S3 source prefix with an invented, clearly-fictional fact (e.g. *"This is the Indigo Reference Edition, compiled by Prospero Quill."*), then re-sync the data source.
2. Ask the avatar about it. If it repeats the planted fact, the answer can only have come from the KB — grounding is proven. Watch the agent log for the matching `[KB] injected N passages` line.
3. Delete the canary from S3 and re-sync before anyone sees it.

On the **Wikipedia** path you can't plant facts, but the `[KB] injected N passages` log line (and the pill's *Querying knowledge base…* stage) still confirm retrieval fired on every turn; ask about very recent events to see it ground on content past the model's training cutoff.

## Make it yours

All in `agent.py` (or via `.env`):

- **Who it embodies** — `PERSONA_NAME` sets the manner (defaults to "Kenji" for Wikipedia — matching the default avatar face — and "Aristotle" for Bedrock); substance always comes from the KB. `build_instructions()` picks the spine by source: a generic grounded assistant for Wikipedia, a first-person embodiment for Bedrock.
- **Retrieved passages** — `RETRIEVAL_TOP_N` controls how many best-first passages are injected. Passages are kept in retrieval rank order (the batched Wikipedia extracts are re-sorted to search rank; Bedrock managed search already returns best-first). For production-grade retrieval, add a reranker (e.g. Cohere Rerank or a cross-encoder) between retrieve and inject.
- **Query rewriting (Wikipedia path)** — spoken questions are rewritten into a clean search query by a small model (`QUERY_REWRITE_MODEL`, default `gpt-4o-mini`) before hitting Wikipedia's keyword search, so *"tell me about Latvia"* searches `Latvia` rather than the song *"Santa Tell Me"*. A heuristic cleaner (`clean_query`) is the fallback. Bedrock's semantic search doesn't need this.
- **Voice / Avatar** — `CARTESIA_VOICE_ID` ([Cartesia voices](https://play.cartesia.ai/voices)) and `SYNTHESIA_AVATAR_ID` (any avatar your workspace can access).
- **Models** — swap the `stt=` / `llm=` / `tts=` lines for any [LiveKit-supported provider](https://docs.livekit.io/agents/models/); the RAG hook is provider-independent.

`server.py` mints room tokens with `sync_streams=True` (avatar audio/video sync) and a `RoomAgentDispatch` matching the worker's `agent_name` (dispatch — see Security & production) — keep both when you build your own token endpoint. Keep `SYNTHESIA_API_KEY` (and your AWS credentials) server-side — they must never reach frontend code.

## Security & production

> **Demo only — do not deploy as-is.** This is a local quickstart, not a production template.

- **The `/token` endpoint is unauthenticated.** The demo server binds to localhost and mints 15-minute, room-scoped tokens, so exposure is limited to your machine — but anyone who can reach the endpoint can dispatch an agent worker (which costs money), so a production endpoint must sit behind your app's authentication.
- **Agent dispatch is explicit.** The worker sets `agent_name` and only joins rooms whose token requests it via `RoomAgentDispatch` (`server.py`) — keep the two names in sync. Don't remove `agent_name`: an unnamed worker auto-joins *every* new room in the LiveKit project.
- **Keep secrets server-side.** `SYNTHESIA_API_KEY`, `OPENAI_API_KEY`, and AWS credentials live only in `.env` (git-ignored) or your secrets manager — never in the frontend.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| Avatar answers but ignores the KB / makes things up | Retrieval returned nothing. Check the CLI `retrieve` works; confirm you're using `managedSearchConfiguration`, the right `BEDROCK_KB_ID`, and the right `AWS_REGION`. |
| `[KB] retrieve failed` in the log | AWS creds missing/expired, wrong region, or the role lacks `bedrock:Retrieve` on this KB. |
| `ExpiredTokenException` / `InvalidSignatureException` | Temporary AWS credentials lapsed — refresh them. |
| `SynthesiaError` with `type` `AUTH` | Synthesia API key invalid or expired. |
| `SynthesiaError` with `type` `UNKNOWN_AVATAR` | `SYNTHESIA_AVATAR_ID` isn't available to your workspace. |
| `SynthesiaError` with `type` `QUOTA_EXCEEDED` | Synthesia minute or concurrent-session cap hit. |
| Avatar never appears, no error | You're in `console` mode, `avatar.start()` ran after `session.start()`, or the token lacks the `RoomAgentDispatch` room config. |
| Avatar joins but doesn't lip-sync | Something reassigned `session.output.audio` after the avatar attached. |
