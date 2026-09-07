"""Speculative ("preflight") generation for a text-only Realtime LLM, standalone edition.

Cartesia Ink-2 STT has built-in turn detection and emits ``PREFLIGHT_TRANSCRIPT`` (eager
end-of-turn) events before the final transcript is committed. LiveKit's built-in preemptive
generation reacts to those events, but only for cascaded ``llm.LLM`` backends -- an OpenAI
``RealtimeModel`` used in text-only mode never enters that path. This module monkeypatches
``AgentActivity`` so it does:

* On ``PREFLIGHT_TRANSCRIPT`` (via ``on_preemptive_generation``): start the Realtime reply
  immediately on the realtime socket -- this is the head start.
* On the final transcript (via ``on_end_of_turn``): if it still matches what we speculated on,
  adopt the in-flight generation by driving LiveKit's native realtime pipeline (TTS + playout +
  interruption support); otherwise interrupt it, restore the chat context, and fall back to
  LiveKit's normal turn handling.

Safety comes from staying tool-less: ``should_speculate`` refuses to speculate unless the
active agent has no tools -- a speculative reply can be rolled back, a tool side effect cannot.

Safety properties:

* Every wrapped method is guarded: any unexpected error abandons speculation and falls back to
  LiveKit's original behaviour rather than breaking the session.
* The realtime socket allows a single active response, so we never speculate while one is in
  flight and always wait for a cancelled response to clear before regenerating.
* LiveKit mirrors server-acked conversation items into the local history
  (``_on_remote_item_added``); the speculation's items are held back from that mirror until the
  turn resolves (replayed on adoption, dropped on rollback) so adopted turns don't record a
  duplicate user message and rollbacks don't leave phantom user/empty-assistant entries.
* Adopted turns preserve LiveKit's end-of-turn bookkeeping and run it in stock order: the
  ``on_user_turn_completed`` hook runs exactly once before adoption (``StopResponse`` or a hook
  error ignores the turn, like stock); if the hook rewrites the user message or edits the chat
  context, the speculation is invalidated and a fresh reply is generated from the post-hook
  state. EOU metrics are attached to the user message, and the realtime session's buffered input
  audio is cleared -- the turn's content entered as text, so leaving the audio buffered would
  leak it into a later turn's ``commit_audio()``.
* Rollbacks remove only speculation-owned server items (the pushed user message and the
  speculative response) instead of restoring a full snapshot, so server items added by others
  in the meantime survive.

Environment:

* ``REALTIME_PREFLIGHT_ENABLED`` -- set to ``false`` to disable speculation (default ``true``).
* ``REALTIME_PREFLIGHT_ADOPTION_TIMEOUT_S`` -- max seconds to wait after the final transcript for
  a matching in-flight speculative response before falling back to normal generation
  (default ``2.0``).

Requires a session with: a text-only ``RealtimeModel`` (``modalities=["text"]``,
``turn_detection=None``), an STT that emits preflight transcripts (Cartesia Ink-2), and
preemptive generation enabled (the LiveKit default). Written against livekit-agents 1.5.x
private internals -- re-verify on upgrade. This module becomes unnecessary once LiveKit
supports RealtimeModel preemptive generation natively (see upstream livekit/agents#6537).
"""

# pyright: reportMissingImports=false
# (this example is a nested uv project; its dependencies live in example/preflight/.venv)

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from livekit.agents import StopResponse
from livekit.agents import llm as llm_mod
from livekit.agents.voice.agent import ModelSettings
from livekit.agents.voice.events import SpeechCreatedEvent
from livekit.agents.voice.speech_handle import InputDetails, SpeechHandle

logger = logging.getLogger("realtime-preflight")

_INSTALLED_FLAG = "_synthesia_preflight_installed"
_SESSION_ATTR = "_synthesia_preflight"
_MIRROR_HOLD_ATTR = "_synthesia_preflight_mirror_hold"


# ---------------------------------------------------------------------------
# Adopt/rollback decision (pure logic, no LiveKit coupling)
# ---------------------------------------------------------------------------
def normalize_transcript(text: str) -> str:
    """Normalize a transcript for equality comparison.

    Lower-cases and strips punctuation entirely so an eager (preflight) transcript and the final
    transcript compare on words alone -- they routinely differ only by trailing/added punctuation.
    """
    without_punctuation = re.sub(r"[^\w\s]", " ", text.casefold())
    return re.sub(r"\s+", " ", without_punctuation).strip()


class PreflightState(Enum):
    SPECULATING = auto()
    ADOPTED = auto()
    ROLLED_BACK = auto()


class PreflightDecision(Enum):
    ADOPT = auto()
    ROLLBACK = auto()


class RealtimePreflightController:
    """Owns the state of a single speculative turn.

    The integration layer records what was speculated, then asks :meth:`decide` what to do when
    the final transcript arrives. Adoption is safe only while still speculating and the final
    transcript still matches the eager one.
    """

    def __init__(self, *, preflight_transcript: str) -> None:
        self.preflight_transcript = preflight_transcript
        self.state = PreflightState.SPECULATING

    def decide(self, final_transcript: str, *, context_unchanged: bool) -> PreflightDecision:
        """Decide whether the final turn can reuse the speculative response."""
        if self.state is not PreflightState.SPECULATING:
            return PreflightDecision.ROLLBACK
        if not context_unchanged:
            return PreflightDecision.ROLLBACK
        if normalize_transcript(final_transcript) != normalize_transcript(self.preflight_transcript):
            return PreflightDecision.ROLLBACK
        return PreflightDecision.ADOPT

    def mark_adopted(self) -> None:
        self.state = PreflightState.ADOPTED

    def rollback(self) -> None:
        self.state = PreflightState.ROLLED_BACK


# ---------------------------------------------------------------------------
# Timing instrumentation
# ---------------------------------------------------------------------------
def _now_ms() -> float:
    return time.perf_counter() * 1000


def _mark(session: "_PreflightSession", name: str) -> None:
    session.extra[f"{name}_at_ms"] = _now_ms()


def _elapsed(session: "_PreflightSession", start: str, end: str | None = None) -> float | None:
    start_at = session.extra.get(f"{start}_at_ms")
    end_at = _now_ms() if end is None else session.extra.get(f"{end}_at_ms")
    if start_at is None or end_at is None:
        return None
    return round(end_at - start_at, 1)


def _timing_extra(session: "_PreflightSession") -> dict[str, float | None]:
    return {
        "preflight_age_ms": _elapsed(session, "preflight_started"),
        "preflight_to_final_ms": _elapsed(session, "preflight_started", "final_transcript"),
    }


@dataclass
class _PreflightSession:
    """Per-turn speculation state attached to an ``AgentActivity`` instance."""

    controller: RealtimePreflightController
    rt_session: Any
    base_chat_ctx: Any
    task: asyncio.Task | None = None
    user_message: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _MirrorHold:
    """Remote items created by the in-flight speculation, held back from the local history.

    LiveKit mirrors server-acked conversation items into ``agent._chat_ctx`` as placeholders
    (``AgentActivity._on_remote_item_added``). The speculative user message and the speculative
    response are deliberately absent from the local history until adoption, so the mirror would
    append them mid-turn: a duplicate user message on adopted turns (the adopt path emits its
    own) and phantom user/empty-assistant items left behind on rollbacks. Held items are
    replayed on adoption and dropped on rollback (the context restore deletes them server-side).
    """

    user_item_id: str | None
    items: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Feature flag + eligibility (pure, testable)
# ---------------------------------------------------------------------------
def realtime_preflight_enabled() -> bool:
    return os.environ.get("REALTIME_PREFLIGHT_ENABLED", "true").strip().lower() not in ("false", "0", "no")


def realtime_preflight_adoption_timeout_seconds() -> float:
    """Post-final timeout for a matching in-flight speculative response."""
    try:
        return float(os.environ.get("REALTIME_PREFLIGHT_ADOPTION_TIMEOUT_S", "2.0"))
    except ValueError:
        return 2.0


def is_text_mode_realtime(llm_obj: Any) -> bool:
    """True for an OpenAI Realtime model used as a text-only LLM with no server turn detection."""
    if not isinstance(llm_obj, llm_mod.RealtimeModel):
        return False
    caps = llm_obj.capabilities
    return not getattr(caps, "audio_output", True) and not getattr(caps, "turn_detection", True)


def should_speculate(activity: Any) -> bool:
    """Mirror LiveKit's preemptive gating, but for the eligible text-mode Realtime case."""
    if not realtime_preflight_enabled():
        return False
    # One speculation per turn: if the user pauses again after resuming (a second eager
    # transcript), we keep the first speculation rather than superseding it. The final-transcript
    # comparison at end of turn adopts it only if it still matches, so correctness is preserved --
    # a longer turn just rolls back and takes the normal path (the head start is forfeited).
    if getattr(activity, _SESSION_ATTR, None) is not None:
        return False
    try:
        opts = activity._session.options.preemptive_generation
        if not opts["enabled"]:
            return False
        if activity._scheduling_paused or activity._new_turns_blocked:
            return False
        current = activity._current_speech
        if current is not None and not current.interrupted:
            return False
        if activity._rt_session is None:
            return False
        # A speculative reply must never be able to trigger side effects: it may be interrupted
        # and rolled back, but a tool call cannot be un-run. Only tool-less agents may speculate
        agent = getattr(activity, "_agent", None)
        if agent is None or agent.tools:
            return False
        # The realtime socket allows only one active response. If one is still in flight (e.g. the
        # opening greeting the user talked over, or the agent mid-reply on a barge-in), issuing a
        # speculative generate_reply would be rejected with conversation_already_has_active_response
        # AND poison the subsequent fallback. Skip speculation and let LiveKit's normal serialized
        # interrupt->generate handle the turn.
        if bool(getattr(activity._rt_session, "has_active_generation", False)):
            return False
        return is_text_mode_realtime(activity.llm)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Orchestration helpers (decoupled from AgentActivity, testable with fakes)
# ---------------------------------------------------------------------------
async def _await_realtime_response_cleared(rt_session: Any, timeout_s: float = 1.5) -> None:
    """Wait for an interrupted realtime response to actually clear before regenerating.

    The realtime socket allows a single active response, and ``interrupt()`` only *sends*
    ``response.cancel`` -- the response stays active until the server acks it. If we (or LiveKit's
    normal path) issue the next ``generate_reply`` before that, OpenAI rejects it with
    ``conversation_already_has_active_response``. Poll the session's ``has_active_generation``
    flag, bounded by ``timeout_s`` so we never hang the turn.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    deadline = loop.time() + timeout_s
    while bool(getattr(rt_session, "has_active_generation", False)):
        if loop.time() >= deadline:
            logger.debug("preflight rollback: timed out waiting for active realtime response to clear")
            return
        await asyncio.sleep(0.01)


async def rollback_preflight(
    controller: RealtimePreflightController,
    *,
    rt_session: Any,
    base_chat_ctx: Any,
    collect_spec_item_ids: Callable[[], set[str]] | None = None,
) -> None:
    """Cancel the speculative response and remove its items from the realtime context.

    Removal is targeted: only speculation-owned items (the pushed user message and the speculative
    response's items, tracked via the mirror hold) are deleted, so server items added by anything
    else since speculation started survive. ``base_chat_ctx`` is the full pre-speculation snapshot,
    kept as a fallback if targeted removal isn't possible.
    """
    try:
        rt_session.interrupt()
    except Exception:
        logger.debug("preflight rollback: rt_session.interrupt() failed", exc_info=True)

    # Ensure the cancelled speculative response has fully cleared before the caller regenerates,
    # otherwise the follow-up generate_reply collides on the single-active-response realtime socket.
    # By this point the speculation's server acks have normally arrived too, so the ids collected
    # below are complete.
    await _await_realtime_response_cleared(rt_session)

    restored = False
    if collect_spec_item_ids is not None:
        try:
            # re-collect each pass: an ack can land while update_chat_ctx is in flight
            for _ in range(3):
                spec_ids = collect_spec_item_ids()
                restore_ctx = rt_session.chat_ctx.copy()
                kept_items = [item for item in restore_ctx.items if item.id not in spec_ids]
                if len(kept_items) == len(restore_ctx.items):
                    break
                restore_ctx.items = kept_items
                await rt_session.update_chat_ctx(restore_ctx)
            restored = True
        except Exception:
            logger.debug("preflight rollback: targeted removal failed; restoring snapshot", exc_info=True)
    if not restored:
        try:
            await rt_session.update_chat_ctx(base_chat_ctx)
        except Exception:
            logger.debug("preflight rollback: restoring chat ctx failed", exc_info=True)
    controller.rollback()


# ---------------------------------------------------------------------------
# AgentActivity binding
# ---------------------------------------------------------------------------
async def _run_preflight(session: _PreflightSession, spec_ctx: Any) -> None:
    """Start the speculative reply on the realtime socket and stash the pending generation.

    The agent is tool-less (see ``should_speculate``), so we fire a text-only ``generate_reply``
    -- this is the head start. We deliberately do NOT consume it here: on adopt we hand the
    generation to LiveKit's native ``_realtime_generation_task`` (TTS + playout + interruption);
    on rollback we interrupt it. A direct ``rt_session.generate_reply()`` is tagged
    ``user_initiated=True`` so LiveKit's auto-handler ignores it, which is why we drive the native
    task ourselves at adopt time.
    """
    try:
        await session.rt_session.update_chat_ctx(spec_ctx)
        session.extra["gen_fut"] = session.rt_session.generate_reply(tool_choice="none")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("speculative preflight generation failed; abandoning speculation")


def _start_preflight(activity: Any, info: Any) -> None:
    transcript = getattr(info, "new_transcript", "") or ""
    if not transcript.strip():
        return

    rt_session = activity._rt_session

    controller = RealtimePreflightController(preflight_transcript=transcript)

    base_chat_ctx = rt_session.chat_ctx.copy()
    spec_ctx = rt_session.chat_ctx.copy()
    spec_user_message = spec_ctx.add_message(role="user", content=transcript)

    session = _PreflightSession(
        controller=controller, rt_session=rt_session, base_chat_ctx=base_chat_ctx, user_message=spec_user_message
    )
    setattr(activity, _SESSION_ATTR, session)
    # hold back remote-item mirroring for the speculation's items until it resolves
    setattr(activity, _MIRROR_HOLD_ATTR, _MirrorHold(user_item_id=spec_user_message.id))
    _mark(session, "preflight_started")
    session.task = asyncio.create_task(_run_preflight(session, spec_ctx), name="realtime_preflight")
    logger.info(
        "Started realtime preflight speculation", extra={"preflight_transcript": transcript, **_timing_extra(session)}
    )


def _emit_user_message(activity: Any, message: Any) -> None:
    """Commit the user's turn message to the agent chat history and emit the conversation event."""
    try:
        activity._agent._chat_ctx._upsert_item(message)
    except Exception:
        logger.debug("preflight adopt: failed to upsert user message into chat ctx", exc_info=True)
    try:
        activity._session._conversation_item_added(message)
    except Exception:
        logger.debug("preflight adopt: failed to emit user conversation item", exc_info=True)


def _collect_spec_item_ids(activity: Any, session: _PreflightSession) -> set[str]:
    """Ids of every server item owned by the speculation.

    The pushed user message id is known up front; the speculative response's item ids are
    whatever the mirror hold has buffered (server acks for the speculation are the only items
    held back).
    """
    ids: set[str] = set()
    if session.user_message is not None:
        ids.add(session.user_message.id)
    hold: _MirrorHold | None = getattr(activity, _MIRROR_HOLD_ATTR, None)
    if hold is not None:
        ids.update(ev.item.id for ev in hold.items if getattr(ev.item, "id", None) is not None)
    return ids


def _release_mirror_hold(activity: Any, *, replay: bool) -> None:
    """Stop holding back remote-item mirroring; replay the held items on adoption.

    On rollback the held items are dropped: the chat-context restore removes them server-side,
    so mirroring them locally would leave phantom history entries.
    """
    hold: _MirrorHold | None = getattr(activity, _MIRROR_HOLD_ATTR, None)
    setattr(activity, _MIRROR_HOLD_ATTR, None)
    if hold is None or not replay:
        return
    for ev in hold.items:
        try:
            activity._on_remote_item_added(ev)  # hold released: applies normally
        except Exception:
            logger.debug("preflight: replaying held remote item failed", exc_info=True)


async def _await_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    if not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except Exception:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _clear_session(activity: Any) -> _PreflightSession | None:
    session = getattr(activity, _SESSION_ATTR, None)
    setattr(activity, _SESSION_ATTR, None)
    return session


def install_realtime_preflight_support() -> None:
    """Idempotently install the preflight wrappers onto ``AgentActivity``.

    Call once at worker startup (e.g. in the prewarm function), before any session is created.
    Wrappers are pure pass-throughs unless an eligible text-mode Realtime session speculates.
    """
    from livekit.agents.voice.agent_activity import AgentActivity

    if getattr(AgentActivity, _INSTALLED_FLAG, False):
        return

    original_on_preemptive = AgentActivity.on_preemptive_generation
    original_on_end_of_turn = AgentActivity.on_end_of_turn
    original_cancel = AgentActivity._cancel_preemptive_generation
    original_on_remote_item_added = AgentActivity._on_remote_item_added

    def _on_remote_item_added(self: Any, ev: Any) -> None:
        hold: _MirrorHold | None = getattr(self, _MIRROR_HOLD_ATTR, None)
        if hold is not None:
            try:
                # hold back items created by the speculation: the pushed user message (known id)
                # and the speculative response's items (assistant-side). Items unrelated to the
                # speculation (e.g. other user items) apply normally.
                if getattr(ev.item, "role", None) != "user" or ev.item.id == hold.user_item_id:
                    hold.items.append(ev)
                    return
            except Exception:
                logger.debug("preflight: mirror hold check failed; applying item normally", exc_info=True)
        original_on_remote_item_added(self, ev)

    def on_preemptive_generation(self: Any, info: Any) -> None:
        try:
            if should_speculate(self):
                _start_preflight(self, info)
                return
        except Exception:
            logger.exception("realtime preflight kickoff failed; using default preemptive generation")
            _clear_session(self)
            _release_mirror_hold(self, replay=False)
        original_on_preemptive(self, info)

    def on_end_of_turn(self: Any, info: Any) -> bool:
        session: _PreflightSession | None = getattr(self, _SESSION_ATTR, None)
        if session is None:
            return original_on_end_of_turn(self, info)

        try:
            final_transcript = getattr(info, "new_transcript", "") or ""
            _mark(session, "final_transcript")
            decision = session.controller.decide(final_transcript, context_unchanged=True)
            if decision is PreflightDecision.ADOPT and not getattr(info, "skip_reply", False):
                # Adopt the in-flight speculative generation by driving LiveKit's native realtime
                # pipeline (TTS + playout). Falls back to a fresh generation if the speculative
                # response never materialises, so we can never go silent.
                self._finalize_preflight_turn(session, info, final_transcript)
                _clear_session(self)
                return True
            logger.info(
                "Rolling back realtime preflight speculation",
                extra={
                    "preflight_transcript": session.controller.preflight_transcript,
                    "final_transcript": final_transcript,
                    "decision": str(decision),
                    **_timing_extra(session),
                },
            )
        except Exception:
            logger.exception("realtime preflight adoption failed; rolling back to normal turn")
        # rollback path: abandon speculation and let LiveKit handle the turn normally
        self._rollback_preflight_then_fallback_turn(_clear_session(self), info, original_on_end_of_turn)
        return True

    def _cancel_preemptive_generation(self: Any) -> None:
        session = _clear_session(self)
        if session is not None:
            self._rollback_preflight_turn(session)
        original_cancel(self)

    def _finalize_preflight_turn(self: Any, session: _PreflightSession, info: Any, final_transcript: str) -> None:
        """Adopt the speculative turn by driving LiveKit's native realtime pipeline.

        Mirrors stock ``_user_turn_completed_task`` order: the ``on_user_turn_completed`` hook
        runs first (``StopResponse`` or a hook error ignores the turn, exactly like stock), then
        the speculation is validated against any edits the hook made (a rewritten user message or
        an edited chat context invalidates it, and a fresh reply is generated from the post-hook
        state without re-running the hook). Only then is the in-flight speculative generation
        handed to ``_realtime_generation_task`` (TTS + playout + interruption). If the speculative
        response never materialises we regenerate, so we never go silent.
        """

        async def _undo_speculation() -> None:
            await rollback_preflight(
                session.controller,
                rt_session=session.rt_session,
                base_chat_ctx=session.base_chat_ctx,
                collect_spec_item_ids=lambda: _collect_spec_item_ids(self, session),
            )
            _release_mirror_hold(self, replay=False)

        async def _do_finalize() -> None:
            timeout_s = realtime_preflight_adoption_timeout_seconds()

            # -- stock end-of-turn bookkeeping (mirrors _user_turn_completed_task) --
            self._preemptive_generation_count = 0
            try:
                await asyncio.gather(*self._interrupt_background_speeches(force=False))
            except Exception:
                logger.debug("preflight adopt: interrupting background speeches failed", exc_info=True)

            user_message = llm_mod.ChatMessage(
                role="user",
                content=[final_transcript],
                transcript_confidence=getattr(info, "transcript_confidence", None),
            )
            if session.user_message is not None:
                # reuse the speculative message's id: the server-side item and any mirrored
                # placeholder share it, so the upsert updates in place instead of duplicating
                user_message.id = session.user_message.id
            metrics_report = None
            try:
                metrics_report = self._init_metrics_from_end_of_turn(info)
                user_message.metrics = metrics_report
            except Exception:
                logger.debug("preflight adopt: attaching EOU metrics failed", exc_info=True)

            # -- the turn-completed hook, stock semantics: StopResponse or any error ignores the
            # turn entirely (no history record, no reply); we additionally undo the speculation --
            ctx_before_hook = self._agent.chat_ctx.copy()
            temp_mutable_chat_ctx = self._agent.chat_ctx.copy()
            hook_started_at = time.perf_counter()
            turn_ignored = False
            try:
                await self._agent.on_user_turn_completed(temp_mutable_chat_ctx, new_message=user_message)
            except StopResponse:
                turn_ignored = True
            except Exception:
                logger.exception("error occurred during on_user_turn_completed")
                turn_ignored = True
            if metrics_report is not None:
                try:
                    metrics_report["on_user_turn_completed_delay"] = time.perf_counter() - hook_started_at
                except Exception:
                    logger.debug("preflight adopt: recording hook delay failed", exc_info=True)

            if turn_ignored:
                await _undo_speculation()
                logger.info(
                    "Realtime preflight: turn ignored by on_user_turn_completed (StopResponse or error)",
                    extra={"final_transcript": final_transcript, **_timing_extra(session)},
                )
                return

            # -- invalidation, mirroring stock's post-hook preemptive check: if the hook rewrote
            # the user message or edited the chat context, the speculative reply (generated
            # against the pre-hook state) no longer applies; regenerate from the post-hook state
            # without re-running the hook --
            try:
                hook_edited = user_message.text_content != final_transcript or not ctx_before_hook.is_equivalent(
                    temp_mutable_chat_ctx
                )
            except Exception:
                logger.debug("preflight adopt: hook-edit comparison failed; invalidating", exc_info=True)
                hook_edited = True

            if hook_edited:
                logger.info(
                    "Realtime preflight: speculation invalidated by on_user_turn_completed edits, regenerating",
                    extra={
                        "preflight_transcript": session.controller.preflight_transcript,
                        "final_transcript": final_transcript,
                        **_timing_extra(session),
                    },
                )
                await _undo_speculation()
                self._generate_reply(
                    user_message=user_message,
                    chat_ctx=temp_mutable_chat_ctx,
                    input_details=InputDetails(modality="audio"),
                )
                return

            # -- adopt: wait (briefly) for the speculative generation to be created, then hand it
            # to LiveKit's native realtime pipeline --
            gen_fut = session.extra.get("gen_fut")
            generation_ev = None
            if gen_fut is not None:
                try:
                    generation_ev = await asyncio.wait_for(asyncio.shield(gen_fut), timeout=timeout_s)
                except Exception:
                    logger.debug("preflight: speculative generation not ready in time", exc_info=True)

            if generation_ev is not None:
                # Commit the user turn to history, then drive the native generation.
                _emit_user_message(self, user_message)
                # release the mirroring hold and replay the speculation's items (dedup by id)
                _release_mirror_hold(self, replay=True)
                # The turn's content entered the conversation as text; drop the mic audio the
                # realtime session buffered for it so a later turn's commit_audio() can't leak it.
                try:
                    session.rt_session.clear_audio()
                except Exception:
                    logger.debug("preflight adopt: clear_audio failed", exc_info=True)
                handle = SpeechHandle.create(
                    allow_interruptions=self.allow_interruptions, input_details=InputDetails(modality="audio")
                )
                self._session.emit(
                    "speech_created",
                    SpeechCreatedEvent(speech_handle=handle, user_initiated=False, source="generate_reply"),
                )
                self._create_speech_task(
                    self._realtime_generation_task(
                        speech_handle=handle, generation_ev=generation_ev, model_settings=ModelSettings()
                    ),
                    speech_handle=handle,
                    name="realtime_preflight_generation",
                )
                self._schedule_speech(handle, SpeechHandle.SPEECH_PRIORITY_NORMAL)
                session.controller.mark_adopted()
                logger.info(
                    "Adopting realtime preflight speculation (native generation)",
                    extra={
                        "preflight_transcript": session.controller.preflight_transcript,
                        "final_transcript": final_transcript,
                        "timeout_seconds": timeout_s,
                        **_timing_extra(session),
                    },
                )
                return

            # The hook already ran, so the stock fallback (which would run it again) is off the
            # table; regenerate directly from the post-hook state instead.
            logger.info(
                "Realtime preflight: speculation unavailable, regenerating",
                extra={
                    "preflight_transcript": session.controller.preflight_transcript,
                    "final_transcript": final_transcript,
                    "timeout_seconds": timeout_s,
                    **_timing_extra(session),
                },
            )
            await _undo_speculation()
            self._generate_reply(
                user_message=user_message, chat_ctx=temp_mutable_chat_ctx, input_details=InputDetails(modality="audio")
            )

        self._create_speech_task(_do_finalize(), name="realtime_preflight_finalize")

    def _rollback_preflight_turn(self: Any, session: _PreflightSession | None) -> None:
        if session is None:
            return

        async def _do_rollback() -> None:
            if session.task is not None and not session.task.done():
                session.task.cancel()
                await _await_task(session.task)
            await rollback_preflight(
                session.controller,
                rt_session=session.rt_session,
                base_chat_ctx=session.base_chat_ctx,
                collect_spec_item_ids=lambda: _collect_spec_item_ids(self, session),
            )
            _release_mirror_hold(self, replay=False)

        self._create_speech_task(_do_rollback(), name="realtime_preflight_rollback")

    def _rollback_preflight_then_fallback_turn(
        self: Any, session: _PreflightSession | None, info: Any, fallback_on_end_of_turn: Callable[[Any, Any], bool]
    ) -> None:
        if session is None:
            fallback_on_end_of_turn(self, info)
            return

        async def _do_rollback_then_fallback() -> None:
            if session.task is not None and not session.task.done():
                session.task.cancel()
                await _await_task(session.task)
            await rollback_preflight(
                session.controller,
                rt_session=session.rt_session,
                base_chat_ctx=session.base_chat_ctx,
                collect_spec_item_ids=lambda: _collect_spec_item_ids(self, session),
            )
            _release_mirror_hold(self, replay=False)
            fallback_on_end_of_turn(self, info)

        self._create_speech_task(_do_rollback_then_fallback(), name="realtime_preflight_rollback_then_fallback")

    AgentActivity.on_preemptive_generation = on_preemptive_generation
    AgentActivity.on_end_of_turn = on_end_of_turn
    AgentActivity._cancel_preemptive_generation = _cancel_preemptive_generation
    AgentActivity._on_remote_item_added = _on_remote_item_added
    AgentActivity._finalize_preflight_turn = _finalize_preflight_turn  # type: ignore[attr-defined]
    AgentActivity._rollback_preflight_turn = _rollback_preflight_turn  # type: ignore[attr-defined]
    AgentActivity._rollback_preflight_then_fallback_turn = _rollback_preflight_then_fallback_turn  # type: ignore[attr-defined]
    setattr(AgentActivity, _INSTALLED_FLAG, True)
    logger.info("Installed realtime preflight support on AgentActivity")
