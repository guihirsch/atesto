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
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

VERSION = "0.2.0"
TOOL_NAME = "atesto"
CONFIG_NAMES = ("atesto.json", "pstack.json")
DEFAULT_ARTIFACTS_ROOT = "docs/atesto"
PROOF_SCHEMA = 1
REVIEW_SCHEMA = 1
REVIEW_CONTRACT_KEYS = frozenset(
    {"schema", "source", "head", "verdict", "reviewers", "intent"}
)
REVIEW_SOURCE_KEYS = frozenset({"kind", "ref"})
REVIEW_SOURCE_KINDS = frozenset({"github", "gitlab", "json"})
MAX_OUTPUT_BYTES = 2_000_000
TEST_OUTPUT_TAIL = 4000
TERMINATION_GRACE_SECONDS = 0.5
VALID_REQUIREMENTS = {"proof", "review"}
VALID_VERDICTS = {"APPROVED", "CHANGES_REQUESTED"}
TOOL_VERSION_PATTERN = re.compile(
    r"^\d+\.\d+\.\d+(?:[.+-]?[0-9A-Za-z]+(?:[.+-][0-9A-Za-z]+)*)?$"
)
MIGRATION_NOTE = (
    "veja a nota de migracao em README.md (Migração para 0.2.0) "
    "e CHANGELOG.md"
)


class ArtifactStatus(str, Enum):
    """Valores estaveis para o estado dos artefatos no `doctor`."""

    ABSENT = "ausente"
    EMPTY = "vazio"
    FRESH = "fresh"
    INVALID = "invalida"
    UNREADABLE = "ilegivel"
    PRESENT = "presente"

    def __str__(self) -> str:
        return self.value


def _uses_process_groups() -> bool:
    return os.name == "posix"


def _unknown_keys_message(name: str, version: Any, keys: list[str]) -> str:
    return (
        f"{name} tem chaves desconhecidas para version {version}: "
        + ", ".join(keys)
        + "; remova-as ou mova a politica de dominio para a config do "
        "adaptador; "
        + MIGRATION_NOTE
    )

# Top-level keys accepted for each config version. A key that is not in the
# set for the declared version is an error: the gate must not silently
# under-enforce a config that looks stronger than it is.
CONFIG_KEYS_V1 = frozenset(
    {
        "version",
        "artifacts_root",
        "require",
        "min_reviewers",
        "test_cmd",
        "review_artifacts_paths",
        "fingerprint_excludes",
        "excludes",
    }
)
CONFIG_KEYS_V2 = CONFIG_KEYS_V1 | {"test_timeout_seconds"}
CONFIG_KEYS_BY_VERSION: dict[int, frozenset[str]] = {
    1: CONFIG_KEYS_V1,
    2: CONFIG_KEYS_V2,
}


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


def config_path(root: Path) -> Path | None:
    """Retorna o arquivo de config escolhido, sem le-lo."""
    for name in CONFIG_NAMES:
        candidate = root / name
        if candidate.is_symlink():
            raise AtestoError(f"{name} nao pode ser symlink")
        if candidate.is_file():
            return candidate
    return None


@dataclass
class Config:
    # Sem `version` explicita a config e v1; `init` grava v2.
    version: int = 1
    artifacts_root: str = DEFAULT_ARTIFACTS_ROOT
    require: list[str] = field(default_factory=lambda: ["proof"])
    min_reviewers: int = 1
    test_cmd: str = ""
    test_timeout_seconds: int | None = None
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
        if type(version) is not int or version not in CONFIG_KEYS_BY_VERSION:
            supported = ", ".join(
                str(item) for item in sorted(CONFIG_KEYS_BY_VERSION)
            )
            raise AtestoError(f"{path.name} version precisa ser {supported}")
        unknown_keys = sorted(set(data) - CONFIG_KEYS_BY_VERSION[version])
        if unknown_keys:
            raise AtestoError(
                _unknown_keys_message(path.name, version, unknown_keys)
            )
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
        if "test_timeout_seconds" in data:
            test_timeout_seconds = data["test_timeout_seconds"]
            if (
                type(test_timeout_seconds) is not int
                or test_timeout_seconds < 1
            ):
                raise AtestoError(
                    f"{path.name} test_timeout_seconds precisa ser inteiro >= 1"
                )
        else:
            test_timeout_seconds = None
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
            version=version,
            artifacts_root=artifacts_root.strip(),
            require=list(dict.fromkeys(require)),
            min_reviewers=min_reviewers,
            test_cmd=test_cmd.strip(),
            test_timeout_seconds=test_timeout_seconds,
            review_artifacts_paths=[item.strip() for item in review_paths],
            excludes=[item.strip() for item in excludes],
            path=path,
        )
        _safe_artifacts_root(root, config.artifacts_root)
        return config

    @classmethod
    def load(cls, root: Path) -> Config:
        chosen = config_path(root)
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


def _validate_tool(data: dict[str, Any], label: str) -> list[str]:
    if "tool" not in data:
        return []
    tool = data["tool"]
    if not isinstance(tool, dict):
        return [f"{label} tool precisa ser objeto"]
    problems: list[str] = []
    name = tool.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"{label} tool.name precisa ser string nao vazia")
    version = tool.get("version")
    if not isinstance(version, str) or not version.strip():
        problems.append(f"{label} tool.version precisa ser string nao vazia")
    elif TOOL_VERSION_PATTERN.fullmatch(version.strip()) is None:
        problems.append(
            f"{label} tool.version precisa ser semver-like "
            "(ex.: 0.2.0, 0.3.0rc1)"
        )
    return problems


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
    problems.extend(_validate_tool(data, "proof.json"))
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


def _validate_review_source(data: dict[str, Any]) -> list[str]:
    if "source" not in data:
        return []
    source = data["source"]
    if not isinstance(source, dict):
        return ["review.json source precisa ser objeto"]
    problems: list[str] = []
    unknown = sorted(set(source) - REVIEW_SOURCE_KEYS)
    if unknown:
        problems.append(
            "review.json source tem chaves desconhecidas: "
            + ", ".join(unknown)
        )
    missing = sorted(REVIEW_SOURCE_KEYS - set(source))
    if missing:
        problems.append(
            "review.json source tem chaves obrigatorias ausentes: "
            + ", ".join(missing)
        )
    kind = source.get("kind")
    if not isinstance(kind, str) or kind not in REVIEW_SOURCE_KINDS:
        problems.append(
            "review.json source.kind precisa ser github, gitlab ou json"
        )
    ref = source.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        problems.append(
            "review.json source.ref precisa ser string nao vazia"
        )
    return problems


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
    problems.extend(_validate_tool(data, "review.json"))
    problems.extend(_validate_review_source(data))
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
            review_path = candidate / "review.json"
            if review_path.exists():
                _review, review_problems = _validate_review(
                    candidate, root, config, head, current
                )
                if review_problems:
                    raise GateError(
                        f"review invalida em {candidate.name}: "
                        + "; ".join(review_problems[:3])
                    )
            elif "review" in config.require:
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


def _process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_test_process(process: subprocess.Popen[str]) -> None:
    """Encerra o test_cmd e seus descendentes.

    Em POSIX o comando roda em uma sessao propria; SIGTERM e enviado ao grupo,
    seguido de SIGKILL depois de uma graca curta. Em plataformas sem grupos de
    processo apenas o filho direto e encerrado (limitacao conhecida).
    """
    if not _uses_process_groups():
        process.terminate()
        try:
            process.wait(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        process.poll()
        if not _process_group_alive(pgid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_test_cmd(
    root: Path,
    command: str,
    timeout_seconds: int | None,
) -> tuple[int, str, bool]:
    """Executa test_cmd; retorna (exit, output, timed_out).

    Sem timeout, preserva o comportamento anterior (subprocess.run). Com
    timeout, roda em um grupo de processos proprio para que o limite valha
    para o comando e seus descendentes.
    """
    if timeout_seconds is None:
        result = subprocess.run(
            command,
            cwd=root,
            shell=True,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
        )
        output = (result.stdout + result.stderr)[-TEST_OUTPUT_TAIL:]
        return result.returncode, output, False
    popen_kwargs: dict[str, bool] = {}
    if _uses_process_groups():
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(
        command,
        cwd=root,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="surrogateescape",
        **popen_kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_test_process(process)
        stdout, stderr = process.communicate()
        exit_code = (
            process.returncode
            if process.returncode is not None
            else -signal.SIGKILL
        )
        return exit_code, (stdout + stderr)[-TEST_OUTPUT_TAIL:], True
    return process.returncode, (stdout + stderr)[-TEST_OUTPUT_TAIL:], False


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
        try:
            config = Config.from_data(base_data, root / base_name, root)
        except AtestoError as exc:
            raise GateError(f"config da base invalida: {exc}") from exc
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
        try:
            head_config = Config.from_data(head_data, root / head_name, root)
        except AtestoError as exc:
            raise GateError(f"config do HEAD invalida: {exc}") from exc
        if head_config.version < config.version:
            raise GateError(
                "config do HEAD faz downgrade de version da base "
                f"({head_config.version} < {config.version})"
            )
        base_timeout = config.test_timeout_seconds
        head_timeout = head_config.test_timeout_seconds
        if base_timeout is not None:
            if head_timeout is None:
                raise GateError(
                    "config do HEAD remove test_timeout_seconds da base"
                )
            if head_timeout > base_timeout:
                raise GateError(
                    "config do HEAD aumenta test_timeout_seconds da base"
                )
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
            version=head_config.version,
            artifacts_root=head_config.artifacts_root,
            require=list(config.require),
            min_reviewers=config.min_reviewers,
            test_cmd=config.test_cmd,
            test_timeout_seconds=(
                head_timeout if head_timeout is not None else base_timeout
            ),
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
    timed_out = False
    if config.test_cmd and run_test and not problems:
        before_test = git(
            root,
            ["status", "--porcelain=v1", "--untracked-files=no"],
            check=False,
        )
        test_exit, test_output, timed_out = _run_test_cmd(
            root, config.test_cmd, config.test_timeout_seconds
        )
        if timed_out:
            problems.append(
                f"test_cmd excedeu o timeout de "
                f"{config.test_timeout_seconds}s: {config.test_cmd}"
            )
        elif test_exit != 0:
            problems.append(f"test_cmd falhou (exit {test_exit})")
        after_test = git(
            root,
            ["status", "--porcelain=v1", "--untracked-files=no"],
            check=False,
        )
        if after_test.stdout != before_test.stdout:
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
        "tool": proof.get("tool") if proof is not None else None,
        "proof": proof,
        "review": review,
        "test_exit": test_exit,
        "test_output": test_output,
        "test_timeout_seconds": config.test_timeout_seconds,
        "timed_out": timed_out,
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
        "tool": {"name": TOOL_NAME, "version": VERSION},
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


def _write_review_artifact(
    root: Path,
    slug: str,
    *,
    head: str,
    tree_sha256: str,
    intent: str,
    verdict: str,
    reviewers: list[str],
    source: dict[str, str] | None = None,
) -> Path:
    config = load_config(root)
    directory = artifacts_dir(root, config, slug)
    review: dict[str, Any] = {
        "schema": REVIEW_SCHEMA,
        "tool": {"name": TOOL_NAME, "version": VERSION},
    }
    if source is not None:
        review["source"] = source
    review.update(
        {
            "head": head,
            "tree_sha256": tree_sha256,
            "intent": intent,
            "verdict": verdict,
            "reviewers": reviewers,
            "findings": [],
        }
    )
    write_regular(
        directory / "review.json",
        json.dumps(review, ensure_ascii=True, indent=2) + "\n",
    )
    return directory


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
    return _write_review_artifact(
        root,
        slug,
        head=resolved,
        tree_sha256=fingerprint["tree_sha256"],
        intent=intent.strip() or f"review de {slug}",
        verdict=verdict,
        reviewers=list(dict.fromkeys(reviewers)),
    )


def _contract_error(path: Path, message: str) -> AtestoError:
    return AtestoError(f"review JSON em {path}: {message}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"chave duplicada: {key}")
        result[key] = value
    return result


def load_review_contract(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _contract_error(path, f"ilegivel: {exc}") from exc
    try:
        data = json.loads(text, object_pairs_hook=_unique_object)
    except ValueError as exc:
        raise _contract_error(path, f"malformado: {exc}") from exc
    if not isinstance(data, dict):
        raise _contract_error(path, "precisa ser objeto")
    unknown = sorted(set(data) - REVIEW_CONTRACT_KEYS)
    if unknown:
        raise _contract_error(
            path, "chaves desconhecidas: " + ", ".join(unknown)
        )
    missing = sorted(REVIEW_CONTRACT_KEYS - set(data))
    if missing:
        raise _contract_error(
            path, "chaves obrigatorias ausentes: " + ", ".join(missing)
        )
    schema = data["schema"]
    if type(schema) is not int or schema != 1:
        raise _contract_error(path, "schema precisa ser 1")
    source = data["source"]
    if not isinstance(source, dict):
        raise _contract_error(path, "source precisa ser objeto")
    unknown_source = sorted(set(source) - REVIEW_SOURCE_KEYS)
    if unknown_source:
        raise _contract_error(
            path,
            "source tem chaves desconhecidas: " + ", ".join(unknown_source),
        )
    missing_source = sorted(REVIEW_SOURCE_KEYS - set(source))
    if missing_source:
        raise _contract_error(
            path,
            "source tem chaves obrigatorias ausentes: "
            + ", ".join(missing_source),
        )
    kind = source["kind"]
    if not isinstance(kind, str) or kind not in REVIEW_SOURCE_KINDS:
        raise _contract_error(
            path, "source.kind precisa ser github, gitlab ou json"
        )
    ref = source["ref"]
    if not isinstance(ref, str) or not ref.strip():
        raise _contract_error(path, "source.ref precisa ser string nao vazia")
    head = data["head"]
    if not isinstance(head, str) or not head.strip():
        raise _contract_error(path, "head precisa ser string nao vazia")
    verdict = data["verdict"]
    if not isinstance(verdict, str) or not verdict.strip():
        raise _contract_error(path, "verdict vazio")
    verdict = verdict.strip().upper()
    if verdict not in VALID_VERDICTS:
        raise _contract_error(
            path, f"verdict invalido: {verdict} (use APPROVED ou CHANGES_REQUESTED)"
        )
    reviewers = data["reviewers"]
    if (
        not isinstance(reviewers, list)
        or not reviewers
        or not all(
            isinstance(item, str) and item.strip() for item in reviewers
        )
    ):
        raise _contract_error(
            path, "reviewers precisa ser lista nao vazia de strings"
        )
    intent = data["intent"]
    if not isinstance(intent, str) or not intent.strip():
        raise _contract_error(path, "intent precisa ser string nao vazia")
    return {
        "source": {"kind": kind, "ref": ref.strip()},
        "head": head.strip(),
        "verdict": verdict,
        "reviewers": list(
            dict.fromkeys(item.strip() for item in reviewers)
        ),
        "intent": intent.strip(),
    }


def write_review_from_json(root: Path, slug: str, path: Path) -> Path:
    ensure_toplevel(root)
    contract = load_review_contract(path)
    config = load_config(root)
    head = git_head(root)
    current_tree = tree_fingerprint(root, config, ref=head)
    try:
        resolved = resolve_ref(root, contract["head"])
    except GateError as exc:
        raise GateError(f"review JSON head invalido: {exc}") from exc
    # A review binds to the evidence tree, not to a commit id: an evidence-only
    # commit on top of the approved head does not make the approval stale.
    if resolved != head:
        if not is_ancestor(root, resolved, head):
            raise GateError(
                f"review JSON head {resolved[:12]} nao e HEAD nem ancestral "
                f"de {head[:12]}"
            )
        approved_tree = tree_fingerprint(root, config, ref=resolved)
        if approved_tree["tree_sha256"] != current_tree["tree_sha256"]:
            raise GateError(
                f"review JSON head {resolved[:12]} e ancestral, mas a arvore "
                f"mudou em relacao a {head[:12]}"
            )
    return _write_review_artifact(
        root,
        slug,
        head=head,
        tree_sha256=current_tree["tree_sha256"],
        intent=contract["intent"],
        verdict=contract["verdict"],
        reviewers=contract["reviewers"],
        source=contract["source"],
    )


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
        "version": 2,
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


def _doctor_fallback_config(data: dict[str, Any], path: Path | None) -> Config:
    """Config tolerante para diagnostico quando a validacao estrita falha.

    Serve apenas para localizar artefatos e mostrar valores efetivos; nao
    substitui a validacao de `Config.from_data`.
    """
    raw_root = data.get("artifacts_root", DEFAULT_ARTIFACTS_ROOT)
    if not isinstance(raw_root, str) or not raw_root.strip():
        artifacts_root = DEFAULT_ARTIFACTS_ROOT
    else:
        try:
            artifacts_root = normalize_rel_path(raw_root, "artifacts_root")
        except AtestoError:
            artifacts_root = DEFAULT_ARTIFACTS_ROOT
    require = data.get("require", ["proof"])
    if (
        not isinstance(require, list)
        or not require
        or not all(isinstance(item, str) for item in require)
        or "proof" not in require
        or set(require) - VALID_REQUIREMENTS
    ):
        require = ["proof"]
    else:
        require = list(dict.fromkeys(require))
    min_reviewers = data.get("min_reviewers", 1)
    if type(min_reviewers) is not int or min_reviewers < 1:
        min_reviewers = 1
    test_cmd = data.get("test_cmd", "")
    if not isinstance(test_cmd, str):
        test_cmd = ""
    test_timeout_seconds = data.get("test_timeout_seconds")
    if type(test_timeout_seconds) is not int or test_timeout_seconds < 1:
        test_timeout_seconds = None
    review_paths = data.get("review_artifacts_paths", [artifacts_root])
    if not isinstance(review_paths, list) or not all(
        isinstance(item, str) and item.strip() for item in review_paths
    ):
        review_paths = [artifacts_root]
    else:
        cleaned: list[str] = []
        for item in review_paths:
            try:
                cleaned.append(
                    normalize_rel_path(item, "review_artifacts_paths")
                )
            except AtestoError:
                continue
        review_paths = cleaned or [artifacts_root]
    excludes = data.get("fingerprint_excludes", data.get("excludes", []))
    if not isinstance(excludes, list) or not all(
        isinstance(item, str) and item.strip() for item in excludes
    ):
        excludes = []
    else:
        cleaned = []
        for item in excludes:
            try:
                cleaned.append(normalize_rel_path(item, "excludes"))
            except AtestoError:
                continue
        excludes = cleaned
    version = data.get("version", 1)
    if type(version) is not int or version not in CONFIG_KEYS_BY_VERSION:
        version = max(CONFIG_KEYS_BY_VERSION)
    return Config(
        version=version,
        artifacts_root=artifacts_root,
        require=require,
        min_reviewers=min_reviewers,
        test_cmd=test_cmd.strip(),
        test_timeout_seconds=test_timeout_seconds,
        review_artifacts_paths=[item.strip() for item in review_paths],
        excludes=[item.strip() for item in excludes],
        path=path,
    )


def _doctor_tool(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict) or "tool" not in data:
        return "ausente"
    if _validate_tool(data, ""):
        return "invalido"
    tool = data["tool"]
    return f"{tool['name'].strip()} {tool['version'].strip()}"


def _doctor_artifacts(
    root: Path, config: Config
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Inspeciona os artefatos sem escrever e sem abortar no primeiro erro."""
    entries: list[dict[str, Any]] = []
    issues: list[str] = []
    required = ", ".join(config.require)
    try:
        base = _safe_artifacts_root(root, config.artifacts_root)
    except AtestoError as exc:
        return entries, [str(exc)], ArtifactStatus.UNREADABLE
    if not base.is_dir() or base.is_symlink():
        if config.require:
            issues.append(
                f"artefatos ausentes em {config.artifacts_root} "
                f"(exigido: {required})"
            )
        return entries, issues, ArtifactStatus.ABSENT
    try:
        slugs = sorted(
            item.name
            for item in base.iterdir()
            if item.is_dir() and not item.is_symlink()
        )
    except OSError as exc:
        message = f"artefatos ilegiveis em {config.artifacts_root}: {exc}"
        return entries, [message], ArtifactStatus.UNREADABLE
    if not slugs:
        if config.require:
            issues.append(
                f"nenhum artefato em {config.artifacts_root} "
                f"(exigido: {required})"
            )
        return entries, issues, ArtifactStatus.EMPTY
    try:
        head = git_head(root)
        current = tree_fingerprint(root, config)
    except AtestoError as exc:
        return (
            entries,
            [f"nao foi possivel avaliar a arvore: {exc}"],
            ArtifactStatus.PRESENT,
        )
    found_proof = False
    review_required = "review" in config.require
    for slug in slugs:
        directory = base / slug
        proof_path = directory / "proof.json"
        entry: dict[str, Any] = {
            "slug": slug,
            "proof": ArtifactStatus.ABSENT,
            "proof_problems": [],
            "proof_tool": "ausente",
            "review": ArtifactStatus.ABSENT,
            "review_problems": [],
            "review_tool": "ausente",
            "review_required": review_required,
        }
        if proof_path.is_symlink():
            found_proof = True
            entry["proof"] = ArtifactStatus.INVALID
            entry["proof_problems"] = ["proof.json nao pode ser symlink"]
            issues.append(f"{slug}: proof.json nao pode ser symlink")
        elif proof_path.is_file():
            found_proof = True
            try:
                proof_data, proof_problems = _validate_proof(
                    directory, root, config, head, current
                )
            except (AtestoError, OSError) as exc:
                proof_data, proof_problems = None, [str(exc)]
            entry["proof"] = (
                ArtifactStatus.FRESH
                if not proof_problems
                else ArtifactStatus.INVALID
            )
            entry["proof_problems"] = list(proof_problems)
            entry["proof_tool"] = _doctor_tool(proof_data)
            for problem in proof_problems:
                issues.append(f"{slug}: {problem}")
        review_path = directory / "review.json"
        if review_path.is_symlink():
            entry["review"] = ArtifactStatus.INVALID
            entry["review_problems"] = ["review.json nao pode ser symlink"]
            issues.append(f"{slug}: review.json nao pode ser symlink")
        elif review_path.is_file():
            try:
                review_data, review_problems = _validate_review(
                    directory, root, config, head, current
                )
            except (AtestoError, OSError) as exc:
                review_data, review_problems = None, [str(exc)]
            entry["review"] = (
                ArtifactStatus.FRESH
                if not review_problems
                else ArtifactStatus.INVALID
            )
            entry["review_problems"] = list(review_problems)
            entry["review_tool"] = _doctor_tool(review_data)
            for problem in review_problems:
                issues.append(f"{slug}: {problem}")
        elif review_required:
            issues.append(f"{slug}: review.json ausente (exigida)")
        entries.append(entry)
    if not found_proof:
        issues.append(
            f"nenhum artefato com proof.json em {config.artifacts_root}"
        )
    return entries, issues, ArtifactStatus.PRESENT


def doctor_report(root: Path) -> dict[str, Any]:
    """Diagnostica config, evidencia e hook sem escrever no repo.

    Le a config crua primeiro: uma config que a validacao estrita rejeita
    ainda pode ser inspecionada. Nunca levanta erro de config; problemas
    entram em `issues` e o relatorio continua.
    """
    issues: list[str] = []

    def add(message: str) -> None:
        if message not in issues:
            issues.append(message)

    report: dict[str, Any] = {
        "ok": False,
        "issues": issues,
        "head": None,
        "config_name": "default",
        "config_version": None,
        "config_valid": True,
        "unknown_keys": [],
        "require": [],
        "min_reviewers": None,
        "test_cmd": "",
        "artifacts_root": DEFAULT_ARTIFACTS_ROOT,
        "review_artifacts_paths": [],
        "excludes": [],
        "test_timeout_seconds": None,
        "hook": hook_status(root),
        "version": VERSION,
        "artifacts": [],
        "artifacts_state": ArtifactStatus.ABSENT,
    }
    try:
        report["head"] = git_head(root)
    except AtestoError as exc:
        add(str(exc))

    path: Path | None = None
    data: dict[str, Any] | None = None
    try:
        path = config_path(root)
    except AtestoError as exc:
        add(str(exc))
        report["config_valid"] = False
    if path is not None:
        report["config_name"] = path.name
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            add(f"{path.name} ilegivel: {exc}")
            report["config_valid"] = False
        else:
            if isinstance(loaded, dict):
                data = loaded
            else:
                add(f"{path.name} precisa ser objeto")
                report["config_valid"] = False
    elif report["config_valid"]:
        report["config_name"] = "default"

    config: Config | None = None
    if data is None:
        config = Config(path=path or (root / "atesto.json"))
        if path is None:
            config.review_artifacts_paths = [config.artifacts_root]
            report["config_version"] = config.version
        else:
            report["config_valid"] = False
    else:
        declared_version = data.get("version", 1)
        report["config_version"] = declared_version
        version_ok = (
            type(declared_version) is int
            and declared_version in CONFIG_KEYS_BY_VERSION
        )
        if not version_ok:
            supported = ", ".join(
                str(item) for item in sorted(CONFIG_KEYS_BY_VERSION)
            )
            add(f"{path.name} version precisa ser {supported}")
            report["config_valid"] = False
        known_keys = (
            CONFIG_KEYS_BY_VERSION[declared_version]
            if version_ok
            else CONFIG_KEYS_V2
        )
        unknown_keys = sorted(set(data) - known_keys)
        report["unknown_keys"] = unknown_keys
        if unknown_keys:
            add(_unknown_keys_message(path.name, declared_version, unknown_keys))
            report["config_valid"] = False
        validation_version = (
            declared_version
            if version_ok
            else max(CONFIG_KEYS_BY_VERSION)
        )
        sanitized = {
            key: value
            for key, value in data.items()
            if key in CONFIG_KEYS_BY_VERSION[validation_version]
        }
        sanitized["version"] = validation_version
        try:
            config = Config.from_data(sanitized, path, root)
        except AtestoError as exc:
            add(str(exc))
            report["config_valid"] = False
    if config is None:
        config = _doctor_fallback_config(data or {}, path)
        report["config_valid"] = False

    report["require"] = list(config.require)
    report["min_reviewers"] = config.min_reviewers
    report["test_cmd"] = config.test_cmd
    report["artifacts_root"] = config.artifacts_root
    report["review_artifacts_paths"] = list(config.review_artifacts_paths)
    report["excludes"] = list(config.excludes)
    report["test_timeout_seconds"] = config.test_timeout_seconds
    if report["config_version"] is None and path is None:
        report["config_version"] = config.version

    artifacts, artifact_issues, artifacts_state = _doctor_artifacts(
        root, config
    )
    report["artifacts"] = artifacts
    report["artifacts_state"] = artifacts_state
    for issue in artifact_issues:
        add(issue)
    report["ok"] = not issues
    return report
