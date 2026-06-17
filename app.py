import asyncio
import os
import sqlite3

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


# --- Database ---


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS transfer_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        results = []
        for event in events or []:
            price = event.get("payment", {}).get("total")
            if price == amount:
                results.append({
                    "id": event["id"],
                    "label": format_event_label(event),
                })
        return results
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

        result = await s.change_response(
            target_event_id, member_id, {"accepted": True}
        )
        return result
    finally:
        await s.clientsession.close()


# --- Routes ---


@app.route("/", methods=["GET", "POST"])
def step_email():
    if request.method == "POST":
        email = request.form.get("member_email", "").strip()
        if not email:
            flash("Please enter your email.", "error")
        else:
            try:
                cancelled, member_name = run_async(
                    _find_cancelled_paid_events(email)
                )
            except KeyError:
                flash(
                    "We couldn't find that email in Spond. "
                    "Make sure you're using the email registered with your account.",
                    "error",
                )
                return render_template("step_email.html")

            if not cancelled:
                flash(
                    "We couldn't find any paid sessions that you've cancelled. "
                    "Make sure you've declined the session in Spond first.",
                    "error",
                )
            else:
                session["email"] = email
                session["member_name"] = member_name
                session["cancelled_events"] = cancelled
                return redirect(url_for("step_cancelled"))

    return render_template("step_email.html")


@app.route("/cancelled", methods=["GET", "POST"])
def step_cancelled():
    if "email" not in session or "cancelled_events" not in session:
        return redirect(url_for("step_email"))

    cancelled_events = session["cancelled_events"]

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
def step_target():
    required = ["email", "cancelled_event_id", "target_events"]
    if not all(k in session for k in required):
        return redirect(url_for("step_email"))

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
            now = datetime.now(timezone.utc).isoformat()
            try:
                run_async(_do_transfer(email, cancelled_id, target_id))
                status = "approved"
                flash(
                    f"Done! You've been added to {selected['label']}.",
                    "success",
                )
            except Exception as e:
                status = "failed"
                flash(
                    f"Something went wrong: {e}. Please contact an admin.",
                    "error",
                )

            db = get_db()
            db.execute(
                """INSERT INTO transfer_requests
                   (member_name, member_email, cancelled_event_id,
                    cancelled_event_name, target_event_id, target_event_name,
                    status, created_at, processed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

            # Clear session state
            for key in [
                "email", "member_name", "cancelled_events",
                "cancelled_event_id", "cancelled_event_label",
                "amount_paid", "target_events",
            ]:
                session.pop(key, None)

            return redirect(url_for("step_email"))

    return render_template(
        "step_target.html",
        events=target_events,
        cancelled_label=session.get("cancelled_event_label", ""),
        amount=f"£{session.get('amount_paid', 0) / 100:.2f}",
    )


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


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)
