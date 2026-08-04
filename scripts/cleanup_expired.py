"""Periodic cleanup of expired data.

Run as a cron job or systemd timer:
    uv run python scripts/cleanup_expired.py

Environment variables:
    FULL_VIEW_DATABASE_URL - PostgreSQL connection string (required)
    FULL_VIEW_POSTGRES_SCHEMA - Schema name (default: full_view_agent)
    FULL_VIEW_RETENTION_DAYS - Days to retain terminal runs (default: 30)
"""

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta


async def cleanup_expired(
    dsn: str,
    *,
    schema: str = "full_view_agent",
    retention_days: int = 30,
) -> dict[str, int]:
    """Delete expired events, old terminal runs, and revoked credentials.

    Returns a dict with counts of deleted rows per category.
    """
    import psycopg

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    counts: dict[str, int] = {}

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        prefix = f'"{schema}".'

        # Delete expired events
        result = await conn.execute(
            f"DELETE FROM {prefix}events WHERE expires_at < %s",
            (datetime.now(UTC),),
        )
        counts["expired_events"] = result.rowcount or 0

        # Find old terminal runs by inspecting data_json status field
        old_runs_result = await conn.execute(
            f"SELECT run_id FROM {prefix}runs "
            "WHERE data_json::jsonb->>'status' "
            "IN ('completed', 'failed', 'cancelled', 'expired') "
            "AND (data_json::jsonb->>'completed_at')::timestamptz < %s",
            (cutoff,),
        )
        run_ids = [row[0] for row in await old_runs_result.fetchall()]
        if run_ids:
            # Analysis plans are authoritative run-scoped records and have no
            # foreign key to the product ledger, so delete them explicitly
            # before their owning runs.
            await conn.execute(
                f"DELETE FROM {prefix}analysis_plans WHERE run_id = ANY(%s)",
                (run_ids,),
            )
            # Delete receipts via frontend_commands (receipts have no run_id)
            await conn.execute(
                f"DELETE FROM {prefix}frontend_command_receipts "
                f"WHERE command_id IN ("
                f"SELECT command_id FROM {prefix}frontend_commands "
                f"WHERE run_id = ANY(%s))",
                (run_ids,),
            )
            # Delete evidence via results (evidence has result_id, not run_id)
            await conn.execute(
                f"DELETE FROM {prefix}evidence "
                f"WHERE result_id IN ("
                f"SELECT result_id FROM {prefix}results "
                f"WHERE run_id = ANY(%s))",
                (run_ids,),
            )
            # Tables with direct run_id column
            for table in (
                "frontend_commands",
                "steers",
                "input_requests",
                "auth_contexts",
                "events",
                "results",
                "messages",
                "runs",
            ):
                await conn.execute(
                    f"DELETE FROM {prefix}{table} WHERE run_id = ANY(%s)",
                    (run_ids,),
                )
            counts["terminal_runs"] = len(run_ids)
        else:
            counts["terminal_runs"] = 0

        # Delete expired/revoked credentials
        result = await conn.execute(
            f"DELETE FROM {prefix}credentials "
            "WHERE expires_at < %s OR revoked_at IS NOT NULL",
            (datetime.now(UTC),),
        )
        counts["expired_credentials"] = result.rowcount or 0

        await conn.commit()
    return counts


def _run_main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())


async def main() -> None:
    dsn = os.environ.get("FULL_VIEW_DATABASE_URL")
    if not dsn:
        print("FULL_VIEW_DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)
    schema = os.environ.get("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent")
    retention_days = int(os.environ.get("FULL_VIEW_RETENTION_DAYS", "30"))

    counts = await cleanup_expired(dsn, schema=schema, retention_days=retention_days)
    for key, value in counts.items():
        print(f"{key}: {value}")
    print("Cleanup completed")


if __name__ == "__main__":
    _run_main()
