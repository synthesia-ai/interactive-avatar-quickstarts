# Synthesia Interactive Avatar — Quickstarts

Runnable recipes for [Synthesia Interactive Avatars](https://www.synthesia.io/features/avatars/interactive-avatars): photoreal, lip-synced avatars you can talk to in your browser.

Each recipe is self-contained, with its own README, dependencies, and `.env.example`, so you can copy a directory out and build on it directly.

| Recipe | What it shows |
| --- | --- |
| [`minimal/`](minimal/) | The smallest useful app: a Python LiveKit voice agent (STT → LLM → TTS) with the avatar integration in three lines. Start here. |
| [`rag/`](rag/) | A LiveKit agent grounded in a knowledge base (public Wikipedia out of the box, AWS Bedrock for your own corpus). It speaks only from retrieved source material. |
| [`tools/`](tools/) | LLM uses tools to fill a booking form in the browser as you speak, and your edits flow back to the agent: tool calling plus UI state sync over LiveKit data channels. |

## Prerequisites

Every recipe needs a **Synthesia API key** for [Interactive Avatars](https://www.synthesia.io/features/avatars/interactive-avatars) and a [**LiveKit Cloud**](https://cloud.livekit.io) project. Each recipe's README lists everything else it needs: providers, keys, and how to run it.

## Optional noise reduction

All recipes support **Quail Voice Focus**, disabled by default. Enable it in noisy environments, such as open offices with background conversations, to help isolate your voice, improve transcription, and reduce unwanted interruptions.

Install the optional official LiveKit plugin and set `QUAIL_ENABLED=true`. Models are bundled, and authentication and voice-isolation billing use your existing LiveKit Cloud account. Setup and pricing details are documented in each recipe: [minimal](minimal/README.md#optional-quail-voice-focus), [RAG](rag/README.md#optional-quail-voice-focus), and [tools](tools/README.md#optional-quail-voice-focus).
