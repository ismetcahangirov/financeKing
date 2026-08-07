"""Every table in the operational database, as one SQLAlchemy Core `MetaData`.

This module is the *live* description of the schema. The migrations under
`migrations/versions/` are *historical snapshots* of how it got there, and they
deliberately do not import this object: a migration that reads live metadata stops
being a record of what it did and becomes a function of whatever the model says today,
which makes replaying history from an empty database produce a different result than it
did last year. The two are kept honest by
`test_the_migrated_schema_matches_the_metadata`, which reflects a migrated database and
compares it against this object column by column.

Core, not the ORM. There are no mapped classes here and no relationship graph, for two
reasons. `fking.domain` already owns the types the system reasons about, and they are
frozen dataclasses that import nothing -- mapping them would either mutate them or
produce a second parallel set of "the same" types. And `fking.platform` imports no other
`fking` module (`.claude/rules/module-boundaries.md`), so the mapping could not name
them even if that were desirable. Rows go in and out as tuples of primitives; the
translation to and from domain objects belongs in the module that owns the concept.

The enum-shaped columns carry `CHECK` constraints written as literal string tuples
rather than as `fking.domain` enum members, for that same boundary reason. Those tuples
are asserted equal to the domain enums by `tests/platform/persistence/test_schema_contract.py`,
so a new `Side` member fails a test rather than silently passing a constraint that
rejects it at 03:00.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from fking.platform.persistence._types import identifier, money, utc_timestamp

# Deterministic constraint names. Without a convention Postgres invents them, so a
# constraint is named one thing on the machine that first ran the migration and another
# on the next -- and an error message quoting a name that exists nowhere in the
# repository is an error message nobody can grep for.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

METADATA: Final[sa.MetaData] = sa.MetaData(naming_convention=NAMING_CONVENTION)

# ---------------------------------------------------------------------------
# Closed vocabularies.
#
# Mirrors of the StrEnums in fking.domain, duplicated here because platform may not
# import domain. The duplication is load-bearing rather than accidental: it is checked
# in a test, and the alternative -- a boundary violation to save six lines -- is the
# one thing the layering exists to prevent.
# ---------------------------------------------------------------------------

VENUE_IDS: Final[tuple[str, ...]] = (
    "binance-spot-testnet",
    "binance-futures-testnet",
    "bybit-testnet",
)
DIRECTIONS: Final[tuple[str, ...]] = ("long", "short", "flat")
SIDES: Final[tuple[str, ...]] = ("buy", "sell")
ORDER_TYPES: Final[tuple[str, ...]] = ("market", "limit")
TIME_IN_FORCES: Final[tuple[str, ...]] = ("gtc", "ioc", "fok")
RISK_VERDICTS: Final[tuple[str, ...]] = ("approved", "rejected")

# Vocabularies with no domain counterpart yet. Each names the module that will own it.
MARKETS: Final[tuple[str, ...]] = ("spot", "futures_um")
BAR_SOURCES: Final[tuple[str, ...]] = ("archive", "stream", "rest_backfill")
SNAPSHOT_SOURCES: Final[tuple[str, ...]] = ("local", "venue")
ORDER_STATUSES: Final[tuple[str, ...]] = (
    "pending",
    "submitted",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "expired",
)
LIFECYCLE_STATES: Final[tuple[str, ...]] = (
    "proposed",
    "validating",
    "paper",
    "challenger",
    "champion",
    "quarantined",
    "retired",
)
STRATEGY_ORIGINS: Final[tuple[str, ...]] = ("human", "agent", "mutation", "crossover")
RETIREMENT_REASONS: Final[tuple[str, ...]] = (
    "decayed",
    "risk_violation",
    "superseded",
    "structural_break",
    "operator",
)
LIMIT_UNITS: Final[tuple[str, ...]] = ("usd", "ratio", "count", "per_minute")
# Mirrors `fking.risk.drawdown`. A budget is measured against either one strategy's
# equity or the whole book's, and the two are not interchangeable: a portfolio-scoped
# breach halts everything, a strategy-scoped one halts one subject.
RISK_SCOPES: Final[tuple[str, ...]] = ("strategy", "portfolio")
RISK_LIMIT_NAMES: Final[tuple[str, ...]] = ("drawdown", "daily_loss", "rolling_loss")
KILL_SWITCH_EVENTS: Final[tuple[str, ...]] = ("tripped", "cleared")
AGENT_OUTCOMES: Final[tuple[str, ...]] = ("succeeded", "failed", "degraded")
# Mirrors `fking.platform.scheduler.JobOutcome`, and asserted equal to it in
# `test_schema_contract.py`. `abandoned` is not a third flavour of failure: a job that
# failed told us something about the world, and a run claimed by a process that then died
# tells us only that the process died, which is a different investigation.
SCHEDULER_JOB_OUTCOMES: Final[tuple[str, ...]] = ("succeeded", "failed", "abandoned")
# Mirrors the keys of `fking.data.alt.registry.ALT_SOURCES`, and asserted equal to them in
# `test_schema_contract.py`. A source id that reaches this table without a registered
# declaration is a row whose availability lag nobody stated, which is the one property the
# alternative-source contract exists to guarantee.
ALT_SOURCE_IDS: Final[tuple[str, ...]] = (
    "alternative.me.fearGreed",
    "binance.fundingRate",
    "binance.openInterest",
    "cryptopanic.posts",
    "stlouisfed.fredReleases",
)


def _one_of(column_name: str, permitted: tuple[str, ...]) -> sa.CheckConstraint:
    """A `CHECK (col IN (...))` whose name states the column it guards.

    A native `CREATE TYPE ... AS ENUM` was the alternative and was rejected: adding a
    member to a Postgres enum cannot run inside a transaction on every supported
    version, and removing one is not supported at all, so the type becomes a
    write-once decision enforced by a migration nobody can revert. A `CHECK` is
    replaced by dropping and re-adding it in one reviewable statement.
    """
    members = ", ".join(f"'{member}'" for member in permitted)
    return sa.CheckConstraint(f"{column_name} IN ({members})", name=f"{column_name}_is_known")


def _recorded_at_utc() -> sa.Column[datetime]:
    """When the database saw the row, supplied by the database.

    `clock_timestamp()` rather than `now()`: `now()` is the transaction start time, so
    every row written in one transaction claims the same instant and the ordering
    within it is lost. Never client-supplied -- a writer that stamps its own insertion
    time can also stamp a convenient one.
    """
    return sa.Column(
        "recorded_at_utc",
        utc_timestamp(),
        nullable=False,
        server_default=sa.text("clock_timestamp()"),
    )


# ---------------------------------------------------------------------------
# Reference
# ---------------------------------------------------------------------------

venue = sa.Table(
    "venue",
    METADATA,
    sa.Column("venue_id", identifier(), primary_key=True),
    sa.Column("display_name", identifier(), nullable=False),
    # Not decoration and not a flag: the CHECK below makes a production venue row
    # unrepresentable. The compiled-in host allowlist is the mechanism that stops a
    # production request (.claude/rules/safety-kernel.md); this stops the *database*
    # from being the place someone records one, which is how a second source of truth
    # about "which venues exist" gets started.
    sa.Column("is_testnet", sa.Boolean(), nullable=False, server_default=sa.true()),
    _recorded_at_utc(),
    _one_of("venue_id", VENUE_IDS),
    sa.CheckConstraint("is_testnet", name="venue_is_a_testnet"),
)

instrument = sa.Table(
    "instrument",
    METADATA,
    sa.Column("instrument_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("venue_id", identifier(), sa.ForeignKey("venue.venue_id"), nullable=False),
    sa.Column("symbol", identifier(), nullable=False),
    sa.Column("market", identifier(), nullable=False),
    sa.Column("base_asset", identifier(), nullable=False),
    sa.Column("quote_asset", identifier(), nullable=False),
    sa.Column("tick_size", money(), nullable=False),
    sa.Column("lot_step", money(), nullable=False),
    sa.Column("min_notional_quote", money(), nullable=False),
    # Point-in-time universe membership. Selecting a backtest universe from today's
    # tradable set is survivorship bias; `listed_at_utc <= as_of < COALESCE(delisted...)`
    # is the query that is not (.claude/rules/no-lookahead.md).
    sa.Column("listed_at_utc", utc_timestamp(), nullable=False),
    sa.Column("delisted_at_utc", utc_timestamp(), nullable=True),
    _recorded_at_utc(),
    sa.UniqueConstraint("venue_id", "symbol", name="uq_instrument_venue_id_symbol"),
    sa.Index("ix_instrument_venue_id", "venue_id"),
    _one_of("market", MARKETS),
    sa.CheckConstraint("tick_size > 0", name="tick_size_is_positive"),
    sa.CheckConstraint("lot_step > 0", name="lot_step_is_positive"),
    sa.CheckConstraint("min_notional_quote > 0", name="min_notional_quote_is_positive"),
    sa.CheckConstraint("base_asset <> quote_asset", name="assets_differ"),
    sa.CheckConstraint(
        "delisted_at_utc IS NULL OR delisted_at_utc > listed_at_utc",
        name="delisting_follows_listing",
    ),
)

# Named for what actually gates trading here rather than for the equity-market concept.
# Crypto has no session calendar -- there is no open, no close and no weekend -- so a
# table called `calendar` would invite session logic that has no referent, and the first
# strategy to ask "is the market open?" would get a meaningless answer.
venue_maintenance_window = sa.Table(
    "venue_maintenance_window",
    METADATA,
    sa.Column("window_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("venue_id", identifier(), sa.ForeignKey("venue.venue_id"), nullable=False),
    sa.Column("starts_at_utc", utc_timestamp(), nullable=False),
    sa.Column("ends_at_utc", utc_timestamp(), nullable=False),
    sa.Column("announced_at_utc", utc_timestamp(), nullable=False),
    sa.Column("reason", identifier(), nullable=False),
    _recorded_at_utc(),
    sa.Index("ix_venue_maintenance_window_venue_id_starts_at_utc", "venue_id", "starts_at_utc"),
    sa.CheckConstraint("ends_at_utc > starts_at_utc", name="window_is_ordered"),
)

# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

bar = sa.Table(
    "bar",
    METADATA,
    sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("timeframe", identifier(), nullable=False),
    sa.Column("open_time_utc", utc_timestamp(), nullable=False),
    sa.Column("close_time_utc", utc_timestamp(), nullable=False),
    sa.Column("open_quote_price", money(), nullable=False),
    sa.Column("high_quote_price", money(), nullable=False),
    sa.Column("low_quote_price", money(), nullable=False),
    sa.Column("close_quote_price", money(), nullable=False),
    sa.Column("base_volume", money(), nullable=False),
    sa.Column("quote_volume", money(), nullable=False),
    sa.Column("taker_buy_base_volume", money(), nullable=False),
    sa.Column("taker_buy_quote_volume", money(), nullable=False),
    sa.Column("trade_count", sa.BigInteger(), nullable=False),
    # Provenance, not decoration. When a backtest result is disputed the first question
    # is which rows came from a live stream and when they landed: a stream-sourced bar
    # backfilled after the fact has different provenance from one that arrived on time.
    # DATA_PIPELINE.md section 6.
    sa.Column("source", identifier(), nullable=False),
    _recorded_at_utc(),
    # The single most common data-pipeline defect is a duplicated bar, and it does not
    # announce itself -- it skews every backtest touching the affected range. The
    # primary key is the constraint that makes it impossible, and it leads with the
    # partitioning column so TimescaleDB accepts it.
    sa.PrimaryKeyConstraint("instrument_id", "timeframe", "open_time_utc", name="pk_bar"),
    sa.ForeignKeyConstraint(
        ["instrument_id"], ["instrument.instrument_id"], name="fk_bar_instrument_id_instrument"
    ),
    _one_of("source", BAR_SOURCES),
    sa.CheckConstraint("close_time_utc > open_time_utc", name="bar_is_ordered"),
    sa.CheckConstraint("open_quote_price > 0", name="open_quote_price_is_positive"),
    sa.CheckConstraint("high_quote_price > 0", name="high_quote_price_is_positive"),
    sa.CheckConstraint("low_quote_price > 0", name="low_quote_price_is_positive"),
    sa.CheckConstraint("close_quote_price > 0", name="close_quote_price_is_positive"),
    sa.CheckConstraint("base_volume >= 0", name="base_volume_is_not_negative"),
    sa.CheckConstraint("trade_count >= 0", name="trade_count_is_not_negative"),
    # The cheapest data-quality gate in the system, and it fires on real corruption: a
    # mis-keyed epoch unit puts a 2026 bar next to a 1970 one and the merge that follows
    # produces a high below its own open.
    sa.CheckConstraint(
        "high_quote_price >= GREATEST(open_quote_price, close_quote_price) "
        "AND low_quote_price <= LEAST(open_quote_price, close_quote_price)",
        name="ohlc_brackets_open_and_close",
    ),
)

funding_rate = sa.Table(
    "funding_rate",
    METADATA,
    sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("funding_time_utc", utc_timestamp(), nullable=False),
    # A fraction, not a percentage and not basis points: Binance publishes 0.0001 for
    # one basis point, and a column named `funding_rate` alone is the one people
    # multiply by 100 twice.
    sa.Column("funding_rate_fraction", money(), nullable=False),
    sa.Column("mark_quote_price", money(), nullable=False),
    _recorded_at_utc(),
    sa.PrimaryKeyConstraint("instrument_id", "funding_time_utc", name="pk_funding_rate"),
    sa.ForeignKeyConstraint(
        ["instrument_id"],
        ["instrument.instrument_id"],
        name="fk_funding_rate_instrument_id_instrument",
    ),
    sa.CheckConstraint("mark_quote_price > 0", name="mark_quote_price_is_positive"),
)

# ---------------------------------------------------------------------------
# Ingestion registry
#
# What the bulk backfill knows about the Parquet corpus. Three tables at two grains,
# because two different questions are asked of them and neither answers the other:
#
#   ingest_file      one archive's NormalizationResult -- rejections, per reason
#   ingest_partition one Parquet file's state -- content digest, coverage, event bounds
#   coverage_gap     a period the corpus does not hold, with the instant it was found
#
# `bar_interval` rather than `interval`, which is a reserved word: `SELECT interval FROM`
# is a syntax error, and a column that can only be read quoted is a column that will be
# read wrongly by the first ad-hoc query written against it.
# ---------------------------------------------------------------------------

# The empty string means "this dataset is not keyed by an interval", and the CHECK below
# ties it to the dataset rather than leaving it a convention. NULL was the alternative and
# is wrong here: these columns are primary-key components, a primary key cannot hold NULL,
# and a surrogate key with a `UNIQUE NULLS NOT DISTINCT` index alongside it would put the
# real identity of a row one index definition away from the thing that enforces it.
_NO_INTERVAL: Final[str] = ""

INGEST_GRANULARITIES: Final[tuple[str, ...]] = ("daily", "monthly")
PARTITION_GRAINS: Final[tuple[str, ...]] = ("daily", "monthly")
# Why a period holds no rows. `cadence` and `seam` are claims about *bars* -- the
# interval says how many should be there and they are not -- while `absent_archive` is a
# claim about *publication*, which is all that can be said about a dataset with no
# cadence. A trades file that does not exist is not evidence that any particular print is
# missing, and recording it as though it were would invent a denominator.
GAP_KINDS: Final[tuple[str, ...]] = ("cadence", "seam", "absent_archive")

# What became of a gap, once something filled it. `backfilled` means the corpus now holds
# the whole region; `superseded` means part of it was recovered and narrower rows carry
# what is still absent. There is no `abandoned`: a gap nobody could fill stays unresolved,
# because "we gave up" and "the data is here" must not read the same to a coverage query.
GAP_RESOLUTIONS: Final[tuple[str, ...]] = ("backfilled", "superseded")


def _coordinate_columns() -> tuple[sa.Column[str], ...]:
    """The four columns that identify a series, spelled identically in all three tables.

    Written once because the coverage view joins on all four, and a join on four columns
    that were declared separately three times is a join that silently produces no rows the
    first time one of them is spelled differently.
    """
    return (
        sa.Column("market", identifier(), nullable=False),
        sa.Column("dataset", identifier(), nullable=False),
        sa.Column("symbol", identifier(), nullable=False),
        sa.Column("bar_interval", identifier(), nullable=False),
    )


def _interval_matches_dataset() -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"(dataset = 'klines') = (bar_interval <> '{_NO_INTERVAL}')",
        name="bar_interval_matches_dataset",
    )


ingest_partition = sa.Table(
    "ingest_partition",
    METADATA,
    *_coordinate_columns(),
    # The first calendar day of the period one Parquet file covers: the month's first day
    # for bars, the day itself for trades. Derived from the partition grain rather than
    # from the archive's own date, so that thirty daily archives feeding one monthly file
    # produce one row here and thirty in `ingest_file`.
    sa.Column("period_start_date", sa.Date(), nullable=False),
    sa.Column("partition_grain", identifier(), nullable=False),
    # The calendar range of archives that were actually read into this file. A backfill
    # resumes on `covered_through_date`: a partition whose coverage already reaches the
    # run's target end date and that holds `content_digest_hex` on disk is skipped
    # entirely, which is what makes the current month cheap to extend and a finished month
    # free to revisit.
    sa.Column("covered_from_date", sa.Date(), nullable=False),
    sa.Column("covered_through_date", sa.Date(), nullable=False),
    sa.Column("archive_count", sa.Integer(), nullable=False),
    # Archives inside the covered range that the host does not publish. Non-zero means the
    # partition is complete only in the sense that we asked: the next run asks again,
    # because absence is a claim about upstream and upstream can publish a missing day
    # later. Caching a 404 is how a fixed archive stays invisible forever.
    sa.Column("absent_archive_count", sa.Integer(), nullable=False),
    sa.Column("rows_in", sa.BigInteger(), nullable=False),
    sa.Column("rows_out", sa.BigInteger(), nullable=False),
    sa.Column("rows_rejected", sa.BigInteger(), nullable=False),
    sa.Column("first_event_time_utc", utc_timestamp(), nullable=False),
    sa.Column("last_event_time_utc", utc_timestamp(), nullable=False),
    # The writer's content digest, which covers the records and their provenance and
    # deliberately not the write clock. Comparing it against the digest stored in the
    # Parquet footer is what makes "the registry and the corpus agree" checkable rather
    # than assumed -- a progress file can disagree with both and be believed by neither.
    sa.Column("content_digest_hex", identifier(), nullable=False),
    sa.Column("parquet_path", identifier(), nullable=False),
    sa.Column("written_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.PrimaryKeyConstraint(
        "market",
        "dataset",
        "symbol",
        "bar_interval",
        "period_start_date",
        name="pk_ingest_partition",
    ),
    _one_of("market", MARKETS),
    _one_of("partition_grain", PARTITION_GRAINS),
    _interval_matches_dataset(),
    sa.CheckConstraint("covered_through_date >= covered_from_date", name="coverage_is_ordered"),
    sa.CheckConstraint("last_event_time_utc >= first_event_time_utc", name="events_are_ordered"),
    # A zero-row partition has no file, and a row here claiming one would make a scan
    # report "we have this period" over a path that does not exist.
    sa.CheckConstraint("rows_out > 0", name="rows_out_is_positive"),
    sa.CheckConstraint("rows_in = rows_out + rows_rejected", name="rows_balance"),
    sa.CheckConstraint("archive_count > 0", name="archive_count_is_positive"),
    sa.CheckConstraint("absent_archive_count >= 0", name="absent_archive_count_is_not_negative"),
)

ingest_file = sa.Table(
    "ingest_file",
    METADATA,
    *_coordinate_columns(),
    sa.Column("archive_date", sa.Date(), nullable=False),
    # In the key, because the same month is legitimately read twice under two
    # granularities: a month backfilled from thirty daily archives before its monthly file
    # was published, and re-read as one monthly archive later. Both readings happened and
    # both are recorded; the partition they feed is the same and is written once.
    sa.Column("granularity", identifier(), nullable=False),
    sa.Column("period_start_date", sa.Date(), nullable=False),
    sa.Column("source_checksum_hex", identifier(), nullable=False),
    sa.Column("rows_in", sa.BigInteger(), nullable=False),
    sa.Column("rows_out", sa.BigInteger(), nullable=False),
    sa.Column("rows_rejected", sa.BigInteger(), nullable=False),
    # Per reason, never a bare total. A run reporting "0.4% rejected" cannot distinguish a
    # drifted boolean encoding from a wrong epoch unit; `boolean_unrecognised=1440/1440`
    # can. DATA_PIPELINE.md section 4.
    sa.Column("rejection_reasons", postgresql.JSONB(), nullable=False),
    sa.Column("epoch_unit_applied", identifier(), nullable=False),
    # NULL only for an archive with no data rows at all, which is a real observation on an
    # illiquid symbol and deliberately not the same claim as a gap.
    sa.Column("first_event_time_utc", utc_timestamp(), nullable=True),
    sa.Column("last_event_time_utc", utc_timestamp(), nullable=True),
    sa.Column("ingested_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.PrimaryKeyConstraint(
        "market",
        "dataset",
        "symbol",
        "bar_interval",
        "archive_date",
        "granularity",
        name="pk_ingest_file",
    ),
    _one_of("market", MARKETS),
    _one_of("granularity", INGEST_GRANULARITIES),
    _interval_matches_dataset(),
    sa.CheckConstraint("rows_in = rows_out + rows_rejected", name="rows_balance"),
    sa.CheckConstraint("rows_out >= 0", name="rows_out_is_not_negative"),
)

coverage_gap = sa.Table(
    "coverage_gap",
    METADATA,
    *_coordinate_columns(),
    # Half-open `[start, end)` over *event* time, naming the missing region itself rather
    # than the observations bracketing it. That is what makes `sum(end - start)` a
    # truthful total gapped duration; bracketing bounds would overstate every gap by one
    # bar and understate nothing, which is the direction that reads as reassuring.
    sa.Column("gap_start_utc", utc_timestamp(), nullable=False),
    sa.Column("gap_end_utc", utc_timestamp(), nullable=False),
    sa.Column("gap_kind", identifier(), nullable=False),
    # NULL where the dataset has no cadence. A trades archive that was never published
    # tells you nothing about how many prints are missing, and a zero there would read as
    # "none", which is a stronger claim than the evidence supports.
    sa.Column("missing_bar_count", sa.BigInteger(), nullable=True),
    # First discovery, preserved across re-runs by ON CONFLICT DO NOTHING. This column is
    # the reason the table exists rather than the gap bounds: a gap discovered inside a
    # range a completed backtest already consumed makes those results suspect, and only
    # the discovery instant can tell you which runs are affected
    # (DATA_PIPELINE.md section 11).
    sa.Column("discovered_at_utc", utc_timestamp(), nullable=False),
    # The only two columns a resolved gap gains, and the only two an UPDATE may touch --
    # enforced by the `coverage_gap_resolution_only` trigger from 0012, because no CHECK
    # can express "everything else is frozen". A filled gap is marked rather than deleted:
    # the range was still incomplete for every backtest that ran before the backfill, and
    # `discovered_at_utc` is how those runs are found.
    sa.Column("resolved_at_utc", utc_timestamp(), nullable=True),
    sa.Column("resolution", identifier(), nullable=True),
    _recorded_at_utc(),
    sa.PrimaryKeyConstraint(
        "market",
        "dataset",
        "symbol",
        "bar_interval",
        "gap_start_utc",
        "gap_end_utc",
        name="pk_coverage_gap",
    ),
    _one_of("market", MARKETS),
    _one_of("gap_kind", GAP_KINDS),
    _interval_matches_dataset(),
    sa.CheckConstraint("gap_end_utc > gap_start_utc", name="gap_is_forward"),
    sa.CheckConstraint(
        "missing_bar_count IS NULL OR missing_bar_count > 0", name="missing_bar_count_is_positive"
    ),
    # Spelled out rather than built by `_one_of`, which produces a NOT NULL-shaped
    # membership test: an unresolved gap is the ordinary state and must stay NULL.
    sa.CheckConstraint(
        "resolution IS NULL OR resolution IN ("
        + ", ".join(f"'{member}'" for member in GAP_RESOLUTIONS)
        + ")",
        name="resolution_is_known",
    ),
    # Both columns or neither. A resolution with no instant cannot be ordered against the
    # backtests it invalidates; an instant with no resolution says a gap was closed
    # without saying whether anything was recovered.
    sa.CheckConstraint(
        "(resolution IS NULL) = (resolved_at_utc IS NULL)",
        name="resolution_pairs_with_instant",
    ),
    sa.Index("ix_coverage_gap_discovered_at_utc", "discovered_at_utc"),
    # Partial, on the predicate every reader now uses. Once a corpus has been repaired
    # for a while the unresolved rows are the rare ones, and a full index would be mostly
    # gaps no availability check can be refused by.
    sa.Index(
        "ix_coverage_gap_unresolved",
        "market",
        "dataset",
        "symbol",
        "bar_interval",
        "gap_start_utc",
        postgresql_where=sa.text("resolved_at_utc IS NULL"),
    ),
)

# ---------------------------------------------------------------------------
# Feature store
#
# The one table in this schema the application role holds *no* privilege on, not even
# SELECT. `fking_app` reaches it only through `fking_feature_as_of()`, which takes an
# `as_of` and cannot be asked to ignore it, so a look-ahead defect is a permission error
# rather than a review miss (.claude/rules/no-lookahead.md, DATA_PIPELINE.md section 7).
#
# `available_at_utc` is the earliest instant this system could have known the value;
# `event_time_utc` is when the thing happened. Only the first governs visibility, and the
# CHECK below is what lets the reader omit an event-time bound as unreachable rather than
# carry one that looks load-bearing and is not.
# ---------------------------------------------------------------------------

feature_values = sa.Table(
    "feature_values",
    METADATA,
    sa.Column("feature_name", identifier(), nullable=False),
    # Part of the key, not a column beside it: a definition change is a new version, and
    # the values computed under the old one stay attributable to the definition that
    # produced them. Recomputing history under a new definition would make every earlier
    # backtest a test of a definition that did not exist then.
    sa.Column("feature_version", sa.Integer(), nullable=False),
    sa.Column("market", identifier(), nullable=False),
    sa.Column("symbol", identifier(), nullable=False),
    sa.Column("event_time_utc", utc_timestamp(), nullable=False),
    sa.Column("available_at_utc", utc_timestamp(), nullable=False),
    # `feature_value`, not `value`: .claude/rules/naming.md bans the bare noun, and the
    # money-column scans key on the suffix, so a column named `value` would be invisible
    # to both.
    sa.Column("feature_value", money(), nullable=False),
    _recorded_at_utc(),
    # available_at_utc closes the key, which is what makes a revision a second row rather
    # than an UPDATE of the first.
    sa.PrimaryKeyConstraint(
        "feature_name",
        "feature_version",
        "market",
        "symbol",
        "event_time_utc",
        "available_at_utc",
        name="pk_feature_values",
    ),
    _one_of("market", MARKETS),
    sa.CheckConstraint("feature_version >= 1", name="feature_version_is_positive"),
    sa.CheckConstraint("available_at_utc >= event_time_utc", name="availability_follows_event"),
)

# Raw third-party series -- funding rates, open interest, an index, a macro release --
# before any feature is computed from them. Separate from `feature_values` rather than
# folded into it, because that table is keyed by `(market, symbol)` and read through a
# function that resolves a `FeatureSpec`, whose `compute` is typed over bars with an open
# and a close. A funding rate is not a bar and an economic release has no symbol, so
# registering these as features would mean widening the feature contract to admit things
# that are not features. They are an *input* to features, and they get the same
# point-in-time guarantee by the same mechanism.
alt_observations = sa.Table(
    "alt_observations",
    METADATA,
    sa.Column("source_id", identifier(), nullable=False),
    # `series_id`, not `symbol`: a per-instrument source files under its symbol and a
    # worldwide one under 'GLOBAL'. A column named `symbol` holding 'GLOBAL' is a column
    # somebody will join against the instrument table.
    sa.Column("series_id", identifier(), nullable=False),
    sa.Column("event_time_utc", utc_timestamp(), nullable=False),
    sa.Column("available_at_utc", utc_timestamp(), nullable=False),
    # Generically named on purpose. The unit is a property of the source and is declared
    # once in `fking.data.alt.registry` -- a funding rate is a dimensionless fraction, an
    # open interest is a base quantity, and the Fear & Greed index is an integer in
    # [0, 100]. A column named for any one of those would be wrong for the other two, and
    # `NUMERIC(38, 18)` is what stops the generic name becoming a float.
    sa.Column("observed_value", money(), nullable=False),
    _recorded_at_utc(),
    # available_at_utc closes the key, which is what makes a revision a second row rather
    # than an UPDATE of the first. The macro sources revise weeks after the first print,
    # and the first print is what a decision at the time was actually made on.
    sa.PrimaryKeyConstraint(
        "source_id",
        "series_id",
        "event_time_utc",
        "available_at_utc",
        name="pk_alt_observations",
    ),
    _one_of("source_id", ALT_SOURCE_IDS),
    sa.CheckConstraint("available_at_utc > event_time_utc", name="availability_follows_event"),
)

# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

strategy = sa.Table(
    "strategy",
    METADATA,
    sa.Column("strategy_id", identifier(), primary_key=True),
    sa.Column("family", identifier(), nullable=False),
    sa.Column("origin", identifier(), nullable=False),
    sa.Column("created_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    _one_of("origin", STRATEGY_ORIGINS),
)

strategy_version = sa.Table(
    "strategy_version",
    METADATA,
    sa.Column("strategy_version_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("strategy_id", identifier(), sa.ForeignKey("strategy.strategy_id"), nullable=False),
    sa.Column("version_number", sa.Integer(), nullable=False),
    sa.Column("parameters", postgresql.JSONB(), nullable=False),
    sa.Column("genome", postgresql.JSONB(), nullable=False),
    # Self-referential and nullable: the founding version of a lineage has no parent,
    # and a sentinel row standing in for "no parent" would be a strategy that never
    # existed appearing in every lineage query.
    sa.Column(
        "parent_strategy_version_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("strategy_version.strategy_version_id"),
        nullable=True,
    ),
    # The hash the trial ledger charges against. If it changes between registration and
    # test the result is void rather than weak (.claude/rules/overfitting-defences.md).
    sa.Column("spec_hash", postgresql.BYTEA(), nullable=False),
    sa.Column("lifecycle_state", identifier(), nullable=False),
    sa.Column("created_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.UniqueConstraint(
        "strategy_id", "version_number", name="uq_strategy_version_strategy_id_version_number"
    ),
    sa.Index("ix_strategy_version_strategy_id", "strategy_id"),
    sa.Index("ix_strategy_version_parent_strategy_version_id", "parent_strategy_version_id"),
    _one_of("lifecycle_state", LIFECYCLE_STATES),
    sa.CheckConstraint("version_number >= 1", name="version_number_is_positive"),
    sa.CheckConstraint(
        "parent_strategy_version_id <> strategy_version_id", name="lineage_is_acyclic_at_depth_one"
    ),
)

strategy_lifecycle_transition = sa.Table(
    "strategy_lifecycle_transition",
    METADATA,
    sa.Column("transition_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "strategy_version_id",
        postgresql.UUID(as_uuid=True),
        # Named explicitly: the convention would generate 69 characters, and Postgres
        # truncates an identifier at 63 -- so the constraint would be called one thing
        # in this file and another in the database, which is the worst of both.
        sa.ForeignKey(
            "strategy_version.strategy_version_id",
            name="fk_lifecycle_transition_strategy_version_id",
        ),
        nullable=False,
    ),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("from_state", identifier(), nullable=False),
    sa.Column("to_state", identifier(), nullable=False),
    sa.Column("reason", identifier(), nullable=False),
    sa.Column("survival_score", money(), nullable=True),
    sa.Column("decided_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.Index("ix_strategy_lifecycle_transition_strategy_version_id", "strategy_version_id"),
    sa.Index("ix_strategy_lifecycle_transition_correlation_id", "correlation_id"),
    _one_of("from_state", LIFECYCLE_STATES),
    _one_of("to_state", LIFECYCLE_STATES),
    sa.CheckConstraint("from_state <> to_state", name="transition_moves"),
)

# ---------------------------------------------------------------------------
# Evolution
# ---------------------------------------------------------------------------

generation = sa.Table(
    "generation",
    METADATA,
    sa.Column("generation_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("generation_number", sa.Integer(), nullable=False, unique=True),
    sa.Column("started_at_utc", utc_timestamp(), nullable=False),
    sa.Column("completed_at_utc", utc_timestamp(), nullable=True),
    sa.Column("population_size", sa.Integer(), nullable=False),
    _recorded_at_utc(),
    sa.CheckConstraint("generation_number >= 0", name="generation_number_is_not_negative"),
    sa.CheckConstraint("population_size >= 0", name="population_size_is_not_negative"),
    sa.CheckConstraint(
        "completed_at_utc IS NULL OR completed_at_utc >= started_at_utc",
        name="generation_is_ordered",
    ),
)

evaluation = sa.Table(
    "evaluation",
    METADATA,
    sa.Column("evaluation_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "strategy_version_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("strategy_version.strategy_version_id"),
        nullable=False,
    ),
    sa.Column(
        "generation_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("generation.generation_id"),
        nullable=True,
    ),
    sa.Column("window_start_utc", utc_timestamp(), nullable=False),
    sa.Column("window_end_utc", utc_timestamp(), nullable=False),
    # Forward evidence is what promotion requires; validation performance is the thing
    # that was selected on, so promoting from it is circular. The flag is what lets the
    # promotion gate refuse to read the wrong rows.
    sa.Column("is_forward", sa.Boolean(), nullable=False),
    sa.Column("trade_count", sa.Integer(), nullable=False),
    # Episodes, never observations: 41,208 hourly bars containing 37 distinct funding
    # extremities is a sample of 37, and a t-statistic computed on the former is wrong
    # by a factor of ~33.
    sa.Column("independent_episode_count", sa.Integer(), nullable=False),
    sa.Column("survival_score", money(), nullable=False),
    sa.Column("deflated_sharpe", money(), nullable=False),
    sa.Column("fold_sign_consistency_fraction", money(), nullable=False),
    sa.Column("global_trial_count", sa.BigInteger(), nullable=False),
    sa.Column("evaluated_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.Index("ix_evaluation_strategy_version_id", "strategy_version_id"),
    sa.Index("ix_evaluation_generation_id", "generation_id"),
    sa.CheckConstraint("window_end_utc > window_start_utc", name="window_is_ordered"),
    sa.CheckConstraint("trade_count >= 0", name="trade_count_is_not_negative"),
    sa.CheckConstraint(
        "independent_episode_count >= 0", name="independent_episode_count_is_not_negative"
    ),
    sa.CheckConstraint(
        "fold_sign_consistency_fraction BETWEEN 0 AND 1",
        name="fold_sign_consistency_fraction_is_a_fraction",
    ),
    sa.CheckConstraint("survival_score BETWEEN 0 AND 1", name="survival_score_is_a_fraction"),
    # Zero means the ledger is empty or unreachable; it never means "nothing was tried".
    sa.CheckConstraint("global_trial_count >= 1", name="global_trial_count_was_read"),
)

promotion = sa.Table(
    "promotion",
    METADATA,
    sa.Column("promotion_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "strategy_version_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("strategy_version.strategy_version_id"),
        nullable=False,
    ),
    sa.Column(
        "evaluation_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("evaluation.evaluation_id"),
        nullable=False,
    ),
    sa.Column("from_state", identifier(), nullable=False),
    sa.Column("to_state", identifier(), nullable=False),
    sa.Column("global_trial_count", sa.BigInteger(), nullable=False),
    sa.Column("deflated_sharpe", money(), nullable=False),
    sa.Column("decided_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.Index("ix_promotion_strategy_version_id", "strategy_version_id"),
    sa.Index("ix_promotion_evaluation_id", "evaluation_id"),
    _one_of("from_state", LIFECYCLE_STATES),
    _one_of("to_state", LIFECYCLE_STATES),
    sa.CheckConstraint("global_trial_count >= 1", name="global_trial_count_was_read"),
)

retirement = sa.Table(
    "retirement",
    METADATA,
    sa.Column("retirement_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "strategy_version_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("strategy_version.strategy_version_id"),
        nullable=False,
    ),
    sa.Column("reason_class", identifier(), nullable=False),
    sa.Column("detail", identifier(), nullable=False),
    # Descendants of a strategy retired for a risk violation inherit the quarantine,
    # because the defect is usually in the genome rather than in the instance.
    sa.Column("quarantines_descendants", sa.Boolean(), nullable=False),
    sa.Column("decided_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.Index("ix_retirement_strategy_version_id", "strategy_version_id"),
    _one_of("reason_class", RETIREMENT_REASONS),
)

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

# "order" is reserved in SQL. SQLAlchemy quotes it everywhere, and the name is worth
# the quoting: `trade_order` or `order_record` would be a workaround visible in every
# query for the lifetime of the schema.
order = sa.Table(
    "order",
    METADATA,
    sa.Column("order_id", postgresql.UUID(as_uuid=True), primary_key=True),
    # The exchange-side idempotency key, derived deterministically from the correlation
    # id and the order's content. UNIQUE here so a duplicate placement fails locally
    # before it reaches the venue (.claude/rules/idempotency.md).
    sa.Column("client_order_id", identifier(), nullable=False, unique=True),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column(
        "instrument_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("instrument.instrument_id"),
        nullable=False,
    ),
    sa.Column(
        "strategy_version_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("strategy_version.strategy_version_id"),
        nullable=True,
    ),
    sa.Column("side", identifier(), nullable=False),
    sa.Column("order_type", identifier(), nullable=False),
    sa.Column("time_in_force", identifier(), nullable=False),
    sa.Column("status", identifier(), nullable=False),
    sa.Column("base_quantity", money(), nullable=False),
    sa.Column("limit_quote_price", money(), nullable=True),
    # The price the decision was formed at, which is what slippage is measured against.
    # Without it a fill price is a number with nothing to compare to.
    sa.Column("decision_quote_price", money(), nullable=False),
    sa.Column("venue_order_id", identifier(), nullable=True),
    # The venue's own update sequence, not our clock. A reclaimed `submitted` event
    # arriving after `filled` must not resurrect the order, and our clock is not the
    # ordering authority for the exchange's state machine.
    sa.Column("venue_seq", sa.BigInteger(), nullable=True),
    sa.Column("created_at_utc", utc_timestamp(), nullable=False),
    sa.Column("submitted_at_utc", utc_timestamp(), nullable=True),
    sa.Column("terminal_at_utc", utc_timestamp(), nullable=True),
    _recorded_at_utc(),
    sa.Index("ix_order_correlation_id", "correlation_id"),
    sa.Index("ix_order_instrument_id", "instrument_id"),
    sa.Index("ix_order_strategy_version_id", "strategy_version_id"),
    _one_of("side", SIDES),
    _one_of("order_type", ORDER_TYPES),
    _one_of("time_in_force", TIME_IN_FORCES),
    _one_of("status", ORDER_STATUSES),
    sa.CheckConstraint("base_quantity > 0", name="base_quantity_is_positive"),
    sa.CheckConstraint("decision_quote_price > 0", name="decision_quote_price_is_positive"),
    # A market order carrying a price is one somebody meant to send as a limit: the
    # venue ignores the field and fills at whatever the book offers.
    sa.CheckConstraint(
        "(order_type = 'limit' AND limit_quote_price IS NOT NULL AND limit_quote_price > 0) "
        "OR (order_type = 'market' AND limit_quote_price IS NULL)",
        name="limit_price_matches_order_type",
    ),
)

fill = sa.Table(
    "fill",
    METADATA,
    sa.Column("fill_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("order.order_id"), nullable=False
    ),
    sa.Column("venue_trade_id", identifier(), nullable=False),
    sa.Column(
        "instrument_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("instrument.instrument_id"),
        nullable=False,
    ),
    sa.Column("side", identifier(), nullable=False),
    # The exchange's timestamp, never ours: our clock's relationship to the venue's is
    # an unknown offset plus network delay, which is why Binance rejects requests
    # outside recvWindow with -1021.
    sa.Column("event_time_utc", utc_timestamp(), nullable=False),
    sa.Column("quote_price", money(), nullable=False),
    sa.Column("base_quantity", money(), nullable=False),
    # A charge, not a rate. The two differ by a factor of the notional.
    sa.Column("fee_quote", money(), nullable=False),
    sa.Column("realised_pnl_quote", money(), nullable=False),
    sa.Column("slippage_bp", money(), nullable=False),
    _recorded_at_utc(),
    # The venue's trade id is the only identifier both sides of a reconciliation agree
    # on, so it is what deduplicates an at-least-once redelivery.
    sa.UniqueConstraint("order_id", "venue_trade_id", name="uq_fill_order_id_venue_trade_id"),
    sa.Index("ix_fill_instrument_id_event_time_utc", "instrument_id", "event_time_utc"),
    _one_of("side", SIDES),
    sa.CheckConstraint("quote_price > 0", name="quote_price_is_positive"),
    sa.CheckConstraint("base_quantity > 0", name="base_quantity_is_positive"),
    sa.CheckConstraint("fee_quote >= 0", name="fee_quote_is_not_negative"),
)

position_snapshot = sa.Table(
    "position_snapshot",
    METADATA,
    sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "instrument_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("instrument.instrument_id"),
        nullable=False,
    ),
    sa.Column("observed_at_utc", utc_timestamp(), nullable=False),
    # Both sides of the reconciliation are stored, and neither overwrites the other.
    # A single "current position" row would make a divergence unobservable: the two
    # numbers disagreeing is the finding.
    sa.Column("source", identifier(), nullable=False),
    sa.Column("direction", identifier(), nullable=False),
    sa.Column("base_quantity", money(), nullable=False),
    sa.Column("average_entry_quote_price", money(), nullable=False),
    sa.Column("unrealised_pnl_quote", money(), nullable=False),
    sa.Column("realised_pnl_quote", money(), nullable=False),
    _recorded_at_utc(),
    sa.Index(
        "ix_position_snapshot_instrument_id_observed_at_utc", "instrument_id", "observed_at_utc"
    ),
    _one_of("source", SNAPSHOT_SOURCES),
    _one_of("direction", DIRECTIONS),
    sa.CheckConstraint("(direction = 'flat') = (base_quantity = 0)", name="flat_is_exactly_zero"),
    sa.CheckConstraint("base_quantity >= 0", name="base_quantity_is_unsigned"),
)

account_snapshot = sa.Table(
    "account_snapshot",
    METADATA,
    sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("venue_id", identifier(), sa.ForeignKey("venue.venue_id"), nullable=False),
    sa.Column("observed_at_utc", utc_timestamp(), nullable=False),
    sa.Column("source", identifier(), nullable=False),
    sa.Column("equity_usd", money(), nullable=False),
    sa.Column("free_balance_usd", money(), nullable=False),
    sa.Column("margin_balance_usd", money(), nullable=False),
    _recorded_at_utc(),
    sa.Index("ix_account_snapshot_venue_id_observed_at_utc", "venue_id", "observed_at_utc"),
    _one_of("source", SNAPSHOT_SOURCES),
)

# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------

risk_decision = sa.Table(
    "risk_decision",
    METADATA,
    sa.Column("decision_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column(
        "instrument_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("instrument.instrument_id"),
        nullable=False,
    ),
    sa.Column(
        "strategy_version_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("strategy_version.strategy_version_id"),
        nullable=True,
    ),
    sa.Column("verdict", identifier(), nullable=False),
    sa.Column(
        "order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("order.order_id"), nullable=True
    ),
    sa.Column("rejection_reason", identifier(), nullable=True),
    sa.Column("conviction", money(), nullable=False),
    sa.Column("portfolio_notional_usd", money(), nullable=False),
    sa.Column("decided_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.Index("ix_risk_decision_correlation_id", "correlation_id"),
    sa.Index("ix_risk_decision_instrument_id", "instrument_id"),
    sa.Index("ix_risk_decision_strategy_version_id", "strategy_version_id"),
    sa.Index("ix_risk_decision_order_id", "order_id"),
    _one_of("verdict", RISK_VERDICTS),
    # An approved decision with no order is one nothing can act on; a rejected decision
    # carrying an order is one somebody downstream acts on anyway, because the order is
    # right there and the verdict is only a string.
    sa.CheckConstraint(
        "(verdict = 'approved' AND order_id IS NOT NULL AND rejection_reason IS NULL) "
        "OR (verdict = 'rejected' AND order_id IS NULL AND rejection_reason IS NOT NULL)",
        name="verdict_governs_order_and_reason",
    ),
    sa.CheckConstraint("conviction BETWEEN 0 AND 1", name="conviction_is_a_fraction"),
)

limit_breach = sa.Table(
    "limit_breach",
    METADATA,
    sa.Column("breach_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "decision_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("risk_decision.decision_id"),
        nullable=True,
    ),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("limit_name", identifier(), nullable=False),
    # The unit is a column because the limits are heterogeneous -- USD notionals,
    # ratios, counts per minute -- and two numbers with no stated unit are two numbers
    # somebody will compare.
    sa.Column("limit_unit", identifier(), nullable=False),
    sa.Column("threshold_in_unit", money(), nullable=False),
    sa.Column("observed_in_unit", money(), nullable=False),
    sa.Column("breached_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.Index("ix_limit_breach_correlation_id", "correlation_id"),
    sa.Index("ix_limit_breach_decision_id", "decision_id"),
    _one_of("limit_unit", LIMIT_UNITS),
)

kill_switch_event = sa.Table(
    "kill_switch_event",
    METADATA,
    sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
    # Two rows rather than one row updated on clear. A `cleared_at_utc` column would be
    # an UPDATE on an append-only table, and the workaround for that is always to make
    # the table not append-only.
    sa.Column("event_type", identifier(), nullable=False),
    sa.Column("reason", identifier(), nullable=False),
    sa.Column("actor", identifier(), nullable=False),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("occurred_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.Index("ix_kill_switch_event_occurred_at_utc", "occurred_at_utc"),
    _one_of("event_type", KILL_SWITCH_EVENTS),
)

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

agent_run = sa.Table(
    "agent_run",
    METADATA,
    sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("agent_id", identifier(), nullable=False),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("started_at_utc", utc_timestamp(), nullable=False),
    sa.Column("completed_at_utc", utc_timestamp(), nullable=True),
    sa.Column("outcome", identifier(), nullable=True),
    sa.Column("escalated_to", identifier(), nullable=True),
    _recorded_at_utc(),
    sa.Index("ix_agent_run_correlation_id", "correlation_id"),
    sa.Index("ix_agent_run_agent_id_started_at_utc", "agent_id", "started_at_utc"),
    # Not `_one_of`: the column is nullable while a run is in flight, and a bare
    # `outcome IN (...)` would reject the NULL that means "still running".
    sa.CheckConstraint(
        "outcome IS NULL OR outcome IN ({})".format(
            ", ".join(f"'{member}'" for member in AGENT_OUTCOMES)
        ),
        name="outcome_is_known",
    ),
    sa.CheckConstraint(
        "(completed_at_utc IS NULL) = (outcome IS NULL)", name="completion_carries_an_outcome"
    ),
)

agent_call = sa.Table(
    "agent_call",
    METADATA,
    sa.Column("call_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "run_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("agent_run.run_id"),
        nullable=False,
    ),
    sa.Column("provider", identifier(), nullable=False),
    # A pinned model id, never a floating alias. The same prompt to two successive
    # models is two different experiments, and a provider rolling `*-latest` forward
    # changes every result in the project with no diff.
    sa.Column("model_id", identifier(), nullable=False),
    sa.Column("temperature", money(), nullable=False),
    # Verbatim, in the database rather than in the log stream: log retention expires
    # and ARCHITECTURE.md section 11 needs the exact prompt months later. The log line
    # carries the audit reference and nothing else from the payload.
    sa.Column("prompt_text", sa.Text(), nullable=False),
    sa.Column("response_text", sa.Text(), nullable=True),
    sa.Column("prompt_token_count", sa.Integer(), nullable=False),
    sa.Column("completion_token_count", sa.Integer(), nullable=False),
    sa.Column("latency_ms", sa.Integer(), nullable=False),
    sa.Column("cache_hit", sa.Boolean(), nullable=False),
    sa.Column("schema_valid", sa.Boolean(), nullable=False),
    sa.Column("called_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.Index("ix_agent_call_run_id", "run_id"),
    sa.Index("ix_agent_call_model_id_called_at_utc", "model_id", "called_at_utc"),
    sa.CheckConstraint("temperature >= 0", name="temperature_is_not_negative"),
    sa.CheckConstraint("prompt_token_count >= 0", name="prompt_token_count_is_not_negative"),
    sa.CheckConstraint(
        "completion_token_count >= 0", name="completion_token_count_is_not_negative"
    ),
    sa.CheckConstraint("latency_ms >= 0", name="latency_ms_is_not_negative"),
    # A response that failed schema validation is still recorded verbatim -- that row is
    # the evidence a prompt regressed. Only a call that produced no response at all may
    # leave it null.
    sa.CheckConstraint(
        "response_text IS NOT NULL OR schema_valid = false", name="valid_response_has_text"
    ),
)

# ---------------------------------------------------------------------------
# Audit substrate
# ---------------------------------------------------------------------------

audit_log = sa.Table(
    "audit_log",
    METADATA,
    # GENERATED ALWAYS, so a writer cannot supply its own sequence number. Postgres
    # computes column defaults before BEFORE ROW triggers fire, which is what lets the
    # chain trigger read `NEW.seq` while assembling the digest.
    sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
    sa.Column("occurred_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("actor", identifier(), nullable=False),
    sa.Column("event_type", identifier(), nullable=False),
    sa.Column("subject_id", identifier(), nullable=False),
    sa.Column("payload", postgresql.JSONB(), nullable=False),
    # Forbidding a rewrite is not the same as detecting one. A superuser, a
    # pg_dump/restore or direct file access can still change history; the chain is what
    # makes that visible, and it is the part people skip.
    sa.Column("prev_hash", postgresql.BYTEA(), nullable=False),
    sa.Column("row_hash", postgresql.BYTEA(), nullable=False),
    sa.Index("ix_audit_log_correlation_id_occurred_at_utc", "correlation_id", "occurred_at_utc"),
    sa.Index("ix_audit_log_event_type_occurred_at_utc", "event_type", "occurred_at_utc"),
)

trial_ledger = sa.Table(
    "trial_ledger",
    METADATA,
    sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
    sa.Column("charged_at_utc", utc_timestamp(), nullable=False),
    _recorded_at_utc(),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("spec_hash", postgresql.BYTEA(), nullable=False, unique=True),
    sa.Column("registered_by", identifier(), nullable=False),
    sa.Column("statement", sa.Text(), nullable=False),
    sa.Column("parameter_grid", postgresql.JSONB(), nullable=False),
    sa.Column("n_parameters", sa.Integer(), nullable=False),
    sa.Column("n_symbols", sa.Integer(), nullable=False),
    sa.Column("n_variants", sa.Integer(), nullable=False),
    # The full declared grid, charged whether or not every point is ever run. Abandoning
    # a search after the first twelve points look good IS the selection event, and
    # charging at execution prices it at zero.
    sa.Column("trials_charged", sa.Integer(), nullable=False),
    sa.Column("cumulative_trials", sa.BigInteger(), nullable=False),
    sa.Column("holdout_touched", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("human_authorisation_ref", identifier(), nullable=True),
    sa.Column("prev_hash", postgresql.BYTEA(), nullable=False),
    sa.Column("row_hash", postgresql.BYTEA(), nullable=False),
    sa.Index("ix_trial_ledger_correlation_id", "correlation_id"),
    sa.CheckConstraint("n_parameters >= 0", name="n_parameters_is_not_negative"),
    sa.CheckConstraint("n_symbols >= 1", name="n_symbols_is_positive"),
    sa.CheckConstraint("n_variants >= 1", name="n_variants_is_positive"),
    sa.CheckConstraint("trials_charged >= 1", name="trials_charged_is_positive"),
    # Reading the permanently held-out period burns it, and burning it is a decision a
    # human takes once. A row claiming to have touched it with nobody's name on it is a
    # row that should not exist.
    sa.CheckConstraint(
        "NOT holdout_touched OR human_authorisation_ref IS NOT NULL",
        name="holdout_needs_authorisation",
    ),
)

# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

processed_events = sa.Table(
    "processed_events",
    METADATA,
    # The composite primary key is the mechanism, not an index choice: it is the
    # arbiter `INSERT ... ON CONFLICT DO NOTHING` needs, and without it the claim
    # statement errors at runtime rather than deduplicating. Keyed by consumer group as
    # well as by event, because two groups consuming the same stream must each apply the
    # event once.
    sa.Column("consumer_group", identifier(), nullable=False),
    # A hash of the event's *semantic content*, never the stream message id. A message
    # id is a delivery coordinate and fails in both directions: XAUTOCLAIM redelivers a
    # message under the same id, and a producer retrying after a timeout republishes the
    # same event under a new one. Deduplicating on the id lets the second case through,
    # and the second append is a fill the position counts twice.
    sa.Column("idempotency_key", identifier(), nullable=False),
    sa.Column("stream", identifier(), nullable=False),
    sa.Column("message_id", identifier(), nullable=False),
    _recorded_at_utc(),
    sa.PrimaryKeyConstraint("consumer_group", "idempotency_key", name="pk_processed_events"),
    # Retention: a consumer cannot deduplicate against rows it has pruned, so the
    # retention window must exceed the longest possible redelivery. The index is what
    # makes pruning by age a range scan rather than a sequential one.
    sa.Index("ix_processed_events_recorded_at_utc", "recorded_at_utc"),
)

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

scheduler_job_run = sa.Table(
    "scheduler_job_run",
    METADATA,
    sa.Column("job_id", identifier(), nullable=False),
    # What the run is *for*, never the instant it started. An hourly ingestion replaying
    # the 04:00 window is a run of 04:00 whenever it happens, which is what makes this
    # column half of a durable idempotency key rather than a timing observation.
    sa.Column("scheduled_fire_utc", utc_timestamp(), nullable=False),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("is_catch_up", sa.Boolean(), nullable=False),
    sa.Column("finished_at_utc", utc_timestamp(), nullable=True),
    sa.Column("outcome", identifier(), nullable=True),
    sa.Column("failure_reason", sa.Text(), nullable=True),
    _recorded_at_utc(),
    # The composite primary key is the mechanism rather than an index choice, exactly as
    # in `processed_events`: it is the arbiter that `INSERT ... ON CONFLICT DO NOTHING`
    # needs, and it is what makes "a restart does not re-fire a window that already ran"
    # a property of a unique index instead of of a code path.
    sa.PrimaryKeyConstraint("job_id", "scheduled_fire_utc", name="pk_scheduler_job_run"),
    # Partial, on the predicate the boot sweep and the overlap check both use. A beat
    # with years of history has a handful of unfinished rows at most, and a full index
    # over every run ever recorded would be almost entirely rows neither query can match.
    sa.Index(
        "ix_scheduler_job_run_unfinished",
        "job_id",
        "scheduled_fire_utc",
        postgresql_where=sa.text("finished_at_utc IS NULL"),
    ),
    # Not `_one_of`: the column is NULL while a run is in flight, and a bare
    # `outcome IN (...)` would reject the NULL that means "still running".
    sa.CheckConstraint(
        "outcome IS NULL OR outcome IN ({})".format(
            ", ".join(f"'{member}'" for member in SCHEDULER_JOB_OUTCOMES)
        ),
        name="outcome_is_known",
    ),
    sa.CheckConstraint(
        "(finished_at_utc IS NULL) = (outcome IS NULL)", name="completion_carries_an_outcome"
    ),
    # A reason belongs to a run that ended, and a run that succeeded has none to give.
    # Without the second clause a green run could carry an explanation, which is exactly
    # the row a later investigation would misread.
    sa.CheckConstraint(
        "failure_reason IS NULL OR outcome IS NOT NULL", name="reason_needs_an_outcome"
    ),
    sa.CheckConstraint(
        "outcome <> 'succeeded' OR failure_reason IS NULL", name="success_carries_no_reason"
    ),
)

# ---------------------------------------------------------------------------
# Risk state
# ---------------------------------------------------------------------------

# The drawdown numbers a restart must not be allowed to recompute. If the high-water mark
# initialises from current equity after a restart, the budget silently re-bases itself
# below the lower equity -- the system grants itself fresh drawdown at exactly the moment
# the evidence says it should have less, and nothing logs an error.
risk_drawdown_state = sa.Table(
    "risk_drawdown_state",
    METADATA,
    sa.Column("state_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("scope", identifier(), nullable=False),
    sa.Column("subject_id", identifier(), nullable=False),
    sa.Column("peak_equity_usd", money(), nullable=False),
    sa.Column("current_equity_usd", money(), nullable=False),
    sa.Column("day_start_utc", utc_timestamp(), nullable=False),
    sa.Column("day_open_equity_usd", money(), nullable=False),
    sa.Column("observed_at_utc", utc_timestamp(), nullable=False),
    sa.Column("breach_limit_name", identifier(), nullable=True),
    sa.Column("breach_observed_ratio", money(), nullable=True),
    sa.Column("breach_budget_ratio", money(), nullable=True),
    sa.Column("breached_at_utc", utc_timestamp(), nullable=True),
    _recorded_at_utc(),
    sa.UniqueConstraint("scope", "subject_id"),
    _one_of("scope", RISK_SCOPES),
    sa.CheckConstraint("btrim(subject_id) <> ''", name="subject_id_is_not_blank"),
    # Every ratio here divides by an equity figure, so zero equity does not make a
    # drawdown smaller -- it makes it undefined.
    sa.CheckConstraint(
        "peak_equity_usd > 0 AND current_equity_usd > 0 AND day_open_equity_usd > 0",
        name="equity_is_positive",
    ),
    # Deliberately redundant with DrawdownState.__post_init__. The type guards the
    # process running now; the constraint guards the row against every writer this
    # schema will ever have, including a repair script run by hand during an incident --
    # which is exactly when somebody is tempted to "fix" a peak downward.
    sa.CheckConstraint("peak_equity_usd >= current_equity_usd", name="peak_is_at_least_current"),
    # date_trunc('day', timestamptz) truncates in the *session* time zone, so the same
    # row would satisfy this on a UTC connection and violate it on one whose TimeZone was
    # set to anything else. Converting at an explicit UTC offset removes the session from
    # the expression entirely.
    sa.CheckConstraint(
        "day_start_utc = date_trunc('day', day_start_utc AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
        name="day_start_is_a_utc_boundary",
    ),
    sa.CheckConstraint(
        "day_start_utc <= observed_at_utc", name="day_start_precedes_the_observation"
    ),
    # All four breach columns present together or absent together. A half-written breach
    # is a subject that reads as halted with no threshold to explain it, or as trading
    # with a threshold that was already crossed.
    sa.CheckConstraint(
        "num_nonnulls(breach_limit_name, breach_observed_ratio, breach_budget_ratio, "
        "breached_at_utc) IN (0, 4)",
        name="breach_is_whole",
    ),
    sa.CheckConstraint(
        "breach_limit_name IS NULL OR breach_limit_name IN ("
        + ", ".join(f"'{member}'" for member in RISK_LIMIT_NAMES)
        + ")",
        name="breach_limit_name_is_known",
    ),
    sa.CheckConstraint(
        "(breach_observed_ratio IS NULL OR breach_observed_ratio >= 0) "
        "AND (breach_budget_ratio IS NULL OR breach_budget_ratio > 0)",
        name="breach_ratios_are_fractions",
    ),
)

# The trailing window, as a child table rather than a jsonb column on the parent: an
# equity figure inside a blob is invisible to the information_schema scan that asserts no
# money column is DOUBLE PRECISION, and NUMERIC(38, 18) per mark is what lets that scan
# see them at all.
risk_drawdown_mark = sa.Table(
    "risk_drawdown_mark",
    METADATA,
    sa.Column("state_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("observed_at_utc", utc_timestamp(), nullable=False),
    sa.Column("equity_usd", money(), nullable=False),
    _recorded_at_utc(),
    sa.PrimaryKeyConstraint("state_id", "observed_at_utc"),
    # CASCADE rather than RESTRICT: a window without its state row is a set of equity
    # readings nothing can interpret, and leaving it behind would make a later re-open of
    # the same subject inherit a stranger's window.
    sa.ForeignKeyConstraint(["state_id"], ["risk_drawdown_state.state_id"], ondelete="CASCADE"),
    sa.CheckConstraint("equity_usd > 0", name="equity_is_positive"),
)

# ---------------------------------------------------------------------------
# Append-only classification
# ---------------------------------------------------------------------------

# Tables the application role may INSERT and SELECT but never UPDATE, DELETE or
# TRUNCATE, enforced by revoked grants AND a BEFORE UPDATE OR DELETE trigger. Grants are
# the primary control -- TRUNCATE does not fire row triggers -- and the trigger is the
# backstop for the migration that later hands a broad role to a new service.
APPEND_ONLY_TABLES: Final[frozenset[str]] = frozenset(
    {
        "audit_log",
        "trial_ledger",
        "agent_call",
        "fill",
        "risk_decision",
        "limit_breach",
        "kill_switch_event",
        "strategy_lifecycle_transition",
        "promotion",
        "retirement",
        "position_snapshot",
        "account_snapshot",
    }
)

# The subset that additionally carries a per-row hash chain, and whose migration is
# irreversible. These two are the ledgers every other claim in the system is checked
# against: the audit log answers "why did this trade happen", and the trial ledger is
# the denominator of every deflated Sharpe the project reports.
HASH_CHAINED_TABLES: Final[frozenset[str]] = frozenset({"audit_log", "trial_ledger"})

# What `test_schema_contract.py` and the information_schema scan key on. A column named
# `price` would be invisible to both, which is why .claude/rules/naming.md bans the bare
# noun in the first place.
MONEY_COLUMN_SUFFIXES: Final[tuple[str, ...]] = (
    "_quote",
    "_price",
    "_quantity",
    "_usd",
    "_bp",
    "_bps",
    "_pnl",
    "_fee",
    "_volume",
    "_notional",
)

__all__: tuple[str, ...] = (
    "ALT_SOURCE_IDS",
    "APPEND_ONLY_TABLES",
    "GAP_KINDS",
    "GAP_RESOLUTIONS",
    "HASH_CHAINED_TABLES",
    "INGEST_GRANULARITIES",
    "METADATA",
    "MONEY_COLUMN_SUFFIXES",
    "NAMING_CONVENTION",
    "PARTITION_GRAINS",
    "RISK_LIMIT_NAMES",
    "RISK_SCOPES",
    "SCHEDULER_JOB_OUTCOMES",
)
