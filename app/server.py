"""
Local web UI — serves the calendar and a small JSON API on 127.0.0.1.

Bound to loopback only. This process has a live LSM session behind it, so
it must not be reachable from the network.

Loopback is a reachability boundary, not an authentication one, and the two
are not the same thing. Two checks stand on top of it: a per-launch key
(below) and an origin check. The key alone does not stop the browser that
holds it being steered: a page the user happens to have open elsewhere can
POST a form to this port, a form POST is not subject to a CORS preflight,
and the key cookie may ride along (see KEY_PARAM). That does not reach the
LSM session (which lives in the Chromium profile, not in this process), but
it does reach the control plane.

So on state-changing methods a request that announces any origin but this
server's own — scheme, host and port — is refused. See
_refuse_cross_site_writes.

What that guards is worth naming, because the endpoints are not all the same
size. A cross-site POST could start a scrape, or pop a real login window at
the university. It could also log the user out — and now that /api/logout
clears the profile's cookies rather than only the snapshot, that ends a
working session and costs a UTORid login and a Duo approval to restore. None
of them reaches the LSM *data* (this app is read-only), and the refusal is
what keeps all of them out of reach of a page the user merely has open.
The update endpoints belong in that list too: without the guard, a page
open anywhere could offer the app a real installer and end its process.

And one *is* authentication, because loopback is shared by every account on
the machine. On a PC two people sign in to, the first person's instance holds
their LSM session and their calendar on 127.0.0.1, and nothing about the port
says whose it is: a second person's app used to open its window on the first
person's server. So the running app mints a per-launch key; the window and
the tray open the UI through a URL carrying it, the server trades it for an
HttpOnly cookie, and without that cookie (or the key as a header, for the
app's own handshake) every route but the favicon answers 401. See
_require_key. create_app without a key — the test clients — serves openly.
"""

from __future__ import annotations

import hmac
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from urllib.parse import urlencode, urlsplit

from flask import (Flask, Response, jsonify, redirect, request,
                   send_from_directory)

from app import avail, store, updater
from app.config import APP_NAME, BROWSER_NAME, WEB_DIR, log
from app.icon import paint
from app.parse import split_title
from app.rooms import floor_sort_key

# Hosts that mean "this machine". The UI is reachable as 127.0.0.1 and, if a
# person typed it, as localhost; both are the same app.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# The favicon, built on the first request that wants one rather than at import:
# it is the only thing here that needs PIL, and no other request should pay for
# it. Module scope so it is built once instead of per request.
_FAVICON: bytes | None = None

# The query parameter and header that carry the per-launch key. The cookie it
# is traded for is named per port, because cookies are scoped to the host and
# not the port: two instances one person runs side by side (LSM_PORT) must not
# overwrite each other's.
#
# The same fact is a known leak, left in place deliberately: the browser
# sends lsm_key_<port> with every request to 127.0.0.1 or localhost on ANY
# port (RFC 6265 §8.5), and SameSite treats every port as the same site. So a
# local server the same browser visits — a dev server, Jupyter — receives the
# key in its Cookie header, and a process holding it can call this API
# directly. Pages on such a server still cannot make the *browser* write here
# (the origin check refuses another port); the leak is to the process. There
# is no small fix: cookies have no port attribute (even __Host- is not
# port-bound), and scoping by Path would mean moving every route under a
# per-launch prefix. The key is per launch, and in the shared-PC case it
# guards, the other account's server would also need this browser to visit it.
KEY_PARAM = "k"
KEY_HEADER = "X-LSM-Key"


def key_cookie_name(port: int | None) -> str:
    return f"lsm_key_{port}" if port else "lsm_key"


def _origin_of(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) of a URL, with the scheme's default port filled in,
    so "http://localhost" and "http://localhost:80" compare equal."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ("", "", None)
    scheme = parts.scheme.lower()
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return (scheme, (parts.hostname or "").lower(), port)


def create_app(orchestrator: Any, *, access_key: str | None = None,
               instance_id: str | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False

    def _same(given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given, access_key or "")

    @app.before_request
    def _require_key() -> Any:
        """
        Serve only the person whose app this is. See the module docstring.

        The key arrives three ways: as ?k= on the URL the window or tray
        opened (traded here for the cookie, then redirected to the clean URL
        so the key does not sit in the address bar or the page's history), as
        the cookie on every request after that, and as a header on the app's
        own handshake. Anything else is refused — including a bookmark of the
        bare address, which gets a page saying where to open it from.
        """
        if access_key is None or request.path == "/favicon.ico":
            return None
        # Named from the port this request arrived on, which is the port
        # actually bound — a fallback bind is not known until after it.
        cookie = key_cookie_name(urlsplit(request.host_url).port)
        if _same(request.args.get(KEY_PARAM)):
            rest = {k: v for k, v in request.args.items(multi=True)
                    if k != KEY_PARAM}
            target = request.path + (("?" + urlencode(rest)) if rest else "")
            resp = redirect(target, code=303)
            resp.set_cookie(cookie, access_key, httponly=True,
                            samesite="Strict", path="/")
            return resp
        if _same(request.cookies.get(cookie)) or _same(
                request.headers.get(KEY_HEADER)):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"status": "unauthorized",
                            "message": "Open the calendar from the app's "
                                       "tray icon"}), 401
        return Response(
            f"<!doctype html><title>{APP_NAME}</title>"
            f"<p style='font:16px system-ui;margin:3em'>This address belongs "
            f"to a running {APP_NAME}, but not to this window.<br>Open the "
            f"calendar from the app's icon in the notification area "
            f"(or the Start menu).</p>",
            status=401, mimetype="text/html")

    @app.before_request
    def _refuse_cross_site_writes() -> Any:
        """
        Refuse a state-changing request that another site's page originated.

        A page open elsewhere can POST a form to this port, and a form POST is
        not subject to a CORS preflight, so the browser will not stop it. What
        the browser *does* do is announce the origin: Origin is sent on every
        non-GET request, this app's own fetches included, so anything but
        this server's own origin is grounds to refuse.

        "Own origin" means scheme, host AND port. Any loopback host used to
        pass, which let a page on another local server — a dev server, a
        Jupyter notebook on 127.0.0.1:8888 — post here with the key cookie
        attached, since cookies and SameSite both ignore the port. Comparing
        with request.host_url keeps 127.0.0.1 and localhost both working: a
        page's Origin is whatever host it was opened as, and so is its Host.
        Sec-Fetch-Site, where the browser sends it, is checked as well: a
        page on another port is "same-site", which is not "same-origin".

        An ABSENT origin is left alone rather than guessed at. `curl`, the
        Flask test client and the packaged `--selftest` all send nothing, and
        treating "unknown" as "hostile" would break every one of them to close
        a hole they cannot open — a cross-site attacker using a form or a
        no-cors fetch always arrives carrying one. That is the honest limit of
        this check and the reason it is a refusal rather than a control: it
        stops other pages driving this app, and it does not pretend to
        authenticate anybody.

        Read-only requests are deliberately untouched. There is nothing to
        protect: nothing here is secret, and no GET changes state.
        """
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
        fetch_site = request.headers.get("Sec-Fetch-Site")
        if not origin and fetch_site is None:
            return None
        own = _origin_of(request.host_url)
        # "Origin: null" (a sandboxed frame) parses to no host at all, so it
        # never equals our own and is refused, which is what it deserves.
        if ((not origin or _origin_of(origin) == own)
                and own[1] in _LOOPBACK_HOSTS
                and fetch_site in (None, "same-origin", "none")):
            return None
        log.warning("refused %s %s from origin %s (Sec-Fetch-Site %s)",
                    request.method, request.path, origin[:120], fetch_site)
        return jsonify({"status": "forbidden",
                        "message": "Cross-site request refused"}), 403

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
        """The app's mark, built from app/icon.py rather than shipped as a file.

        This was a bare 204, which is a valid answer and leaves the browser tab
        blank. Generating it here keeps the tab, the tray and the exe showing
        one mark from one function, and avoids committing a second copy of a
        binary that would then be the one that goes stale.
        """
        global _FAVICON
        if _FAVICON is None:
            import io

            buf = io.BytesIO()
            paint(32).save(buf, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
            _FAVICON = buf.getvalue()
        return Response(_FAVICON, mimetype="image/x-icon")

    # ── Data ─────────────────────────────────────────────────────────────

    @app.get("/api/bootstrap")
    def bootstrap() -> Any:
        """
        Everything the calendar needs in one round trip: bookings, the
        room list actually present in the data, and the group definitions.
        """
        events = store.get_events()
        rooms = _active_rooms(e["room"] for e in events)
        return jsonify({
            "events": [_public_event(e) for e in events],
            "rooms": [r["room"] for r in rooms],
            "room_meta": {r["room"]: r for r in rooms},
            "groups": store.load_groups(),
            "count": len(events),
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

        # The store answers in SQL rather than over a table read into Python:
        # this runs on every keystroke. The matches are the same ones search
        # finds, because both go through _like — a suggestion that search then
        # came up empty for was the failure mode.
        rooms = store.suggest_rooms(q)
        titles = sorted({split_title(t)[0] for t in store.suggest_titles(q)})
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

        Answers two questions through one predicate: the Free-now drawer,
        which passes nothing, and the filter panel's "free at a time",
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
        # for — it is a question about the bookings of one date and the first
        # hours of the next.
        minutes = max(0, min(request.args.get("for", 0, type=int) or 0, 24 * 60))
        when_iso = when.isoformat()
        day_iso = when.strftime("%Y-%m-%d")

        # A window can run past midnight, and the end day decides what to
        # read: "free from 23:00 for 180 min" ends at 02:00 the *next* day,
        # and a booking at 00:30 tomorrow decides whether the room is free
        # for it. That is the one answer here that sends someone to a room
        # that is already taken, so the predicate is asked about both days
        # and left to decide, rather than the window being refused whenever
        # it crosses midnight (an hour from 23:00 stops at midnight and is
        # still free).
        end_day = (when + timedelta(minutes=minutes)).strftime("%Y-%m-%d")

        dates_arg = request.args.get("dates")
        if dates_arg is not None:
            # The batch form gets the same narrowing across its whole span:
            # one read bounded to [earliest, latest + 1], which covers every
            # day a window can touch. Dates that do not parse are left to
            # _availability_by_day to reject with its own 400 — the bounds
            # are an optimisation, not a second validator.
            days = [d.strip() for d in dates_arg.split(",") if d.strip()]
            try:
                lo = min(date.fromisoformat(d) for d in days)
                hi = max(date.fromisoformat(d) for d in days)
                batch_events = store.get_events(
                    date_from=lo.strftime("%Y-%m-%d"),
                    date_to=(hi + timedelta(days=1)).strftime("%Y-%m-%d"))
            except ValueError:
                batch_events = store.get_events()
            return _availability_by_day(batch_events, dates_arg, when, minutes)

        # Read only the days the answer is about, not the whole table. This
        # endpoint is polled every 60 seconds, and the table is a rolling
        # year — the full read spent a minute's worth of row objects to
        # answer a question about one or two days.
        rows = store.get_events(date_from=day_iso, date_to=end_day)
        events = [e for e in rows if e.get("date") == day_iso]
        # The window's rows are the same read: the day's own plus the next
        # day's when the window reaches into it, exactly the span read.
        window_events = (events if end_day == day_iso else rows)

        # Seed every known room, not just the ones with a booking that day.
        # Building this map from the day's bookings alone meant a room with a
        # completely empty day was absent from the response entirely — so the
        # emptiest rooms, the ones you actually want, were the ones missing.
        # rooms_present() is the distinct-room read that makes the narrowed
        # window above safe: it still sees rooms whose only bookings are
        # months away.
        by_room: dict[str, list[dict[str, Any]]] = {
            r["room"]: [] for r in _active_rooms(store.rooms_present())
        }
        # The predicate's view of the room: the same rows, plus the next day's
        # when the window reaches into it. Kept apart from by_room because the
        # payload's `bookings`, `total_bookings` and `next` are about the day
        # this response names, and only the free/busy answer is about the
        # window.
        win_room: dict[str, list[dict[str, Any]]] = {room: [] for room in by_room}
        for ev in events:
            by_room.setdefault(ev["room"], []).append(ev)
        for ev in window_events:
            win_room.setdefault(ev["room"], []).append(ev)

        until_iso = (when + timedelta(minutes=minutes)).isoformat()
        rooms = []
        for room, bookings in by_room.items():
            bookings.sort(key=lambda e: e.get("start") or "")
            rows = win_room.get(room, bookings)
            rows.sort(key=lambda e: e.get("start") or "")
            current = (avail.busy_between(rows, when_iso, until_iso) if minutes
                       else avail.busy_at(rows, when_iso))
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
            # Which launch answered: the app's own handshake insists on its
            # id, so it can never mistake another process's server for its own.
            "instance": instance_id,
            "browser": BROWSER_NAME,
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
            # The updater's state, including current_version — this is the
            # only route the UI needs for everything from the version line
            # in the sidebar to the install banner.
            "update": st.get("update"),
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
            "changes": [_public_change(r) for r in rows],
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
        """Sign out of LSM, for real, and report what the probe found after.

        Queued like /api/login, and for the same reason: it drives the Chromium
        profile the worker owns, so it must not open a second browser on it from
        a request thread. The "started" reply means the request was accepted,
        not that the session is gone — the sidebar re-reads /api/status for
        that, which is what makes the outcome the measured one.
        """
        if orchestrator.is_busy():
            return jsonify({"status": "busy",
                            "message": "Something else is running"}), 409
        orchestrator.request_logout()
        return jsonify({"status": "started", "message": "Signing out of LSM"})

    # ── Updates ──────────────────────────────────────────────────────────

    @app.post("/api/update/check")
    def update_check() -> Any:
        if orchestrator.is_busy():
            return jsonify({"status": "busy",
                            "message": "Something else is running"}), 409
        orchestrator.request_update_check()
        return jsonify({"status": "started",
                        "message": "Checking for updates"})

    @app.post("/api/update/install")
    def update_install() -> Any:
        if orchestrator.is_busy():
            return jsonify({"status": "busy",
                            "message": "Something else is running"}), 409
        orchestrator.request_update_install()
        return jsonify({"status": "started",
                        "message": "Downloading the update"})

    @app.post("/api/update/skip")
    def update_skip() -> Any:
        """Hide this release until a strictly newer one ships.

        The version comes from the orchestrator's own status, not from the
        request: the server is the one party that knows what was actually
        offered, and a client dictating what to skip could skip a version it
        was never shown — or invent one that hides a future release.
        """
        if orchestrator.is_busy():
            return jsonify({"status": "busy",
                            "message": "Something else is running"}), 409
        update = (orchestrator.status().get("update") or {})
        if update.get("state") != "available" or not update.get("latest_version"):
            return jsonify({"status": "error",
                            "message": "Nothing is available to skip"}), 400
        state = store.load_update_state()
        state["skipped"] = update["latest_version"]
        store.save_update_state(state)
        # The store write makes the skip survive a restart; this makes it
        # visible now. Without it the banner, the Install button and the
        # Skip link stay on screen until the next check — up to 24 h — and
        # the click that was supposed to hide the offer appears to have
        # done nothing.
        orchestrator.skip_update(update["latest_version"])
        return jsonify({"status": "ok",
                        "skipped": update["latest_version"]})

    @app.post("/api/update/token")
    def update_token() -> Any:
        """Store a read-only GitHub token for the private repo, DPAPI-encrypted.

        The reply says whether a token is set, never what it is: echoing it
        back would put the credential in a response body that browser
        tooling and any page script could read, which is the one place the
        token must never go. The value lives in the body as JSON because a
        password field's paste can carry characters form encoding would
        mangle.
        """
        body = request.get_json(silent=True) or {}
        token = body.get("token")
        if not isinstance(token, str):
            return jsonify({"status": "error",
                            "message": "No token was sent"}), 400
        try:
            updater.save_token(token)
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400
        except OSError as exc:
            # dpapi.protect refusing — no encrypted store means no store:
            # better a clear refusal than a token written in the clear.
            return jsonify({"status": "error",
                            "message": f"The token could not be stored "
                                       f"encrypted ({exc})"}), 400
        return jsonify({"status": "ok", "token_set": True})

    @app.delete("/api/update/token")
    def update_token_clear() -> Any:
        updater.clear_token()
        return jsonify({"status": "ok", "token_set": False})

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


# The store keeps the title exactly as LSM wrote it — it is the change feed's
# booking identity, so rewriting it at ingest would give every stored booking
# a new identity and flood the feed once. The code prefix
# ("208/CIBC.1/A.MAHAJAN") is parsed out on the way *out*, at every point a
# title reaches the UI, and the who/what after the name travels beside it as
# `booked_by`. Nothing is lost: the raw title stays in the database and still
# answers search, because a search for any part of it is a search over the
# same string the display was built from.
def _public_event(ev: dict[str, Any]) -> dict[str, Any]:
    name, booked_by = split_title(ev.get("title") or "")
    out = dict(ev)
    out["title"] = name
    out["booked_by"] = booked_by
    return out


def _public_change(row: dict[str, Any]) -> dict[str, Any]:
    name, booked_by = split_title(row.get("title") or "")
    out = dict(row)
    out["title"] = name
    out["booked_by"] = booked_by
    return out


# The most days one batch request may ask about. A month cell is 42 days at
# worst, so this covers every view the UI has and still refuses a request that
# would make the server do unbounded work.
MAX_DATES = 62


def _availability_by_day(
    events: list[dict[str, Any]], dates_arg: str, when: datetime, minutes: int
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

    The rows a day's window is asked about are that day's and, when a length is
    given and the window runs past midnight, the next day's — as in the
    single-day form, and for the same reason.
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
    # Seeded with every active room, for the same reason the single-day form
    # seeds it: a room with nothing booked is free, and it is the one you want.
    active = [r["room"] for r in _active_rooms(store.rooms_present())]
    # The days a window opened on each asked-for date can touch — itself, and
    # the next when it runs past midnight. Same reason as the single-day form:
    # the rows were bucketed by the day the question opened on, so an 00:30
    # booking was invisible to "free from 23:00 for 180 min".
    spans: dict[str, list[str]] = {}
    for d in days:
        end = datetime.fromisoformat(f"{d}T{clock}:00") + timedelta(minutes=minutes)
        spans[d] = [d] if end.strftime("%Y-%m-%d") == d else [
            d, end.strftime("%Y-%m-%d")
        ]
    # Read once for every date any of the spans needs, rather than per day.
    rows_on: dict[str, list[dict[str, Any]]] = {
        d: [] for d in {day for span in spans.values() for day in span}
    }
    for ev in events:
        # The row's own date — the same field the single-day form buckets on,
        # so the two cannot disagree about which day a booking belongs to.
        day = ev.get("date")
        if day in rows_on:
            rows_on[day].append(ev)

    out: dict[str, Any] = {}
    for day in days:
        # The clock time is the caller's; the date is this day's. Asking each
        # day at its own date is the whole point of the batch.
        start = datetime.fromisoformat(f"{day}T{clock}:00")
        start_iso = start.isoformat()
        end_iso = (start + timedelta(minutes=minutes)).isoformat()
        # The day's own rows are what gets counted; the predicate also sees the
        # next day's when the window reaches into it, so the count stays a
        # count of the day the entry is about.
        own = rows_on[day]
        window_rows = [e for d in spans[day] for e in rows_on[d]]
        own_by_room: dict[str, list[dict[str, Any]]] = {room: [] for room in active}
        win_by_room: dict[str, list[dict[str, Any]]] = {room: [] for room in active}
        for ev in own:
            own_by_room.setdefault(ev["room"], []).append(ev)
        for ev in window_rows:
            win_by_room.setdefault(ev["room"], []).append(ev)
        free: list[str] = []
        for room, bookings in own_by_room.items():
            rows = win_by_room.get(room, bookings)
            rows.sort(key=lambda e: e.get("start") or "")
            busy = (avail.busy_between(rows, start_iso, end_iso) if minutes
                    else avail.busy_at(rows, start_iso))
            if not busy:
                free.append(room)
        out[day] = {"free": sorted(free), "total_bookings": len(own)}

    return jsonify({"at": clock, "minutes": minutes, "days": out})


def _active_rooms(names: Iterable[str]) -> list[dict[str, Any]]:
    """Rooms that actually appear in current data, sorted by floor then name.

    Takes room names rather than events so /api/today can seed its answer
    from store.rooms_present() — a distinct-room read — while reading only
    the narrowed day window for the bookings themselves.
    """
    from app.rooms import describe

    seen = {r for r in names if r}
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
