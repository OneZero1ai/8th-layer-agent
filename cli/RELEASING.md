# Releasing the cq binary

The 8th-Layer fork ships its own `cq` binary because it carries Go-side
additions (e.g. the `propose_batch` MCP tool) that upstream's published
binary does not. The plugin (`plugins/cq/scripts/cq_binary.py`) fetches
the binary over unauthenticated HTTPS from a CloudFront distribution in
front of a private S3 bucket.

There is no CI release pipeline yet — releases are cut manually with the
steps below. Automating this is tracked as a follow-up.

## Hosting

- S3 bucket: `8l-cli-releases-124074140789-us-east-1` (account `124074140789`,
  profile `8th-layer-app`, private, OAC-only).
- CloudFront: distribution `E2KE74D3FCXBSX`, domain `dyejnuj2nvzpy.cloudfront.net`.
- Asset layout: `cli/v{version}/cq_{OS}_{arch}.{tar.gz|zip}`
  where `OS` ∈ {`Darwin`, `Linux`, `Windows`} and `arch` ∈ {`x86_64`, `arm64`}.

## Cut a release

From `cli/`, pick the new `VER` (must be ≥ the `cli_min_version` you will
set in `plugins/cq/scripts/bootstrap.json`):

```sh
VER=0.9.0
SHA=$(git rev-parse --short HEAD)
DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
VP=github.com/mozilla-ai/cq/cli/internal/version
LD="-s -w -X $VP.version=$VER -X $VP.commit=$SHA -X $VP.date=$DATE"
OUT=/tmp/cq-release; rm -rf $OUT; mkdir -p $OUT

# version MUST be injected via ldflags — `cq --version` is parsed by
# cq_binary.py; a "dev" version fails the min-version check and re-downloads
# on every launch.

for t in "Darwin arm64 darwin arm64" "Darwin x86_64 darwin amd64" \
         "Linux x86_64 linux amd64" "Linux arm64 linux arm64"; do
  set -- $t
  d=$(mktemp -d)
  GOOS=$3 GOARCH=$4 go build -ldflags "$LD" -o "$d/cq" .
  tar -C "$d" -czf "$OUT/cq_$1_$2.tar.gz" cq
done
for t in "x86_64 amd64" "arm64 arm64"; do
  set -- $t
  d=$(mktemp -d)
  GOOS=windows GOARCH=$2 go build -ldflags "$LD" -o "$d/cq.exe" .
  (cd "$d" && zip -q "$OUT/cq_Windows_$1.zip" cq.exe)
done
```

Note: run the loops in `bash`, not `zsh` — `zsh` does not word-split
`$t` and the `set --` trick silently degrades to native-only builds.

Upload and bump the gate:

```sh
B=8l-cli-releases-124074140789-us-east-1
for f in $OUT/*.tar.gz; do aws s3 cp "$f" "s3://$B/cli/v$VER/$(basename $f)" \
  --profile 8th-layer-app --content-type application/gzip; done
for f in $OUT/*.zip; do aws s3 cp "$f" "s3://$B/cli/v$VER/$(basename $f)" \
  --profile 8th-layer-app --content-type application/zip; done
```

Then set `cli_min_version` in `plugins/cq/scripts/bootstrap.json` to `$VER`
and bump the plugin version in `plugins/cq/.claude-plugin/plugin.json`.
