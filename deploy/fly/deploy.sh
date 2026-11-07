#!/usr/bin/env bash
# Deploy a released image to fly.io by digest:  deploy/fly/deploy.sh 0.2.0
# The digest is recorded as BENCH_IMAGE_DIGEST, so frozen protocols name the exact image.
set -euo pipefail

version="${1:?usage: $0 <version, e.g. 0.2.0>}"
repo="ghcr.io/dietrichliko/vfgh-llm-bench"
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"

if command -v skopeo >/dev/null; then
  digest="$(skopeo inspect --no-creds --format '{{.Digest}}' "docker://$repo:$version")"
else
  digest="$(gh release view "v$version" --repo dietrichliko/vfgh-llm-bench --json body \
    --jq '.body' | grep -o 'sha256:[0-9a-f]\{64\}' | head -1)"
fi
[[ "$digest" == sha256:* ]] || { echo "no digest for $repo:$version" >&2; exit 1; }
[[ -f "$root/config/providers.fly.yaml" ]] || {
  echo "missing config/providers.fly.yaml (see README.md, fly.io)" >&2; exit 1; }

echo "deploying $repo@$digest"
cd "$root"
fly deploy --config deploy/fly/web.toml --image "$repo@$digest" \
  --env "BENCH_IMAGE_DIGEST=$digest"
