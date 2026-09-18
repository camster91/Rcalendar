"""
Local web UI — serves the calendar and a small JSON API on 127.0.0.1.

Bound to loopback only. This process has a live LSM session behind it, so
it must not be reachable from the network; there is deliberately no
authentication because there is no remote surface.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from app import store
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
        if not isinstance(payload, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        store.save_groups(payload)
        return jsonify({"status": "ok", "groups": payload})

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
        """Per-room availability for today — booked vs free, with the next slot."""
        now = datetime.now()
        today_iso = now.strftime("%Y-%m-%d")
        all_events = store.get_events()
        events = [e for e in all_events if e.get("date") == today_iso]

        # Seed every known room, not just the ones with a booking today.
        # Building this map from today's bookings alone meant a room with a
        # completely empty day was absent from the response entirely — so the
        # emptiest rooms, the ones you actually want, were the ones missing.
        by_room: dict[str, list[dict[str, Any]]] = {
            r["room"]: [] for r in _active_rooms(all_events)
        }
        for ev in events:
            by_room.setdefault(ev["room"], []).append(ev)

        now_iso = now.isoformat()
        rooms = []
        for room, bookings in by_room.items():
            bookings.sort(key=lambda e: e.get("start") or "")
            current = next(
                (b for b in bookings
                 if (b.get("start") or "") <= now_iso < (b.get("end") or "")),
                None,
            )
            upcoming = [b for b in bookings if (b.get("start") or "") > now_iso]
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
            "date": today_iso,
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
            "date_range": _range_label(stats.get("date_from"), stats.get("date_to")),
            "total_events": stats.get("total_events", 0),
            "rooms": stats.get("rooms", 0),
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


def _range_label(date_from: str | None, date_to: str | None) -> str:
    if not date_from or not date_to:
        return ""
    return f"{date_from} → {date_to}"
