from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.database.database import session_scope
from app.expenses.repository import ExpenseRepository
from app.expenses.service import ExpenseDraft, ExpenseService
from app.budgets.repository import BudgetRepository
from app.budgets.service import BudgetDraft, BudgetService, BudgetValidationError


def _cat_id(name: str) -> int:
    with session_scope() as s:
        from app.categories.models import Category
        return s.query(Category).filter(Category.name == name).one().id


def _expense(name: str, amount, category: str, today: date) -> None:
    with session_scope() as s:
        ExpenseService(ExpenseRepository(s)).register_many(
            user_id=1,
            drafts=[
                ExpenseDraft(
                    name=name,
                    amount=Decimal(str(amount)),
                    currency="ARS",
                    category=category,
                    expense_date=today,
                    confidence=Decimal("1"),
                )
            ],
            original_message="seed",
        )


def test_upsert_creates_new_budget(in_memory_db):
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        obj, created = service.upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Comida", monthly_limit=Decimal("50000"), currency=None
            ),
        )
    assert created is True
    assert obj.monthly_limit == Decimal("50000.00")
    assert obj.currency == "ARS"


def test_upsert_updates_existing_active(in_memory_db):
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        service.upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Comida", monthly_limit=Decimal("50000"), currency=None
            ),
        )
        obj, created = service.upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Comida", monthly_limit=Decimal("80000"), currency=None
            ),
        )
    assert created is False
    assert obj.monthly_limit == Decimal("80000.00")


def test_invalid_amount_rejected(in_memory_db):
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        try:
            service.upsert_from_draft(
                user_id=1,
                draft=BudgetDraft(
                    category="Comida", monthly_limit=Decimal("0"), currency=None
                ),
            )
        except BudgetValidationError:
            pass
        else:
            raise AssertionError("expected validation error")


def test_unknown_currency_rejected(in_memory_db):
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        try:
            service.upsert_from_draft(
                user_id=1,
                draft=BudgetDraft(
                    category="Comida",
                    monthly_limit=Decimal("1000"),
                    currency="XXX",
                ),
            )
        except BudgetValidationError:
            pass
        else:
            raise AssertionError("expected validation error")


def test_evaluate_no_budget_returns_none(in_memory_db):
    today = date(2026, 8, 21)
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        alert = service.evaluate_after_expense(
            user_id=1,
            category_id=_cat_id("Comida"),
            currency="ARS",
            spent_in_month=Decimal("100000"),
            today=today,
        )
    assert alert is None


def test_evaluate_warning_at_80_percent(in_memory_db):
    today = date(2026, 8, 21)
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        service.upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Comida", monthly_limit=Decimal("100000"), currency=None
            ),
        )
        alert = service.evaluate_after_expense(
            user_id=1,
            category_id=_cat_id("Comida"),
            currency="ARS",
            spent_in_month=Decimal("85000"),
            today=today,
        )
    assert alert is not None
    assert alert.level == "warning"
    assert alert.percent == Decimal("85000") / Decimal("100000")
    assert alert.category_name == "Comida"


def test_evaluate_exceeded_at_100_percent(in_memory_db):
    today = date(2026, 8, 21)
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        service.upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Comida", monthly_limit=Decimal("50000"), currency=None
            ),
        )
        alert = service.evaluate_after_expense(
            user_id=1,
            category_id=_cat_id("Comida"),
            currency="ARS",
            spent_in_month=Decimal("60000"),
            today=today,
        )
    assert alert is not None
    assert alert.level == "exceeded"


def test_evaluate_does_not_re_alert_same_month(in_memory_db):
    today = date(2026, 8, 21)
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        service.upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Comida", monthly_limit=Decimal("100000"), currency=None
            ),
        )
        # First call: alerts.
        first = service.evaluate_after_expense(
            user_id=1,
            category_id=_cat_id("Comida"),
            currency="ARS",
            spent_in_month=Decimal("85000"),
            today=today,
        )
        # Second call same month: silent.
        second = service.evaluate_after_expense(
            user_id=1,
            category_id=_cat_id("Comida"),
            currency="ARS",
            spent_in_month=Decimal("90000"),
            today=today,
        )
    assert first is not None
    assert second is None


def test_evaluate_re_alerts_next_month(in_memory_db):
    from app.budgets.models import Budget
    from datetime import datetime
    from zoneinfo import ZoneInfo

    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        service.upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Comida", monthly_limit=Decimal("100000"), currency=None
            ),
        )
        # Force last_alerted_at to a date in a previous month so we can
        # simulate the new-month re-alert without relying on real time.
        budget = s.query(Budget).filter(Budget.telegram_user_id == 1).one()
        budget.last_alerted_at = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("UTC"))

    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        new_month = service.evaluate_after_expense(
            user_id=1,
            category_id=_cat_id("Comida"),
            currency="ARS",
            spent_in_month=Decimal("85000"),
            today=date(2026, 9, 5),
        )
    assert new_month is not None


def test_evaluate_no_alert_below_80_percent(in_memory_db):
    today = date(2026, 8, 21)
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        service.upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Comida", monthly_limit=Decimal("100000"), currency=None
            ),
        )
        alert = service.evaluate_after_expense(
            user_id=1,
            category_id=_cat_id("Comida"),
            currency="ARS",
            spent_in_month=Decimal("50000"),
            today=today,
        )
    assert alert is None


def test_delete_deactivates_budget(in_memory_db):
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        obj, _ = service.upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Comida", monthly_limit=Decimal("100000"), currency=None
            ),
        )
        bid = obj.id
        assert service.delete(1, bid) is True
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        active = service.list(1)
        all_b = service.list.__self__.repo.list_for_user(1, include_inactive=True)
    assert active == []
    assert len(all_b) == 1
    assert all_b[0].is_active is False


def test_budget_with_real_expenses_alert_path(in_memory_db):
    """End-to-end: register expenses, configure budget, verify alert."""
    today = date(2026, 8, 21)
    _expense("Café", 5000, "Café", today)
    _expense("Almuerzo", 15000, "Restaurante", today)
    _expense("Pizza", 20000, "Restaurante", today)
    _expense("Bondi", 5000, "Transporte público", today)

    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        service.upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Restaurante",
                monthly_limit=Decimal("30000"),
                currency=None,
            ),
        )
        # Spent in Restaurante = 15000 + 20000 = 35000 → 117% → exceeded
        alert = service.evaluate_after_expense(
            user_id=1,
            category_id=_cat_id("Restaurante"),
            currency="ARS",
            spent_in_month=Decimal("35000"),
            today=today,
        )
    assert alert is not None
    assert alert.level == "exceeded"


def test_reactivate_paused_budget(in_memory_db):
    with session_scope() as s:
        service = BudgetService(BudgetRepository(s))
        obj, _ = service.upsert_from_draft(
            user_id=1,
            draft=BudgetDraft(
                category="Comida", monthly_limit=Decimal("50000"), currency=None
            ),
        )
        bid = obj.id
        service.deactivate(1, bid)
        assert service.list(1) == []
        service.reactivate(1, bid)
        assert len(service.list(1)) == 1
