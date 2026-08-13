#!/usr/bin/env python3
"""RED behavior contract tests for the repository quick validator
(DS-20260812-24 REV-3).

The repository validator scripts/quick_validate.py does not exist yet, so
every test starts with a callable gate that fails with the fixed prefix
``validator_missing``.  The current module therefore yields six failures
(never errors) until the GREEN phase implements the validator.

All repositories are synthetic temporary directories; no real credentials,
environment variables, network, subprocess or worktree state is touched.
Secret fixtures are assembled from fragments at runtime so the source file
contains no complete value a future secret scanner could flag.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VALIDATOR_PATH = _REPO_ROOT / "scripts" / "quick_validate.py"

# Rule codes fixed by this RED contract.  The GREEN validator must emit
# exactly these codes in findings and CLI output lines.
CODE_ENC_BOM = "ENC_BOM"
CODE_ENC_CRLF = "ENC_CRLF"
CODE_ENC_BARE_CR = "ENC_BARE_CR"
CODE_ENC_NUL = "ENC_NUL"
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
SUCCESS_LINE = "repository validation passed"

_MINIMAL_SKILL = (
    "---\n"
    "name: demo-skill\n"
    "description: A minimal skill used only by synthetic validator tests.\n"
    "---\n"
    "\n"
    "# Demo Skill\n"
)
_OPENAI_SHORT = "A demo OpenAI interface entry"  # 30 chars, inside 25..64
_MINIMAL_OPENAI = (
    "interface:\n"
    '  display_name: "Demo Skill"\n'
    '  short_description: "' + _OPENAI_SHORT + '"\n'
    '  default_prompt: "Use $codex-opencode-go-subagent for demo work."\n'
)


def _write(path: Path, data) -> None:
    """Write bytes or UTF-8 text with LF-only line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8", newline="\n")


def _finding_lines(findings):
    """Serialize findings as 'CODE relative/path' lines (CLI contract)."""
    return ["%s %s" % (finding.code, finding.path) for finding in findings]


class QuickValidateRedTests(unittest.TestCase):
    """Six RED behaviour contracts for scripts/quick_validate.py."""

    def _require_validator(self):
        """Unified callable gate; every test calls this first."""
        if not _VALIDATOR_PATH.is_file():
            self.fail("validator_missing: scripts/quick_validate.py is absent")
        spec = importlib.util.spec_from_file_location(
            "quick_validate_under_test", _VALIDATOR_PATH)
        if spec is None or spec.loader is None:
            self.fail("validator_missing: cannot create import spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ("validate_repository", "main"):
            if not callable(getattr(module, name, None)):
                self.fail("validator_missing: %s is not callable" % name)
        if not isinstance(getattr(module, "Finding", None), type):
            self.fail("validator_missing: Finding is not a class")
        return module

    def _make_valid_repo(self, directory):
        skill = directory / "demo-skill"
        _write(skill / "SKILL.md", _MINIMAL_SKILL)
        _write(skill / "agents" / "openai.yaml", _MINIMAL_OPENAI)
        _write(directory / "notes.txt", "plain text\n")
        return skill

    def _snapshot(self, directory):
        snap = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                snap[path.relative_to(directory).as_posix()] = path.read_bytes()
        return snap

    def _run_cli(self, module, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = module.main(argv)
        return stdout.getvalue(), stderr.getvalue(), rc

    def test_valid_repository_is_zero_write(self):
        module = self._require_validator()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = self._make_valid_repo(root)
            before = self._snapshot(root)
            findings = module.validate_repository(
                str(skill), repo_root=str(root))
            after = self._snapshot(root)
            self.assertEqual(findings, [])
            self.assertEqual(before, after)

    def test_encoding_failures_are_relative_and_redacted(self):
        module = self._require_validator()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = self._make_valid_repo(root)
            _write(root / "bom.md", b"\xef\xbb\xbfBOM_MARKER_123\n")
            _write(root / "crlf.txt", b"CRLF_MARKER_456\r\nsecond\r\n")
            _write(root / "barecr.txt", b"BARE_CR_MARKER_789\rsecond\n")
            _write(root / "nul.txt", b"NUL_MARKER_abc\x00tail\n")
            findings = module.validate_repository(
                str(skill), repo_root=str(root))
            by_path = {finding.path: finding.code for finding in findings}
            self.assertEqual(by_path.get("bom.md"), CODE_ENC_BOM)
            self.assertEqual(by_path.get("crlf.txt"), CODE_ENC_CRLF)
            self.assertEqual(by_path.get("barecr.txt"), CODE_ENC_BARE_CR)
            self.assertEqual(by_path.get("nul.txt"), CODE_ENC_NUL)
            text = "\n".join(_finding_lines(findings))
            for marker in ("BOM_MARKER_123", "CRLF_MARKER_456",
                           "BARE_CR_MARKER_789", "NUL_MARKER_abc"):
                self.assertNotIn(marker, text)
            self.assertNotIn(str(root), text)
            self.assertNotIn(root.as_posix(), text)
            for finding in findings:
                self.assertFalse(Path(finding.path).is_absolute())

    def test_skill_and_openai_metadata_contract(self):
        module = self._require_validator()
        skill_without_name = (
            "---\n"
            "description: Missing name on purpose.\n"
            "---\n"
        )
        skill_without_description = (
            "---\n"
            "name: demo-skill\n"
            "---\n"
        )
        skill_disallowed_key = (
            "---\n"
            "name: demo-skill\n"
            "description: A minimal skill used only by synthetic validator tests.\n"
            "foo: bar\n"
            "---\n"
        )
        skill_allowed_optional = (
            "---\n"
            "name: demo-skill\n"
            "description: A minimal skill used only by synthetic validator tests.\n"
            "license: MIT\n"
            "metadata: demo-meta\n"
            "---\n"
        )
        openai_without_display = _MINIMAL_OPENAI.replace(
            '  display_name: "Demo Skill"\n', "")
        openai_without_short = _MINIMAL_OPENAI.replace(
            '  short_description: "' + _OPENAI_SHORT + '"\n', "")
        openai_without_prompt = _MINIMAL_OPENAI.replace(
            '  default_prompt: "Use $codex-opencode-go-subagent for demo work."\n',
            "")
        openai_short_too_short = _MINIMAL_OPENAI.replace(
            _OPENAI_SHORT, "too short")
        openai_short_too_long = _MINIMAL_OPENAI.replace(
            _OPENAI_SHORT, "x" * 70)
        openai_missing_marker = _MINIMAL_OPENAI.replace(
            "$codex-opencode-go-subagent", "the-demo-skill")
        openai_policy_true = _MINIMAL_OPENAI + (
            "policy:\n"
            "  allow_implicit_invocation: true\n"
        )
        openai_policy_false = _MINIMAL_OPENAI + (
            "policy:\n"
            "  allow_implicit_invocation: false\n"
        )
        openai_policy_invalid = _MINIMAL_OPENAI + (
            "policy:\n"
            "  allow_implicit_invocation: maybe\n"
        )
        cases = [
            ("skill_without_name", skill_without_name, _MINIMAL_OPENAI,
             CODE_META_SKILL),
            ("skill_without_description", skill_without_description,
             _MINIMAL_OPENAI, CODE_META_SKILL),
            ("skill_disallowed_key", skill_disallowed_key, _MINIMAL_OPENAI,
             CODE_META_SKILL),
            ("skill_allowed_optional", skill_allowed_optional, _MINIMAL_OPENAI,
             None),
            ("openai_without_display", _MINIMAL_SKILL, openai_without_display,
             CODE_META_OPENAI),
            ("openai_without_short", _MINIMAL_SKILL, openai_without_short,
             CODE_META_OPENAI),
            ("openai_without_prompt", _MINIMAL_SKILL, openai_without_prompt,
             CODE_META_OPENAI),
            ("openai_short_too_short", _MINIMAL_SKILL, openai_short_too_short,
             CODE_META_OPENAI),
            ("openai_short_too_long", _MINIMAL_SKILL, openai_short_too_long,
             CODE_META_OPENAI),
            ("openai_missing_marker", _MINIMAL_SKILL, openai_missing_marker,
             CODE_META_OPENAI),
            ("openai_policy_true", _MINIMAL_SKILL, openai_policy_true, None),
            ("openai_policy_false", _MINIMAL_SKILL, openai_policy_false, None),
            ("openai_policy_invalid", _MINIMAL_SKILL, openai_policy_invalid,
             CODE_META_OPENAI),
        ]
        for label, skill_text, openai_text, expected in cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    skill = root / "demo-skill"
                    _write(skill / "SKILL.md", skill_text)
                    _write(skill / "agents" / "openai.yaml", openai_text)
                    _write(root / "notes.txt", "plain text\n")
                    findings = module.validate_repository(
                        str(skill), repo_root=str(root))
                    codes = [finding.code for finding in findings]
                    if expected is None:
                        self.assertEqual(codes, [])
                    else:
                        self.assertIn(expected, codes)

        missing_cases = [
            ("missing_skill_md", "agents/openai.yaml", _MINIMAL_OPENAI,
             CODE_META_SKILL, "demo-skill/SKILL.md"),
            ("missing_openai_yaml", "SKILL.md", _MINIMAL_SKILL,
             CODE_META_OPENAI, "demo-skill/agents/openai.yaml"),
        ]
        for label, present_rel, present_text, expected_code, expected_path in missing_cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    skill = root / "demo-skill"
                    _write(skill / present_rel, present_text)
                    findings = module.validate_repository(
                        str(skill), repo_root=str(root))
                    self.assertEqual([(f.code, f.path) for f in findings],
                                     [(expected_code, expected_path)])
        close_eof = (
            "---\n"
            "name: demo-skill\n"
            "description: A minimal skill used only by synthetic validator tests.\n"
            "---")
        close_lf = close_eof + "\n"
        close_trailing = (
            "---\n"
            "name: demo-skill\n"
            "description: A minimal skill used only by synthetic validator tests.\n"
            "--- trailing\n")
        duplicate_key = _MINIMAL_SKILL.replace(
            "name: demo-skill\n", "name: demo-skill\nname: demo-skill\n", 1)
        strict_cases = [
            ("frontmatter_close_eof", close_eof, None),
            ("frontmatter_close_lf", close_lf, None),
            ("frontmatter_close_trailing", close_trailing, CODE_META_SKILL),
            ("frontmatter_duplicate_key", duplicate_key, CODE_META_SKILL),
        ]
        for label, skill_text, expected in strict_cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    skill = root / "demo-skill"
                    _write(skill / "SKILL.md", skill_text)
                    _write(skill / "agents" / "openai.yaml", _MINIMAL_OPENAI)
                    findings = module.validate_repository(
                        str(skill), repo_root=str(root))
                    codes = [finding.code for finding in findings]
                    if expected is None:
                        self.assertEqual(codes, [])
                    else:
                        self.assertIn(expected, codes)

    def test_high_confidence_secrets_are_detected_without_echo(self):
        module = self._require_validator()
        pem = ("-----BEGIN " + "PRIVATE KEY-----\n"
               + "A" * 64 + "\n"
               + "-----END " + "PRIVATE KEY-----\n")
        openai_token = "sk-" + ("Ab1Cd2Ef3Gh4Ij5Kl6" * 3)
        github_pat = "ghp_" + ("A1b2C3d4E5f6G7h8I9j0" * 3)
        github_oauth = "gho_" + ("aB1cD2eF3gH4iJ5kL6m" * 3)
        aws_key = "AKIA" + "0A1B2C3D4E5F6789"
        workspace_id = "wrk_" + "ABCDEF0123456789"
        fixtures = [
            ("pem.txt", pem, CODE_SECRET_PEM),
            ("openai.txt", openai_token, CODE_SECRET_OPENAI),
            ("github_pat.txt", github_pat, CODE_SECRET_GITHUB),
            ("github_oauth.txt", github_oauth, CODE_SECRET_GITHUB),
            ("aws.txt", aws_key, CODE_SECRET_AWS),
            ("workspace.txt", workspace_id, CODE_SECRET_WORKSPACE),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = self._make_valid_repo(root)
            for name, value, _code in fixtures:
                _write(root / name, value)
            findings = module.validate_repository(
                str(skill), repo_root=str(root))
            by_path = {finding.path: finding.code for finding in findings}
            for name, value, code in fixtures:
                self.assertEqual(by_path.get(name), code)
                self.assertNotIn(name, value)
            text = "\n".join(_finding_lines(findings))
            for _name, value, _code in fixtures:
                self.assertNotIn(value, text)
                self.assertNotIn(value.rstrip("\n"), text)
            self.assertNotIn(str(root), text)
            self.assertNotIn(root.as_posix(), text)
            for finding in findings:
                self.assertFalse(Path(finding.path).is_absolute())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = self._make_valid_repo(root)
            _write(root / "LICENSE", pem)
            _write(root / "NOTICE", openai_token)
            _write(root / "CHANGELOG", openai_token)
            _write(root / ".git" / "LICENSE", pem)
            findings = module.validate_repository(
                str(skill), repo_root=str(root))
            by_path = {finding.path: finding.code for finding in findings}
            self.assertEqual(by_path.get("LICENSE"), CODE_SECRET_PEM)
            self.assertEqual(by_path.get("NOTICE"), CODE_SECRET_OPENAI)
            self.assertNotIn("CHANGELOG", by_path)
            self.assertNotIn(".git/LICENSE", by_path)
            text = "\n".join(_finding_lines(findings))
            self.assertNotIn(pem, text)
            self.assertNotIn(openai_token, text)

    def test_generated_artifacts_rejected_at_any_depth_but_git_skipped(self):
        module = self._require_validator()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = self._make_valid_repo(root)
            _write(root / "__pycache__" / "a.pyc", b"pyc")
            _write(root / "nested" / "deep" / "__pycache__" / "b.pyc", b"pyc")
            _write(root / "stray.pyc", b"pyc")
            _write(root / ".git" / "__pycache__" / "c.pyc", b"pyc")
            _write(root / ".git" / "stray2.pyc", b"pyc")
            findings = module.validate_repository(
                str(skill), repo_root=str(root))
            by_path = {finding.path: finding.code for finding in findings}
            self.assertEqual(by_path.get("__pycache__"), CODE_ARTIFACT_PYCACHE)
            self.assertEqual(
                by_path.get("nested/deep/__pycache__"), CODE_ARTIFACT_PYCACHE)
            self.assertEqual(by_path.get("stray.pyc"), CODE_ARTIFACT_PYC)
            git_paths = [path for path in by_path if path.startswith(".git/")]
            self.assertEqual(git_paths, [])
            for finding in findings:
                self.assertFalse(Path(finding.path).is_absolute())

    def test_cli_success_and_failure_output_contract(self):
        module = self._require_validator()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = self._make_valid_repo(root)
            out, err, rc = self._run_cli(
                module, [str(skill), "--repo-root", str(root)])
            self.assertEqual(rc, 0)
            self.assertEqual(out, SUCCESS_LINE + "\n")
            self.assertEqual(err, "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = self._make_valid_repo(root)
            _write(root / "bom.txt", b"\xef\xbb\xbfBOM_CLI_MARKER\n")
            _write(root / "nul.txt", b"NUL_CLI_MARKER\x00tail\n")
            out, err, rc = self._run_cli(
                module, [str(skill), "--repo-root", str(root)])
            self.assertEqual(rc, 1)
            expected = sorted([
                CODE_ENC_BOM + " bom.txt",
                CODE_ENC_NUL + " nul.txt",
            ])
            self.assertEqual(out.splitlines(), expected)
            self.assertNotIn(str(root), out)
            self.assertNotIn(root.as_posix(), out)
            self.assertNotIn("BOM_CLI_MARKER", out)
            self.assertNotIn("NUL_CLI_MARKER", out)
            self.assertNotIn(str(root), err)
            self.assertNotIn(root.as_posix(), err)
            self.assertNotIn("BOM_CLI_MARKER", err)
            self.assertNotIn("NUL_CLI_MARKER", err)

        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory)
            root = top / "repo"
            root.mkdir()
            outside = top / "outside"
            self._make_valid_repo(outside)
            _write(root / "not-a-skill.txt", "plain text\n")
            missing_root = top / "missing-repo"
            invalid_cases = [
                ("missing_repo_root", str(root / "demo-skill"), str(missing_root)),
                ("skill_outside_root", str(outside / "demo-skill"), str(root)),
                ("skill_is_plain_file", str(root / "not-a-skill.txt"), str(root)),
            ]
            for label, skill_arg, repo_arg in invalid_cases:
                with self.subTest(case=label):
                    try:
                        out, err, rc = self._run_cli(
                            module, [skill_arg, "--repo-root", repo_arg])
                    except Exception as exc:
                        self.fail("%s raised: %r" % (label, exc))
                    self.assertEqual(rc, 1)
                    self.assertEqual(out, "PATH_INVALID .\n")
                    self.assertEqual(err, "")
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = self._make_valid_repo(root)
            marker = "SENSITIVE_OSERROR_MARKER_7f3a"
            try:
                with mock.patch.object(
                        module, "_walk_findings",
                        side_effect=OSError(marker)):
                    findings = module.validate_repository(
                        str(skill), repo_root=str(root))
            except Exception as exc:
                self.fail("os error escaped: %r" % (exc,))
            self.assertEqual([(f.code, f.path) for f in findings],
                             [(CODE_PATH_INVALID, ".")])
            serialized = "\n".join(_finding_lines(findings))
            self.assertNotIn(marker, serialized)
            self.assertNotIn(str(root), serialized)


if __name__ == "__main__":
    unittest.main()
