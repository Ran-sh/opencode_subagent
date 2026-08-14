#!/usr/bin/env python3
"""README synchronization checker (OC-20260813-01, REV-2, GREEN).

Deterministic, Python 3.11+ standard-library-only, read-only repository
checker.  Validates that README.md and README.en.md form a synchronized
pair per PKG-OC-20260814-01-V1: equal README_SYNC markers, reciprocal
language links, the fixed ordered 8-pair H2 map, and the exact contract
tokens.  With ``--base <rev>``, changed paths are the union of
``git diff --name-only <rev> --`` and ``git ls-files --others
--exclude-standard``; exactly one changed README fails, both or neither
pass, and an invalid revision fails.

Diagnostics are stable rule codes plus repository-relative POSIX paths
only; README contents, secrets and absolute paths are never emitted.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

README_CN = "README.md"
README_EN = "README.en.md"

CODE_FILE_MISSING = "README_FILE_MISSING"
CODE_ENCODING_INVALID = "README_ENCODING_INVALID"
CODE_MARKER_MISMATCH = "README_SYNC_MARKER_MISMATCH"
CODE_LINK_MISSING = "README_LINK_MISSING"
CODE_SECTION_MISMATCH = "README_SECTION_MISMATCH"
CODE_CONTRACT_TOKEN_MISSING = "README_CONTRACT_TOKEN_MISSING"
CODE_BASE_INVALID = "README_BASE_INVALID"
CODE_PAIRED_CHANGE_MISSING = "README_PAIRED_CHANGE_MISSING"

_MARKER_PATTERN = re.compile(r"<!--\s*README_SYNC:[^>]*-->")
_ZH_EN_LINK = '<a href="./README.en.md">English</a>'
_EN_ZH_LINK = '<a href="./README.md">简体中文</a>'

H2_PAIRS = (
    ("项目简介", "Overview"),
    ("核心特性", "Key features"),
    ("快速开始", "Quick start"),
    ("默认配置", "Default configuration"),
    ("切换模型", "Switch models"),
    ("原生子 Agent 调用", "Native subagent call"),
    ("详细文档", "Technical reference"),
    ("许可证", "License"),
)

TOKENS = (
    'spawn_agent(agent_type="OpenCode", fork_context=false, ...)',
    "model=deepseek-v4-flash, effort=max",
    "chat_completions",
    "responses",
    "anthropic_messages",
    "19",
    "127.0.0.1",
    "--api-key-stdin",
)


def _h2_headings(text):
    """Return the ordered H2 heading texts of a README."""
    return [line[3:].strip() for line in text.splitlines()
            if line.startswith("## ")]


def _extract_marker(text):
    """Return the README_SYNC comment, or None when absent."""
    match = _MARKER_PATTERN.search(text)
    return match.group(0) if match else None


def _validate_markers(zh, en):
    zh_marker = _extract_marker(zh)
    en_marker = _extract_marker(en)
    if zh_marker is None or zh_marker != en_marker:
        return [(CODE_MARKER_MISMATCH, README_CN),
                (CODE_MARKER_MISMATCH, README_EN)]
    return []


def _validate_links(zh, en):
    findings = []
    if _ZH_EN_LINK not in zh:
        findings.append((CODE_LINK_MISSING, README_CN))
    if _EN_ZH_LINK not in en:
        findings.append((CODE_LINK_MISSING, README_EN))
    return findings


def _validate_sections(zh, en):
    findings = []
    if _h2_headings(zh) != [zh_h for zh_h, _ in H2_PAIRS]:
        findings.append((CODE_SECTION_MISMATCH, README_CN))
    if _h2_headings(en) != [en_h for _, en_h in H2_PAIRS]:
        findings.append((CODE_SECTION_MISMATCH, README_EN))
    return findings


def _validate_tokens(zh, en):
    findings = []
    for token in TOKENS:
        if token not in zh:
            findings.append((CODE_CONTRACT_TOKEN_MISSING, README_CN))
            break
        if token not in en:
            findings.append((CODE_CONTRACT_TOKEN_MISSING, README_EN))
            break
    return findings


def _changed_readmes(repo_root, base):
    """Return (changed set, error code) per ADR-3 union semantics."""
    diff = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--name-only", base, "--"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    if diff.returncode != 0:
        return None, CODE_BASE_INVALID
    untracked = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--others",
         "--exclude-standard"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    if untracked.returncode != 0:
        return None, CODE_BASE_INVALID
    changed = (set(diff.stdout.splitlines())
               | set(untracked.stdout.splitlines()))
    return changed, None


def _paired_change_findings(changed):
    zh_changed = README_CN in changed
    en_changed = README_EN in changed
    if zh_changed == en_changed:
        return []
    return [(CODE_PAIRED_CHANGE_MISSING,
             README_CN if zh_changed else README_EN)]


def validate_repo(repo_root, base=None):
    """Validate one repository; return sorted (code, relative path) pairs."""
    findings = []
    zh_path = repo_root / README_CN
    en_path = repo_root / README_EN
    if not zh_path.is_file():
        findings.append((CODE_FILE_MISSING, README_CN))
    if not en_path.is_file():
        findings.append((CODE_FILE_MISSING, README_EN))
    if not findings:
        try:
            zh = zh_path.read_text(encoding="utf-8")
            en = en_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append((CODE_ENCODING_INVALID, README_CN))
            findings.append((CODE_ENCODING_INVALID, README_EN))
        else:
            findings.extend(_validate_markers(zh, en))
            findings.extend(_validate_links(zh, en))
            findings.extend(_validate_sections(zh, en))
            findings.extend(_validate_tokens(zh, en))
    if base is not None:
        changed, error = _changed_readmes(repo_root, base)
        if error is not None:
            findings.append((error, "."))
        else:
            findings.extend(_paired_change_findings(changed))
    findings.sort()
    return findings


def main(argv=None):
    """CLI entry point: returns 0 on success and 1 on findings."""
    parser = argparse.ArgumentParser(
        prog="check_readme_sync",
        description="Validate README.md / README.en.md synchronization.")
    parser.add_argument("--repo-root", default=".",
                        help="repository root used for relative paths")
    parser.add_argument("--base", default=None,
                        help="git revision; both READMEs must change together")
    args = parser.parse_args(argv)
    findings = validate_repo(pathlib.Path(args.repo_root), args.base)
    for code, path in findings:
        print("%s %s" % (code, path))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
