# Atesto

**Evidence gate for AI-written code.** Atesto binds a proof and an optional
review to the exact code tree, then refuses to call work done when the evidence
is missing or stale.

Atesto is deliberately small: proof + review + one deterministic checker. It is
not an agent runtime, a code review bot, a workflow engine, or a test runner.

- `spec-v0.md` — acceptance criteria for v0.

## Quick start

```bash
python -m pip install .
atesto init --require proof
atesto proof demo -- python3 -c "print('ok')"
atesto check
```

With review required:

```bash
atesto init --require proof,review
atesto proof demo -- pytest -q
atesto review demo --head HEAD --verdict APPROVED --reviewer "reviewer"
atesto check
```

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

## Commands

```bash
atesto --version
atesto init [--artifacts-root docs/atesto] [--require proof,review] [--test-cmd CMD]
atesto proof [--root .] [--no-output] <slug> -- <command>
atesto review <slug> [--head HEAD] [--verdict APPROVED] [--reviewer NAME]...
atesto check [--root .] [--base origin/main] [--slug SLUG] [--ci] [--json]
atesto doctor [--root .]
atesto hooks install|uninstall|status [--root .] [--force]
atesto fingerprint [--root .] [--ref REF]
```

`--ci` fails closed when `atesto.json` is not present in the base ref. A PR
cannot initialize its own gate.

## Config

`atesto.json` at the repo root (v2, written by `atesto init`):

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

Two config versions are supported:

- **v1** is the original key set. Omitting `version` means v1.
- **v2** requires `"version": 2` and adds `test_timeout_seconds`; `atesto init`
  writes v2 from now on.

Both versions reject **unknown top-level keys** in every mode (`check`,
`check --ci`, `proof`, `review`, `doctor`), naming all unknown keys at once.
There is no warning-only mode: a config that looks stronger than the tool can
enforce fails instead of silently under-enforcing. `check --ci` also rejects
unknown keys and a config `version` lower than the base.

The config is validated by
[`atesto.schema.json`](src/atesto/schemas/atesto.schema.json) (JSON Schema
draft 2020-12; `additionalProperties: false` in both variants). Point your
editor at its `$id` or the local path.

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
"tool": { "name": "atesto", "version": "0.2.0" }
```

The shape is validated when present; artifacts written by older versions
without `tool` remain valid. `check --json` exposes the proof's `tool` and
`doctor` reports it, so evidence provenance can be audited.

### Doctor

`atesto doctor` reads the raw config without hard-failing and reports unknown
keys, effective keys, version, timeout, artifacts root, artifact freshness,
hook status and tool version. It exits nonzero when it finds an issue, so it
shows why a gate would fail before `check` does.

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
  the PR instead. It pins `atesto==0.2.0` with a sha256, installs with
  `--require-hashes --no-deps` on Python 3.12, and runs `atesto --version`
  before the gate. It becomes runnable once 0.2.0 is published on PyPI.
- `integrations/hooks/pre-push` is the reference hook; `atesto hooks install`
  writes the same behavior inline. Both are optional adapters, not core.

## Limits

- `proof.json` is self-reported. For stronger enforcement, run `test_cmd` in CI
  (recommended) or adopt signed attestations later.
- A textual review is not cryptographically signed. Atesto checks that it is
  bound to the head, not that the reviewer is trustworthy.
- Atesto does not prove the code is correct; it proves the evidence claims are
  consistent and current.
- A dirty submodule is bound by its HEAD, status and tracked diff. Content of
  untracked files inside the submodule is still not hashed.
- The CI template pins the published 0.2.0 wheel by sha256. Until 0.2.0 is on
  PyPI, install from a pinned commit or a local checkout instead. Keep the
  workflow and `atesto.json` in CODEOWNERS; a PR that can edit the workflow
  can replace the gate.
- `--ci` does not allow a PR to change `test_cmd`, `artifacts_root`,
  `min_reviewers` downward, remove or raise a base timeout, downgrade the
  config version, or add excludes/review paths. Fixing those requires a
  base/admin change, not a feature PR.

## Release

The 0.2.0 wheel is built reproducibly so the CI template can pin a real hash:

- `[build-system]` pins `setuptools==84.2.1`;
- `.github/workflows/release.yml` pins `build==1.6.1` and sets
  `SOURCE_DATE_EPOCH=1795651200` (2026-11-26T00:00:00Z);
- rebuilding `atesto-0.2.0-py3-none-any.whl` from the same tree with those
  settings must produce the sha256 in
  `integrations/github-actions/atesto-gate.yml`.

After a release, recompute with
`sha256sum dist/atesto-0.2.0-py3-none-any.whl`. If the build backend or this
fixed timestamp changes, update the template hash in the same change.

## Development

```bash
python -m unittest discover -s tests -v
```

Apache-2.0. See `NOTICE` for attribution to the pstack line of work that
inspired this project.
