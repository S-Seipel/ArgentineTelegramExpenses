"""Centralized model registration.

Every SQLAlchemy model that should appear in ``Base.metadata`` is imported
here exactly once. Both ``app.database.database.init_db`` and
``alembic/env.py`` call ``register_all_models`` so production metadata and
migrations never silently diverge.
"""
from __future__ import annotations


def register_all_models() -> None:
    # Import side-effects: each module attaches its mapped classes to
    # ``Base.metadata`` on import.
    from app.categories.models import Category  # noqa: F401, F811
    from app.expenses.models import Expense  # noqa: F401, F811
    from app.recurring.models import RecurringExpense  # noqa: F401, F811
    from app.budgets.models import Budget  # noqa: F401, F811
    from app.fixed_expenses.models import (  # noqa: F401, F811
        FixedExpense,
        FixedExpensePayment,
        MonthlyBudget,
    )
