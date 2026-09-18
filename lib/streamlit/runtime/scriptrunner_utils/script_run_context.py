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
import contextlib
import contextvars
import dataclasses
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Final,
    TypeAlias,
    TypedDict,
)

from typing_extensions import Unpack

from streamlit import config
from streamlit.errors import (
    NoSessionContext,
)
from streamlit.logger import get_logger
from streamlit.runtime.forward_msg_cache import (
    create_reference_msg,
    populate_hash_if_needed,
)
from streamlit.runtime.parallel_coordinator import ParallelFragmentCoordinator
from streamlit.runtime.scriptrunner_utils.script_run_context_attr import (
    SCRIPT_RUN_CONTEXT_ATTR_NAME,
)
from streamlit.runtime.scriptrunner_utils.shared_run_state import SharedRunState

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from streamlit.cursor import RunningCursor
    from streamlit.proto.ClientState_pb2 import ContextInfo
    from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
    from streamlit.runtime.fragment import FragmentStorage
    from streamlit.runtime.pages_manager import PagesManager
    from streamlit.runtime.scriptrunner_utils.script_requests import ScriptRequests
    from streamlit.runtime.state import SafeSessionState
    from streamlit.runtime.uploaded_file_manager import UploadedFileManager

OnScriptErrorHandler: TypeAlias = Callable[[Exception], bool | None]

_LOGGER: Final = get_logger(__name__)

UserInfoType: TypeAlias = dict[str, str | bool | dict[str, str] | None]


class RunLocation(Enum):
    """The execution phase of the currently running Streamlit code.

    Tracks which of three phases is active on the current thread:

    - ``MAIN_SCRIPT`` — the top-level app script body is running.
    - ``FRAGMENT`` — a ``@st.fragment`` body is executing.
    - ``CALLBACK`` — a widget callback (``on_change``, ``on_click``, etc.)
      is executing.
    """

    MAIN_SCRIPT = "main_script"
    FRAGMENT = "fragment"
    CALLBACK = "callback"


# If true, it indicates that we are in a cached function that disallows the usage of
# widgets. Using contextvars to be thread-safe.
in_cached_function: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_cached_function", default=False
)


@dataclass(frozen=True)
class FragmentThreadState:
    """Per-thread state for a fragment execution.

    Frozen so that all mutations must go through ThreadState.update() or
    ThreadState.scoped(), which rebind the ContextVar and guarantee context
    isolation when using copy_context().
    """

    fragment_id: str | None = None
    delta_path: tuple[int, ...] | None = None
    run_location: RunLocation = RunLocation.MAIN_SCRIPT
    active_script_hash: str = ""
    # Set on parallel-fragment workers so wrapped_fragment() skips creating a
    # second st.container(); the main thread already pre-allocated one before
    # dispatching the worker.  Cleared after first use.
    pre_allocated_container_fragment_id: str | None = None
    # True while executing inside a parallel fragment worker thread; used by
    # _check_not_parallel_worker() to gate APIs that are unsafe during
    # concurrent execution (e.g. st.dialog, st.switch_page).
    is_parallel_worker: bool = False

    @property
    def in_fragment_callback(self) -> bool:
        """True when executing inside a widget callback for a fragment widget.

        Derived from ``run_location`` and ``fragment_id`` so that all existing
        consumers of this flag keep working without change.
        """
        return (
            self.run_location is RunLocation.CALLBACK and self.fragment_id is not None
        )


class _FragmentThreadStateFields(TypedDict, total=False):
    """Keyword-arg shape for ``ThreadState.{initialize, update, scoped}``.

    Mirrors ``FragmentThreadState``'s fields. Using ``TypedDict + Unpack``
    catches typos (e.g., ``fragmentid=`` vs ``fragment_id=``) and wrong
    value types at type-check time rather than as a runtime ``TypeError``.
    """

    fragment_id: str | None
    delta_path: tuple[int, ...] | None
    run_location: RunLocation
    active_script_hash: str
    pre_allocated_container_fragment_id: str | None
    is_parallel_worker: bool


_thread_state: contextvars.ContextVar[FragmentThreadState] = contextvars.ContextVar(
    "fragment_thread_state",
)


class ThreadState:
    """Encapsulates all access to the per-thread FragmentThreadState ContextVar.

    The ContextVar ``_thread_state`` is module-private. External code interacts
    exclusively through this class. The frozen dataclass ensures callers cannot
    bypass the API — in-place mutation raises ``FrozenInstanceError``.
    """

    @staticmethod
    def initialize(**kwargs: Unpack[_FragmentThreadStateFields]) -> None:
        """Create a fresh FragmentThreadState and bind it in the current context.

        Called from ``reset()`` at the start of every script run, and from
        parallel fragment worker setup to seed the worker's copied context
        with the parent's snapshot.
        """
        _thread_state.set(FragmentThreadState(**kwargs))

    @staticmethod
    def get() -> FragmentThreadState:
        """Read the current context's FragmentThreadState.

        Returns a frozen object — callers can read fields but cannot mutate
        them. Raises ``RuntimeError`` if called before ``initialize()``.
        """
        try:
            return _thread_state.get()
        except LookupError:
            raise RuntimeError(
                "FragmentThreadState not initialized — "
                "ScriptRunContext.reset() or add_script_run_ctx() must be "
                "called on this thread first."
            ) from None

    @staticmethod
    def update(**kwargs: Unpack[_FragmentThreadStateFields]) -> None:
        """Update one or more fields on the current context's state.

        Creates a new frozen dataclass via ``dataclasses.replace()`` and
        rebinds the ContextVar, ensuring context isolation.
        """
        _thread_state.set(dataclasses.replace(ThreadState.get(), **kwargs))

    @staticmethod
    @contextlib.contextmanager
    def scoped(
        **overrides: Unpack[_FragmentThreadStateFields],
    ) -> Generator[None, None, None]:
        """Temporarily override fields, automatically restore on exit.

        Uses ``ContextVar.reset(token)`` to atomically restore the entire
        previous state. Used for nesting within a single thread (e.g. nested
        fragments) where ``copy_context()`` is not involved.
        """
        token = _thread_state.set(dataclasses.replace(ThreadState.get(), **overrides))
        try:
            yield
        finally:
            _thread_state.reset(token)


@dataclass
class ScriptRunContext:
    """A context object that contains data for a "script run" - that is,
    data that's scoped to a single ScriptRunner execution (and therefore also
    scoped to a single connected "session").

    ScriptRunContext is used internally by virtually every `st.foo()` function.
    It is accessed only from the script thread that's created by ScriptRunner,
    or from app-created helper threads that have been "attached" to the
    ScriptRunContext via `add_script_run_ctx`.

    Streamlit code typically retrieves the active ScriptRunContext via the
    `get_script_run_ctx` function.

    Note: ``__post_init__`` adds a non-field ``_main_thread_ident`` used
    by ``reset()``'s thread guard.
    """

    session_id: str
    _enqueue: Callable[[ForwardMsg], None]
    query_string: str
    session_state: SafeSessionState
    uploaded_file_mgr: UploadedFileManager
    main_script_path: str
    user_info: UserInfoType
    fragment_storage: FragmentStorage
    pages_manager: PagesManager
    on_script_error: OnScriptErrorHandler | None = None

    # Hashes of messages that are cached in the client browser:
    cached_message_hashes: frozenset[str] = field(default_factory=frozenset)
    context_info: ContextInfo | None = None
    gather_usage_stats: bool = False
    command_tracking_deactivated: bool = False
    _has_script_started: bool = False
    shared: SharedRunState = field(default_factory=SharedRunState)
    cursors: dict[int, RunningCursor] = field(default_factory=dict)
    script_requests: ScriptRequests | None = None
    fragment_ids_this_run: list[str] | None = None
    # we allow only one dialog to be open at the same time
    has_dialog_opened: bool = False
    parallel_coordinator: ParallelFragmentCoordinator | None = None

    def __post_init__(self) -> None:
        # Capture the main script thread's identity so reset() can refuse to
        # run from worker threads.
        self._main_thread_ident = threading.get_ident()

    @property
    def page_script_hash(self) -> str:
        return self.pages_manager.current_page_script_hash

    @property
    def main_script_parent(self) -> Path:
        return self.pages_manager.main_script_parent

    @contextlib.contextmanager
    def run_with_active_hash(self, page_hash: str) -> Generator[None, None, None]:
        with ThreadState.scoped(active_script_hash=page_hash):
            yield

    def set_mpa_v2_page(self, page_script_hash: str) -> None:
        ThreadState.update(active_script_hash=self.pages_manager.main_script_hash)
        self.pages_manager.set_current_page_script_hash(page_script_hash)

    def reset(
        self,
        query_string: str = "",
        page_script_hash: str = "",
        fragment_ids_this_run: list[str] | None = None,
        cached_message_hashes: frozenset[str] | None = None,
        context_info: ContextInfo | None = None,
        # Checked by fragment workers to cease execution.
        yield_check: Callable[[], None] = lambda: None,
    ) -> None:
        if threading.get_ident() != self._main_thread_ident:
            raise RuntimeError(
                "ScriptRunContext.reset() must only be called from the main "
                "script thread"
            )
        # Check if this is a same-page rerun BEFORE updating page_script_hash
        is_same_page = self.page_script_hash == page_script_hash

        self.cursors = {}
        self.shared.reset()
        self.query_string = query_string
        self.context_info = context_info
        self.pages_manager.set_current_page_script_hash(page_script_hash)
        ThreadState.initialize(
            active_script_hash=self.pages_manager.main_script_hash,
        )
        self.parallel_coordinator = ParallelFragmentCoordinator(
            yield_check=yield_check,
            max_workers=config.get_option("runner.parallelMaxWorkers"),
        )
        self._has_script_started = False
        self.command_tracking_deactivated: bool = False
        self.fragment_ids_this_run = fragment_ids_this_run
        self.has_dialog_opened = False
        self.cached_message_hashes = frozenset(cached_message_hashes or ())

        in_cached_function.set(False)

        with self.session_state.query_params() as qp:
            # For same-page reruns (widget interactions), populate _query_params from URL
            # and set initial params for widget seeding.
            # For page transitions, both populate_from_query_string() AND
            # set_initial_query_params_from_current() are called in script_runner.py
            # BEFORE reset() to ensure filtering is applied to both _query_params
            # AND _initial_query_params, preventing stale params from previous pages
            # from seeding widgets on the new page.
            if is_same_page:
                qp.set_initial_query_params(query_string)
                qp.populate_from_query_string(query_string)

    def on_script_start(self) -> None:
        self._has_script_started = True

    def enqueue(self, msg: ForwardMsg) -> None:
        """Enqueue a ForwardMsg for this context's session."""
        msg.metadata.active_script_hash = ThreadState.get().active_script_hash

        # We populate the hash and cacheable field for all messages.
        # Besides the forward message cache, the hash might also be used
        # for other aspects within the frontend.
        populate_hash_if_needed(msg)
        msg_to_send = msg
        if (
            msg.metadata.cacheable
            and msg.hash
            and msg.hash in self.cached_message_hashes
        ):
            _LOGGER.debug("Sending cached message ref (hash=%s)", msg.hash)
            msg_to_send = create_reference_msg(msg)

        # Pass the message up to our associated ScriptRunner.
        self._enqueue(msg_to_send)


def _current_ctx_holder() -> asyncio.Task[Any] | threading.Thread:
    # Stlite: the script body runs as an asyncio.Task, so the Task normally
    # stands in for upstream's script thread as the ScriptRunContext holder.
    # Python can also be entered from JS outside any Task step, e.g. a
    # ``pyodide.ffi.create_proxy`` callback that a JS library fires from
    # ``setTimeout`` or a promise handler (whitphx/stlite#2113). No Task is
    # current there, and on host CPython the loop may not be running at all,
    # so the thread object takes over the holder role. That keeps upstream's
    # ``add_script_run_ctx(threading.current_thread(), ctx)`` recipe working.
    try:
        task = asyncio.current_task()
    except RuntimeError:  # no running event loop
        task = None
    return task if task is not None else threading.current_thread()


def _ensure_thread_state(ctx: ScriptRunContext) -> None:
    # Stlite: a ScriptRunContext attached to a holder from outside the script
    # Task (the thread object a JS-invoked callback runs on, or a Task created
    # from such a callback) is read in a contextvars Context that
    # ``ctx.reset()`` never ran in, so no ThreadState is bound there. Seed it
    # from ``ctx`` so ``enqueue_message()`` and friends work. Only the script
    # hash is recoverable; see the self-attach contract on add_script_run_ctx.
    if _thread_state.get(None) is None:
        ThreadState.initialize(active_script_hash=ctx.pages_manager.main_script_hash)


def add_script_run_ctx(
    # Stlite: threading is not supported on Pyodide. The script's asyncio.Task
    # takes the place of upstream's thread, and the thread object itself is
    # the holder wherever no Task is current (see _current_ctx_holder).
    thread: asyncio.Task[Any] | threading.Thread | None = None,
    ctx: ScriptRunContext | None = None,
) -> asyncio.Task[Any] | threading.Thread:
    """Attach the current ScriptRunContext to a task (or, outside any task,
    to the thread) and make sure a FragmentThreadState is bound for it.

    Normal usage: call from the parent task, before the child starts.
    Repeat attaches on the same not-yet-started task are last-wins for
    ``ctx``.

    Self-attach fallback: when called from inside the holder it is
    attaching to (current task, ``threading.current_thread()`` from a
    JS-invoked callback, or ``thread`` omitted with explicit ``ctx``),
    ``ThreadState`` is seeded directly from ``ctx``. Only the
    script hash carries over, so:

    - ``fragment_id`` and ``delta_path`` are NOT propagated; worker
      writes won't be stamped with the parent's ``fragment_id``.
    - ``active_script_hash`` is seeded from
      ``ctx.pages_manager.main_script_hash``. MPA v1 page bodies will
      therefore see the main hash, not the page hash; migrate to MPA v2
      / ``st.navigation`` if that matters. Locked in by
      ``test_add_script_run_ctx_self_attach_uses_main_script_hash_not_page_hash``.

    The thread holds a single ctx for the whole worker. With several apps
    in one SharedWorker, a ctx attached at script level can be overwritten
    by another app before the callback fires, so attach from inside the
    callback right before drawing. An ``async def`` callback runs as its
    own task and must attach to that task instead
    (``add_script_run_ctx(ctx=ctx)`` inside the coroutine).

    Parameters
    ----------
    thread : asyncio.Task, threading.Thread or None
        Task or thread to attach to. Defaults to the current task, or to
        the current thread when no task is running.
    ctx : ScriptRunContext or None
        Context to attach. Defaults to the caller's current
        ScriptRunContext.

    Returns
    -------
    asyncio.Task or threading.Thread
        The same holder that was passed in, for chaining.

    """
    holder = _current_ctx_holder()
    if thread is None:
        thread = holder
    if ctx is None:
        ctx = get_script_run_ctx()
    if ctx is not None:
        setattr(thread, SCRIPT_RUN_CONTEXT_ATTR_NAME, ctx)

    # Stlite: upstream stashes the parent's FragmentThreadState on the child and
    # wraps its run() so the child re-initializes the ContextVar, because
    # ContextVars don't cross thread boundaries. Here a child asyncio.Task
    # copies the current context when it is created, so the parent's state
    # already reaches it and there is no run() to wrap. What is left is the
    # self-attach case described in the docstring above.
    if ctx is not None and thread is holder:
        _ensure_thread_state(ctx)

    return thread


def get_script_run_ctx(suppress_warning: bool = False) -> ScriptRunContext | None:
    """
    Parameters
    ----------
    suppress_warning : bool
        If True, don't log a warning if there's no ScriptRunContext.

    Returns
    -------
    ScriptRunContext | None
        The current thread's ScriptRunContext, or None if it doesn't have one.

    """
    holder = _current_ctx_holder()
    ctx: ScriptRunContext | None = getattr(holder, SCRIPT_RUN_CONTEXT_ATTR_NAME, None)
    if ctx is None:
        if not suppress_warning:
            # Only warn about a missing ScriptRunContext if suppress_warning is False,
            # and we were started via `streamlit run`. Otherwise, the user is likely
            # running a script "bare", and doesn't need to be warned about streamlit
            # bits that are irrelevant when not connected to a session.
            _LOGGER.warning(
                "%s '%s': missing ScriptRunContext! This warning can be ignored when "
                "running in bare mode.",
                type(holder).__name__,
                holder.get_name() if isinstance(holder, asyncio.Task) else holder.name,
            )
    elif isinstance(holder, threading.Thread):
        # Stlite: the ctx may have been attached to the thread from another
        # Context, e.g. ``add_script_run_ctx(threading.current_thread(), ctx)``
        # run at script level for a JS callback that fires later. A Task
        # holder is left alone so a missing ThreadState still fails loudly.
        _ensure_thread_state(ctx)

    return ctx


def enqueue_message(msg: ForwardMsg) -> None:
    """Enqueues a ForwardMsg proto to send to the app."""
    ctx = get_script_run_ctx()

    if ctx is None:
        raise NoSessionContext()

    ts = ThreadState.get()
    if ts.fragment_id and msg.WhichOneof("type") == "delta":
        msg.delta.fragment_id = ts.fragment_id

    ctx.enqueue(msg)
