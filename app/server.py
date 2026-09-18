"""
Local web UI — serves the calendar and a small JSON API on 127.0.0.1.

Bound to loopback only. This process has a live LSM session behind it, so
it must not be reachable from the network; there is deliberately no
authentication because there is no remote surface.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from app import avail, store
from app.config import APP_NAME, WEB_DIR, log
from app.ics import build_ics
from app.rooms import floor_sort_key


def create_app(orchestrator: Any) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False

    # ── Assets ───────────────────────────────────────────────────────────

    @app.get("/")
    def calendar() -> Any:
        return send_from_directory(WEB_DIR, "calendar.html")

    @app.get("/list")
    def listing() -> Any:
        return send_from_directory(WEB_DIR, "list.html")

    # The helpers both pages share. A dedicated route rather than the usual
    # static folder: create_app is built with static_folder=None, so there is no
    # /static to put it in, and a <path:filename> catch-all would expose the
    # whole web directory to serve one file on an app that binds loopback only
    # and holds a live LSM session. One named file is the smaller surface.
    @app.get("/filters.js")
    def filters_js() -> Any:
        return send_from_directory(WEB_DIR, "filters.js", mimetype="text/javascript")

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status=204)

    # ── Data ─────────────────────────────────────────────────────────────

    @app.get("/api/bootstrap")
    def bootstrap() -> Any:
        """
        Everything the calendar needs in one round trip: bookings, the
        room list actually present in the data, and the group definitions.
        """
        events = store.get_events()
        rooms = _active_rooms(events)
        return jsonify({
            "events": events,
            "rooms": [r["room"] for r in rooms],
            "room_meta": {r["room"]: r for r in rooms},
            "groups": store.load_groups(),
            "count": len(events),
        })

    @app.get("/api/events")
    def api_events() -> Any:
        events = store.get_events(
            room=request.args.get("room"),
            date=request.args.get("date"),
            q=request.args.get("q"),
            date_from=request.args.get("from"),
            date_to=request.args.get("to"),
            include_cancelled=request.args.get("cancelled") == "1",
        )
        return jsonify({"total_events": len(events), "events": events})

    @app.get("/api/rooms")
    def api_rooms() -> Any:
        events = store.get_events()
        return jsonify({
            "rooms": _active_rooms(events),
            "all": store.get_rooms(),
            "groups": store.load_groups(),
        })

    @app.get("/api/room-groups")
    def api_get_groups() -> Any:
        return jsonify(store.load_groups())

    @app.post("/api/room-groups")
    def api_set_groups() -> Any:
        payload = request.get_json(silent=True)
        # Validated, not merely type-checked. load_groups has no merge, so
        # whatever arrives here *becomes* the whole set — anything malformed
        # that got written would take every user group with it.
        if not store.valid_groups(payload):
            return jsonify({"error": "expected {name: [room, ...]}"}), 400
        store.save_groups(payload)
        return jsonify({"status": "ok", "groups": payload})

    @app.get("/api/presets")
    def api_get_presets() -> Any:
        return jsonify({"presets": store.load_presets()})

    @app.post("/api/presets")
    def api_save_preset() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        name = (payload.get("name") or "").strip()
        filters = payload.get("filters")
        if not name or len(name) > 60:
            return jsonify({"error": "name must be 1-60 characters"}), 400
        if not isinstance(filters, dict):
            return jsonify({"error": "expected a filters object"}), 400
        return jsonify({"status": "ok", "presets": store.save_preset(name, filters)})

    @app.delete("/api/presets")
    def api_delete_preset() -> Any:
        name = (request.args.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400
        return jsonify({"status": "ok", "presets": store.delete_preset(name)})

    @app.get("/api/autocomplete")
    def autocomplete() -> Any:
        q = (request.args.get("q") or "").lower()
        if not q:
            return jsonify({"rooms": [], "groups": [], "titles": []})

        events = store.get_events()
        rooms = sorted({e["room"] for e in events if q in (e["room"] or "").lower()})
        titles = sorted({e["title"] for e in events if q in (e["title"] or "").lower()})
        groups = [g for g in store.load_groups() if q in g.lower()]
        return jsonify({
            "rooms": rooms[:8],
            "groups": groups,
            "titles": titles[:10],
        })

    @app.get("/api/today")
    def today() -> Any:
        """
        Per-room availability at an instant — booked vs free, with the next slot.

        Answers two questions through one predicate: the sidebar's Free Right
        Now, which passes nothing, and the filter panel's "free at a time",
        which passes the viewed date, a clock time and a length. Both go through
        app.avail, so the two cannot drift apart.

        A `dates` list asks the same window question of many days at once, which
        is what the month and week views need. See _availability_by_day.
        """
        now = datetime.now()
        date_arg = request.args.get("date") or ""
        at_arg = request.args.get("at") or ""
        try:
            # Either may be omitted on its own: a date with no time means "as of
            # now, on that date", and a time with no date means "today, then".
            when = datetime.fromisoformat(
                f"{date_arg or f'{now:%Y-%m-%d}'}T{at_arg or f'{now:%H:%M}'}:00"
            )
        except ValueError:
            return jsonify({"error": "date is YYYY-MM-DD, at is HH:MM"}), 400

        # Absent or zero means the point in time itself, which is what Free
        # Right Now asks and what the old implementation did. A window is
        # capped at a day because that is the longest the question is meaningful
        # for — the payload it filters is one date's bookings.
        minutes = max(0, min(request.args.get("for", 0, type=int) or 0, 24 * 60))
        when_iso = when.isoformat()
        day_iso = when.strftime("%Y-%m-%d")

        all_events = store.get_events()

        # Read once, above, and shared: the batch form is the same question
        # asked of a month of days, and reading the table per day would be a
        # full scan up to 42 times over.
        dates_arg = request.args.get("dates")
        if dates_arg is not None:
            return _availability_by_day(all_events, dates_arg, when, minutes)

        events = [e for e in all_events if e.get("date") == day_iso]

        # Seed every known room, not just the ones with a booking that day.
        # Building this map from the day's bookings alone meant a room with a
        # completely empty day was absent from the response entirely — so the
        # emptiest rooms, the ones you actually want, were the ones missing.
        by_room: dict[str, list[dict[str, Any]]] = {
            r["room"]: [] for r in _active_rooms(all_events)
        }
        for ev in events:
            by_room.setdefault(ev["room"], []).append(ev)

        until_iso = (when + timedelta(minutes=minutes)).isoformat()
        rooms = []
        for room, bookings in by_room.items():
            bookings.sort(key=lambda e: e.get("start") or "")
            current = (avail.busy_between(bookings, when_iso, until_iso) if minutes
                       else avail.busy_at(bookings, when_iso))
            upcoming = [b for b in bookings if (b.get("start") or "") > when_iso]
            rooms.append({
                "room": room,
                "status": "booked" if current else "free",
                "current": current["title"] if current else None,
                "until": current.get("end") if current else None,
                "next": upcoming[0]["title"] if upcoming else None,
                "next_at": upcoming[0]["start"] if upcoming else None,
                "bookings": bookings,
            })

        rooms.sort(key=lambda r: (r["status"] != "booked", r["room"]))
        return jsonify({
            "date": day_iso,
            "at": when_iso,
            "minutes": minutes,
            "total_bookings": len(events),
            "free": sum(1 for r in rooms if r["status"] == "free"),
            "booked": sum(1 for r in rooms if r["status"] == "booked"),
            "rooms": rooms,
        })

    # ── Control ──────────────────────────────────────────────────────────

    @app.get("/api/status")
    def status() -> Any:
        st = orchestrator.status()
        stats = store.stats()
        # last_run() skips backfill rows, so the status and the finish time
        # below stay paired with the orchestrator's last_scrape_message — a
        # backfill is a run but not a scrape.
        last = store.last_run()

        return jsonify({
            "session": st.get("session", "unknown"),
            "session_message": st.get("session_message", ""),
            "busy": st.get("busy", False),
            "busy_action": st.get("busy_action", ""),
            "progress": st.get("progress", ""),
            "last_scrape": st.get("last_scrape"),
            "last_scrape_human": _human_when(
                (last or {}).get("finished_at") or st.get("last_scrape")
            ),
            "last_scrape_message": st.get("last_scrape_message", ""),
            "last_scrape_status": (last or {}).get("status"),
            "backfill": st.get("backfill"),
            "last_backfill": _backfill_label(store.last_backfill()),
            # Whether the one-time history fill has landed. The sidebar says
            # "filling…" rather than "not backfilled yet" while this is false,
            # because the app now does it unprompted - there is nothing for the
            # reader to go and start.
            "backfill_done": store.backfill_done(),
            "date_range": _range_label(stats.get("date_from"), stats.get("date_to")),
            "total_events": stats.get("total_events", 0),
            "rooms": stats.get("rooms", 0),
        })

    # ── Change feed ──────────────────────────────────────────────────────

    @app.get("/api/changes")
    def api_changes() -> Any:
        """Additions and removals between scrapes, newest first."""
        # A negative or zero limit is nonsense rather than a request for one
        # row, so it falls back to the default. It must never reach SQLite:
        # `LIMIT -1` means *unlimited* there, so the cap would invert and
        # `truncated` below would compare the row count against -1 and report
        # false on a full-table response.
        raw = request.args.get("limit", 200, type=int)
        limit = min(raw, 2000) if raw and raw > 0 else 200
        # An empty `rooms` means "no restriction", so it is omitted rather than
        # passed as a list — the same trap get_events documents.
        rooms = [r for r in (request.args.get("rooms") or "").split(",") if r]
        rows = store.get_changes(
            since=request.args.get("since"),
            kind=request.args.get("kind"),
            room=request.args.get("room"),
            run_id=request.args.get("run_id", type=int),
            include_backfill=request.args.get("include_backfill") == "1",
            limit=limit,
            q=request.args.get("q") or None,
            rooms=rooms or None,
        )
        return jsonify({
            "since": request.args.get("since"),
            "count": len(rows),
            "truncated": len(rows) == limit,
            "added": sum(1 for r in rows if r["kind"] == "added"),
            "removed": sum(1 for r in rows if r["kind"] == "removed"),
            "changes": rows,
        })

    @app.post("/api/scrape")
    def trigger_scrape() -> Any:
        if orchestrator.is_busy():
            return jsonify({"status": "busy",
                            "message": "A scrape is already running"}), 409
        interactive = request.form.get("interactive") == "1"
        orchestrator.request_scrape(interactive=interactive)
        return jsonify({"status": "started", "message": "Scrape started"})

    @app.post("/api/login")
    def trigger_login() -> Any:
        if orchestrator.is_busy():
            return jsonify({"status": "busy",
                            "message": "Something else is running"}), 409
        orchestrator.request_login()
        return jsonify({"status": "started",
                        "message": "Login window opening — approve Duo"})

    @app.post("/api/logout")
    def logout() -> Any:
        from app import session
        if orchestrator.is_busy():
            return jsonify({"status": "busy"}), 409
        session.clear()
        return jsonify({"status": "ok", "message": "Session cleared"})

    # ── Downloads ────────────────────────────────────────────────────────

    @app.get("/download/ics")
    def download_ics() -> Any:
        events = _download_events()
        body = build_ics(events)
        return Response(
            body,
            mimetype="text/calendar",
            headers={"Content-Disposition": "attachment; filename=rotman_bookings.ics"},
        )

    @app.get("/download/json")
    def download_json() -> Any:
        events = _download_events()
        payload = {
            "generated_at": datetime.now().isoformat(),
            "total_events": len(events),
            "events": events,
        }
        return Response(
            json.dumps(payload, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=rotman_bookings.json"},
        )

    # ── Errors ───────────────────────────────────────────────────────────

    @app.errorhandler(404)
    def not_found(_exc: Any) -> Any:
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(500)
    def server_error(exc: Any) -> Any:
        log.exception("unhandled error in web UI: %s", exc)
        return jsonify({"error": "internal error"}), 500

    return app


# ── Helpers ──────────────────────────────────────────────────────────────

def _download_events() -> list[dict[str, Any]]:
    """Events for a download, narrowed by whatever the caller is looking at.

    Both download endpoints used to ignore the UI's filters entirely, so
    "download .ics" always exported the whole four-month database no matter
    what was on screen. They now accept the same room, text and date
    narrowing the calendar applies, and no parameters still means everything.
    """
    rooms = [r for r in (request.args.get("rooms") or "").split(",") if r]
    return store.get_events(
        rooms=rooms or None,
        q=request.args.get("q") or None,
        date_from=request.args.get("from") or None,
        date_to=request.args.get("to") or None,
    )


# The most days one batch request may ask about. A month cell is 42 days at
# worst, so this covers every view the UI has and still refuses a request that
# would make the server do unbounded work.
MAX_DATES = 62


def _availability_by_day(
    all_events: list[dict[str, Any]], dates_arg: str, when: datetime, minutes: int
) -> Any:
    """The window question asked of many days, answered per day.

    "Free at 14:00" is asked of the day on screen, but the month and week views
    draw bookings from every day in the range at once. Answering for the viewed
    day alone and applying that one set to all of them made a room booked every
    day of the month read as free: the answer was about the 3rd, and the
    calendar was showing the whole of September.

    The generalisation is per-day — a booking belongs on screen when its *own*
    room is free for the window on its *own* day. That is still app.avail, asked
    once per day, so there is one predicate and one answer, rather than a second
    implementation in JavaScript that would drift from this one.
    """
    days = [d.strip() for d in (dates_arg or "").split(",") if d.strip()]
    if not days or len(days) > MAX_DATES:
        return jsonify(
            {"error": f"dates is 1-{MAX_DATES} comma-separated YYYY-MM-DD dates"}
        ), 400
    for d in days:
        try:
            # Strict on purpose: "2026-3-1" parses but is not the shape the
            # client sends, and a batch is the wrong place to guess.
            if date.fromisoformat(d).isoformat() != d:
                raise ValueError(d)
        except ValueError:
            return jsonify(
                {"error": "dates is comma-separated YYYY-MM-DD dates"}
            ), 400

    clock = when.strftime("%H:%M")
    wanted = set(days)
    # Seeded with every active room, for the same reason the single-day form
    # seeds it: a room with nothing booked is free, and it is the one you want.
    active = [r["room"] for r in _active_rooms(all_events)]
    by_day: dict[str, dict[str, list[dict[str, Any]]]] = {
        d: {room: [] for room in active} for d in days
    }
    for ev in all_events:
        # The row's own date — the same field the single-day form buckets on,
        # so the two cannot disagree about which day a booking belongs to.
        day = ev.get("date")
        if day in wanted:
            by_day[day].setdefault(ev["room"], []).append(ev)

    out: dict[str, Any] = {}
    for day in days:
        # The clock time is the caller's; the date is this day's. Asking each
        # day at its own date is the whole point of the batch.
        start = datetime.fromisoformat(f"{day}T{clock}:00")
        start_iso = start.isoformat()
        end_iso = (start + timedelta(minutes=minutes)).isoformat()
        free: list[str] = []
        bookings = 0
        for room, rows in by_day[day].items():
            rows.sort(key=lambda e: e.get("start") or "")
            bookings += len(rows)
            busy = (avail.busy_between(rows, start_iso, end_iso) if minutes
                    else avail.busy_at(rows, start_iso))
            if not busy:
                free.append(room)
        out[day] = {"free": sorted(free), "total_bookings": bookings}

    return jsonify({"at": clock, "minutes": minutes, "days": out})


def _active_rooms(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rooms that actually appear in current data, sorted by floor then name."""
    from app.rooms import describe

    seen = {e["room"] for e in events if e.get("room")}
    rooms = [describe(r) for r in seen]
    rooms.sort(key=lambda r: (floor_sort_key(r["floor"]), r["room"]))
    return rooms


def _human_when(raw: str | None) -> str:
    if not raw:
        return "never"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return "never"

    delta = datetime.now() - dt
    if delta.total_seconds() < 120:
        return "just now"
    if dt.date() == datetime.now().date():
        return f"today {dt:%H:%M}"
    if (datetime.now().date() - dt.date()).days == 1:
        return f"yesterday {dt:%H:%M}"
    return f"{dt:%b %d, %H:%M}"


def _backfill_label(run: dict[str, Any] | None) -> dict[str, Any] | None:
    """Last backfill, shaped for the sidebar."""
    if not run:
        return None
    message = run.get("message") or ""
    # The run row is where the empty and errored counts end up: they are notes
    # on an otherwise clean run rather than a status of their own. Split the
    # parenthetical out so the sidebar can show it without reprinting the
    # booking count the line above already gives.
    note = ""
    if message.endswith(")") and "(" in message:
        note = message[message.index("(") + 1:-1]
    return {
        "when": _human_when(run.get("finished_at")),
        "status": run.get("status"),
        "events": run.get("events_count") or 0,
        "message": message,
        "note": note,
    }


def _range_label(date_from: str | None, date_to: str | None) -> str:
    if not date_from or not date_to:
        return ""
    return f"{date_from} → {date_to}"
