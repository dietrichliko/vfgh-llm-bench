# vfgh-llm-bench

Web application for the study *Do LLMs Reason Like the Austrian Constitutional Court?*:
language models decide regulatory review cases of the VfGH (Art 139 B-VG), and their
answers are compared with the actual decisions.

| Document | Content |
| --- | --- |
| [docs/paper.md](docs/paper.md) | Study design (snapshot of the Claude Doc) |
| [docs/architecture.md](docs/architecture.md) | How the application is built |
| [docs/publication.md](docs/publication.md) | Study protocol in git, Zenodo, OSF |
| [docs/prior-work.md](docs/prior-work.md) | The predecessor project `vfgh-llm-begruendung` |

**Status:** stage 2 (cases): RIS import, input/reference split, outcome per provision,
review and release. Stage 1: login, providers and models with test calls, users, container
setup. Runs, evaluation and dashboard follow in stages 3–6
(see the roadmap in `docs/architecture.md`).

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

- **Settings** (environment, prefix `BENCH_`): `BENCH_DATA_DIR` (`/data`), `BENCH_CONFIG`
  (`/config/providers.yaml`), `BENCH_SECRETS_DIR` (`/run/secrets`), `BENCH_DATABASE_URL`
  (default SQLite in the data dir), `BENCH_PUBLIC_URL`, `BENCH_OIDC_ISSUER`,
  `BENCH_OIDC_CLIENT_ID`, `BENCH_ADMIN_EMAILS` (comma-separated), `BENCH_DEV_LOGIN`.

## Run with containers

```bash
cp config/providers.example.yaml config/providers.yaml      # then edit
mkdir -p deploy/secrets
openssl rand -hex 32 > deploy/secrets/BENCH_SESSION_SECRET
for s in BENCH_OIDC_CLIENT_SECRET UAI_API_KEY OPENROUTER_API_KEY; do
  touch deploy/secrets/$s                                   # empty = not set
done
# u:ai bridge credentials: deploy/secrets/bridge-univie.env
#   AAI_BRIDGE_CLIENT_ID=… AAI_BRIDGE_CLIENT_SECRET=… AAI_BRIDGE_API_KEY=…
BENCH_DEV_LOGIN=1 BENCH_ADMIN_EMAILS=you@example.org \
  podman compose -f deploy/compose.yaml up --build           # http://localhost:8000
```

fly.io (temporary): see `deploy/fly/web.toml`.

## Development

```bash
uv sync
BENCH_DATA_DIR=dev-data BENCH_CONFIG=config/providers.yaml BENCH_SECRETS_DIR=dev-secrets \
  BENCH_DEV_LOGIN=1 BENCH_ADMIN_EMAILS=you@example.org uv run bench serve
uv run bench check-providers            # test call to every model with a key
uv run bench import-cases --from 2025-07-01   # same as the import on the cases page
uv run pytest && uv run ruff check && uv run mypy src
pre-commit install --hook-type pre-commit --hook-type commit-msg
```

## Commits and releases

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (checked by the
commitizen commit-msg hook). A release is one command:

```bash
uv run cz bump          # next version from the commits, updates pyproject.toml, uv.lock and
                        # CHANGELOG.md, commits and tags vX.Y.Z
git push --follow-tags
```

`uv run cz changelog --dry-run` shows the changelog for unreleased commits.
