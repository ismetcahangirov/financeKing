# Rule — No Look-Ahead

## The rule

**A feature value computed at time *t* must be reproducible using only data that existed at *t*.** Not "data timestamped before *t*" — data that had *arrived* by *t*.

Nine clauses, all mechanical:

1. **Every record carries both `event_time` and `available_at`.** `event_time` is when the thing happened; `available_at` is the earliest instant this system could have known it. They are different, `available_at >= event_time` is a database `CHECK`, and only `available_at` governs visibility.
2. **The feature store is queried with an explicit `as_of` and physically cannot return rows with `available_at > as_of`.** Not by convention — the application role has no `SELECT` on the underlying table.
3. **Bar alignment: you may not trade on the close of the bar whose close you used to decide.** A decision derived from bar `[t0, t1)` has `decision_time >= t1` and its earliest fill is in the bar after that.
4. **Label alignment shifts by one bar.** The label for a decision at bar *i* is measured from the entry the decision could actually have achieved — the open of bar *i+1* — not from the close of bar *i*.
5. **Rolling normalization uses trailing statistics only.** Never a full-sample mean, std, quantile, min, max, or `StandardScaler.fit` over the whole series.
6. **The symbol universe is point-in-time.** Membership is resolved `as_of`, from listing and delisting timestamps, never from today's symbol list.
7. **Cross-validation purges and embargoes, sized to the label horizon.** Purge = the full label horizon on both sides of every test fold; embargo follows the test fold.
8. **Revisions are appended, never updated.** A corrected value is a new row with a later `available_at`; the original stays.
9. **Every feature declares `availability_lag` and `label_horizon` in the registry.** A feature without both cannot be registered, and therefore cannot be used.

## Why

This is the most dangerous defect class in the project, and the reason is stated in one sentence in `../../CLAUDE.md` §2: **it does not fail, it makes bad strategies look excellent.**

Every other defect class announces itself. A `float` in the money path produces a reconciliation drift someone eventually chases. A missing `correlation_id` produces an investigation that stalls. A bypassed `guarded_client()` fails a contract in CI. Look-ahead produces a Sharpe of 2.4, a clean equity curve, a validation gate that passes, a promotion, a live deployment, and six weeks of confused underperformance that gets attributed to regime change. By the time anyone suspects the feature pipeline, the strategy has been mutated four times and its descendants inherited the leak.

The asymmetry that makes this worth extreme measures: the cost of a false negative is one rejected feature. The cost of a false positive is a corrupted scoring engine, and `../../ARCHITECTURE.md` §13 names exactly this — validated results that decay forward — as the assumption most likely to be wrong in the whole system and the one to watch hardest.

`event_time` versus `available_at` is the distinction almost every leak reduces to. A funding rate has an `event_time` of the settlement instant and an `available_at` of when the venue published it. A corrected bar has the original `event_time` and an `available_at` hours later. An exchange listing has an `event_time` of the listing announcement and an `available_at` of when your ingester saw it. Filter on `event_time` and you have written a backtest that knew about corrections before they were issued. The single most common form of this bug is `WHERE event_time <= :t`, and it looks completely correct.

The universe clause is survivorship bias in its operational form. Backtesting today's tradable symbols over 2021–2026 tests a universe selected by having survived to 2026, and that selection is worth several points of annualised return on its own. On this platform there is a second layer: testnet's listed set differs from production's by 79 symbols on spot and 189 on futures (`./exchange-integration.md`, `../contexts/binance-testnet.md`), so "the symbols we can trade" and "the symbols we have history for" are different sets whose intersection changes over time.

## Incorrect

```python
import pandas as pd


def build_features(bars: pd.DataFrame, funding: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
    df = bars[bars["event_time"] <= t].copy()

    df["z_close"] = (df["close"] - df["close"].mean()) / df["close"].std()
    df["rv_20"] = df["close"].pct_change().rolling(20).std()
    df["funding"] = df.merge(funding, on="event_time", how="left")["rate"]

    df["label"] = df["close"].shift(-24) / df["close"] - 1
    return df


UNIVERSE = [m["symbol"] for m in exchange.load_markets()]   # today's list
```

Five leaks, none of which raises:

`bars["event_time"] <= t` filters on when the bar happened, not on when it arrived. Any revised bar in this frame carries its corrected value under its original timestamp, so the backtest sees a correction that had not been published yet.

`df["close"].mean()` and `.std()` are computed over the whole slice, so the z-score at row 10 is normalised against statistics that include row 40,000. This is the leak that most often survives review, because the slice *is* bounded by `t` and it looks point-in-time. It is not: within the slice, every row sees every other row.

`.rolling(20).std()` includes the current bar. Whether that is a leak depends entirely on whether the current bar has closed at decision time, and the code does not say — which means it will be wrong in one of backtest or live.

The funding merge is an exact join on `event_time` with no availability lag, so a funding rate settled at 08:00:00 is visible in the 08:00:00 bar even though the venue publishes it afterwards.

`df["close"].shift(-24) / df["close"] - 1` labels the decision using the *close of the decision bar* as the entry price. You could not have transacted at a price that was only known once the bar was over. The measured edge includes the move from the last trade of the bar to the price you would actually have paid — which, for any momentum or reversal feature computed from that same close, is exactly the move the feature is built on. This single line reliably produces a strategy that looks profitable and is not.

`UNIVERSE` from `load_markets()` is today's set: delisted symbols are absent, and symbols listed in 2024 appear to have been tradable in 2021.

## Correct

The storage layer makes the guarantee, not the caller:

```sql
-- alembic/versions/0007_feature_store_as_of.py
CREATE TABLE feature_values (
    feature_id   text        NOT NULL,
    symbol       text        NOT NULL,
    event_time   timestamptz NOT NULL,
    available_at timestamptz NOT NULL,
    value        numeric     NOT NULL,
    revision     integer     NOT NULL DEFAULT 0,
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT available_at_not_before_event_time CHECK (available_at >= event_time),
    PRIMARY KEY (feature_id, symbol, event_time, available_at)
);
SELECT create_hypertable('feature_values', 'event_time');

-- The only path the application has to feature data. DISTINCT ON returns the latest
-- revision that had been published by p_as_of -- not the latest revision that exists.
CREATE FUNCTION feature_as_of(
    p_feature_id text, p_symbol text, p_as_of timestamptz, p_lookback interval
) RETURNS TABLE (event_time timestamptz, value numeric)
LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT DISTINCT ON (fv.event_time) fv.event_time, fv.value
    FROM feature_values fv
    WHERE fv.feature_id = p_feature_id
      AND fv.symbol     = p_symbol
      AND fv.available_at <= p_as_of
      AND fv.event_time   >  p_as_of - p_lookback
    ORDER BY fv.event_time, fv.available_at DESC;
$$;

REVOKE ALL   ON TABLE feature_values FROM PUBLIC;
REVOKE ALL   ON TABLE feature_values FROM fking_app;
GRANT  SELECT, INSERT ON TABLE feature_values TO fking_ingest;
GRANT  EXECUTE ON FUNCTION feature_as_of(text, text, timestamptz, interval) TO fking_app;
```

The two roles are the mechanism. `fking_app` — the role every strategy, backtest and risk process connects as — has no `SELECT` on `feature_values` at all. A leak is not a code review failure, it is a `permission denied for table feature_values`. `fking_ingest` writes and never reads through the application path. `DISTINCT ON ... ORDER BY available_at DESC` handles revisions and late arrivals in the same clause: you get the value as it was believed at `as_of`, and the corrected value only becomes visible once `as_of` passes its `available_at`.

The Python signature carries the same requirement into the type system:

```python
# src/fking/data/features/store.py
from datetime import datetime, timedelta
from typing import Protocol

from fking.domain.identifiers import FeatureId, Symbol


class FeatureStore(Protocol):
    def load(
        self,
        *,
        feature_id: FeatureId,
        symbol: Symbol,
        as_of: datetime,          # keyword-only, no default, tz-aware, non-optional
        lookback: timedelta,
    ) -> FeatureSeries:
        """Values as they were knowable at `as_of`.

        `as_of` has no default on purpose. A default is a value someone forgets to
        override, and the value they would forget is `now()`, which is the leak.
        """
```

Feature construction, with the alignment made explicit:

```python
# src/fking/data/features/volatility.py
def realised_vol(closes: pd.Series, *, window: int) -> pd.Series:
    """Trailing realised volatility, excluding the current bar.

    `.shift(1)` before `.rolling` rather than `closed="left"`: the shift is unambiguous
    across pandas versions and index types, and it makes the exclusion visible in the
    diff. `min_periods=window` so the first window-1 rows are NaN rather than being
    computed from a partial sample that no live run would ever have had.
    """
    return closes.pct_change().shift(1).rolling(window=window, min_periods=window).std()


def cross_sectional_z(values: pd.Series, *, window: int) -> pd.Series:
    """Trailing z-score. The full-sample mean/std is the single most common leak in
    this repository's history and is why this helper exists at all."""
    trailing = values.shift(1).rolling(window=window, min_periods=window)
    return (values - trailing.mean()) / trailing.std()
```

Labels are measured from an achievable entry:

```python
def forward_return(bars: pd.DataFrame, *, horizon_bars: int) -> pd.Series:
    """Label for a decision taken on bar i.

    Entry is the OPEN of bar i+1, because the decision used the close of bar i and the
    close is not knowable until bar i is over. Exit is the close of bar i+horizon.
    Using close[i] as the entry inflates the measured edge by exactly the move the
    feature was computed from -- see the Incorrect section.
    """
    entry = bars["open"].shift(-1)
    exit_ = bars["close"].shift(-(horizon_bars + 1))
    return exit_ / entry - 1
```

The universe is resolved `as_of` too:

```sql
CREATE FUNCTION universe_as_of(p_venue text, p_as_of timestamptz)
RETURNS TABLE (symbol text)
LANGUAGE sql STABLE AS $$
    SELECT sl.symbol FROM symbol_listing sl
    WHERE sl.venue = p_venue
      AND sl.listed_at <= p_as_of
      AND (sl.delisted_at IS NULL OR sl.delisted_at > p_as_of);
$$;
```

Purge and embargo are sized from the registry, not chosen:

```python
def cpcv_splits(index: pd.DatetimeIndex, *, spec: FeatureSpec, n_groups: int, n_test: int):
    """Purge the full label horizon on BOTH sides of each test fold: a training label
    that starts before the fold can still be resolving inside it. Embargo follows the
    fold to absorb serial correlation the purge does not reach."""
    purge = spec.label_horizon + spec.availability_lag
    embargo = max(spec.label_horizon, (index[-1] - index[0]) * 0.01)
    ...
```

Methodology beyond the sizing rule lives in `../../BACKTEST_ENGINE.md`; this file owns only the constraint that the numbers come from the feature's own declaration rather than from a tuning pass.

## Enforcement

**The registry is the gate.** A feature that does not declare its timing cannot exist:

```python
# src/fking/data/features/registry.py
from datetime import timedelta
from typing import Callable, Final, Mapping

from pydantic import BaseModel, ConfigDict, Field


class FeatureSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    feature_id: FeatureId
    compute: Callable[..., pd.Series]
    inputs: frozenset[FeatureId]
    availability_lag: timedelta = Field(ge=timedelta(0))   # event_time -> available_at
    label_horizon: timedelta = Field(gt=timedelta(0))      # sizes purge and embargo
    uses_trailing_statistics_only: bool


FEATURES: Final[Mapping[FeatureId, FeatureSpec]] = {...}
```

**The `LookaheadProbe`.** The highest-value test in the repository. It perturbs the future and asserts the past did not move:

```python
# tests/lookahead/test_probe.py
import hashlib

import pytest

from fking.data.features.registry import FEATURES
from tests.lookahead.harness import canonical_digest, poison_after, replay


@pytest.mark.parametrize("spec", sorted(FEATURES.values(), key=lambda s: s.feature_id))
def test_future_perturbation_cannot_reach_the_past(spec: FeatureSpec, archive_slice) -> None:
    """Adversarial: mutate every bar after `cut` beyond recognition, replay, and require
    that every feature value and every backtest decision at or before `cut` is
    byte-identical. Anything that moves read the future."""
    cut = archive_slice.index[len(archive_slice) // 2]

    baseline = replay(spec, archive_slice, seed=20260801)
    poisoned = replay(spec, poison_after(archive_slice, cut, seed=20260801), seed=20260801)

    assert canonical_digest(baseline.features.loc[:cut]) == canonical_digest(
        poisoned.features.loc[:cut]
    ), f"{spec.feature_id}: feature values before {cut} changed when the future changed"
    assert canonical_digest(baseline.decisions_until(cut)) == canonical_digest(
        poisoned.decisions_until(cut)
    ), f"{spec.feature_id}: backtest decisions before {cut} changed when the future changed"
```

`poison_after` does not add noise — it multiplies closes by 3, inverts returns, zeroes volume and shifts every `available_at` forward, because a subtle perturbation can be absorbed by rounding and produce a false pass. `canonical_digest` serialises `Decimal` values as their exact string form and hashes with `blake2b`, so "byte-identical" is literal and a `1e-15` difference fails.

**The probe must fail closed, and that is itself tested.** A probe that can never fail proves nothing:

```python
# tests/lookahead/test_probe_detects_a_known_leak.py
from tests.lookahead.leaky import LEAKY_SPECS   # deliberately broken features, never in src/


@pytest.mark.parametrize("spec", LEAKY_SPECS)
def test_probe_catches_a_deliberate_leak(spec, archive_slice) -> None:
    """If this passes, the probe is broken and every other lookahead result is void."""
    with pytest.raises(AssertionError):
        test_future_perturbation_cannot_reach_the_past(spec, archive_slice)
```

`LEAKY_SPECS` holds four known leak shapes — full-sample z-score, `event_time` filtering, a centred rolling window, and a label using the decision bar's close — one per failure mode in the Incorrect section. Each must be caught. Adding a leak shape to that list is how a newly discovered leak class gets permanently guarded.

**CI runs the probe on every feature, automatically.** The probe is parametrized over `FEATURES.values()`, so adding a feature to the registry adds a probe case with no test-file edit; and `scripts/check_feature_registry.py` fails the build if any callable under `src/fking/data/features/` is referenced by a strategy but absent from `FEATURES`, which closes the "compute it outside the registry" route. The job is required on `main` and is not skippable by path filter — a change to the archive loader can introduce a leak without touching a feature file.

**Database-level.** `tests/integration/test_feature_store_permissions.py` connects as `fking_app` and asserts `SELECT * FROM feature_values` raises `InsufficientPrivilege`, and that `feature_as_of` with an `as_of` earlier than a revision's `available_at` returns the pre-revision value. `tests/integration/test_availability_check.py` asserts the `CHECK` constraint rejects `available_at < event_time` at insert.

**Parity.** The same strategy over the same window through `BacktestVenue` and `PaperVenue` must produce identical signals and identical risk decisions (`./testing-rules.md`). A leak present only in backtest shows up here as a divergence, which is the second independent detector and the reason backtest/live parity is an architectural invariant rather than a convenience.

## The one exception

**None.** Not for a plot, not for a chart in a research notebook, not for a "quick sanity check", not for an exploratory notebook that will "never be productionised".

The plot exception deserves naming specifically, because it is the exact path by which look-ahead enters this kind of system, and it does not enter as code — it enters as belief.

The sequence is always the same. You want to see whether a feature separates future returns, so you compute both over the full sample and scatter them. Full-sample normalisation is fine here, you reason, because nothing is being traded. The plot looks good. That plot is now the reason the feature gets built, the reason its threshold is set at 1.5 rather than 2.0, and the reason it survives the first weak backtest result — *because you have seen the picture and you know the effect is there.* The leak reached the strategy through your priors, and no test in this repository can find it, because there is no leaky line of production code to find.

The second path is more concrete: notebook code is copied. The cell that produced the plot has a `.mean()` over the full series, and it becomes `build_features()` two weeks later with the fast-and-loose parts intact, because it worked. `research/` is not a sandbox with a wall around it; it is the upstream of `src/fking/data/features/`.

So the rule holds in notebooks too. `research/` imports the same `FeatureStore` protocol and the same `feature_as_of` function, connects as `fking_app`, and passes an explicit `as_of` — which costs one extra argument and buys the property that a chart you find convincing is a chart of something that could actually have been known. If a plot genuinely needs the full sample — a distributional summary of the archive itself, a data-quality histogram, a coverage map — it is not a feature plot, it does not touch `close` alongside a forward return, and it says so in the cell above it.
