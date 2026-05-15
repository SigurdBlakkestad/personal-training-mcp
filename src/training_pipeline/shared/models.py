from datetime import date as date_type
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, REAL
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sport_type: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    distance_meters: Mapped[float | None] = mapped_column(REAL)
    elevation_gain_meters: Mapped[float | None] = mapped_column(REAL)
    avg_hr: Mapped[int | None] = mapped_column(SmallInteger)
    max_hr: Mapped[int | None] = mapped_column(SmallInteger)
    avg_power: Mapped[int | None] = mapped_column(SmallInteger)
    normalized_power: Mapped[int | None] = mapped_column(SmallInteger)
    calories: Mapped[int | None] = mapped_column(Integer)
    avg_cadence: Mapped[int | None] = mapped_column(SmallInteger)
    training_load: Mapped[float | None] = mapped_column(REAL)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    notion_page_id: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_activities_source_source_id"),
    )


class BodyMeasurement(Base):
    __tablename__ = "body_measurements"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    weight_kg: Mapped[float | None] = mapped_column(REAL)
    body_fat_pct: Mapped[float | None] = mapped_column(REAL)
    muscle_mass_kg: Mapped[float | None] = mapped_column(REAL)
    bone_mass_kg: Mapped[float | None] = mapped_column(REAL)
    water_pct: Mapped[float | None] = mapped_column(REAL)
    visceral_fat: Mapped[float | None] = mapped_column(REAL)
    bmi: Mapped[float | None] = mapped_column(REAL)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DailySummary(Base):
    __tablename__ = "daily_summary"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    sleep_score: Mapped[int | None] = mapped_column(SmallInteger)
    sleep_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    resting_hr: Mapped[int | None] = mapped_column(SmallInteger)
    hrv_ms: Mapped[float | None] = mapped_column(REAL)
    stress_avg: Mapped[int | None] = mapped_column(SmallInteger)
    body_battery_high: Mapped[int | None] = mapped_column(SmallInteger)
    body_battery_low: Mapped[int | None] = mapped_column(SmallInteger)
    steps: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("date", "source", name="uq_daily_summary_date_source"),)


class ManualLog(Base):
    __tablename__ = "manual_logs"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activity_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("activities.id", ondelete="SET NULL"),
    )
    rpe: Mapped[int | None] = mapped_column(SmallInteger)
    pain_score: Mapped[int | None] = mapped_column(SmallInteger)
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WeeklyPlan(Base):
    __tablename__ = "weekly_plans"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    week_of: Mapped[date_type] = mapped_column(Date, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    plan: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("week_of", "version", name="uq_weekly_plans_week_of_version"),
    )


class DerivedMetric(Base):
    __tablename__ = "derived_metrics"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    metric_name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(REAL, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("date", "metric_name", name="uq_derived_metrics_date_metric"),
    )


class AthleteContext(Base):
    __tablename__ = "athlete_context"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=text("1"))
    ftp_watts: Mapped[int | None] = mapped_column(Integer)
    max_hr: Mapped[int | None] = mapped_column(SmallInteger)
    body_weight_kg: Mapped[float | None] = mapped_column(REAL)
    current_phase: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (CheckConstraint("id = 1", name="ck_athlete_context_singleton"),)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(Text)
    records_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    records_inserted: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    records_updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    cursor: Mapped[str | None] = mapped_column(Text)
