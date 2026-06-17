"""Pytest setup that runs before any test module (and thus before `app` is
imported).

`app.py` calls `load_dotenv()` at import and picks the database backend from
`DATABASE_URL`. We force it empty here so the suite always runs against the
local SQLite path, regardless of whether a real Postgres `DATABASE_URL` is set
in the developer's `.env`. `load_dotenv()` uses `override=False`, so this
value wins over anything in `.env`.
"""

import os

os.environ["DATABASE_URL"] = ""
