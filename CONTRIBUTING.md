# Contributing

Atesto is intentionally small: evidence + gate. Changes should preserve that
scope.

```bash
python3 -m pip install -e .
PYTHONPATH=src python3 -m unittest discover -s tests -v
ruff check .
```

- No new runtime dependencies without a strong reason.
- Every behavior change needs a negative test (the gate must fail for the
  falsified case).
- Do not add blast radius, statechart, queue, dashboard or CI takeover to the
  core. Adapters live under `integrations/` and are optional.
- License: Apache-2.0. By contributing you agree to license your contribution
  under the same terms.

## Issues and labels

Work is tracked in GitHub Issues. Triage uses five labels: `needs-triage`,
`needs-info`, `ready-for-agent`, `ready-for-human` and `wontfix`.

The conventions agents follow in this repo — tracker operations, label
vocabulary and domain docs — live under `docs/agents/`, with entry points in
`AGENTS.md`.
