# Analysis plan

As of 2026-11-15. DRAFT, to be frozen with the protocol (see `docs/publication.md`). It fixes how the metrics are computed from the stored runs, so that nothing is chosen after seeing results. The code is `bench/analysis/`; every rule below has a test.

## 0 Research question and dimensions (ratio-lab)

**Research question.** Under what practical conditions can an LLM act as a reliable and reviewable epistemic actor in legal reasoning? Regulatory review by the VfGH (Art 139 B-VG) is the test case ("legal laboratory"). The court's carrying reasons (*ratio decidendi*) are the **reference, not the target**: the outcome is one measurement among many.

**Conditions.** Each case is decided under up to three conditions:

| | Condition | The model gets |
| --- | --- | --- |
| A | Closed knowledge system (*Geschlossenes Wissenssystem*) | the case only |
| B | Provided sources (*Vorgegebene Quellen*) | the case and the approved norm set, valid on the Stichtag |
| C | Own research (*Eigene Recherche*) | the case and RIS tools (`suche_normen`, `hole_norm`), at most 15 calls |

B − A isolates **inference given the right sources**; C − B isolates the cost of **acquiring** them.

**Five dimensions and the stress test**, with the metrics that measure them:

| # | Dimension | Question | Metrics (section) |
| --- | --- | --- | --- |
| 1 | Knowledge acquisition | Does the model find the relevant norms, in the version valid on the decisive date? | reference-norm recall, version correctness, search effort, condition C (§10); accuracy C vs B (§2) |
| 2 | Knowledge representation | Does each cited norm say what the model claims, or is it distorted or invented? | not found in the RIS, inaccurate (§4); contamination probe (§8); error category 5 (§11) |
| 3 | Legal inference | Does it connect the norms to the case and reach a legally supported conclusion? | outcome (§2), share of reference reasons (§3); error categories 1–3 (§11) |
| 4 | Justification / reviewability | Can a lawyer follow, examine and contest the reasoning? | expert reviewability 1–4 (§11) |
| 5 | Reliability | Same outcome and similar reasons across runs and phrasings? | unstable across runs and across phrasings, Fleiss' κ (§5) |
| – | Stress test | Does fluent reasoning carry information about legal correctness? | opposing reasonings: both sound, AUC (§6); error categories 4 and 6 (§11) |

The dashboard ("Epistemisches Profil") leads with these dimensions; the outcome table is sorted by model, not by accuracy.

**Opposing reasonings (pilot).** A helper model writes one reasoning for and one against unlawfulness; the tested models assess each separately (condition A). This measures whether a model *recognises* a legally supported reasoning, not whether it can *produce* a persuasive one; the latter (tested models write both, a lawyer picks blind) is a later extension.

## 1 Data

- **Cells:** the denominator is the planned cells (case, model, condition, phrasing, repetition) of the orders. A cell counts with its first run that has an answer (`done` or `not_evaluable`). A cell whose job ended without an answer (`failed`, `unknown`) is retried once; after two attempts it counts as an answer without a label, i.e. **wrong**. Cells still open are left out and their number is shown; cancelled cells are not planned any more.
- **Who answered:** a run whose endpoint reports another model than the configured one (`model_mismatch`) is left out of every metric; the reported provider and model per tested model are listed. Freezing the protocol requires a pinned upstream for every tested model on OpenRouter (`provider.order` with `allow_fallbacks: false`) and warns if `temperature` is not stated in its `parameters`.
- **Time limit:** a run that exceeds the wall-clock limit is `not_evaluable` (wrong), like an answer cut off at the token cap: both are a model that does not finish.
- **Cases per model:** only cases that pass the protocol's rule; orders never plan the others. *Cutoff rule:* decided at least 3 months after the model's training cutoff. *Release rule* (pilot): decided after the model's public release (`released` in the model config), so the decision cannot be in its training data whatever the real cutoff is.
- **Outcome label:** one label per case: *unlawful*, *partly unlawful*, *dismissed*, *rejected*, *discontinued* (the confirmed outcome at release). A run's label comes from its operative part by the same rule. A run without a label (`not_evaluable`, or an operative part that maps to nothing) is **wrong**.

## 2 Level 1: outcome

- **Unit:** per case, model and condition the **majority label** of the model's runs (condition A: up to 5 repetitions × 3 phrasings; C: 1 run). A tie between labels counts as wrong.
- **Accuracy** = share of cases whose majority label equals the VfGH's, with a **Wilson** 95 % interval. Second column: share of correct runs per case, averaged over cases.
- **Cohen's κ** between majority label and VfGH label; **confusion matrix** VfGH × model.
- **Baselines**, on the same cases as the model (they differ by cutoff): *always unlawful* (at case level the same as *follow the application*, see architecture.md §6). A model beats the baseline if its accuracy is higher; the comparison is descriptive.
- **Common cases:** models differ in their cases (cutoff rule). Level 1 is also reported per condition on the cases every model of that condition was run on.
- **Conditions compared** (A vs B, A vs C) per model: **McNemar**, exact binomial test on the discordant pairs, two-sided. **Primary: one run against one run**, the cell with phrasing 1 and repetition 1 in both conditions, on the cases that have it in both. A majority of many runs against one run would favour the condition with more runs (the majority averages out sampling noise) for a reason unrelated to source access. The majority comparison is reported in addition only where both conditions have the same number of runs in every shared case.

## 3 Level 2: decisive reasons

- **Per judgment:** each reference reason scored ja = 1, teilweise = ½, nein = 0; the **mean of the judges** per reason; the run's share = mean over its reasons.
- **Per model and condition:** mean over cases of the mean over the case's runs; 95 % interval by bootstrap over cases (2,000 resamples, percentile, fixed seed 20261106).
- **Evaluable answers only:** levels 2 and 3 are measured on judged (`done`) answers; the number of judged answers is reported next to the number of cells, so a model with many unusable answers is not hidden behind its good ones.

## 4 Level 3: citations

- **Not found in the RIS:** share of checkable citations (norms and decisions) the RIS check did not find (`not_found`); `unresolvable` is reported separately, `error`, literature and `not_checkable` (regulations, EU law and treaties the RIS does not have) are excluded. A regulation cited by its name "X zum Y" (e.g. "Zweite Durchführungsverordnung zum Tiroler Jagdgesetz 2004") is looked up as X and counts as found only if the RIS title also names Y; otherwise it is `not_checkable`, never `not_found`. A norm not in force on the Stichtag but found in another version counts as found; a compound citation ("iVm") is not found if any part is not.
- **Checker miss rate (baseline):** the same check over the citations in the court's own considerations of all released cases (`bench check-citations-baseline`); these all exist, so their `not_found` share is the checker's error rate (Wilson interval). It is reported next to the models' rate. "Not found" is not called "fabricated": only the part of a model's rate above the baseline can point to invented citations.
- **Inaccurate:** among found citations, the mean of the judges' "trägt" (ja = 1, teilweise = ½, nein = 0; *nicht beurteilbar* left out) below ½.
- Per model and condition pooled over runs; interval by bootstrap over cases.

## 5 Level 4: stability

- Condition A, per model: a case is **unstable** if its runs do not all give the same label (a missing label is a label of its own).
- **Fleiss' κ** over the cases with all runs, runs as raters.
- Reported per phrasing too (5 repetitions each), to separate wording from sampling.
- **Across phrasings:** among cases run in more than one phrasing, a case is unstable across phrasings if the majority labels of its phrasings differ, or one of them is a tie; share with a Wilson interval.

## 6 Level 5: opposing reasonings

- Values tragfähig = 4, eher tragfähig = 3, eher nicht tragfähig = 2, nicht tragfähig = 1; **sound** = 3 or 4. With repetitions, a side's value is the mean.
- **Court side:** *unlawful* cases → the reasoning for unlawfulness; *dismissed* → the reasoning against. **Primary metrics use these two kinds only.** Partly unlawful cases get their own row: share judged sound per side.
- **Judged sound on both sides:** share of cases where both reasonings are sound.
- **AUC:** probability that the court-side reasoning gets the higher value than the sham one (ties ½), over all case pairs of the model (Mann–Whitney); interval by bootstrap over cases.
- **Within the case:** share of cases in which the court-side reasoning gets the higher value than the sham one of the same case (ties ½); interval by bootstrap over cases. The AUC compares across cases and so mixes in how convincing each case's writing is; this measure does not. Neither knows whether the court-side reasoning reaches the outcome for the court's reasons.

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
- **Pilot:** every comparison is exploratory. McNemar p-values (conditions; each model against the *always unlawful* baseline) are reported without correction for multiplicity and support no claim.
- **Full study:** "a model beats the baseline" is tested per model by exact McNemar on its cases (model right vs baseline right, discordant pairs), with **Holm** correction over the tested models; the comparison of conditions likewise, Holm over models per pair of conditions.
- Every figure and export names the protocol tag and hash it is based on.

## 10 Knowledge acquisition (condition C)

- **Reference norms:** the case's approved norm set (the norms the admin checked against the decision's legal framework, part II "Rechtslage"), in the version valid on the Stichtag; items that are left out, procedural (VfGG, Art 139/140 B-VG) or EU law do not count. Without an approved norm set a run has no reference and is left out of recall and version correctness.
- **Identity of a norm:** short title and section as the RIS states them (blocks *Kurztitel* and *§/Artikel/Anlage*); a later part of a long document is known by its document number.
- **Reference-norm recall:** share of reference norms fetched with `hole_norm` in any version; pooled over the reference norms of a model's C runs, Wilson interval.
- **Version correctness:** among the fetched reference norms, share fetched as the same RIS document as the reference version (each version has its own document number); pooled, Wilson interval. A version the tool marked as not in force on the Stichtag is shown on the run page.
- **Search effort:** mean number of tool calls per C run, and the number of calls the limit cut off.
- **Cited:** a fetched document counts as cited if the citation check found the same document for one of the answer's citations (shown per step, no metric).

## 11 Human assessment: error categories, reviewability, time

- On each calibration item the expert also records, blind, **one error category**:
  1. right result, right reasons;
  2. right result, other reasons;
  3. wrong result, recognisable legal misunderstanding;
  4. applicant's arguments adopted uncritically;
  5. inaccurate or invented legal propositions;
  6. fluent reasoning that masks a weak legal basis;
- and **reviewability**: "Lässt sich die Begründung nachvollziehen und angreifen?" on a scale 1 (no: steps and sources stay obscure) to 4 (yes: every step can be followed and contested).
- Reported per model and condition from the first rating of each item: counts per category and the **median** reviewability. Judges do not rate these; they are human only and descriptive.
- **Time per evaluation:** from opening the rating page to submitting it (pages open longer than 12 hours are left out); median per evaluator, to plan the effort of a larger study.
