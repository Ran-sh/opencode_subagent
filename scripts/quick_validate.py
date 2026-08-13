#!/usr/bin/env python3
"""Repository quick validator (DS-20260812-24).

Deterministic, Python 3.11+ standard-library only, read-only repository
validation.  Diagnostics are only stable rule codes and repository-relative
POSIX paths; file contents, secrets and absolute paths are never emitted.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

SUCCESS_LINE = "repository validation passed"

CODE_ENC_BOM = "ENC_BOM"
CODE_ENC_CRLF = "ENC_CRLF"
CODE_ENC_BARE_CR = "ENC_BARE_CR"
CODE_ENC_NUL = "ENC_NUL"
CODE_ENC_UTF8 = "ENC_UTF8"
CODE_META_SKILL = "META_SKILL_FRONTMATTER"
CODE_META_OPENAI = "META_OPENAI_INTERFACE"
CODE_SECRET_PEM = "SECRET_PRIVATE_KEY"
CODE_SECRET_OPENAI = "SECRET_OPENAI_TOKEN"
CODE_SECRET_GITHUB = "SECRET_GITHUB_TOKEN"
CODE_SECRET_AWS = "SECRET_AWS_ACCESS_KEY"
CODE_SECRET_WORKSPACE = "SECRET_WORKSPACE_ID"
CODE_ARTIFACT_PYCACHE = "ARTIFACT_PYCACHE"
CODE_ARTIFACT_PYC = "ARTIFACT_PYC"
CODE_PATH_INVALID = "PATH_INVALID"

_ENCODING_CODES = frozenset({
    CODE_ENC_BOM, CODE_ENC_CRLF, CODE_ENC_BARE_CR, CODE_ENC_NUL,
    CODE_ENC_UTF8,
})
_TEXT_EXTENSIONS = frozenset({
    ".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".json",
})
_TEXT_NAMES = frozenset({
    ".gitignore", ".gitattributes", "LICENSE", "NOTICE",
})
_SKILL_ALLOWED_KEYS = frozenset({
    "name", "description", "license", "allowed-tools", "metadata",
})
_MARKER = "$codex-opencode-go-subagent"
_SECRET_PATTERNS = (
    (CODE_SECRET_PEM, re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (CODE_SECRET_OPENAI, re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    (CODE_SECRET_GITHUB, re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    (CODE_SECRET_AWS, re.compile(r"AKIA[0-9A-Z]{16}")),
    (CODE_SECRET_WORKSPACE, re.compile(r"wrk_[A-Z0-9]{16,}")),
)
_QUOTED_VALUE = re.compile(r'^"(.*)"$')
_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class Finding:
    """One diagnostic: a stable rule code and a repository-relative path."""

    def __init__(self, code, path):
        self.code = code
        self.path = path


def validate_repository(skill_dir, repo_root):
    """Validate a repository and return sorted findings (read-only)."""
    try:
        root = pathlib.Path(repo_root).resolve()
        skill = pathlib.Path(skill_dir).resolve()
        if not root.is_dir():
            return [Finding(CODE_PATH_INVALID, ".")]
        try:
            skill_rel = skill.relative_to(root)
        except ValueError:
            return [Finding(CODE_PATH_INVALID, ".")]
        if not skill.is_dir() and skill.exists():
            return [Finding(CODE_PATH_INVALID, ".")]
        findings = _walk_findings(root)
        encoded = {finding.path for finding in findings
                   if finding.code in _ENCODING_CODES}
        skill_md = skill / "SKILL.md"
        if skill_md.is_file():
            rel = skill_rel.joinpath("SKILL.md").as_posix()
            if rel not in encoded:
                finding = _skill_metadata_finding(skill_md, rel)
                if finding is not None:
                    findings.append(finding)
        else:
            findings.append(Finding(CODE_META_SKILL,
                                    skill_rel.joinpath("SKILL.md").as_posix()))
        openai_yaml = skill / "agents" / "openai.yaml"
        if openai_yaml.is_file():
            rel = skill_rel.joinpath("agents", "openai.yaml").as_posix()
            if rel not in encoded:
                finding = _openai_metadata_finding(openai_yaml, rel)
                if finding is not None:
                    findings.append(finding)
        else:
            findings.append(Finding(CODE_META_OPENAI, skill_rel.joinpath(
                "agents", "openai.yaml").as_posix()))
        findings.sort(key=lambda finding: (finding.code, finding.path))
        return findings
    except OSError:
        return [Finding(CODE_PATH_INVALID, ".")]


def main(argv=None):
    """CLI entry point: returns 0 on success and 1 on findings."""
    parser = argparse.ArgumentParser(
        prog="quick_validate",
        description="Validate repository text, metadata, artifacts and secrets.")
    parser.add_argument("skill_dir", help="skill directory to validate")
    parser.add_argument(
        "--repo-root", required=True,
        help="repository root used for relative paths")
    args = parser.parse_args(argv)
    findings = validate_repository(args.skill_dir, args.repo_root)
    if not findings:
        print(SUCCESS_LINE)
        return 0
    for finding in findings:
        print("%s %s" % (finding.code, finding.path))
    return 1


def _walk_findings(root):
    findings = []
    for base, dirs, files in os.walk(root):
        base_path = pathlib.Path(base)
        rel_base = base_path.relative_to(root).as_posix()
        kept_dirs = []
        for name in dirs:
            if name == ".git":
                continue
            if name == "__pycache__":
                rel = name if rel_base == "." else rel_base + "/" + name
                findings.append(Finding(CODE_ARTIFACT_PYCACHE, rel))
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            file_path = base_path / name
            rel = name if rel_base == "." else rel_base + "/" + name
            if name.endswith(".pyc"):
                findings.append(Finding(CODE_ARTIFACT_PYC, rel))
                continue
            if not _is_text_candidate(file_path):
                continue
            encoding_finding = _encoding_finding(file_path, rel)
            if encoding_finding is not None:
                findings.append(encoding_finding)
                continue
            text = file_path.read_text(encoding="utf-8")
            secret_finding = _secret_finding(text, rel)
            if secret_finding is not None:
                findings.append(secret_finding)
    return findings


def _is_text_candidate(path):
    return path.name in _TEXT_NAMES or path.suffix in _TEXT_EXTENSIONS


def _encoding_finding(path, rel):
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return Finding(CODE_ENC_BOM, rel)
    if b"\r\n" in data:
        return Finding(CODE_ENC_CRLF, rel)
    if b"\r" in data:
        return Finding(CODE_ENC_BARE_CR, rel)
    if b"\x00" in data:
        return Finding(CODE_ENC_NUL, rel)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return Finding(CODE_ENC_UTF8, rel)
    return None


def _secret_finding(text, rel):
    for code, pattern in _SECRET_PATTERNS:
        if pattern.search(text) is not None:
            return Finding(code, rel)
    return None


def _skill_metadata_finding(skill_md, rel):
    text = skill_md.read_text(encoding="utf-8")
    if _parse_frontmatter(text) is None:
        return Finding(CODE_META_SKILL, rel)
    return None


def _openai_metadata_finding(openai_yaml, rel):
    text = openai_yaml.read_text(encoding="utf-8")
    if _parse_openai_interface(text) is None:
        return Finding(CODE_META_OPENAI, rel)
    return None


def _parse_frontmatter(text):
    if not text.startswith("---\n"):
        return None
    fields = {}
    for line in text[4:].split("\n"):
        if line == "---":
            break
        if not line:
            continue
        if ":" not in line:
            return None
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value or not re.fullmatch(r"[a-z0-9-]+", key):
            return None
        if key in fields:
            return None
        fields[key] = value
    else:
        return None
    if set(fields) - _SKILL_ALLOWED_KEYS:
        return None
    if "name" not in fields or "description" not in fields:
        return None
    name = fields["name"]
    description = fields["description"]
    if not 1 <= len(name) <= 64 or _NAME_PATTERN.fullmatch(name) is None:
        return None
    if not 1 <= len(description) <= 1024:
        return None
    if "<" in description or ">" in description:
        return None
    return fields


def _parse_openai_interface(text):
    interface = {}
    policy_seen = False
    section = None
    for line in text.split("\n"):
        if not line:
            continue
        if line == "interface:":
            if section is not None:
                return None
            section = "interface"
            continue
        if line == "policy:":
            if section != "interface":
                return None
            section = "policy"
            policy_seen = True
            continue
        match = re.fullmatch(r"  ([a-z_]+): (.*)", line)
        if match is None:
            return None
        key, value = match.group(1), match.group(2)
        if section == "policy":
            if key != "allow_implicit_invocation" or value not in ("true", "false"):
                return None
            continue
        if section != "interface" or key not in (
                "display_name", "short_description", "default_prompt"):
            return None
        if _QUOTED_VALUE.fullmatch(value) is None:
            return None
        interface[key] = _QUOTED_VALUE.fullmatch(value).group(1)
    if section not in ("interface", "policy"):
        return None
    if policy_seen and section != "policy":
        return None
    for key in ("display_name", "short_description", "default_prompt"):
        if key not in interface:
            return None
    if not interface["display_name"]:
        return None
    short = interface["short_description"]
    if not 25 <= len(short) <= 64:
        return None
    if _MARKER not in interface["default_prompt"]:
        return None
    return interface


if __name__ == "__main__":
    sys.exit(main())
