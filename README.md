# Synthesia Interactive Avatar — Quickstarts

Runnable recipes for [Synthesia Interactive Avatars](https://www.synthesia.io/features/avatars/interactive-avatars): photoreal, lip-synced avatars you can talk to in your browser.

Each recipe is self-contained — its own README, dependencies, and `.env.example` — so you can copy a directory out and build on it directly.

| Recipe | What it shows |
| --- | --- |
| [`minimal/`](minimal/) | The smallest useful app: a Python LiveKit voice agent (STT → LLM → TTS) with the avatar integration in three lines. Start here. |
| [`rag/`](rag/) | A LiveKit agent grounded in a knowledge base (public Wikipedia out of the box, AWS Bedrock for your own corpus) — it speaks only from retrieved source material. |
| [`tools/`](tools/) | An avatar that acts, not just talks: LLM function tools fill a booking form in the browser live, and your edits flow back — tool calling + UI state sync over LiveKit data channels. |

## Prerequisites

Every recipe needs a **Synthesia API key** for [Interactive Avatars](https://www.synthesia.io/features/avatars/interactive-avatars). Each recipe's README lists everything else it needs — providers, keys, and how to run it.
