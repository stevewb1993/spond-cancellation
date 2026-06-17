import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Patch env vars before importing app
import os

os.environ.setdefault("SPOND_USERNAME", "test@example.com")
os.environ.setdefault("SPOND_PASSWORD", "testpass")
os.environ.setdefault("SPOND_CLUB_ID", "CLUB123")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

from app import app, format_event_label, init_db, _hash_code


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


def make_mock_response(json_data=None, status=200):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    return resp


def make_mock_http_session(get_responses=None, post_responses=None):
    """Create a mock aiohttp.ClientSession with async context manager support."""
    mock = MagicMock()

    if post_responses:
        post_iter = iter(post_responses)
        mock.post = MagicMock(
            side_effect=lambda *a, **kw: MockAsyncContextManager(next(post_iter))
        )
    else:
        login_resp = make_mock_response({"loginToken": "tok"})
        mock.post = MagicMock(
            return_value=MockAsyncContextManager(login_resp)
        )

    if get_responses:
        get_iter = iter(get_responses)
        mock.get = MagicMock(
            side_effect=lambda *a, **kw: MockAsyncContextManager(next(get_iter))
        )

    return mock


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    app.config["TESTING"] = True
    with patch("app.DB_PATH", db_path):
        db = sqlite3.connect(db_path)
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


def make_transaction(
    tx_id="TX1",
    payment_name="STV Swim",
    paid_by_id="PROF1",
    total=350,
    paid_at="2026-06-18T10:00:00Z",
    status="FULFILLED",
):
    return {
        "id": tx_id,
        "paymentName": payment_name,
        "total": total,
        "paidAt": paid_at,
        "status": status,
        "paidById": paid_by_id,
        "paidByName": "Test User",
        "currency": "GBP",
        "fee": 29,
        "refunded": 0,
        "refunds": [],
        "feeChargedAsItem": False,
    }


# --- format_event_label tests ---


class TestFormatEventLabel:
    def test_with_timestamp(self):
        event = {"heading": "STV Swim", "startTimestamp": "2026-06-20T07:00:00Z"}
        label = format_event_label(event)
        assert "STV Swim" in label
        assert "20 Jun 2026" in label
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
    def test_failed_transfer_shows_error(self, mock_run, client):
        login(
            client,
            cancelled_event_id="EVT1",
            cancelled_event_label="STV Swim — Fri 20 Jun",
            amount_paid=350,
            target_events=[{"id": "EVT2", "label": "STV Swim — Mon 23 Jun"}],
        )
        mock_run.side_effect = [
            ValueError("Payment not found"),  # _do_transfer raises
            ([], "Test User"),                # reload in step_cancelled after redirect
        ]
        resp = client.post(
            "/target",
            data={"target_event": "EVT2"},
            follow_redirects=True,
        )
        assert b"Something went wrong" in resp.data


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


# --- Transaction matching tests ---


class TestFindCancelledPaidEvents:
    """Test the logic that matches transactions to declined events."""

    @pytest.mark.asyncio
    @patch("app.aiohttp.ClientSession")
    @patch("app.Spond")
    async def test_matches_payment_to_closest_event(self, MockSpond, MockSession):
        from app import _find_cancelled_paid_events

        member = make_person()
        event_jun17 = make_event(
            event_id="EVT_JUN17",
            start="2026-06-17T07:00:00Z",
            declined_ids=["MEM1"],
        )
        event_jun22 = make_event(
            event_id="EVT_JUN22",
            start="2026-06-22T07:00:00Z",
            declined_ids=["MEM1"],
        )

        tx_list = [{"id": "TX1", "paymentName": "STV Swim"}]
        tx_detail = make_transaction(paid_at="2026-06-16T10:00:00Z")

        mock_spond = AsyncMock()
        mock_spond.get_person = AsyncMock(return_value=member)
        mock_spond.get_events = AsyncMock(return_value=[event_jun17, event_jun22])
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        mock_http = make_mock_http_session(
            get_responses=[
                make_mock_response(tx_list),      # transaction list
                make_mock_response(tx_detail),     # transaction detail
            ]
        )
        MockSession.return_value = MockAsyncContextManager(mock_http)

        results, name = await _find_cancelled_paid_events("user@example.com")

        assert len(results) == 1
        assert results[0]["event_id"] == "EVT_JUN17"
        assert results[0]["amount_paid"] == 350

    @pytest.mark.asyncio
    @patch("app.aiohttp.ClientSession")
    @patch("app.Spond")
    async def test_no_match_for_unpaid_declined_event(self, MockSpond, MockSession):
        from app import _find_cancelled_paid_events

        member = make_person()
        event = make_event(declined_ids=["MEM1"])

        mock_spond = AsyncMock()
        mock_spond.get_person = AsyncMock(return_value=member)
        mock_spond.get_events = AsyncMock(return_value=[event])
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        mock_http = make_mock_http_session(
            get_responses=[make_mock_response([])]  # empty transactions
        )
        MockSession.return_value = MockAsyncContextManager(mock_http)

        results, name = await _find_cancelled_paid_events("user@example.com")
        assert len(results) == 0

    @pytest.mark.asyncio
    @patch("app.aiohttp.ClientSession")
    @patch("app.Spond")
    async def test_ignores_free_events(self, MockSpond, MockSession):
        from app import _find_cancelled_paid_events

        member = make_person()
        free_event = make_event(payment_total=None, declined_ids=["MEM1"])

        mock_spond = AsyncMock()
        mock_spond.get_person = AsyncMock(return_value=member)
        mock_spond.get_events = AsyncMock(return_value=[free_event])
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        results, name = await _find_cancelled_paid_events("user@example.com")
        assert len(results) == 0

    @pytest.mark.asyncio
    @patch("app.aiohttp.ClientSession")
    @patch("app.Spond")
    async def test_two_payments_match_two_events(self, MockSpond, MockSession):
        from app import _find_cancelled_paid_events

        member = make_person()
        event1 = make_event(
            event_id="EVT1", start="2026-06-17T07:00:00Z", declined_ids=["MEM1"]
        )
        event2 = make_event(
            event_id="EVT2", start="2026-06-22T07:00:00Z", declined_ids=["MEM1"]
        )

        tx_list = [
            {"id": "TX1", "paymentName": "STV Swim"},
            {"id": "TX2", "paymentName": "STV Swim"},
        ]
        tx_detail1 = make_transaction(tx_id="TX1", paid_at="2026-06-16T10:00:00Z")
        tx_detail2 = make_transaction(tx_id="TX2", paid_at="2026-06-21T10:00:00Z")

        mock_spond = AsyncMock()
        mock_spond.get_person = AsyncMock(return_value=member)
        mock_spond.get_events = AsyncMock(return_value=[event1, event2])
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        mock_http = make_mock_http_session(
            get_responses=[
                make_mock_response(tx_list),
                make_mock_response(tx_detail1),
                make_mock_response(tx_detail2),
            ]
        )
        MockSession.return_value = MockAsyncContextManager(mock_http)

        results, name = await _find_cancelled_paid_events("user@example.com")

        assert len(results) == 2
        result_ids = {r["event_id"] for r in results}
        assert result_ids == {"EVT1", "EVT2"}

    @pytest.mark.asyncio
    @patch("app.aiohttp.ClientSession")
    @patch("app.Spond")
    async def test_ignores_non_fulfilled_transactions(self, MockSpond, MockSession):
        from app import _find_cancelled_paid_events

        member = make_person()
        event = make_event(declined_ids=["MEM1"])

        tx_list = [{"id": "TX1", "paymentName": "STV Swim"}]
        tx_detail = make_transaction(status="REFUNDED")

        mock_spond = AsyncMock()
        mock_spond.get_person = AsyncMock(return_value=member)
        mock_spond.get_events = AsyncMock(return_value=[event])
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        mock_http = make_mock_http_session(
            get_responses=[
                make_mock_response(tx_list),
                make_mock_response(tx_detail),
            ]
        )
        MockSession.return_value = MockAsyncContextManager(mock_http)

        results, name = await _find_cancelled_paid_events("user@example.com")
        assert len(results) == 0


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


class TestDoTransfer:
    @pytest.mark.asyncio
    @patch("app.aiohttp.ClientSession")
    @patch("app.Spond")
    async def test_rejects_if_not_declined(self, MockSpond, MockSession):
        from app import _do_transfer

        member = make_person()
        cancelled = make_event(event_id="EVT1", unanswered_ids=["MEM1"])
        target = make_event(event_id="EVT2")

        mock_spond = AsyncMock()
        mock_spond.get_person = AsyncMock(return_value=member)
        mock_spond.get_event = AsyncMock(side_effect=[cancelled, target])
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        with pytest.raises(ValueError, match="cancelled your spot"):
            await _do_transfer("user@example.com", "EVT1", "EVT2")

    @pytest.mark.asyncio
    @patch("app.aiohttp.ClientSession")
    @patch("app.Spond")
    async def test_rejects_if_prices_differ(self, MockSpond, MockSession):
        from app import _do_transfer

        member = make_person()
        cancelled = make_event(
            event_id="EVT1", payment_total=350, declined_ids=["MEM1"]
        )
        target = make_event(event_id="EVT2", payment_total=500)

        mock_spond = AsyncMock()
        mock_spond.get_person = AsyncMock(return_value=member)
        mock_spond.get_event = AsyncMock(side_effect=[cancelled, target])
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        tx_list = [{"id": "TX1", "paymentName": "STV Swim"}]
        tx_detail = make_transaction(total=350)

        mock_http = make_mock_http_session(
            get_responses=[
                make_mock_response(tx_list),
                make_mock_response(tx_detail),
            ]
        )
        MockSession.return_value = MockAsyncContextManager(mock_http)

        with pytest.raises(ValueError, match="prices don't match"):
            await _do_transfer("user@example.com", "EVT1", "EVT2")

    @pytest.mark.asyncio
    @patch("app.aiohttp.ClientSession")
    @patch("app.Spond")
    async def test_rejects_if_no_payment_found(self, MockSpond, MockSession):
        from app import _do_transfer

        member = make_person()
        cancelled = make_event(
            event_id="EVT1", payment_total=350, declined_ids=["MEM1"]
        )
        target = make_event(event_id="EVT2", payment_total=350)

        mock_spond = AsyncMock()
        mock_spond.get_person = AsyncMock(return_value=member)
        mock_spond.get_event = AsyncMock(side_effect=[cancelled, target])
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        mock_http = make_mock_http_session(
            get_responses=[make_mock_response([])]  # no transactions
        )
        MockSession.return_value = MockAsyncContextManager(mock_http)

        with pytest.raises(ValueError, match="payment record"):
            await _do_transfer("user@example.com", "EVT1", "EVT2")

    @pytest.mark.asyncio
    @patch("app._accept_without_payment")
    @patch("app.aiohttp.ClientSession")
    @patch("app.Spond")
    async def test_successful_transfer(self, MockSpond, MockSession, mock_accept):
        from app import _do_transfer

        member = make_person()
        cancelled = make_event(
            event_id="EVT1", payment_total=350, declined_ids=["MEM1"]
        )
        target = make_event(event_id="EVT2", payment_total=350)

        mock_spond = AsyncMock()
        mock_spond.get_person = AsyncMock(return_value=member)
        mock_spond.get_event = AsyncMock(side_effect=[cancelled, target])
        mock_spond.clientsession = AsyncMock()
        MockSpond.return_value = mock_spond

        mock_accept.return_value = {"acceptedIds": ["MEM1"]}

        tx_list = [{"id": "TX1", "paymentName": "STV Swim"}]
        tx_detail = make_transaction(total=350)

        mock_http = make_mock_http_session(
            get_responses=[
                make_mock_response(tx_list),
                make_mock_response(tx_detail),
            ]
        )
        MockSession.return_value = MockAsyncContextManager(mock_http)

        result = await _do_transfer("user@example.com", "EVT1", "EVT2")
        assert "acceptedIds" in result
        # The accept must bypass payment (X-Spond-SkipPayment header path)
        mock_accept.assert_called_once_with(mock_spond, "EVT2", "MEM1")


class TestAcceptWithoutPayment:
    @pytest.mark.asyncio
    async def test_sends_skip_payment_header(self):
        from app import _accept_without_payment

        s = MagicMock()
        s.token = "tok"
        s.api_url = "https://api.spond.com/core/v1/"
        s.auth_headers = {"Authorization": "Bearer tok"}

        captured = {}

        def fake_put(url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return MockAsyncContextManager(
                make_mock_response({"acceptedIds": ["MEM1"]}, status=200)
            )

        s.clientsession = MagicMock()
        s.clientsession.put = fake_put

        result = await _accept_without_payment(s, "EVT2", "MEM1")

        assert result == {"acceptedIds": ["MEM1"]}
        assert captured["headers"]["X-Spond-SkipPayment"] == "true"
        assert captured["json"] == {"accepted": True}
        assert "sponds/EVT2/responses/MEM1" in captured["url"]

    @pytest.mark.asyncio
    async def test_raises_on_payment_required(self):
        from app import _accept_without_payment

        s = MagicMock()
        s.token = "tok"
        s.api_url = "https://api.spond.com/core/v1/"
        s.auth_headers = {"Authorization": "Bearer tok"}

        resp = make_mock_response({"paymentIntent": "pi_x"}, status=402)
        resp.text = AsyncMock(return_value='{"paymentIntent": "pi_x"}')
        s.clientsession = MagicMock()
        s.clientsession.put = MagicMock(
            return_value=MockAsyncContextManager(resp)
        )

        with pytest.raises(ValueError, match="HTTP 402"):
            await _accept_without_payment(s, "EVT2", "MEM1")
