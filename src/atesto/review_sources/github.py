"""GitHub PR review source (optional adapter)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import core
from .base import (
    GIT_OID_PATTERN,
    ProviderError,
    provider_error,
    provider_json,
)

GITHUB_REVIEW_STATES = frozenset(
    {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"}
)
GITHUB_REVIEW_DECISIONS = frozenset(
    {"", "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}
)


@dataclass(frozen=True)
class GithubReview:
    login: str
    state: str
    commit_id: str | None
    submitted_at: str
    review_id: int
    is_bot: bool = False


def _github_metadata(root: Path, pr_number: int) -> tuple[str, str, str]:
    label = f"PR #{pr_number}"
    data = provider_json(
        "gh",
        [
            "pr",
            "view",
            str(pr_number),
            "--json",
            "url,headRefOid,reviewDecision",
        ],
        f"metadados do {label}",
        cwd=root,
    )
    if not isinstance(data, dict):
        raise provider_error(label, "metadados precisam ser objeto JSON")
    head = data.get("headRefOid")
    if not isinstance(head, str) or not GIT_OID_PATTERN.fullmatch(
        head.strip().lower()
    ):
        raise provider_error(label, "headRefOid invalido")
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        raise provider_error(label, "url invalida")
    decision = data.get("reviewDecision")
    if decision is None:
        decision = ""
    if not isinstance(decision, str) or decision not in GITHUB_REVIEW_DECISIONS:
        raise provider_error(label, f"reviewDecision invalido: {decision!r}")
    return head.strip().lower(), url.strip(), decision


def _github_reviews(root: Path, pr_number: int) -> list[GithubReview]:
    label = f"PR #{pr_number}"
    raw = provider_json(
        "gh",
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/reviews",
        ],
        f"reviews do {label}",
        cwd=root,
    )
    if not isinstance(raw, list) or not all(
        isinstance(page, list) for page in raw
    ):
        raise provider_error(
            label, "reviews precisam ser lista de paginas JSON"
        )
    reviews: list[GithubReview] = []
    for page in raw:
        for item in page:
            if not isinstance(item, dict):
                raise provider_error(label, "review precisa ser objeto")
            user = item.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            if not isinstance(login, str) or not login.strip():
                raise provider_error(label, "review sem user.login")
            user_type = user.get("type")
            if user_type is not None and not isinstance(user_type, str):
                raise provider_error(label, "review user.type invalido")
            login = login.strip()
            is_bot = user_type == "Bot" or login.lower().endswith("[bot]")
            state = item.get("state")
            if not isinstance(state, str) or state not in GITHUB_REVIEW_STATES:
                raise provider_error(label, f"review state invalido: {state!r}")
            commit_id = item.get("commit_id")
            if commit_id is not None:
                if not isinstance(commit_id, str) or not (
                    GIT_OID_PATTERN.fullmatch(commit_id.strip().lower())
                ):
                    raise provider_error(label, "review commit_id invalido")
                commit_id = commit_id.strip().lower()
            submitted_at = item.get("submitted_at")
            if submitted_at is None:
                submitted_at = ""
            if not isinstance(submitted_at, str):
                raise provider_error(label, "review submitted_at invalido")
            review_id = item.get("id", 0)
            if type(review_id) is not int:
                raise provider_error(label, "review id invalido")
            reviews.append(
                GithubReview(
                    login=login,
                    state=state,
                    commit_id=commit_id,
                    submitted_at=submitted_at,
                    review_id=review_id,
                    is_bot=is_bot,
                )
            )
    return reviews


def _github_effective_reviews(
    reviews: list[GithubReview],
) -> dict[str, GithubReview]:
    ordered = sorted(
        reviews, key=lambda item: (item.submitted_at, item.review_id)
    )
    effective: dict[str, GithubReview] = {}
    for review in ordered:
        # Comment/pending nao revogam uma aprovacao anterior.
        if review.state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            effective[review.login] = review
    return effective


def write_review_from_github(
    root: Path, slug: str, pr_number: int
) -> Path:
    core.ensure_toplevel(root)
    if type(pr_number) is not int or pr_number < 1:
        raise core.AtestoError("--from-github-pr exige um numero de PR >= 1")
    head = core.git_head(root)
    provider_head, url, decision = _github_metadata(root, pr_number)
    label = f"PR #{pr_number}"
    if provider_head != head:
        raise ProviderError(
            f"head do {label} ({provider_head[:12]}) difere do HEAD local "
            f"({head[:12]}); faca checkout do head do PR"
        )
    if decision == "CHANGES_REQUESTED":
        raise ProviderError(
            f"{label} tem changes requested; resolva antes de gerar review"
        )
    if decision == "REVIEW_REQUIRED":
        raise ProviderError(
            f"{label} ainda requer aprovacao (reviewDecision REVIEW_REQUIRED)"
        )
    effective = _github_effective_reviews(_github_reviews(root, pr_number))
    changes_requested = sorted(
        login
        for login, review in effective.items()
        if review.state == "CHANGES_REQUESTED"
    )
    if changes_requested:
        raise ProviderError(
            f"{label} tem changes requested de: "
            + ", ".join(changes_requested)
        )
    approved = sorted(
        login
        for login, review in effective.items()
        if review.state == "APPROVED" and review.commit_id == provider_head
    )
    # Bots are automation identities: they can approve, but they are not
    # people and must not satisfy min_reviewers. Bot logins are recognised by
    # ``user.type == "Bot"`` or the ``[bot]`` suffix.
    reviewers = [
        login
        for login in approved
        if not effective[login].is_bot
    ]
    if not reviewers:
        if approved:
            raise ProviderError(
                f"{label} so tem aprovacao de bot no head "
                f"{provider_head[:12]}; uma pessoa precisa aprovar"
            )
        raise ProviderError(
            f"{label} nao tem aprovacao no head {provider_head[:12]}; "
            "peca aprovacao no commit atual"
        )
    return core.write_review_artifact(
        root,
        slug,
        intent=f"review do {label}",
        verdict="APPROVED",
        reviewers=reviewers,
        source={"kind": "github", "ref": url},
    )
