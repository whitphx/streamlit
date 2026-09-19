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

"""The ContextVar that holds the active ScriptRunContext.

Stlite: the ctx lives in a ``contextvars`` variable rather than a thread
attribute. An asyncio task copies the current Context when it is created,
and stlite_lib runs a ``create_proxy`` callback inside the Context captured
when the proxy was made, so both see the script's ctx with no explicit
attach. This is a dependency-free leaf module so that both
``script_run_context`` and ``parallel_coordinator`` can share the variable
without importing each other. ``SCRIPT_RUN_CONTEXT_ATTR_NAME`` stays for
existing import paths; nothing stores a ctx under it any more.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from streamlit.runtime.scriptrunner_utils.script_run_context import (
        ScriptRunContext,
    )

SCRIPT_RUN_CONTEXT_ATTR_NAME: Final = "streamlit_script_run_ctx"

script_run_ctx_var: contextvars.ContextVar[ScriptRunContext | None] = (
    contextvars.ContextVar("script_run_ctx", default=None)
)
