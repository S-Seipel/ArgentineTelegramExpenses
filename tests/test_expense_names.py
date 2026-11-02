from app.expenses.names import normalize_expense_name


def test_normalize_expense_name_groups_spacing_and_accents():
    assert normalize_expense_name("tapa bocas") == "tapabocas"
    assert normalize_expense_name("tapabocas") == "tapabocas"
    assert normalize_expense_name("Café") == "cafe"
