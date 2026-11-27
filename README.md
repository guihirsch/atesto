# Atesto

**Evidence gate for AI-written code.** Atesto binds a proof and an optional
review to the exact code tree, then refuses to call work done when the evidence
is missing or stale.

Atesto is deliberately small: proof + review + one deterministic checker. It is
not an agent runtime, a code review bot, a workflow engine, or a test runner.

- `spec-v0.md` — acceptance criteria for v0.

## Quick start

The full path from an empty repository to the first green check. It uses a
manual review so no provider CLI is needed; for provider or custom approvals
see [Review sources](#review-sources).

```bash
# 1. Fresh repository
mkdir atesto-demo && cd atesto-demo
git init
git config user.email you@example.com
git config user.name "Your Name"
python -m pip install "atesto==0.3.0"

# 2. Enable the gate and commit the config with the code
atesto init --require proof,review
printf 'print("ok")\n' > app.py
git add atesto.json app.py
git commit -m "chore: initial commit"

# 3. Prove the code head, then commit the evidence
atesto proof demo -- python3 app.py
git add docs/atesto/demo
git commit -m "docs(atesto): proof"

# 4. Review the same tree, then commit the evidence
atesto review demo --head HEAD --verdict APPROVED --reviewer alice
git add docs/atesto/demo
git commit -m "docs(atesto): review"

# 5. Check the head
atesto check
# atesto: ok
```

`atesto check` reads the committed artifacts, recomputes the tree and prints
`atesto: ok`. To take the review from GitHub, GitLab or a custom approval
system instead of passing the flags by hand, use `--from-github-pr`,
`--from-gitlab-mr` or `--from-json` in step 4 as described in
[Review sources](#review-sources). Commit the generated `review.json` before
running `check`; CI cannot create evidence for a head that was never
approved.

## What it checks

- `proof.json` exists in `docs/atesto/<slug>/`;
- the proof command exited 0;
- the proof output file matches its recorded size and SHA-256;
- `proof.json.head` is an ancestor of `HEAD`;
- `proof.json.tree_sha256` matches the current code tree (artifacts excluded);
- if `review` is required, `review.json` is APPROVED, has enough reviewers,
  is bound to the same tree, and nothing outside the artifacts root changed
  after the reviewed head;
- if evidence is valid and `test_cmd` is configured, it runs in the repo,
  must exit 0, and must not change the code tree; with
  `test_timeout_seconds`, exceeding the limit kills the process group and
  fails closed.

Artifacts are excluded from the tree hash, so committing the evidence does not
invalidate the proof.

## Threat model

Atesto is an evidence checker, not an enforcement point. It records and
validates claims about a tree; the CI host decides whether a merge is allowed.

**What `proof` proves:** a command ran, exited 0, and its captured output is
unchanged since the artifact was written for this tree. **What it does not
prove:** that the command is meaningful, that the tests would catch a bug,
that the output was not fabricated, or that the environment is reproducible.
A local `proof.json` is self-reported. Running `test_cmd` in CI is the
stronger signal; protect that config as described below.

**What `review` proves:** `review.json` is APPROVED, has enough distinct
reviewers, and is bound to this head and tree; `source` records where the
review came from. **What it does not prove:** that a human read the diff, that
the reviewer identity is trustworthy, or that an approval was not copied by
hand. Manual and JSON reviews are self-reported and unsigned. The GitHub and
GitLab adapters read the provider's current-head approval through `gh`/`glab`,
but Atesto never handles provider tokens; the provider API and the local CLI
decide authenticity.

**Host enforcement:** a nonzero exit from `atesto check` does not block a
merge. Make the job a required check:

- GitHub: branch protection/rulesets or the merge queue. An admin bypass of a
  ruleset, or a direct push to an unprotected branch, is outside Atesto's
  control.
- GitLab: protected branches and approval rules. An admin bypass or a project
  configured to merge without approval is outside Atesto's control. Approvals
  are tied to the MR's current SHA: a new push resets them (depending on the
  project settings), and the adapter fails if the MR SHA is not the local
  HEAD. `/approvals` requires a `GITLAB_TOKEN` with `read_api`/`api` scope;
  `CI_JOB_TOKEN` cannot read it. Atesto never stores the token.
- Jenkins, CircleCI and Buildkite: the host's required-step, branch-protection
  or pipeline-permission settings. The snippets only run the gate.

Anyone who can edit the workflow, the container image tag or `atesto.json` can
replace the gate; keep them in CODEOWNERS and pin images by version, preferably
by digest. On self-hosted runners, a preinstalled `atesto` earlier on `PATH`
can shadow the hash-pinned install; the `atesto --version` smoke step only
catches a different version, not a malicious same-version binary. Do not run
the gate on runners that untrusted pull requests can modify. Signed
attestations are not part of 0.3.0.

## Commands

```bash
atesto --version
atesto init [--artifacts-root docs/atesto] [--require proof,review] [--test-cmd CMD]
atesto proof [--root .] [--no-output] <slug> -- <command>
atesto review <slug> [--head HEAD] [--verdict APPROVED] [--reviewer NAME]...
atesto review <slug> [--from-json PATH | --from-github-pr N | --from-gitlab-mr IID]
atesto check [--root .] [--base origin/main] [--slug SLUG] [--ci] [--json]
atesto doctor [--root .]
atesto hooks install|uninstall|status [--root .] [--force]
atesto fingerprint [--root .] [--ref REF]
```

`--ci` fails closed when `atesto.json` is not present in the base ref. A PR
cannot initialize its own gate.

## Review sources

Manual review is the default:

```bash
atesto review demo --head HEAD --verdict APPROVED --reviewer alice
```

For automation, `review` accepts exactly one source. A source flag cannot be
combined with another source or with the manual `--head`, `--verdict`,
`--reviewer` and `--intent` flags.

- `--from-json PATH` reads the strict review JSON contract v1.
- `--from-github-pr N` reads a GitHub PR through `gh`.
- `--from-gitlab-mr IID` reads a GitLab MR through `glab`.

The provider readers live in the optional `atesto.review_sources` package and
are imported only when their flag is used. The core keeps manual `review`,
`--from-json` and `check`; it does not encode provider merge policy. CI and
domain adapters live under `integrations/` and stay optional as well.

### GitHub PR approvals

`gh` must be installed and authenticated (`gh auth login` or `GH_TOKEN`);
Atesto never reads or stores provider tokens. The adapter reads the PR head and
all reviews with:

```bash
gh api --paginate --slurp repos/{owner}/{repo}/pulls/N/reviews
```

The adapter then:

- counts only `APPROVED` reviews whose `commit_id` is the PR head;
- counts people, not automation: a review whose `user.type` is `Bot` or whose
  login ends in `[bot]` never contributes to `min_reviewers` (a bot-only
  approval set fails closed);
- keeps the latest review per login (a later comment does not revoke an
  approval; a dismissal does), dedupes logins and sorts them;
- fails closed on `CHANGES_REQUESTED`, `REVIEW_REQUIRED`, no current-head
  approval, a local `HEAD` that differs from the PR head, missing or
  unauthenticated `gh`, malformed output, and provider HTTP errors; no
  `review.json` is written on failure;
- writes `source: {"kind": "github", "ref": "<PR URL>"}` and leaves
  `min_reviewers` enforcement to `check`.

An empty `reviewDecision` (`""`) means GitHub has no required-review decision
for the PR (usually no branch-protection review rule). This is a deliberate
clarification of ticket #14: the adapter does not treat the absence of a
decision as approval, and still requires at least one current-head approval
(`APPROVED`) from a person. It fails closed on `CHANGES_REQUESTED` and
`REVIEW_REQUIRED`.

The adapter uses whatever repository `gh` resolves (the current clone, or
`GH_REPO`). Inside a fork `gh` resolves the fork, so to read approvals for an
upstream PR run from an upstream clone or set
`GH_REPO=<upstream-owner>/<repo>`; Atesto does not guess the upstream
repository. Check out the PR head locally (for example `gh pr checkout N`)
before running the command.

### GitLab MR approvals

`glab` must be installed and authenticated (`glab auth login` or
`GITLAB_TOKEN`). Atesto never reads or stores provider tokens. The adapter
reads the MR and its approvals with:

```bash
glab api "projects/<project>/merge_requests/IID"
glab api "projects/<project>/merge_requests/IID/approvals"
```

When the job exports `CI_MERGE_REQUEST_PROJECT_ID` (the MR target, including
fork pipelines) or `CI_PROJECT_ID`, the adapter uses that numeric project id.
Outside CI it uses the `:fullpath` placeholder, so `glab` resolves the project
from the current clone. For a fork MR, run from a clone of the target project
or export the target project id.

The adapter then:

- requires the MR `sha` to be exactly the local `HEAD`;
- fails on `detailed_merge_status` `requested_changes` or `not_approved`, and
  on unresolved blocking discussions (`blocking_discussions_resolved: false`);
- requires `approved: true` and at least one login in `approved_by`;
- dedupes and sorts approver usernames and leaves `min_reviewers` enforcement
  to `check`;
- fails closed on missing or unauthenticated `glab`, 401/403 responses,
  malformed output and provider HTTP errors; no `review.json` is written on
  failure;
- writes `source: {"kind": "gitlab", "ref": "<MR URL>"}`.

**Stale approval limitation:** GitLab approvals carry no per-approval SHA, so
Atesto cannot prove that an approval was issued for the current push. The MR
`sha` check is the only head guard the API exposes. When the project enables
`reset_approvals_on_push`, GitLab resets approvals on a push; when it is
disabled, an old approval can survive a new commit. Atesto does not invent
per-approval SHA data: enable `reset_approvals_on_push` (and re-approve after a
push) when approvals must bind to the exact head.

GitLab requires authentication to read `/approvals`; `CI_JOB_TOKEN` is not
allowed to call that endpoint, so provide `GITLAB_TOKEN` with `read_api` (or
`api`) scope. Check out the MR head locally before running the command.

## Strict JSON contract v1

`--from-json PATH` is the escape hatch for Jira, Sonar, or any custom approval
system. The file is strict and versioned: every field below is required and
non-empty, and unknown fields fail closed.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | integer | Contract version; must be `1`. |
| `source` | object | `{"kind": ..., "ref": ...}` provenance. |
| `source.kind` / `kind` | string | `github`, `gitlab` or `json`. |
| `source.ref` / `ref` | string | Provider URL, change id or ticket id. |
| `head` | string | Commit id the approval applies to (guard only). |
| `verdict` | string | `APPROVED` or `CHANGES_REQUESTED`. |
| `reviewers` | array of strings | At least one distinct reviewer login. |
| `intent` | string | Why the change was approved. |

```json
{
  "schema": 1,
  "source": {"kind": "json", "ref": "jira-42"},
  "head": "0123456789abcdef0123456789abcdef01234567",
  "verdict": "APPROVED",
  "reviewers": ["alice", "bob"],
  "intent": "aprovar a mudanca 123"
}
```

**Head guard:** Atesto resolves `head` itself and computes the tree from the
repo. The value must resolve to `HEAD` or to an ancestor whose evidence tree is
the same tree (an evidence-only commit on top is fine; a code change is not).
The artifact always records the resolved current `HEAD` and the computed
`tree_sha256`, never the values from the file.

**Failure modes:** malformed JSON, a duplicated key, an unknown top-level key,
`schema` other than 1, a missing or blank `source.kind`/`source.ref`, an
unknown source kind, a blank `head`, an unresolvable head, a non-ancestor head,
an ancestor with a changed tree, a missing or invalid `verdict`, an
empty/blank reviewer list, and a blank `intent` all fail closed. No
`review.json` is written on failure.

## Config

`atesto.json` at the repo root. **Example** v2 config (not the exact file
`atesto init` writes: the optional `test_timeout_seconds` and
`fingerprint_excludes` below are shown here, and `init` leaves them out):

```json
{
  "version": 2,
  "artifacts_root": "docs/atesto",
  "require": ["proof"],
  "min_reviewers": 1,
  "test_cmd": "",
  "test_timeout_seconds": 30,
  "review_artifacts_paths": ["docs/atesto"],
  "fingerprint_excludes": []
}
```

`atesto init --require proof` writes the smaller config below; add optional
keys yourself when you need them:

```json
{
  "version": 2,
  "artifacts_root": "docs/atesto",
  "require": ["proof"],
  "min_reviewers": 1,
  "test_cmd": "",
  "review_artifacts_paths": ["docs/atesto"]
}
```

Two config versions are supported:

- **v1** is the original key set. Omitting `version` means v1.
- **v2** requires `"version": 2` and adds `test_timeout_seconds`; `atesto init`
  writes v2 from now on.

Both versions reject **unknown top-level keys** in every mode (`check`,
`check --ci`, `proof`, `review`, `doctor`), naming all unknown keys at once.
There is no warning-only mode: a config that looks stronger than the tool can
enforce fails instead of silently under-enforcing. `check --ci` also rejects
unknown keys and a config `version` lower than the base. The error points to
[Migração para 0.2.0](#migração-para-020) and the CHANGELOG.

The config is validated by
[`atesto.schema.json`](src/atesto/schemas/atesto.schema.json) (JSON Schema
draft 2020-12; `additionalProperties: false` in both variants). Point your
editor at its `$id` or the local path.

### Migração para 0.2.0

Unknown top-level keys now fail closed. This is breaking by design: a config
that appears to require more evidence must not run with only the subset the
tool understands. Before upgrading from 0.1.x:

1. Remove every key the core does not recognize from `atesto.json` (or
   `pstack.json`).
2. Move adapter/domain policy — for example `statechart`, `blast_radius`,
   decisions or per-path requirements — to the adapter's own config, next to
   the adapter that consumes it.
3. Run `atesto doctor` to list unknown keys and effective config, then
   `atesto check` to confirm the gate still passes.

`version` is not a free-form migration switch: v1 keeps the original key set,
v2 adds `test_timeout_seconds` and requires `"version": 2`. A config that
omits `version` is v1.

### Test timeout

`test_timeout_seconds` is a positive integer (minimum 1, a one-second floor)
and exists only in v2; under v1 it is an unknown key and fails. Absent means
no timeout, preserving the old behavior. On expiry Atesto sends SIGTERM to the
test command's process group, waits a short grace period, then sends SIGKILL,
so descendants do not survive the gate. The failure names the limit and the
command. `check --json` exposes `test_timeout_seconds` and `timed_out`. On
platforms without POSIX process groups only the direct child is terminated;
the timeout is best-effort there.

Under `--ci` the base timeout is a floor: the head cannot remove it or raise
it. The head may lower it, or add one when the base has none.

### Tool identity

`proof.json` and `review.json` record the producing tool:

```json
"tool": { "name": "atesto", "version": "0.3.0" }
```

The shape is validated when present; artifacts written by older versions
without `tool` remain valid. `check --json` exposes the proof's `tool` and
`doctor` reports it, so evidence provenance can be audited.

### Doctor

`atesto doctor` reads the raw config without hard-failing and reports unknown
keys, effective keys (`require`, `min_reviewers`, `test_cmd`,
`review_artifacts_paths`, `excludes`, `artifacts_root`, timeout), version,
artifact freshness, hook status and tool version. It exits nonzero when it
finds an issue, including missing required evidence when an artifacts root is
absent or empty, so it shows why a gate would fail before `check` does. A
missing hook is informational, not an issue.

### Domain policy belongs to adapters

The core recognizes only evidence and gate keys. Statechart, blast radius,
decisions, per-path requirements and similar domain policy live in an
adapter-owned config next to the adapter that consumes them, never in
`atesto.json`. Adapters compose core facts from `atesto check --json`
(validated artifact directory, requirements, tree hash, tool identity) instead
of teaching the core new policy. Unknown-key rejection is what keeps this
boundary honest.

`review` is opt-in: add it to `require` for production or high-risk changes.
If a `review.json` exists but is not required, it is still validated
(fail-closed); a stale review blocks.

`atesto proof` commits stdout/stderr into `docs/atesto/<slug>/`. Do not print
secrets; use `--no-output` to omit the output (the command line is still
recorded, so keep secrets out of arguments too).
`pstack.json` is read as a fallback for migration; `atesto.json` wins.

## Local hook and CI

- `atesto hooks install` installs a **pre-push** hook. It is fast feedback, not
  a security boundary; `--no-verify` can bypass it.
- `integrations/github-actions/atesto-gate.yml` is an **optional new workflow**
  outside the Python package. It never edits an existing CI. If you are not
  responsible for the repo's CI, use the local hook and attach the evidence to
  the PR instead. It pins `atesto==0.3.0` with a sha256, installs with
  `--require-hashes --no-deps` on Python 3.12, and runs `atesto --version`
  before the gate. It becomes runnable once 0.3.0 is published on PyPI.
- `integrations/hooks/pre-push` is the reference hook; `atesto hooks install`
  writes the same behavior inline. Both are optional adapters, not core.

## CI snippets and host matrix

`integrations/ci/` contains additive snippets for GitLab, GitHub, Jenkins,
CircleCI and Buildkite. Each one runs the published container image
(`ghcr.io/guihirsch/atesto:0.3.0`), fetches full history so `check --ci` can
read the base config, sets the base ref and runs only the gate. The version in
the snippets must match `pyproject.toml`; `CISnippetTest` fails on drift.

The published 0.3.0 image is also available by digest:
`ghcr.io/guihirsch/atesto@sha256:bdd41341c8a1dc9a7cfb535f4288aa889ac1a754877dc67bcb2673a9f59daa19`.
Prefer the digest in production; the version tag is the minimum.

### Generate the review before you commit

`check --ci` requires the evidence directory to be part of the PR diff. The
snippets therefore **do not generate, commit or push evidence**: a CI-created
artifact is either absent from the diff (the gate fails) or belongs to a new
commit the provider approval was not issued for. This is a deliberate
deviation from ticket #16's "review + check" wording: the snippets run only
`check --ci`, and review generation is a documented pre-commit step because
the evidence must be in the PR/MR diff. Generate the review on the branch as a
pre-commit step for the author or agent, then commit it with the code:

```bash
# GitHub PR approvals (needs gh, checked out on the PR head)
atesto review demo --from-github-pr 123
# GitLab MR approvals (needs glab, checked out on the MR head)
atesto review demo --from-gitlab-mr 42
# Custom approvals (Jira/Sonar/...): strict JSON contract v1
atesto review demo --from-json approvals.json
# Manual review
atesto review demo --head HEAD --verdict APPROVED --reviewer alice

git add docs/atesto/demo/review.json
git commit -m "docs(atesto): add review evidence"
```

The same flow applies to `proof.json`: run `atesto proof`, then commit
`docs/atesto/<slug>/`. Provider tokens stay in `gh`/`glab`; Atesto never reads
them, and the container image does not bundle either CLI. Run provider-backed
review generation on a machine that has the provider CLI, not in the gate job.

| Host | Snippet | Full history | Base ref | Gate command |
| --- | --- | --- | --- | --- |
| GitLab | `integrations/ci/gitlab-ci.yml` | `GIT_DEPTH: "0"` | `origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME` | `atesto check --ci --base "origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"` |
| GitHub | `integrations/ci/github-actions.yml` | `fetch-depth: 0` | `origin/${{ github.base_ref }}` | `docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/repo" -w /repo ghcr.io/guihirsch/atesto:0.3.0 check --ci --base "$BASE_REF"` |
| Jenkins | `integrations/ci/jenkins.Jenkinsfile` | `depth: 0, shallow: false` | `origin/$CHANGE_TARGET` | `docker run --rm --user "$(id -u):$(id -g)" -v "$WORKSPACE:/repo" -w /repo ghcr.io/guihirsch/atesto:0.3.0 check --ci --base "$BASE_REF"` |
| CircleCI | `integrations/ci/circleci.yml` | `git fetch --unshallow` | `ATESTO_BASE_REF` (default `origin/main`) | `docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/repo" -w /repo ghcr.io/guihirsch/atesto:0.3.0 check --ci --base "$BASE_REF"` |
| Buildkite | `integrations/ci/buildkite.yml` | `git fetch --unshallow` | `BUILDKITE_PULL_REQUEST_BASE_BRANCH` (default `main`) | `docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/repo" -w /repo ghcr.io/guihirsch/atesto:0.3.0 check --ci --base "$BASE_REF"` |

Every snippet is additive: copy it as a new job, workflow or step and leave
the rest of the pipeline alone. If `docs/atesto/` has more than one slug, add
`--slug <slug>` to the gate command (or set the snippet's slug variable where
it has one).

**Enforcement stays with the host.** Atesto exits nonzero on missing or stale
evidence, but it cannot block a merge by itself. Make the Atesto job a required
check in GitHub branch protection/rulesets (or the merge queue), in GitLab
protected branches/approval rules, or in the equivalent Jenkins, CircleCI and
Buildkite configuration. A PR that can edit the workflow can replace the gate,
so keep the snippet and `atesto.json` in CODEOWNERS.

## Limits

- `proof.json` is self-reported. For stronger enforcement, run `test_cmd` in CI
  (recommended) or adopt signed attestations later.
- A textual review is not cryptographically signed. Atesto checks that it is
  bound to the head, not that the reviewer is trustworthy.
- Atesto does not prove the code is correct; it proves the evidence claims are
  consistent and current.
- A dirty submodule is bound by its HEAD, status and tracked diff. Content of
  untracked files inside the submodule is still not hashed.
- The CI template pins the published 0.3.0 wheel by sha256. Keep the workflow
  and `atesto.json` in CODEOWNERS; a PR that can edit the workflow can replace
  the gate.
- `--ci` does not allow a PR to change `test_cmd`, `artifacts_root`,
  `min_reviewers` downward, remove or raise a base timeout, downgrade the
  config version, or add excludes/review paths. Fixing those requires a
  base/admin change, not a feature PR.

## Release

0.3.0 ships a reproducible wheel to PyPI and a matching image to GHCR:

- `[build-system]` stays permissive (`setuptools>=69`) so source builds are
  not tied to the release backend;
- `.github/workflows/release.yml` pins the build environment
  (`build==1.6.1`, `setuptools==84.2.1`, `wheel==0.48.0`), runs
  `python -m build --no-isolation`, and sets `SOURCE_DATE_EPOCH=1795651200`
  (2026-11-26T00:00:00Z);
- a clean checkout built with `umask 022` reproduces
  `atesto-0.3.0-py3-none-any.whl` byte for byte. File permissions are stored
  in the wheel zip, so a group-writable checkout (for example `umask 002`)
  changes the hash;
- the gate template is the single source of truth for the release pin:
  `atesto==0.3.0` with the published wheel sha256. The README does not repeat
  the hash, so it cannot drift; `Dockerfile` and `container-publish.yml` have
  no hash defaults and require the value from their caller;
- before the PyPI publish, `release.yml` builds the wheel with `umask 022` and
  compares its sha256 to the template pin. A mismatch stops the release before
  anything is uploaded;
- after the PyPI publish, the `container` job in `release.yml` downloads the
  published `atesto==0.3.0`, checks the hash again, builds and **loads** the
  image, smokes it, verifies the version and wheel-hash labels, and only then
  pushes `ghcr.io/guihirsch/atesto:0.3.0` and `:latest`.
  `.github/workflows/container-publish.yml` remains available for manual
  dispatch; it requires the version and wheel hash (no defaults), downloads the
  PyPI wheel, verifies the supplied hash, and follows the same
  build/load/smoke/label/push order.
- the PR container workflow builds a wheel from the current source with the
  same pinned environment and `umask 022`, then builds `Dockerfile.ci` and
  smokes that local-wheel image. It is green before 0.3.0 exists on PyPI; the
  release `Dockerfile` keeps installing the published wheel with the hash.

PyPI and GHCR are updated after the release commit is merged (post-merge).
The template hash is verified against the reproducible local build before the
publish and against the published wheel after it; it is not assumed.

For production, pin the image by digest, not only by version tag: the version
tag is the minimum. Record the digest from the release image and use
`ghcr.io/guihirsch/atesto@sha256:<digest>` in your job:

```bash
docker buildx imagetools inspect ghcr.io/guihirsch/atesto:0.3.0
# Copy the manifest digest, e.g.
# ghcr.io/guihirsch/atesto@sha256:0123... in the snippet.
```

Verify a release directly from the published artifacts instead of trusting a
local rebuild:

```bash
python -m pip download --no-deps --only-binary=:all: --dest dist \
  "atesto==0.3.0"
sha256sum dist/atesto-0.3.0-py3-none-any.whl
grep -o 'sha256:[0-9a-f]\{64\}' integrations/github-actions/atesto-gate.yml
# The published wheel hash and the template pin must match.

docker pull ghcr.io/guihirsch/atesto:0.3.0
docker run --rm ghcr.io/guihirsch/atesto:0.3.0 --version
docker image inspect \
  --format '{{ index .Config.Labels "dev.atesto.wheel-sha256" }}' \
  ghcr.io/guihirsch/atesto:0.3.0
# The image label must match the wheel hash; record the digest printed by pull.
```

Rebuild the wheel from a clean checkout and compare before changing the pin:

```bash
umask 022
git clone --no-hardlinks <repo> atesto-rebuild
cd atesto-rebuild
python -m pip install "build==1.6.1" "setuptools==84.2.1" "wheel==0.48.0"
SOURCE_DATE_EPOCH=1795651200 python -m build --no-isolation
sha256sum dist/atesto-0.3.0-py3-none-any.whl
```

Bump `pyproject.toml`/`VERSION`, the wheel pin, the image build arguments and
every snippet together: `ReleaseMetadataTest`, `ContainerMetadataTest` and
`CISnippetTest` fail on version drift. If the pinned build environment or
timestamp changes, update the template hash in the same change.

## Development

```bash
python -m unittest discover -s tests -v
```

Apache-2.0. See `NOTICE` for attribution to the pstack line of work that
inspired this project.
