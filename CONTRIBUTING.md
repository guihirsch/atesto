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
  core. Keep the boundary explicit:
  - review-source adapters (GitHub, GitLab, future providers) live in the
    optional `atesto/review_sources/` package. They shell out to the provider
    CLI and emit the strict review contract; the CLI imports only the adapter
    selected by its flag.
  - the core keeps the review writer, `check` and `--from-json`; it must not
    encode provider merge policy.
  - CI and domain adapters live under `integrations/` and stay optional too.
- License: Apache-2.0. By contributing you agree to license your contribution
  under the same terms.
