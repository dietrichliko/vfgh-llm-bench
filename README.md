# vfgh-llm-bench

Web application for the study *Do LLMs Reason Like the Austrian Constitutional Court?*:
language models decide regulatory review cases of the VfGH (Art 139 B-VG), and their
answers are compared with the actual decisions.

| Document | Content |
| --- | --- |
| [docs/paper.md](docs/paper.md) | Study design (snapshot of the Claude Doc) |
| [docs/plain-language.md](docs/plain-language.md) | The study in plain terms, for readers without a technical background |
| [docs/architecture.md](docs/architecture.md) | How the application is built |
| [docs/publication.md](docs/publication.md) | Study protocol in git, Zenodo, OSF |
| [docs/analysis-plan.md](docs/analysis-plan.md) | How every metric is computed (frozen with the protocol) |

**Status:** all planned stages are built (see the roadmap in `docs/architecture.md` §12).

| Stage | What it adds |
| --- | --- |
| 1 | Login, providers and models with test calls, users, container setup |
| 2 | RIS import, input/reference split, outcome per provision, review and release |
| 3 | Condition A: versioned prompts, orders with preview, cost estimate and snapshot, background worker with progress, pause, resume, cancel, retry and cost caps |
| 4 | Condition B (norm texts at the Stichtag, reviewed per case) and C (RIS legislation MCP server, tool limit and log) |
| 5 | Evaluation: reference checklists with expert review, judges with citation check in the RIS, calibration queue, opposing reasonings, contamination probe |
| 6 | Analysis: analysis plan and metrics, dashboard (figures 1–4), export, Freeze protocol and Publish results |

Before the study runs: the legal expert approves the draft prompts, and the study
repository is set up (see *Study repository* below and `docs/publication.md`).

## Configuration

- **Providers and models:** `config/providers.yaml` (start from
  `config/providers.example.yaml`). Every provider is an OpenAI-compatible endpoint.
- **Secrets** are never in the config. Each is read from `/run/secrets/<NAME>` or the
  environment variable `<NAME>`:

  | Secret | Purpose |
  | --- | --- |
  | `BENCH_SESSION_SECRET` | Signs the login cookie (required unless dev login) |
  | `BENCH_OIDC_CLIENT_SECRET` | OIDC login (e.g. CERN SSO) |
  | `UAI_API_KEY`, `OPENROUTER_API_KEY`, … | Whatever `api_key_secret` names in `providers.yaml` |
  | `RIS_MCP_API_KEY` | Shared by `bench ris-mcp` and the worker (condition C) |
  | `STUDY_REPO_DEPLOY_KEY` | SSH deploy key with write access to the study repository only (optional) |

- **Settings** (environment, prefix `BENCH_`): `BENCH_DATA_DIR` (`/data`), `BENCH_CONFIG`
  (`/config/providers.yaml`), `BENCH_SECRETS_DIR` (`/run/secrets`), `BENCH_DATABASE_URL`
  (default SQLite in the data dir), `BENCH_PUBLIC_URL`, `BENCH_OIDC_ISSUER`,
  `BENCH_OIDC_CLIENT_ID`, `BENCH_ADMIN_EMAILS` (comma-separated), `BENCH_DEV_LOGIN`,
  `BENCH_COST_CAP_TOTAL_USD` (spending limit over all orders), `BENCH_WORKER_CONCURRENCY`
  (LLM calls in flight, default 8), `BENCH_RIS_MCP_URL` (default `http://ris-mcp:8000/mcp`),
  `BENCH_RIS_MCP_ALLOWED_HOSTS` (host check of the MCP server, comma-separated),
  `BENCH_STUDY_REPO_URL` and `BENCH_STUDY_REPO_BRANCH` (default `main`) for the study
  repository, `BENCH_GIT_COMMIT` (set at image build) and `BENCH_IMAGE_DIGEST` (set when
  running a released image, see *Commits and releases*); both are recorded in the frozen
  protocol.

## Study repository

Freeze protocol and Publish results (page *Studie*) commit to a separate git repository
below `studies/<slug>/` and push an annotated tag (`<slug>-protocol-vN`,
`<slug>-results-vN`). Set `BENCH_STUDY_REPO_URL` (e.g.
`git@github.com:<owner>/vfgh-llm-bench-study.git`) and put a deploy key with write access
to that repository into the secret `STUDY_REPO_DEPLOY_KEY`. Without a URL, the page offers
protocol and results as ZIP files to commit by hand. A GitHub release from each tag
(created by hand) triggers the Zenodo archive and DOI.

## Run with containers

```bash
cp config/providers.example.yaml config/providers.yaml      # then edit
mkdir -p deploy/secrets
openssl rand -hex 32 > deploy/secrets/BENCH_SESSION_SECRET
openssl rand -hex 32 > deploy/secrets/RIS_MCP_API_KEY
for s in BENCH_OIDC_CLIENT_SECRET UAI_API_KEY OPENROUTER_API_KEY STUDY_REPO_DEPLOY_KEY; do
  touch deploy/secrets/$s                                   # empty = not set
done
# u:ai bridge credentials: deploy/secrets/bridge-univie.env
#   AAI_BRIDGE_CLIENT_ID=… AAI_BRIDGE_CLIENT_SECRET=… AAI_BRIDGE_API_KEY=…
# UAI_API_KEY has the same value as AAI_BRIDGE_API_KEY.
# Rootless Podman with SELinux (Fedora): the containers read the files as another user
# and need the container label; the directory keeps them private on the host.
chmod 700 deploy/secrets && chmod 644 deploy/secrets/*
chcon -R -t container_file_t deploy/secrets
GIT_COMMIT=$(git rev-parse HEAD) BENCH_DEV_LOGIN=1 BENCH_ADMIN_EMAILS=you@example.org \
  podman compose -f deploy/compose.yaml up --build           # http://localhost:8000
```

`podman compose` needs a compose provider. Without one installed, `uvx podman-compose`
works in its place (`uvx podman-compose -f deploy/compose.yaml up --build`). After a
rebuild, podman-compose keeps the running containers on the old image: run `down` first.

**Upgrading** an existing installation: new secrets in `compose.yaml` must exist as files,
even if empty (compose refuses to start otherwise). Since the study repository was added:
`touch deploy/secrets/STUDY_REPO_DEPLOY_KEY`.

Locally, the containers are built from the working tree. fly.io runs the released image.

## fly.io

Two apps in the region `fra`:

- **`vfgh-bench`** (`deploy/fly/web.toml`): the released image from ghcr.io, one machine
  with a volume. `bench all` runs web, worker and ris-mcp together, because the SQLite
  volume belongs to one machine. It stops itself 15 minutes after the last request once
  no job is open; the next request starts it again (cold start a few seconds). fly.io's
  own auto-stop is off: it sees only requests, so it would stop a running order.
- **`vfgh-bench-bridge`** (`deploy/fly/bridge.toml`): the u:ai bridge, private only. The
  worker calls it at `vfgh-bench-bridge.internal:8000`, not through fly.io's proxy,
  which closes connections that are silent for 60 s (u:ai calls are not streamed).
  `bench all` starts it when it starts and stops it when it stops.

**Backups:** daily volume snapshots (kept 30 days) and off-site backups to a Tigris
bucket, made by `bench all` before a migration, every 6 hours while it runs and when it
stops, each only if the database changed. All backups of the last 14 days are kept,
before that the newest of each week.

**First setup** (needs [flyctl](https://fly.io/docs/flyctl/install/) and `fly auth login`):

```bash
# provider config for fly.io: as config/providers.yaml, but the u:ai base_url is
#   http://vfgh-bench-bridge.internal:8000/v1
cp config/providers.yaml config/providers.fly.yaml          # then edit

# 1. bridge
fly apps create vfgh-bench-bridge
fly secrets set --app vfgh-bench-bridge --stage AAI_BRIDGE_CLIENT_ID=… \
    AAI_BRIDGE_CLIENT_SECRET=… AAI_BRIDGE_API_KEY=…
# fly.io cannot pull from registry.cern.ch: copy the (public) image into fly's registry
skopeo copy --dest-creds "x:$(fly auth token)" \
    docker://registry.cern.ch/mbi-vienna/aai-bridge:0.4.0 \
    docker://registry.fly.io/vfgh-bench-bridge:aai-bridge-0.4.0
fly deploy --config deploy/fly/bridge.toml --ha=false \
    --image registry.fly.io/vfgh-bench-bridge:aai-bridge-0.4.0

# 2. web app, volume and backup bucket
fly apps create vfgh-bench
fly volumes create bench_data --app vfgh-bench --region fra --size 1
fly storage create --app vfgh-bench --name vfgh-bench-backups   # sets BUCKET_NAME, AWS_*
fly secrets set --app vfgh-bench --stage \
    BENCH_SESSION_SECRET=$(openssl rand -hex 32) \
    RIS_MCP_API_KEY=$(openssl rand -hex 32) \
    BENCH_OIDC_CLIENT_SECRET=… OPENROUTER_API_KEY=… \
    UAI_API_KEY=…                       # = AAI_BRIDGE_API_KEY of the bridge
fly secrets set --app vfgh-bench --stage \
    FLY_API_TOKEN="$(fly tokens create deploy --app vfgh-bench-bridge --expiry 8760h)"
fly secrets set --app vfgh-bench --stage BENCH_ADMIN_EMAILS=…   # not in the public repo
# BENCH_OIDC_CLIENT_ID is in deploy/fly/web.toml (CERN application vfgh-law-bench), then:
deploy/fly/deploy.sh 0.4.0

# 3. own domain (DNS at Cloudflare): CNAME vfgh-llm-bench → vfgh-bench.fly.dev,
#    "DNS only" (grey cloud), so that fly.io gets its own certificate
fly certs add vfgh-llm-bench.mbi-ai.dev --app vfgh-bench
fly certs show vfgh-llm-bench.mbi-ai.dev --app vfgh-bench    # until it is issued
```

**Login:** CERN SSO (OIDC). Register the application in the
[CERN Application Portal](https://application-portal.web.cern.ch) with the redirect URI
`https://vfgh-llm-bench.mbi-ai.dev/auth/callback`; its default role must admit eduGAIN
accounts for users from other universities (e.g. a University of Vienna account via
"Home organisation – eduGAIN"). The app needs an e-mail address from the login. New
accounts wait for an admin; addresses in `BENCH_ADMIN_EMAILS` are admins at once.

**Upgrade** to a new release: `deploy/fly/deploy.sh X.Y.Z`. It deploys the image by
digest and sets `BENCH_IMAGE_DIGEST`. A pending migration is preceded by a backup.
Deploy only while no order is running (pause it first): the deploy stops the worker, and
a call already sent ends as *unbekannt*, to be retried from the order page.

**Restore** a backup:

```bash
fly ssh console --app vfgh-bench -C "bench restore --list"
fly ssh console --app vfgh-bench -C "bench restore backups/<stamp>-idle.sqlite.gz"
fly machine restart --app vfgh-bench    # swaps it in; the old database stays beside it
```

`fly ssh console` needs the machine running: open the app in the browser first. The
token for the bridge expires after a year (`--expiry`); renew it with the same command.

## Development

```bash
uv sync
BENCH_DATA_DIR=dev-data BENCH_CONFIG=config/providers.yaml BENCH_SECRETS_DIR=dev-secrets \
  BENCH_DEV_LOGIN=1 BENCH_ADMIN_EMAILS=you@example.org uv run bench serve
uv run bench worker                     # same env as serve; runs the orders' LLM calls
uv run bench ris-mcp --port 8001        # condition C; worker: BENCH_RIS_MCP_URL=http://localhost:8001/mcp
uv run bench check-providers            # test call to every model with a key
uv run bench import-cases --from 2025-07-01   # same as the import on the cases page
uv run bench export results.zip         # data, metrics and dashboard (as on the dashboard)
uv run pytest && uv run ruff check && uv run mypy src tests
pre-commit install --hook-type pre-commit --hook-type commit-msg
```

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request: ruff, mypy over `src` and
`tests`, and the test suite; then it builds the container image with the commit sha and
smoke-tests it (`bench --help`, git and ssh present, `bench migrate` on an empty database).
Branch pushes build the image without pushing it; releases push it (below).

## Commits and releases

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (checked by the
commitizen commit-msg hook). A release is one command:

```bash
uv run cz bump          # next version from the commits, updates pyproject.toml, uv.lock and
                        # CHANGELOG.md, commits and tags vX.Y.Z
git push --follow-tags
```

The tag runs `.github/workflows/release.yml`. It first checks that the tag matches
`pyproject.toml` and that `CHANGELOG.md` has its section, then runs the CI checks, pushes
the image to `ghcr.io/dietrichliko/vfgh-llm-bench`, smoke-tests exactly that image and
creates a GitHub release with the changelog section and the image digest. Image tags:
`X.Y.Z`, `X.Y` (for a pre-release such as `0.2.0rc1` only the full version) and
`sha-<full commit sha>`; there is no `latest`.

Deploy the released image by version or digest and set `BENCH_IMAGE_DIGEST` to the digest
from the release, so frozen protocols record which image produced them. Do not set it for
an image built locally (`compose.yaml` builds its own, fly.io too): the digest would not
describe it. After the first release, check the package's visibility on GitHub; while it
is private, servers need `podman login ghcr.io` with a token that can read packages.
Freeze the study protocol only from a released version.

`uv run cz changelog --dry-run` shows the changelog for unreleased commits.
