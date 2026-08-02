# financeKing task runner.
#
# Every target goes through `uv run` so that the tool version is the one in
# uv.lock rather than whatever happens to be on PATH. A check that passes locally
# against a different ruff than CI's is a check that has told you nothing.

UV ?= uv
SRC := src/fking
ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help check lint format types imports checks test cover secrets

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
imports:  ## import-linter architecture contracts
	$(UV) run lint-imports

## The AST checks enforce rules that no off-the-shelf linter knows about: that a
## money-named field is never a float, that strategy and risk never read the wall
## clock, that SafetyViolation is never caught, and that ambiguous trading nouns
## never become identifiers. .claude/rules/ carries the reasoning for each.
checks:  ## Project-specific AST checks
	$(UV) run python tools/checks/money_types.py $(SRC)
	$(UV) run python tools/checks/clock_isolation.py $(SRC)
	$(UV) run python tools/checks/no_catch_safety.py $(SRC) tests
	$(UV) run python tools/checks/naming.py $(SRC)

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
