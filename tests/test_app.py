import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Patch env vars before importing app
import os

os.environ.setdefault("SPOND_USERNAME", "test@example.com")
os.environ.setdefault("SPOND_PASSWORD", "testpass")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

from app import (
    app,
    format_event_label,
    get_db,
    get_used_cancelled_event_ids,
    init_db,
    _hash_code,
)


def insert_transfer(
    cancelled_event_id,
    member_email="user@example.com",
    status="approved",
):
    """Seed a row in the transfer log (used by the one-use-per-session tests)."""
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO transfer_requests (member_name, member_email, "
            "cancelled_event_id, cancelled_event_name, target_event_id, "
            "target_event_name, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "Test User", member_email, cancelled_event_id, "Cancelled",
                "TGT", "Target", status, "2026-06-17T00:00:00",
            ),
        )
        db.commit()


def login(client, **extra):
    """Mark the test client as a verified, logged-in member."""
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["email"] = "user@example.com"
        sess["member_name"] = "Test User"
        sess.update(extra)


def set_pending_code(client, code="123456", expired=False):
    """Put a pending verification code into the session."""
    delta = timedelta(minutes=-1) if expired else timedelta(minutes=10)
    with client.session_transaction() as sess:
        sess["pending_email"] = "user@example.com"
        sess["pending_name"] = "Test User"
        sess["code_hash"] = _hash_code(code)
        sess["code_expires"] = (datetime.now(timezone.utc) + delta).isoformat()
        sess["code_attempts"] = 0


# --- Helpers ---


class MockAsyncContextManager:
    """Helper to mock `async with session.get/post(...)` patterns."""

    def __init__(self, return_value):
        self._return_value = return_value

    async def __aenter__(self):
        return self._return_value

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    app.config["TESTING"] = True
    with patch("app.DB_PATH", db_path):
        # Build the schema through the app's own init_db so tests exercise the
        # real table definition and indexes (e.g. the one-approved-per-session
        # unique index), not a hand-rolled copy that can drift.
        init_db()
        with app.test_client() as client:
            yield client


def make_event(
    event_id="EVT1",
    heading="STV Swim",
    start="2026-06-20T07:00:00Z",
    end="2026-06-20T08:00:00Z",
    payment_total=350,
    accepted_ids=None,
    declined_ids=None,
    unanswered_ids=None,
):
    event = {
        "id": event_id,
        "heading": heading,
        "startTimestamp": start,
        "endTimestamp": end,
        "responses": {
            "acceptedIds": accepted_ids or [],
            "declinedIds": declined_ids or [],
            "unansweredIds": unanswered_ids or [],
            "participantIds": (accepted_ids or [])
            + (declined_ids or [])
            + (unanswered_ids or []),
        },
    }
    if payment_total is not None:
        event["payment"] = {"total": payment_total, "currency": "GBP"}
    return event


def make_person(member_id="MEM1", profile_id="PROF1", email="user@example.com"):
    return {
        "id": member_id,
        "profile": {"id": profile_id},
        "firstName": "Test",
        "lastName": "User",
        "email": email,
    }


# --- format_event_label tests ---


class TestFormatEventLabel:
    def test_with_timestamp_converts_utc_to_uk_summer_time(self):
        # June is BST (UTC+1), so 07:00Z must display as 08:00 UK time.
        event = {"heading": "STV Swim", "startTimestamp": "2026-06-20T07:00:00Z"}
        label = format_event_label(event)
        assert "STV Swim" in label
        assert "20 Jun 2026" in label
        assert "08:00" in label

    def test_with_timestamp_winter_is_utc(self):
        # January is GMT (UTC+0), so the displayed time matches the UTC time.
        event = {"heading": "STV Swim", "startTimestamp": "2026-01-20T07:00:00Z"}
        label = format_event_label(event)
        assert "20 Jan 2026" in label
        assert "07:00" in label

    def test_without_timestamp(self):
        event = {"heading": "STV Swim"}
        assert format_event_label(event) == "STV Swim"

    def test_unnamed_event(self):
        event = {}
        assert format_event_label(event) == "Unnamed event"


# --- Route tests ---


class TestStepEmail:
    def test_get_shows_form(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"member_email" in resp.data

    @patch("app.send_verification_email")
    @patch("app.run_async")
    def test_post_member_sends_code_and_redirects(
        self, mock_run, mock_send, client
    ):
        mock_run.return_value = "Test User"  # _lookup_member
        resp = client.post("/", data={"member_email": "user@example.com"})
        assert resp.status_code == 302
        assert "/verify" in resp.headers["Location"]
        mock_send.assert_called_once()

    @patch("app.send_verification_email")
    @patch("app.run_async")
    def test_post_unknown_email_shows_error(self, mock_run, mock_send, client):
        mock_run.side_effect = KeyError("No person matched")
        resp = client.post(
            "/", data={"member_email": "nobody@example.com"}, follow_redirects=True
        )
        assert b"couldn" in resp.data
        mock_send.assert_not_called()

    @patch("app.send_verification_email")
    @patch("app.run_async")
    def test_post_email_send_failure_shows_error(
        self, mock_run, mock_send, client
    ):
        mock_run.return_value = "Test User"
        mock_send.side_effect = Exception("smtp down")
        resp = client.post(
            "/", data={"member_email": "user@example.com"}, follow_redirects=True
        )
        assert b"couldn" in resp.data.lower()

    def test_post_empty_email_shows_error(self, client):
        resp = client.post("/", data={"member_email": ""}, follow_redirects=True)
        assert b"enter your email" in resp.data.lower()

    @patch("app.send_verification_email")
    @patch("app.run_async")
    def test_authenticated_member_skips_to_cancelled(
        self, mock_run, mock_send, client
    ):
        login(client)
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/cancelled" in resp.headers["Location"]


class TestVerify:
    def test_redirects_without_pending(self, client):
        resp = client.get("/verify")
        assert resp.status_code == 302
        assert "/" == resp.headers["Location"] or resp.headers["Location"].endswith("/")

    def test_correct_code_authenticates(self, client):
        set_pending_code(client, code="123456")
        resp = client.post("/verify", data={"code": "123456"})
        assert resp.status_code == 302
        assert "/loading" in resp.headers["Location"]
        with client.session_transaction() as sess:
            assert sess["authenticated"] is True
            assert sess["email"] == "user@example.com"

    def test_loading_page_requires_auth(self, client):
        resp = client.get("/loading")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")

    def test_loading_page_shown_when_authenticated(self, client):
        login(client)
        resp = client.get("/loading")
        assert resp.status_code == 200
        assert b"Authentication successful" in resp.data
        # It should point the browser onward to the sessions page.
        assert b"/cancelled" in resp.data

    def test_wrong_code_shows_error(self, client):
        set_pending_code(client, code="123456")
        resp = client.post(
            "/verify", data={"code": "000000"}, follow_redirects=True
        )
        assert b"wasn" in resp.data.lower() or b"correct" in resp.data.lower()

    def test_expired_code_redirects(self, client):
        set_pending_code(client, code="123456", expired=True)
        resp = client.post(
            "/verify", data={"code": "123456"}, follow_redirects=True
        )
        assert b"expired" in resp.data.lower()


class TestStepCancelled:
    def test_redirects_without_auth(self, client):
        resp = client.get("/cancelled")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")

    def test_shows_cancelled_events(self, client):
        cancelled = [
            {"event_id": "EVT1", "label": "STV Swim — Fri 20 Jun", "amount_paid": 350}
        ]
        login(client, cancelled_events=cancelled)
        resp = client.get("/cancelled")
        assert resp.status_code == 200
        assert b"STV Swim" in resp.data

    @patch("app.run_async")
    def test_post_with_matching_targets_redirects(self, mock_run, client):
        cancelled = [
            {"event_id": "EVT1", "label": "STV Swim — Fri 20 Jun", "amount_paid": 350}
        ]
        login(client, cancelled_events=cancelled)

        targets = [{"id": "EVT2", "label": "STV Swim — Mon 23 Jun"}]
        mock_run.return_value = targets
        resp = client.post("/cancelled", data={"cancelled_event": "EVT1"})
        assert resp.status_code == 302
        assert "/target" in resp.headers["Location"]

    @patch("app.run_async")
    def test_post_no_matching_targets_shows_error(self, mock_run, client):
        cancelled = [
            {"event_id": "EVT1", "label": "STV Swim — Fri 20 Jun", "amount_paid": 350}
        ]
        login(client, cancelled_events=cancelled)

        mock_run.return_value = []
        resp = client.post(
            "/cancelled",
            data={"cancelled_event": "EVT1"},
            follow_redirects=True,
        )
        assert b"no upcoming sessions" in resp.data.lower()

    @patch("app.run_async")
    def test_spond_failure_shows_retry_message(self, mock_run, client):
        login(client)
        mock_run.side_effect = RuntimeError("HTTP 429")
        resp = client.get("/cancelled")
        assert resp.status_code == 200
        assert b"couldn&#39;t load your sessions" in resp.data
        assert b"429" not in resp.data
        with client.session_transaction() as sess:
            assert "cancelled_events" not in sess


class TestStepTarget:
    def test_redirects_without_auth(self, client):
        resp = client.get("/target")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")

    @patch("app.run_async")
    def test_successful_transfer(self, mock_run, client):
        login(
            client,
            cancelled_event_id="EVT1",
            cancelled_event_label="STV Swim — Fri 20 Jun",
            amount_paid=350,
            target_events=[{"id": "EVT2", "label": "STV Swim — Mon 23 Jun"}],
        )
        mock_run.side_effect = [
            {"acceptedIds": ["MEM1"]},  # _do_transfer
            ([], "Test User"),          # reload in step_cancelled after redirect
        ]
        resp = client.post(
            "/target",
            data={"target_event": "EVT2"},
            follow_redirects=True,
        )
        assert b"Done" in resp.data or b"added" in resp.data.lower()

    @patch("app.run_async")
    def test_failed_transfer_shows_friendly_error(self, mock_run, client):
        login(
            client,
            cancelled_event_id="EVT1",
            cancelled_event_label="STV Swim — Fri 20 Jun",
            amount_paid=350,
            target_events=[{"id": "EVT2", "label": "STV Swim — Mon 23 Jun"}],
        )
        mock_run.side_effect = [
            ValueError("Payment not found"),  # _do_transfer raises (expected)
            ([], "Test User"),                # reload in step_cancelled after redirect
        ]
        resp = client.post(
            "/target",
            data={"target_event": "EVT2"},
            follow_redirects=True,
        )
        # The friendly message is shown, and we never imply success.
        # (Jinja HTML-escapes the apostrophe in "Couldn't", so match the rest.)
        assert b"complete the transfer" in resp.data
        assert b"Payment not found" in resp.data
        assert b"Done" not in resp.data

    @patch("app.run_async")
    def test_unexpected_error_does_not_imply_success(self, mock_run, client):
        login(
            client,
            cancelled_event_id="EVT1",
            cancelled_event_label="STV Swim — Fri 20 Jun",
            amount_paid=350,
            target_events=[{"id": "EVT2", "label": "STV Swim — Mon 23 Jun"}],
        )
        mock_run.side_effect = [
            RuntimeError("kaboom"),  # _do_transfer blows up unexpectedly
            ([], "Test User"),       # reload in step_cancelled after redirect
        ]
        resp = client.post(
            "/target",
            data={"target_event": "EVT2"},
            follow_redirects=True,
        )
        # Generic message, no leak of internals, no false "added" claim.
        assert b"you have not been added" in resp.data
        assert b"kaboom" not in resp.data
        assert b"Done" not in resp.data


class TestAdmin:
    def test_requires_login(self, client):
        resp = client.get("/admin")
        assert b"password" in resp.data.lower()

    def test_wrong_password(self, client):
        resp = client.post(
            "/admin",
            data={"action": "login", "password": "wrong"},
            follow_redirects=True,
        )
        assert b"Incorrect" in resp.data

    def test_correct_password(self, client):
        resp = client.post(
            "/admin",
            data={"action": "login", "password": "admin"},
            follow_redirects=True,
        )
        assert b"Transfer Log" in resp.data

    def test_logout(self, client):
        client.post("/admin", data={"action": "login", "password": "admin"})
        resp = client.get("/admin/logout", follow_redirects=True)
        assert b"password" in resp.data.lower()


def admin_login(client):
    with client.session_transaction() as sess:
        sess["admin"] = True


def impersonate(client, **extra):
    admin_login(client)
    login(client, impersonating=True, **extra)


TARGET_STATE = {
    "cancelled_event_id": "EVT1",
    "cancelled_event_label": "STV Swim — Fri 20 Jun",
    "amount_paid": 350,
    "target_events": [{"id": "EVT2", "label": "STV Swim — Mon 23 Jun"}],
}


class TestImpersonation:
    def test_admin_page_shows_impersonate_form(self, client):
        admin_login(client)
        resp = client.get("/admin")
        assert b'action="/admin/impersonate"' in resp.data

    @patch("app.run_async")
    def test_requires_admin(self, mock_run, client):
        resp = client.post(
            "/admin/impersonate", data={"member_email": "user@example.com"}
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/admin")
        mock_run.assert_not_called()
        with client.session_transaction() as sess:
            assert "authenticated" not in sess
            assert "impersonating" not in sess

    @patch("app.run_async")
    def test_signs_in_as_member_without_code(self, mock_run, client):
        admin_login(client)
        mock_run.return_value = "Jane Member"
        resp = client.post(
            "/admin/impersonate", data={"member_email": " jane@example.com "}
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/loading")
        with client.session_transaction() as sess:
            assert sess["authenticated"] is True
            assert sess["email"] == "jane@example.com"
            assert sess["member_name"] == "Jane Member"
            assert sess["impersonating"] is True
            assert sess["admin"] is True

    @patch("app.run_async")
    def test_drops_previous_member_state(self, mock_run, client):
        admin_login(client)
        login(client, cancelled_events=[{"event_id": "OLD"}], **TARGET_STATE)
        mock_run.return_value = "Jane Member"
        client.post("/admin/impersonate", data={"member_email": "jane@example.com"})
        with client.session_transaction() as sess:
            for key in ("cancelled_events", *TARGET_STATE):
                assert key not in sess

    @patch("app.run_async")
    def test_unknown_email(self, mock_run, client):
        admin_login(client)
        mock_run.side_effect = KeyError("nobody@example.com")
        resp = client.post(
            "/admin/impersonate",
            data={"member_email": "nobody@example.com"},
            follow_redirects=True,
        )
        assert b"No club member found" in resp.data
        with client.session_transaction() as sess:
            assert "authenticated" not in sess
            assert "impersonating" not in sess

    @patch("app.run_async")
    def test_empty_email(self, mock_run, client):
        admin_login(client)
        resp = client.post(
            "/admin/impersonate",
            data={"member_email": "  "},
            follow_redirects=True,
        )
        assert b"enter the member" in resp.data
        mock_run.assert_not_called()

    def test_banner_shown_while_impersonating(self, client):
        impersonate(client, cancelled_events=[])
        resp = client.get("/cancelled")
        assert b"Stop impersonating" in resp.data
        assert b"Impersonating <strong>Test User</strong>" in resp.data
        assert b"/admin/stop-impersonating" in resp.data

    def test_loading_page_names_impersonated_member(self, client):
        impersonate(client)
        resp = client.get("/loading")
        assert b"Impersonating Test User" in resp.data
        assert b"Authentication successful" not in resp.data

    @patch("app.run_async")
    def test_lookup_failure_shows_error(self, mock_run, client):
        admin_login(client)
        mock_run.side_effect = RuntimeError("spond down")
        resp = client.post(
            "/admin/impersonate",
            data={"member_email": "jane@example.com"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"look up that member" in resp.data
        with client.session_transaction() as sess:
            assert sess["admin"] is True
            assert "impersonating" not in sess

    @patch("app.run_async")
    def test_impersonation_is_logged(self, mock_run, client, caplog):
        admin_login(client)
        mock_run.return_value = "Jane Member"
        client.post("/admin/impersonate", data={"member_email": "jane@example.com"})
        assert "Admin started impersonating jane@example.com" in caplog.text

    def test_session_cookie_is_samesite_lax(self, client):
        resp = client.post("/admin", data={"action": "login", "password": "admin"})
        assert "SameSite=Lax" in resp.headers["Set-Cookie"]

    @patch("app.run_async")
    def test_rejects_name_or_id(self, mock_run, client):
        admin_login(client)
        resp = client.post(
            "/admin/impersonate",
            data={"member_email": "Jane Member"},
            follow_redirects=True,
        )
        assert b"not a name or ID" in resp.data
        mock_run.assert_not_called()
        with client.session_transaction() as sess:
            assert "impersonating" not in sess

    @pytest.mark.parametrize("password", ["", "admin"])
    def test_admin_login_disabled_without_password(self, client, password):
        with patch("app.ADMIN_PASSWORD", ""):
            resp = client.post(
                "/admin", data={"action": "login", "password": password}
            )
        assert b"ADMIN_PASSWORD is not set" in resp.data
        with client.session_transaction() as sess:
            assert "admin" not in sess

    def test_admin_login_wrong_password(self, client):
        resp = client.post("/admin", data={"action": "login", "password": "nope"})
        assert b"Incorrect password" in resp.data
        with client.session_transaction() as sess:
            assert "admin" not in sess

    def test_no_banner_for_real_member(self, client):
        login(client, cancelled_events=[])
        resp = client.get("/cancelled")
        assert b"Stop impersonating" not in resp.data

    @patch("app.run_async")
    def test_transfer_goes_through_while_impersonating(self, mock_run, client):
        impersonate(client, **TARGET_STATE)
        mock_run.side_effect = [
            {"acceptedIds": ["MEM1"]},
            ([], "Test User"),
        ]
        resp = client.post(
            "/target", data={"target_event": "EVT2"}, follow_redirects=True
        )
        assert b"Done" in resp.data
        with app.app_context():
            rows = get_db().execute("SELECT * FROM transfer_requests").fetchall()
        assert [(r["member_email"], r["status"]) for r in rows] == [
            ("user@example.com", "approved")
        ]
        with client.session_transaction() as sess:
            assert sess["impersonating"] is True

    def test_target_page_still_viewable_while_impersonating(self, client):
        impersonate(client, **TARGET_STATE)
        resp = client.get("/target")
        assert resp.status_code == 200
        assert b"STV Swim" in resp.data

    def test_stop_impersonating_keeps_admin(self, client):
        impersonate(client, cancelled_events=[], **TARGET_STATE)
        resp = client.get("/admin/stop-impersonating")
        assert resp.headers["Location"].endswith("/admin")
        with client.session_transaction() as sess:
            assert sess["admin"] is True
            for key in (
                "authenticated", "email", "member_name", "impersonating",
                "cancelled_events", *TARGET_STATE,
            ):
                assert key not in sess

    def test_stop_impersonating_leaves_real_member_alone(self, client):
        login(client)
        client.get("/admin/stop-impersonating")
        with client.session_transaction() as sess:
            assert sess["authenticated"] is True
            assert sess["email"] == "user@example.com"

    def test_logout_while_impersonating_returns_to_admin(self, client):
        impersonate(client)
        resp = client.get("/logout")
        assert resp.headers["Location"].endswith("/admin")
        with client.session_transaction() as sess:
            assert sess["admin"] is True
            assert "authenticated" not in sess
            assert "impersonating" not in sess

    def test_admin_logout_ends_impersonation(self, client):
        impersonate(client)
        client.get("/admin/logout")
        with client.session_transaction() as sess:
            assert "admin" not in sess
            assert "authenticated" not in sess
            assert "email" not in sess
            assert "impersonating" not in sess


# --- Transaction matching tests ---


def make_payment(member_id="MEM1", total=350, status="FULFILLED"):
    """A row from Spond's per-session payments endpoint."""
    return {
        "id": f"PAY_{member_id}",
        "status": status,
        "total": total,
        "name": "STV Swim",
        "behalfOfMembershipId": member_id,
    }


def mock_spond_for(member, events=None, get_event=None):
    mock_spond = AsyncMock()
    mock_spond.get_person = AsyncMock(return_value=member)
    mock_spond.get_events = AsyncMock(return_value=events or [])
    if get_event is not None:
        mock_spond.get_event = AsyncMock(side_effect=get_event)
    mock_spond.clientsession = AsyncMock()
    return mock_spond


def payments_by_event(mapping):
    """Fake _get_session_payments that returns `mapping[event_id]`."""
    return AsyncMock(side_effect=lambda s, event_id: mapping[event_id])


class TestFindCancelledPaidEvents:
    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_lists_declined_sessions_the_member_paid_for(self, MockSpond):
        from app import _find_cancelled_paid_events

        paid = make_event(event_id="EVT_PAID", declined_ids=["MEM1"])
        unpaid = make_event(event_id="EVT_UNPAID", declined_ids=["MEM1"])
        attended = make_event(event_id="EVT_ATTENDED", accepted_ids=["MEM1"])
        MockSpond.return_value = mock_spond_for(
            make_person(), events=[paid, unpaid, attended]
        )
        fetch = payments_by_event({
            "EVT_PAID": [make_payment("OTHER"), make_payment("MEM1")],
            "EVT_UNPAID": [make_payment("OTHER")],
        })

        with patch("app._get_session_payments", fetch):
            results, name = await _find_cancelled_paid_events("user@example.com")

        assert results == [{
            "event_id": "EVT_PAID",
            "label": format_event_label(paid),
            "amount_paid": 350,
        }]
        assert name == "Test User"
        assert sorted(c.args[1] for c in fetch.call_args_list) == [
            "EVT_PAID", "EVT_UNPAID",
        ]

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_payment_for_an_attended_session_does_not_fund_a_later_one(
        self, MockSpond
    ):
        from app import _find_cancelled_paid_events

        attended = make_event(
            event_id="EVT_JUN03", start="2026-06-03T07:00:00Z", accepted_ids=["MEM1"]
        )
        declined_unpaid = make_event(
            event_id="EVT_JUN10", start="2026-06-10T07:00:00Z", declined_ids=["MEM1"]
        )
        MockSpond.return_value = mock_spond_for(
            make_person(), events=[attended, declined_unpaid]
        )
        fetch = payments_by_event({"EVT_JUN10": [make_payment("OTHER")]})

        with patch("app._get_session_payments", fetch):
            results, _ = await _find_cancelled_paid_events("user@example.com")

        assert results == []

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_results_sorted_by_session_date(self, MockSpond):
        from app import _find_cancelled_paid_events

        later = make_event(
            event_id="EVT_JUN22", start="2026-06-22T07:00:00Z", declined_ids=["MEM1"]
        )
        earlier = make_event(
            event_id="EVT_JUN17", start="2026-06-17T07:00:00Z", declined_ids=["MEM1"]
        )
        MockSpond.return_value = mock_spond_for(make_person(), events=[later, earlier])
        fetch = payments_by_event({
            "EVT_JUN22": [make_payment()],
            "EVT_JUN17": [make_payment()],
        })

        with patch("app._get_session_payments", fetch):
            results, _ = await _find_cancelled_paid_events("user@example.com")

        assert [r["event_id"] for r in results] == ["EVT_JUN17", "EVT_JUN22"]

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_ignores_free_events(self, MockSpond):
        from app import _find_cancelled_paid_events

        free = make_event(payment_total=None, declined_ids=["MEM1"])
        MockSpond.return_value = mock_spond_for(make_person(), events=[free])
        fetch = payments_by_event({})

        with patch("app._get_session_payments", fetch):
            results, _ = await _find_cancelled_paid_events("user@example.com")

        assert results == []
        fetch.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_ignores_payments_that_are_not_fulfilled(self, MockSpond):
        from app import _find_cancelled_paid_events

        event = make_event(declined_ids=["MEM1"])
        MockSpond.return_value = mock_spond_for(make_person(), events=[event])
        fetch = payments_by_event({"EVT1": [make_payment(status="PENDING")]})

        with patch("app._get_session_payments", fetch):
            results, _ = await _find_cancelled_paid_events("user@example.com")

        assert results == []

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_amount_paid_comes_from_the_payment(self, MockSpond):
        from app import _find_cancelled_paid_events

        event = make_event(payment_total=350, declined_ids=["MEM1"])
        MockSpond.return_value = mock_spond_for(make_person(), events=[event])
        fetch = payments_by_event({"EVT1": [make_payment(total=300)]})

        with patch("app._get_session_payments", fetch):
            results, _ = await _find_cancelled_paid_events("user@example.com")

        assert results[0]["amount_paid"] == 300

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_a_failed_payments_fetch_raises(self, MockSpond):
        from app import _find_cancelled_paid_events

        events = [
            make_event(event_id=f"EVT{i}", declined_ids=["MEM1"]) for i in range(3)
        ]
        MockSpond.return_value = mock_spond_for(make_person(), events=events)

        async def fetch(s, event_id):
            if event_id == "EVT1":
                raise RuntimeError("HTTP 503")
            return [make_payment()]

        with (
            patch("app._get_session_payments", side_effect=fetch),
            pytest.raises(ExceptionGroup) as exc_info,
        ):
            await _find_cancelled_paid_events("user@example.com")
        assert exc_info.group_contains(RuntimeError, match="503")

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_malformed_payment_is_not_mistaken_for_a_missing_member(
        self, MockSpond
    ):
        from app import _find_cancelled_paid_events

        event = make_event(declined_ids=["MEM1"])
        MockSpond.return_value = mock_spond_for(make_person(), events=[event])
        fetch = payments_by_event(
            {"EVT1": [{"behalfOfMembershipId": "MEM1", "status": "FULFILLED"}]}
        )

        with (
            patch("app._get_session_payments", fetch),
            pytest.raises(ExceptionGroup) as exc_info,
        ):
            await _find_cancelled_paid_events("user@example.com")
        assert exc_info.group_contains(KeyError)


class TestGetMatchingEvents:
    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_only_returns_exact_price_match(self, MockSpond):
        from app import _get_matching_events

        events = [
            make_event(event_id="EVT1", payment_total=350),
            make_event(event_id="EVT2", payment_total=500),
            make_event(event_id="EVT3", payment_total=350),
        ]

        mock_spond = AsyncMock()
        mock_spond.get_events = AsyncMock(return_value=events)
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        results = await _get_matching_events(350)
        assert len(results) == 2
        assert {r["id"] for r in results} == {"EVT1", "EVT3"}

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_excludes_free_events(self, MockSpond):
        from app import _get_matching_events

        events = [
            make_event(event_id="EVT1", payment_total=None),
            make_event(event_id="EVT2", payment_total=350),
        ]

        mock_spond = AsyncMock()
        mock_spond.get_events = AsyncMock(return_value=events)
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        results = await _get_matching_events(350)
        assert len(results) == 1
        assert results[0]["id"] == "EVT2"

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_no_match_for_different_price(self, MockSpond):
        from app import _get_matching_events

        events = [make_event(event_id="EVT1", payment_total=500)]

        mock_spond = AsyncMock()
        mock_spond.get_events = AsyncMock(return_value=events)
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        results = await _get_matching_events(350)
        assert len(results) == 0

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_includes_session_on_seventh_day(self, MockSpond):
        """A session dated exactly ~7 days out must still be offered.

        Regression for the date-truncation bug: the Spond library truncates
        ``max_start`` to midnight, so ``now + 7 days`` used to drop the whole
        final day and hide a session exactly a week out (e.g. Elizabeth's
        06:30 S&C on the 11th). We now request a day wider and filter to the
        precise 7×24h cutoff, so the boundary-day session comes through.
        """
        from app import _get_matching_events

        now = datetime.now(timezone.utc)
        # An early-morning session just under 7×24h away — the case that broke.
        boundary = make_event(
            event_id="EVT_7D",
            heading="S&C",
            start=(now + timedelta(days=7) - timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            payment_total=750,
        )

        mock_spond = AsyncMock()
        mock_spond.get_events = AsyncMock(return_value=[boundary])
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        results = await _get_matching_events(750)
        assert [r["id"] for r in results] == ["EVT_7D"]

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_excludes_session_beyond_seven_days(self, MockSpond):
        """A session more than 7×24h away isn't bookable, so must be excluded.

        The API's date-only ``max_start`` can over-return the wider day we ask
        for; the Python cutoff must still drop anything past the real window.
        """
        from app import _get_matching_events

        now = datetime.now(timezone.utc)
        too_far = make_event(
            event_id="EVT_8D",
            heading="S&C",
            start=(now + timedelta(days=7) + timedelta(hours=2)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            payment_total=750,
        )

        mock_spond = AsyncMock()
        mock_spond.get_events = AsyncMock(return_value=[too_far])
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        results = await _get_matching_events(750)
        assert results == []


class TestGetSessionPayments:
    def _make_spond(self, status=200, body=None, token="tok"):
        s = MagicMock()
        s.token = token
        s.login = AsyncMock()
        s.api_url = "https://api.spond.com/core/v1/"
        s.auth_headers = {"Authorization": "Bearer tok"}
        resp = MagicMock()
        resp.status = status
        resp.json = AsyncMock(return_value=body if body is not None else [])
        s.clientsession = MagicMock()
        s.clientsession.get = MagicMock(return_value=MockAsyncContextManager(resp))
        return s

    @pytest.mark.asyncio
    async def test_returns_the_session_payments(self):
        from app import _get_session_payments

        s = self._make_spond(body=[make_payment()])
        assert await _get_session_payments(s, "EVT1") == [make_payment()]
        call = s.clientsession.get.call_args
        assert call.args[0] == "https://api.spond.com/core/v1/payments/spond"
        assert call.kwargs["params"] == {"spondId": "EVT1"}
        assert call.kwargs["headers"] == s.auth_headers
        s.login.assert_not_called()

    @pytest.mark.asyncio
    async def test_logs_in_first_when_needed(self):
        from app import _get_session_payments

        s = self._make_spond(token=None)
        await _get_session_payments(s, "EVT1")
        s.login.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 404, 429, 500])
    async def test_raises_on_any_non_200(self, status):
        from app import _get_session_payments

        s = self._make_spond(status=status)
        with pytest.raises(RuntimeError, match=str(status)):
            await _get_session_payments(s, "EVT1")


class TestDoTransfer:
    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_rejects_if_not_declined(self, MockSpond):
        from app import _do_transfer

        cancelled = make_event(event_id="EVT1", unanswered_ids=["MEM1"])
        target = make_event(event_id="EVT2")
        MockSpond.return_value = mock_spond_for(
            make_person(), get_event=[cancelled, target]
        )

        with pytest.raises(ValueError, match="cancelled your spot"):
            await _do_transfer("user@example.com", "EVT1", "EVT2")

    @pytest.mark.asyncio
    @patch("app.Spond")
    async def test_rejects_if_prices_differ(self, MockSpond):
        from app import _do_transfer

        cancelled = make_event(event_id="EVT1", payment_total=350, declined_ids=["MEM1"])
        target = make_event(event_id="EVT2", payment_total=500)
        MockSpond.return_value = mock_spond_for(
            make_person(), get_event=[cancelled, target]
        )
        fetch = payments_by_event({"EVT1": [make_payment(total=350)]})

        with (
            patch("app._get_session_payments", fetch),
            pytest.raises(ValueError, match="prices don't match"),
        ):
            await _do_transfer("user@example.com", "EVT1", "EVT2")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payments",
        [[], [make_payment("OTHER")], [make_payment(status="PENDING")]],
        ids=["none", "someone-else", "not-fulfilled"],
    )
    @patch("app.Spond")
    async def test_rejects_if_member_did_not_pay_for_that_session(
        self, MockSpond, payments
    ):
        from app import _do_transfer

        cancelled = make_event(event_id="EVT1", payment_total=350, declined_ids=["MEM1"])
        target = make_event(event_id="EVT2", payment_total=350)
        MockSpond.return_value = mock_spond_for(
            make_person(), get_event=[cancelled, target]
        )
        fetch = payments_by_event({"EVT1": payments})

        with (
            patch("app._get_session_payments", fetch),
            pytest.raises(ValueError, match="payment record"),
        ):
            await _do_transfer("user@example.com", "EVT1", "EVT2")

    @pytest.mark.asyncio
    @patch("app._accept_without_payment")
    @patch("app.Spond")
    async def test_successful_transfer(self, MockSpond, mock_accept):
        from app import _do_transfer

        cancelled = make_event(event_id="EVT1", payment_total=350, declined_ids=["MEM1"])
        target = make_event(event_id="EVT2", payment_total=350)
        mock_spond = mock_spond_for(make_person(), get_event=[cancelled, target])
        MockSpond.return_value = mock_spond
        mock_accept.return_value = {"acceptedIds": ["MEM1"]}
        fetch = payments_by_event({"EVT1": [make_payment("OTHER"), make_payment()]})

        with patch("app._get_session_payments", fetch):
            result = await _do_transfer("user@example.com", "EVT1", "EVT2")

        assert "acceptedIds" in result
        fetch.assert_awaited_once_with(mock_spond, "EVT1")
        mock_accept.assert_called_once_with(mock_spond, "EVT2", "MEM1")


class TestAcceptWithoutPayment:
    """The transfer must only be treated as successful if Spond actually
    adds the member — never trust the HTTP call blindly."""

    def _make_spond(self, put_status=200, put_body="{}"):
        s = MagicMock()
        s.token = "tok"  # truthy -> skips login
        s.api_url = "https://api.spond.com/core/v1/"
        s.auth_headers = {"Authorization": "Bearer tok"}
        resp = MagicMock()
        resp.status = put_status
        resp.text = AsyncMock(return_value=put_body)
        s.clientsession = MagicMock()
        s.clientsession.put = MagicMock(
            return_value=MockAsyncContextManager(resp)
        )
        # The post-write check reads the event fresh via clientsession.get
        # (bypassing the library cache), so tests configure that, not get_event.
        s.clientsession.get = MagicMock()
        return s

    def _set_fresh_event(self, s, event):
        """Configure the post-check's cache-bypassing GET to return `event`."""
        get_resp = MagicMock()
        get_resp.json = AsyncMock(return_value=event)
        s.clientsession.get = MagicMock(
            return_value=MockAsyncContextManager(get_resp)
        )

    @pytest.mark.asyncio
    async def test_succeeds_when_member_actually_accepted(self):
        from app import _accept_without_payment

        s = self._make_spond(put_status=200)
        self._set_fresh_event(
            s, make_event(event_id="EVT2", accepted_ids=["MEM1"])
        )
        result = await _accept_without_payment(s, "EVT2", "MEM1")
        assert "MEM1" in result["acceptedIds"]
        # It must PUT the accept, with the payment-skip header, to the right URL.
        call = s.clientsession.put.call_args
        assert "sponds/EVT2/responses/MEM1" in call.args[0]
        assert call.kwargs["headers"]["X-Spond-SkipPayment"] == "true"
        assert call.kwargs["json"] == {"accepted": True}

    @pytest.mark.asyncio
    async def test_raises_on_non_200(self):
        from app import _accept_without_payment

        # e.g. the 402 payment-required response we used to swallow.
        s = self._make_spond(put_status=402, put_body='{"paymentIntent":"pi_x"}')
        with pytest.raises(ValueError, match="HTTP 402"):
            await _accept_without_payment(s, "EVT2", "MEM1")
        # Must not even bother verifying — it already failed hard.
        s.clientsession.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_when_200_but_not_actually_accepted(self):
        from app import _accept_without_payment

        # 200, but the member never landed in acceptedIds.
        s = self._make_spond(put_status=200)
        self._set_fresh_event(
            s, make_event(event_id="EVT2", unanswered_ids=["MEM1"])
        )
        with pytest.raises(ValueError, match="didn't take effect"):
            await _accept_without_payment(s, "EVT2", "MEM1")

    @pytest.mark.asyncio
    async def test_raises_when_waitlisted(self):
        from app import _accept_without_payment

        s = self._make_spond(put_status=200)
        event = make_event(event_id="EVT2")
        event["responses"]["waitinglistIds"] = ["MEM1"]
        self._set_fresh_event(s, event)
        with pytest.raises(ValueError, match="waiting list"):
            await _accept_without_payment(s, "EVT2", "MEM1")


class TestOneUsePerCancelledSession:
    """A cancelled session may only fund a single transfer."""

    def test_used_ids_only_counts_approved(self, client):
        insert_transfer("EVT1", status="approved")
        insert_transfer("EVT3", status="failed")
        with app.app_context():
            used = get_used_cancelled_event_ids("user@example.com")
        assert used == {"EVT1"}

    def test_used_ids_are_scoped_to_member(self, client):
        insert_transfer("EVT1", member_email="other@example.com")
        with app.app_context():
            used = get_used_cancelled_event_ids("user@example.com")
        assert used == set()

    def test_cancelled_list_hides_spent_session(self, client):
        insert_transfer("EVT1")  # already transferred from EVT1
        login(client, cancelled_events=[
            {"event_id": "EVT1", "label": "Monday Swim", "amount_paid": 350},
            {"event_id": "EVT2", "label": "Tuesday Swim", "amount_paid": 350},
        ])
        resp = client.get("/cancelled")
        assert b"Tuesday Swim" in resp.data
        assert b"Monday Swim" not in resp.data

    @patch("app.run_async")
    def test_target_blocks_reused_session(self, mock_run, client):
        insert_transfer("EVT1")  # EVT1 already spent
        login(
            client,
            cancelled_event_id="EVT1",
            cancelled_event_label="Monday Swim",
            amount_paid=350,
            target_events=[{"id": "EVT2", "label": "Wednesday Swim"}],
        )
        mock_run.return_value = ([], "Test User")  # step_cancelled reload only
        resp = client.post(
            "/target", data={"target_event": "EVT2"}, follow_redirects=True
        )
        assert b"already used" in resp.data
        assert b"Done" not in resp.data
        # The transfer itself must never run for a spent session.
        assert mock_run.call_count <= 1

    def test_unique_index_rejects_second_approved_row(self, client):
        """The DB itself must refuse a second approved transfer for the same
        (member, cancelled session) — the backstop against concurrent
        double-submits that the in-handler check can race past."""
        insert_transfer("EVT1", status="approved")
        with app.app_context(), pytest.raises(sqlite3.IntegrityError):
            db = get_db()
            db.execute(
                "INSERT INTO transfer_requests (member_name, member_email, "
                "cancelled_event_id, cancelled_event_name, target_event_id, "
                "target_event_name, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "Test User", "user@example.com", "EVT1", "Cancelled",
                    "TGT2", "Target2", "approved", "2026-06-17T00:01:00",
                ),
            )
            db.commit()

    def test_unique_index_allows_failed_retry(self, client):
        """A failed attempt must not block a later approved transfer of the
        same cancelled session — only approved rows are constrained."""
        insert_transfer("EVT1", status="failed")
        insert_transfer("EVT1", status="failed")  # retries are fine
        insert_transfer("EVT1", status="approved")  # eventual success is fine
        with app.app_context():
            used = get_used_cancelled_event_ids("user@example.com")
        assert used == {"EVT1"}

    def test_duplicate_transfer_post_does_not_error(self, client):
        """If a transfer succeeds in Spond but the approved row already exists
        (the losing half of a concurrent double-submit), the member still sees
        success — the duplicate row is dropped, not surfaced as an error."""
        insert_transfer("EVT1", status="approved")  # request A already logged it
        login(
            client,
            cancelled_events=[
                {"event_id": "EVT1", "label": "Monday Swim", "amount_paid": 350}
            ],
            cancelled_event_id="EVT1",
            cancelled_event_label="Monday Swim",
            amount_paid=350,
            target_events=[{"id": "EVT2", "label": "Wednesday Swim"}],
        )
        # The reused-session guard fires first here (EVT1 is already spent),
        # so the member is sent back rather than charged again — and crucially
        # no second approved row is written.
        with patch("app.run_async", return_value=([], "Test User")):
            resp = client.post(
                "/target", data={"target_event": "EVT2"}, follow_redirects=True
            )
        assert resp.status_code == 200
        with app.app_context():
            db = get_db()
            count = db.execute(
                "SELECT COUNT(*) AS n FROM transfer_requests "
                "WHERE cancelled_event_id = 'EVT1' AND status = 'approved'"
            ).fetchone()["n"]
        assert count == 1

    def test_migration_dedupes_existing_rows_and_adds_index(self, tmp_path):
        """The one-off migration collapses duplicate approved rows that predate
        the index, so an existing database can adopt the index cleanly."""
        from migrations.dedupe_approved_transfers import run

        db_path = str(tmp_path / "dupes.db")
        # Seed a table (no index yet) with two identical approved rows — the
        # bug's output — then run the migration and confirm it collapses them
        # to one and the unique index now exists.
        db = sqlite3.connect(db_path)
        db.execute("""
            CREATE TABLE transfer_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_name TEXT NOT NULL, member_email TEXT NOT NULL,
                cancelled_event_id TEXT NOT NULL,
                cancelled_event_name TEXT NOT NULL,
                target_event_id TEXT NOT NULL,
                target_event_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL, processed_at TEXT
            )
        """)
        for _ in range(2):
            db.execute(
                "INSERT INTO transfer_requests (member_name, member_email, "
                "cancelled_event_id, cancelled_event_name, target_event_id, "
                "target_event_name, status, created_at) VALUES "
                "('Sarah', 's@x.com', 'EVT1', 'C', 'TGT', 'T', 'approved', "
                "'2026-06-23T08:16:00')"
            )
        db.commit()

        run(db)

        n = db.execute(
            "SELECT COUNT(*) FROM transfer_requests WHERE status='approved'"
        ).fetchone()[0]
        has_index = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_one_approved_transfer'"
        ).fetchone()
        db.close()
        assert n == 1
        assert has_index is not None
