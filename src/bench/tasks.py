"""Helper tasks: LLM jobs besides deciding a case, run by the same worker and orders.

A helper task has one prompt (``<task>.p1``), an output schema, the model role it needs,
the text it reads from the case, and what happens with a result that fits the schema.
A task that reads more than the case (the judge reads a run) builds its own messages and
may reject an answer that fits the schema but not the job.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel
from sqlmodel import Session

from bench.llm.schemas import (
    Bewertung,
    Einschaetzung,
    Erinnerung,
    Gegenbegruendungen,
    Referenzgruende,
    Wiedererkennung,
)
from bench.models import CaseParse, Job, Order, Run
from bench.providers import Role


@dataclass(frozen=True)
class HelperTask:
    key: str
    label: str
    role: Role
    output_type: type[BaseModel]
    input_text: Callable[[CaseParse], str]  # also the basis of the cost check
    on_done: Callable[[Session, Run], None]
    messages: Callable[[Session, Order, Job, str, CaseParse], list[dict[str, str]]] | None = None
    validate: Callable[[Session, Order, Job, BaseModel], str | None] | None = None


def _checklist_from_run(db: Session, run: Run) -> None:
    from bench import checklists  # avoid an import cycle

    checklists.from_run(db, run)


def _judge_messages(
    db: Session, order: Order, job: Job, prompt: str, parse: CaseParse
) -> list[dict[str, str]]:
    from bench import judges

    return judges.messages(db, order, job, prompt, parse)


def _judge_validate(db: Session, order: Order, job: Job, output: BaseModel) -> str | None:
    from bench import judges

    return judges.validate(db, order, job, output)


def _score_from_run(db: Session, run: Run) -> None:
    from bench import judges

    judges.from_run(db, run)


def _pair_validate(db: Session, order: Order, job: Job, output: BaseModel) -> str | None:
    from bench import opposing

    return opposing.validate_pair(db, order, job, output)


def _pair_from_run(db: Session, run: Run) -> None:
    from bench import opposing

    opposing.from_writer_run(db, run)


def _assess_messages(
    db: Session, order: Order, job: Job, prompt: str, parse: CaseParse
) -> list[dict[str, str]]:
    from bench import opposing

    return opposing.messages(db, order, job, prompt, parse)


def _assessment_from_run(db: Session, run: Run) -> None:
    from bench import opposing

    opposing.from_assess_run(db, run)


def _probe_messages(
    db: Session, order: Order, job: Job, prompt: str, parse: CaseParse
) -> list[dict[str, str]]:
    from bench import contamination

    return contamination.probe_messages(db, order, job, prompt, parse)


def _probe_from_run(db: Session, run: Run) -> None:
    from bench import contamination

    contamination.from_probe_run(db, run)


def _probe_judge_messages(
    db: Session, order: Order, job: Job, prompt: str, parse: CaseParse
) -> list[dict[str, str]]:
    from bench import contamination

    return contamination.judge_messages(db, order, job, prompt, parse)


def _probe_judge_validate(db: Session, order: Order, job: Job, output: BaseModel) -> str | None:
    from bench import contamination

    return contamination.judge_validate(db, order, job, output)


def _probe_score_from_run(db: Session, run: Run) -> None:
    from bench import contamination

    contamination.from_judge_run(db, run)


HELPERS: dict[str, HelperTask] = {
    "extract_reasons": HelperTask(
        key="extract_reasons",
        label="Referenzgründe extrahieren",
        role=Role.EXTRACTOR,
        output_type=Referenzgruende,
        # headnote, operative part and considerations: the reference part, never a model input
        input_text=lambda parse: parse.reference_text,
        on_done=_checklist_from_run,
    ),
    "judge": HelperTask(
        key="judge",
        label="Bewertung durch Richtermodelle",
        role=Role.JUDGE,
        output_type=Bewertung,
        input_text=lambda parse: parse.input_text,  # plus checklist, answer and citations
        on_done=_score_from_run,
        messages=_judge_messages,
        validate=_judge_validate,
    ),
    "write_opposing": HelperTask(
        key="write_opposing",
        label="Gegenbegründungen schreiben",
        role=Role.WRITER,
        output_type=Gegenbegruendungen,
        input_text=lambda parse: parse.input_text,  # the case input only, never the decision
        on_done=_pair_from_run,
        validate=_pair_validate,
    ),
    "assess": HelperTask(
        key="assess",
        label="Einschätzung von Gegenbegründungen",
        role=Role.TESTED,
        output_type=Einschaetzung,
        input_text=lambda parse: parse.input_text,  # plus one reasoning
        on_done=_assessment_from_run,
        messages=_assess_messages,
    ),
    "probe": HelperTask(
        key="probe",
        label="Kontaminationsprobe",
        role=Role.TESTED,
        output_type=Erinnerung,
        input_text=lambda parse: "",  # date and case number only, never the case input
        on_done=_probe_from_run,
        messages=_probe_messages,
    ),
    "probe_judge": HelperTask(
        key="probe_judge",
        label="Bewertung der Kontaminationsprobe",
        role=Role.JUDGE,
        output_type=Wiedererkennung,
        input_text=lambda parse: "",  # reference reasons and recalled reasons only
        on_done=_probe_score_from_run,
        messages=_probe_judge_messages,
        validate=_probe_judge_validate,
    ),
}
TASK_LABELS = {"decide": "Entscheidung", **{k: t.label for k, t in HELPERS.items()}}
