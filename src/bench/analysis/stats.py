"""Statistics for the analysis plan, in plain Python (samples are small)."""

import math
import random
from collections import Counter
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from statistics import fmean, variance

Z95 = 1.959963984540054
SEED = 20261106
RESAMPLES = 2000


@dataclass(frozen=True)
class Interval:
    estimate: float
    low: float
    high: float


def wilson(successes: int, n: int, z: float = Z95) -> Interval | None:
    """Wilson score interval for a proportion."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return Interval(p, max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact p-value on the discordant pairs (b: right only in the first, c: only
    in the second)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail: float = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def cohen_kappa(a: Sequence[Hashable], b: Sequence[Hashable]) -> float | None:
    """Unweighted κ for two raters over the same items."""
    if len(a) != len(b) or not a:
        return None
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[k] * cb[k] for k in ca) / (n * n)
    if expected == 1:
        return 1.0 if observed == 1 else None
    return (observed - expected) / (1 - expected)


def weighted_kappa(a: Sequence[float], b: Sequence[float], levels: Sequence[float]) -> float | None:
    """Linearly weighted κ for ordinal values on ``levels``."""
    if len(a) != len(b) or not a:
        return None
    k = len(levels)
    index = {v: i for i, v in enumerate(levels)}
    n = len(a)

    def w(i: int, j: int) -> float:
        return abs(i - j) / (k - 1)

    observed = sum(w(index[x], index[y]) for x, y in zip(a, b, strict=True)) / n
    pa = Counter(index[x] for x in a)
    pb = Counter(index[y] for y in b)
    expected = sum(w(i, j) * pa[i] * pb[j] for i in range(k) for j in range(k)) / (n * n)
    if expected == 0:
        return 1.0 if observed == 0 else None
    return 1 - observed / expected


def fleiss_kappa(items: Sequence[Sequence[Hashable]]) -> float | None:
    """Fleiss' κ; each item is the list of labels its raters gave (same number per item)."""
    items = [list(i) for i in items if i]
    if not items:
        return None
    m = len(items[0])
    if m < 2 or any(len(i) != m for i in items):
        return None
    n = len(items)
    totals: Counter[Hashable] = Counter()
    agreement = 0.0
    for labels in items:
        counts = Counter(labels)
        totals.update(counts)
        agreement += (sum(c * c for c in counts.values()) - m) / (m * (m - 1))
    p_bar = agreement / n
    p_e = sum((c / (n * m)) ** 2 for c in totals.values())
    if p_e == 1:
        return 1.0 if p_bar == 1 else None
    return (p_bar - p_e) / (1 - p_e)


def auc(positive: Sequence[float], negative: Sequence[float]) -> float | None:
    """Probability that a positive scores higher than a negative (ties ½)."""
    if not positive or not negative:
        return None
    wins = sum(1.0 if p > q else 0.5 if p == q else 0.0 for p in positive for q in negative)
    return wins / (len(positive) * len(negative))


def bootstrap[T](
    clusters: Sequence[T],
    statistic: Callable[[Sequence[T]], float | None],
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> Interval | None:
    """Percentile interval, resampling whole clusters (cases) with replacement."""
    estimate = statistic(clusters)
    if estimate is None or not clusters:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(resamples):
        sample = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        value = statistic(sample)
        if value is not None:
            values.append(value)
    if not values:
        return None
    values.sort()
    low = values[int(0.025 * (len(values) - 1))]
    high = values[math.ceil(0.975 * (len(values) - 1))]
    return Interval(estimate, low, high)


def ppi_mean(
    judge_all: Sequence[float], judge_cal: Sequence[float], human_cal: Sequence[float]
) -> Interval | None:
    """Prediction-powered mean: judge mean over all items, corrected by the judge–human
    difference on the calibration items (Angelopoulos et al. 2023)."""
    n_all, n = len(judge_all), len(judge_cal)
    if n_all < 2 or n < 2 or n != len(human_cal):
        return None
    diff = [j - h for j, h in zip(judge_cal, human_cal, strict=True)]
    estimate = fmean(judge_all) - fmean(diff)
    se = math.sqrt(variance(judge_all) / n_all + variance(diff) / n)
    return Interval(estimate, estimate - Z95 * se, estimate + Z95 * se)


def majority(labels: Sequence[Hashable]) -> Hashable | None:
    """The most frequent label; None on a tie or without labels."""
    if not labels:
        return None
    top = Counter(labels).most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return None
    return top[0][0]


def mean_or_none(values: Sequence[float]) -> float | None:
    return fmean(values) if values else None
