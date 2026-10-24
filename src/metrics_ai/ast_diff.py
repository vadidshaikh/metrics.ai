"""Python `ast` structural profiling and before/after diff extraction."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

BATCH_CALLS = {"executemany", "bulk_create", "bulk_update", "bulk_insert_mappings",
               "insert_many", "bulk_write", "batch", "chunked", "imap", "starmap"}
CACHE_DECORATORS = {"lru_cache", "cache", "cached_property", "cached", "memoize", "cachetools"}
CONCURRENCY_CALLS = {"ThreadPoolExecutor", "ProcessPoolExecutor", "gather", "create_task",
                     "Pool", "run_in_executor", "as_completed", "TaskGroup"}
LOOP_NODES = (ast.For, ast.AsyncFor, ast.While, ast.comprehension)


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


@dataclass
class Profile:
    functions: set[str] = field(default_factory=set)
    classes: set[str] = field(default_factory=set)
    decorators: set[str] = field(default_factory=set)
    calls: Counter = field(default_factory=Counter)
    calls_in_loops: Counter = field(default_factory=Counter)
    loops: int = 0
    nested_loops: int = 0
    max_loop_depth: int = 0
    async_defs: int = 0
    awaits: int = 0
    parse_error: str | None = None


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.p = Profile()
        self.scope: list[str] = []
        self.loop_depth = 0

    def _def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qual = ".".join([*self.scope, node.name])
        self.p.functions.add(qual)
        if isinstance(node, ast.AsyncFunctionDef):
            self.p.async_defs += 1
        for deco in node.decorator_list:
            self.p.decorators.add(f"{qual}@{_name(deco)}")
        self.scope.append(node.name)
        outer, self.loop_depth = self.loop_depth, 0
        self.generic_visit(node)
        self.loop_depth = outer
        self.scope.pop()

    visit_FunctionDef = _def
    visit_AsyncFunctionDef = _def

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.p.classes.add(".".join([*self.scope, node.name]))
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _loop(self, node: ast.AST) -> None:
        self.p.loops += 1
        self.loop_depth += 1
        if self.loop_depth > 1:
            self.p.nested_loops += 1
        self.p.max_loop_depth = max(self.p.max_loop_depth, self.loop_depth)
        self.generic_visit(node)
        self.loop_depth -= 1

    visit_For = visit_AsyncFor = visit_While = visit_comprehension = _loop

    def visit_Await(self, node: ast.Await) -> None:
        self.p.awaits += 1
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _name(node)
        if name:
            self.p.calls[name] += 1
            if self.loop_depth:
                self.p.calls_in_loops[name] += 1
        self.generic_visit(node)


def profile_source(source: str | None) -> Profile:
    if not source:
        return Profile()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return Profile(parse_error=str(exc))
    visitor = _Visitor()
    visitor.visit(tree)
    return visitor.p


def _deco_names(decos: set[str]) -> set[str]:
    return {d.split("@", 1)[1] for d in decos}


def diff_profiles(before: Profile, after: Profile) -> dict[str, Any]:
    added_calls = set(after.calls) - set(before.calls)
    removed_calls = set(before.calls) - set(after.calls)
    hoisted = sorted(n for n, c in before.calls_in_loops.items() if after.calls_in_loops.get(n, 0) < c)
    deco_added = after.decorators - before.decorators
    deco_removed = before.decorators - after.decorators
    return {
        "added_functions": sorted(after.functions - before.functions),
        "removed_functions": sorted(before.functions - after.functions),
        "added_classes": sorted(after.classes - before.classes),
        "removed_classes": sorted(before.classes - after.classes),
        "decorators_added": sorted(deco_added),
        "decorators_removed": sorted(deco_removed),
        "caching_decorators_added": sorted(_deco_names(deco_added) & CACHE_DECORATORS),
        "batching_calls_added": sorted(n for n in added_calls if n in BATCH_CALLS or "batch" in n.lower()
                                       or "bulk" in n.lower()),
        "batching_calls_removed": sorted(n for n in removed_calls if n in BATCH_CALLS),
        "concurrency_calls_added": sorted(added_calls & CONCURRENCY_CALLS),
        "calls_added": sorted(added_calls),
        "calls_removed": sorted(removed_calls),
        "calls_hoisted_out_of_loops": hoisted,
        "loops": {"before": before.loops, "after": after.loops},
        "nested_loops": {"before": before.nested_loops, "after": after.nested_loops},
        "max_loop_depth": {"before": before.max_loop_depth, "after": after.max_loop_depth},
        "async": {"before": before.async_defs + before.awaits, "after": after.async_defs + after.awaits},
    }


def structural_signals(diff: dict[str, Any]) -> list[str]:
    """Human-readable structural shift tags derived from a (file or total) diff."""
    signals = []
    if diff["nested_loops"]["after"] < diff["nested_loops"]["before"]:
        signals.append("nested_loops_removed")
    if diff["batching_calls_added"]:
        signals.append("batched_operations_added")
    if diff["caching_decorators_added"]:
        signals.append("caching_decorator_added")
    if diff["concurrency_calls_added"] or diff["async"]["after"] > diff["async"]["before"]:
        signals.append("async_concurrency_added")
    if diff["calls_hoisted_out_of_loops"]:
        signals.append("calls_hoisted_out_of_loops")
    if len(diff["added_functions"]) >= 2 or diff["added_classes"]:
        signals.append("modularization")
    return signals


def merge_diffs(per_file: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-file diffs into one repo-level structural diff."""
    total = diff_profiles(Profile(), Profile())
    qualified = ("added_functions", "removed_functions", "added_classes", "removed_classes",
                 "decorators_added", "decorators_removed")
    for path, diff in per_file.items():
        for key, value in diff.items():
            if isinstance(value, list):
                total[key].extend(f"{path}::{v}" if key in qualified else v for v in value)
            elif isinstance(value, dict):
                total[key]["before"] += value["before"]
                total[key]["after"] += value["after"]
    for key, value in total.items():
        if isinstance(value, list):
            total[key] = sorted(set(value))
    return total
