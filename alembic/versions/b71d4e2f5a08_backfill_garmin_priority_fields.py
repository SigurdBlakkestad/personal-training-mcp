"""backfill measurement columns from garmin_supplement

Revision ID: b71d4e2f5a08
Revises: a3e5c7d9f213
Create Date: 2026-05-16 09:00:00.000000

Historical Strava-canonical rows kept Strava's re-processed duration / HR /
power values in the measurement columns and stashed the Garmin payload in
``garmin_supplement``. Strava's re-processing strips Garmin's auto-pause and
re-averages over the longer wall-clock window, distorting HR and power
intensity for indoor / trainer sessions. Garmin's device numbers are the
source of truth for these fields whenever they exist — so we overwrite the
columns from the Garmin payload.

Idempotent: re-running this migration on already-backfilled rows is a no-op
(every UPDATE just re-applies the same Garmin-derived value).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "b71d4e2f5a08"
down_revision: str | Sequence[str] | None = "a3e5c7d9f213"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE activities
        SET
          duration_seconds = COALESCE(
            (garmin_supplement ->> 'duration')::numeric::integer,
            duration_seconds
          ),
          avg_hr = COALESCE(
            (garmin_supplement ->> 'averageHR')::numeric::integer,
            avg_hr
          ),
          max_hr = COALESCE(
            (garmin_supplement ->> 'maxHR')::numeric::integer,
            max_hr
          ),
          distance_meters = COALESCE(
            (garmin_supplement ->> 'distance')::double precision,
            distance_meters
          ),
          elevation_gain_meters = COALESCE(
            (garmin_supplement ->> 'elevationGain')::double precision,
            elevation_gain_meters
          ),
          avg_power = COALESCE(
            (garmin_supplement ->> 'avgPower')::numeric::integer,
            avg_power
          ),
          max_power = COALESCE(
            (garmin_supplement ->> 'maxPower')::numeric::integer,
            (garmin_supplement ->> 'maxAvgPower')::numeric::integer,
            max_power
          ),
          normalized_power = COALESCE(
            (garmin_supplement ->> 'normPower')::numeric::integer,
            normalized_power
          ),
          avg_cadence = COALESCE(
            (garmin_supplement ->> 'averageBikingCadenceInRevPerMinute')::numeric::integer,
            (garmin_supplement ->> 'averageRunningCadenceInStepsPerMinute')::numeric::integer,
            (garmin_supplement ->> 'averageCadenceInStepsPerMinute')::numeric::integer,
            avg_cadence
          ),
          calories = COALESCE(
            (garmin_supplement ->> 'calories')::numeric::integer,
            calories
          ),
          aerobic_training_effect = COALESCE(
            aerobic_training_effect,
            (garmin_supplement ->> 'aerobicTrainingEffect')::real
          ),
          anaerobic_training_effect = COALESCE(
            anaerobic_training_effect,
            (garmin_supplement ->> 'anaerobicTrainingEffect')::real
          ),
          training_effect_label = COALESCE(
            training_effect_label,
            garmin_supplement ->> 'trainingEffectLabel'
          ),
          vo2_max = COALESCE(
            vo2_max,
            (garmin_supplement ->> 'vO2MaxValue')::real
          ),
          moderate_intensity_minutes = COALESCE(
            moderate_intensity_minutes,
            (garmin_supplement ->> 'moderateIntensityMinutes')::numeric::integer
          ),
          vigorous_intensity_minutes = COALESCE(
            vigorous_intensity_minutes,
            (garmin_supplement ->> 'vigorousIntensityMinutes')::numeric::integer
          ),
          min_hr = COALESCE(
            min_hr,
            (garmin_supplement ->> 'minHR')::numeric::integer
          ),
          avg_stride_length_cm = COALESCE(
            avg_stride_length_cm,
            (garmin_supplement ->> 'avgStrideLength')::real
          ),
          avg_ground_contact_time_ms = COALESCE(
            avg_ground_contact_time_ms,
            (garmin_supplement ->> 'avgGroundContactTime')::numeric::integer
          ),
          updated_at = NOW()
        WHERE garmin_supplement IS NOT NULL
        """
    )


def downgrade() -> None:
    # Cannot reconstruct Strava's pre-backfill values — they'd have to be
    # re-derived from each Strava activity's raw payload, which isn't
    # guaranteed to still be present. Leave the backfill in place on
    # downgrade.
    pass
