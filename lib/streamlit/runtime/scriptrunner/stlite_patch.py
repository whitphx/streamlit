# Copyright (c) Streamlit Inc. (2018-2022) Snowflake Inc. (2022-2024)
# Copyright (c) Yuichiro Tachibana (Tsuchiya) (2022-2024)
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

import ast


def patch(code: str | ast.Module, script_path: str) -> ast.Module:
    if isinstance(code, str):
        tree = ast.parse(code, script_path, "exec")
    elif isinstance(code, ast.Module):
        tree = code
    else:
        raise ValueError("code must be a string or an ast.Module")

    _modify_ast_subtree(tree)

    ast.fix_missing_locations(tree)

    return tree


def _modify_ast_subtree(
    tree: ast.AST,
    body_attr: str = "body",
):
    # Ref: magic._modify_ast_subtree
    body = getattr(tree, body_attr)

    for node in body:
        node_type = type(node)
        if (
            node_type is ast.With
            or node_type is ast.For
            or node_type is ast.While
            or node_type is ast.Try
        ):
            _modify_ast_subtree(node)
        elif node_type is ast.Try:
            for j, inner_node in enumerate(node.handlers):
                node.handlers[j] = _modify_ast_subtree(inner_node)
            finally_node = _modify_ast_subtree(node, body_attr="finalbody")
            node.finalbody = finally_node.finalbody
            _modify_ast_subtree(node)
        elif node_type is ast.If:
            _modify_ast_subtree(node)
            _modify_ast_subtree(node, "orelse")
        elif node_type is ast.Expr:
            if type(node.value) is ast.Call:
                called_func = node.value.func
                if (
                    type(called_func) is ast.Attribute
                    and called_func.value.id == "st"
                    and isinstance(called_func.value.ctx, ast.Load)
                ):
                    # `st.*` function call
                    if called_func.attr == "write_stream":
                        # Modify `st.write_stream()` to `await st.write_stream()`
                        node.value = ast.Await(value=node.value)
