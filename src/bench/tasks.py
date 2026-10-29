"""Helper tasks: LLM jobs besides deciding a case, run by the same worker and orders.

A helper task has one prompt (``<task>.p1``), an output schema, the model role it needs,
the text it reads from the case, and what happens with a result that fits the schema.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel
from sqlmodel import Session

from bench.llm.schemas import Referenzgruende
from bench.models import CaseParse, Run
from bench.providers import Role


@dataclass(frozen=True)
class HelperTask:
    key: str
    label: str
    role: Role
    output_type: type[BaseModel]
    input_text: Callable[[CaseParse], str]
    on_done: Callable[[Session, Run], None]


def _checklist_from_run(db: Session, run: Run) -> None:
    from bench import checklists  # avoid an import cycle

    checklists.from_run(db, run)


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
}
TASK_LABELS = {"decide": "Entscheidung", **{k: t.label for k, t in HELPERS.items()}}
