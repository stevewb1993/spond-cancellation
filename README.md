# Spond Session Transfer

A web app that lets triathlon club members transfer a cancelled paid session to a different session of equal value, without admin intervention.

## How it works

1. Member enters their email address
2. The app checks the email belongs to a club member, then emails a 6-digit verification code to that address
3. Member enters the code to prove they own the email
4. The app finds sessions they've cancelled that they previously paid for (via the Spond Club transactions API)
5. Member selects which cancelled session they want to transfer
6. The app shows upcoming sessions (next 7 days) at exactly the same price
7. Member picks a target session and is automatically added to it for free

All transfers are logged in a local SQLite database and viewable at `/admin`.

## Access control

- **Club members only.** The Spond admin account only sees its own club's members, so an email that isn't a club member can't request a code.
- **Verified email.** A member can only act on the sessions belonging to the email they verified — every step keys off that verified email, and the transfer is re-verified server-side before it goes through.
- Verification codes expire after 10 minutes and allow 5 attempts before a new code is required.

## Verification

Before a transfer goes through, the app verifies:

- The member has **declined** the cancelled session in Spond
- A **matching payment** exists in the club's transaction history (matched by payer, event name, date, and `FULFILLED` status)
- The target session costs **exactly the same** as what was paid for the cancelled session

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A Spond account with admin access to your club

### Configuration

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `SPOND_USERNAME` | Your Spond login email |
| `SPOND_PASSWORD` | Your Spond password |
| `SPOND_CLUB_ID` | Your club's ID from the Spond Club API |
| `ADMIN_PASSWORD` | Password for the `/admin` log page |
| `SECRET_KEY` | Flask session secret (use a random string) |
| `SMTP_HOST` | SMTP server for sending codes (default `smtp.gmail.com`) |
| `SMTP_PORT` | SMTP port (default `587`, STARTTLS) |
| `SMTP_USERNAME` | The sending email account (e.g. `bathamphibiansbookings@gmail.com`) |
| `SMTP_PASSWORD` | Gmail **App Password** (16 chars, requires 2FA) — not your normal password |
| `SMTP_FROM` | From address on the emails (defaults to `SMTP_USERNAME`) |

### Setting up the Gmail sender

Verification codes are sent from a Gmail account using an **App Password**:

1. Enable 2-Step Verification on the Gmail account.
2. Go to <https://myaccount.google.com/apppasswords> and create an App Password.
3. Put the 16-character password (spaces removed) in `SMTP_PASSWORD`.

Gmail's free sending limit (~500/day) is far more than this app needs.

### Finding your Club ID

Your club ID can be found by logging into the Spond Club API:

```bash
curl -s -X POST https://api.spond.com/club/v1/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"your@email.com","password":"your-password"}' | \
  python3 -c "import sys,json; token=json.load(sys.stdin)['loginToken']; print(token)" > /tmp/club_token

curl -s https://api.spond.com/club/v1/clubs \
  -H "Authorization: Bearer $(cat /tmp/club_token)" | python3 -m json.tool
```

### Running

```bash
uv run python main.py
```

The app runs at `http://localhost:5001`.

## Pages

- `/` — Enter email to receive a verification code
- `/verify` — Enter the emailed code
- `/cancelled`, `/target` — Multi-step transfer flow (require a verified email)
- `/logout` — Log out the current member
- `/admin` — Password-protected transfer log
