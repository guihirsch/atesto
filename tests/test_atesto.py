"""Acceptance tests for Atesto v0."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ENV = {**os.environ, "PYTHONPATH": str(SRC)}
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from atesto import core


def package_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise AssertionError("version ausente em pyproject.toml")
    return match.group(1)


def cli(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "atesto", *args],
        cwd=repo,
        env={**ENV, **(env or {})},
        text=True,
        capture_output=True,
        check=False,
    )


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def make_repo(tmp: str) -> Path:
    repo = Path(tmp) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "atesto@example.com")
    git(repo, "config", "user.name", "Atesto")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-qm", "base")
    return repo


def init(
    repo: Path, *, require: str = "proof", test_cmd: str = ""
) -> None:
    result = cli(
        repo, "init", "--require", require, "--test-cmd", test_cmd
    )
    assert result.returncode == 0, result.stdout + result.stderr


def proof(repo: Path, *, command: str = "print('ok')") -> None:
    result = cli(
        repo,
        "proof",
        "demo",
        "--",
        sys.executable,
        "-c",
        command,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def commit(repo: Path, message: str = "evidence") -> None:
    git(repo, "add", ".")
    git(repo, "commit", "-qm", message)


FAKE_GH_SCRIPT = """\
#!{python}
import json
import os
import sys

with open(os.environ["FAKE_PROVIDER_SCENARIO"], encoding="utf-8") as handle:
    scenario = json.load(handle)
args = sys.argv[1:]
with open(os.environ["FAKE_PROVIDER_LOG"], "a", encoding="utf-8") as log:
    log.write(
        json.dumps(
            {{
                "args": args,
                "GH_REPO": os.environ.get("GH_REPO"),
                "cwd": os.getcwd(),
            }}
        )
    )
    log.write("\\n")
if args[:2] == ["pr", "view"]:
    key = "pr_view"
elif args[:1] == ["api"]:
    key = "api"
else:
    sys.stderr.write("fake gh: comando inesperado: " + " ".join(args) + "\\n")
    raise SystemExit(97)
payload = scenario[key]
exit_code = payload.get("exit", 0)
if exit_code:
    sys.stdout.write(payload.get("stdout", ""))
    sys.stderr.write(payload.get("stderr", ""))
    raise SystemExit(exit_code)
if "stdout" in payload:
    sys.stdout.write(payload["stdout"])
else:
    json.dump(payload.get("json"), sys.stdout)
"""


FAKE_GLAB_SCRIPT = """\
#!{python}
import json
import os
import sys

with open(os.environ["FAKE_PROVIDER_SCENARIO"], encoding="utf-8") as handle:
    scenario = json.load(handle)
args = sys.argv[1:]
with open(os.environ["FAKE_PROVIDER_LOG"], "a", encoding="utf-8") as log:
    log.write(
        json.dumps(
            {{
                "args": args,
                "cwd": os.getcwd(),
                "CI_PROJECT_ID": os.environ.get("CI_PROJECT_ID"),
                "CI_MERGE_REQUEST_PROJECT_ID": os.environ.get(
                    "CI_MERGE_REQUEST_PROJECT_ID"
                ),
            }}
        )
    )
    log.write("\\n")
endpoint = args[-1] if args else ""
if endpoint.endswith("/approvals"):
    key = "approvals"
elif "/merge_requests/" in endpoint:
    key = "mr"
else:
    sys.stderr.write(
        "fake glab: comando inesperado: " + " ".join(args) + "\\n"
    )
    raise SystemExit(97)
payload = scenario[key]
exit_code = payload.get("exit", 0)
if exit_code:
    sys.stdout.write(payload.get("stdout", ""))
    sys.stderr.write(payload.get("stderr", ""))
    raise SystemExit(exit_code)
if "stdout" in payload:
    sys.stdout.write(payload["stdout"])
else:
    json.dump(payload.get("json"), sys.stdout)
"""


def fake_provider_cli(
    tmp: str, name: str, script: str, scenario: dict
) -> dict[str, str]:
    """Instala um CLI falso no PATH e retorna o env para o CLI do Atesto."""
    bin_dir = Path(tmp) / f"fake-{name}-bin"
    bin_dir.mkdir(exist_ok=True)
    executable = bin_dir / name
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    scenario_path = Path(tmp) / f"fake-{name}-scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    log_path = Path(tmp) / f"fake-{name}.log"
    path = os.environ.get("PATH", "")
    return {
        "PATH": f"{bin_dir}{os.pathsep}{path}",
        "FAKE_PROVIDER_SCENARIO": str(scenario_path),
        "FAKE_PROVIDER_LOG": str(log_path),
    }


def fake_gh(tmp: str, *, pr_view: dict, api: dict) -> dict[str, str]:
    return fake_provider_cli(
        tmp,
        "gh",
        FAKE_GH_SCRIPT.format(python=sys.executable),
        {"pr_view": pr_view, "api": api},
    )


def provider_calls(tmp: str, name: str) -> list[dict[str, object]]:
    log_path = Path(tmp) / f"fake-{name}.log"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def gh_calls(tmp: str) -> list[dict[str, object]]:
    return provider_calls(tmp, "gh")


def github_pr(head: str, url: str, **fields: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": 42,
        "url": url,
        "headRefOid": head,
        "reviewDecision": "APPROVED",
    }
    payload.update(fields)
    return {"json": payload}


def github_review(
    review_id: int,
    login: str,
    state: str,
    commit_id: str | None,
    submitted_at: str,
    *,
    user_type: str = "User",
) -> dict[str, object]:
    return {
        "id": review_id,
        "user": {"login": login, "type": user_type},
        "state": state,
        "commit_id": commit_id,
        "submitted_at": submitted_at,
    }


def github_reviews(*pages: list[dict[str, object]]) -> dict[str, object]:
    return {"json": list(pages)}


def fake_glab(
    tmp: str,
    *,
    mr: dict,
    approvals: dict,
    project: str = "",
    merge_request_project: str = "",
) -> dict[str, str]:
    env = fake_provider_cli(
        tmp,
        "glab",
        FAKE_GLAB_SCRIPT.format(python=sys.executable),
        {"mr": mr, "approvals": approvals},
    )
    # Keep the tests deterministic: the developer/CI environment may export
    # GitLab variables of its own.
    env["CI_PROJECT_ID"] = project
    env["CI_MERGE_REQUEST_PROJECT_ID"] = merge_request_project
    return env


def glab_calls(tmp: str) -> list[dict[str, object]]:
    return provider_calls(tmp, "glab")


def gitlab_mr(head: str, url: str, **fields: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "sha": head,
        "detailed_merge_status": "mergeable",
        "blocking_discussions_resolved": True,
        "web_url": url,
    }
    payload.update(fields)
    return {"json": payload}


def gitlab_approvals(
    *logins: str, approved: bool = True
) -> dict[str, object]:
    return {
        "json": {
            "approved": approved,
            "approved_by": [
                {"user": {"id": index + 1, "username": login}}
                for index, login in enumerate(logins)
            ],
        }
    }


class AtestoTest(unittest.TestCase):
    def test_proof_check_and_artifact_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("atesto: ok", result.stdout)

    def test_review_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("review.json ausente", result.stdout)

            result = cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            commit(repo, "review")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stale_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            self.assertEqual(cli(repo, "check").returncode, 0)

            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            commit(repo, "code")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("prova stale", result.stdout)

    def test_failed_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            result = cli(
                repo,
                "proof",
                "demo",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(3)",
            )
            self.assertEqual(result.returncode, 3)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("exit=3", result.stdout)

    def test_output_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            output = repo / "docs" / "atesto" / "demo" / "proof-output.txt"
            output.write_text("adulterado\n", encoding="utf-8")
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("output", result.stdout)

    def test_review_opcional_stale_falha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "CHANGES_REQUESTED",
                "--reviewer",
                "reviewer",
            )
            commit(repo, "review opcional")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("APPROVED", result.stdout)

    def test_output_sha_mesmo_tamanho(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            output = repo / "docs" / "atesto" / "demo" / "proof-output.txt"
            output.write_text("no\n", encoding="utf-8")
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("sha256", result.stdout)

    def test_review_symlink_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            review = repo / "docs" / "atesto" / "demo" / "review.json"
            original = Path(tmp) / "outside-review.json"
            original.write_bytes(review.read_bytes())
            review.unlink()
            review.symlink_to(original)
            commit(repo, "review symlink")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("review.json", result.stdout)

    def test_review_tree_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            review = repo / "docs" / "atesto" / "demo" / "review.json"
            data = json.loads(review.read_text(encoding="utf-8"))
            data["tree_sha256"] = "0" * 64
            review.write_text(json.dumps(data), encoding="utf-8")
            commit(repo, "tamper")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("tree_sha256", result.stdout)

    def test_ci_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            commit(repo, "config")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            proof(repo)
            commit(repo, "evidence")
            cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            commit(repo, "review")
            result = cli(repo, "check", "--ci", "--base", base)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_review_stale_after_code_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            commit(repo, "review")
            (repo / "app.py").write_text("x = 9\n", encoding="utf-8")
            commit(repo, "code")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("stale", result.stdout + result.stderr)

    def test_ci_uses_base_config_not_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            commit(repo, "config")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            proof(repo)
            commit(repo, "evidence")
            config = json.loads(
                (repo / "atesto.json").read_text(encoding="utf-8")
            )
            config["require"] = ["proof"]
            (repo / "atesto.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            commit(repo, "weaken config")
            result = cli(repo, "check", "--ci", "--base", base)
            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "config do HEAD remove requisito", result.stdout + result.stderr
            )

    def test_ci_rejects_head_config_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            commit(repo, "config")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            (repo / "atesto.json").unlink()
            commit(repo, "remove config")
            result = cli(repo, "check", "--ci", "--base", base)
            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "config do Atesto ausente no HEAD",
                result.stdout + result.stderr,
            )

    def test_ci_rejects_added_test_cmd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            commit(repo, "config")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            proof(repo)
            commit(repo, "evidence")
            config = json.loads(
                (repo / "atesto.json").read_text(encoding="utf-8")
            )
            config["test_cmd"] = "echo added"
            (repo / "atesto.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            commit(repo, "add test cmd")
            result = cli(repo, "check", "--ci", "--base", base)
            self.assertEqual(result.returncode, 1)
            self.assertIn("test_cmd", result.stdout + result.stderr)

    def test_ci_uses_committed_head_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            commit(repo, "config")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            proof(repo)
            commit(repo, "evidence")
            config = json.loads(
                (repo / "atesto.json").read_text(encoding="utf-8")
            )
            config["require"] = ["proof"]
            (repo / "atesto.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            commit(repo, "weak config")
            config["require"] = ["proof", "review"]
            (repo / "atesto.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            result = cli(repo, "check", "--ci", "--base", base)
            self.assertEqual(result.returncode, 1)
            self.assertIn("config do HEAD remove", result.stdout + result.stderr)

    def test_hooks_install_fora_de_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = cli(Path(tmp), "hooks", "install")
            self.assertEqual(result.returncode, 2)
            self.assertIn("nao e um repo git", result.stderr)

    def test_ci_rejects_weakening_fields(self) -> None:
        cases = [
            ("min_reviewers", 2, 1, "min_reviewers"),
            ("artifacts_root", "docs/atesto", "docs/other", "artifacts_root"),
            ("fingerprint_excludes", [], ["vendor"], "excludes"),
            (
                "review_artifacts_paths",
                ["docs/atesto"],
                ["docs/atesto", "other"],
                "review_artifacts_paths",
            ),
        ]
        for field, base_value, head_value, expected in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                repo = make_repo(tmp)
                init(repo)
                config_path = repo / "atesto.json"
                config = json.loads(
                    config_path.read_text(encoding="utf-8")
                )
                config[field] = base_value
                config_path.write_text(json.dumps(config), encoding="utf-8")
                commit(repo, "base config")
                base = git(repo, "rev-parse", "HEAD")
                git(repo, "checkout", "-qb", "feature")
                proof(repo)
                commit(repo, "evidence")
                config = json.loads(
                    config_path.read_text(encoding="utf-8")
                )
                config[field] = head_value
                config_path.write_text(json.dumps(config), encoding="utf-8")
                commit(repo, "weaken")
                result = cli(repo, "check", "--ci", "--base", base)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    expected, result.stdout + result.stderr
                )

    def test_unknown_config_keys_reprova_nomeando_todas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["statechart"] = {"state": "done"}
            config["blast_radius"] = ["src"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 2)
            output = result.stdout + result.stderr
            self.assertIn("desconhecid", output)
            self.assertIn("statechart", output)
            self.assertIn("blast_radius", output)
            self.assertIn("migra", output.lower())
            self.assertIn("README.md", output)
            self.assertIn("CHANGELOG.md", output)

    def test_ci_unknown_config_key_reprova_base_e_head(self) -> None:
        for side in ("base", "head"):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as tmp:
                repo = make_repo(tmp)
                init(repo)
                if side == "base":
                    self._set_unknown_config_key(repo)
                if side == "head":
                    proof(repo)
                commit(repo, "config")
                base = git(repo, "rev-parse", "HEAD")
                git(repo, "checkout", "-qb", "feature")
                if side == "head":
                    self._set_unknown_config_key(repo)
                    commit(repo, "unknown key")
                result = cli(repo, "check", "--ci", "--base", base)
                self.assertEqual(result.returncode, 1)
                output = result.stdout + result.stderr
                expected = "config da base" if side == "base" else "config do HEAD"
                self.assertIn(expected, output)
                self.assertIn("bogus_key", output)

    def test_config_v1_com_excludes_legado_passa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["version"] = 1
            config.pop("fingerprint_excludes", None)
            config["excludes"] = ["vendor"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            (repo / "vendor").mkdir()
            (repo / "vendor" / "lib.py").write_text("v = 1\n", encoding="utf-8")
            commit(repo, "vendor")
            proof(repo)
            commit(repo, "evidence")
            (repo / "vendor" / "lib.py").write_text("v = 2\n", encoding="utf-8")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_init_escreve_config_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config = json.loads(
                (repo / "atesto.json").read_text(encoding="utf-8")
            )
            self.assertEqual(config["version"], 2)
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_init_writes_minimal_v2_config_documented_in_readme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config = json.loads(
                (repo / "atesto.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                config,
                {
                    "version": 2,
                    "artifacts_root": "docs/atesto",
                    "require": ["proof"],
                    "min_reviewers": 1,
                    "test_cmd": "",
                    "review_artifacts_paths": ["docs/atesto"],
                },
            )

    def test_config_v1_sem_chaves_v2_passa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["version"] = 1
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_config_v1_rejeita_timeout_como_chave_desconhecida(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["version"] = 1
            config["test_timeout_seconds"] = 5
            config_path.write_text(json.dumps(config), encoding="utf-8")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 2)
            output = result.stdout + result.stderr
            self.assertIn("test_timeout_seconds", output)
            self.assertIn("desconhecid", output)

    def test_config_version_fora_do_suportado_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["version"] = 3
            config_path.write_text(json.dumps(config), encoding="utf-8")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 2)
            self.assertIn("1, 2", result.stdout + result.stderr)

    def test_config_v2_timeout_valido(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_timeout_seconds"] = 5
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_json_sem_timeout_reporta_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, test_cmd="true")
            proof(repo)
            commit(repo)
            result = cli(repo, "check", "--json")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsNone(payload["test_timeout_seconds"])
            self.assertFalse(payload["timed_out"])

    def test_check_json_reporta_timeout_configurado(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, test_cmd="true")
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_timeout_seconds"] = 5
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo)
            result = cli(repo, "check", "--json")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["test_timeout_seconds"], 5)
            self.assertFalse(payload["timed_out"])

    def test_test_cmd_acima_do_timeout_falha_com_limite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            cmd = f"{sys.executable} -c \"import time; time.sleep(30)\""
            init(repo, test_cmd=cmd)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_timeout_seconds"] = 1
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo)
            result = cli(repo, "check", "--json")
            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["timed_out"])
            self.assertEqual(payload["test_timeout_seconds"], 1)
            problems = "\n".join(payload["problems"])
            self.assertIn("timeout de 1s", problems)
            self.assertIn("time.sleep(30)", problems)

    def test_config_v2_timeout_invalido_reprova(self) -> None:
        for value in (0, -1, True, "5", None, 1.5):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                repo = make_repo(tmp)
                init(repo)
                config_path = repo / "atesto.json"
                config = json.loads(
                    config_path.read_text(encoding="utf-8")
                )
                config["test_timeout_seconds"] = value
                config_path.write_text(json.dumps(config), encoding="utf-8")
                result = cli(repo, "check")
                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    "test_timeout_seconds", result.stdout + result.stderr
                )

    @unittest.skipUnless(os.name == "posix", "grupos de processo sao POSIX")
    def test_timeout_mata_grupo_de_processos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            (repo / "hang.py").write_text(
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, 'child.py'])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            (repo / "child.py").write_text(
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "open('started.txt', 'w', encoding='utf-8').write('x')\n"
                "time.sleep(2)\n"
                "open('survivor.txt', 'w', encoding='utf-8').write('x')\n",
                encoding="utf-8",
            )
            init(repo, test_cmd=f"{sys.executable} hang.py")
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_timeout_seconds"] = 1
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("timeout de 1s", result.stdout)
            self.assertTrue(
                (repo / "started.txt").exists(),
                "descendente nao chegou a rodar",
            )
            time.sleep(2.5)
            self.assertFalse(
                (repo / "survivor.txt").exists(),
                "descendente sobreviveu ao timeout",
            )

    def test_ci_rejects_version_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            commit(repo, "config")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["version"] = 1
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo, "downgrade")
            result = cli(repo, "check", "--ci", "--base", base)
            self.assertEqual(result.returncode, 1)
            self.assertIn("downgrade", result.stdout + result.stderr)

    def test_ci_rejects_remocao_de_timeout_da_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_timeout_seconds"] = 10
            config_path.write_text(json.dumps(config), encoding="utf-8")
            commit(repo, "config")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.pop("test_timeout_seconds")
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo, "remove timeout")
            result = cli(repo, "check", "--ci", "--base", base)
            self.assertEqual(result.returncode, 1)
            output = result.stdout + result.stderr
            self.assertIn("test_timeout_seconds", output)
            self.assertIn("remove", output)

    def test_ci_rejects_aumento_de_timeout_da_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_timeout_seconds"] = 10
            config_path.write_text(json.dumps(config), encoding="utf-8")
            commit(repo, "config")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_timeout_seconds"] = 20
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo, "increase timeout")
            result = cli(repo, "check", "--ci", "--base", base)
            self.assertEqual(result.returncode, 1)
            output = result.stdout + result.stderr
            self.assertIn("test_timeout_seconds", output)
            self.assertIn("aumenta", output)

    def test_ci_aceita_reducao_de_timeout_da_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_timeout_seconds"] = 20
            config_path.write_text(json.dumps(config), encoding="utf-8")
            commit(repo, "config")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_timeout_seconds"] = 10
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo, "reduce timeout")
            result = cli(repo, "check", "--ci", "--base", base, "--json")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["test_timeout_seconds"], 10)

    def test_ci_aceita_timeout_quando_base_nao_tem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            commit(repo, "config")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_timeout_seconds"] = 10
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo, "add timeout")
            result = cli(repo, "check", "--ci", "--base", base)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pstack_fallback_tambem_e_estrito(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            (repo / "atesto.json").rename(repo / "pstack.json")
            self._set_unknown_config_key(repo, name="pstack.json")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 2)
            output = result.stdout + result.stderr
            self.assertIn("pstack.json", output)
            self.assertIn("bogus_key", output)

    def _set_unknown_config_key(
        self, repo: Path, *, name: str = "atesto.json"
    ) -> None:
        config_path = repo / name
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["bogus_key"] = "x"
        config_path.write_text(json.dumps(config), encoding="utf-8")

    def test_ci_remove_exclude_nao_fica_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["fingerprint_excludes"] = ["vendor"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            commit(repo, "base exclude")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["fingerprint_excludes"] = []
            config_path.write_text(json.dumps(config), encoding="utf-8")
            (repo / "vendor").mkdir()
            (repo / "vendor" / "lib.py").write_text("v = 1\n", encoding="utf-8")
            commit(repo, "remove exclude")
            proof(repo)
            commit(repo, "evidence")
            result = cli(repo, "check", "--ci", "--base", base)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ci_ignora_worktree_sujo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            commit(repo, "config")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-qb", "feature")
            proof(repo)
            commit(repo, "evidence")
            (repo / "app.py").write_text("x = 77\n", encoding="utf-8")
            result = cli(repo, "check", "--ci", "--base", base)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)

    def test_ci_requires_config_in_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            result = cli(repo, "check", "--ci", "--base", "HEAD")
            self.assertEqual(result.returncode, 1)
            self.assertIn("nao existe na base", result.stdout + result.stderr)

    def test_ci_requires_artifact_in_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            commit(repo, "config")
            git(repo, "checkout", "-qb", "feature")
            proof(repo)
            result = cli(repo, "check", "--ci", "--base", "HEAD")
            self.assertEqual(result.returncode, 1)
            self.assertIn("diff do PR", result.stdout + result.stderr)

    def test_dirty_tree_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
            proof(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_test_cmd_runs_and_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, test_cmd="false")
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("test_cmd falhou", result.stdout)

    def test_two_valid_artifacts_choose_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            for slug in ("alpha", "beta"):
                result = cli(
                    repo,
                    "proof",
                    slug,
                    "--",
                    sys.executable,
                    "-c",
                    f"print('{slug}')",
                )
                self.assertEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )
                commit(repo, slug)
            result = cli(repo, "check", "--json")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["artifacts"].endswith("/beta"))

    def test_multiple_artifacts_selects_current_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            result = cli(
                repo,
                "proof",
                "alpha",
                "--",
                sys.executable,
                "-c",
                "print('alpha')",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            commit(repo, "alpha")
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            commit(repo, "code")
            result = cli(
                repo,
                "proof",
                "beta",
                "--",
                sys.executable,
                "-c",
                "print('beta')",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            commit(repo, "beta")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _future_commit(self, repo: Path) -> str:
        tree = git(repo, "rev-parse", "HEAD^{tree}")
        return git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "future")

    def test_proof_head_future_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            future = self._future_commit(repo)
            proof_path = repo / "docs" / "atesto" / "demo" / "proof.json"
            data = json.loads(proof_path.read_text(encoding="utf-8"))
            data["head"] = future
            proof_path.write_text(json.dumps(data), encoding="utf-8")
            commit(repo, "future head")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("ancestral", result.stdout)

    def test_review_head_future_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            commit(repo)
            future = self._future_commit(repo)
            review_path = repo / "docs" / "atesto" / "demo" / "review.json"
            data = json.loads(review_path.read_text(encoding="utf-8"))
            data["head"] = future
            review_path.write_text(json.dumps(data), encoding="utf-8")
            commit(repo, "future review")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("ancestral", result.stdout + result.stderr)

    def test_review_verdict_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "CHANGES_REQUESTED",
                "--reviewer",
                "reviewer",
            )
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("APPROVED", result.stdout)

    def test_min_reviewers_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["min_reviewers"] = 2
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo)
            cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "one",
            )
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("pelo menos 2", result.stdout)

    def test_min_reviewers_duplicados_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["min_reviewers"] = 2
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo)
            cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "alice",
            )
            review_path = repo / "docs" / "atesto" / "demo" / "review.json"
            data = json.loads(review_path.read_text(encoding="utf-8"))
            data["reviewers"] = ["alice", "alice"]
            review_path.write_text(json.dumps(data), encoding="utf-8")
            commit(repo, "duplicate reviewers")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("unicos", result.stdout)

    def test_schema_bool_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            proof_path = repo / "docs" / "atesto" / "demo" / "proof.json"
            data = json.loads(proof_path.read_text(encoding="utf-8"))
            data["schema"] = True
            proof_path.write_text(json.dumps(data), encoding="utf-8")
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("schema", result.stdout)

    def test_output_path_traversal_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            proof_path = repo / "docs" / "atesto" / "demo" / "proof.json"
            data = json.loads(proof_path.read_text(encoding="utf-8"))
            data["output"]["path"] = "../../outside.txt"
            proof_path.write_text(json.dumps(data), encoding="utf-8")
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("output.path", result.stdout)

    def test_proof_symlink_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            directory = repo / "docs" / "atesto" / "demo"
            original = Path(tmp) / "outside-proof.json"
            original.write_bytes((directory / "proof.json").read_bytes())
            (directory / "proof.json").unlink()
            (directory / "proof.json").symlink_to(original)
            commit(repo, "symlink proof")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("proof.json", result.stdout)

    def test_exec_mode_change_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            (repo / "app.py").chmod(0o755)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("stale", result.stdout)

    def test_review_stale_specific(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            commit(repo)
            (repo / "app.py").write_text("x = 9\n", encoding="utf-8")
            commit(repo, "code")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("review stale", result.stdout + result.stderr)

    def test_dirty_proof_then_edit_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
            proof(repo)
            self.assertEqual(cli(repo, "check").returncode, 0)
            (repo / "app.py").write_text("x = 4\n", encoding="utf-8")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("stale", result.stdout)

    def test_pstack_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            (repo / "atesto.json").rename(repo / "pstack.json")
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_crlf_dirty_proof_stable_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            git(repo, "config", "core.autocrlf", "true")
            init(repo)
            (repo / "app.py").write_bytes(b"x = 1\r\ny = 2\r\n")
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sha256_repo_stable_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            result = subprocess.run(
                ["git", "init", "--object-format=sha256", "-q"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest("git sem suporte a sha256")
            git(repo, "config", "user.email", "atesto@example.com")
            git(repo, "config", "user.name", "Atesto")
            git(repo, "config", "commit.gpgsign", "false")
            (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
            git(repo, "add", "app.py")
            git(repo, "commit", "-qm", "base")
            init(repo)
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_artifacts_root_normalizado(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            result = cli(
                repo, "init", "--artifacts-root", "./evidence"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_require_review_sem_proof_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            result = cli(repo, "init", "--require", "review")
            self.assertEqual(result.returncode, 2)
            self.assertIn("incluir proof", result.stderr)

    def test_check_slug_absoluto_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            result = cli(repo, "check", "--slug", "/tmp/outside")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("slug invalido", result.stderr)

    def test_excludes_normalizado(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["fingerprint_excludes"] = ["./vendor"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            (repo / "vendor").mkdir()
            (repo / "vendor" / "lib.py").write_text("v = 1\n", encoding="utf-8")
            commit(repo, "vendor")
            proof(repo)
            commit(repo, "evidence")
            (repo / "vendor" / "lib.py").write_text("v = 2\n", encoding="utf-8")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_require_sem_proof_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            result = cli(repo, "init", "--require", "")
            self.assertEqual(result.returncode, 2)
            self.assertIn("incluir proof", result.stderr)

    def test_no_output_nao_captura(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            result = cli(
                repo,
                "proof",
                "--no-output",
                "demo",
                "--",
                sys.executable,
                "-c",
                "print('secret')",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            commit(repo)
            output = repo / "docs" / "atesto" / "demo" / "proof-output.txt"
            self.assertEqual(output.read_bytes(), b"")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_json_gate_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            result = cli(repo, "check", "--json")
            self.assertEqual(result.returncode, 1)
            data = json.loads(result.stdout)
            self.assertFalse(data["ok"])
            self.assertTrue(data["problems"])

    def test_check_em_subdir_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            (repo / "pkg").mkdir()
            result = cli(repo, "check", "--root", "pkg")
            self.assertEqual(result.returncode, 2)
            self.assertIn("raiz do repo", result.stderr)

    def test_init_em_subdir_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            (repo / "pkg").mkdir()
            result = cli(repo, "init", "--root", "pkg")
            self.assertEqual(result.returncode, 2)
            self.assertIn("raiz do repo", result.stderr)

    def test_hook_install_status_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            result = cli(repo, "hooks", "install")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            hook = repo / ".git" / "hooks" / "pre-push"
            self.assertTrue(hook.is_file())
            self.assertIn("atesto pre-push hook", hook.read_text(encoding="utf-8"))
            self.assertTrue(os.access(hook, os.X_OK))
            self.assertEqual(cli(repo, "hooks", "status").returncode, 0)
            result = cli(repo, "hooks", "uninstall")
            self.assertEqual(result.returncode, 0)
            self.assertFalse(hook.exists())

    def test_hook_respeita_core_hooks_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            git(repo, "config", "core.hooksPath", ".githooks")
            result = cli(repo, "hooks", "install")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(
                (repo / ".githooks" / "pre-push").is_file()
            )
            self.assertFalse(
                (repo / ".git" / "hooks" / "pre-push").exists()
            )

    def test_test_cmd_alterando_arvore_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, test_cmd="printf 'x = 666\\n' > app.py")
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("test_cmd alterou", result.stdout)

    def test_symlink_para_diretorio_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            outside1 = Path(tmp) / "outside1"
            outside2 = Path(tmp) / "outside2"
            outside1.mkdir()
            outside2.mkdir()
            (repo / "link").symlink_to(outside1)
            commit(repo, "link")
            init(repo)
            proof(repo)
            commit(repo, "evidence")
            (repo / "link").unlink()
            (repo / "link").symlink_to(outside2)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("stale", result.stdout)

    def test_test_cmd_com_arvore_suja_pre_existente(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, test_cmd="true")
            (repo / "app.py").write_text("x = 55\n", encoding="utf-8")
            proof(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_review_opcional_multi_artefato_falha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            cli(
                repo, "proof", "alpha", "--",
                sys.executable, "-c", "print('alpha')",
            )
            commit(repo, "alpha")
            cli(
                repo, "review", "alpha", "--head", "HEAD",
                "--verdict", "CHANGES_REQUESTED", "--reviewer", "reviewer",
            )
            commit(repo, "alpha review")
            cli(
                repo, "proof", "beta", "--",
                sys.executable, "-c", "print('beta')",
            )
            commit(repo, "beta")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("review invalida", result.stdout + result.stderr)

    def test_test_cmd_cache_untracked_passa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(
                repo,
                test_cmd="mkdir -p __pycache__ && echo x > __pycache__/x",
            )
            proof(repo)
            commit(repo)
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dois_artefatos_escolhe_com_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            cli(
                repo, "proof", "alpha", "--",
                sys.executable, "-c", "print('alpha')",
            )
            commit(repo, "alpha")
            cli(
                repo, "review", "alpha", "--head", "HEAD",
                "--verdict", "APPROVED", "--reviewer", "reviewer",
            )
            commit(repo, "alpha review")
            cli(
                repo, "proof", "beta", "--",
                sys.executable, "-c", "print('beta')",
            )
            commit(repo, "beta")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_submodule_sujo_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            child = base / "child"
            child.mkdir()
            git(child, "init", "-q")
            git(child, "config", "user.email", "atesto@example.com")
            git(child, "config", "user.name", "Atesto")
            git(child, "config", "commit.gpgsign", "false")
            (child / "lib.py").write_text("x = 1\n", encoding="utf-8")
            git(child, "add", ".")
            git(child, "commit", "-qm", "child")
            repo = make_repo(tmp)
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(child),
                    "sub",
                ],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "submodule")
            init(repo)
            proof(repo)
            commit(repo, "evidence")
            (repo / "sub" / "lib.py").write_text("x = 2\n", encoding="utf-8")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("stale", result.stdout)

    def test_hook_arvore_valida_passa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            self.assertEqual(cli(repo, "hooks", "install").returncode, 0)
            env = {**os.environ, "PYTHONPATH": str(SRC)}
            hook = repo / ".git" / "hooks" / "pre-push"
            result = subprocess.run(
                [str(hook)],
                cwd=repo,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_hook_bloqueia_arvore_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            self.assertEqual(cli(repo, "hooks", "install").returncode, 0)
            (repo / "app.py").write_text("x = 99\n", encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(SRC)}
            hook = repo / ".git" / "hooks" / "pre-push"
            result = subprocess.run(
                [str(hook)],
                cwd=repo,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_slug_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            result = cli(repo, "proof", "../x", "--", "true")
            self.assertEqual(result.returncode, 2)
            self.assertIn("slug invalido", result.stderr)

    def test_init_does_not_create_ci(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            self.assertFalse((repo / ".github").exists())

    def test_version_constant_matches_package_metadata(self) -> None:
        result = cli(ROOT, "--version")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), f"atesto {package_version()}")

    def test_proof_records_tool_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            data = json.loads(
                (
                    repo / "docs" / "atesto" / "demo" / "proof.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                data.get("tool"),
                {"name": "atesto", "version": package_version()},
            )

    def test_old_artifacts_without_tool_still_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            reviewed = cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            self.assertEqual(
                reviewed.returncode, 0, reviewed.stdout + reviewed.stderr
            )
            directory = repo / "docs" / "atesto" / "demo"
            for name in ("proof.json", "review.json"):
                path = directory / name
                data = json.loads(path.read_text(encoding="utf-8"))
                data.pop("tool", None)
                path.write_text(json.dumps(data), encoding="utf-8")
            commit(repo, "artefatos antigos")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_review_records_tool_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            result = cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(
                (
                    repo / "docs" / "atesto" / "demo" / "review.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                data.get("tool"),
                {"name": "atesto", "version": package_version()},
            )

    def test_proof_tool_shape_invalida_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            path = repo / "docs" / "atesto" / "demo" / "proof.json"
            original = json.loads(path.read_text(encoding="utf-8"))
            cases = [
                ("nao objeto", "atesto", "proof.json tool precisa ser objeto"),
                (
                    "sem version",
                    {"name": "atesto"},
                    "proof.json tool.version",
                ),
                (
                    "name vazio",
                    {"name": "", "version": package_version()},
                    "proof.json tool.name",
                ),
            ]
            for label, value, expected in cases:
                with self.subTest(case=label):
                    data = dict(original)
                    data["tool"] = value
                    path.write_text(json.dumps(data), encoding="utf-8")
                    result = cli(repo, "check")
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(
                        expected, result.stdout + result.stderr
                    )

    def test_review_tool_shape_invalida_reprova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            reviewed = cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            self.assertEqual(
                reviewed.returncode, 0, reviewed.stdout + reviewed.stderr
            )
            commit(repo, "review")
            path = repo / "docs" / "atesto" / "demo" / "review.json"
            original = json.loads(path.read_text(encoding="utf-8"))
            cases = [
                ("nao objeto", "atesto", "review.json tool precisa ser objeto"),
                (
                    "sem name",
                    {"version": package_version()},
                    "review.json tool.name",
                ),
            ]
            for label, value, expected in cases:
                with self.subTest(case=label):
                    data = dict(original)
                    data["tool"] = value
                    path.write_text(json.dumps(data), encoding="utf-8")
                    result = cli(repo, "check")
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(
                        expected, result.stdout + result.stderr
                    )

    def test_proof_tool_version_requires_semver_like(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            path = repo / "docs" / "atesto" / "demo" / "proof.json"
            original = json.loads(path.read_text(encoding="utf-8"))
            for version in ("0.3.0rc1", "0.3.0-dev.1", "1.0.0+build.5"):
                with self.subTest(valid=version):
                    data = dict(original)
                    data["tool"] = {"name": "atesto", "version": version}
                    path.write_text(json.dumps(data), encoding="utf-8")
                    result = cli(repo, "check")
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
            for version in ("banana", "1.2", "v1.2.3", ""):
                with self.subTest(invalid=version):
                    data = dict(original)
                    data["tool"] = {"name": "atesto", "version": version}
                    path.write_text(json.dumps(data), encoding="utf-8")
                    result = cli(repo, "check")
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("tool.version", result.stdout)

    def test_review_tool_version_requires_semver_like(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            reviewed = cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            self.assertEqual(
                reviewed.returncode, 0, reviewed.stdout + reviewed.stderr
            )
            commit(repo, "review")
            path = repo / "docs" / "atesto" / "demo" / "review.json"
            original = json.loads(path.read_text(encoding="utf-8"))
            for version in ("0.3.0rc1", "0.3.0-dev.1", "1.0.0+build.5"):
                with self.subTest(valid=version):
                    data = dict(original)
                    data["tool"] = {"name": "atesto", "version": version}
                    path.write_text(json.dumps(data), encoding="utf-8")
                    result = cli(repo, "check")
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
            data = dict(original)
            data["tool"] = {"name": "atesto", "version": "banana"}
            path.write_text(json.dumps(data), encoding="utf-8")
            result = cli(repo, "check")
            self.assertEqual(result.returncode, 1)
            self.assertIn("tool.version", result.stdout)

    def test_doctor_invalid_config_reports_unknown_keys_without_crashing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["statechart"] = {"state": "done"}
            config["blast_radius"] = ["src"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            result = cli(repo, "doctor")
            self.assertEqual(result.returncode, 1)
            output = result.stdout + result.stderr
            self.assertNotIn("Traceback", output)
            self.assertIn("desconhecid", output)
            self.assertIn("statechart", output)
            self.assertIn("blast_radius", output)
            self.assertIn("migra", output.lower())
            # It still reports what it could inspect.
            self.assertIn("version: 2", output)
            self.assertIn("artifacts_root: docs/atesto", output)
            self.assertIn("artefato demo", output)
            self.assertIn(f"tool=atesto {package_version()}", output)

    def test_doctor_healthy_reports_config_evidence_and_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["test_timeout_seconds"] = 30
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo)
            reviewed = cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            self.assertEqual(
                reviewed.returncode, 0, reviewed.stdout + reviewed.stderr
            )
            commit(repo, "review")
            result = cli(repo, "doctor")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            output = result.stdout + result.stderr
            self.assertIn("config: atesto.json", output)
            self.assertIn("version: 2", output)
            self.assertIn("require: proof,review", output)
            self.assertIn("artifacts_root: docs/atesto", output)
            self.assertIn("timeout: 30s", output)
            self.assertIn("hook: ausente", output)
            self.assertIn(f"versao: {package_version()}", output)
            self.assertIn("artefato demo: proof fresh", output)
            self.assertIn("artefato demo: review fresh", output)
            self.assertEqual(
                output.count(f"tool=atesto {package_version()}"), 2
            )

    def test_doctor_reports_stale_artifact_and_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            commit(repo, "code")
            result = cli(repo, "doctor")
            self.assertEqual(result.returncode, 1)
            output = result.stdout + result.stderr
            self.assertIn("artefato demo", output)
            self.assertIn("stale", output)

    def test_doctor_unreadable_config_still_reports_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            (repo / "atesto.json").write_text("{nao json", encoding="utf-8")
            result = cli(repo, "doctor")
            self.assertEqual(result.returncode, 1)
            output = result.stdout + result.stderr
            self.assertNotIn("Traceback", output)
            self.assertIn("ilegivel", output)
            self.assertIn("hook:", output)
            self.assertIn(f"versao: {package_version()}", output)

    def test_doctor_missing_tool_is_reported_but_not_an_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            proof(repo)
            commit(repo)
            path = repo / "docs" / "atesto" / "demo" / "proof.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("tool", None)
            path.write_text(json.dumps(data), encoding="utf-8")
            commit(repo, "artefato antigo")
            result = cli(repo, "doctor")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            output = result.stdout + result.stderr
            self.assertIn("artefato demo: proof fresh", output)
            self.assertIn("tool=ausente", output)
            self.assertIn("timeout: nenhum", output)

    def test_doctor_missing_artifacts_with_requirement_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            (repo / "docs" / "atesto").rmdir()
            result = cli(repo, "doctor")
            self.assertEqual(result.returncode, 1)
            output = result.stdout + result.stderr
            self.assertIn("artefatos ausentes", output)
            self.assertIn("proof", output)

    def test_doctor_empty_artifacts_root_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            result = cli(repo, "doctor")
            self.assertEqual(result.returncode, 1)
            output = result.stdout + result.stderr
            self.assertIn("artefatos: vazio", output)
            self.assertIn("nenhum artefato", output)

    def test_doctor_required_review_missing_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            result = cli(repo, "doctor")
            self.assertEqual(result.returncode, 1)
            output = result.stdout + result.stderr
            self.assertIn("artefato demo: review ausente (exigida)", output)

    def test_doctor_without_config_reports_defaults_and_missing_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            result = cli(repo, "doctor")
            self.assertEqual(result.returncode, 1)
            output = result.stdout + result.stderr
            self.assertIn("config: default", output)
            self.assertIn("version: 1", output)
            self.assertIn("require: proof", output)
            self.assertIn("artefatos ausentes", output)

    def test_doctor_symlink_config_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            target = Path(tmp) / "outside-atesto.json"
            target.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "require": ["proof"],
                    }
                ),
                encoding="utf-8",
            )
            (repo / "atesto.json").symlink_to(target)
            result = cli(repo, "doctor")
            self.assertEqual(result.returncode, 1)
            output = result.stdout + result.stderr
            self.assertIn("symlink", output)

    def test_doctor_reports_effective_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, test_cmd="pytest -q")
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["review_artifacts_paths"] = ["docs/atesto"]
            config["fingerprint_excludes"] = ["vendor"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            result = cli(repo, "doctor")
            output = result.stdout + result.stderr
            self.assertIn("test_cmd: pytest -q", output)
            self.assertIn("review_artifacts_paths: docs/atesto", output)
            self.assertIn("excludes: vendor", output)

    def test_doctor_does_not_modify_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            (repo / "docs" / "atesto").rmdir()
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["bogus_key"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")
            before = git(
                repo, "status", "--porcelain=v1", "--untracked-files=all"
            )
            result = cli(repo, "doctor")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((repo / "docs" / "atesto").exists())
            after = git(
                repo, "status", "--porcelain=v1", "--untracked-files=all"
            )
            self.assertEqual(before, after)

    def test_check_json_exposes_tool_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            reviewed = cli(
                repo,
                "review",
                "demo",
                "--head",
                "HEAD",
                "--verdict",
                "APPROVED",
                "--reviewer",
                "reviewer",
            )
            self.assertEqual(
                reviewed.returncode, 0, reviewed.stdout + reviewed.stderr
            )
            commit(repo, "review")
            result = cli(repo, "check", "--json")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(
                payload.get("tool"),
                {"name": "atesto", "version": package_version()},
            )


def approval_json(tmp: str, head: str, **fields: object) -> Path:
    payload: dict[str, object] = {
        "schema": 1,
        "source": {"kind": "json", "ref": "change-123"},
        "head": head,
        "verdict": "APPROVED",
        "reviewers": ["alice"],
        "intent": "aprovar mudanca",
    }
    payload.update(fields)
    path = Path(tmp) / "approval.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ReviewFromJsonTest(unittest.TestCase):
    def test_from_json_writes_source_and_bound_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            reviewed = git(repo, "rev-parse", "HEAD")
            path = approval_json(
                tmp, reviewed, source={"kind": "json", "ref": "jira-42"}
            )
            result = cli(repo, "review", "demo", "--from-json", str(path))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            review_path = repo / "docs" / "atesto" / "demo" / "review.json"
            artifact = json.loads(review_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["schema"], 1)
            self.assertEqual(
                artifact["source"], {"kind": "json", "ref": "jira-42"}
            )
            self.assertEqual(artifact["head"], reviewed)
            self.assertEqual(artifact["verdict"], "APPROVED")
            self.assertEqual(artifact["reviewers"], ["alice"])
            self.assertEqual(artifact["intent"], "aprovar mudanca")
            commit(repo, "review")
            checked = cli(repo, "check", "--json")
            self.assertEqual(
                checked.returncode, 0, checked.stdout + checked.stderr
            )
            payload = json.loads(checked.stdout)
            self.assertEqual(
                payload["review"]["source"],
                {"kind": "json", "ref": "jira-42"},
            )

    def test_check_rejects_invalid_artifact_source_shape(self) -> None:
        invalid_sources: list[object] = [
            "json",
            {"kind": "bitbucket", "ref": "change-1"},
            {"kind": "json"},
            {"kind": "json", "ref": ""},
            {"kind": "json", "ref": "change-1", "extra": True},
        ]
        for source in invalid_sources:
            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = make_repo(tmp)
                init(repo, require="proof,review")
                proof(repo)
                commit(repo)
                reviewed = cli(
                    repo,
                    "review",
                    "demo",
                    "--reviewer",
                    "alice",
                )
                self.assertEqual(
                    reviewed.returncode, 0, reviewed.stdout + reviewed.stderr
                )
                review_path = repo / "docs" / "atesto" / "demo" / "review.json"
                data = json.loads(review_path.read_text(encoding="utf-8"))
                data["source"] = source
                review_path.write_text(json.dumps(data), encoding="utf-8")
                commit(repo, "source")
                result = cli(repo, "check")
                self.assertEqual(result.returncode, 1)
                self.assertIn("source", result.stdout)

    def test_check_accepts_old_review_without_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            reviewed = cli(repo, "review", "demo", "--reviewer", "alice")
            self.assertEqual(
                reviewed.returncode, 0, reviewed.stdout + reviewed.stderr
            )
            artifact = json.loads(
                (
                    repo / "docs" / "atesto" / "demo" / "review.json"
                ).read_text(encoding="utf-8")
            )
            self.assertNotIn("source", artifact)
            commit(repo, "review")
            checked = cli(repo, "check", "--json")
            self.assertEqual(
                checked.returncode, 0, checked.stdout + checked.stderr
            )
            payload = json.loads(checked.stdout)
            self.assertNotIn("source", payload["review"])

    def test_from_json_rejects_manual_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            path = approval_json(tmp, head)
            for flag in (
                ["--head", "HEAD"],
                ["--verdict", "APPROVED"],
                ["--reviewer", "alice"],
                ["--intent", "manual"],
            ):
                with self.subTest(flag=flag):
                    result = cli(
                        repo,
                        "review",
                        "demo",
                        "--from-json",
                        str(path),
                        *flag,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(
                        "nao combina", result.stdout + result.stderr
                    )
                    self.assertFalse(
                        (
                            repo
                            / "docs"
                            / "atesto"
                            / "demo"
                            / "review.json"
                        ).exists()
                    )

    def test_from_json_rejects_invalid_contract_shapes(self) -> None:
        base: dict[str, object] = {
            "schema": 1,
            "source": {"kind": "json", "ref": "change-123"},
            "head": "",
            "verdict": "APPROVED",
            "reviewers": ["alice"],
            "intent": "aprovar mudanca",
        }
        cases: list[tuple[str, dict[str, object], str]] = [
            ("schema 2", {"schema": 2}, "schema"),
            ("schema bool", {"schema": True}, "schema"),
            ("schema string", {"schema": "1"}, "schema"),
            ("source string", {"source": "json"}, "source"),
            (
                "source kind",
                {"source": {"kind": "bitbucket", "ref": "x"}},
                "source.kind",
            ),
            ("source missing ref", {"source": {"kind": "json"}}, "source"),
            (
                "source blank ref",
                {"source": {"kind": "json", "ref": "  "}},
                "source.ref",
            ),
            (
                "source extra key",
                {"source": {"kind": "json", "ref": "x", "extra": 1}},
                "source",
            ),
            ("blank head", {"head": "  "}, "head"),
            ("head null", {"head": None}, "head"),
            ("invalid verdict", {"verdict": "MAYBE"}, "verdict"),
            ("blank intent", {"intent": " "}, "intent"),
            ("reviewer blank", {"reviewers": ["alice", " "]}, "reviewers"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            path = Path(tmp) / "approval.json"
            for name, fields, expected in cases:
                with self.subTest(case=name):
                    payload = {**base, "head": head, **fields}
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    result = cli(
                        repo, "review", "demo", "--from-json", str(path)
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(
                        expected, result.stdout + result.stderr
                    )
                    self.assertFalse(
                        (
                            repo
                            / "docs"
                            / "atesto"
                            / "demo"
                            / "review.json"
                        ).exists()
                    )

    def test_from_json_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            path = Path(tmp) / "approval.json"
            path.write_text(
                "{"
                '"schema": 1,'
                '"source": {"kind": "json", "ref": "change-1"},'
                f'"head": "{head}",'
                '"verdict": "APPROVED",'
                '"verdict": "CHANGES_REQUESTED",'
                '"reviewers": ["alice"],'
                '"intent": "aprovar"'
                "}",
                encoding="utf-8",
            )
            result = cli(repo, "review", "demo", "--from-json", str(path))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("duplicad", result.stdout + result.stderr)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_json_rejects_non_object_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            path = Path(tmp) / "approval.json"
            path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
            result = cli(repo, "review", "demo", "--from-json", str(path))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("objeto", result.stdout + result.stderr)

    def test_from_json_dedupes_reviewers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            path = approval_json(
                tmp, head, reviewers=["alice", "alice", "bob"]
            )
            result = cli(repo, "review", "demo", "--from-json", str(path))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            artifact = json.loads(
                (
                    repo / "docs" / "atesto" / "demo" / "review.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["reviewers"], ["alice", "bob"])

    def test_from_json_accepts_all_source_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            path = Path(tmp) / "approval.json"
            for kind in ("github", "gitlab", "json"):
                with self.subTest(kind=kind):
                    path = approval_json(
                        tmp, head, source={"kind": kind, "ref": f"{kind}-1"}
                    )
                    result = cli(
                        repo, "review", "demo", "--from-json", str(path)
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    artifact = json.loads(
                        (
                            repo
                            / "docs"
                            / "atesto"
                            / "demo"
                            / "review.json"
                        ).read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        artifact["source"],
                        {"kind": kind, "ref": f"{kind}-1"},
                    )

    def test_from_json_rejects_unknown_top_level_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            path = approval_json(tmp, head, extra="typo")
            result = cli(repo, "review", "demo", "--from-json", str(path))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("extra", result.stdout + result.stderr)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_json_accepts_ancestor_with_same_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof")
            commit(repo, "config")
            approved = git(repo, "rev-parse", "HEAD")
            proof(repo)
            commit(repo, "evidence")
            current = git(repo, "rev-parse", "HEAD")
            self.assertNotEqual(approved, current)
            path = approval_json(tmp, approved)
            result = cli(repo, "review", "demo", "--from-json", str(path))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            artifact = json.loads(
                (
                    repo / "docs" / "atesto" / "demo" / "review.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["head"], current)

    def test_from_json_rejects_ancestor_with_changed_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            approved = git(repo, "rev-parse", "HEAD")
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            commit(repo, "code")
            path = approval_json(tmp, approved)
            result = cli(repo, "review", "demo", "--from-json", str(path))
            self.assertNotEqual(result.returncode, 0)
            output = result.stdout + result.stderr
            self.assertIn("arvore", output)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_json_rejects_non_ancestor_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            git(repo, "checkout", "-qb", "other")
            (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
            commit(repo, "other")
            other = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "-q", "-")
            path = approval_json(tmp, other)
            result = cli(repo, "review", "demo", "--from-json", str(path))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ancestral", result.stdout + result.stderr)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_json_rejects_unresolvable_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            path = approval_json(tmp, "deadbeef")
            result = cli(repo, "review", "demo", "--from-json", str(path))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("deadbeef", result.stdout + result.stderr)

    def test_from_json_rejects_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            path = Path(tmp) / "approval.json"
            path.write_text("{not json", encoding="utf-8")
            result = cli(repo, "review", "demo", "--from-json", str(path))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("JSON", result.stdout + result.stderr)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_json_rejects_missing_or_blank_verdict(self) -> None:
        for fields in ({"verdict": ""}, {"verdict": "   "}, {"verdict": None}):
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as tmp:
                repo = make_repo(tmp)
                init(repo)
                head = git(repo, "rev-parse", "HEAD")
                payload = {
                    "schema": 1,
                    "source": {"kind": "json", "ref": "change-123"},
                    "head": head,
                    "verdict": "APPROVED",
                    "reviewers": ["alice"],
                    "intent": "aprovar mudanca",
                }
                payload.update(fields)
                path = Path(tmp) / "approval.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                result = cli(
                    repo, "review", "demo", "--from-json", str(path)
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("verdict", result.stdout + result.stderr)
                self.assertFalse(
                    (
                        repo
                        / "docs"
                        / "atesto"
                        / "demo"
                        / "review.json"
                    ).exists()
                )

    def test_from_json_rejects_empty_reviewers(self) -> None:
        for value in ([], ["  "], None, "alice"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                repo = make_repo(tmp)
                init(repo)
                head = git(repo, "rev-parse", "HEAD")
                path = approval_json(tmp, head, reviewers=value)
                result = cli(
                    repo, "review", "demo", "--from-json", str(path)
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("reviewers", result.stdout + result.stderr)
                self.assertFalse(
                    (
                        repo
                        / "docs"
                        / "atesto"
                        / "demo"
                        / "review.json"
                    ).exists()
                )


class ReviewSourceIdValidationTest(unittest.TestCase):
    """Provider ids are positive integers; argparse must reject the rest."""

    def test_provider_review_flags_reject_invalid_ids(self) -> None:
        for flag in ("--from-github-pr", "--from-gitlab-mr"):
            for value in ("0", "-1", "abc", "1.5", ""):
                with (
                    self.subTest(flag=flag, value=value),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    repo = make_repo(tmp)
                    init(repo)
                    result = cli(repo, "review", "demo", flag, value)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(
                        "numero inteiro positivo",
                        result.stdout + result.stderr,
                    )
                    self.assertFalse(
                        (
                            repo
                            / "docs"
                            / "atesto"
                            / "demo"
                            / "review.json"
                        ).exists()
                    )


class ReviewFromGithubTest(unittest.TestCase):
    def test_from_github_writes_source_and_bound_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            head = git(repo, "rev-parse", "HEAD")
            url = "https://github.com/acme/repo/pull/42"
            env = fake_gh(
                tmp,
                pr_view=github_pr(head, url),
                api=github_reviews(
                    [
                        github_review(
                            1, "alice", "APPROVED", head, "2026-11-27T10:00:00Z"
                        ),
                        github_review(
                            2, "bob", "COMMENTED", head, "2026-11-27T10:01:00Z"
                        ),
                    ]
                ),
            )
            result = cli(
                repo, "review", "demo", "--from-github-pr", "42", env=env
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            artifact = json.loads(
                (
                    repo / "docs" / "atesto" / "demo" / "review.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["source"], {"kind": "github", "ref": url})
            self.assertEqual(artifact["head"], head)
            self.assertEqual(artifact["verdict"], "APPROVED")
            self.assertEqual(artifact["reviewers"], ["alice"])
            self.assertTrue(artifact["intent"].strip())
            commit(repo, "review")
            checked = cli(repo, "check", "--json")
            self.assertEqual(
                checked.returncode, 0, checked.stdout + checked.stderr
            )
            payload = json.loads(checked.stdout)
            self.assertEqual(
                payload["review"]["source"], {"kind": "github", "ref": url}
            )

    def test_from_github_ignores_approval_on_older_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            old = git(repo, "rev-parse", "HEAD")
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            commit(repo, "code")
            head = git(repo, "rev-parse", "HEAD")
            env = fake_gh(
                tmp,
                pr_view=github_pr(head, "https://github.com/acme/repo/pull/7"),
                api=github_reviews(
                    [
                        github_review(
                            1, "alice", "APPROVED", old, "2026-11-27T10:00:00Z"
                        )
                    ]
                ),
            )
            result = cli(
                repo, "review", "demo", "--from-github-pr", "7", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "aprovacao", (result.stdout + result.stderr).lower()
            )
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_github_uses_effective_latest_review_per_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            old = git(repo, "rev-parse", "HEAD")
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            commit(repo, "code")
            head = git(repo, "rev-parse", "HEAD")
            env = fake_gh(
                tmp,
                pr_view=github_pr(head, "https://github.com/acme/repo/pull/8"),
                api=github_reviews(
                    [
                        github_review(
                            1, "alice", "APPROVED", old, "2026-11-27T10:00:00Z"
                        ),
                        github_review(
                            2, "alice", "APPROVED", head, "2026-11-27T10:02:00Z"
                        ),
                    ],
                    [
                        github_review(
                            3,
                            "bob",
                            "CHANGES_REQUESTED",
                            old,
                            "2026-11-27T10:01:00Z",
                        ),
                        github_review(
                            4, "bob", "APPROVED", head, "2026-11-27T10:03:00Z"
                        ),
                        github_review(
                            5, "carol", "APPROVED", head, "2026-11-27T10:04:00Z"
                        ),
                        github_review(
                            6, "carol", "COMMENTED", head, "2026-11-27T10:05:00Z"
                        ),
                    ],
                ),
            )
            result = cli(
                repo, "review", "demo", "--from-github-pr", "8", env=env
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            artifact = json.loads(
                (
                    repo / "docs" / "atesto" / "demo" / "review.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["reviewers"], ["alice", "bob", "carol"])
            calls = [
                call for call in gh_calls(tmp) if call["args"][:1] == ["api"]
            ]
            self.assertEqual(len(calls), 1)
            self.assertIn("--paginate", calls[0]["args"])
            self.assertIn("--slurp", calls[0]["args"])
            self.assertIn(
                "repos/{owner}/{repo}/pulls/8/reviews", calls[0]["args"]
            )

    def test_from_github_does_not_count_bot_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["min_reviewers"] = 2
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo)
            head = git(repo, "rev-parse", "HEAD")
            env = fake_gh(
                tmp,
                pr_view=github_pr(
                    head, "https://github.com/acme/repo/pull/23"
                ),
                api=github_reviews(
                    [
                        github_review(
                            1,
                            "alice",
                            "APPROVED",
                            head,
                            "2026-11-27T10:00:00Z",
                        ),
                        github_review(
                            2,
                            "ci[bot]",
                            "APPROVED",
                            head,
                            "2026-11-27T10:01:00Z",
                            user_type="Bot",
                        ),
                        github_review(
                            3,
                            "release[bot]",
                            "APPROVED",
                            head,
                            "2026-11-27T10:02:00Z",
                        ),
                    ]
                ),
            )
            result = cli(
                repo, "review", "demo", "--from-github-pr", "23", env=env
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            artifact = json.loads(
                (
                    repo / "docs" / "atesto" / "demo" / "review.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["reviewers"], ["alice"])
            commit(repo, "review")
            checked = cli(repo, "check")
            self.assertEqual(checked.returncode, 1)
            self.assertIn("pelo menos 2", checked.stdout)

    def test_from_github_changes_requested_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            env = fake_gh(
                tmp,
                pr_view=github_pr(head, "https://github.com/acme/repo/pull/9"),
                api=github_reviews(
                    [
                        github_review(
                            1, "alice", "APPROVED", head, "2026-11-27T10:00:00Z"
                        ),
                        github_review(
                            2,
                            "bob",
                            "CHANGES_REQUESTED",
                            head,
                            "2026-11-27T10:01:00Z",
                        ),
                    ]
                ),
            )
            result = cli(
                repo, "review", "demo", "--from-github-pr", "9", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            output = (result.stdout + result.stderr).lower()
            self.assertIn("changes requested", output)
            self.assertIn("bob", output)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_github_review_decision_fails_closed(self) -> None:
        for decision in ("CHANGES_REQUESTED", "REVIEW_REQUIRED"):
            with (
                self.subTest(decision=decision),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = make_repo(tmp)
                init(repo)
                head = git(repo, "rev-parse", "HEAD")
                env = fake_gh(
                    tmp,
                    pr_view=github_pr(
                        head,
                        "https://github.com/acme/repo/pull/10",
                        reviewDecision=decision,
                    ),
                    api=github_reviews(
                        [
                            github_review(
                                1,
                                "alice",
                                "APPROVED",
                                head,
                                "2026-11-27T10:00:00Z",
                            )
                        ]
                    ),
                )
                result = cli(
                    repo, "review", "demo", "--from-github-pr", "10", env=env
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(
                    (
                        repo / "docs" / "atesto" / "demo" / "review.json"
                    ).exists()
                )

    def test_from_github_empty_review_decision_is_not_a_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            env = fake_gh(
                tmp,
                pr_view=github_pr(
                    head,
                    "https://github.com/acme/repo/pull/11",
                    reviewDecision="",
                ),
                api=github_reviews(
                    [
                        github_review(
                            1, "alice", "APPROVED", head, "2026-11-27T10:00:00Z"
                        )
                    ]
                ),
            )
            result = cli(
                repo, "review", "demo", "--from-github-pr", "11", env=env
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_from_github_requires_current_head_approval(self) -> None:
        cases = {
            "no reviews": [],
            "dismissed": [
                github_review(
                    1, "alice", "APPROVED", None, "2026-11-27T10:00:00Z"
                ),
                github_review(
                    2, "alice", "DISMISSED", None, "2026-11-27T10:01:00Z"
                ),
            ],
        }
        for name, reviews in cases.items():
            with (
                self.subTest(case=name),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = make_repo(tmp)
                init(repo)
                head = git(repo, "rev-parse", "HEAD")
                for index, review in enumerate(reviews):
                    review["commit_id"] = head
                env = fake_gh(
                    tmp,
                    pr_view=github_pr(
                        head, "https://github.com/acme/repo/pull/12"
                    ),
                    api=github_reviews(reviews),
                )
                result = cli(
                    repo, "review", "demo", "--from-github-pr", "12", env=env
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "aprovacao", (result.stdout + result.stderr).lower()
                )
                self.assertFalse(
                    (
                        repo / "docs" / "atesto" / "demo" / "review.json"
                    ).exists()
                )

    def test_from_github_missing_cli_fails_with_actionable_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            git_binary = shutil.which("git")
            self.assertIsNotNone(git_binary)
            (bin_dir / "git").symlink_to(git_binary)
            result = cli(
                repo,
                "review",
                "demo",
                "--from-github-pr",
                "13",
                env={"PATH": str(bin_dir)},
            )
            self.assertNotEqual(result.returncode, 0)
            output = (result.stdout + result.stderr).lower()
            self.assertIn("gh", output)
            self.assertIn("nao encontrado", output)
            self.assertIn("instale", output)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_github_unauthenticated_fails_with_actionable_message(
        self,
    ) -> None:
        cases = [
            {
                "exit": 4,
                "stderr": (
                    "To get started with GitHub CLI, please run:  "
                    "gh auth login\n"
                ),
            },
            {
                "exit": 1,
                "stderr": (
                    "gh: To use GitHub CLI in a GitHub Actions workflow, "
                    "set the GH_TOKEN environment variable.\n"
                ),
            },
        ]
        for payload in cases:
            with (
                self.subTest(stderr=payload["stderr"]),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = make_repo(tmp)
                init(repo)
                env = fake_gh(
                    tmp, pr_view=payload, api={"json": []}
                )
                result = cli(
                    repo, "review", "demo", "--from-github-pr", "14", env=env
                )
                self.assertNotEqual(result.returncode, 0)
                output = (result.stdout + result.stderr).lower()
                self.assertIn("nao autenticado", output)
                self.assertIn("gh auth login", output)
                self.assertFalse(
                    (
                        repo / "docs" / "atesto" / "demo" / "review.json"
                    ).exists()
                )

    def test_from_github_malformed_output_fails_closed(self) -> None:
        cases: list[tuple[str, dict | None, dict, str]] = [
            ("metadata not json", None, {"json": []}, "malformado"),
            (
                "reviews not pages",
                {},
                {"json": {"message": "Not Found"}},
                "paginas",
            ),
            (
                "review without user",
                {},
                {"json": [[{"state": "APPROVED"}]]},
                "user.login",
            ),
            (
                "review bad state",
                {},
                {"json": [[{"user": {"login": "a"}, "state": "MAYBE"}]]},
                "state",
            ),
        ]
        for name, metadata, api, expected in cases:
            with (
                self.subTest(case=name),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = make_repo(tmp)
                init(repo)
                head = git(repo, "rev-parse", "HEAD")
                pr_view = (
                    {"stdout": "{not json"}
                    if metadata is None
                    else {"json": {"headRefOid": head, "url": "u"}}
                )
                env = fake_gh(tmp, pr_view=pr_view, api=api)
                result = cli(
                    repo, "review", "demo", "--from-github-pr", "15", env=env
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    expected, (result.stdout + result.stderr).lower()
                )
                self.assertFalse(
                    (
                        repo / "docs" / "atesto" / "demo" / "review.json"
                    ).exists()
                )

    def test_from_github_rejects_invalid_commit_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            env = fake_gh(
                tmp,
                pr_view={"json": {"headRefOid": "a" * 41, "url": "u"}},
                api={"json": []},
            )
            result = cli(
                repo, "review", "demo", "--from-github-pr", "22", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("headRefOid", result.stdout + result.stderr)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

            env = fake_gh(
                tmp,
                pr_view=github_pr(
                    head, "https://github.com/acme/repo/pull/22"
                ),
                api=github_reviews(
                    [
                        github_review(
                            1,
                            "alice",
                            "APPROVED",
                            "b" * 41,
                            "2026-11-27T10:00:00Z",
                        )
                    ]
                ),
            )
            result = cli(
                repo, "review", "demo", "--from-github-pr", "22", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("commit_id", result.stdout + result.stderr)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_github_rest_404_checks_exit_before_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            env = fake_gh(
                tmp,
                pr_view=github_pr(
                    head, "https://github.com/acme/repo/pull/16"
                ),
                api={
                    "exit": 1,
                    "stdout": '{"message":"Not Found","status":"404"}',
                    "stderr": "gh: Not Found (HTTP 404)\n",
                },
            )
            result = cli(
                repo, "review", "demo", "--from-github-pr", "16", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("404", result.stdout + result.stderr)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_github_requires_local_head_to_match_pr_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            old = git(repo, "rev-parse", "HEAD")
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            commit(repo, "code")
            env = fake_gh(
                tmp,
                pr_view=github_pr(
                    old, "https://github.com/acme/repo/pull/17"
                ),
                api=github_reviews([]),
            )
            result = cli(
                repo, "review", "demo", "--from-github-pr", "17", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            output = (result.stdout + result.stderr).lower()
            self.assertIn("head", output)
            self.assertIn("checkout", output)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_github_rejects_manual_flags_and_other_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            path = approval_json(tmp, head)
            for extra in (
                ["--head", "HEAD"],
                ["--verdict", "APPROVED"],
                ["--reviewer", "alice"],
                ["--intent", "manual"],
                ["--from-json", str(path)],
            ):
                with self.subTest(extra=extra):
                    result = cli(
                        repo,
                        "review",
                        "demo",
                        "--from-github-pr",
                        "18",
                        *extra,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    output = (result.stdout + result.stderr).lower()
                    self.assertTrue(
                        "nao combina" in output or "apenas uma fonte" in output
                    )
                    self.assertFalse(
                        (
                            repo
                            / "docs"
                            / "atesto"
                            / "demo"
                            / "review.json"
                        ).exists()
                    )

    def test_from_github_keeps_min_reviewers_for_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["min_reviewers"] = 2
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo)
            head = git(repo, "rev-parse", "HEAD")
            env = fake_gh(
                tmp,
                pr_view=github_pr(
                    head, "https://github.com/acme/repo/pull/19"
                ),
                api=github_reviews(
                    [
                        github_review(
                            1, "alice", "APPROVED", head, "2026-11-27T10:00:00Z"
                        )
                    ]
                ),
            )
            result = cli(
                repo, "review", "demo", "--from-github-pr", "19", env=env
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            commit(repo, "review")
            checked = cli(repo, "check")
            self.assertEqual(checked.returncode, 1)
            self.assertIn("pelo menos 2", checked.stdout)

    def test_from_github_leaves_repo_resolution_to_gh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            url = "https://github.com/upstream/repo/pull/20"
            env = fake_gh(
                tmp,
                pr_view=github_pr(head, url),
                api=github_reviews(
                    [
                        github_review(
                            1, "alice", "APPROVED", head, "2026-11-27T10:00:00Z"
                        )
                    ]
                ),
            )
            env["GH_REPO"] = "upstream/repo"
            result = cli(
                repo, "review", "demo", "--from-github-pr", "20", env=env
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = gh_calls(tmp)
            self.assertEqual(
                {call["GH_REPO"] for call in calls}, {"upstream/repo"}
            )
            api_calls = [
                call for call in calls if call["args"][:1] == ["api"]
            ]
            self.assertEqual(len(api_calls), 1)
            self.assertIn(
                "repos/{owner}/{repo}/pulls/20/reviews",
                api_calls[0]["args"],
            )

    def test_from_github_runs_gh_from_the_review_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            env = fake_gh(
                tmp,
                pr_view=github_pr(
                    head, "https://github.com/acme/repo/pull/21"
                ),
                api=github_reviews(
                    [
                        github_review(
                            1, "alice", "APPROVED", head, "2026-11-27T10:00:00Z"
                        )
                    ]
                ),
            )
            result = cli(
                elsewhere,
                "review",
                "demo",
                "--root",
                str(repo),
                "--from-github-pr",
                "21",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                {call["cwd"] for call in gh_calls(tmp)},
                {str(repo.resolve())},
            )


class ReviewFromGitlabTest(unittest.TestCase):
    def test_from_gitlab_writes_source_and_bound_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            proof(repo)
            commit(repo)
            head = git(repo, "rev-parse", "HEAD")
            url = "https://gitlab.com/acme/repo/-/merge_requests/42"
            env = fake_glab(
                tmp,
                mr=gitlab_mr(head, url),
                approvals=gitlab_approvals("alice", "bob", "alice"),
            )
            result = cli(
                repo, "review", "demo", "--from-gitlab-mr", "42", env=env
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            artifact = json.loads(
                (
                    repo / "docs" / "atesto" / "demo" / "review.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["source"], {"kind": "gitlab", "ref": url})
            self.assertEqual(artifact["head"], head)
            self.assertEqual(artifact["verdict"], "APPROVED")
            self.assertEqual(artifact["reviewers"], ["alice", "bob"])
            self.assertTrue(artifact["intent"].strip())
            commit(repo, "review")
            checked = cli(repo, "check", "--json")
            self.assertEqual(
                checked.returncode, 0, checked.stdout + checked.stderr
            )
            payload = json.loads(checked.stdout)
            self.assertEqual(
                payload["review"]["source"], {"kind": "gitlab", "ref": url}
            )
            calls = glab_calls(tmp)
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                calls[0]["args"],
                ["api", "projects/:fullpath/merge_requests/42"],
            )
            self.assertEqual(
                calls[1]["args"],
                ["api", "projects/:fullpath/merge_requests/42/approvals"],
            )
            self.assertEqual(
                {call["cwd"] for call in calls}, {str(repo.resolve())}
            )

    def test_from_gitlab_sha_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            old = git(repo, "rev-parse", "HEAD")
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            commit(repo, "code")
            env = fake_glab(
                tmp,
                mr=gitlab_mr(
                    old, "https://gitlab.com/acme/repo/-/merge_requests/7"
                ),
                approvals=gitlab_approvals("alice"),
            )
            result = cli(
                repo, "review", "demo", "--from-gitlab-mr", "7", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            output = (result.stdout + result.stderr).lower()
            self.assertIn("sha", output)
            self.assertIn("head", output)
            self.assertIn("checkout", output)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_gitlab_requires_at_least_one_approval(self) -> None:
        cases = {
            "empty approved_by": gitlab_approvals(),
            "approved false": gitlab_approvals("alice", approved=False),
        }
        for name, approvals in cases.items():
            with (
                self.subTest(case=name),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = make_repo(tmp)
                init(repo)
                head = git(repo, "rev-parse", "HEAD")
                env = fake_glab(
                    tmp,
                    mr=gitlab_mr(
                        head, "https://gitlab.com/acme/repo/-/merge_requests/8"
                    ),
                    approvals=approvals,
                )
                result = cli(
                    repo, "review", "demo", "--from-gitlab-mr", "8", env=env
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "aprov", (result.stdout + result.stderr).lower()
                )
                self.assertFalse(
                    (
                        repo / "docs" / "atesto" / "demo" / "review.json"
                    ).exists()
                )

    def test_from_gitlab_blocking_merge_status_fails_closed(self) -> None:
        cases = {
            "requested_changes": "requested changes",
            "not_approved": "not_approved",
        }
        for status, expected in cases.items():
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = make_repo(tmp)
                init(repo)
                head = git(repo, "rev-parse", "HEAD")
                env = fake_glab(
                    tmp,
                    mr=gitlab_mr(
                        head,
                        "https://gitlab.com/acme/repo/-/merge_requests/9",
                        detailed_merge_status=status,
                    ),
                    approvals=gitlab_approvals("alice"),
                )
                result = cli(
                    repo, "review", "demo", "--from-gitlab-mr", "9", env=env
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    expected, (result.stdout + result.stderr).lower()
                )
                self.assertFalse(
                    (
                        repo / "docs" / "atesto" / "demo" / "review.json"
                    ).exists()
                )

    def test_from_gitlab_unresolved_blocking_discussions_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            env = fake_glab(
                tmp,
                mr=gitlab_mr(
                    head,
                    "https://gitlab.com/acme/repo/-/merge_requests/10",
                    blocking_discussions_resolved=False,
                ),
                approvals=gitlab_approvals("alice"),
            )
            result = cli(
                repo, "review", "demo", "--from-gitlab-mr", "10", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "discuss", (result.stdout + result.stderr).lower()
            )
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_gitlab_missing_cli_fails_with_actionable_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            git_binary = shutil.which("git")
            self.assertIsNotNone(git_binary)
            (bin_dir / "git").symlink_to(git_binary)
            result = cli(
                repo,
                "review",
                "demo",
                "--from-gitlab-mr",
                "11",
                env={
                    "PATH": str(bin_dir),
                    "CI_PROJECT_ID": "",
                    "CI_MERGE_REQUEST_PROJECT_ID": "",
                },
            )
            self.assertNotEqual(result.returncode, 0)
            output = (result.stdout + result.stderr).lower()
            self.assertIn("glab", output)
            self.assertIn("nao encontrado", output)
            self.assertIn("instale", output)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_gitlab_unauthenticated_fails_with_actionable_message(
        self,
    ) -> None:
        cases = [
            {
                "exit": 1,
                "stderr": (
                    "glab is not authenticated; run `glab auth login` "
                    "to authenticate\n"
                ),
            },
            {
                "exit": 1,
                "stderr": "glab: 401 Unauthorized (HTTP 401)\n",
            },
        ]
        for payload in cases:
            with (
                self.subTest(stderr=payload["stderr"]),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = make_repo(tmp)
                init(repo)
                env = fake_glab(
                    tmp, mr=payload, approvals={"json": {}}
                )
                result = cli(
                    repo, "review", "demo", "--from-gitlab-mr", "12", env=env
                )
                self.assertNotEqual(result.returncode, 0)
                output = (result.stdout + result.stderr).lower()
                self.assertIn("nao autenticado", output)
                self.assertIn("glab auth login", output)
                self.assertIn("gitlab_token", output)
                self.assertFalse(
                    (
                        repo / "docs" / "atesto" / "demo" / "review.json"
                    ).exists()
                )

    def test_from_gitlab_403_approvals_explains_required_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            env = fake_glab(
                tmp,
                mr=gitlab_mr(
                    head, "https://gitlab.com/acme/repo/-/merge_requests/13"
                ),
                approvals={
                    "exit": 1,
                    "stdout": '{"message":"403 Forbidden"}',
                    "stderr": "glab: 403 Forbidden (HTTP 403)\n",
                },
            )
            env["CI_JOB_TOKEN"] = "job-token"
            result = cli(
                repo, "review", "demo", "--from-gitlab-mr", "13", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            output = (result.stdout + result.stderr).lower()
            self.assertIn("403", output)
            self.assertIn("gitlab_token", output)
            self.assertIn("ci_job_token", output)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_gitlab_malformed_output_fails_closed(self) -> None:
        url = "https://gitlab.com/acme/repo/-/merge_requests/15"

        def mr_json(head: str, **fields: object) -> dict[str, object]:
            return dict(gitlab_mr(head, url)["json"], **fields)

        cases: list[tuple[str, object, dict, str]] = [
            (
                "metadata not json",
                lambda head: {"stdout": "{not json"},
                gitlab_approvals("alice"),
                "malformado",
            ),
            (
                "metadata not object",
                lambda head: {"json": []},
                gitlab_approvals("alice"),
                "objeto",
            ),
            (
                "bad sha",
                lambda head: {"json": mr_json(head, sha="abc")},
                gitlab_approvals("alice"),
                "sha",
            ),
            (
                "bad merge status",
                lambda head: {
                    "json": mr_json(head, detailed_merge_status=1)
                },
                gitlab_approvals("alice"),
                "detailed_merge_status",
            ),
            (
                "bad blocking flag",
                lambda head: {
                    "json": mr_json(
                        head, blocking_discussions_resolved="yes"
                    )
                },
                gitlab_approvals("alice"),
                "blocking_discussions_resolved",
            ),
            (
                "bad url",
                lambda head: {"json": mr_json(head, web_url="  ")},
                gitlab_approvals("alice"),
                "web_url",
            ),
            (
                "approvals not object",
                lambda head: {"json": mr_json(head)},
                {"json": []},
                "objeto",
            ),
            (
                "approved not bool",
                lambda head: {"json": mr_json(head)},
                {
                    "json": {
                        "approved": "yes",
                        "approved_by": [{"user": {"username": "alice"}}],
                    }
                },
                "approved",
            ),
            (
                "approved_by not list",
                lambda head: {"json": mr_json(head)},
                {"json": {"approved": True, "approved_by": {}}},
                "approved_by",
            ),
            (
                "approved_by entry not object",
                lambda head: {"json": mr_json(head)},
                {"json": {"approved": True, "approved_by": ["alice"]}},
                "approved_by",
            ),
            (
                "approved_by missing username",
                lambda head: {"json": mr_json(head)},
                {"json": {"approved": True, "approved_by": [{"user": {}}]}},
                "username",
            ),
        ]
        for name, make_mr, approvals, expected in cases:
            with (
                self.subTest(case=name),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = make_repo(tmp)
                init(repo)
                head = git(repo, "rev-parse", "HEAD")
                env = fake_glab(
                    tmp, mr=make_mr(head), approvals=approvals
                )
                result = cli(
                    repo, "review", "demo", "--from-gitlab-mr", "15", env=env
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    expected, (result.stdout + result.stderr).lower()
                )
                self.assertFalse(
                    (
                        repo / "docs" / "atesto" / "demo" / "review.json"
                    ).exists()
                )

    def test_from_gitlab_mr_http_error_checks_exit_before_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            env = fake_glab(
                tmp,
                mr={
                    "exit": 1,
                    "stdout": '{"message":"404 Not Found"}',
                    "stderr": "glab: 404 Not Found (HTTP 404)\n",
                },
                approvals=gitlab_approvals("alice"),
            )
            result = cli(
                repo, "review", "demo", "--from-gitlab-mr", "16", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("404", result.stdout + result.stderr)
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_gitlab_uses_ci_project_id(self) -> None:
        cases = [
            ("CI_PROJECT_ID", "1234", "5678"),
            ("CI_PROJECT_ID", "1234", ""),
            ("CI_MERGE_REQUEST_PROJECT_ID", "1234", "5678"),
        ]
        for name, project, mr_project in cases:
            expected = mr_project or project
            with (
                self.subTest(name=name, project=project, mr_project=mr_project),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = make_repo(tmp)
                init(repo)
                head = git(repo, "rev-parse", "HEAD")
                env = fake_glab(
                    tmp,
                    mr=gitlab_mr(
                        head,
                        "https://gitlab.com/acme/repo/-/merge_requests/17",
                    ),
                    approvals=gitlab_approvals("alice"),
                    project=project,
                    merge_request_project=mr_project,
                )
                result = cli(
                    repo, "review", "demo", "--from-gitlab-mr", "17", env=env
                )
                self.assertEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )
                calls = glab_calls(tmp)
                self.assertEqual(
                    calls[0]["args"],
                    [
                        "api",
                        f"projects/{expected}/merge_requests/17",
                    ],
                )

    def test_from_gitlab_rejects_invalid_ci_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            env = fake_glab(
                tmp,
                mr=gitlab_mr(
                    head, "https://gitlab.com/acme/repo/-/merge_requests/18"
                ),
                approvals=gitlab_approvals("alice"),
                project="acme/repo",
            )
            result = cli(
                repo, "review", "demo", "--from-gitlab-mr", "18", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("CI_PROJECT_ID", result.stdout + result.stderr)
            self.assertEqual(glab_calls(tmp), [])
            self.assertFalse(
                (repo / "docs" / "atesto" / "demo" / "review.json").exists()
            )

    def test_from_gitlab_rejects_manual_flags_and_other_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo)
            head = git(repo, "rev-parse", "HEAD")
            path = approval_json(tmp, head)
            for extra in (
                ["--head", "HEAD"],
                ["--verdict", "APPROVED"],
                ["--reviewer", "alice"],
                ["--intent", "manual"],
                ["--from-json", str(path)],
                ["--from-github-pr", "19"],
            ):
                with self.subTest(extra=extra):
                    result = cli(
                        repo,
                        "review",
                        "demo",
                        "--from-gitlab-mr",
                        "19",
                        *extra,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    output = (result.stdout + result.stderr).lower()
                    self.assertTrue(
                        "nao combina" in output or "apenas uma fonte" in output
                    )
                    self.assertFalse(
                        (
                            repo
                            / "docs"
                            / "atesto"
                            / "demo"
                            / "review.json"
                        ).exists()
                    )

    def test_from_gitlab_keeps_min_reviewers_for_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            init(repo, require="proof,review")
            config_path = repo / "atesto.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["min_reviewers"] = 2
            config_path.write_text(json.dumps(config), encoding="utf-8")
            proof(repo)
            commit(repo)
            head = git(repo, "rev-parse", "HEAD")
            env = fake_glab(
                tmp,
                mr=gitlab_mr(
                    head, "https://gitlab.com/acme/repo/-/merge_requests/20"
                ),
                approvals=gitlab_approvals("alice", "alice"),
            )
            result = cli(
                repo, "review", "demo", "--from-gitlab-mr", "20", env=env
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            commit(repo, "review")
            checked = cli(repo, "check")
            self.assertEqual(checked.returncode, 1)
            self.assertIn("pelo menos 2", checked.stdout)


class ReviewSourceBoundaryTest(unittest.TestCase):
    """Provider adapters are optional modules; the core stays policy-free."""

    def test_adapters_live_in_their_own_package(self) -> None:
        package = SRC / "atesto" / "review_sources"
        self.assertTrue(package.is_dir(), package)
        for name in ("__init__.py", "base.py", "github.py", "gitlab.py"):
            with self.subTest(name=name):
                self.assertTrue((package / name).is_file(), package / name)

    def test_core_does_not_encode_provider_policy(self) -> None:
        source = (SRC / "atesto" / "core.py").read_text(encoding="utf-8")
        for token in (
            "reviewDecision",
            "detailed_merge_status",
            "requested_changes",
            "not_approved",
            "provider_json",
            "gh api",
            "glab",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)
        self.assertNotIn("review_sources", source)

    def test_importing_core_does_not_load_provider_adapters(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import atesto.core; "
                    "assert 'atesto.review_sources' not in sys.modules"
                ),
            ],
            cwd=ROOT,
            env=ENV,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_docs_define_the_adapter_boundary(self) -> None:
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for text in (contributing, readme):
            with self.subTest(document=text[:20]):
                self.assertIn("review_sources", text)
                self.assertIn("integrations/", text)
                self.assertRegex(text, r"(?i)optional")


class ReviewSchemaTest(unittest.TestCase):
    def test_optional_source_matches_loader_contract(self) -> None:
        schema = json.loads(
            (
                SRC / "atesto" / "schemas" / "review.schema.json"
            ).read_text(encoding="utf-8")
        )
        source = schema["properties"]["source"]
        self.assertNotIn("source", schema["required"])
        self.assertEqual(set(source["required"]), set(core.REVIEW_SOURCE_KEYS))
        self.assertEqual(
            set(source["properties"]["kind"]["enum"]),
            set(core.REVIEW_SOURCE_KINDS),
        )
        self.assertIs(source["additionalProperties"], False)


class ConfigSchemaTest(unittest.TestCase):
    """The published schema must agree with the loader's version registry."""

    schema_path = SRC / "atesto" / "schemas" / "atesto.schema.json"

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(cls.schema_path.read_text(encoding="utf-8"))
        cls.variants = cls.schema["$defs"]

    def test_properties_match_loader_key_sets(self) -> None:
        self.assertEqual(
            self.schema["$schema"],
            "https://json-schema.org/draft/2020-12/schema",
        )
        self.assertEqual(set(self.variants), {"v1", "v2"})
        for name, keys in (
            ("v1", core.CONFIG_KEYS_V1),
            ("v2", core.CONFIG_KEYS_V2),
        ):
            with self.subTest(version=name):
                variant = self.variants[name]
                self.assertEqual(set(variant["properties"]), set(keys))
                self.assertIs(variant["additionalProperties"], False)

    def test_types_and_constraints_match_loader_contract(self) -> None:
        expected_types = {
            "version": "integer",
            "artifacts_root": "string",
            "require": "array",
            "min_reviewers": "integer",
            "test_cmd": "string",
            "review_artifacts_paths": "array",
            "fingerprint_excludes": "array",
            "excludes": "array",
        }
        for name in ("v1", "v2"):
            properties = self.variants[name]["properties"]
            with self.subTest(version=name):
                for key, expected in expected_types.items():
                    self.assertEqual(properties[key].get("type"), expected)
                for key in (
                    "require",
                    "review_artifacts_paths",
                    "fingerprint_excludes",
                    "excludes",
                ):
                    self.assertEqual(
                        properties[key]["items"].get("type"), "string"
                    )
                self.assertEqual(
                    properties["min_reviewers"].get("minimum"), 1
                )

        self.assertNotIn(
            "test_timeout_seconds", self.variants["v1"]["properties"]
        )
        timeout = self.variants["v2"]["properties"]["test_timeout_seconds"]
        self.assertEqual(timeout.get("type"), "integer")
        self.assertEqual(timeout.get("minimum"), 1)

    def test_version_selects_variant(self) -> None:
        self.assertEqual(
            self.schema["oneOf"],
            [{"$ref": "#/$defs/v1"}, {"$ref": "#/$defs/v2"}],
        )
        self.assertEqual(
            self.variants["v1"]["properties"]["version"].get("const"), 1
        )
        self.assertEqual(
            self.variants["v2"]["properties"]["version"].get("const"), 2
        )
        self.assertNotIn("version", self.variants["v1"].get("required", []))
        self.assertIn("version", self.variants["v2"].get("required", []))

    def test_readme_links_schema(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("src/atesto/schemas/atesto.schema.json", readme)


class ArtifactSchemaTest(unittest.TestCase):
    """Proof/review schemas must reject non-semver tool versions."""

    def test_tool_version_pattern_is_semver_like(self) -> None:
        for name in ("proof", "review"):
            with self.subTest(schema=name):
                schema = json.loads(
                    (
                        SRC / "atesto" / "schemas" / f"{name}.schema.json"
                    ).read_text(encoding="utf-8")
                )
                pattern = schema["properties"]["tool"]["properties"][
                    "version"
                ]["pattern"]
                for version in (
                    "0.2.0",
                    "0.3.0rc1",
                    "0.3.0-dev.1",
                    "1.0.0+build.5",
                ):
                    self.assertIsNotNone(
                        re.fullmatch(pattern, version), version
                    )
                for version in ("banana", "1.2", "v1.2.3"):
                    self.assertIsNone(
                        re.fullmatch(pattern, version), version
                    )

    def test_tool_version_pattern_matches_loader(self) -> None:
        for name in ("proof", "review"):
            with self.subTest(schema=name):
                schema = json.loads(
                    (
                        SRC / "atesto" / "schemas" / f"{name}.schema.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    schema["properties"]["tool"]["properties"]["version"][
                        "pattern"
                    ],
                    core.TOOL_VERSION_PATTERN.pattern,
                )


class ProviderDocsTest(unittest.TestCase):
    """Provider behavior and the JSON contract must be documented."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_readme_documents_json_contract_v1(self) -> None:
        self.assertIn("## Strict JSON contract v1", self.readme)
        section = self.readme.split("## Strict JSON contract v1", 1)[1].split(
            "\n## ", 1
        )[0]
        for field in (
            "schema",
            "source",
            "kind",
            "ref",
            "head",
            "verdict",
            "reviewers",
            "intent",
        ):
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", section)
        blocks = [
            json.loads(block)
            for block in re.findall(
                r"```json\n(.*?)```", section, re.DOTALL
            )
        ]
        self.assertTrue(blocks, section)
        example = blocks[0]
        self.assertEqual(
            set(example),
            {"schema", "source", "head", "verdict", "reviewers", "intent"},
        )
        self.assertEqual(example["schema"], 1)
        self.assertEqual(set(example["source"]), {"kind", "ref"})
        lowered = section.lower()
        for phrase in (
            "ancestor",
            "same tree",
            "unknown",
            "malformed",
            "duplicate",
            "blank",
            "non-empty",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lowered)

    def test_readme_documents_github_bot_and_empty_decision_policy(self) -> None:
        section = self.readme.split("### GitHub PR approvals", 1)[1].split(
            "\n### ", 1
        )[0]
        lowered = section.lower()
        self.assertIn("bot", lowered)
        self.assertIn("people", lowered)
        self.assertIn("reviewdecision", lowered)
        self.assertIn("empty", lowered)
        self.assertIn("current-head approval", lowered)

    def test_readme_documents_gitlab_stale_approval_limitation(self) -> None:
        section = self.readme.split("### GitLab MR approvals", 1)[1].split(
            "\n## ", 1
        )[0]
        lowered = section.lower()
        self.assertIn("reset_approvals_on_push", lowered)
        self.assertIn("stale", lowered)
        self.assertIn("no per-approval sha", lowered)

    def test_readme_documents_digest_pinning(self) -> None:
        lowered = self.readme.lower()
        self.assertIn("docker buildx imagetools inspect", lowered)
        self.assertIn("digest", lowered)
        self.assertIn("minimum", lowered)
        self.assertIn("production", lowered)

    def test_jenkins_stage_fragment_is_additive(self) -> None:
        path = ROOT / "integrations" / "ci" / "jenkins.stage.groovy"
        self.assertTrue(path.is_file(), path)
        text = path.read_text(encoding="utf-8")
        self.assertIn("stage('Atesto gate')", text)
        self.assertIn("check --ci", text)
        self.assertIn("CHANGE_TARGET", text)
        self.assertNotIn("pipeline {", text)
        readme = (ROOT / "integrations" / "ci" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("jenkins.stage.groovy", readme)


class ReleaseMetadataTest(unittest.TestCase):
    """Release artifacts must stay consistent with the packaged version."""

    def test_ci_template_pins_current_release_with_hash(self) -> None:
        template = (
            ROOT / "integrations" / "github-actions" / "atesto-gate.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(f"atesto=={package_version()}", template)
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", template)
        self.assertEqual(len(hashes), 1, template)
        self.assertNotIn("0" * 64, hashes[0])
        self.assertIn("--require-hashes", template)
        self.assertIn("--no-deps", template)
        self.assertIn('python-version: "3.12"', template)
        self.assertIn("self-hosted", template.lower())
        smoke = template.index("atesto --version")
        gate = template.index("atesto check --ci")
        self.assertLess(smoke, gate)

    def test_release_build_pins_environment_not_source_metadata(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertRegex(workflow, r'SOURCE_DATE_EPOCH:\s*"[0-9]{9,10}"')
        self.assertIn('"build==1.6.1"', workflow)
        self.assertIn('"setuptools==84.2.1"', workflow)
        self.assertIn('"wheel==', workflow)
        self.assertIn("python -m build --no-isolation", workflow)
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        build_system = pyproject.split("[build-system]", 1)[1].split(
            "[project]", 1
        )[0]
        self.assertNotIn("==", build_system)
        self.assertRegex(build_system, r'setuptools>=[0-9]')

    def test_release_verifies_built_wheel_before_publish(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        verify = workflow.index("Verify the built wheel")
        publish = workflow.index("Publish to PyPI")
        self.assertLess(verify, publish)
        self.assertLess(workflow.index("sha256sum"), publish)
        self.assertLess(
            workflow.index("integrations/github-actions/atesto-gate.yml"),
            publish,
        )

    def test_release_build_uses_umask_022(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        build = workflow.split("- name: Build sdist and wheel", 1)[1]
        build = build.split("- name:", 1)[0]
        self.assertIn("umask 022", build)

    def test_release_scopes_id_token_to_the_publish_job(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        top = workflow.split("jobs:", 1)[0]
        self.assertNotIn("id-token", top)
        publish = workflow.split("\n  publish:", 1)[1].split(
            "\n  container:", 1
        )[0]
        self.assertIn("id-token: write", publish)
        container = workflow.split("\n  container:", 1)[1]
        self.assertNotIn("id-token", container)

    def test_release_container_smokes_before_pushing(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        container = workflow.split("\n  container:", 1)[1]
        self.assertIn("load: true", container)
        self.assertIn("push: false", container)
        self.assertIn("tests/container-smoke.sh", container)
        self.assertIn("docker image inspect", container)
        self.assertIn("docker push", container)
        self.assertLess(
            container.index("load: true"),
            container.index("tests/container-smoke.sh"),
        )
        self.assertLess(
            container.index("tests/container-smoke.sh"),
            container.index("docker push"),
        )
        self.assertLess(
            container.index("docker image inspect"),
            container.index("docker push"),
        )

    def test_changelog_documents_provider_neutral_release(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        marker = f"## [{package_version()}]"
        self.assertIn(marker, changelog)
        entry = (
            changelog.split(marker, 1)[1].split("\n## [", 1)[0].lower()
        )
        for term in (
            "--from-json",
            "--from-github-pr",
            "--from-gitlab-mr",
            "container",
            "snippet",
            "readme",
        ):
            with self.subTest(term=term):
                self.assertIn(term, entry)

    def test_changelog_keeps_020_breaking_migration(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## [0.2.0]", changelog)
        entry = changelog.split("## [0.2.0]", 1)[1].split("\n## [", 1)[0]
        self.assertRegex(entry, r"(?i)breaking")
        self.assertIn("desconhecid", entry)
        self.assertIn("adaptador", entry)

    def test_readme_quickstart_reaches_green_check(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Quick start", readme)
        section = readme.split("## Quick start", 1)[1].split("\n## ", 1)[0]
        for command in (
            f'python -m pip install "atesto=={package_version()}"',
            "git init",
            "atesto init --require proof,review",
            "atesto proof demo",
            "atesto review demo",
            "git add docs/atesto/demo",
            "git commit",
            "atesto check",
            "atesto: ok",
        ):
            with self.subTest(command=command):
                self.assertIn(command, section)
        self.assertLess(
            section.index("atesto proof demo"),
            section.index("atesto check"),
        )

    def test_readme_documents_threat_model_and_host_enforcement(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Threat model", readme)
        section = readme.split("## Threat model", 1)[1].split("\n## ", 1)[0]
        lowered = section.lower()
        for phrase in (
            "self-reported",
            "does not prove",
            "host",
            "admin",
            "bypass",
            "gitlab",
            "token",
            "head",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lowered)

    def test_readme_documents_release_verification(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("## Release", 1)[1].split("\n## ", 1)[0]
        lowered = section.lower()
        for phrase in (
            "pip download",
            "sha256sum",
            "umask 022",
            "docker pull",
            f"ghcr.io/guihirsch/atesto:{package_version()}",
            "published",
            "post-merge",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, lowered)

    def test_release_publishes_container_with_verified_wheel(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("container:", workflow)
        self.assertRegex(workflow, r"needs:\s*publish")
        self.assertIn("packages: write", workflow)
        self.assertIn(f"atesto=={package_version()}", workflow)
        self.assertIn("sha256sum", workflow)
        self.assertIn(
            "integrations/github-actions/atesto-gate.yml", workflow
        )
        self.assertIn(
            "ATESTO_SHA256=${{ env.ATESTO_WHEEL_SHA256 }}", workflow
        )
        self.assertIn(
            f"ghcr.io/guihirsch/atesto:{package_version()}", workflow
        )
        self.assertIn("ghcr.io/guihirsch/atesto:latest", workflow)
        self.assertIn("docker image inspect", workflow)
        self.assertIn("org.opencontainers.image.version", workflow)
        self.assertIn("dev.atesto.wheel-sha256", workflow)

    def test_readme_documents_v2_contract(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn('"version": 2', readme)
        for term in (
            "test_timeout_seconds",
            "doctor",
            "tool",
            "adapter",
            "unknown",
        ):
            with self.subTest(term=term):
                self.assertIn(term, readme)

    def test_readme_config_block_is_labeled_example(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("**Example**", readme)
        self.assertIn("not the exact file", readme)
        self.assertIn("`atesto init` writes", readme)
        blocks = [
            json.loads(block)
            for block in re.findall(
                r"```json\n(.*?)```", readme, re.DOTALL
            )
            if '"artifacts_root"' in block
        ]
        self.assertEqual(len(blocks), 2, blocks)
        example, init_output = blocks
        self.assertLessEqual(set(example), core.CONFIG_KEYS_V2)
        self.assertIn("test_timeout_seconds", example)
        self.assertEqual(
            set(init_output),
            {
                "version",
                "artifacts_root",
                "require",
                "min_reviewers",
                "test_cmd",
                "review_artifacts_paths",
            },
        )


class ContainerMetadataTest(unittest.TestCase):
    """The container image must match the ticket #13 contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        cls.dockerfile_ci = (ROOT / "Dockerfile.ci").read_text(
            encoding="utf-8"
        )
        cls.ci = (
            ROOT / ".github" / "workflows" / "container.yml"
        ).read_text(encoding="utf-8")
        cls.publish = (
            ROOT / ".github" / "workflows" / "container-publish.yml"
        ).read_text(encoding="utf-8")
        cls.gate = (
            ROOT / "integrations" / "github-actions" / "atesto-gate.yml"
        ).read_text(encoding="utf-8")
        cls.smoke_path = ROOT / "tests" / "container-smoke.sh"
        cls.smoke = cls.smoke_path.read_text(encoding="utf-8")
        release = re.search(
            r"atesto==([0-9]+\.[0-9]+\.[0-9]+) "
            r"--hash=sha256:([0-9a-f]{64})",
            cls.gate,
        )
        if release is None:
            raise AssertionError("gate template nao pina o wheel")
        cls.release_version, cls.release_sha256 = release.groups()

    def test_base_images_are_pinned_python_312_slim(self) -> None:
        for name, text in (
            ("Dockerfile", self.dockerfile),
            ("Dockerfile.ci", self.dockerfile_ci),
        ):
            with self.subTest(name=name):
                self.assertRegex(
                    text,
                    r"(?m)^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$",
                )

    def test_release_dockerfile_installs_the_hashed_pypi_wheel(self) -> None:
        self.assertIn(
            "apt-get install -y --no-install-recommends git", self.dockerfile
        )
        self.assertIn("--require-hashes", self.dockerfile)
        self.assertIn("--no-deps", self.dockerfile)
        self.assertIn("ARG ATESTO_VERSION", self.dockerfile)
        self.assertIn("ARG ATESTO_SHA256", self.dockerfile)
        self.assertIn('atesto==%s --hash=sha256:%s', self.dockerfile)

    def test_dockerfile_requires_the_version_and_hash_inputs(self) -> None:
        # The gate template is the single source of the pin. A default here
        # would duplicate it and silently build a different release.
        self.assertNotRegex(
            self.dockerfile, r"(?m)^ARG ATESTO_(?:VERSION|SHA256)="
        )

    def test_ci_dockerfile_installs_the_local_wheel(self) -> None:
        self.assertIn("COPY dist/ /tmp/dist/", self.dockerfile_ci)
        self.assertIn("/tmp/dist/atesto-*.whl", self.dockerfile_ci)
        self.assertIn("--no-deps", self.dockerfile_ci)
        self.assertNotRegex(
            self.dockerfile_ci, r"(?m)^RUN .*--require-hashes"
        )
        self.assertRegex(self.dockerfile_ci, r"(?m)^USER atesto$")
        self.assertNotRegex(self.dockerfile_ci, r"(?m)^USER (?:root|0)$")
        self.assertRegex(self.dockerfile_ci, r"(?m)^WORKDIR /repo$")
        self.assertRegex(
            self.dockerfile_ci, r'(?m)^ENTRYPOINT \["atesto"\]$'
        )

    def test_runs_as_non_root_in_repo_with_atesto_entrypoint(self) -> None:
        self.assertRegex(self.dockerfile, r"(?m)^USER atesto$")
        self.assertNotRegex(self.dockerfile, r"(?m)^USER (?:root|0)$")
        self.assertRegex(self.dockerfile, r"(?m)^WORKDIR /repo$")
        self.assertRegex(
            self.dockerfile, r'(?m)^ENTRYPOINT \["atesto"\]$'
        )

    def test_labels_bind_image_to_release_wheel(self) -> None:
        for name, text in (
            ("Dockerfile", self.dockerfile),
            ("Dockerfile.ci", self.dockerfile_ci),
        ):
            with self.subTest(name=name):
                self.assertIn(
                    'org.opencontainers.image.version="${ATESTO_VERSION}"',
                    text,
                )
                self.assertIn(
                    'dev.atesto.wheel-sha256="${ATESTO_SHA256}"', text
                )

    def test_does_not_install_provider_clis(self) -> None:
        for name, text in (
            ("Dockerfile", self.dockerfile),
            ("Dockerfile.ci", self.dockerfile_ci),
        ):
            for cli in ("gh", "glab"):
                with self.subTest(name=name, cli=cli):
                    self.assertNotRegex(
                        text,
                        rf"(?m)^.*apt-get install[^\n]*\b{cli}\b",
                    )

    def test_ci_builds_a_local_wheel_and_smokes_the_ci_image(self) -> None:
        for trigger in ("push:", "pull_request:"):
            with self.subTest(trigger=trigger):
                self.assertIn(trigger, self.ci)
        for path in (
            "Dockerfile",
            "Dockerfile.ci",
            ".github/workflows/container.yml",
            ".github/workflows/container-publish.yml",
            "tests/container-smoke.sh",
            "pyproject.toml",
            "src/**",
        ):
            with self.subTest(path=path):
                self.assertIn(f"- {path}", self.ci)
        for step in (
            "python -m build --no-isolation",
            "umask 022",
            "SOURCE_DATE_EPOCH",
            '"build==1.6.1"',
            '"setuptools==84.2.1"',
            '"wheel==0.48.0"',
            "docker build",
            "--file Dockerfile.ci",
        ):
            with self.subTest(step=step):
                self.assertIn(step, self.ci)
        self.assertIn(
            'tests/container-smoke.sh atesto:smoke "$ATESTO_VERSION"',
            self.ci,
        )
        self.assertLess(
            self.ci.index("python -m build --no-isolation"),
            self.ci.index("docker build"),
        )
        self.assertLess(
            self.ci.index("docker build"),
            self.ci.index("bash tests/container-smoke.sh"),
        )

    def test_publish_requires_inputs_and_verifies_the_pypi_wheel(self) -> None:
        self.assertIn("workflow_dispatch:", self.publish)
        self.assertIn("packages: write", self.publish)
        self.assertGreaterEqual(self.publish.count("required: true"), 2)
        self.assertNotIn("default:", self.publish)
        for step in (
            "pip download",
            "atesto==${{ inputs.version }}",
            "sha256sum",
            "inputs.wheel_sha256",
            "ATESTO_WHEEL_SHA256",
            "ATESTO_SHA256=${{ env.ATESTO_WHEEL_SHA256 }}",
            "ghcr.io/guihirsch/atesto:${{ inputs.version }}",
            "ghcr.io/guihirsch/atesto:latest",
        ):
            with self.subTest(step=step):
                self.assertIn(step, self.publish)
        self.assertLess(
            self.publish.index("Verify the PyPI wheel"),
            self.publish.index("docker push"),
        )
        self.assertIn("tests/container-smoke.sh", self.publish)

    def test_smoke_checks_version_user_provider_clis_and_gate(self) -> None:
        self.assertTrue(os.access(self.smoke_path, os.X_OK))
        for needle in (
            "--version",
            "id -u",
            "gh",
            "glab",
            "atesto: ok",
            "init",
            "proof",
            "check",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.smoke)


class CISnippetTest(unittest.TestCase):
    """CI snippets must run the same pinned container gate on every host."""

    SNIPPETS: ClassVar[dict[str, dict[str, list[str] | str]]] = {
        "gitlab-ci.yml": {
            "history": ['GIT_DEPTH: "0"'],
            "base": "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
        },
        "github-actions.yml": {
            "history": ["fetch-depth: 0"],
            "base": "github.base_ref",
        },
        "jenkins.Jenkinsfile": {
            "history": ["depth: 0", "shallow: false"],
            "base": "CHANGE_TARGET",
        },
        "jenkins.stage.groovy": {
            "history": ["depth: 0", "shallow: false"],
            "base": "CHANGE_TARGET",
        },
        "circleci.yml": {
            "history": ["--unshallow"],
            "base": "ATESTO_BASE_REF",
        },
        "buildkite.yml": {
            "history": ["--unshallow"],
            "base": "BUILDKITE_PULL_REQUEST_BASE_BRANCH",
        },
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.version = package_version()
        directory = ROOT / "integrations" / "ci"
        cls.directory = directory
        cls.text = {
            name: (directory / name).read_text(encoding="utf-8")
            for name in cls.SNIPPETS
        }

    def test_every_host_has_a_snippet(self) -> None:
        self.assertTrue(self.directory.is_dir())
        found = {
            item.name
            for item in self.directory.iterdir()
            if item.is_file()
        }
        self.assertLessEqual(set(self.SNIPPETS), found)

    def test_snippets_pin_the_package_version(self) -> None:
        expected = f"ghcr.io/guihirsch/atesto:{self.version}"
        for name, text in self.text.items():
            with self.subTest(snippet=name):
                self.assertIn(expected, text)
                tags = re.findall(
                    r"ghcr\.io/guihirsch/atesto:([A-Za-z0-9._-]+)", text
                )
                self.assertTrue(tags, name)
                self.assertEqual(set(tags), {self.version}, name)

    def test_snippets_fetch_full_history(self) -> None:
        for name, spec in self.SNIPPETS.items():
            for token in spec["history"]:
                with self.subTest(snippet=name, token=token):
                    self.assertIn(token, self.text[name])

    def test_snippets_set_base_ref_and_run_gate(self) -> None:
        for name, spec in self.SNIPPETS.items():
            text = self.text[name]
            with self.subTest(snippet=name):
                self.assertIn("check --ci", text)
                self.assertIn("--base", text)
                self.assertIn(spec["base"], text)

    def test_snippets_are_additive_and_do_not_write_evidence(self) -> None:
        # The review artifact must be part of the PR diff before CI runs.
        # A snippet that generates or pushes evidence would either fail
        # `check --ci` or mutate the reviewed branch.
        for name, text in self.text.items():
            for forbidden in (
                "review --from-",
                "atesto review",
                "git commit",
                "git push",
                "docker push",
            ):
                with self.subTest(snippet=name, forbidden=forbidden):
                    self.assertNotIn(forbidden, text)

    def test_readme_documents_host_matrix_and_precommit_review(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        hosts = ("GitLab", "GitHub", "Jenkins", "CircleCI", "Buildkite")
        rows = [
            line
            for line in readme.splitlines()
            if line.startswith("|") and any(host in line for host in hosts)
        ]
        self.assertEqual(len(rows), len(hosts), rows)
        for host, row in zip(hosts, rows):
            with self.subTest(host=host):
                self.assertIn(host, row)
                self.assertIn("check --ci", row)
        self.assertIn(
            f"ghcr.io/guihirsch/atesto:{self.version}", readme
        )
        for source in (
            "--from-github-pr",
            "--from-gitlab-mr",
            "--from-json",
        ):
            with self.subTest(source=source):
                self.assertIn(source, readme)
        self.assertIn("pre-commit", readme)
        self.assertRegex(readme, r"(?i)part of the (?:pr|merge request) diff")
        for phrase in ("branch protection", "rulesets", "merge queue"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main()
