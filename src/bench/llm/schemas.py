"""Output schemas the models fill. Field names and descriptions are German (the models see
them as JSON schema); values are mapped to the outcome labels used everywhere else."""

from typing import Literal

from pydantic import BaseModel, Field

from bench.ris.outcome import CaseOutcome, Outcome, OutcomeItem, case_outcome

ERGEBNIS = {
    "aufgehoben": Outcome.ANNULLED,
    "war_gesetzwidrig": Outcome.WAS_UNLAWFUL,
    "abgewiesen": Outcome.DISMISSED,
    "zurueckgewiesen": Outcome.REJECTED,
    "eingestellt": Outcome.DISCONTINUED,
}


class Spruchpunkt(BaseModel):
    bestimmung: str = Field(
        description="Die angefochtene Bestimmung oder Verordnung, über die entschieden wird"
    )
    antrag: Literal["hauptantrag", "eventualantrag"] = Field(
        description="Ob über den Hauptantrag oder einen Eventualantrag entschieden wird"
    )
    ergebnis: Literal[
        "aufgehoben", "war_gesetzwidrig", "abgewiesen", "zurueckgewiesen", "eingestellt"
    ]


class Zitat(BaseModel):
    text: str = Field(description="Das Zitat so, wie es in der Begründung steht")
    art: Literal["norm", "entscheidung", "literatur"]


class Entscheidung(BaseModel):
    """Die Entscheidung des Verfassungsgerichtshofes über den Antrag."""

    spruch: list[Spruchpunkt] = Field(min_length=1, description="Ein Punkt je Ausspruch")
    tragende_gruende: list[str] = Field(
        min_length=1, description="Die Gründe, die die Entscheidung tragen, je ein Satz"
    )
    begruendung: str = Field(description="Die Begründung der Entscheidung")
    zitate: list[Zitat] = Field(
        description="Alle in der Begründung zitierten Normen, Entscheidungen und Literatur"
    )

    def outcome(self) -> CaseOutcome | None:
        items = [OutcomeItem("", p.bestimmung, ERGEBNIS[p.ergebnis]) for p in self.spruch]
        return case_outcome(items)


# Text for models without JSON-schema support (output_mode: prompted). Versioned with the code.
PROMPTED_OUTPUT_TEMPLATE = (
    "Antworte ausschließlich mit einem einzigen JSON-Objekt ohne weiteren Text, "
    "das diesem JSON-Schema entspricht:\n\n{schema}"
)


class TragenderGrund(BaseModel):
    text: str = Field(description="Der Grund in einem Satz, so konkret wie in der Entscheidung")
    randzahlen: list[str] = Field(
        default_factory=list, description="Randzahlen der Erwägungen, in denen der Grund steht"
    )


class Referenzgruende(BaseModel):
    """Die tragenden Gründe einer Entscheidung des Verfassungsgerichtshofes."""

    gruende: list[TragenderGrund] = Field(min_length=1, max_length=8)


class GrundBewertung(BaseModel):
    nr: int = Field(description="Nummer des Referenzgrundes")
    enthalten: Literal["ja", "teilweise", "nein"] = Field(
        description="Ob die Antwort den Referenzgrund enthält"
    )
    beleg: str = Field(description="Die Stelle der Antwort, kurz wiedergegeben; leer bei „nein“")


class ZitatBewertung(BaseModel):
    nr: int = Field(description="Nummer des Zitats")
    traegt: Literal["ja", "teilweise", "nein", "nicht_beurteilbar"] = Field(
        description="Ob die Quelle die Aussage trägt, für die sie zitiert wird"
    )
    begruendung: str = Field(description="Kurz, worauf sich die Bewertung stützt")


class Bewertung(BaseModel):
    """Die Bewertung einer Antwort: Referenzgründe und Zitate, je mit ihrer Nummer."""

    gruende: list[GrundBewertung] = Field(description="Ein Eintrag je Referenzgrund")
    zitate: list[ZitatBewertung] = Field(description="Ein Eintrag je Zitat; leer ohne Zitate")


class Gegenbegruendungen(BaseModel):
    """Zwei gleich lange Begründungen zum selben Antrag mit entgegengesetztem Ergebnis."""

    gesetzwidrig: str = Field(
        description="Begründung, warum die angefochtenen Bestimmungen gesetzwidrig sind"
    )
    gesetzmaessig: str = Field(
        description="Begründung, warum die angefochtenen Bestimmungen nicht gesetzwidrig sind"
    )


class Einschaetzung(BaseModel):
    """Die Einschätzung einer Begründung."""

    begruendung: str = Field(description="Kurz, worauf sich die Einschätzung stützt")
    urteil: Literal[
        "tragfaehig", "eher_tragfaehig", "eher_nicht_tragfaehig", "nicht_tragfaehig"
    ] = Field(description="Ob die Begründung rechtlich tragfähig ist")


class Erinnerung(BaseModel):
    """Was das Modell über eine Entscheidung weiß, nur aus Datum und Geschäftszahl."""

    bekannt: Literal["ja", "unsicher", "nein"] = Field(description="Ob du die Entscheidung kennst")
    ergebnis: Literal[
        "gesetzwidrig", "teilweise_gesetzwidrig", "abgewiesen", "zurueckgewiesen", "unbekannt"
    ] = Field(description="Das Ergebnis der Entscheidung; „unbekannt“, wenn du es nicht weißt")
    gruende: list[str] = Field(
        default_factory=list,
        description="Die tragenden Gründe, an die du dich erinnerst, je ein Satz; leer, wenn keine",
    )


class Wiedererkennung(BaseModel):
    """Welche Referenzgründe die erinnerten Gründe enthalten."""

    gruende: list[GrundBewertung] = Field(description="Ein Eintrag je Referenzgrund")
