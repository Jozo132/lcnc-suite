#!/usr/bin/env python3
"""
audit-scoped-css.py — catch Vue scoped-CSS leaks across SFC boundaries.

Vue 3 propagates a parent's scoped `data-v-xxx` attribute to the root of any
child component instance it renders, but NOT to nested elements within that
child. So a class defined in component A's <style scoped> block applies to:

  - elements in A's own <template>
  - the root element of any child component A renders directly

But it silently fails to apply when:

  - another component B uses the class on any non-root element in its template

This script flags every such silent failure.

  DEFINITE   — class used on a native HTML element on a non-root line. Bug.
  ROOT?      — class used on the consumer's root element. The parent caller's
               data-v reaches it, so this may work depending on who renders B.
               Suppressed by default; pass --all to see.

Pass-through usages on PascalCase child component tags are skipped (the class
flows to the child's root where the child's own scoped CSS applies).
Consumers with their own scoped copy or a global fallback in style.css are skipped.

Exit 1 if any DEFINITE finding.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

SRC = Path("lcnc-webui/src")
STYLE = SRC / "style.css"
TAG = r"[A-Za-z][\w-]*"


def repo_root() -> Path:
    return Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
    )


def block_range(text: str, open_re: str, close: str) -> tuple[int, int] | None:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines, 1):
        if start is None and re.search(open_re, line):
            start = i
        elif start is not None and close in line:
            return (start, i)
    return None


@lru_cache(maxsize=None)
def read(path: str) -> str:
    return Path(path).read_text()


@lru_cache(maxsize=None)
def scoped_classes(path: str) -> frozenset[str]:
    rng = block_range(read(path), r"<style[^>]*\bscoped\b", "</style>")
    if not rng:
        return frozenset()
    s, e = rng
    block = "\n".join(read(path).splitlines()[s - 1 : e])
    return frozenset(re.findall(rf"(?m)^\s*\.({TAG})", block))


@lru_cache(maxsize=None)
def template_range(path: str) -> tuple[int, int] | None:
    return block_range(read(path), r"<template>", "</template>")


@lru_cache(maxsize=None)
def root_range(path: str) -> tuple[int, int] | None:
    """1-indexed line range of the root opening tag inside <template>."""
    tpl = template_range(path)
    if not tpl:
        return None
    lines = read(path).splitlines()
    open_tag = re.compile(rf"<({TAG})")
    start = None
    for i in range(tpl[0], tpl[1]):
        if open_tag.search(lines[i]):
            start = i + 1
            break
    if start is None:
        return None
    for i in range(start - 1, min(tpl[1], start - 1 + 12)):
        if ">" in lines[i]:
            return (start, i + 1)
    return (start, start)


def tag_at(lines: list[str], lineno: int) -> str:
    """Best-effort: nearest preceding <tag> on or above lineno."""
    pat = re.compile(rf"<({TAG})")
    for i in range(lineno - 1, max(-1, lineno - 8), -1):
        cleaned = re.sub(r"<!--.*?-->", "", lines[i])
        matches = pat.findall(cleaned)
        if matches:
            return matches[-1]
    return "?"


def class_lines(path: str, name: str) -> list[tuple[int, str]]:
    tpl = template_range(path)
    if not tpl:
        return []
    lines = read(path).splitlines()
    pat = re.compile(rf'class="[^"]*(?<![\w-]){re.escape(name)}(?![\w-])')
    out = []
    for i in range(tpl[0], tpl[1] + 1):
        if pat.search(lines[i - 1]):
            out.append((i, tag_at(lines, i)))
    return out


def main(argv: list[str]) -> int:
    show_all = "--all" in argv

    os.chdir(repo_root())
    if not SRC.is_dir() or not STYLE.is_file():
        print(f"missing {SRC} or {STYLE}", file=sys.stderr)
        return 2

    vue_files = sorted(SRC.glob("*.vue"))
    # Capture every class token in style.css, including those buried in compound
    # selectors like `.dialog.lg` or `.val-status.warn`. Top-line-only would miss
    # these and produce dozens of false positives.
    global_classes = set(re.findall(rf"\.({TAG})", STYLE.read_text()))

    findings = []
    for def_file in vue_files:
        for name in scoped_classes(str(def_file)):
            if name in global_classes:
                continue
            for use_file in vue_files:
                if use_file == def_file:
                    continue
                if name in scoped_classes(str(use_file)):
                    continue
                rspan = root_range(str(use_file))
                for ln, tag in class_lines(str(use_file), name):
                    if tag and tag[0].isupper():
                        continue
                    sev = (
                        "ROOT?"
                        if rspan and rspan[0] <= ln <= rspan[1]
                        else "DEFINITE"
                    )
                    findings.append((sev, str(use_file), ln, name, str(def_file), tag))

    findings.sort()
    definite = 0
    for sev, f, ln, name, dfile, tag in findings:
        if sev == "DEFINITE":
            definite += 1
        if sev == "DEFINITE" or show_all:
            print(
                f"{sev:<9} {f}:{ln:<4} .{name:<24} on <{tag}>  (scoped in {dfile})"
            )

    if definite:
        print(f"\nFAIL: {definite} definite scoped-CSS leak(s).", file=sys.stderr)
        return 1
    suffix = "" if show_all else " (use --all to see ROOT? candidates)"
    print(f"OK: no definite scoped-CSS leaks.{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
