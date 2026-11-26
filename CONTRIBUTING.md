# Contributing

Thanks for helping. Bug reports and pull requests are welcome. For a security
problem, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Setup

The project uses [uv](https://docs.astral.sh/uv/).

```console
uv sync                          # create .venv with the package and dev tools
uv run pre-commit install        # optional: run ruff and mypy on every commit
```

## Checks

CI runs these, and a pull request needs all of them to pass:

```console
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run mypy                      # types
uv run pytest -m "not live"      # test suite, without calling Notion
uv run pytest -m "not live" --cov --cov-report=term   # coverage, 95% minimum
```

Ruff does both lint and formatting. Don't add black or isort.

Tests run on SQLite by default. To run them on PostgreSQL, set
`NOTION_SYNC_TEST_DB=postgres` and the usual `PGHOST`, `PGPORT`, `PGUSER`,
`PGPASSWORD` and `PGDATABASE` variables. CI runs both databases on every
supported Python and Django version.

To test against another supported Django version:

```console
uv pip install "django~=5.2.0"
uv run --no-sync pytest -m "not live"
```

### Live tests

Tests marked `live` call the real Notion API, and they're skipped unless
`NOTION_LIVE_TOKEN` is set. They need a throwaway database shared with that
token's integration; `tests/live/conftest.py` lists the properties it must
have. The tests move the pages they create to the trash.

Copy `.env.example` to `.env` and fill it in. The test suite reads `.env` by
itself, and the file is gitignored.

```console
cp .env.example .env
uv run pytest -m live            # only the live tests
```

Once `.env` holds a token, a plain `uv run pytest` runs the live tests too, so
use `-m "not live"` while you work.

### Example project

`example/` is a minimal Django project with the app installed, and
`tests/testapp/` shows a mapped model. Set `NOTION_TOKEN` and
`NOTION_DATA_SOURCE_ID` to give it a target named `inbox`.

```console
uv run python example/manage.py migrate
uv run python example/manage.py notion_sync_check
```

## Code conventions

- Imports are absolute and at the top of the module. Ruff enforces this.
  An import inside a function needs `# noqa: PLC0415` and a comment saying
  why.
- Modules are layered, lowest first: `pages` → `records` → `outbox` → `sync`
  → `api`. A module imports only from the layers below it.
- The public modules (see "Public API" in the README) list their names in
  `__all__` and are fully typed; mypy rejects an unannotated function there.
  A change to a public name needs a CHANGELOG entry.
- Views, management commands and admin actions stay thin. They parse input,
  call a function in the package, and report the result.
- Each test file covers one module: `tests/test_<module>.py`. To fake Notion,
  extend `tests/fake_notion.py` rather than mocking the client.
- Comments explain what a future change could break by accident, not what the
  next line does.
- Tokens never go into a model, a log line or an exception message.

## Changes

- Add a line to the `Unreleased` section of [CHANGELOG.md](CHANGELOG.md) for
  anything a user would notice.
- Generate migrations with `makemigrations`. Never edit one that has been
  released.
- Keep a pull request to one change, and say in it how you tested it.
