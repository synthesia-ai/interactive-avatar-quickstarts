"""Regression tests for the preflight active-response race.

The final transcript can arrive while speculative ``update_chat_ctx`` is still in flight, so
``gen_fut`` does not exist yet. Regenerating at that moment used to leave the preflight task
free to send its own ``response.create``, and OpenAI then rejected the fallback.
"""

from __future__ import annotations

import asyncio
import unittest

from realtime_preflight import (
    RealtimePreflightController,
    _PreflightSession,
    _await_speculative_generation,
    _await_task,
    _cancel_preflight_task,
    _is_active_response_error,
    _regenerate_after_abandoned_preflight,
    _reply_rejected_for_active_response,
)


def _session() -> _PreflightSession:
    return _PreflightSession(
        controller=RealtimePreflightController(preflight_transcript="how many days are there in a week?"),
        rt_session=None,
        base_chat_ctx=None,
    )


class _ReplyHandle:
    def __init__(self, *, done: bool, error: BaseException | None = None) -> None:
        self._done = done
        self._error = error

    def done(self) -> bool:
        return self._done

    def exception(self) -> BaseException | None:
        if not self._done:
            raise asyncio.InvalidStateError("not done")
        return self._error


class _ActiveResponse(Exception):
    def __init__(self) -> None:
        super().__init__("Conversation already has an active response")
        self.code = "conversation_already_has_active_response"


class _RtSession:
    def __init__(self, *, active: bool) -> None:
        self.has_active_generation = active
        self.interrupts = 0

    def interrupt(self) -> None:
        self.interrupts += 1
        self.has_active_generation = False


class PreflightActiveResponseTest(unittest.IsolatedAsyncioTestCase):
    async def test_early_final_waits_for_the_speculative_response(self) -> None:
        session = _session()
        started = asyncio.Event()

        async def preflight() -> None:
            started.set()
            await asyncio.sleep(0.05)  # update_chat_ctx still in flight; gen_fut is unset
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            session.extra["gen_fut"] = fut
            fut.set_result("generation")

        session.task = asyncio.create_task(preflight())
        await started.wait()
        self.assertIsNone(session.extra.get("gen_fut"))

        generation = await _await_speculative_generation(session, timeout_s=1.0)

        self.assertEqual(generation, "generation")

    async def test_cancel_stops_a_late_response_create(self) -> None:
        session = _session()
        created: list[str] = []
        release = asyncio.Event()

        async def preflight() -> None:
            await release.wait()
            created.append("response.create")
            session.extra["gen_fut"] = asyncio.get_running_loop().create_future()

        session.task = asyncio.create_task(preflight())
        await asyncio.sleep(0)  # block inside the stand-in for update_chat_ctx

        await _cancel_preflight_task(session)
        release.set()
        await asyncio.sleep(0)

        self.assertEqual(created, [])
        self.assertIsNone(session.extra.get("gen_fut"))

    async def test_failed_speculative_future_is_abandoned(self) -> None:
        session = _session()
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        fut.set_exception(_ActiveResponse())
        session.extra["gen_fut"] = fut

        generation = await _await_speculative_generation(session, timeout_s=1.0)

        self.assertIsNone(generation)

    async def test_rejected_reply_is_retried_once_after_the_response_clears(self) -> None:
        rt = _RtSession(active=True)
        calls: list[bool] = []

        def start_reply(is_retry: bool) -> _ReplyHandle:
            calls.append(is_retry)
            if is_retry:
                return _ReplyHandle(done=False)
            return _ReplyHandle(done=True, error=_ActiveResponse())

        await _regenerate_after_abandoned_preflight(
            rt,
            start_reply,
            rejection_timeout_s=0.2,
            settle_timeout_s=0.5,
        )

        self.assertEqual(calls, [False, True])
        self.assertEqual(rt.interrupts, 1)
        self.assertFalse(rt.has_active_generation)

    async def test_accepted_reply_is_not_retried(self) -> None:
        rt = _RtSession(active=False)
        calls: list[bool] = []

        def start_reply(is_retry: bool) -> _ReplyHandle:
            calls.append(is_retry)
            return _ReplyHandle(done=False)

        await _regenerate_after_abandoned_preflight(
            rt,
            start_reply,
            rejection_timeout_s=0.05,
            settle_timeout_s=0.05,
        )

        self.assertEqual(calls, [False])
        self.assertEqual(rt.interrupts, 0)

    async def test_child_cancellation_does_not_abort_the_caller(self) -> None:
        async def sleeper() -> None:
            await asyncio.sleep(30)

        task = asyncio.create_task(sleeper())
        await asyncio.sleep(0)
        task.cancel()

        await _await_task(task)

        self.assertTrue(task.cancelled())

    async def test_caller_cancellation_still_propagates(self) -> None:
        async def sleeper() -> None:
            await asyncio.sleep(30)

        task = asyncio.create_task(sleeper())

        async def waiter() -> None:
            await _await_task(task)

        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)
        waiter_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await waiter_task

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def test_active_response_error_matches_provider_code_or_message(self) -> None:
        self.assertTrue(_is_active_response_error(_ActiveResponse()))
        self.assertTrue(_is_active_response_error(RuntimeError("conversation_already_has_active_response")))
        self.assertFalse(_is_active_response_error(RuntimeError("generate_reply timed out.")))
        self.assertFalse(_is_active_response_error(None))

    async def test_open_reply_is_not_an_active_response_rejection(self) -> None:
        rejected = await _reply_rejected_for_active_response(_ReplyHandle(done=False), timeout_s=0.05)
        self.assertFalse(rejected)


if __name__ == "__main__":
    unittest.main()
