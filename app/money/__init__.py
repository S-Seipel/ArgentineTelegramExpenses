"""F1 money domain (R1 bootstrap + R2 money read model).

Public surface for the cycle domain lives in :mod:`app.money.cycle_service`
and :mod:`app.money.repository`. The models in :mod:`app.money.models` are
imported for ``Base.metadata`` registration. R2 adds the read-only
``MoneyEngine``; mutation APIs for income/savings land in R3.
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
from app.money.repository import (
    DEFAULT_PAYDAY_DAY,
    FinancialCycleRepository,
    UserSettingsRepository,
)

__all__ = [
    "DEFAULT_PAYDAY_DAY",
    "CurrentCycleView",
    "CycleCatchUpLimitExceeded",
    "CycleHasPendingError",
    "CycleIncome",
    "CycleRolloverRequired",
    "CycleService",
    "CycleWindow",
    "FinancialCycle",
    "FinancialCycleRepository",
    "MoneyEngine",
    "SavingsRealization",
    "SavingsReserved",
    "SavingsTarget",
    "SnapshotVersionMismatchError",
    "UserSettings",
    "UserSettingsRepository",
    "UserSettingsService",
    "cycle_window_for",
    "payday_for",
]
