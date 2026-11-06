# Analysis plan

As of 2026-11-06. DRAFT, to be frozen with the protocol (see `docs/publication.md`). It fixes how the five levels of `docs/paper.md` §5 are computed from the stored runs, so that nothing is chosen after seeing results. The code is `bench/analysis/`; every rule below has a test.

## 1 Data

- **Runs:** decide runs with an answer (`done` or `not_evaluable`). Runs without an answer (`failed`, `unknown`, `cancelled`) are retried, not scored. If one cell (case, model, condition, phrasing, repetition) has several runs, the first one counts.
- **Cases per model:** only cases that pass the model's cutoff rule (decided at least 3 months after its training cutoff); orders never plan the others.
- **Outcome label:** one label per case: *unlawful*, *partly unlawful*, *dismissed*, *rejected*, *discontinued* (the confirmed outcome at release). A run's label comes from its operative part by the same rule. A run without a label (`not_evaluable`, or an operative part that maps to nothing) is **wrong**.

## 2 Level 1: outcome

- **Unit:** per case, model and condition the **majority label** of the model's runs (condition A: up to 5 repetitions × 3 phrasings; C: 1 run). A tie between labels counts as wrong.
- **Accuracy** = share of cases whose majority label equals the VfGH's, with a **Wilson** 95 % interval. Second column: share of correct runs per case, averaged over cases.
- **Cohen's κ** between majority label and VfGH label; **confusion matrix** VfGH × model.
- **Baselines**, on the same cases as the model (they differ by cutoff): *always unlawful* (at case level the same as *follow the application*, see architecture.md §6). A model beats the baseline if its accuracy is higher; the comparison is descriptive.
- **Conditions compared** (A vs B, A vs C) per model on the cases run in both: **McNemar**, exact binomial test on the discordant pairs, two-sided.

## 3 Level 2: decisive reasons

- **Per judgment:** each reference reason scored ja = 1, teilweise = ½, nein = 0; the **mean of the judges** per reason; the run's share = mean over its reasons.
- **Per model and condition:** mean over cases of the mean over the case's runs; 95 % interval by bootstrap over cases (2,000 resamples, percentile, fixed seed 20261106).

## 4 Level 3: citations

- **Fabricated:** share of checkable citations (norms and decisions) the RIS check did not find (`not_found`); `unresolvable` is reported separately, `error` and literature are excluded.
- **Inaccurate:** among found citations, the mean of the judges' "trägt" (ja = 1, teilweise = ½, nein = 0; *nicht beurteilbar* left out) below ½.
- Per model and condition pooled over runs; interval by bootstrap over cases.

## 5 Level 4: stability

- Condition A, per model: a case is **unstable** if its runs do not all give the same label (a missing label is a label of its own).
- **Fleiss' κ** over the cases with all runs, runs as raters.
- Reported per phrasing too (5 repetitions each), to separate wording from sampling.

## 6 Level 5: opposing reasonings

- Values tragfähig = 4, eher tragfähig = 3, eher nicht tragfähig = 2, nicht tragfähig = 1; **sound** = 3 or 4. With repetitions, a side's value is the mean.
- **Court side:** *unlawful* cases → the reasoning for unlawfulness; *dismissed* → the reasoning against. **Primary metrics use these two kinds only.** Partly unlawful cases get their own row: share judged sound per side.
- **Judged sound on both sides:** share of cases where both reasonings are sound.
- **AUC:** probability that the court-side reasoning gets the higher value than the sham one (ties ½), over all case pairs of the model (Mann–Whitney); interval by bootstrap over cases.

## 7 Judges and calibration

- **Expert ratings:** the first rating of each calibration item. Human–human agreement on the double items is the upper bound.
- **Agreement per criterion** (reference reasons; citations "trägt") and **per condition:** linearly weighted κ between the expert and the judges' mean, rounded to ja / teilweise / nein (0.25 and 0.75 count as teilweise). For citations, items where either side says *nicht beurteilbar* are left out. Also reported per judge.
- **Threshold:** κ ≥ 0.7 in **every** condition run. A criterion below it in any condition, or without calibration items in a condition, is shown but marked **not calibrated** and is not part of the confirmatory claims.
- **PPI** (prediction-powered inference, Angelopoulos et al. 2023) for the level 2 share and the level 3 inaccurate share, **per condition, pooled over models**: estimate = judge mean over all runs − mean (judge − expert) over the calibration items of that condition; variance = var(judge)/N + var(judge − expert)/n; normal 95 % interval. The calibration sample is stratified by model, not proportional, so this is an approximation (stated in the paper).

## 8 Contamination

- A probe **recalls** a case if its outcome matches and at least one judge finds at least one reference reason (*ja*).
- **Primary results include all cases.** A sensitivity table repeats levels 1 and 2 without the cases a model recalls. Pre-cutoff probes (positive control) are reported per model: share recalled before vs after the cutoff.

## 9 Reporting

- All intervals 95 %. With about 20 cases, results are descriptive; confirmatory are only the hypotheses registered at OSF (publication.md §4).
- Every figure and export names the protocol tag and hash it is based on.
