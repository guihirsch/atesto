"""Acceptance tests for Atesto v0."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

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


def cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "atesto", *args],
        cwd=repo,
        env=ENV,
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

    def test_changelog_documents_breaking_release(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        marker = f"## [{package_version()}]"
        self.assertIn(marker, changelog)
        entry = changelog.split(marker, 1)[1].split("\n## [", 1)[0]
        self.assertRegex(entry, r"(?i)breaking")
        self.assertIn("desconhecid", entry)
        self.assertIn("adaptador", entry)

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


if __name__ == "__main__":
    unittest.main()
