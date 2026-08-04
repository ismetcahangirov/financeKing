# financeKing task runner.
#
# Every target goes through `uv run` so that the tool version is the one in
# uv.lock rather than whatever happens to be on PATH. A check that passes locally
# against a different ruff than CI's is a check that has told you nothing.

UV ?= uv
SRC := src/fking
ARGS ?=

COMPOSE ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help check lint format types imports checks corrupt-fixtures adr-index test cover secrets audit up down logs ps config migrate migrate-down migrate-sql seed ingest data-coverage

help:  ## Show the available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

## check runs everything that gates a pull request. CLAUDE.md 12: it must be green
## before opening one, and you must have run it.
check: lint types imports checks test cover  ## Full gate: lint, types, boundaries, AST checks, tests, coverage floors

lint:  ## ruff check + ruff format --check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:  ## Apply ruff formatting and safe fixes
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

## `python -m mypy` rather than the console-script shim: the generated .exe wrapper is
## unsigned and Windows Application Control blocks it on some machines. Identical on
## Linux, and it removes a class of "works on my machine".
types:  ## mypy --strict over src, tests and tools
	$(UV) run python -m mypy

## import-linter must use its shim: `python -m importlinter.cli` imports the module,
## runs no contracts, and exits 0 -- a silent pass that looks exactly like success.
##
## PYTHONIOENCODING is not cosmetic. import-linter's progress spinner is a `rich`
## widget containing a non-ASCII glyph, and on Windows a redirected stdout defaults to
## cp1252, where writing it raises inside rich's teardown. The contracts all pass and
## the process still exits 1 -- so `make check > log` fails while `make check` on a
## terminal succeeds, which is the worst available failure mode for a gate: it is the
## captured run, the one in CI logs and in a PR body, that reports a phantom breach.
imports:  ## import-linter architecture contracts
	PYTHONIOENCODING=utf-8 $(UV) run lint-imports

## The AST checks enforce rules that no off-the-shelf linter knows about: that a
## money-named field is never a float, that strategy and risk never read the wall
## clock, that SafetyViolation is never caught, and that ambiguous trading nouns
## never become identifiers. .claude/rules/ carries the reasoning for each.
##
## adr_index is not an AST check -- it reads Markdown front matter -- but it belongs
## here for the same reason: a stale decision index is a document that answers a
## question wrongly, and a reader who gets an answer stops looking.
checks:  ## Project-specific AST and documentation checks
	$(UV) run python tools/checks/money_types.py $(SRC)
	$(UV) run python tools/checks/clock_isolation.py $(SRC)
	$(UV) run python tools/checks/no_catch_safety.py $(SRC) tests
	$(UV) run python tools/checks/naming.py $(SRC)
	$(UV) run python tools/checks/adr_index.py docs/adr
	$(UV) run python tools/corrupt_archive_fixture.py --check

## The corrupt corpus is derived, not authored: every file under tests/fixtures/corrupt/
## is a declared mutation of a real recording. --check proves the committed bytes still
## match the derivation, which is what makes a hand-edit to a corrupt fixture visible.
## Run without --check to regenerate after adding a mutation.
corrupt-fixtures:  ## Regenerate the corrupted-archive corpus from the recordings
	$(UV) run python tools/corrupt_archive_fixture.py

adr-index:  ## Regenerate the ADR index in docs/adr/README.md
	$(UV) run python tools/checks/adr_index.py --write docs/adr

test:  ## pytest; pass extra flags with ARGS="..."
	$(UV) run python -m pytest $(ARGS)

## Per-module floors, not one global number: a single aggregate lets well-tested
## utilities subsidise untested risk logic forever. CLAUDE.md 5.
cover:  ## Enforce per-module coverage floors
	$(UV) run python tools/coverage_floors.py

## Not part of `check`: gitleaks is a standalone binary rather than a uv
## dependency, so requiring it would make `make check` fail on a clean checkout
## that has only run `uv sync`. CI wires it in separately (#15).
secrets:  ## Scan the working tree for committed secrets (requires gitleaks on PATH)
	gitleaks detect --config .gitleaks.toml --redact --verbose

## Also not part of `check`: it resolves advisories over the network, and a gate that
## fails because PyPI was slow is a gate people learn to re-run rather than read. CI
## runs it on every pull request, and .github/workflows/nightly-security.yml runs it
## on a schedule -- which is the half that matters, because the common shape is an
## advisory published against a dependency that is already locked. SECURITY.md 7.
##
## Exported from uv.lock rather than from the installed environment, so the audit
## covers exactly what a reproducible install would produce.
audit:  ## Audit the locked dependency set for published advisories
	$(UV) export --frozen --all-groups --no-emit-project --format requirements.txt \
		> audited-requirements.txt
	$(UV) tool run pip-audit --strict --requirement audited-requirements.txt

# ---------------------------------------------------------------------------
# Local stack. DEPLOYMENT.md is the specification.
# ---------------------------------------------------------------------------

## Compose loads docker-compose.yml + docker-compose.override.yml automatically,
## so `up` is the developer stack. The demo runtime must be asked for by name:
##   docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d
## That friction is deliberate -- the mode that places orders should not be the
## mode you get by accident.
up:  ## Start the local stack in the background
	$(COMPOSE) up -d --wait

## NOTE: no -v, and there is deliberately no target that adds it. Dropping the
## volumes destroys every audit row and hours of checksum-verified archives, and
## that is an act you perform by typing the full command with the volume name on
## purpose. DEPLOYMENT.md 3.
down:  ## Stop the stack. Never removes volumes
	$(COMPOSE) down

logs:  ## Follow logs; scope with ARGS="postgres redis"
	$(COMPOSE) logs -f --tail=100 $(ARGS)

ps:  ## Show service state and health
	$(COMPOSE) ps

config:  ## Render the merged compose configuration without starting anything
	$(COMPOSE) config

# ---------------------------------------------------------------------------
# Schema. .claude/rules/append-only-audit.md is the specification for the audit
# substrate; docs/adr/0015 records why it is enforced by the database.
# ---------------------------------------------------------------------------

## The DSN comes from the settings tree, not from alembic.ini, so `make migrate` and
## the running application read the same value from the same place.
migrate:  ## Apply every migration up to head
	$(UV) run alembic upgrade head

## One step, never `base`. Reverting to base would attempt 0002, which refuses -- and
## that refusal is the point: rolling back a schema holding the audit trail is a
## data-destruction operation dressed as a schema operation.
migrate-down:  ## Revert the most recent migration
	$(UV) run alembic downgrade -1

## Review the DDL as SQL before it touches anything. Most of this schema's correctness
## lives in triggers, grants and hypertable policies, and none of that is visible in a
## Python diff the way it is in the statement that will actually run.
migrate-sql:  ## Print the SQL for an upgrade without executing it
	$(UV) run alembic upgrade head --sql

seed:  ## Insert venues and instruments for local development; idempotent
	$(UV) run python -m fking.platform.persistence

# ---------------------------------------------------------------------------
# Data. DATA_PIPELINE.md is the specification.
# ---------------------------------------------------------------------------

SYMBOLS  ?= BTCUSDT
INTERVAL ?= 1m
MARKET   ?= spot
DATASET  ?= klines

## Resumable: killing it and re-running produces identical Parquet digests and no
## duplicate registry rows, because resume is derived from the registry and the corpus
## rather than from a progress file. Stops at T-1 -- today's archive does not exist
## until the day is over. Needs the database up (`make up`) for the coverage registry.
ingest:  ## Backfill the archive into the Parquet corpus; SYMBOLS=... INTERVAL=...
	$(UV) run python -m fking.data.backfill ingest \
		--symbols $(SYMBOLS) --interval $(INTERVAL) --market $(MARKET) --dataset $(DATASET)

## The report backtest reads before every run: first timestamp, last timestamp, gap count
## and total gapped duration per series. A window containing a gap either narrows or the
## run refuses -- BACKTEST_ENGINE.md owns that decision, this is the input to it.
data-coverage:  ## Print the coverage and gap report per (market, dataset, symbol)
	$(UV) run python -m fking.data.backfill coverage
