#!/usr/bin/env bash
# Deploy a version of the u:ai bridge to fly.io:  deploy/fly/deploy-bridge.sh 0.4.0
#
# The bridge image is built by its own CI and published on registry.cern.ch. fly.io cannot
# resolve images there (HTTP 500, 2026-11-12), although the image is public, so this
# script copies the linux/amd64 image unchanged into the app's own registry, checks that
# the digest is the same, and deploys it from there by digest. If the web app is not
# running, the bridge is stopped again afterwards: `bench all` starts it when needed.
set -euo pipefail

version="${1:?usage: $0 <bridge version, e.g. 0.4.0>}"
app="vfgh-bench-bridge"
web_app="vfgh-bench"
src="registry.cern.ch/mbi-vienna/aai-bridge:$version"
dst="registry.fly.io/$app:aai-bridge-$version"
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"

for tool in skopeo fly python3; do
  command -v "$tool" >/dev/null || { echo "$tool is needed" >&2; exit 1; }
done

# digest of the linux/amd64 image (the tag may point to a multi-platform index)
want="$(skopeo inspect --raw --no-creds "docker://$src" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for m in d.get("manifests", []):
    p = m.get("platform", {})
    if p.get("os") == "linux" and p.get("architecture") == "amd64":
        print(m["digest"])
        break
')"
if [[ -z "$want" ]]; then  # a single-platform image: its own digest
  want="$(skopeo inspect --no-creds --format '{{.Digest}}' "docker://$src")"
fi
[[ "$want" == sha256:* ]] || { echo "no linux/amd64 image for $src" >&2; exit 1; }

creds="x:$(fly auth token 2>/dev/null)"
echo "copying $src ($want) to $dst"
skopeo copy --quiet --override-os linux --override-arch amd64 --dest-creds "$creds" \
  "docker://$src" "docker://$dst"
got="$(skopeo inspect --creds "$creds" --format '{{.Digest}}' "docker://$dst")"
[[ "$got" == "$want" ]] || { echo "digest differs after the copy: $got != $want" >&2; exit 1; }

echo "deploying $dst@$want"
cd "$root"
fly deploy --config deploy/fly/bridge.toml --app "$app" --ha=false --image "$dst@$want"

web_state="$(fly machine list --app "$web_app" --json | python3 -c '
import json, sys
print(" ".join(m["state"] for m in json.load(sys.stdin)))
')"
if [[ " $web_state " != *" started "* ]]; then
  echo "$web_app is not running: stopping the bridge again"
  fly machine list --app "$app" --json | python3 -c '
import json, sys
print("\n".join(m["id"] for m in json.load(sys.stdin)))
' | xargs -r fly machine stop --app "$app"
fi
