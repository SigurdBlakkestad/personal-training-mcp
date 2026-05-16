"""One-off cleanup for the 2026-05-15 Sykkeløkt / "Afternoon Ride" duplicate.

A Garmin row and a Strava row landed for the same indoor trainer session
because the bidirectional dedup logic in
``src/training_pipeline/ingestors/strava.py:_consume_garmin_row_if_exists``
did not exist yet on May 15 when those two ingestors ran. See the migration
``b71d4e2f5a08`` for the Garmin-priority rule that prevents this going
forward.

Run once after deploying the dedup fix::

    python scripts/cleanup_duplicate_cycling.py

Does nothing if the duplicate is already gone (idempotent on the (source_id)
pair).
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text

GARMIN_ID = "ffe1c69e-b91b-426a-87b6-6471c492f03f"
STRAVA_ID = "dd4d72a4-6a81-4fe4-af63-79c25ec20bbb"

# Garmin-only columns that the Strava row picked up via a post-fix merge but
# were never written onto the Garmin row (those columns didn't exist when
# this Garmin row was first inserted). Copy them over before deleting Strava.
PORT_FIELDS = (
    "aerobic_training_effect",
    "anaerobic_training_effect",
    "training_effect_label",
    "vo2_max",
    "moderate_intensity_minutes",
    "vigorous_intensity_minutes",
    "min_hr",
    "avg_stride_length_cm",
    "avg_ground_contact_time_ms",
    "notion_page_id",
)


def main() -> None:
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url)
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT id FROM activities WHERE id = :id"), {"id": STRAVA_ID}
        ).scalar()
        if exists is None:
            print(f"strava row {STRAVA_ID} already gone — nothing to do")
            return
        set_clauses = ", ".join(
            f"{f} = COALESCE({f}, (SELECT {f} FROM activities WHERE id = :strava))"
            for f in PORT_FIELDS
        )
        conn.execute(
            text(
                f"""
                UPDATE activities
                SET {set_clauses}, updated_at = NOW()
                WHERE id = :garmin
                """
            ),
            {"strava": STRAVA_ID, "garmin": GARMIN_ID},
        )
        deleted = conn.execute(
            text("DELETE FROM activities WHERE id = :strava"),
            {"strava": STRAVA_ID},
        ).rowcount
        print(f"strava duplicate deleted: {deleted}")


if __name__ == "__main__":
    main()
