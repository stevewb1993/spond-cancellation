import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import urllib.request
from functools import wraps

from dotenv import load_dotenv

load_dotenv()
from datetime import datetime, timedelta, timezone

import aiohttp
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from spond.spond import Spond

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

SPOND_USERNAME = os.environ.get("SPOND_USERNAME", "")
SPOND_PASSWORD = os.environ.get("SPOND_PASSWORD", "")
SPOND_CLUB_ID = os.environ.get("SPOND_CLUB_ID", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
DB_PATH = os.path.join(os.path.dirname(__file__), "transfers.db")

# When DATABASE_URL is set (e.g. a hosted Postgres like Neon) we use Postgres;
# otherwise we fall back to a local SQLite file, which is handy for dev and tests.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)

# --- Email / verification config ---
# Verification codes are sent via Brevo's HTTPS API rather than SMTP, because
# hosts like Render block outbound SMTP ports.
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "Bath Amphibians")

# How long a verification code stays valid, and how many guesses are allowed.
CODE_TTL = timedelta(minutes=10)
MAX_CODE_ATTEMPTS = 5


# --- Database ---


def _connect():
    """Open a new connection to the active backend.

    Both psycopg and sqlite3 connections expose the small slice of the DB-API
    this app relies on: ``.execute(query, params)`` returning a cursor,
    ``.commit()`` and ``.close()``. Rows come back keyed by column name in both
    cases (``dict_row`` for psycopg, ``sqlite3.Row`` for SQLite).
    """
    if USE_POSTGRES:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _q(query):
    """Translate ?-style placeholders to the active driver's paramstyle."""
    return query.replace("?", "%s") if USE_POSTGRES else query


def get_db():
    if "db" not in g:
        g.db = _connect()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    # SERIAL on Postgres, AUTOINCREMENT on SQLite; everything else is identical.
    id_column = (
        "id SERIAL PRIMARY KEY"
        if USE_POSTGRES
        else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    db = _connect()
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS transfer_requests (
            {id_column},
            member_name TEXT NOT NULL,
            member_email TEXT NOT NULL,
            cancelled_event_id TEXT NOT NULL,
            cancelled_event_name TEXT NOT NULL,
            target_event_id TEXT NOT NULL,
            target_event_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            processed_at TEXT
        )
    """)
    db.commit()
    db.close()


def get_used_cancelled_event_ids(member_email):
    """Cancelled-event ids this member has already transferred *from*.

    Each cancelled session funds exactly one transfer, so a successful
    ('approved') transfer spends it — it can't be used to claim another.
    """
    db = get_db()
    rows = db.execute(
        _q(
            "SELECT DISTINCT cancelled_event_id FROM transfer_requests "
            "WHERE member_email = ? AND status = 'approved'"
        ),
        (member_email,),
    ).fetchall()
    return {row["cancelled_event_id"] for row in rows}


# --- Spond helpers ---


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def format_event_label(event):
    start = event.get("startTimestamp", "")
    name = event.get("heading", "Unnamed event")
    if start:
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            return f"{name} — {dt.strftime('%a %d %b %Y, %H:%M')}"
        except (ValueError, TypeError):
            pass
    return name


async def _get_club_token(http_session):
    login_url = "https://api.spond.com/club/v1/login"
    data = {"email": SPOND_USERNAME, "password": SPOND_PASSWORD}
    async with http_session.post(login_url, json=data) as r:
        return (await r.json())["loginToken"]


async def _get_transactions_in_range(http_session, club_token, min_date, max_date):
    """Fetch all transactions in a date range."""
    headers = {
        "Authorization": f"Bearer {club_token}",
        "X-Spond-Clubid": SPOND_CLUB_ID,
    }
    url = "https://api.spond.com/club/v1/transactions"
    params = {"minDate": min_date, "maxDate": max_date}

    all_transactions = []
    skip = 0
    while True:
        p = {**params}
        if skip:
            p["skip"] = str(skip)
        async with http_session.get(url, headers=headers, params=p) as r:
            if r.status != 200:
                break
            batch = await r.json()
        if not batch:
            break
        all_transactions.extend(batch)
        skip += len(batch)
        if len(batch) < 25:
            break
    return all_transactions


async def _get_transaction_detail(http_session, club_token, tx_id):
    headers = {
        "Authorization": f"Bearer {club_token}",
        "X-Spond-Clubid": SPOND_CLUB_ID,
    }
    url = f"https://api.spond.com/club/v1/transactions/{tx_id}"
    async with http_session.get(url, headers=headers) as r:
        return await r.json()


async def _get_event_fresh(s, event_id):
    """Fetch a single event straight from the API, bypassing the library cache.

    ``Spond.get_event`` searches ``self.events``, which is populated once and
    never refreshed — so a read right after a write returns the stale,
    pre-write copy. The post-write check below must see the *current* state, so
    we hit the single-event endpoint directly.
    """
    if not s.token:
        await s.login()
    url = f"{s.api_url}sponds/{event_id}"
    async with s.clientsession.get(url, headers=s.auth_headers) as r:
        return await r.json()


async def _accept_without_payment(s, event_id, member_id):
    """Accept a member onto a paid event without charging them, and verify it.

    The library's change_response can't set custom headers. The organizer
    web app bypasses the Stripe payment flow by sending the
    `X-Spond-SkipPayment: true` header (the plain accept now returns an
    HTTP 402 payment intent without it).

    Crucially, this never trusts the write blindly. It checks the HTTP status
    and then re-reads the event to confirm the member actually landed in
    acceptedIds, so a silently-rejected transfer is reported as a failure
    rather than a false success.
    """
    if not s.token:
        await s.login()
    url = f"{s.api_url}sponds/{event_id}/responses/{member_id}"
    headers = {**s.auth_headers, "X-Spond-SkipPayment": "true"}
    async with s.clientsession.put(
        url, headers=headers, json={"accepted": True}
    ) as r:
        body = await r.text()
        if r.status != 200:
            print(f"[transfer] accept rejected: HTTP {r.status} body={body[:300]}")
            raise ValueError(
                f"Spond rejected the transfer (HTTP {r.status}). "
                "You have not been added. Please contact an admin."
            )

    # A 200 alone isn't proof — confirm the change really took effect. Read
    # fresh from the API, not the stale library cache populated pre-transfer.
    event = await _get_event_fresh(s, event_id)
    responses = event.get("responses", {})
    if member_id in responses.get("acceptedIds", []):
        return responses
    if member_id in responses.get("waitinglistIds", []):
        raise ValueError(
            "That session looks full — you've been put on the waiting list "
            "rather than confirmed. Please contact an admin."
        )
    counts = {k: len(v) for k, v in responses.items() if isinstance(v, list)}
    print(
        f"[transfer] post-check failed: {member_id} not in acceptedIds for "
        f"{event_id}; response buckets={counts}"
    )
    raise ValueError(
        "The transfer didn't take effect in Spond — you have not been added. "
        "Please contact an admin."
    )


async def _find_cancelled_paid_events(email):
    """Find events where the member declined after paying.

    Returns a list of dicts with event info and amount paid.
    """
    s = Spond(SPOND_USERNAME, SPOND_PASSWORD)
    try:
        person = await s.get_person(email)
        member_id = person["id"]
        profile_id = person["profile"]["id"]
        member_name = f"{person['firstName']} {person['lastName']}"

        events = await s.get_events(
            min_end=datetime.now(timezone.utc) - timedelta(days=30),
            max_events=100,
        )
        events = events or []

        # Find paid events where member has declined
        declined_events = []
        for event in events:
            if not event.get("payment"):
                continue
            declined_ids = event.get("responses", {}).get("declinedIds", [])
            if member_id not in declined_ids:
                continue
            declined_events.append(event)

        if not declined_events:
            return [], member_name

        # Check transactions to find which declined events were paid for
        earliest_event = min(
            declined_events, key=lambda e: e["startTimestamp"]
        )
        earliest_date = datetime.fromisoformat(
            earliest_event["startTimestamp"].replace("Z", "+00:00")
        ).date()
        min_date = (earliest_date - timedelta(days=30)).isoformat()
        max_date = datetime.now(timezone.utc).date().isoformat()

        async with aiohttp.ClientSession() as http_session:
            club_token = await _get_club_token(http_session)
            transactions = await _get_transactions_in_range(
                http_session, club_token, min_date, max_date
            )

            # Get details for transactions matching declined event names
            declined_headings = {e["heading"] for e in declined_events}
            member_txns = []
            for tx in transactions:
                if tx.get("paymentName") not in declined_headings:
                    continue
                detail = await _get_transaction_detail(
                    http_session, club_token, tx["id"]
                )
                if (
                    detail.get("paidById") == profile_id
                    and detail.get("status") == "FULFILLED"
                ):
                    member_txns.append(detail)

        # Match each transaction to the closest future event with the
        # same name. Each event and transaction can only be used once.
        matched_event_ids = set()
        matched_tx_ids = set()
        results = []

        for tx in member_txns:
            if tx["id"] in matched_tx_ids:
                continue
            paid_date = datetime.fromisoformat(
                tx["paidAt"].replace("Z", "+00:00")
            ).date()

            # Find the closest event after payment with the same name
            best_event = None
            best_gap = None
            for event in declined_events:
                if event["id"] in matched_event_ids:
                    continue
                if event["heading"] != tx["paymentName"]:
                    continue
                event_date = datetime.fromisoformat(
                    event["startTimestamp"].replace("Z", "+00:00")
                ).date()
                if paid_date > event_date:
                    continue
                gap = (event_date - paid_date).days
                if best_gap is None or gap < best_gap:
                    best_event = event
                    best_gap = gap

            if best_event is not None:
                matched_event_ids.add(best_event["id"])
                matched_tx_ids.add(tx["id"])
                results.append({
                    "event_id": best_event["id"],
                    "label": format_event_label(best_event),
                    "amount_paid": tx["total"],
                })

        return results, member_name
    finally:
        await s.clientsession.close()


async def _get_matching_events(amount):
    """Get future paid events that cost exactly the given amount."""
    s = Spond(SPOND_USERNAME, SPOND_PASSWORD)
    try:
        now = datetime.now(timezone.utc)
        events = await s.get_events(
            min_end=now,
            max_start=now + timedelta(days=7),
            max_events=50,
        )
        matching = [
            event
            for event in events or []
            if event.get("payment", {}).get("total") == amount
        ]
        # Show the soonest sessions first.
        matching.sort(key=lambda e: e.get("startTimestamp", ""))
        return [
            {"id": event["id"], "label": format_event_label(event)}
            for event in matching
        ]
    finally:
        await s.clientsession.close()


async def _do_transfer(email, cancelled_event_id, target_event_id):
    """Execute the transfer. Re-verifies everything before acting."""
    s = Spond(SPOND_USERNAME, SPOND_PASSWORD)
    try:
        person = await s.get_person(email)
        member_id = person["id"]
        profile_id = person["profile"]["id"]

        cancelled_event = await s.get_event(cancelled_event_id)
        target_event = await s.get_event(target_event_id)

        # Verify declined
        declined_ids = cancelled_event.get("responses", {}).get("declinedIds", [])
        if member_id not in declined_ids:
            raise ValueError(
                "You don't appear to have cancelled your spot on that session. "
                "Make sure you've declined the session in Spond first."
            )

        # Verify prices match
        target_price = target_event.get("payment", {}).get("total", 0)

        # Verify payment
        event_date = datetime.fromisoformat(
            cancelled_event["startTimestamp"].replace("Z", "+00:00")
        ).date()
        min_date = (event_date - timedelta(days=30)).isoformat()
        max_date = event_date.isoformat()

        async with aiohttp.ClientSession() as http_session:
            club_token = await _get_club_token(http_session)
            transactions = await _get_transactions_in_range(
                http_session, club_token, min_date, max_date
            )
            amount_paid = None
            for tx in transactions:
                if tx.get("paymentName") != cancelled_event["heading"]:
                    continue
                detail = await _get_transaction_detail(
                    http_session, club_token, tx["id"]
                )
                if (
                    detail.get("paidById") == profile_id
                    and detail.get("status") == "FULFILLED"
                ):
                    paid_date = datetime.fromisoformat(
                        detail["paidAt"].replace("Z", "+00:00")
                    ).date()
                    if paid_date <= event_date:
                        amount_paid = detail["total"]
                        break

        if amount_paid is None:
            raise ValueError(
                "We couldn't find a payment record for that session. "
                "If you believe this is an error, please contact an admin."
            )

        if amount_paid != target_price:
            raise ValueError(
                f"The session prices don't match "
                f"(£{amount_paid / 100:.2f} vs £{target_price / 100:.2f}). "
                f"You can only transfer to a session that costs exactly the same."
            )

        result = await _accept_without_payment(s, target_event_id, member_id)
        return result
    finally:
        await s.clientsession.close()


async def _lookup_member(email):
    """Return the member's full name if the email belongs to the club.

    Raises KeyError if no club member matches the email. The Spond admin
    account only has visibility of its own club's members, so a successful
    lookup is what establishes club membership.
    """
    s = Spond(SPOND_USERNAME, SPOND_PASSWORD)
    try:
        person = await s.get_person(email)
        return f"{person['firstName']} {person['lastName']}"
    finally:
        await s.clientsession.close()


# --- Authentication ---


def _hash_code(code):
    """Salted hash of a verification code so the raw code never sits in the
    session cookie."""
    salted = f"{app.secret_key}:{code}".encode()
    return hashlib.sha256(salted).hexdigest()


def generate_code():
    """A 6-digit numeric one-time code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def send_verification_email(to_email, code):
    """Email a one-time verification code via the Brevo HTTPS API.

    Uses HTTPS (port 443) rather than SMTP so it works on hosts that block
    outbound SMTP. Raises on a network error or non-2xx response, which the
    caller turns into a friendly "couldn't send" message.
    """
    minutes = int(CODE_TTL.total_seconds() // 60)
    payload = {
        "sender": {"email": EMAIL_FROM, "name": EMAIL_FROM_NAME},
        "to": [{"email": to_email}],
        "subject": "Your Bath Amphibians verification code",
        "textContent": (
            f"Your Bath Amphibians session-transfer verification code is:\n\n"
            f"    {code}\n\n"
            f"It expires in {minutes} minutes. If you didn't request this, "
            f"you can ignore this email."
        ),
    }
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode(),
        headers={
            "api-key": BREVO_API_KEY,
            "content-type": "application/json",
            "accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def _clear_pending():
    """Drop the in-progress verification state from the session."""
    for key in (
        "pending_email", "pending_name", "code_hash",
        "code_expires", "code_attempts",
    ):
        session.pop(key, None)


def login_required(view):
    """Guard a view so only members who've verified their email can reach it."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated") or not session.get("email"):
            flash("Please verify your email to continue.", "error")
            return redirect(url_for("step_email"))
        return view(*args, **kwargs)

    return wrapped


# --- Routes ---


@app.route("/", methods=["GET", "POST"])
def step_email():
    # Already-verified members skip straight to their sessions.
    if session.get("authenticated") and session.get("email"):
        return redirect(url_for("step_cancelled"))

    if request.method == "POST":
        email = request.form.get("member_email", "").strip()
        if not email:
            flash("Please enter your email.", "error")
            return render_template("step_email.html")

        # Requirement 1: only people in the club can get in. The Spond admin
        # account only sees its own club's members, so a failed lookup means
        # they're not a member.
        try:
            member_name = run_async(_lookup_member(email))
        except KeyError:
            flash(
                "We couldn't find that email in the club. "
                "Make sure you're using the email registered with your Spond account.",
                "error",
            )
            return render_template("step_email.html")

        # Requirement 2: prove they own the email before acting on their
        # behalf, by emailing a one-time code to that address.
        code = generate_code()
        try:
            send_verification_email(email, code)
        except Exception:
            flash(
                "We couldn't send a verification email right now. "
                "Please try again shortly, or contact an admin.",
                "error",
            )
            return render_template("step_email.html")

        session.pop("authenticated", None)
        session["pending_email"] = email
        session["pending_name"] = member_name
        session["code_hash"] = _hash_code(code)
        session["code_expires"] = (
            datetime.now(timezone.utc) + CODE_TTL
        ).isoformat()
        session["code_attempts"] = 0
        flash(f"We've emailed a 6-digit verification code to {email}.", "success")
        return redirect(url_for("step_verify"))

    return render_template("step_email.html")


@app.route("/verify", methods=["GET", "POST"])
def step_verify():
    if "pending_email" not in session:
        return redirect(url_for("step_email"))

    if request.method == "POST":
        expires = datetime.fromisoformat(session["code_expires"])
        if datetime.now(timezone.utc) > expires:
            _clear_pending()
            flash("That code has expired. Please request a new one.", "error")
            return redirect(url_for("step_email"))

        if session.get("code_attempts", 0) >= MAX_CODE_ATTEMPTS:
            _clear_pending()
            flash(
                "Too many incorrect attempts. Please request a new code.",
                "error",
            )
            return redirect(url_for("step_email"))

        entered = request.form.get("code", "").strip()
        if hmac.compare_digest(_hash_code(entered), session.get("code_hash", "")):
            email = session["pending_email"]
            member_name = session.get("pending_name", "")
            _clear_pending()
            session["authenticated"] = True
            session["email"] = email
            session["member_name"] = member_name
            # Finding cancelled sessions is slow, so show a loading page
            # while it runs; the actual lookup happens on /cancelled.
            return redirect(url_for("loading"))

        session["code_attempts"] = session.get("code_attempts", 0) + 1
        flash("That code wasn't correct. Please try again.", "error")

    return render_template("step_verify.html", email=session["pending_email"])


@app.route("/loading")
@login_required
def loading():
    return render_template("loading.html")


@app.route("/cancelled", methods=["GET", "POST"])
@login_required
def step_cancelled():
    # Reload the member's cancelled sessions if they're not in the session
    # (e.g. after a completed transfer or a direct visit).
    if "cancelled_events" not in session:
        try:
            cancelled, member_name = run_async(
                _find_cancelled_paid_events(session["email"])
            )
        except KeyError:
            cancelled, member_name = [], session.get("member_name", "")
        session["cancelled_events"] = cancelled
        session["member_name"] = member_name

    # A cancelled session can only fund one transfer — hide any already spent.
    used = get_used_cancelled_event_ids(session["email"])
    cancelled_events = [
        e for e in session["cancelled_events"] if e["event_id"] not in used
    ]

    if request.method == "POST":
        cancelled_id = request.form.get("cancelled_event")
        selected = next(
            (e for e in cancelled_events if e["event_id"] == cancelled_id), None
        )
        if not selected:
            flash("Please select a session.", "error")
        else:
            target_events = run_async(
                _get_matching_events(selected["amount_paid"])
            )
            # Exclude the cancelled event itself
            target_events = [
                e for e in target_events if e["id"] != cancelled_id
            ]
            if not target_events:
                flash(
                    f"There are no upcoming sessions at "
                    f"£{selected['amount_paid'] / 100:.2f} to transfer to.",
                    "error",
                )
            else:
                session["cancelled_event_id"] = cancelled_id
                session["cancelled_event_label"] = selected["label"]
                session["amount_paid"] = selected["amount_paid"]
                session["target_events"] = target_events
                return redirect(url_for("step_target"))

    return render_template("step_cancelled.html", events=cancelled_events)


@app.route("/target", methods=["GET", "POST"])
@login_required
def step_target():
    required = ["cancelled_event_id", "target_events"]
    if not all(k in session for k in required):
        return redirect(url_for("step_cancelled"))

    target_events = session["target_events"]

    if request.method == "POST":
        target_id = request.form.get("target_event")
        selected = next(
            (e for e in target_events if e["id"] == target_id), None
        )
        if not selected:
            flash("Please select a session.", "error")
        else:
            email = session["email"]
            cancelled_id = session["cancelled_event_id"]

            # Re-check here too: the cancelled session must not already be
            # spent (guards against stale sessions and double submits).
            if cancelled_id in get_used_cancelled_event_ids(email):
                for key in [
                    "cancelled_events", "cancelled_event_id",
                    "cancelled_event_label", "amount_paid", "target_events",
                ]:
                    session.pop(key, None)
                flash(
                    "You've already used that cancelled session to transfer to "
                    "another session. Each cancelled session can only be used once.",
                    "error",
                )
                return redirect(url_for("step_cancelled"))

            now = datetime.now(timezone.utc).isoformat()
            try:
                run_async(_do_transfer(email, cancelled_id, target_id))
                status = "approved"
                flash(
                    f"Done! You've been added to {selected['label']}.",
                    "success",
                )
            except ValueError as e:
                # Expected, user-actionable failures carry a friendly message
                # (declined check, price mismatch, transfer didn't take, etc.).
                status = "failed"
                flash(f"Couldn't complete the transfer: {e}", "error")
            except Exception:
                # Anything unexpected: log it for debugging, but never imply
                # to the member that the transfer succeeded.
                status = "failed"
                app.logger.exception("Unexpected error during transfer")
                flash(
                    "Something went wrong while processing your transfer and "
                    "you have not been added. Please contact an admin.",
                    "error",
                )

            db = get_db()
            db.execute(
                _q("""INSERT INTO transfer_requests
                   (member_name, member_email, cancelled_event_id,
                    cancelled_event_name, target_event_id, target_event_name,
                    status, created_at, processed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""),
                (
                    session.get("member_name", ""),
                    email,
                    cancelled_id,
                    session.get("cancelled_event_label", "Unknown"),
                    target_id,
                    selected["label"],
                    status,
                    now,
                    now,
                ),
            )
            db.commit()

            # Clear the in-progress selection but keep the member logged in
            # (and drop cancelled_events so it reloads fresh for another go).
            for key in [
                "cancelled_events",
                "cancelled_event_id", "cancelled_event_label",
                "amount_paid", "target_events",
            ]:
                session.pop(key, None)

            return redirect(url_for("step_cancelled"))

    return render_template(
        "step_target.html",
        events=target_events,
        cancelled_label=session.get("cancelled_event_label", ""),
        amount=f"£{session.get('amount_paid', 0) / 100:.2f}",
    )


@app.route("/logout")
def logout():
    session.clear()
    flash("You've been logged out.", "success")
    return redirect(url_for("step_email"))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST" and request.form.get("action") == "login":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin"))
        else:
            flash("Incorrect password.", "error")

    if not session.get("admin"):
        return render_template("admin_login.html")

    db = get_db()
    requests = db.execute(
        "SELECT * FROM transfer_requests ORDER BY created_at DESC LIMIT 50"
    ).fetchall()

    return render_template("admin.html", requests=requests)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin"))


# Under a WSGI server (gunicorn) the __main__ block below never runs, so make
# sure the schema exists at import time when a real database is configured.
if USE_POSTGRES:
    init_db()


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)
