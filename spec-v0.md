# Atesto v0 — spec

## Problem

AI coding agents claim work is done without durable proof. Existing CI checks
answer "did the pipeline pass?", not "is this claim bound to the code being
merged?". Atesto makes the claim falsifiable.

## Scope

- `proof.json`: command, exit, output hash, head, tree hash, dirty state.
- `review.json` (optional): head, tree hash, verdict, reviewers, findings.
- `atesto check`: deterministic gate for local use or CI.
- Local pre-push hook.
- Optional, additive CI workflow.
- Python 3.10+, stdlib only.

## Non-goals

- Agent runtime, routing, stages, queue, dashboard.
- Statechart, blast radius, domain policy.
- Multi-language SDK.
- GitHub App, branch protection, signed attestations.
- Judging code correctness or review quality.

## Decisions

- License: Apache-2.0.
- Artifacts default: `docs/atesto/<slug>/`.
- `proof` required; `review` opt-in via `require`.
- `test_cmd` optional; CI should run it for stronger enforcement.
- CI template is a new file; never edit an existing workflow.
- `pstack.json` fallback for migration.

## Acceptance criteria

1. `atesto init` creates a valid config without touching CI.
2. `atesto proof` writes a valid `proof.json` and output file.
3. `atesto check` passes on a clean repo with fresh proof.
4. Changing code after proof makes check fail with "prova stale".
5. Missing required review fails; a review bound to another head/tree fails.
6. A failed proof command fails the gate.
7. `atesto check --ci` without config in base fails closed.
8. Artifacts are excluded from the tree hash: committing evidence does not
   invalidate proof.
9. `atesto hooks install` writes a non-destructive pre-push hook.
10. `python -m unittest discover -s tests` passes.

## OSS readiness (after v0)

- 3 real repos and 20+ PRs measured.
- False positives/negatives counted.
- Security review (path traversal, symlinks, test command, large outputs).
- Docs, examples, release workflow, signed tags, SHA-256.
- License/NOTICE, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT.
