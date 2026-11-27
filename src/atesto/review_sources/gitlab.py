"""GitLab MR review source (optional adapter)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import core
from .base import (
    GIT_OID_PATTERN,
    ProviderError,
    provider_error,
    provider_json,
)

GITLAB_TOKEN_HINT = (
    "GitLab exige GITLAB_TOKEN com escopo read_api (ou api) para ler o MR "
    "e as aprovacoes; CI_JOB_TOKEN nao le /approvals; rode `glab auth login` "
    "para autenticar"
)


def _gitlab_project() -> str:
    for name in ("CI_MERGE_REQUEST_PROJECT_ID", "CI_PROJECT_ID"):
        value = os.environ.get(name, "").strip()
        if value:
            if not value.isdigit():
                raise ProviderError(
                    f"{name} invalido: {value!r}; esperava o id numerico "
                    "do projeto GitLab"
                )
            return value
    return ":fullpath"


def _gitlab_endpoint(iid: int, suffix: str = "") -> str:
    return f"projects/{_gitlab_project()}/merge_requests/{iid}{suffix}"


def _gitlab_api(root: Path, iid: int, suffix: str, label: str) -> Any:
    try:
        return provider_json(
            "glab",
            ["api", _gitlab_endpoint(iid, suffix)],
            label,
            cwd=root,
        )
    except ProviderError as exc:
        lowered = str(exc).lower()
        if "401" in lowered or "unauthorized" in lowered:
            raise ProviderError(
                f"{label}: glab nao autenticado; {GITLAB_TOKEN_HINT}"
            ) from exc
        if "403" in lowered or "forbidden" in lowered:
            raise ProviderError(
                f"{label}: acesso negado (403); {GITLAB_TOKEN_HINT}"
            ) from exc
        raise


def _gitlab_mr(root: Path, iid: int) -> tuple[str, str, str, bool]:
    label = f"MR !{iid}"
    data = _gitlab_api(
        root, iid, "", f"metadados do {label}"
    )
    if not isinstance(data, dict):
        raise provider_error(label, "metadados precisam ser objeto JSON")
    sha = data.get("sha")
    if not isinstance(sha, str) or not GIT_OID_PATTERN.fullmatch(
        sha.strip().lower()
    ):
        raise provider_error(label, "sha invalido")
    status = data.get("detailed_merge_status")
    if not isinstance(status, str) or not status.strip():
        raise provider_error(label, "detailed_merge_status invalido")
    discussions_resolved = data.get("blocking_discussions_resolved")
    if type(discussions_resolved) is not bool:
        raise provider_error(
            label, "blocking_discussions_resolved invalido"
        )
    url = data.get("web_url")
    if not isinstance(url, str) or not url.strip():
        raise provider_error(label, "web_url invalido")
    return (
        sha.strip().lower(),
        status.strip(),
        url.strip(),
        discussions_resolved,
    )


def _gitlab_approvers(root: Path, iid: int) -> list[str]:
    label = f"MR !{iid}"
    data = _gitlab_api(
        root, iid, "/approvals", f"aprovacoes do {label}"
    )
    if not isinstance(data, dict):
        raise provider_error(label, "aprovacoes precisam ser objeto JSON")
    approved = data.get("approved")
    if type(approved) is not bool:
        raise provider_error(label, "approved precisa ser booleano")
    approved_by = data.get("approved_by")
    if not isinstance(approved_by, list):
        raise provider_error(label, "approved_by precisa ser lista")
    logins: list[str] = []
    for entry in approved_by:
        if not isinstance(entry, dict):
            raise provider_error(label, "approved_by precisa conter objetos")
        user = entry.get("user")
        username = user.get("username") if isinstance(user, dict) else None
        if not isinstance(username, str) or not username.strip():
            raise provider_error(label, "approved_by sem user.username")
        logins.append(username.strip())
    if not approved:
        raise ProviderError(
            f"{label} nao esta aprovado (approved=false)"
        )
    if not logins:
        raise ProviderError(
            f"{label} nao tem aprovacoes (approved_by vazio)"
        )
    return sorted(set(logins))


def write_review_from_gitlab(
    root: Path, slug: str, iid: int
) -> Path:
    core.ensure_toplevel(root)
    if type(iid) is not int or iid < 1:
        raise core.AtestoError("--from-gitlab-mr exige um IID de MR >= 1")
    head = core.git_head(root)
    provider_head, status, url, discussions_resolved = _gitlab_mr(root, iid)
    label = f"MR !{iid}"
    if provider_head != head:
        raise ProviderError(
            f"sha do {label} ({provider_head[:12]}) difere do HEAD local "
            f"({head[:12]}); faca checkout do head do MR"
        )
    if status == "requested_changes":
        raise ProviderError(
            f"{label} tem requested changes; resolva antes de gerar review"
        )
    if status == "not_approved":
        raise ProviderError(
            f"{label} ainda nao foi aprovado "
            "(detailed_merge_status not_approved)"
        )
    if not discussions_resolved or status == "discussions_not_resolved":
        raise ProviderError(
            f"{label} tem discussoes bloqueantes nao resolvidas"
        )
    # Limitacao do provedor: approvals do GitLab nao carregam o SHA de cada
    # aprovacao. O check de `sha` do MR (acima) e o unico guard disponivel;
    # se o projeto nao resetar approvals no push (reset_approvals_on_push),
    # uma aprovacao antiga pode sobreviver. Ver README, "GitLab MR approvals".
    reviewers = _gitlab_approvers(root, iid)
    return core.write_review_artifact(
        root,
        slug,
        intent=f"review do {label}",
        verdict="APPROVED",
        reviewers=reviewers,
        source={"kind": "gitlab", "ref": url},
    )
