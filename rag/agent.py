"""Knowledge-grounded LiveKit agent with a Synthesia interactive avatar (RAG).

The avatar answers spoken questions using ONLY passages retrieved from a knowledge
base — not the model's own training knowledge. On each user turn we query the KB,
inject the passages into the model context, and tell the model to answer from that
context (or decline).

Two retrieval sources, selected by KB_SOURCE:
  - "wikipedia" (default): the public Wikipedia API — no key, any topic, runs cold.
  - "bedrock": an AWS Bedrock managed KB over your own documents in S3.

Run: python agent.py dev  (`console` mode uses a mock room — no avatar)
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx  # noqa: E402 — boto3 is imported lazily, only for KB_SOURCE=bedrock
from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    inference,
    llm,
    metrics,
)
from livekit.plugins import openai, silero, synthesia

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logger = logging.getLogger("rag-avatar")

# --- Avatar & voice (any Synthesia avatar; any Cartesia library voice — TTS billed via LiveKit Inference) ---
AVATAR_ID = os.getenv("SYNTHESIA_AVATAR_ID", "7572faa9-15da-400d-8227-ef1ab8932523")  # Kenji (the Aristotle demo face)
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "b134c304-d095-4d2b-a77a-914f5e8e84e7")  # Sterling — Monarch (gravitas)

# --- Knowledge base source: "wikipedia" (public, no key, any topic) or "bedrock" ---
KB_SOURCE = os.getenv("KB_SOURCE", "wikipedia").lower()
if KB_SOURCE not in ("wikipedia", "bedrock"):
    raise ValueError(f'KB_SOURCE must be "wikipedia" or "bedrock", got {KB_SOURCE!r}')
RETRIEVAL_TOP_N = int(os.getenv("RETRIEVAL_TOP_N", "5"))

# Bedrock path (only used when KB_SOURCE=bedrock): a managed KB over your own S3 docs.
# See "Grounding your own corpus with Bedrock" in the README to build one.
BEDROCK_KB_ID = os.getenv("BEDROCK_KB_ID", "")
AWS_REGION = os.getenv("AWS_REGION", "eu-west-1")
if KB_SOURCE == "bedrock" and not BEDROCK_KB_ID:
    raise ValueError("KB_SOURCE=bedrock requires BEDROCK_KB_ID in .env — see the README")

# Wikipedia path — Wikimedia asks anonymous callers to send a descriptive User-Agent.
WIKI_SEARCH_URL = "https://api.wikimedia.org/core/v1/wikipedia/en/search/page"
WIKI_EXTRACT_URL = "https://en.wikipedia.org/w/api.php"
WIKI_USER_AGENT = os.getenv(
    "WIKI_USER_AGENT", "synthesia-ia-rag-quickstart/0.1 (interactive-avatar demo)"
)

# Small, fast model used to rewrite a spoken utterance into a clean search query
# before hitting Wikipedia's keyword search (see _rewrite_query).
QUERY_REWRITE_MODEL = os.getenv("QUERY_REWRITE_MODEL", "gpt-4o-mini")

# How many chars of the TOP hit's full article to pull for depth (answers often sit
# below the lead section). The extracts API only returns full text for one page per
# call, so we go deep on the best match and keep lead sections for the rest.
WIKI_TOP_CHARS = int(os.getenv("WIKI_TOP_CHARS", "4000"))

# Fallback query cleaner (used only if the LLM rewrite fails). Wikipedia search is
# keyword-based, so conversational filler pollutes it — "tell me about Latvia" matches
# the song "Santa Tell Me", not the country. This strips common spoken lead-ins so the
# SUBJECT drives the search; the LLM rewrite handles the general case more robustly.
_QUERY_PREFIXES = sorted(
    {
        "hey", "hi", "so", "well", "okay", "ok", "um", "uh", "please",
        "what can you tell me about", "can you tell me about", "could you tell me about",
        "tell me more about", "tell me about", "tell me", "what do you know about",
        "do you know anything about", "do you know about", "i'd like to know about",
        "i would like to know about", "i want to know about", "give me information about",
        "give me info about", "what's", "what is", "what are", "who's", "who is",
        "who are", "explain", "describe", "about",
    },
    key=len,
    reverse=True,  # match the longest lead-in first
)


def clean_query(text: str) -> str:
    """Strip conversational lead-ins so keyword search sees the subject, not the filler."""
    q = text.strip().strip("?.!,").lower()
    changed = True
    while changed and q:
        changed = False
        for prefix in _QUERY_PREFIXES:
            if q.startswith(prefix + " "):
                q = q[len(prefix) + 1:].strip(" ,?.!")
                changed = True
                break
    return q or text.strip()

# Who the avatar is. Substance always comes from the KB; this only sets manner.
PERSONA_NAME = os.getenv("PERSONA_NAME", "Aristotle" if KB_SOURCE == "bedrock" else "Kenji")

# Pipeline-status pill: the agent publishes its current stage on this LiveKit data
# topic; the browser (index.html) renders it, exposing the visible tool chain.
STATUS_TOPIC = "pipeline-status"


# Keep strong references to in-flight publish tasks. asyncio only holds a WEAK
# reference to tasks, so an unreferenced create_task() can be garbage-collected
# before its publish_data I/O completes — silently dropping the packet.
_status_tasks: set = set()


def emit_status(room, stage: str, label: str) -> None:
    """Fire-and-forget publish of the current pipeline stage to the browser."""
    payload = json.dumps({"stage": stage, "label": label}).encode()
    task = asyncio.create_task(
        room.local_participant.publish_data(payload, reliable=True, topic=STATUS_TOPIC)
    )
    _status_tasks.add(task)
    task.add_done_callback(_status_tasks.discard)

_SPOKEN = (
    "This is a spoken conversation: your replies are spoken aloud, so never refer to "
    "typing, reading, chat, bullet points, markdown, or URLs. Answer in one to three "
    "short sentences."
)
_GROUNDING = (
    "You are GROUNDED in a knowledge base. Before each of your turns you receive a "
    'system message with "Knowledge base context" retrieved for the user\'s question. '
    "Draw all facts ONLY from that retrieved context — never add facts from your own "
    "training knowledge. If told nothing relevant was found, say that it isn't in your "
    "knowledge base (your source material) and invite another question — frame it as the "
    "information being outside your sources, not as a failure or an inability to answer."
)


def build_instructions() -> str:
    if KB_SOURCE == "bedrock":
        # First-person embodiment grounded in a private corpus (the Aristotle pattern):
        # apply documented principles, but never invent facts about things not retrieved.
        return (
            f"You are {PERSONA_NAME}, speaking in the first person and rendered as a "
            f"real-time Synthesia avatar. {_SPOKEN}\n\n{_GROUNDING} You MAY apply the "
            "documented principles in that context to new questions, but you must NOT "
            "invent specific facts, opinions, or judgements about particular people, "
            "events, or things not in the retrieved context — even in character, even "
            "if it would feel natural or flattering."
        )
    # Generic grounded assistant over the public encyclopedia (the cold-start default).
    return (
        f"You are {PERSONA_NAME}, a friendly assistant rendered as a real-time "
        f"Synthesia avatar. {_SPOKEN}\n\n{_GROUNDING}"
    )


INSTRUCTIONS = build_instructions()


class GroundedAgent(Agent):
    """An agent whose every turn is grounded in KB retrieval (Wikipedia or Bedrock)."""

    def __init__(self, room) -> None:
        super().__init__(instructions=INSTRUCTIONS)
        self._room = room
        self._kb = None
        self._http = None
        self._oai = None
        if KB_SOURCE == "bedrock":
            import boto3  # lazy: only the bedrock path needs it (pip install boto3)
            from botocore.config import Config

            self._kb = boto3.client(
                "bedrock-agent-runtime",
                region_name=AWS_REGION,
                # Bound retrieval like the httpx path — boto3's 60s defaults would
                # freeze the turn on a hung retrieve.
                config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
            )
        else:
            self._http = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": WIKI_USER_AGENT})
            from openai import AsyncOpenAI  # already pulled in by the LiveKit openai plugin

            self._oai = AsyncOpenAI()  # reads OPENAI_API_KEY from env; used for query rewriting

    async def on_exit(self) -> None:
        if self._http is not None:
            await self._http.aclose()
        if self._oai is not None:
            await self._oai.close()

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        # The documented RAG hook: runs after the user's turn is transcribed and
        # before the LLM generates, so injected context reaches this exact reply.
        query = new_message.text_content
        if not query:
            return

        # Retrieval happens inside the turn, before generation — drive the pill from
        # here so the chain reads Querying KB… → Thinking… in the right order.
        emit_status(self._room, "kb", "Querying knowledge base…")
        passages = await self._retrieve(query)
        emit_status(self._room, "thinking", "Thinking…")
        if passages:
            turn_ctx.add_message(
                role="system",
                content=(
                    "Knowledge base context — answer using ONLY the passages below. "
                    "Do not add facts or opinions from outside them.\n\n" + passages
                ),
            )
            # Log only the retrieval signal, not the user's words (avoid logging PII).
            logger.info("[KB] injected %d passages", passages.count("\n\n---\n\n") + 1)
        else:
            turn_ctx.add_message(
                role="system",
                content=(
                    "No relevant passages were retrieved for this question. Tell the user "
                    "that this isn't in your knowledge base (your source material) and "
                    "invite another question — frame it as the information being outside "
                    "your sources, not as a failure. Do NOT answer from general knowledge."
                ),
            )
            logger.info("[KB] no relevant passages retrieved")

    async def _retrieve(self, query: str) -> str:
        if KB_SOURCE == "bedrock":
            return await asyncio.to_thread(self._retrieve_bedrock, query)
        return await self._retrieve_wikipedia(query)

    def _retrieve_bedrock(self, query: str) -> str:
        """Query the Bedrock managed KB; return the top-N passages as one string."""
        try:
            resp = self._kb.retrieve(
                knowledgeBaseId=BEDROCK_KB_ID,
                retrievalQuery={"text": query},
                # A MANAGED knowledge base requires managedSearchConfiguration. Passing
                # a vectorSearchConfiguration here is silently rejected and retrieval
                # returns nothing — the classic managed-KB RAG trap.
                retrievalConfiguration={"managedSearchConfiguration": {}},
            )
        except Exception:  # noqa: BLE001 — never let a retrieval hiccup crash the turn
            logger.exception("[KB] bedrock retrieve failed (check AWS creds / region / KB id)")
            return ""

        results = resp.get("retrievalResults", [])[:RETRIEVAL_TOP_N]
        passages = [r["content"]["text"] for r in results if r.get("content", {}).get("text")]
        return "\n\n---\n\n".join(passages)

    async def _rewrite_query(self, utterance: str) -> str:
        """Turn a spoken utterance into a clean Wikipedia search query.

        Keyword search is easily thrown by conversational phrasing ("tell me about
        Latvia" surfaces the song "Santa Tell Me"), so we let a small, fast model
        extract the subject. Falls back to the heuristic cleaner if the call fails.
        """
        try:
            resp = await self._oai.chat.completions.create(
                model=QUERY_REWRITE_MODEL,
                temperature=0,
                max_tokens=32,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Rewrite the user's spoken message as a concise Wikipedia search "
                            "query for the subject they're asking about. Return ONLY the query "
                            "— key nouns / proper nouns, no filler, no punctuation, no quotes. "
                            "If there is no clear subject, return the message unchanged."
                        ),
                    },
                    {"role": "user", "content": utterance},
                ],
            )
            rewritten = (resp.choices[0].message.content or "").strip().strip('"').strip()
            return rewritten or clean_query(utterance)
        except Exception:  # noqa: BLE001 — degrade to the heuristic, never crash the turn
            logger.warning("[KB] query rewrite failed; using heuristic fallback")
            return clean_query(utterance)

    async def _retrieve_wikipedia(self, query: str) -> str:
        """Search Wikipedia; return the top hit's full article + lead sections of the rest."""
        try:
            # 1) most relevant article titles — the search API already ranks these best-first.
            #    Rewrite the spoken utterance into a clean search query first, so the subject
            #    (not conversational filler) drives keyword search.
            search_q = await self._rewrite_query(query)
            r = await self._http.get(WIKI_SEARCH_URL, params={"q": search_q, "limit": RETRIEVAL_TOP_N})
            r.raise_for_status()
            titles = [p["title"] for p in r.json().get("pages", [])]
            if not titles:
                return ""
            # 2) Two bounded requests (2 is well under the burst that triggers 429s):
            #    - lead sections of ALL hits (breadth); the extracts API only returns
            #      multiple pages when exintro is set.
            #    - the FULL text of the TOP hit (depth — answers often sit below the lead).
            leads_req = self._http.get(WIKI_EXTRACT_URL, params={
                "action": "query", "format": "json", "prop": "extracts",
                "explaintext": 1, "exintro": 1, "redirects": 1, "titles": "|".join(titles),
            })
            top_req = self._http.get(WIKI_EXTRACT_URL, params={
                "action": "query", "format": "json", "prop": "extracts",
                "explaintext": 1, "redirects": 1, "titles": titles[0],
            })
            r_leads, r_top = await asyncio.gather(leads_req, top_req)
            r_leads.raise_for_status()
            r_top.raise_for_status()

            # The extracts API returns pages in arbitrary order, so re-sort to search rank —
            # resolving title normalisation + redirects so the lookup lines up (LLMs weight
            # position, so keeping the top hit first matters).
            data = r_leads.json().get("query", {})
            alias = {m["from"]: m["to"] for m in data.get("normalized", [])}
            alias.update({m["from"]: m["to"] for m in data.get("redirects", [])})
            by_title = {p["title"]: p.get("extract", "").strip() for p in data.get("pages", {}).values()}

            def resolve(t: str) -> str:  # search title -> normalised -> redirect target
                seen: set = set()
                while t in alias and t not in seen:
                    seen.add(t)
                    t = alias[t]
                return t

            passages = [by_title.get(resolve(t), "") for t in titles]

            # Swap the top hit's lead for its fuller (truncated) article text.
            top_pages = r_top.json().get("query", {}).get("pages", {})
            top_full = next(
                (p.get("extract", "").strip() for p in top_pages.values() if p.get("extract", "").strip()),
                "",
            )
            if top_full:
                passages[0] = top_full[:WIKI_TOP_CHARS]

            return "\n\n---\n\n".join(p for p in passages if p)
        except Exception:  # noqa: BLE001 — never let a retrieval hiccup crash the turn
            logger.exception("[KB] wikipedia retrieve failed")
            return ""


def prewarm(proc) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    logger.info("grounding source: %s | persona: %s", KB_SOURCE, PERSONA_NAME)

    # AgentSession owns the per-turn pipeline: STT → turn detection → the awaited
    # GroundedAgent.on_user_turn_completed hook (where KB retrieval injects) → LLM → TTS.
    # The sequence lives in livekit-agents, not here; see "How a turn flows" in the README.
    session = AgentSession(
        stt=inference.STT(model="cartesia/ink-2"),
        # GPT-4o as a lightweight streaming brain. It only ever sees the retrieved
        # KB context we inject each turn — swap the model freely; the RAG hook is
        # independent of the provider.
        llm=openai.LLM(model="gpt-4o"),
        tts=inference.TTS(model="cartesia/sonic-3.6", voice=CARTESIA_VOICE_ID),
        vad=ctx.proc.userdata["vad"],
        turn_handling={
            "turn_detection": "stt",
            # Preemptive generation races the per-turn retrieval and can start the
            # reply before KB context is injected — producing ungrounded answers.
            # Grounding requires it OFF.
            "preemptive_generation": {"enabled": False},
            "interruption": {"min_duration": 0.75, "min_words": 1, "false_interruption_timeout": 2.0},
        },
    )

    @session.on("metrics_collected")
    def on_metrics(ev) -> None:
        metrics.log_metrics(ev.metrics)

    # Speaking/Listening come from the session's own state; the KB + Thinking
    # stages are emitted from the retrieval hook (they occur within one turn).
    @session.on("agent_state_changed")
    def on_agent_state(ev) -> None:
        if ev.new_state == "speaking":
            emit_status(ctx.room, "speaking", "Speaking…")
        elif ev.new_state == "listening":
            emit_status(ctx.room, "listening", "Listening…")

    # Must attach before session.start().
    avatar = synthesia.AvatarSession(
        synthesia.AvatarConfig(avatar_ids=[AVATAR_ID]),
        join_timeout=60.0,
    )
    # The avatar cold-start can take several seconds — show it in the pill.
    emit_status(ctx.room, "loading", "Loading avatar…")
    # Retry only errors the plugin marks retryable (timeout, connection, rate limit).
    for attempt in range(3):
        try:
            await avatar.start(session, room=ctx.room)
            break
        except synthesia.SynthesiaError as e:
            if not e.retryable or attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))

    await session.start(agent=GroundedAgent(room=ctx.room), room=ctx.room)

    # No AI disclosure by default — this is a quickstart; how (and whether) to declare
    # the avatar is AI is left to the implementer. Add one here for real deployments,
    # especially real-person likenesses.
    invite = "invite a question about your works" if KB_SOURCE == "bedrock" else "invite the user to ask about any topic"
    session.generate_reply(
        instructions=(
            f"In character as {PERSONA_NAME}, greet the user in one short sentence "
            f"and {invite}."
        ),
        allow_interruptions=False,
    )


if __name__ == "__main__":
    # agent_name makes dispatch explicit: the worker only joins rooms whose token
    # requests it (server.py's RoomAgentDispatch) instead of every room in the project.
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="avatar-quickstart-rag",
        )
    )
