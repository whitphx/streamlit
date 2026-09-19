# Copyright (c) Streamlit Inc. (2018-2022) Snowflake Inc. (2022-2026)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
import contextvars
import threading
import unittest
from typing import TYPE_CHECKING

import pytest

from streamlit.errors import NoSessionContext
from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
from streamlit.proto.PageProfile_pb2 import Command
from streamlit.runtime.forward_msg_cache import populate_hash_if_needed
from streamlit.runtime.fragment import MemoryFragmentStorage
from streamlit.runtime.memory_uploaded_file_manager import MemoryUploadedFileManager
from streamlit.runtime.pages_manager import PagesManager
from streamlit.runtime.parallel_coordinator import ParallelFragmentCoordinator
from streamlit.runtime.scriptrunner_utils.script_run_context import (
    SCRIPT_RUN_CONTEXT_ATTR_NAME,
    ScriptRunContext,
    ThreadState,
    add_script_run_ctx,
    enqueue_message,
    get_script_run_ctx,
)
from streamlit.runtime.scriptrunner_utils.script_run_context_attr import (
    script_run_ctx_var,
)
from streamlit.runtime.scriptrunner_utils.shared_run_state import SharedRunState
from streamlit.runtime.state import SafeSessionState, SessionState
from streamlit.testing.v1.util import patch_config_options
from tests.conftest import enable_mpa_v2_mode
from tests.streamlit.message_mocks import create_dataframe_msg

if TYPE_CHECKING:
    from collections.abc import Callable


def _make_command(name: str) -> Command:
    return Command(name=name)


def _create_script_run_context(
    fake_enqueue: Callable[[ForwardMsg], None],
    pages_manager: PagesManager | None = None,
    cached_message_hashes: frozenset[str] | None = None,
):
    return ScriptRunContext(
        session_id="TestSessionID",
        _enqueue=fake_enqueue,
        query_string="",
        session_state=SafeSessionState(SessionState(), lambda: None),
        uploaded_file_mgr=MemoryUploadedFileManager("/mock/upload"),
        main_script_path="",
        user_info={"email": "test@example.com"},
        fragment_storage=MemoryFragmentStorage(),
        pages_manager=pages_manager or PagesManager(""),
        cached_message_hashes=cached_message_hashes or frozenset(),
    )


class ScriptRunContextTest(unittest.TestCase):
    def setUp(self):
        # Stlite: clear the ctx ContextVar as it otherwise would be carried over
        # between tests, which all run in this thread's root Context.
        script_run_ctx_var.set(None)
        ThreadState.initialize()

    def test_allow_set_page_config_once(self):
        """st.set_page_config can be called once"""

        def fake_enqueue(msg):
            return None

        ctx = _create_script_run_context(fake_enqueue)

        msg = ForwardMsg()
        msg.page_config_changed.title = "foo"
        ctx.enqueue(msg)

    def test_allow_set_page_config_twice(self):
        """st.set_page_config can be called twice"""

        def fake_enqueue(msg):
            return None

        ctx = _create_script_run_context(fake_enqueue)

        msg = ForwardMsg()
        msg.page_config_changed.title = "foo"
        ctx.enqueue(msg)

        same_msg = ForwardMsg()
        same_msg.page_config_changed.title = "bar"
        ctx.enqueue(same_msg)

    def test_active_script_hash(self):
        """ensures active script hash is set correctly when enqueueing messages"""

        fake_path = "my/custom/script/path"
        pg_mgr = PagesManager(fake_path)

        def fake_enqueue(msg):
            return None

        ctx = _create_script_run_context(fake_enqueue, pages_manager=pg_mgr)
        ctx.reset(page_script_hash="main_script_hash")

        ctx.on_script_start()

        msg = ForwardMsg()
        msg.delta.new_element.markdown.body = "foo"

        ctx.enqueue(msg)
        assert msg.metadata.active_script_hash == ThreadState.get().active_script_hash

        ctx.set_mpa_v2_page("new_hash")

        with ctx.run_with_active_hash("new_hash"):
            new_msg = ForwardMsg()
            new_msg.delta.new_element.markdown.body = "bar"

            ctx.enqueue(new_msg)
            assert new_msg.metadata.active_script_hash == "new_hash"

    def test_enqueue_message_raise_if_ctx_is_none(self):
        msg = ForwardMsg()
        msg.delta.new_element.markdown.body = "foo"

        with pytest.raises(NoSessionContext):
            enqueue_message(msg)

    def test_enqueue_message(self):
        fake_enqueue_result: dict[str, ForwardMsg] = {}

        def fake_enqueue(msg: ForwardMsg):
            fake_enqueue_result["msg"] = msg

        ctx = _create_script_run_context(fake_enqueue)
        add_script_run_ctx(ctx=ctx)
        msg = ForwardMsg()
        msg.delta.new_element.markdown.body = "foo"
        enqueue_message(msg)
        assert fake_enqueue_result is not None
        assert (
            fake_enqueue_result["msg"].delta.new_element.markdown.body
            == msg.delta.new_element.markdown.body
        )

    def test_enqueue_message_sets_cacheable_flag(self):
        """Test that the metadata.cacheable flag is set correctly on outgoing ForwardMsgs."""
        fake_enqueue_result: dict[str, ForwardMsg] = {}

        def fake_enqueue(msg: ForwardMsg):
            fake_enqueue_result["msg"] = msg

        ctx = _create_script_run_context(fake_enqueue)
        add_script_run_ctx(ctx=ctx)

        with patch_config_options({"global.minCachedMessageSize": 0}):
            cacheable_msg = create_dataframe_msg([1, 2, 3])
            enqueue_message(cacheable_msg)
            assert fake_enqueue_result is not None
            assert fake_enqueue_result["msg"].metadata.cacheable

        with patch_config_options({"global.minCachedMessageSize": 1000}):
            cacheable_msg = create_dataframe_msg([4, 5, 6])
            enqueue_message(cacheable_msg)
            assert fake_enqueue_result is not None
            assert not fake_enqueue_result["msg"].metadata.cacheable

    def test_enqueue_reference_message_if_cached(self):
        """Test that a reference message is enqueued if the original message is cached."""
        fake_enqueue_result: dict[str, ForwardMsg] = {}

        def fake_enqueue(msg: ForwardMsg):
            fake_enqueue_result["msg"] = msg

        with patch_config_options({"global.minCachedMessageSize": 0}):
            cacheable_msg = create_dataframe_msg([1, 2, 3])
            populate_hash_if_needed(cacheable_msg)
            assert bool(cacheable_msg.hash)
            ctx = _create_script_run_context(
                fake_enqueue, cached_message_hashes=frozenset({cacheable_msg.hash})
            )
            add_script_run_ctx(ctx=ctx)
            enqueue_message(cacheable_msg)
            assert fake_enqueue_result is not None
            assert fake_enqueue_result["msg"].WhichOneof("type") == "ref_hash"

    def test_enqueue_message_with_fragment_id(self):
        fake_enqueue_result = {}

        def fake_enqueue(msg: ForwardMsg):
            fake_enqueue_result["msg"] = msg

        ThreadState.update(fragment_id="my_fragment_id")
        ctx = _create_script_run_context(fake_enqueue)
        add_script_run_ctx(ctx=ctx)
        msg = ForwardMsg()
        msg.delta.new_element.markdown.body = "foo"
        enqueue_message(msg)
        assert fake_enqueue_result is not None
        assert (
            fake_enqueue_result["msg"].delta.new_element.markdown.body
            == msg.delta.new_element.markdown.body
        )
        assert fake_enqueue_result["msg"].delta.fragment_id == "my_fragment_id"

    def test_run_with_active_hash(self):
        """Ensure the active script is set correctly"""
        pages_manager = PagesManager("")
        ctx = _create_script_run_context(
            lambda _msg: None,
            pages_manager=pages_manager,
        )

        ctx.reset(page_script_hash=pages_manager.main_script_hash)
        assert ThreadState.get().active_script_hash == pages_manager.main_script_hash

        enable_mpa_v2_mode(pages_manager)
        ctx.set_mpa_v2_page("new_hash")
        assert ThreadState.get().active_script_hash == pages_manager.main_script_hash

        with ctx.run_with_active_hash("new_hash"):
            assert ThreadState.get().active_script_hash == "new_hash"

        assert ThreadState.get().active_script_hash == pages_manager.main_script_hash

    def test_add_script_run_ctx_self_attach_seeds_thread_state(self):
        """Worker thread self-attach: ``add_script_run_ctx(ctx=...)`` from
        inside a worker thread (with no prior ThreadState bound) should seed
        ThreadState from ``ctx`` so subsequent ``ThreadState.get()`` calls
        don't crash. ``fragment_id`` / ``delta_path`` are not recoverable in
        this mode and must remain at their defaults.
        """
        pages_manager = PagesManager("/main/script/path")
        enable_mpa_v2_mode(pages_manager)  # populate main_script_hash
        ctx = _create_script_run_context(lambda _msg: None, pages_manager=pages_manager)

        result: dict[str, object] = {}

        def worker() -> None:
            try:
                add_script_run_ctx(ctx=ctx)
                ts = ThreadState.get()
                result["ts"] = ts
                # Stlite: the ctx is bound in the worker's Context, not as a
                # thread attribute, so read it back through the getter.
                result["attached_ctx"] = get_script_run_ctx()
            except Exception as exc:
                result["exc"] = exc

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert result.get("exc") is None, f"worker raised: {result.get('exc')!r}"
        ts = result["ts"]
        assert ts.active_script_hash == pages_manager.main_script_hash
        assert ts.fragment_id is None
        assert ts.delta_path is None
        assert ts.in_fragment_callback is False
        assert result["attached_ctx"] is ctx

    def test_add_script_run_ctx_self_attach_does_not_propagate_fragment_id(self):
        """Self-attached workers (``add_script_run_ctx(ctx=...)`` from inside
        the worker) do NOT inherit the parent's ``fragment_id``.
        """
        pages_manager = PagesManager("/main/script/path")
        enable_mpa_v2_mode(pages_manager)
        ctx = _create_script_run_context(lambda _msg: None, pages_manager=pages_manager)
        ThreadState.update(fragment_id="parent_fragment")

        result: dict[str, object] = {}

        def worker() -> None:
            add_script_run_ctx(ctx=ctx)
            result["fragment_id"] = ThreadState.get().fragment_id

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert result["fragment_id"] is None

    def test_add_script_run_ctx_self_attach_uses_main_script_hash_not_page_hash(
        self,
    ):
        """Self-attached workers seed ``active_script_hash`` from
        ``ctx.pages_manager.main_script_hash``, NOT the parent's current
        ``ThreadState.active_script_hash``.

        Concretely: inside an MPA v1 ``run_with_active_hash(page_hash)`` scope
        the parent observes the page hash, but a self-attaching worker still
        sees the main hash.
        """
        pages_manager = PagesManager("/main/script/path")
        enable_mpa_v2_mode(pages_manager)
        ctx = _create_script_run_context(lambda _msg: None, pages_manager=pages_manager)

        captured: dict[str, object] = {}

        with ctx.run_with_active_hash("page_hash"):
            assert ThreadState.get().active_script_hash == "page_hash"

            def worker() -> None:
                add_script_run_ctx(ctx=ctx)
                captured["active_script_hash"] = ThreadState.get().active_script_hash

            t = threading.Thread(target=worker)
            t.start()
            t.join()

        assert captured["active_script_hash"] == pages_manager.main_script_hash
        assert captured["active_script_hash"] != "page_hash"

    def test_add_script_run_ctx_double_attach_is_last_wins(self):
        """Repeat ``add_script_run_ctx`` calls on a not-yet-started task are
        last-wins for ``ctx``.
        """
        # Stlite: the child is a pending asyncio.Task rather than a Thread.
        ctx_a, _ = self._ctx_with_enqueue_log()
        ctx_b, _ = self._ctx_with_enqueue_log()
        captured: dict[str, object] = {}

        async def script() -> None:
            async def child() -> None:
                captured["ctx"] = get_script_run_ctx()

            task = asyncio.get_running_loop().create_task(
                child(), context=contextvars.Context()
            )
            add_script_run_ctx(task, ctx_a)
            add_script_run_ctx(task, ctx_b)
            await task

        asyncio.run(script())

        assert captured["ctx"] is ctx_b

    def test_reset_raises_when_called_from_non_main_thread(self):
        """``reset()`` may only be called from the script thread that
        constructed the context. Worker threads (parallel fragments) must
        not be able to mutate the context's coordinator out from under the
        main thread."""
        ctx = _create_script_run_context(lambda _msg: None)
        captured: list[Exception] = []

        def worker() -> None:
            try:
                ctx.reset()
            except RuntimeError as e:
                captured.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert len(captured) == 1
        assert "main script thread" in str(captured[0])

    def test_reset_creates_fresh_coordinator_each_run(self):
        """Each ``reset()`` constructs a new coordinator. Reusing one across
        runs would let a previous run's stop event / worker exception leak
        into the next run."""
        pages_manager = PagesManager("/main/script/path")
        enable_mpa_v2_mode(pages_manager)
        ctx = _create_script_run_context(lambda _msg: None, pages_manager=pages_manager)

        ctx.reset(page_script_hash=pages_manager.main_script_hash)
        first = ctx.parallel_coordinator
        assert isinstance(first, ParallelFragmentCoordinator)

        ctx.reset(page_script_hash=pages_manager.main_script_hash)
        second = ctx.parallel_coordinator
        assert isinstance(second, ParallelFragmentCoordinator)
        assert first is not second

    def test_reset_passes_yield_check_to_coordinator(self):
        """The yield_check supplied to ``reset()`` is wired into the
        coordinator so ``join()`` can drive the script runner's
        execution-control hook."""
        ctx = _create_script_run_context(lambda _msg: None)
        calls: list[int] = []
        ctx.reset(yield_check=lambda: calls.append(1))
        assert ctx.parallel_coordinator is not None
        ctx.parallel_coordinator._yield_check()
        assert calls == [1]

    def test_add_script_run_ctx_propagates_thread_state_to_child(self):
        """Child tasks observe the parent's ``FragmentThreadState`` on start;
        child mutations stay isolated from the parent's ContextVar.
        """
        # Stlite: the child is an asyncio.Task, which copies the parent's
        # Context when it is created, so no attach call is involved.
        ctx, _ = self._ctx_with_enqueue_log()
        captured: dict[str, object] = {}

        async def script() -> None:
            add_script_run_ctx(ctx=ctx)
            ThreadState.initialize(
                fragment_id="parent_fragment",
                delta_path=(1, 2, 3),
                active_script_hash="parent_hash",
            )

            async def child() -> None:
                ts_before = ThreadState.get()
                captured["before"] = (
                    ts_before.fragment_id,
                    ts_before.delta_path,
                    ts_before.active_script_hash,
                )
                ThreadState.update(fragment_id="child_changed_it")
                captured["after_child_update"] = ThreadState.get().fragment_id

            await asyncio.create_task(child())
            parent_ts = ThreadState.get()
            captured["parent"] = (
                parent_ts.fragment_id,
                parent_ts.delta_path,
                parent_ts.active_script_hash,
            )

        asyncio.run(script())

        assert captured["before"] == ("parent_fragment", (1, 2, 3), "parent_hash")
        assert captured["after_child_update"] == "child_changed_it"
        assert captured["parent"] == ("parent_fragment", (1, 2, 3), "parent_hash")

    def test_ctx_construction_creates_shared(self):
        """A freshly constructed ScriptRunContext owns its own SharedRunState."""
        ctx_a = _create_script_run_context(lambda _msg: None)
        ctx_b = _create_script_run_context(lambda _msg: None)

        assert isinstance(ctx_a.shared, SharedRunState)
        assert ctx_a.shared is not ctx_b.shared

    def test_reset_resets_shared_state(self):
        """``ctx.reset()`` clears the shared mutable state container."""
        pages_manager = PagesManager("/main/script/path")
        enable_mpa_v2_mode(pages_manager)
        ctx = _create_script_run_context(lambda _msg: None, pages_manager=pages_manager)

        ctx.shared.widget_ids_this_run.check_and_add("widget")
        ctx.shared.widget_user_keys_this_run.check_and_add("key")
        ctx.shared.form_ids_this_run.check_and_add("form")
        ctx.shared.new_fragment_ids.check_and_add("fragment")
        ctx.shared.track_command(_make_command("markdown"), max_per_command=5)

        ctx.reset(page_script_hash=pages_manager.main_script_hash)

        assert "widget" not in ctx.shared.widget_ids_this_run
        assert "key" not in ctx.shared.widget_user_keys_this_run
        assert "form" not in ctx.shared.form_ids_this_run
        assert "fragment" not in ctx.shared.new_fragment_ids
        assert ctx.shared.tracked_commands == ()
        assert ctx.shared.tracked_commands_count == 0

    # Stlite: under Pyodide a Python callback that JS invokes (a
    # ``pyodide.ffi.create_proxy`` fired from ``setTimeout`` or a promise
    # handler) runs on the event loop thread but outside any asyncio.Task.
    # ``loop.call_soon`` with an explicit Context reproduces that on host
    # CPython: an empty Context stands for a bare JS entry, and a
    # ``copy_context()`` taken in the script stands for what stlite_lib's
    # ``create_proxy`` wrapper captures.
    @staticmethod
    def _run_script_with_js_callback(
        script: Callable[[], contextvars.Context],
        callback: Callable[[], None],
    ) -> None:
        async def script_task() -> None:
            loop = asyncio.get_running_loop()
            done: asyncio.Future[None] = loop.create_future()

            def js_callback() -> None:
                try:
                    callback()
                except BaseException as exc:
                    done.set_exception(exc)
                else:
                    done.set_result(None)

            context = script()
            loop.call_soon(js_callback, context=context)
            await done

        asyncio.run(script_task())

    def _ctx_with_enqueue_log(self) -> tuple[ScriptRunContext, list[ForwardMsg]]:
        pages_manager = PagesManager("/main/script/path")
        enable_mpa_v2_mode(pages_manager)
        enqueued: list[ForwardMsg] = []
        ctx = _create_script_run_context(enqueued.append, pages_manager=pages_manager)
        return ctx, enqueued

    @staticmethod
    def _enqueue_markdown(body: str) -> None:
        msg = ForwardMsg()
        msg.delta.new_element.markdown.body = body
        enqueue_message(msg)

    def test_get_script_run_ctx_outside_task_without_ctx_returns_none(self):
        """Regression test for whitphx/stlite#2113: a bare JS entry has no task
        and no ctx; ``get_script_run_ctx()`` and ``add_script_run_ctx()`` must
        return None rather than crash on the missing task.
        """
        captured: dict[str, object] = {}

        def callback() -> None:
            captured["ctx"] = get_script_run_ctx(suppress_warning=True)
            captured["attached"] = add_script_run_ctx()

        self._run_script_with_js_callback(contextvars.Context, callback)

        assert captured["ctx"] is None
        assert captured["attached"] is None

    def test_js_callback_inherits_ctx_from_captured_context(self):
        """A callback run inside the Context captured in the script sees the
        script's ctx and its full ThreadState with no attach, which is what
        stlite_lib's ``create_proxy`` wrapper arranges under Pyodide. Unlike a
        self-attach, this carries ``fragment_id`` over too.
        """
        ctx, enqueued = self._ctx_with_enqueue_log()

        def script() -> contextvars.Context:
            add_script_run_ctx(ctx=ctx)
            ThreadState.update(
                active_script_hash="page_hash", fragment_id="my_fragment"
            )
            return contextvars.copy_context()

        self._run_script_with_js_callback(
            script, lambda: self._enqueue_markdown("from js")
        )

        assert [m.delta.new_element.markdown.body for m in enqueued] == ["from js"]
        assert enqueued[0].metadata.active_script_hash == "page_hash"
        assert enqueued[0].delta.fragment_id == "my_fragment"

    def test_js_callback_self_attaches_to_current_thread(self):
        """Regression test for whitphx/stlite#2113: the recipe from the issue,
        ``add_script_run_ctx(threading.current_thread(), ctx)`` inside a bare
        JS entry, binds the ctx in that callback's Context.
        """
        ctx, enqueued = self._ctx_with_enqueue_log()

        def callback() -> None:
            add_script_run_ctx(threading.current_thread(), ctx)
            self._enqueue_markdown("from js")

        self._run_script_with_js_callback(contextvars.Context, callback)

        assert [m.delta.new_element.markdown.body for m in enqueued] == ["from js"]
        assert (
            enqueued[0].metadata.active_script_hash
            == ctx.pages_manager.main_script_hash
        )

    def test_child_task_inherits_ctx(self):
        """A task created from the script copies its Context, so it enqueues
        into the script's session without any attach.
        """
        ctx, enqueued = self._ctx_with_enqueue_log()

        async def script() -> None:
            add_script_run_ctx(ctx=ctx)

            async def child() -> None:
                self._enqueue_markdown("from child")

            await asyncio.create_task(child())

        asyncio.run(script())

        assert [m.delta.new_element.markdown.body for m in enqueued] == ["from child"]

    def test_add_script_run_ctx_binds_into_pending_task(self):
        """``add_script_run_ctx(task, ctx)`` on a task created in an unrelated
        Context binds the ctx and a seeded ThreadState in that task's Context.
        """
        ctx, enqueued = self._ctx_with_enqueue_log()
        captured: dict[str, object] = {}

        async def script() -> None:
            async def child() -> None:
                captured["hash"] = ThreadState.get().active_script_hash
                self._enqueue_markdown("from child")

            task = asyncio.get_running_loop().create_task(
                child(), context=contextvars.Context()
            )
            add_script_run_ctx(task, ctx)
            await task

        asyncio.run(script())

        assert captured["hash"] == ctx.pages_manager.main_script_hash
        assert [m.delta.new_element.markdown.body for m in enqueued] == ["from child"]

    def test_add_script_run_ctx_rejects_other_thread(self):
        ctx, _ = self._ctx_with_enqueue_log()
        with pytest.raises(TypeError):
            add_script_run_ctx(threading.Thread(target=lambda: None), ctx)


def test_script_run_context_attr_name_reexported_from_leaf_module() -> None:
    """SCRIPT_RUN_CONTEXT_ATTR_NAME stays importable from script_run_context,
    re-exporting the same object defined in the script_run_context_attr leaf
    module, so existing import paths keep working."""
    from streamlit.runtime.scriptrunner_utils import script_run_context_attr

    assert (
        SCRIPT_RUN_CONTEXT_ATTR_NAME
        is script_run_context_attr.SCRIPT_RUN_CONTEXT_ATTR_NAME
    )
