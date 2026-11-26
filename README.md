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
  must exit 0, and must not change the code tree.

Artifacts are excluded from the tree hash, so committing the evidence does not
invalidate the proof.

## Commands

```bash
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

`atesto.json` at the repo root:

```json
{
  "version": 1,
  "artifacts_root": "docs/atesto",
  "require": ["proof"],
  "min_reviewers": 1,
  "test_cmd": "",
  "review_artifacts_paths": ["docs/atesto"],
  "fingerprint_excludes": []
}
```

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
  the PR instead. It becomes runnable after the first PyPI release.
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
- The CI template installs a PyPI version that is not published yet; it is a
  post-v0 artifact. Keep the workflow and `atesto.json` in CODEOWNERS; a PR
  that can edit the workflow can replace the gate.
- `--ci` does not allow a PR to change `test_cmd`, `artifacts_root`, `min_reviewers`
  downward, or add excludes/review paths. Fixing those requires a base/admin
  change, not a feature PR.

## Development

```bash
python -m unittest discover -s tests -v
```

Apache-2.0. See `NOTICE` for attribution to the pstack line of work that
inspired this project.
