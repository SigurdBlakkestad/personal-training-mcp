"""Generate and write ``docs/training.ics`` from weekly plans.

The publisher fetches every past weekly plan plus the current week and up to
three following weeks (all flagged ``is_current = true``), renders them as a
single .ics calendar, and writes the result to ``docs/training.ics``. Past
weeks are retained so calendars subscribed to the feed keep historical
sessions instead of deleting them once a week rolls by. It returns whether
the on-disk file changed so the workflow can decide to commit + push.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from training_pipeline.calendar_publish.ics_builder import render
from training_pipeline.shared.db import get_session
from training_pipeline.shared.logging import get_logger
from training_pipeline.shared.models import WeeklyPlan

logger = get_logger(__name__)

DEFAULT_WEEKS_AHEAD = 4
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parents[3] / "docs" / "training.ics"


@dataclass
class PublishResult:
    output_path: Path
    plans: int
    sessions: int
    changed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "output_path": str(self.output_path),
            "plans": self.plans,
            "sessions": self.sessions,
            "changed": self.changed,
        }


def _current_monday(today: date_type | None = None) -> date_type:
    today = today or date_type.today()
    return today - timedelta(days=today.weekday())


def fetch_plans(
    session: Session,
    *,
    today: date_type | None = None,
    weeks_ahead: int = DEFAULT_WEEKS_AHEAD,
) -> list[WeeklyPlan]:
    """Return all past plans plus the current and up to ``weeks_ahead - 1`` future weeks.

    Past weeks carry no lower bound so a subscribed calendar keeps historical
    sessions; only the future is capped, at ``weeks_ahead - 1`` weeks out.
    """
    end_monday = _current_monday(today) + timedelta(weeks=weeks_ahead - 1)
    stmt = (
        select(WeeklyPlan)
        .where(WeeklyPlan.is_current.is_(True))
        .where(WeeklyPlan.week_of <= end_monday)
        .order_by(WeeklyPlan.week_of.asc())
    )
    return list(session.scalars(stmt))


def write_calendar(plans: list[WeeklyPlan], output_path: Path) -> bool:
    """Render plans and write to ``output_path``. Return True if content changed.

    Rendered with ``public=True``: the file is served on a public URL that the
    iPhone calendar subscribes to, so free-text health/injury detail is stripped.
    """
    content = render(plans, public=True).encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    previous = output_path.read_bytes() if output_path.exists() else None
    if previous == content:
        return False
    output_path.write_bytes(content)
    return True


def publish(
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    weeks_ahead: int = DEFAULT_WEEKS_AHEAD,
    today: date_type | None = None,
) -> PublishResult:
    with get_session() as session:
        plans = fetch_plans(session, today=today, weeks_ahead=weeks_ahead)
        session_count = sum(
            sum(1 for s in plan.plan if isinstance(s, dict))
            for plan in plans
            if isinstance(plan.plan, list)
        )
        changed = write_calendar(plans, output_path)
    result = PublishResult(
        output_path=output_path,
        plans=len(plans),
        sessions=session_count,
        changed=changed,
    )
    logger.info("calendar.publish", **result.to_dict())
    return result
