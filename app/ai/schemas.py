from __future__ import annotations

from datetime import date as _date
from decimal import Decimal
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field, field_validator, model_validator


IntentType = Literal[
    "register_expense",
    "register_recurring",
    "query",
    "greeting",
    "help",
    "cancel",
    "confirm",
    "unknown",
]

QueryPeriod = Literal["today", "yesterday", "week", "month", "all", "last_n_days"]

SortOrder = Literal["amount_desc", "amount_asc", "date_desc", "date_asc"]


class AIError(Exception):
    """Raised when the AI cannot produce a usable response."""


class AIUnavailable(AIError):
    """Raised when the AI provider is unreachable or times out."""


class ExpenseExtraction(BaseModel):
    """Single expense extraction entry.

    The AI returns one of these per detected expense. The business layer
    decides whether to persist or to ask for clarification.
    """

    name: str | None = Field(default=None)
    amount: Decimal | None = Field(default=None)
    currency: str | None = Field(default=None)
    category: str | None = Field(default=None)
    date: _date | None = Field(default=None)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None)

    @field_validator("amount", mode="before")
    @classmethod
    def _normalize_amount(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, Decimal):
            return v
        try:
            return Decimal(str(v))
        except Exception:
            return None

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        s = str(v).strip().upper()
        if s in {"$", "AR$"}:
            return "ARS"
        if s in {"U$S", "U$D"}:
            return "USD"
        return s[:8]

    @field_validator("date", mode="before")
    @classmethod
    def _normalize_date(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, _date):
            return v
        text = str(v).strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return _date.fromisoformat(text) if fmt == "%Y-%m-%d" else _date(
                    *(_parse(text, fmt))
                )
            except Exception:
                continue
        return None

    @model_validator(mode="after")
    def _check_clarification(self) -> "ExpenseExtraction":
        if self.needs_clarification and not self.clarification_question:
            self.clarification_question = (
                "¿Podés darme más detalles sobre el gasto?"
            )
        return self


def _parse(text: str, fmt: str) -> tuple[int, ...]:
    from datetime import datetime as _dt

    return _dt.strptime(text, fmt).timetuple()[:3]


class QueryIntent(BaseModel):
    period: QueryPeriod | None = None
    days: int | None = Field(default=None, ge=1, le=365)
    category: str | None = None
    currency: str | None = None
    limit: int | None = Field(default=None, ge=1, le=200)
    order_by: SortOrder | None = None
    raw_period_text: str | None = None

    @field_validator("category", "currency", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        if v is None:
            return None
        text = str(v).strip()
        return text or None


class Intent(BaseModel):
    type: IntentType
    expenses: list[ExpenseExtraction] = Field(default_factory=list)
    recurring: RecurringExtraction | None = None
    query: QueryIntent | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    rationale: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "Intent":
        if self.type == "register_expense" and not self.expenses:
            self.expenses = [
                ExpenseExtraction(
                    name=None,
                    amount=None,
                    currency=None,
                    category=None,
                    date=None,
                    confidence=0.0,
                    needs_clarification=True,
                    clarification_question="¿Qué gasto querés registrar?",
                )
            ]
        if self.type == "register_recurring" and self.recurring is None:
            self.recurring = RecurringExtraction(
                needs_clarification=True,
                clarification_question=(
                    "¿Qué gasto recurrente querés registrar? "
                    "Ej: 'gasto 30k en Netflix cada mes el día 15'."
                ),
            )
        if self.type == "query" and self.query is None:
            self.query = QueryIntent(period="today")
        return self


class ExtractedExpense:
    """Lightweight internal DTO consumed by the expense service."""

    def __init__(self, model: ExpenseExtraction) -> None:
        self.name = model.name
        self.amount = model.amount
        self.currency = (model.currency or "ARS").upper()
        self.category = model.category or "Otros"
        self.date = model.date
        self.confidence = Decimal(str(model.confidence))
        self.needs_clarification = model.needs_clarification
        self.clarification_question = model.clarification_question


class RecurringExtraction(BaseModel):
    """Single recurring expense template."""

    name: str | None = Field(default=None)
    amount: Decimal | None = Field(default=None)
    currency: str | None = Field(default=None)
    category: str | None = Field(default=None)
    frequency: Literal["monthly", "yearly"] | None = Field(default=None)
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    month_of_year: int | None = Field(default=None, ge=1, le=12)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None)

    @field_validator("amount", mode="before")
    @classmethod
    def _normalize_amount(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, Decimal):
            return v
        try:
            return Decimal(str(v))
        except Exception:
            return None

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        s = str(v).strip().upper()
        if s in {"$", "AR$"}:
            return "ARS"
        if s in {"U$S", "U$D"}:
            return "USD"
        return s[:8]

    @model_validator(mode="after")
    def _check_clarification(self) -> "RecurringExtraction":
        if self.needs_clarification and not self.clarification_question:
            self.clarification_question = (
                "¿Podés darme más detalles sobre el gasto recurrente?"
            )
        if (
            self.frequency == "yearly"
            and self.month_of_year is None
            and not self.needs_clarification
        ):
            self.needs_clarification = True
            self.clarification_question = (
                "¿En qué mes del año vence? (1=enero, 12=diciembre)"
            )
        return self


class ParsedMessage:
    """Top-level result from the AI service."""

    def __init__(
        self,
        intent: Intent,
        extracted: Sequence[ExtractedExpense] | None = None,
    ) -> None:
        self.intent = intent
        self.extracted: list[ExtractedExpense] = list(extracted or [])
        if not self.extracted and intent.expenses:
            self.extended_extracted_from_model(intent)
        self.recurring = intent.recurring

    def extended_extracted_from_model(self, intent: Intent) -> None:
        self.extracted = [ExtractedExpense(e) for e in intent.expenses]


def parse_ai_response(raw: Any) -> Intent:
    """Validate an arbitrary payload coming from the model."""
    if raw is None:
        raise AIError("Empty AI response")
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except Exception as exc:
            raise AIError(f"AI returned non-JSON response: {exc}") from exc
    if not isinstance(raw, dict):
        raise AIError("AI response must be a JSON object")
    try:
        return Intent.model_validate(raw)
    except Exception as exc:
        raise AIError(f"AI response failed schema validation: {exc}") from exc
