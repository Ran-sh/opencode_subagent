"""RED tests for the README synchronization checker (OC-20260813-01, REV-2).

Behavior contract under test (PKG-OC-20260813-01-V2):

- R1: ``scripts/check_readme_sync.py`` exits 0 for a synchronized pair and
  exits 1 for marker, reciprocal-link, ordered H2-section or contract-token
  divergence, without printing README contents or absolute paths.
- R2: with ``--base <rev>``, changed paths are the union of
  ``git diff --name-only <rev> --`` and ``git ls-files --others
  --exclude-standard``; exactly one changed README fails, both or neither
  pass, and an invalid revision fails.

These tests are RED while the checker is missing: every subprocess
invocation then exits with code 2 ("can't open file"), which fails the
launch guard in every test.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check_readme_sync.py"

MARKER = "<!-- README_SYNC: 2026-08-13.1 -->"
PAIR_COMMENT = ("<!-- README.md and README.en.md are maintained as "
                "a synchronized pair. -->")
ZH_LINK_LINE = ('  <strong>简体中文</strong> · '
                '<a href="./README.en.md">English</a>')
EN_LINK_LINE = ('  <a href="./README.md">简体中文</a> · '
                '<strong>English</strong>')

H2_PAIRS = (
    ("五步快速开始", "Five-step quick start"),
    ("主要功能", "Features"),
    ("架构", "Architecture"),
    ("安装", "Installation"),
    ("首次配置", "Initial setup"),
    ("日常编码派发", "Daily coding delegation"),
    ("支持模型", "Supported models"),
    ("模型与思考档位切换", "Switching models and reasoning effort"),
    ("管理命令", "Management commands"),
    ("协议与安全边界", "Protocol and security boundaries"),
    ("验证", "Verification"),
    ("数据来源", "Data sources"),
    ("致谢、商标与许可", "Credits, trademarks, and license"),
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

ZH_SENTINEL = "UNIQUE_ZH_SENTINEL_正文"
EN_SENTINEL = "UNIQUE_EN_SENTINEL_BODY"


def build_text(lang, headings, tokens, marker=MARKER, link=True,
               sentinel=None):
    """Build one README side with UTF-8 content and the given structure."""
    if lang == "zh":
        title = "简体中文测试 README"
        link_line = ZH_LINK_LINE if link else (
            '  <strong>简体中文</strong> · English')
    else:
        title = "English test README"
        link_line = EN_LINK_LINE if link else (
            '  简体中文 · <strong>English</strong>')
    lines = [
        f"# {title}",
        "",
        link_line,
        "",
        marker,
        PAIR_COMMENT,
        "",
        "Contract tokens present in this file:",
    ]
    lines.extend("- " + token for token in tokens)
    lines.append("")
    for heading in headings:
        lines.extend([f"## {heading}", "", f"Body for {heading}. {sentinel}",
                      ""])
    return "\n".join(lines) + "\n"


def write_pair(root, *, en_marker=MARKER, zh_link=True, en_link=True,
               en_headings=None, en_tokens=None):
    """Write a README pair into root; return the written texts."""
    root.mkdir(parents=True, exist_ok=True)
    zh_headings = [zh for zh, _ in H2_PAIRS]
    en_headings = [en for _, en in H2_PAIRS] if en_headings is None \
        else en_headings
    en_tokens = TOKENS if en_tokens is None else en_tokens
    zh = build_text("zh", zh_headings, TOKENS, marker=MARKER,
                    link=zh_link, sentinel=ZH_SENTINEL)
    en = build_text("en", en_headings, en_tokens, marker=en_marker,
                    link=en_link, sentinel=EN_SENTINEL)
    (root / "README.md").write_text(zh, encoding="utf-8", newline="\n")
    (root / "README.en.md").write_text(en, encoding="utf-8", newline="\n")
    return zh, en


def run_checker(root, *args):
    """Run the public CLI against root (repository-relative diagnostics)."""
    return subprocess.run(
        [sys.executable, "-B", str(CHECKER), "--repo-root", str(root),
         *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )


def init_repo(test, repo):
    repo.mkdir(parents=True, exist_ok=True)
    for args in (("init", "-q"),
                 ("config", "user.name", "sync-test"),
                 ("config", "user.email", "sync-test@example.invalid")):
        proc = _git(repo, *args)
        test.assertEqual(proc.returncode, 0, proc.stderr)


def commit(test, repo, message, files):
    proc = _git(repo, "add", *files)
    test.assertEqual(proc.returncode, 0, proc.stderr)
    proc = _git(repo, "commit", "-q", "-m", message)
    test.assertEqual(proc.returncode, 0, proc.stderr)


def assert_launched(test, proc):
    test.assertIn(
        proc.returncode, (0, 1),
        "checker must run and exit 0/1; got %d: %s"
        % (proc.returncode, proc.stderr.strip()),
    )


def assert_clean_output(test, proc, root):
    combined = (proc.stdout or "") + (proc.stderr or "")
    test.assertNotIn(ZH_SENTINEL, combined)
    test.assertNotIn(EN_SENTINEL, combined)
    test.assertNotIn(str(root), combined)


class ReadmeSyncCheckerTest(unittest.TestCase):

    def test_valid_pair_exits_zero_without_mutating_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            write_pair(root)
            before = ((root / "README.md").read_bytes(),
                      (root / "README.en.md").read_bytes())
            proc = run_checker(root)
            assert_launched(self, proc)
            assert_clean_output(self, proc, root)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((root / "README.md").read_bytes(), before[0])
            self.assertEqual((root / "README.en.md").read_bytes(), before[1])

    def test_marker_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            write_pair(root, en_marker="<!-- README_SYNC: 2026-08-13.2 -->")
            proc = run_checker(root)
            assert_launched(self, proc)
            assert_clean_output(self, proc, root)
            self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_missing_reciprocal_link_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for variant, kw in (("zh-missing", {"zh_link": False}),
                                ("en-missing", {"en_link": False})):
                with self.subTest(variant=variant):
                    write_pair(root, **kw)
                    proc = run_checker(root)
                    assert_launched(self, proc)
                    assert_clean_output(self, proc, root)
                    self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_section_missing_or_out_of_order_fails(self):
        en_headings = [en for _, en in H2_PAIRS]
        variants = (
            ("missing", en_headings[:1] + en_headings[2:]),
            ("out-of-order",
             [en_headings[1], en_headings[0]] + en_headings[2:]),
        )
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for variant, headings in variants:
                with self.subTest(variant=variant):
                    write_pair(root, en_headings=headings)
                    proc = run_checker(root)
                    assert_launched(self, proc)
                    assert_clean_output(self, proc, root)
                    self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_missing_contract_token_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for token in TOKENS:
                with self.subTest(token=token):
                    missing = tuple(t for t in TOKENS if t != token)
                    write_pair(root, en_tokens=missing)
                    proc = run_checker(root)
                    assert_launched(self, proc)
                    assert_clean_output(self, proc, root)
                    self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_base_both_or_neither_changed_passes(self):
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "repo"
            init_repo(self, repo)
            zh, en = write_pair(repo)
            commit(self, repo, "initial pair",
                   ("README.md", "README.en.md"))

            with self.subTest(variant="neither-changed"):
                proc = run_checker(repo, "--base", "HEAD")
                assert_launched(self, proc)
                assert_clean_output(self, proc, repo)
                self.assertEqual(proc.returncode, 0, proc.stderr)

            with self.subTest(variant="both-tracked-changed"):
                (repo / "README.md").write_text(
                    zh + "\nzh update\n", encoding="utf-8", newline="\n")
                (repo / "README.en.md").write_text(
                    en + "\nen update\n", encoding="utf-8", newline="\n")
                proc = run_checker(repo, "--base", "HEAD")
                assert_launched(self, proc)
                assert_clean_output(self, proc, repo)
                self.assertEqual(proc.returncode, 0, proc.stderr)

        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "repo"
            init_repo(self, repo)
            zh, _en = write_pair(repo)
            commit(self, repo, "zh only", ("README.md",))
            (repo / "README.md").write_text(
                zh + "\nzh update\n", encoding="utf-8", newline="\n")
            with self.subTest(variant="tracked-zh-plus-untracked-en"):
                proc = run_checker(repo, "--base", "HEAD")
                assert_launched(self, proc)
                assert_clean_output(self, proc, repo)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_base_only_one_changed_fails(self):
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "repo"
            init_repo(self, repo)
            zh, en = write_pair(repo)
            commit(self, repo, "initial pair",
                   ("README.md", "README.en.md"))

            with self.subTest(variant="only-zh"):
                (repo / "README.md").write_text(
                    zh + "\nzh update\n", encoding="utf-8", newline="\n")
                proc = run_checker(repo, "--base", "HEAD")
                assert_launched(self, proc)
                assert_clean_output(self, proc, repo)
                self.assertEqual(proc.returncode, 1, proc.stderr)

            with self.subTest(variant="only-en"):
                (repo / "README.md").write_text(
                    zh, encoding="utf-8", newline="\n")
                (repo / "README.en.md").write_text(
                    en + "\nen update\n", encoding="utf-8", newline="\n")
                proc = run_checker(repo, "--base", "HEAD")
                assert_launched(self, proc)
                assert_clean_output(self, proc, repo)
                self.assertEqual(proc.returncode, 1, proc.stderr)

        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "repo"
            init_repo(self, repo)
            write_pair(repo)
            commit(self, repo, "zh only", ("README.md",))
            with self.subTest(variant="untracked-en-only"):
                proc = run_checker(repo, "--base", "HEAD")
                assert_launched(self, proc)
                assert_clean_output(self, proc, repo)
                self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_base_invalid_revision_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            write_pair(root)
            proc = run_checker(root, "--base",
                               "no-such-revision-OC-20260813")
            assert_launched(self, proc)
            assert_clean_output(self, proc, root)
            self.assertEqual(proc.returncode, 1, proc.stderr)


if __name__ == "__main__":
    unittest.main()
