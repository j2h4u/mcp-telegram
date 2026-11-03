"""SQL predicates for persisted dialog access state."""


def not_access_lost_sql(dialog_id_expression: str) -> str:
    """Return a correlated SQL predicate excluding peers whose access is suspended."""
    return (
        "NOT EXISTS (SELECT 1 FROM synced_dialogs access_state "
        f"WHERE access_state.dialog_id={dialog_id_expression} "
        "AND access_state.status='access_lost')"
    )
