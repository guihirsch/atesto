# Atesto CI snippets

Additive examples for running the Atesto gate in the published container image
on five hosts. Copy one file (or one job/stage/step) into your pipeline; none
of them edit or replace existing CI.

| Host | File |
| --- | --- |
| GitLab CI | `gitlab-ci.yml` |
| GitHub Actions | `github-actions.yml` |
| Jenkins | `jenkins.Jenkinsfile` |
| CircleCI | `circleci.yml` |
| Buildkite | `buildkite.yml` |

Every snippet pins `ghcr.io/guihirsch/atesto:0.2.0`, fetches full history, sets
`BASE_REF` and runs `atesto check --ci --base "$BASE_REF"`. The image version
must match `pyproject.toml`; `tests/test_atesto.py::CISnippetTest` fails when a
snippet drifts.

## Review and proof are committed before CI runs

`check --ci` requires the evidence directory (`docs/atesto/<slug>/`) to be part
of the PR/MR diff. Generate the artifact on the branch, commit it and push the
commit; CI must not generate, commit or push evidence. A review created inside
the gate job would not be in the diff, and committing it in CI would create a
head that the provider approval was never issued for.

Generate on the branch (provider CLI required only for the provider sources):

```bash
atesto proof demo -- pytest -q
atesto review demo --from-github-pr 123      # or --from-gitlab-mr 42
atesto review demo --from-json approvals.json
atesto review demo --head HEAD --verdict APPROVED --reviewer alice
git add docs/atesto/demo
git commit -m "docs(atesto): add evidence"
```

Provider tokens stay with `gh`/`glab`; Atesto never reads them, and the image
does not include either CLI.

## Base refs

- GitLab: `origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME`.
- GitHub: `origin/${{ github.base_ref }}`.
- Jenkins: `origin/$CHANGE_TARGET`.
- CircleCI: `ATESTO_BASE_REF`, default `origin/main`.
- Buildkite: `origin/${BUILDKITE_PULL_REQUEST_BASE_BRANCH:-main}`.

Add `--slug <slug>` when `docs/atesto/` contains more than one evidence
directory.

## Enforcement

Atesto returns a nonzero exit code; it does not block merges. Make the job a
required check in the host: GitHub branch protection/rulesets or merge queue,
GitLab protected branches/approval rules, or the equivalent Jenkins, CircleCI
and Buildkite settings. Keep the snippet and `atesto.json` in CODEOWNERS: a PR
that can edit the gate can replace it.
