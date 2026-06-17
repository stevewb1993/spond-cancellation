# Spond Session Transfer

A web app that lets triathlon club members transfer a cancelled paid session to a different session of equal value, without admin intervention.

## How it works

1. Member enters their email address
2. The app finds sessions they've cancelled that they previously paid for (via the Spond Club transactions API)
3. Member selects which cancelled session they want to transfer
4. The app shows upcoming sessions (next 7 days) at exactly the same price
5. Member picks a target session and is automatically added to it for free

All transfers are logged in a local SQLite database and viewable at `/admin`.

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

- `/` — Member-facing transfer form (multi-step)
- `/admin` — Password-protected transfer log
