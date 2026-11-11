"""Calibration queue: the legal expert rates judged answers blind, on the judges' criteria.

- **Draw** (admin): decide runs that at least one judge has scored, stratified by
  condition and model (round robin over the strata, within a stratum the least used case
  first), in random order with a stored seed. The checklist version is the one the
  judges used. The first ``double`` picks of a draw (spread over the strata) are also
  rated by a second person, as an upper bound of agreement.
- **Queue** (expert): an item needs one rating, a double item two from different people.
  A person sees the items they can still rate, in item order; the rating page shows the
  case, the reference reasons, the answer and the citation check, as the judges see
  them, but never model, provider, condition or the judges' scores.
- Items and ratings are immutable (trigger). Agreement metrics follow in stage 6.
"""

import random
from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy import func
from sqlmodel import Session, col, select

from bench import citations
from bench.models import (
    CalibrationItem,
    CalibrationRating,
    JobStatus,
    JudgeScore,
    ReferenceChecklist,
    Run,
    audit,
    utcnow,
)

REASON_VALUES = ("ja", "teilweise", "nein")
CITATION_VALUES = ("ja", "teilweise", "nein", "nicht_beurteilbar")
DEFAULT_SIZE = 40
DEFAULT_DOUBLE = 10


def stratum_of(run: Run) -> str:
    return f"{run.condition} · {run.model_key}"


def candidates(db: Session, subset: str = "alle") -> dict[str, list[tuple[Run, int]]]:
    """Judged decide runs not yet drawn, with the checklist the judges used, per stratum.

    A run whose judges used different checklist versions is left out. ``subset`` limits
    the cells as for judge orders (``judges.SUBSETS``), e.g. phrasing 1, repetition 1:
    one answer per case, model and condition.
    """
    from bench.judges import SUBSETS  # avoid an import cycle

    max_phrasing, max_repetition = SUBSETS[subset]
    drawn = set(db.exec(select(CalibrationItem.target_run_id)).all())
    used: defaultdict[int, set[int]] = defaultdict(set)
    for target_id, checklist_id in db.exec(
        select(JudgeScore.target_run_id, JudgeScore.checklist_id)
    ).all():
        used[target_id].add(checklist_id)
    ids = [i for i, c in used.items() if len(c) == 1 and i not in drawn]
    out: defaultdict[str, list[tuple[Run, int]]] = defaultdict(list)
    query = select(Run).where(
        col(Run.id).in_(ids), Run.task == "decide", Run.status == JobStatus.DONE
    )
    if max_phrasing is not None:
        query = query.where(Run.phrasing <= max_phrasing)
    if max_repetition is not None:
        query = query.where(Run.repetition <= max_repetition)
    for run in db.exec(query.order_by(col(Run.id))).all():
        out[stratum_of(run)].append((run, next(iter(used[run.id or 0]))))
    return dict(out)


def pick(
    strata: dict[str, list[tuple[Run, int]]],
    n: int,
    rng: random.Random,
    case_use: Counter[int] | None = None,
) -> list[tuple[Run, int]]:
    """Round robin over the strata; in each, the run of the least used case (ties random)."""
    case_use = Counter() if case_use is None else Counter(case_use)
    pools = {k: list(v) for k, v in sorted(strata.items())}
    for pool in pools.values():
        rng.shuffle(pool)
    picked: list[tuple[Run, int]] = []
    while len(picked) < n and any(pools.values()):
        for key in list(pools):
            pool = pools[key]
            if not pool or len(picked) >= n:
                continue
            best = min(range(len(pool)), key=lambda i: case_use[pool[i][0].case_id])
            run, checklist_id = pool.pop(best)
            case_use[run.case_id] += 1
            picked.append((run, checklist_id))
    return picked


def draw(
    db: Session,
    n: int,
    double: int,
    user_id: int | None,
    seed: int | None = None,
    subset: str = "alle",
) -> list[CalibrationItem]:
    """Add ``n`` items to the queue, ``double`` of them for a second rating."""
    if n < 1:
        raise ValueError("Mindestens eine Antwort ziehen")
    if not 0 <= double <= n:
        raise ValueError("Zweitbewertungen: zwischen 0 und der Zahl der Antworten")
    strata = candidates(db, subset)
    available = sum(len(v) for v in strata.values())
    if available == 0:
        raise ValueError("Keine bewerteten Antworten, die noch nicht gezogen sind")
    if n > available:
        raise ValueError(f"Nur {available} bewertete Antworten verfügbar")
    seed = random.SystemRandom().randrange(2**31) if seed is None else seed
    rng = random.Random(seed)
    case_use = Counter(db.exec(select(CalibrationItem.case_id)).all())
    picked = pick(strata, n, rng, case_use)
    flagged = {run.id for run, _ in picked[:double]}
    last = db.exec(select(func.max(CalibrationItem.draw))).one()
    number = (last or 0) + 1
    order = list(picked)
    rng.shuffle(order)  # the id says nothing about stratum or second rating
    items = []
    for run, checklist_id in order:
        item = CalibrationItem(
            target_run_id=run.id or 0,
            case_id=run.case_id,
            checklist_id=checklist_id,
            stratum=stratum_of(run),
            double=run.id in flagged,
            draw=number,
            seed=seed,
            created_by=user_id,
        )
        db.add(item)
        items.append(item)
    db.flush()
    audit(
        db,
        "calibration.draw",
        user_id=user_id,
        obj=f"calibration_draw:{number}",
        detail={"items": len(items), "double": len(flagged), "seed": seed, "subset": subset},
    )
    return items


# ── queue ───────────────────────────────────────────────────────────────────────


def _ratings(db: Session) -> defaultdict[int, list[CalibrationRating]]:
    out: defaultdict[int, list[CalibrationRating]] = defaultdict(list)
    for r in db.exec(select(CalibrationRating).order_by(col(CalibrationRating.id))).all():
        out[r.item_id].append(r)
    return out


def needed(item: CalibrationItem) -> int:
    return 2 if item.double else 1


def can_rate(item: CalibrationItem, ratings: list[CalibrationRating], user_id: int) -> bool:
    return len(ratings) < needed(item) and all(r.rater_id != user_id for r in ratings)


def queue(db: Session, user_id: int) -> list[CalibrationItem]:
    """The items this person can still rate, in item order."""
    ratings = _ratings(db)
    items = db.exec(select(CalibrationItem).order_by(col(CalibrationItem.id))).all()
    return [i for i in items if can_rate(i, ratings[i.id or 0], user_id)]


def rated_by(db: Session, user_id: int) -> list[CalibrationRating]:
    return list(
        db.exec(
            select(CalibrationRating)
            .where(CalibrationRating.rater_id == user_id)
            .order_by(col(CalibrationRating.id))
        ).all()
    )


@dataclass
class Progress:
    items: int
    double: int
    ratings_needed: int
    ratings_done: int
    open_items: int
    by_stratum: dict[str, tuple[int, int]]  # items, items fully rated
    draws: list[tuple[int, int, int]]  # number, items, seed


def progress(db: Session) -> Progress:
    """For the admin: what is drawn and rated, per stratum."""
    ratings = _ratings(db)
    items = db.exec(select(CalibrationItem).order_by(col(CalibrationItem.id))).all()
    by_stratum: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    draws: dict[int, list[int]] = {}
    done = 0
    for item in items:
        n = min(len(ratings[item.id or 0]), needed(item))
        done += n
        complete = n == needed(item)
        by_stratum[item.stratum][0] += 1
        by_stratum[item.stratum][1] += int(complete)
        draws.setdefault(item.draw, [0, item.seed])[0] += 1
    return Progress(
        items=len(items),
        double=sum(1 for i in items if i.double),
        ratings_needed=sum(needed(i) for i in items),
        ratings_done=done,
        open_items=sum(1 for i in items if len(ratings[i.id or 0]) < needed(i)),
        by_stratum={k: (v[0], v[1]) for k, v in sorted(by_stratum.items())},
        draws=[(k, v[0], v[1]) for k, v in sorted(draws.items())],
    )


# ── rating ──────────────────────────────────────────────────────────────────────


def rate(
    db: Session,
    item: CalibrationItem,
    user_id: int,
    reasons: list[tuple[str, str]],
    cited: list[tuple[str, str]],
    note: str = "",
) -> CalibrationRating:
    """Store one rating; ``reasons`` and ``cited`` are (value, evidence/note) in list order."""
    ratings = list(
        db.exec(select(CalibrationRating).where(CalibrationRating.item_id == item.id)).all()
    )
    if any(r.rater_id == user_id for r in ratings):
        raise ValueError("Diese Antwort haben Sie schon bewertet")
    if len(ratings) >= needed(item):
        raise ValueError("Diese Antwort ist schon bewertet")
    checklist = db.get(ReferenceChecklist, item.checklist_id)
    target = db.get(Run, item.target_run_id)
    assert checklist is not None and target is not None
    n, m = len(checklist.items), len(citations.citations_of(target))
    if len(reasons) != n or any(v not in REASON_VALUES for v, _ in reasons):
        raise ValueError("Bitte jeden Referenzgrund bewerten")
    if len(cited) != m or any(v not in CITATION_VALUES for v, _ in cited):
        raise ValueError("Bitte jedes Zitat bewerten")
    rating = CalibrationRating(
        item_id=item.id or 0,
        rater_id=user_id,
        reasons=[
            {"nr": i, "value": v, "evidence": e.strip()} for i, (v, e) in enumerate(reasons, 1)
        ],
        citations=[{"nr": i, "value": v, "note": t.strip()} for i, (v, t) in enumerate(cited, 1)],
        note=note.strip() or None,
        created_at=utcnow(),
    )
    db.add(rating)
    db.flush()
    audit(
        db,
        "calibration.rate",
        user_id=user_id,
        obj=f"calibration_item:{item.id}",
        detail={"rating": rating.id},
    )
    return rating
