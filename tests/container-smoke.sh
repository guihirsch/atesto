#!/usr/bin/env bash
# Smoke-test the Atesto container image: pinned version, non-root user, no
# provider CLIs, and an init/proof/check cycle in a mounted temporary repo.
#
# Usage: tests/container-smoke.sh IMAGE [EXPECTED_VERSION]
set -euo pipefail

image="${1:-}"
expected_version="${2:-}"

if [ -z "$image" ]; then
  echo "usage: $0 IMAGE [EXPECTED_VERSION]" >&2
  exit 2
fi

version="$(docker run --rm "$image" --version)"
if [ -n "$expected_version" ] && [ "$version" != "atesto $expected_version" ]; then
  echo "version mismatch: got '$version', want 'atesto $expected_version'" >&2
  exit 1
fi
case "$version" in
  "atesto "*) ;;
  *)
    echo "unexpected version output: $version" >&2
    exit 1
    ;;
esac

uid="$(docker run --rm --entrypoint id "$image" -u)"
if [ "$uid" = "0" ]; then
  echo "image must run as non-root, got uid 0" >&2
  exit 1
fi

if docker run --rm --entrypoint sh "$image" -c \
  'command -v gh >/dev/null 2>&1 || command -v glab >/dev/null 2>&1'; then
  echo "image must not bundle gh or glab" >&2
  exit 1
fi

repo="$(mktemp -d)"
cleanup() { rm -rf "$repo"; }
trap cleanup EXIT

git -C "$repo" init -q
git -C "$repo" config user.email "atesto@example.com"
git -C "$repo" config user.name "Atesto"
git -C "$repo" config commit.gpgsign false
printf 'x = 1\n' > "$repo/app.py"
git -C "$repo" add app.py
git -C "$repo" commit -qm base

run() {
  docker run --rm --user "$(id -u):$(id -g)" -v "$repo:/repo" "$image" "$@"
}

run init --require proof
run proof demo -- python3 -c "print('ok')"
git -C "$repo" add -A
git -C "$repo" commit -qm evidence

output="$(run check)"
printf '%s\n' "$output"
grep -q "atesto: ok" <<<"$output"
