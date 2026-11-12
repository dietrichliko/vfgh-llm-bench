## v0.4.0 (2026-11-12)

### Feat

- **worker**: wait for a rate-limited provider instead of failing the job

### Fix

- **worker**: a refused connection before a 429 still waits for the provider
- **citations**: a regulation cited by its full name is not an invented source
- **judges**: a left-out citation is not assessable, not a lost score

## v0.3.1 (2026-11-12)

### Fix

- **db**: one process migrates at a time

## v0.3.0 (2026-11-11)

### Feat

- **providers**: a tested model needs a cutoff or a release date
- **cases**: printable page with the court's decision beside the models' answers
- **calibration**: draw the expert's answers from a chosen subset of cells
- **judges**: judge a chosen subset of the answers
- **orders**: release rule: only cases decided after the model's release
- **analysis**: within-case measure for level 5; pilot comparisons exploratory
- **protocol**: pinned providers for tested models; leave out other models' runs
- **analysis**: compare conditions run against run
- **analysis**: count every planned cell, show what levels 2 and 3 rest on
- **citations**: checker miss rate on the court's own citations
- **llm**: cap answer length and run time per provider
- **cases**: button to remove the Stichtag
- **cases**: show the fetched norm version in the review, clearer exclusion label

### Fix

- **llm**: a run over the time limit is not evaluable
- **ris-mcp**: read "erster/letzter Satz" as part of a norm citation
- **citations**: a lookup miss is not a fabricated citation
- **orders**: cutoff rule uses the real month end
- **orders**: retry re-sends only failed and unknown jobs
- **worker**: keep the paid answer when post-processing fails
- **llm**: store usage with Pydantic AI's Decimal cost estimate
- **llm**: decode compressed responses only once

## v0.2.0 (2026-11-07)

### Feat

- **deploy**: fly.io with the released image and a private u:ai bridge
- **deploy**: bench all with idle stop, off-site backups and restore

### Fix

- **deploy**: review findings for bench all, restore and the bridge

## v0.1.0 (2026-11-07)

### Feat

- **study**: freeze protocol, amendments and publish results (stage 6d)
- **analysis**: export of data, metrics and dashboard as one ZIP (stage 6c)
- **analysis**: results dashboard with figures 1-4 (stage 6b)
- **analysis**: analysis plan and metrics for the five levels (stage 6a)
- **eval**: contamination probe from date and case number (stage 5e)
- **eval**: opposing reasonings and their assessment (stage 5d)
- **eval**: calibration queue for the legal expert (stage 5c)
- **eval**: judges and citation check (stage 5b)
- **eval**: reference checklists with expert review (stage 5a)
- **runs**: condition B with norm texts reviewed per case
- **runs**: condition C with the RIS legislation MCP server
- **runs**: model runs in condition A with orders, worker and progress pages
- **cases**: import VfGH decisions from the RIS, split, review and release

### Fix

- **deploy**: correct commit in podman builds, worker health, upgrade path
- **version**: read the version from the package metadata
