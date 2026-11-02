"""Independent financial cycles and payday settings per currency.

Revision ID: 0017_currency_cycles
Revises: 0016_legacy_monthly_budget

Historical snapshots and their money rows are never rewritten or split.
An open mixed-currency cycle must first be closed with the previous app;
the preflight fails before changing any schema or association. Active
templates and historical carry-over are included conservatively so an
upgrade cannot silently hide an outstanding currency.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0017_currency_cycles"
down_revision = "0016_legacy_monthly_budget"
branch_labels = None
depends_on = None

_RULE_CHECK = (
    "(pay_rule = 'fixed_day' AND payday_weekday IS NULL) OR "
    "(pay_rule = 'first_weekday' AND payday_weekday IS NOT NULL "
    "AND payday_weekday BETWEEN 0 AND 6)"
)
_CURRENCY_CHECK = "currency = upper(trim(currency)) AND length(currency) BETWEEN 1 AND 8"
_MONEY_FIELDS = (
    "income", "extra", "gross_income", "savings_target", "savings_reserved",
    "savings_realized", "savings_committed", "realized_expenses",
    "pending_fixed_obligations", "pending_recurring_obligations",
    "pending_reserved_obligations", "available_to_spend", "daily_safe_to_spend",
)


def _add_currency(currencies: set[str], value, *, strict: bool) -> None:
    if not isinstance(value, str):
        if strict:
            raise RuntimeError("An open financial cycle contains invalid currency data; repair it before upgrading.")
        return
    normalized = value.strip().upper()
    if not normalized or len(normalized) > 8 or (strict and normalized != value):
        if strict:
            raise RuntimeError("An open financial cycle contains noncanonical currency data; repair it before upgrading.")
        return
    currencies.add(normalized)


def _snapshot_currencies(snapshot, *, carry_only: bool = False) -> set[str]:
    currencies: set[str] = set()
    if not isinstance(snapshot, dict):
        return currencies
    if not carry_only:
        money = snapshot.get("money")
        if isinstance(money, dict):
            for field in _MONEY_FIELDS:
                values = money.get(field)
                if isinstance(values, dict):
                    for value in values:
                        _add_currency(currencies, value, strict=False)
    for entry in snapshot.get("carried_over_fixed", ()) or ():
        if isinstance(entry, dict):
            _add_currency(currencies, entry.get("currency"), strict=False)
    return currencies


def _cycle_currencies(bind, cycle: dict) -> set[str]:
    currencies: set[str] = set()
    strict = cycle["status"] == "open"
    # Include deleted identities too: migration must not detach or silently
    # relabel money history that can still be restored by source_key.
    for table in ("cycle_incomes", "savings_targets", "savings_reserved", "savings_realizations"):
        values = bind.execute(sa.text(
            f"SELECT DISTINCT currency FROM {table} WHERE cycle_id=:cycle"
        ), {"cycle": cycle["id"]}).scalars()
        for value in values:
            _add_currency(currencies, value, strict=strict)
    currencies.update(_snapshot_currencies(cycle["closing_snapshot"]))
    if not strict:
        return currencies

    values = bind.execute(sa.text(
        "SELECT DISTINCT currency FROM expenses WHERE telegram_user_id=:user "
        "AND deleted_at IS NULL AND expense_date BETWEEN :start AND :end"
    ), {"user": cycle["telegram_user_id"], "start": cycle["start_date"], "end": cycle["end_date"]}).scalars()
    for value in values:
        _add_currency(currencies, value, strict=True)
    # Reserving too many currencies is safer than hiding an active template
    # whose due date or prior carry-over would be interpreted by the old app.
    for query in (
        "SELECT DISTINCT currency FROM fixed_expenses WHERE telegram_user_id=:user AND is_active",
        "SELECT DISTINCT currency FROM recurring_expenses WHERE telegram_user_id=:user AND is_active AND is_obligation",
    ):
        for value in bind.execute(sa.text(query), {"user": cycle["telegram_user_id"]}).scalars():
            _add_currency(currencies, value, strict=True)
    previous = bind.execute(sa.text(
        "SELECT closing_snapshot FROM financial_cycles WHERE telegram_user_id=:user "
        "AND cycle_index<:index AND status='closed' ORDER BY cycle_index DESC LIMIT 1"
    ), {"user": cycle["telegram_user_id"], "index": cycle["cycle_index"]}).scalar_one_or_none()
    currencies.update(_snapshot_currencies(previous, carry_only=True))
    return currencies


def upgrade() -> None:
    bind = op.get_bind()
    cycles = bind.execute(sa.text("SELECT * FROM financial_cycles ORDER BY id")).mappings().all()
    plan = []
    for row in cycles:
        cycle = dict(row)
        currencies = _cycle_currencies(bind, cycle)
        if cycle["status"] == "open" and len(currencies) > 1:
            raise RuntimeError(
                f"Open mixed-currency cycle {cycle['id']} cannot be upgraded safely "
                f"({', '.join(sorted(currencies))}). Close it with the previous app version "
                "before upgrading; no schema, snapshot or money association was changed. "
                "Active fixed/recurring templates and historical carry-over are checked conservatively."
            )
        currency = next(iter(currencies)) if len(currencies) == 1 else "ARS"
        plan.append((cycle, currency, len(currencies) > 1))

    op.create_table(
        "currency_payday_settings",
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("payday_day", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("pay_rule", sa.String(24), nullable=False, server_default="fixed_day"),
        sa.Column("payday_weekday", sa.Integer(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("telegram_user_id", "currency", name="pk_currency_payday_settings"),
        sa.CheckConstraint("payday_day BETWEEN 1 AND 31", name="ck_currency_payday_settings_payday_day_range"),
        sa.CheckConstraint(_RULE_CHECK, name="ck_currency_payday_settings_payday_rule"),
        sa.CheckConstraint(_CURRENCY_CHECK, name="ck_currency_payday_settings_currency"),
    )
    op.add_column("financial_cycles", sa.Column("currency", sa.String(8), nullable=False, server_default="ARS"))
    op.add_column("financial_cycles", sa.Column("legacy_mixed_currency", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_check_constraint("ck_financial_cycles_currency", "financial_cycles", _CURRENCY_CHECK)
    op.create_check_constraint("ck_financial_cycles_legacy_closed", "financial_cycles", "NOT legacy_mixed_currency OR status='closed'")
    op.drop_index("uq_financial_cycles_user_open", table_name="financial_cycles")
    op.drop_constraint("uq_financial_cycles_user_index", "financial_cycles", type_="unique")
    op.create_unique_constraint("uq_financial_cycles_user_index", "financial_cycles", ["telegram_user_id", "currency", "cycle_index"])
    op.create_index("uq_financial_cycles_user_open", "financial_cycles", ["telegram_user_id", "currency"],
                    unique=True, postgresql_where=sa.text("status='open'"))

    for cycle, currency, mixed in plan:
        bind.execute(sa.text(
            "UPDATE financial_cycles SET currency=:currency,legacy_mixed_currency=:mixed WHERE id=:id"
        ), {"currency": currency, "mixed": mixed, "id": cycle["id"]})
        if cycle["status"] == "open" and currency != "ARS":
            # An already-open foreign-currency cycle owns its frozen rule.
            # Do not let a more recent ARS preference change its successor.
            bind.execute(sa.text(
                "INSERT INTO currency_payday_settings "
                "(telegram_user_id,currency,payday_day,pay_rule,payday_weekday,timezone) "
                "VALUES (:user,:currency,:day,:rule,:weekday,"
                "(SELECT timezone FROM user_settings WHERE telegram_user_id=:user))"
            ), {"user": cycle["telegram_user_id"], "currency": currency,
                "day": cycle["payday_day"], "rule": cycle["pay_rule"],
                "weekday": cycle["payday_weekday"]})


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM currency_payday_settings LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade while currency-specific payday settings exist; doing so would discard calendar configuration.")
    if bind.execute(sa.text(
        "SELECT telegram_user_id FROM financial_cycles WHERE status='open' "
        "GROUP BY telegram_user_id HAVING count(*)>1 LIMIT 1"
    )).first():
        raise RuntimeError("Cannot downgrade while a user has multiple open currency cycles; close them explicitly first.")
    if bind.execute(sa.text(
        "SELECT telegram_user_id,cycle_index FROM financial_cycles "
        "GROUP BY telegram_user_id,cycle_index HAVING count(*)>1 LIMIT 1"
    )).first():
        raise RuntimeError("Cannot downgrade currency histories with duplicate per-user cycle indexes without losing their identity.")
    op.drop_index("uq_financial_cycles_user_open", table_name="financial_cycles")
    op.drop_constraint("uq_financial_cycles_user_index", "financial_cycles", type_="unique")
    op.create_unique_constraint("uq_financial_cycles_user_index", "financial_cycles", ["telegram_user_id", "cycle_index"])
    op.create_index("uq_financial_cycles_user_open", "financial_cycles", ["telegram_user_id"],
                    unique=True, postgresql_where=sa.text("status='open'"))
    op.drop_constraint("ck_financial_cycles_currency", "financial_cycles", type_="check")
    op.drop_constraint("ck_financial_cycles_legacy_closed", "financial_cycles", type_="check")
    op.drop_column("financial_cycles", "legacy_mixed_currency")
    op.drop_column("financial_cycles", "currency")
    op.drop_table("currency_payday_settings")
