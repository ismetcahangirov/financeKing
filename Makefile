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
.PHONY: help check lint format types imports checks corrupt-fixtures adr-index test cover secrets audit bench up down logs ps config migrate migrate-down migrate-sql seed ingest data-coverage backtest release release-tag rollback-drill restore-drill backup backup-list backup-prune

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
##
## It is set as a target-specific exported variable rather than as a `VAR=value cmd`
## prefix on the recipe line, because that prefix is shell syntax: it forces make to
## hand the whole line to `sh` instead of exec'ing it, and `sh` then eats the
## backslashes in an absolute Windows $(UV) -- turning
## `C:\...\Scripts\uv.exe` into `C:...Scriptsuv.exe: command not found` (#141). Same
## class of trap as the one above, same direction: the gate failed while the thing it
## gates passed. The variable still reaches the child process, which is all the
## paragraph above requires.
imports: export PYTHONIOENCODING = utf-8
imports:  ## import-linter architecture contracts
	$(UV) run lint-imports

## The AST checks enforce rules that no off-the-shelf linter knows about: that a
## money-named field is never a float, that strategy and risk never read the wall
## clock, that SafetyViolation is never caught, that ambiguous trading nouns never
## become identifiers, that nothing but fking.risk constructs an Order -- which
## import-linter cannot express, because the forbidden thing is the call and not the
## import -- that every feature computation is in the registry the
## look-ahead probe iterates, that no agent schema can express a position size or a
## host, and that nothing branches on an LLM's free-text rationale. docs/rules/
## carries the reasoning for each.
##
## property_coverage is the one that gates the tests rather than the source: risk and
## position math without a Hypothesis property test is a merge blocker (#170).
##
## adr_index is not an AST check -- it reads Markdown front matter -- but it belongs
## here for the same reason: a stale decision index is a document that answers a
## question wrongly, and a reader who gets an answer stops looking.
checks:  ## Project-specific AST and documentation checks
	$(UV) run python tools/checks/money_types.py $(SRC)
	$(UV) run python tools/checks/clock_isolation.py $(SRC)
	$(UV) run python tools/checks/no_catch_safety.py $(SRC) tests
	$(UV) run python tools/checks/naming.py $(SRC)
	$(UV) run python tools/checks/order_construction.py $(SRC)
	$(UV) run python tools/checks/feature_registry.py $(SRC)
	$(UV) run python tools/checks/agent_schema_fields.py $(SRC)/agents
	$(UV) run python tools/checks/rationale_untouched.py $(SRC)
	$(UV) run python tools/checks/metric_cardinality.py $(SRC)
	$(UV) run python tools/checks/property_coverage.py $(SRC) tests/property
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
# Schema. docs/rules/append-only-audit.md is the specification for the audit
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

# ---------------------------------------------------------------------------
# Backtest. BACKTEST_ENGINE.md is the specification.
# ---------------------------------------------------------------------------

BACKTEST_CONFIG ?= config/backtest.toml

## Resolves the run's market-data window against the Parquet corpus, prints coverage per
## symbol with the gap ranges, and REFUSES a window it cannot serve from bars that were
## actually observed -- exit 65, no output beyond the report. Bars are never interpolated:
## an invented bar is a price that existed nowhere, at a timestamp at which nobody could
## have traded, and a breakout strategy trades into it and is filled at it
## (BACKTEST_ENGINE.md section 9).
##
## Today the target stops after gating the data and reporting the event-sequence digest;
## the venue simulator, cost model and validation harness are not yet wired to the stream,
## and the command says so in its own output rather than leaving a reader to assume.
backtest:  ## Gate a backtest's data window on coverage; BACKTEST_CONFIG=... to choose the file
	$(UV) run python -m fking.backtest.feed $(BACKTEST_CONFIG) $(ARGS)

## The pinned reference workload -- one strategy, one symbol, a fixed window, a fixed seed,
## a full 28-path CPCV -- reported as wall clock, peak RSS and events/second.
##
## Not part of `check`, and not asserted on a developer machine. The laptop this budget was
## developed on produced 11.6 s and 43.4 s for the identical workload within one session,
## so a local `--check` would be a coin flip. CI runs `--check` on a runner whose spread is
## small enough to mean something; locally this target is for comparing a change against
## the commit before it, back to back, in one sitting. PERFORMANCE_GUIDE.md section 10.
##
##   make bench                                    # measure and print
##   make bench ARGS="--check"                     # gate against the committed budget
##   make bench ARGS="--profile docs/perf/x.md"    # rewrite the profiling record
bench:  ## Measure the pinned backtest reference workload; ARGS="--check" to gate
	$(UV) run python -m tools.bench $(ARGS)

# ---------------------------------------------------------------------------
# Releases. RELEASE_PROCESS.md is the specification; tools/release/ is the part of
# it a machine runs.
# ---------------------------------------------------------------------------

VERSION      ?=
IRREVERSIBLE ?=

## Refuses on a dirty tree, on any branch but main, on a local main that diverges from
## origin, on a version that already exists or does not exceed the last one, on a CI
## verdict that is anything other than green -- including *absent*, which is the normal
## state of a commit pushed a minute ago and is not a pass -- and on an unmarked
## irreversible migration in the range. Every refusal is reported, not just the first:
## a release freeze is a stop-the-world window, and three attempts costs three of them.
##
## It creates NO tag. The notes land in CHANGELOG-v<version>.md and carry the rollback
## procedure that a future incident will be run from; reading that before the immutable
## object quoting it exists is the entire point of the split. `make release-tag` is the
## confirming step.
##
##   make release VERSION=0.4.0
##   make release VERSION=0.4.0 IRREVERSIBLE=1
release:  ## Preflight a release and write its notes. Creates no tag. VERSION=x.y.z
	@test -n "$(VERSION)" || { echo "VERSION=x.y.z is required"; exit 2; }
	$(UV) run python -m tools.release cut --version $(VERSION) \
		$(if $(IRREVERSIBLE),--contains-irreversible-migration,)

## Re-runs every refusal, then creates the annotated tag. It does not push: pushing is
## what triggers .github/workflows/release.yml, so it stays a deliberate act with a name
## against it in the reflog. The command to run next is printed.
release-tag:  ## Create the annotated tag after `make release`. VERSION=x.y.z
	@test -n "$(VERSION)" || { echo "VERSION=x.y.z is required"; exit 2; }
	$(UV) run python -m tools.release cut --version $(VERSION) --confirm \
		$(if $(IRREVERSIBLE),--contains-irreversible-migration,)

## The schema half of the rollback procedure, executed against a real PostgreSQL rather
## than reasoned about. It proves that `alembic downgrade` cannot walk past the audit
## substrate -- and, less obviously, that the refusal arrives *after* every revision
## above it has already been dropped and committed. RELEASE_PROCESS.md 7.
rollback-drill:  ## Execute the rollback drill against a real database
	$(UV) run python -m pytest tests/infra/test_release_rollback_drill.py --no-cov -v

# ---------------------------------------------------------------------------
# Backup and recovery. DEPLOYMENT.md section 9 is the procedure; this is the part
# of it a machine runs.
# ---------------------------------------------------------------------------

BACKUP_DIR ?= backups
KEEP_DAYS  ?= 30

## A dump in pg_dump's custom format, its SHA-256, the Alembic revision it was taken at,
## and the hash-chain tip of every append-only table. The tips are the load-bearing part:
## without them a restore that silently dropped its tail verifies cleanly, because a
## prefix of a valid chain is a valid chain.
##
## The client is version-matched to the server -- from PATH if the majors agree, else
## from the pinned TimescaleDB image via docker. A pg_restore from a different major
## omits objects it does not understand and still exits 0.
backup:  ## Take a verified dump with a chain-tip manifest into BACKUP_DIR
	$(UV) run python -m tools.backup --directory $(BACKUP_DIR) dump

backup-list:  ## List the backups present, newest first
	$(UV) run python -m tools.backup --directory $(BACKUP_DIR) list

## Prints what it would delete and deletes nothing without APPLY=1. Archival that cannot
## be inspected before it runs is indistinguishable from truncation, and three copies are
## retained regardless of age so retention can never remove the last restorable backup.
backup-prune:  ## Apply retention; KEEP_DAYS=30 APPLY=1 to act
	$(UV) run python -m tools.backup --directory $(BACKUP_DIR) prune \
		--keep-days $(KEEP_DAYS) $(if $(APPLY),--apply,)

## A backup nobody has restored is a hypothesis. This target is the experiment: it dumps
## a live database, restores it into a scratch one, and verifies both hash chains against
## the tips its own manifest recorded -- then proves a backup missing its tail fails at
## the exact seq, and that a dump taken under the previous schema upgrades cleanly.
## .github/workflows/restore-drill.yml runs it nightly, because a drill that waits for
## somebody to remember it is a documented intention.
restore-drill:  ## Execute the backup/restore drill against a real database
	$(UV) run python -m pytest tests/infra/test_backup_restore_drill.py --no-cov -v
