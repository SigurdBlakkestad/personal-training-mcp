"""Create Calendar + Gallery views on the Notion plan database.

The Notion API shipped view creation in version 2025-09-03 (``POST /v1/views``).
Before that, views were UI-only — which is why every popular Notion workout
template asks the user to "add a calendar view manually." With the new
endpoint we can wire the same layouts the popular templates use without any
clicks in the browser.

Idempotent: existing views with matching names are left untouched, so this
script is safe to re-run after schema or label changes.
"""

from __future__ import annotations

import os
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
    resp = client.get(f"{NOTION_API}/views", params={"database_id": db_id}, headers=_headers(token))
    resp.raise_for_status()
    return list(resp.json().get("results", []) or [])


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


def main() -> None:
    token = os.environ["NOTION_TOKEN"]
    db_id = os.environ["NOTION_DB_PLAN_ID"]

    with httpx.Client(timeout=30) as client:
        # The new API splits database (container) from data_source (the
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
        existing_names = {(v.get("name") or "").lower() for v in existing}
        print(f"existing views: {sorted(existing_names) or '[unnamed default]'}")

        # Calendar view — week-by-week training plan, with key fields visible
        # on each tile. Workout-template-style layout.
        if "calendar" not in existing_names:
            calendar_payload: dict[str, Any] = {
                "database_id": db_id,
                "data_source_id": ds_id,
                "name": "Calendar",
                "type": "calendar",
                "configuration": {
                    "type": "calendar",
                    "date_property_id": date_id,
                    # Week range gives each day cell much more vertical space
                    # than month view — month tiles render as gray dots when
                    # there is more than the title to display.
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

        # Gallery view — card-style "what does the week look like" overview,
        # matching the layout most public Notion workout-planner templates use.
        if "gallery" not in existing_names:
            gallery_payload = {
                "database_id": db_id,
                "data_source_id": ds_id,
                "name": "Gallery",
                "type": "gallery",
                "configuration": {
                    "type": "gallery",
                    "cover_size": "medium",
                    "cover_aspect": "cover",
                    "card_layout": "list",
                    "properties": [
                        {"property_id": "title", "visible": True},
                        *[
                            {"property_id": prop_ids[name], "visible": True}
                            for name in ("Date", "Session Type", "Duration (min)", "Intensity")
                            if name in prop_ids
                        ],
                    ],
                },
            }
            result = _create_view(client, token, gallery_payload)
            print(f"created Gallery view: id={result.get('id')}")
        else:
            print("Gallery view already exists — skipped")


if __name__ == "__main__":
    main()
