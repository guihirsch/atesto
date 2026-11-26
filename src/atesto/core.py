"""Atesto core: proof/review evidence and a deterministic gate.

Stdlib only. The checker is intentionally small and runtime-agnostic: it
validates evidence against the working tree, not the quality of the code.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
CONFIG_NAMES = ("atesto.json", "pstack.json")
DEFAULT_ARTIFACTS_ROOT = "docs/atesto"
PROOF_SCHEMA = 1
REVIEW_SCHEMA = 1
MAX_OUTPUT_BYTES = 2_000_000
VALID_REQUIREMENTS = {"proof", "review"}
VALID_VERDICTS = {"APPROVED", "CHANGES_REQUESTED"}


class AtestoError(Exception):
    """Erro de uso/configuracao."""


class GateError(AtestoError):
    """Falha do gate."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode(value: bytes) -> str:
    return value.decode("utf-8", "surrogateescape")


def git(
    root: Path,
    args: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        errors="surrogateescape",
        check=False,
    )
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise GateError(f"git {' '.join(args)} falhou: {message}")
    return result


def git_bytes(root: Path, args: list[str]) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "surrogateescape").strip()
        raise GateError(f"git {' '.join(args)} falhou: {message}")
    return result.stdout


def ensure_toplevel(root: Path) -> None:
    result = git(root, ["rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        raise AtestoError("nao e um repo git")
    top = Path(result.stdout.strip()).resolve()
    if top != root.resolve():
        raise AtestoError(f"root deve ser a raiz do repo: {top}")


def git_head(root: Path) -> str:
    result = git(root, ["rev-parse", "HEAD"])
    return result.stdout.strip()


def resolve_ref(root: Path, ref: str) -> str:
    result = git(root, ["rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
    if result.returncode != 0:
        raise GateError(f"ref {ref!r} nao resolve para um commit")
    return result.stdout.strip()


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = git(
        root,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GateError(
        f"git merge-base falhou: {(result.stderr or result.stdout).strip()}"
    )


def changed_paths(root: Path, base: str, head: str = "HEAD") -> list[str]:
    raw = git_bytes(root, ["diff", "--name-only", "-z", f"{base}...{head}"])
    return [item for item in _decode(raw).split("\0") if item]


def _norm(path: str) -> str:
    return path.strip("/")


def normalize_rel_path(raw: str, label: str) -> str:
    value = raw.strip().replace("\\", "/")
    if not value or Path(value).is_absolute() or posixpath.isabs(value):
        raise AtestoError(f"{label} precisa ser relativo: {raw!r}")
    norm = posixpath.normpath(value)
    if norm in {".", ""} or norm.startswith("../"):
        raise AtestoError(f"{label} invalido: {raw!r}")
    return norm


def is_excluded(path: str, excludes: Iterable[str]) -> bool:
    path = _norm(path)
    for raw in excludes:
        pattern = _norm(raw)
        if not pattern:
            continue
        if path == pattern or path.startswith(pattern + "/"):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def _inside(root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _safe_artifacts_root(root: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    rel = _inside(root, candidate)
    if rel is None:
        raise AtestoError(
            f"artifacts_root precisa ficar dentro do repo: {raw!r}"
        )
    if rel.startswith("../") or rel == "..":
        raise AtestoError(f"artifacts_root invalido: {raw!r}")
    return root / rel


def write_regular(path: Path, data: str | bytes) -> None:
    if path.is_symlink():
        raise AtestoError(f"symlink recusado ao escrever: {path}")
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(raw)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except OSError:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise


def ensure_dir(root: Path, path: Path) -> None:
    rel = _inside(root, path)
    if rel is None:
        raise AtestoError(f"diretorio fora do repo: {path}")
    current = root
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            raise AtestoError(f"symlink recusado no caminho de artefatos: {current}")
        current.mkdir(exist_ok=True)


@dataclass
class Config:
    version: int = 1
    artifacts_root: str = DEFAULT_ARTIFACTS_ROOT
    require: list[str] = field(default_factory=lambda: ["proof"])
    min_reviewers: int = 1
    test_cmd: str = ""
    review_artifacts_paths: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    path: Path | None = None

    @property
    def artifacts_path(self) -> Path:
        root = (self.path.parent if self.path else Path.cwd())
        return _safe_artifacts_root(root, self.artifacts_root)

    def excludes_for(self, root: Path) -> list[str]:
        values = [
            _norm(self.artifacts_root),
            *(_norm(item) for item in self.excludes),
        ]
        return [item for item in dict.fromkeys(values) if item]

    @classmethod
    def from_data(cls, data: dict, path: Path, root: Path) -> Config:
        if not isinstance(data, dict):
            raise AtestoError(f"{path.name} precisa ser objeto")
        version = data.get("version", 1)
        if type(version) is not int or version != 1:
            raise AtestoError(f"{path.name} version precisa ser 1")
        artifacts_root = data.get("artifacts_root", DEFAULT_ARTIFACTS_ROOT)
        if not isinstance(artifacts_root, str) or not artifacts_root.strip():
            raise AtestoError(f"{path.name} artifacts_root invalido")
        artifacts_root = normalize_rel_path(
            artifacts_root, f"{path.name} artifacts_root"
        )
        require = data.get("require", ["proof"])
        if not isinstance(require, list) or not all(
            isinstance(item, str) for item in require
        ):
            raise AtestoError(f"{path.name} require precisa ser lista de strings")
        unknown = set(require) - VALID_REQUIREMENTS
        if unknown:
            raise AtestoError(
                f"{path.name} require desconhecido: {sorted(unknown)}"
            )
        min_reviewers = data.get("min_reviewers", 1)
        if type(min_reviewers) is not int or min_reviewers < 1:
            raise AtestoError(f"{path.name} min_reviewers precisa ser >= 1")
        test_cmd = data.get("test_cmd", "")
        if not isinstance(test_cmd, str):
            raise AtestoError(f"{path.name} test_cmd precisa ser string")
        review_paths = data.get(
            "review_artifacts_paths", [artifacts_root]
        )
        if not isinstance(review_paths, list) or not all(
            isinstance(item, str) and item.strip() for item in review_paths
        ):
            raise AtestoError(
                f"{path.name} review_artifacts_paths precisa ser lista de strings"
            )
        review_paths = [
            normalize_rel_path(item, f"{path.name} review_artifacts_paths")
            for item in review_paths
        ]
        excludes = data.get(
            "fingerprint_excludes", data.get("excludes", [])
        )
        if not isinstance(excludes, list) or not all(
            isinstance(item, str) and item.strip() for item in excludes
        ):
            raise AtestoError(f"{path.name} excludes precisa ser lista de strings")
        excludes = [
            normalize_rel_path(item, f"{path.name} excludes")
            for item in excludes
        ]
        if "proof" not in require:
            raise AtestoError(f"{path.name} require precisa incluir proof")
        config = cls(
            version=1,
            artifacts_root=artifacts_root.strip(),
            require=list(dict.fromkeys(require)),
            min_reviewers=min_reviewers,
            test_cmd=test_cmd.strip(),
            review_artifacts_paths=[item.strip() for item in review_paths],
            excludes=[item.strip() for item in excludes],
            path=path,
        )
        _safe_artifacts_root(root, config.artifacts_root)
        return config

    @classmethod
    def load(cls, root: Path) -> Config:
        chosen: Path | None = None
        for name in CONFIG_NAMES:
            candidate = root / name
            if candidate.is_symlink():
                raise AtestoError(f"{name} nao pode ser symlink")
            if candidate.is_file():
                chosen = candidate
                break
        if chosen is None:
            config = cls(path=root / "atesto.json")
            config.review_artifacts_paths = [config.artifacts_root]
            return config
        try:
            data = json.loads(chosen.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AtestoError(f"{chosen.name} ilegivel: {exc}") from exc
        return cls.from_data(data, chosen, root)


def load_config(root: Path) -> Config:
    return Config.load(root)


def validate_slug(slug: str) -> str:
    if (
        not slug
        or "/" in slug
        or "\\" in slug
        or slug in {".", ".."}
        or Path(slug).is_absolute()
    ):
        raise AtestoError(f"slug invalido: {slug!r}")
    return slug


def artifacts_dir(root: Path, config: Config, slug: str) -> Path:
    validate_slug(slug)
    base = _safe_artifacts_root(root, config.artifacts_root)
    directory = base / slug
    ensure_dir(root, directory)
    return directory


def list_slugs(root: Path, config: Config) -> list[str]:
    base = _safe_artifacts_root(root, config.artifacts_root)
    if not base.is_dir() or base.is_symlink():
        return []
    return sorted(
        item.name
        for item in base.iterdir()
        if item.is_dir() and not item.is_symlink()
    )


def _status_entries(root: Path) -> list[tuple[str, str, str]]:
    """Retorna (codigo, path, path_original) do git status."""
    raw = git_bytes(
        root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    )
    fields = _decode(raw).split("\0")
    entries: list[tuple[str, str, str]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        code = field[:2]
        path = field[3:] if len(field) > 3 else ""
        original = ""
        if (code[:1] in {"R", "C"} or code[1:2] in {"R", "C"}) and index < len(
            fields
        ):
            original = fields[index]
            index += 1
        entries.append((code, path, original))
    return entries


def _object_format(root: Path) -> str:
    result = git(root, ["rev-parse", "--show-object-format"], check=False)
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value if value in {"sha1", "sha256"} else "sha1"


def _blob_sha(data: bytes, algorithm: str = "sha1") -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.new(algorithm, header + data, usedforsecurity=False).hexdigest()


def _git_hash_object(root: Path, rel: str, full: Path) -> str:
    result = subprocess.run(
        ["git", "hash-object", f"--path={rel}", "--", str(full)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GateError(
            f"git hash-object falhou em {rel}: {result.stderr.strip()}"
        )
    value = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise GateError(f"git hash-object retornou hash invalido em {rel}")
    return value


def _file_mode(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return "100644"
    if stat.S_ISLNK(mode):
        return "120000"
    return "100755" if mode & stat.S_IXUSR else "100644"


def tree_fingerprint(
    root: Path,
    config: Config,
    *,
    ref: str | None = None,
) -> dict[str, Any]:
    ensure_toplevel(root)
    head = resolve_ref(root, ref) if ref else git_head(root)
    excludes = config.excludes_for(root)
    tracked: dict[str, tuple[str, str]] = {}
    raw_tree = git_bytes(root, ["ls-tree", "-r", "-z", head])
    for entry in raw_tree.split(b"\0"):
        if not entry:
            continue
        meta, path_raw = entry.split(b"\t", 1)
        mode, _kind, sha = _decode(meta).split(" ", 2)
        path = _decode(path_raw)
        if is_excluded(path, excludes):
            continue
        tracked[path] = (mode, sha)

    dirty_paths: list[str] = []
    if ref is None:
        for code, path, original in _status_entries(root):
            path = _norm(path)
            original = _norm(original)
            if original and not is_excluded(original, excludes):
                tracked.pop(original, None)
            if not path or is_excluded(path, excludes):
                continue
            full = root / path
            deleted = code.strip() == "D" or (
                not full.is_symlink() and not full.exists()
            )
            if deleted:
                tracked.pop(path, None)
                dirty_paths.append(path)
                continue
            if full.is_symlink():
                target = os.readlink(full)
                digest = _blob_sha(
                    target.encode("utf-8", "surrogateescape"),
                    _object_format(root),
                )
            elif full.is_dir():
                sub_head = git(full, ["rev-parse", "HEAD"], check=False)
                if sub_head.returncode != 0:
                    continue
                sub_status = git_bytes(
                    full,
                    ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
                )
                sub_diff = git_bytes(full, ["diff", "--binary", "HEAD"])
                digest = hashlib.sha256(
                    sub_head.stdout.encode("utf-8", "surrogateescape")
                    + b"\0"
                    + sub_status
                    + b"\0"
                    + sub_diff
                ).hexdigest()
                tracked[path] = ("160000", digest)
                dirty_paths.append(path)
            elif full.is_file():
                digest = _git_hash_object(root, path, full)
            else:
                continue
            tracked[path] = (_file_mode(full), digest)
            dirty_paths.append(path)

    lines = [
        f"{path}\0{mode}\0{sha}\n"
        for path, (mode, sha) in sorted(tracked.items())
    ]
    payload = "atesto-tree-v1\n" + "".join(lines)
    return {
        "head": head,
        "tree_sha256": hashlib.sha256(payload.encode("utf-8", "surrogateescape")).hexdigest(),
        "dirty": bool(dirty_paths),
        "dirty_paths": sorted(set(dirty_paths)),
    }


def _validate_proof(
    directory: Path,
    root: Path,
    config: Config,
    head: str,
    current: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    problems: list[str] = []
    path = directory / "proof.json"
    if not path.is_file() or path.is_symlink():
        return None, ["proof.json ausente"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"proof.json ilegivel: {exc}"]
    if not isinstance(data, dict):
        return None, ["proof.json precisa ser objeto"]
    if type(data.get("schema")) is not int or data.get("schema") != PROOF_SCHEMA:
        problems.append("proof.json schema precisa ser 1")
    if data.get("kind") != "command":
        problems.append("proof.json kind precisa ser command")
    proof_head = str(data.get("head") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", proof_head):
        problems.append("proof.json head invalido")
    elif not is_ancestor(root, proof_head, head):
        problems.append(
            f"proof.json head {proof_head[:12]} nao e ancestral de {head[:12]}"
        )
    tree_hash = str(data.get("tree_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", tree_hash):
        problems.append("proof.json tree_sha256 invalido")
    elif tree_hash != current["tree_sha256"]:
        problems.append(
            "prova stale: a arvore de codigo mudou depois da prova"
        )
    if not isinstance(data.get("dirty"), bool):
        problems.append("proof.json dirty precisa ser booleano")
    dirty_paths = data.get("dirty_paths")
    if not isinstance(dirty_paths, list) or not all(
        isinstance(item, str) for item in dirty_paths
    ):
        problems.append("proof.json dirty_paths precisa ser lista de strings")
    elif isinstance(data.get("dirty"), bool) and data["dirty"] != bool(
        dirty_paths
    ):
        problems.append("proof.json dirty nao bate com dirty_paths")
    command = data.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item.strip() for item in command)
    ):
        problems.append("proof.json command precisa ser lista nao vazia")
    exit_code = data.get("exit")
    if type(exit_code) is not int or exit_code != 0:
        problems.append(f"proof.json exit={exit_code!r} (esperado 0)")
    output = data.get("output")
    if not isinstance(output, dict):
        problems.append("proof.json output precisa ser objeto")
    else:
        rel = str(output.get("path") or "")
        full = (directory / rel).resolve()
        if not rel or _inside(directory, full) is None:
            problems.append("proof.json output.path invalido")
        elif not full.is_file():
            problems.append("proof.json output nao existe")
        else:
            raw = full.read_bytes()
            expected_bytes = output.get("bytes")
            expected_hash = str(output.get("sha256") or "").lower()
            if type(expected_bytes) is not int or expected_bytes != len(raw):
                problems.append("proof.json output.bytes nao confere")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                problems.append("proof.json output.sha256 invalido")
            elif hashlib.sha256(raw).hexdigest() != expected_hash:
                problems.append("proof.json output.sha256 nao confere")
    return data, problems


def _validate_review(
    directory: Path,
    root: Path,
    config: Config,
    head: str,
    current: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    problems: list[str] = []
    path = directory / "review.json"
    if not path.is_file() or path.is_symlink():
        return None, ["review.json ausente"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"review.json ilegivel: {exc}"]
    if not isinstance(data, dict):
        return None, ["review.json precisa ser objeto"]
    if type(data.get("schema")) is not int or data.get("schema") != REVIEW_SCHEMA:
        problems.append("review.json schema precisa ser 1")
    intent = str(data.get("intent") or "").strip()
    if not intent:
        problems.append("review.json intent vazio")
    review_head = str(data.get("head") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", review_head):
        problems.append("review.json head invalido")
    elif not is_ancestor(root, review_head, head):
        problems.append(
            f"review.json head {review_head[:12]} nao e ancestral de {head[:12]}"
        )
    tree_hash = str(data.get("tree_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", tree_hash):
        problems.append("review.json tree_sha256 invalido")
    elif tree_hash != current["tree_sha256"]:
        problems.append("review.json tree_sha256 nao corresponde a arvore")
    if str(data.get("verdict") or "").strip() != "APPROVED":
        problems.append("review.json verdict precisa ser APPROVED")
    reviewers = data.get("reviewers")
    if not isinstance(reviewers, list) or not all(
        isinstance(item, str) and item.strip() for item in reviewers
    ):
        problems.append("review.json reviewers precisa ser lista de strings")
    else:
        clean_reviewers = list(
            dict.fromkeys(item.strip() for item in reviewers)
        )
        if len(clean_reviewers) < config.min_reviewers:
            problems.append(
                f"review.json precisa de pelo menos {config.min_reviewers} revisor(es) unicos"
            )
    findings = data.get("findings")
    if not isinstance(findings, list):
        problems.append("review.json findings precisa ser lista")
    if review_head and review_head != head:
        changed = changed_paths(root, review_head, head)
        review_paths = config.review_artifacts_paths or [config.artifacts_root]
        stale = [
            item
            for item in changed
            if not is_excluded(item, [*config.excludes_for(root), *review_paths])
        ]
        if stale:
            problems.append(
                "review stale: mudou depois do head revisado: "
                + ", ".join(stale[:5])
            )
    return data, problems


def _find_artifact_dir(
    root: Path,
    config: Config,
    slug: str | None,
    base: str | None,
    head: str,
    current: dict[str, Any],
) -> Path:
    base_path = _safe_artifacts_root(root, config.artifacts_root)
    if slug:
        validate_slug(slug)
        directory = base_path / slug
        if not directory.is_dir() or directory.is_symlink():
            raise GateError(f"artefato nao existe: {directory}")
        return directory
    candidates = [
        base_path / item
        for item in list_slugs(root, config)
        if (base_path / item / "proof.json").is_file()
    ]
    if base:
        changed = changed_paths(root, base)
        filtered = [
            item
            for item in candidates
            if any(
                path == _norm(item.relative_to(root).as_posix())
                or path.startswith(
                    _norm(item.relative_to(root).as_posix()) + "/"
                )
                for path in changed
            )
        ]
        if filtered:
            candidates = filtered
    if len(candidates) > 1:
        valid: list[Path] = []
        for candidate in candidates:
            _data, problems = _validate_proof(
                candidate, root, config, head, current
            )
            if problems:
                continue
            if "review" in config.require or (candidate / "review.json").exists():
                _review, review_problems = _validate_review(
                    candidate, root, config, head, current
                )
                if review_problems:
                    continue
            valid.append(candidate)
        if len(valid) == 1:
            return valid[0]
        if len(valid) > 1:
            def _key(candidate: Path) -> tuple[str, str, str]:
                try:
                    data = json.loads(
                        (candidate / "proof.json").read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    return ("", "", candidate.name)
                return (
                    str(data.get("finished_at") or ""),
                    str(data.get("head") or ""),
                    candidate.name,
                )

            return max(valid, key=_key)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise GateError(
            f"nenhum artefato com proof.json em {Path(config.artifacts_root)}"
        )
    names = ", ".join(item.name for item in candidates)
    raise GateError(
        f"mais de um artefato com prova ({names}); use --slug"
    )


def check(
    root: Path,
    *,
    base: str | None = None,
    slug: str | None = None,
    ci: bool = False,
    run_test: bool = True,
) -> dict[str, Any]:
    if ci:
        if not base:
            raise AtestoError("--ci exige --base")
        base_name = None
        for name in CONFIG_NAMES:
            if git(
                root,
                ["cat-file", "-e", f"{base}:{name}"],
                check=False,
            ).returncode == 0:
                base_name = name
                break
        if base_name is None:
            raise GateError(
                "config do Atesto nao existe na base; adicione atesto.json "
                "(ou pstack.json) na base antes do PR"
            )
        shown = git(root, ["show", f"{base}:{base_name}"], check=False)
        if shown.returncode != 0:
            raise GateError(f"nao foi possivel ler {base_name} da base")
        try:
            base_data = json.loads(shown.stdout)
        except ValueError as exc:
            raise GateError(f"{base_name} da base e invalido: {exc}") from exc
        config = Config.from_data(base_data, root / base_name, root)
        head_name = None
        for name in CONFIG_NAMES:
            if git(
                root, ["cat-file", "-e", f"HEAD:{name}"], check=False
            ).returncode == 0:
                head_name = name
                break
        if head_name is None:
            raise GateError(
                "config do Atesto ausente no HEAD; o PR nao pode remover a config"
            )
        shown_head = git(root, ["show", f"HEAD:{head_name}"], check=False)
        if shown_head.returncode != 0:
            raise GateError(f"nao foi possivel ler {head_name} do HEAD")
        try:
            head_data = json.loads(shown_head.stdout)
        except ValueError as exc:
            raise GateError(f"{head_name} do HEAD ilegivel: {exc}") from exc
        head_config = Config.from_data(head_data, root / head_name, root)
        missing = set(config.require) - set(head_config.require)
        if missing:
            raise GateError(
                "config do HEAD remove requisito da base: "
                + ", ".join(sorted(missing))
            )
        if head_config.min_reviewers < config.min_reviewers:
            raise GateError("config do HEAD reduz min_reviewers da base")
        if head_config.artifacts_root != config.artifacts_root:
            raise GateError("config do HEAD muda artifacts_root da base")
        if head_config.test_cmd != config.test_cmd:
            raise GateError("config do HEAD muda test_cmd da base")
        extra_excludes = set(head_config.excludes) - set(config.excludes)
        if extra_excludes:
            raise GateError(
                "config do HEAD adiciona excludes nao presentes na base"
            )
        extra_review_paths = set(head_config.review_artifacts_paths) - set(
            config.review_artifacts_paths
        )
        if extra_review_paths:
            raise GateError(
                "config do HEAD adiciona review_artifacts_paths da base"
            )
        # A base define o piso de requisitos/test_cmd/reviewers. O HEAD pode
        # endurecer (remover excludes/review paths) e e ele que descreve a
        # arvore efetivamente provada; usar a base para o fingerprint faria
        # uma prova valida do HEAD parecer stale.
        config = Config(
            version=1,
            artifacts_root=head_config.artifacts_root,
            require=list(config.require),
            min_reviewers=config.min_reviewers,
            test_cmd=config.test_cmd,
            review_artifacts_paths=list(head_config.review_artifacts_paths),
            excludes=list(head_config.excludes),
            path=head_config.path,
        )
    else:
        config = load_config(root)
    head = git_head(root)
    current = tree_fingerprint(root, config, ref="HEAD" if ci else None)
    directory = _find_artifact_dir(
        root, config, slug, base, head, current
    )
    proof, problems = _validate_proof(directory, root, config, head, current)

    review = None
    if "review" in config.require or (directory / "review.json").exists():
        review, review_problems = _validate_review(
            directory, root, config, head, current
        )
        problems.extend(review_problems)

    if base and proof is not None:
        changed = changed_paths(root, base)
        rel = _norm(directory.relative_to(root).as_posix())
        if not any(
            item == rel or item.startswith(rel + "/") for item in changed
        ):
            problems.append("artefato nao faz parte do diff do PR")

    test_exit: int | None = None
    test_output = ""
    if config.test_cmd and run_test and not problems:
        result = subprocess.run(
            config.test_cmd,
            cwd=root,
            shell=True,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
        )
        test_exit = result.returncode
        test_output = (result.stdout + result.stderr)[-4000:]
        if result.returncode != 0:
            problems.append(f"test_cmd falhou (exit {result.returncode})")
        after_test = git(
            root,
            ["status", "--porcelain=v1", "--untracked-files=no"],
            check=False,
        )
        if after_test.stdout.strip():
            problems.append("test_cmd alterou arquivos rastreados")
        if git_head(root) != head:
            problems.append("test_cmd criou commit")

    return {
        "ok": not problems,
        "problems": problems,
        "head": head,
        "artifacts": _norm(directory.relative_to(root).as_posix()),
        "config": config.path.name if config.path else "atesto.json",
        "require": config.require,
        "proof": proof,
        "review": review,
        "test_exit": test_exit,
        "test_output": test_output,
        "tree_sha256": current["tree_sha256"],
        "dirty": current["dirty"],
        "dirty_paths": current["dirty_paths"],
    }


def make_proof(
    root: Path,
    slug: str,
    command: list[str],
    *,
    capture_output: bool = True,
) -> tuple[int, Path]:
    ensure_toplevel(root)
    if not command or not all(
        isinstance(item, str) and item.strip() for item in command
    ):
        raise AtestoError("proof exige um comando nao vazio depois de --")
    config = load_config(root)
    directory = artifacts_dir(root, config, slug)
    started = now_iso()
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
        )
    except OSError as exc:
        raise AtestoError(f"comando nao iniciou: {exc}") from exc
    finished = now_iso()
    output = result.stdout + result.stderr if capture_output else ""
    raw = output.encode("utf-8", "surrogateescape")
    truncated = len(raw) > MAX_OUTPUT_BYTES
    if truncated:
        raw = raw[:MAX_OUTPUT_BYTES]
    output_path = directory / "proof-output.txt"
    write_regular(output_path, raw)
    fingerprint = tree_fingerprint(root, config)
    proof = {
        "schema": PROOF_SCHEMA,
        "kind": "command",
        "head": fingerprint["head"],
        "tree_sha256": fingerprint["tree_sha256"],
        "dirty": fingerprint["dirty"],
        "dirty_paths": fingerprint["dirty_paths"],
        "command": command,
        "started_at": started,
        "finished_at": finished,
        "exit": result.returncode,
        "output": {
            "path": "proof-output.txt",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "truncated": truncated,
        },
    }
    write_regular(
        directory / "proof.json",
        json.dumps(proof, ensure_ascii=True, indent=2) + "\n",
    )
    return result.returncode, directory


def write_review(
    root: Path,
    slug: str,
    *,
    head: str = "HEAD",
    verdict: str = "APPROVED",
    reviewers: list[str] | None = None,
    intent: str = "",
) -> Path:
    ensure_toplevel(root)
    verdict = verdict.upper()
    if verdict not in VALID_VERDICTS:
        raise AtestoError(f"verdict invalido: {verdict}")
    reviewers = [item.strip() for item in (reviewers or []) if item.strip()]
    if not reviewers:
        raise AtestoError("review exige pelo menos um --reviewer")
    config = load_config(root)
    resolved = resolve_ref(root, head)
    fingerprint = tree_fingerprint(root, config, ref=resolved)
    directory = artifacts_dir(root, config, slug)
    review = {
        "schema": REVIEW_SCHEMA,
        "head": resolved,
        "tree_sha256": fingerprint["tree_sha256"],
        "intent": intent.strip() or f"review de {slug}",
        "verdict": verdict,
        "reviewers": list(dict.fromkeys(reviewers)),
        "findings": [],
    }
    write_regular(
        directory / "review.json",
        json.dumps(review, ensure_ascii=True, indent=2) + "\n",
    )
    return directory


def init_config(
    root: Path,
    *,
    artifacts_root: str = DEFAULT_ARTIFACTS_ROOT,
    require: list[str] | None = None,
    test_cmd: str = "",
    force: bool = False,
) -> Path:
    ensure_toplevel(root)
    path = root / "atesto.json"
    if path.is_symlink():
        raise AtestoError("atesto.json nao pode ser symlink")
    if path.exists() and not force:
        raise AtestoError("atesto.json ja existe; use --force para recriar")
    require = (
        ["proof"] if require is None else list(dict.fromkeys(require))
    )
    unknown = set(require) - VALID_REQUIREMENTS
    if unknown:
        raise AtestoError(f"require desconhecido: {sorted(unknown)}")
    if "proof" not in require:
        raise AtestoError("require precisa incluir proof")
    artifacts_root = normalize_rel_path(artifacts_root, "artifacts_root")
    data = {
        "version": 1,
        "artifacts_root": artifacts_root,
        "require": require,
        "min_reviewers": 1,
        "test_cmd": test_cmd,
        "review_artifacts_paths": [artifacts_root],
    }
    _safe_artifacts_root(root, artifacts_root)
    write_regular(
        path, json.dumps(data, ensure_ascii=True, indent=2) + "\n"
    )
    ensure_dir(root, _safe_artifacts_root(root, artifacts_root))
    return path


HOOK_MARKER = "# atesto pre-push hook"


def hook_path(root: Path) -> Path:
    result = git(root, ["rev-parse", "--git-path", "hooks"], check=False)
    if result.returncode == 0 and result.stdout.strip():
        hooks = Path(result.stdout.strip())
        if not hooks.is_absolute():
            hooks = root / hooks
        return hooks / "pre-push"
    return root / ".git" / "hooks" / "pre-push"


def install_hook(root: Path, *, force: bool = False) -> Path:
    probe = git(root, ["rev-parse", "--git-dir"], check=False)
    if probe.returncode != 0:
        raise AtestoError("nao e um repo git")
    path = hook_path(root)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise AtestoError(f"hook nao pode ser symlink: {path}")
    if path.exists() and HOOK_MARKER not in path.read_text(
        encoding="utf-8", errors="replace"
    ):
        if not force:
            raise AtestoError(
                f"hook existente nao e do Atesto: {path}; use --force"
            )
        path.with_suffix(".pre-atesto").write_text(
            path.read_text(encoding="utf-8", errors="replace"),
            encoding="utf-8",
        )
    path.write_text(
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        f"exec {shlex.quote(sys.executable)} -m atesto check --local || exit 1\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def uninstall_hook(root: Path) -> bool:
    path = hook_path(root)
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    if HOOK_MARKER not in text:
        raise AtestoError(f"hook nao e do Atesto: {path}")
    path.unlink()
    backup = path.with_suffix(".pre-atesto")
    if backup.is_file():
        backup.rename(path)
        path.chmod(0o755)
    return True


def hook_status(root: Path) -> str:
    path = hook_path(root)
    if not path.is_file():
        return "ausente"
    text = path.read_text(encoding="utf-8", errors="replace")
    return "instalado" if HOOK_MARKER in text else "outro"
