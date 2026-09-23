"""Enumerate canonical Radon function blocks, including methods in local classes."""

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeGuard, cast

from radon.complexity import cc_visit, cc_visit_ast


class _RadonBlock(Protocol):
    name: str
    lineno: int
    complexity: int


class _RadonFunctionBlock(_RadonBlock, Protocol):
    closures: Sequence[_RadonFunctionBlock]
    is_method: bool


class _RadonClassBlock(_RadonBlock, Protocol):
    inner_classes: Sequence[_RadonClassBlock]
    methods: Sequence[_RadonFunctionBlock]


_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, slots=True)
class FunctionComplexity:
    name: str
    lineno: int
    complexity: int


def _is_class_block(block: _RadonBlock) -> TypeGuard[_RadonClassBlock]:
    return hasattr(block, "methods") and hasattr(block, "inner_classes")


def _walk_blocks(
    blocks: Sequence[_RadonBlock],
    prefix: tuple[str, ...] = (),
) -> list[FunctionComplexity]:
    found: list[FunctionComplexity] = []
    for block in blocks:
        if _is_class_block(block):
            class_prefix = (*prefix, block.name)
            found.extend(_walk_blocks(block.inner_classes, class_prefix))
            found.extend(_walk_blocks(block.methods, class_prefix))
            continue

        function_block = cast(_RadonFunctionBlock, block)
        if not prefix and function_block.is_method:
            continue
        qualname = ".".join((*prefix, function_block.name))
        found.append(FunctionComplexity(qualname, function_block.lineno, function_block.complexity))
        found.extend(_walk_blocks(function_block.closures, (*prefix, function_block.name)))
    return found


def _local_class_methods(tree: ast.Module) -> list[tuple[str, _FunctionNode]]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        parents.update(dict.fromkeys(ast.iter_child_nodes(parent), parent))

    methods: list[tuple[str, _FunctionNode]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parent = parents.get(node)
        if not isinstance(parent, ast.ClassDef):
            continue

        ancestors: list[ast.AST] = []
        ancestor = parent
        while ancestor in parents:
            ancestors.append(ancestor)
            ancestor = parents[ancestor]

        inside_function_local_class = False
        has_function_ancestor = False
        for scope in reversed(ancestors):
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                has_function_ancestor = True
            elif isinstance(scope, ast.ClassDef) and has_function_ancestor:
                inside_function_local_class = True
        if not inside_function_local_class:
            continue

        scope_names = [
            scope.name
            for scope in reversed(ancestors)
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        methods.append((".".join((*scope_names, node.name)), node))
    return methods


def collect_function_complexities(source: str) -> list[FunctionComplexity]:
    blocks = _walk_blocks(cast(Sequence[_RadonBlock], cc_visit(source)))
    present = {(block.name, block.lineno) for block in blocks}

    tree = ast.parse(source)
    for qualname, method in _local_class_methods(tree):
        prefix = tuple(qualname.rpartition(".")[0].split("."))
        module = ast.Module(body=[method], type_ignores=[])
        local_blocks = _walk_blocks(cast(Sequence[_RadonBlock], cc_visit_ast(module)), prefix)
        for block in local_blocks:
            identity = (block.name, block.lineno)
            if identity not in present:
                blocks.append(block)
                present.add(identity)
    return blocks
