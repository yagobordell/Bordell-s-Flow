from __future__ import annotations

INSTANCE_DRAIN_LOCK_NAMESPACE = 1_497_450_320


def acquire_instance_drain_lock(connection: object, instance_id: str) -> None:
    """Serialize drain publication and job claims for one Salad instance."""

    resolved = str(instance_id or "").strip()
    if not resolved:
        raise ValueError("instance_id is required for drain coordination")
    connection.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (INSTANCE_DRAIN_LOCK_NAMESPACE, resolved),
    )
