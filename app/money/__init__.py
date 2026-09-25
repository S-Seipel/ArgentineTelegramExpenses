"""F1 money domain (R1 bootstrap + R2 read engine + R3 mutations).

Public surface:

- :mod:`app.money.cycle_service` — cycle lifecycle (open / close /
  rollover). ``CycleService`` is the only writer of
  ``FinancialCycle.status``.
- :mod:`app.money.engine` — read-only ``MoneyEngine`` that produces
  ``CycleSummary`` from the live state. Pure SELECTs; never mutates.
- :mod:`app.money.money_state_service` — R3 mutations for income
  (base + extras) and savings (target / reserved / realized /
  release). Never computes ``available``; never opens/closes cycles;
  never touches Expense / FixedExpense / RecurringExpense.

The dependency arrow stays one-way:

    MoneyStateService ──► MoneyRepository (in :mod:`app.money.models`)
                                       ▲
                                       │
    MoneyEngine ────────────────────────┘ (read-only)
    CycleService ──► MoneyEngine (read-only summary inside close tx)
"""
from app.money.cycle_resolution import (
    CycleWindow,
    cycle_window_for,
    payday_for,
)
from app.money.cycle_service import (
    CurrentCycleView,
    CycleRolloverRequired,
    CycleService,
    UserSettingsService,
)
from app.money.engine import (
    CycleHasPendingError,
    CycleCatchUpLimitExceeded,
    MoneyEngine,
    SnapshotVersionMismatchError,
)
from app.money.models import (
    CycleIncome,
    FinancialCycle,
    SavingsRealization,
    SavingsReserved,
    SavingsTarget,
    UserSettings,
)
from app.money.money_state_service import (
    CycleClosedError,
    CycleNotFoundError,
    CycleOwnershipMismatchError,
    ExtraIncomeNotFoundError,
    InvalidAmountError,
    InvalidSourceKeyError,
    InvalidTargetValueError,
    MoneyStateError,
    MoneyStateService,
    SavingsRealizationNotFoundError,
    TargetXorViolationError,
)
from app.money.repository import (
    DEFAULT_PAYDAY_DAY,
    FinancialCycleRepository,
    UserSettingsRepository,
)

__all__ = [
    "DEFAULT_PAYDAY_DAY",
    "CurrentCycleView",
    "CycleCatchUpLimitExceeded",
    "CycleClosedError",
    "CycleHasPendingError",
    "CycleIncome",
    "CycleNotFoundError",
    "CycleOwnershipMismatchError",
    "CycleRolloverRequired",
    "CycleService",
    "CycleWindow",
    "ExtraIncomeNotFoundError",
    "FinancialCycle",
    "FinancialCycleRepository",
    "InvalidAmountError",
    "InvalidSourceKeyError",
    "InvalidTargetValueError",
    "MoneyEngine",
    "MoneyStateError",
    "MoneyStateService",
    "SavingsRealization",
    "SavingsRealizationNotFoundError",
    "SavingsReserved",
    "SavingsTarget",
    "SnapshotVersionMismatchError",
    "TargetXorViolationError",
    "UserSettings",
    "UserSettingsRepository",
    "UserSettingsService",
    "cycle_window_for",
    "payday_for",
]
