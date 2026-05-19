"""Create Upcoming + Calendar views on the Notion plan database.

The Notion API shipped view creation in version 2025-09-03 (``POST /v1/views``).
Before that, views were UI-only — which is why every popular Notion workout
template still asks users to "add a calendar view manually."

Layout this script provisions:

- **Upcoming** (gallery, sort Date asc, filter ``Date >= today``) — the
  default leftmost view. Designed for mobile: Notion's calendar view on
  iOS collapses every entry to a gray dot regardless of view range, so a
  card-style upcoming-sessions list is the only legible layout on phone.
- **Calendar** (week range, week-by-week visualization) — useful on desktop
  where calendar tiles have enough vertical space to render content.

Order matters: Upcoming is created first so it stays leftmost (Notion's
default view is whichever tab is leftmost, and view order in the tab bar
follows creation order — the API does not expose a reorder/position field).

Idempotent: existing views with matching names are left untouched, so this
script is safe to re-run after schema or label changes. Re-running also
refreshes the ``Date >= today`` filter to today's date.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

NOTION_VERSION = "2025-09-03"
NOTION_API = "https://api.notion.com/v1"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _existing_views(client: httpx.Client, token: str, db_id: str) -> list[dict[str, Any]]:
    # The /views?database_id=... list endpoint currently returns name=None for
    # every view (Notion bug as of 2026-05). Fetching each individually gives
    # us the real names.
    resp = client.get(f"{NOTION_API}/views", params={"database_id": db_id}, headers=_headers(token))
    resp.raise_for_status()
    views = list(resp.json().get("results", []) or [])
    detailed: list[dict[str, Any]] = []
    for v in views:
        r = client.get(f"{NOTION_API}/views/{v['id']}", headers=_headers(token))
        if r.status_code == 200:
            detailed.append(r.json())
    return detailed


def _property_ids(client: httpx.Client, token: str, ds_id: str) -> dict[str, str]:
    resp = client.get(f"{NOTION_API}/data_sources/{ds_id}", headers=_headers(token))
    resp.raise_for_status()
    props = resp.json().get("properties") or {}
    return {name: meta["id"] for name, meta in props.items() if "id" in meta}


def _create_view(client: httpx.Client, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = client.post(f"{NOTION_API}/views", headers=_headers(token), json=payload)
    if resp.status_code >= 400:
        raise RuntimeError(f"create view failed: {resp.status_code} {resp.text}")
    return dict(resp.json())


def _patch_view(
    client: httpx.Client, token: str, view_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    resp = client.patch(f"{NOTION_API}/views/{view_id}", headers=_headers(token), json=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"patch view {view_id} failed: {resp.status_code} {resp.text}")
    return dict(resp.json())


def main() -> None:
    token = os.environ["NOTION_TOKEN"]
    db_id = os.environ["NOTION_DB_PLAN_ID"]
    today = date.today().isoformat()

    with httpx.Client(timeout=30) as client:
        # The 2025-09-03 API splits database (container) from data_source (the
        # schema/rows). Most plan databases have exactly one data source.
        db_resp = client.get(f"{NOTION_API}/databases/{db_id}", headers=_headers(token))
        db_resp.raise_for_status()
        data_sources = db_resp.json().get("data_sources") or []
        if not data_sources:
            raise RuntimeError("plan database has no data sources")
        ds_id = data_sources[0]["id"]

        prop_ids = _property_ids(client, token, ds_id)
        date_id = prop_ids.get("Date")
        if not date_id:
            raise RuntimeError("plan data source has no Date property to anchor a calendar on")

        existing = _existing_views(client, token, db_id)
        existing_by_name = {(v.get("name") or "").lower(): v for v in existing}
        print(f"existing views: {sorted(existing_by_name) or '[none]'}")

        # 1) Upcoming gallery view — leftmost = default. Mobile-friendly.
        # Sort + filter use property name (not property_id) because that is
        # what Notion's query filter/sort schema accepts.
        if "upcoming" not in existing_by_name:
            upcoming_payload: dict[str, Any] = {
                "database_id": db_id,
                "data_source_id": ds_id,
                "name": "Upcoming",
                "type": "gallery",
                "sorts": [{"property": "Date", "direction": "ascending"}],
                "filter": {
                    "property": "Date",
                    "date": {"on_or_after": today},
                },
                "configuration": {
                    "type": "gallery",
                    # Notion's default when no cover image is set is to show
                    # the page body as card preview text — workout
                    # description renders directly on each card.
                    "card_layout": "list",
                    "properties": [
                        {"property_id": "title", "visible": True},
                        *[
                            {"property_id": prop_ids[name], "visible": True}
                            for name in (
                                "Date",
                                "Session Type",
                                "Duration (min)",
                                "Intensity",
                                "Status",
                            )
                            if name in prop_ids
                        ],
                    ],
                },
            }
            result = _create_view(client, token, upcoming_payload)
            print(f"created Upcoming gallery view: id={result.get('id')}")
        else:
            # PATCH the existing view's filter so today's date is fresh.
            # Sort never changes; configuration never changes; only filter
            # needs refreshing. (Full-configuration PATCH is not supported
            # for gallery/calendar views — only top-level filter and sorts.)
            view_id = existing_by_name["upcoming"]["id"]
            _patch_view(
                client,
                token,
                view_id,
                {
                    "filter": {
                        "property": "Date",
                        "date": {"on_or_after": today},
                    }
                },
            )
            print(f"refreshed Upcoming view filter to Date >= {today}")

        # 2) Calendar view — week range, for desktop use. Created after
        # Upcoming so the latter stays as the leftmost (default) tab.
        if "calendar" not in existing_by_name:
            calendar_payload: dict[str, Any] = {
                "database_id": db_id,
                "data_source_id": ds_id,
                "name": "Calendar",
                "type": "calendar",
                "configuration": {
                    "type": "calendar",
                    "date_property_id": date_id,
                    # Week range gives each day cell enough vertical space
                    # for the title + properties. Month-range tiles collapse
                    # to gray dots on mobile and even on desktop when there
                    # is more than a single short line per entry.
                    "view_range": "week",
                    "show_weekends": True,
                    "properties": [
                        {"property_id": "title", "visible": True},
                        *[
                            {"property_id": prop_ids[name], "visible": True}
                            for name in ("Session Type", "Duration (min)", "Intensity", "Status")
                            if name in prop_ids
                        ],
                    ],
                },
            }
            result = _create_view(client, token, calendar_payload)
            print(f"created Calendar view: id={result.get('id')}")
        else:
            print("Calendar view already exists — skipped")


if __name__ == "__main__":
    main()
