# Stage 3 status (work in progress)

Saved 2026-10-29 on branch `stage-3`. Stages 1 and 2 are on `main`.

## Scope agreed for stage 3

- Model runs in **condition A** only. B needs the norm texts (stage 4), and C needs ris-mcp (stage 4).
- Prompts versioned in the DB; orders with cost estimate and config snapshot; DB job queue run by `bench worker`; progress page.
- **Freezing the protocol** (git push to the study repository, see `docs/publication.md`) is a separate step after the runs work.

## Done (not yet tested beyond smoke tests)

| File | Content |
| --- | --- |
| `src/bench/models/runs.py` | Tables `PromptVersion`, `Order`, `Job`, `Run`; statuses |
| `src/bench/migrations/versions/0003_prompts_orders_jobs_runs.py` | Migration; `runs` and `prompt_versions` append-only; unique (key, version) |
| `src/bench/llm/schemas.py` | Output schema `Entscheidung` (German field names), mapped to the stage 2 outcome labels; German prompted-output template |
| `src/bench/prompts/decide.p1–p3.md` | Three phrasings of the decision task (drafts, to be approved by the legal expert) |
| `src/bench/prompts.py` | Seed from files, versions in DB, save new version, render system/user messages |
| `src/bench/llm/task.py` | `run_task`: one call via Pydantic AI, status done / not_evaluable / failed, cost, raw capture |
| `src/bench/providers.py` | `expected_output_tokens` per model; `json_object` per provider; model profile set from our config (smoke-tested: prompted mode sends no `response_format`, native sends `json_schema`) |
| `src/bench/orders.py` | Plan (interleaved jobs, cutoff + 3 months rule), estimate, snapshot, create, pause/resume/cancel, retry, progress |
| `src/bench/worker.py` | Worker: atomic claim, per-provider concurrency and rate, lease + heartbeat, recovery (unsent → queued, sent → unknown), cost caps, pause after 5 provider failures, config from order snapshot |
| `src/bench/settings.py`, `src/bench/cli.py` | `BENCH_COST_CAP_TOTAL_USD`, `BENCH_WORKER_CONCURRENCY`; `bench worker` runs the real worker |

## Open

1. Fix the 6 mypy errors and 1 lint finding (`uv run mypy src`, `uv run ruff check src tests`):
   - `providers.py`: `model_profile()` may return None;
   - `orders.py`: Counter of floats, `spent()` return type, status type in `progress()`;
   - `worker.py`: an unused `type: ignore`.
2. Seed prompts at web start-up too (`prompts.seed` in the app lifespan).
3. Web pages:
   - **Prompts:** list, history, edit (new version).
   - **Orders:** form with cases, tested models, conditions (B/C disabled), repetitions, phrasings and cost cap; preview with job count, cutoff exclusions and estimate; create.
   - **Order detail:** progress polled by htmx; pause, resume, cancel and retry.
   - **Run detail:** messages, response, parsed output, raw exchanges.
   - Add the navigation links.
4. Tests:
   - prompts (seed, save, render);
   - plan (interleaving, cutoff, estimate);
   - worker with a mocked LLM: done, not evaluable, failed, recovery, cost cap, pause after failures, snapshot hash;
   - pages.
5. Update `config/providers.example.yaml` (`expected_output_tokens`, `json_object`, OpenRouter `usage: {include: true}` for reported cost), `docs/architecture.md` and the README.
6. Commit as `feat(runs): …`, merge into `main`.

## How to resume

```bash
cd ~/working/vfgh-llm-bench && git checkout stage-3
uv sync && uv run pytest -q        # 99 tests from stages 1–2 pass
```

Then tell Claude: "continue stage 3 from docs/stage-3-status.md".
