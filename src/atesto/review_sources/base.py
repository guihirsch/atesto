"""Shared process boundary for optional review-source adapters.

Adapters shell out to the provider CLI (``gh``/``glab``), translate process
failures into actionable errors and return only the derived facts the core
needs. The core never imports this module.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .. import core

# One object-id pattern for both providers: GitHub and GitLab are passing
# SHA-1 today and SHA-256 is available on both, so accept either width.
GIT_OID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

PROVIDER_AUTH_HINTS = {
    "gh": "rode `gh auth login` ou defina GH_TOKEN",
    "glab": "rode `glab auth login` ou defina GITLAB_TOKEN",
}
PROVIDER_AUTH_MARKERS = (
    "not authenticated",
    "authentication required",
    "bad credentials",
    "to get started with github cli",
    "gh auth login",
    "glab auth login",
    "gh_token",
    "github_token",
    "gitlab_token",
    "oauth_token",
)


class ProviderError(core.AtestoError):
    """Falha ao ler um provedor externo (gh/glab)."""


def provider_error(label: str, message: str) -> ProviderError:
    return ProviderError(f"{label}: {message}")


def provider_not_found(command: str) -> ProviderError:
    hint = PROVIDER_AUTH_HINTS.get(command, f"rode `{command} auth login`")
    return ProviderError(
        f"{command} nao encontrado no PATH; instale o CLI e {hint}"
    )


def run_provider_command(
    command: str, args: list[str], *, cwd: Path | None = None
) -> str:
    """Executa um CLI de provedor e devolve stdout.

    O exit code e checado antes de qualquer parse: `gh api` e `glab api`
    escrevem um corpo JSON de erro em stdout mesmo quando falham.
    """
    try:
        result = subprocess.run(
            [command, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
        )
    except FileNotFoundError as exc:
        raise provider_not_found(command) from exc
    except OSError as exc:
        raise ProviderError(f"{command} nao iniciou: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        message = stderr or stdout
        lowered = f"{stderr}\n{stdout}".lower()
        if result.returncode == 4 or any(
            marker in lowered for marker in PROVIDER_AUTH_MARKERS
        ):
            hint = PROVIDER_AUTH_HINTS.get(
                command, f"rode `{command} auth login`"
            )
            raise ProviderError(f"{command} nao autenticado; {hint}")
        raise ProviderError(
            f"{command} falhou (exit {result.returncode}): "
            + (message or "sem mensagem")
        )
    return result.stdout


def provider_json(
    command: str,
    args: list[str],
    label: str,
    *,
    cwd: Path | None = None,
) -> Any:
    output = run_provider_command(command, args, cwd=cwd)
    try:
        return json.loads(output)
    except ValueError as exc:
        raise ProviderError(
            f"{command} retornou {label} malformado: esperava JSON"
        ) from exc
