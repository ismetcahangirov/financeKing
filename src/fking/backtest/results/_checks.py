"""Six of the seven audit checks: everything but look-ahead, which is `_lookahead_guard`.

Each function takes exactly the evidence it needs and returns one `AuditFinding`. None of
them re-derives a number another module already owns -- `check_cost_model` reads
`round_trip_cost_bp` and `calibration_source` rather than re-running
`fking.backtest.costs.assess_run`, and `check_sample_size` reads
`fking.backtest.cpcv.MINIMUM_PATH_TRADES` rather than restating the per-fold floor.
Recomputing here is exactly how a check and the thing it audits drift apart, silently,
until the two disagree about a run neither of them is actually looking at any more.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

from fking.backtest.costs import names_testnet
from fking.backtest.cpcv import MINIMUM_PATH_TRADES
from fking.backtest.results._finding import AuditCheck, AuditFinding, AuditStatus

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")

# `BACKTEST_ENGINE.md` section 6.7: the minimum sample the Sharpe's own standard error does
# not swamp. Below it the result is not evidence in either direction.
MIN_CREDIBLE_TRADE_COUNT: Final = 200


def check_cost_model(*, calibration_source: str, round_trip_cost_bp: Decimal) -> AuditFinding:
    """FAIL a testnet-sourced calibration, or one that charged nothing or a credit.

    `round_trip_cost_bp <= 0` mirrors `fking.backtest.costs.CostVerdict.VOID`: a cost
    model that charged zero or a net credit did not run. A `BacktestResult` built from a
    void `RunCostReport` is refused before construction (`BacktestResult`'s own
    `edge_to_cost_ratio` guard), so this branch exists for the `BacktestResult`-level
    string and Decimal fields specifically, which can be re-checked in isolation from
    storage, independent of whether the `RunCostReport` that produced them is still
    around.
    """
    if names_testnet(calibration_source):
        return AuditFinding(
            check=AuditCheck.COST_MODEL,
            status=AuditStatus.FAIL,
            evidence=(
                f"calibration_source={calibration_source!r} names testnet; production "
                f"spread is 0.16bp against testnet's measured 7.5bp -- a cost model "
                f"calibrated there is fiction, per CLAUDE.md section 2"
            ),
        )
    if round_trip_cost_bp <= _ZERO:
        return AuditFinding(
            check=AuditCheck.COST_MODEL,
            status=AuditStatus.FAIL,
            evidence=(
                f"round_trip_cost_bp={round_trip_cost_bp} is not positive; a cost model "
                f"that charges nothing or a net credit did not run"
            ),
        )
    return AuditFinding(
        check=AuditCheck.COST_MODEL,
        status=AuditStatus.PASS,
        evidence=(
            f"calibration_source={calibration_source!r} is production-provenanced; "
            f"round_trip_cost_bp={round_trip_cost_bp}"
        ),
    )


def check_timestamp_alignment(
    *,
    declared_epoch_unit_by_market: Mapping[str, str],
    resolved_epoch_unit_by_market: Mapping[str, str],
) -> AuditFinding:
    """FAIL any `(market, date)` whose ingested epoch unit disagrees with its declaration.

    `docs/rules/no-lookahead.md`'s companion timestamp rule: spot data ingested after
    2025-01-01 is microseconds, futures stayed milliseconds, and a loader that applies one
    divisor globally misaligns whichever market it guessed wrong by three orders of
    magnitude. Two markets legitimately using *different* units is not itself a defect --
    the defect is a market whose *resolved* unit disagrees with what was declared for it.
    """
    if not declared_epoch_unit_by_market:
        return AuditFinding(
            check=AuditCheck.TIMESTAMP_ALIGNMENT,
            status=AuditStatus.INCONCLUSIVE,
            evidence="no market carried a declared epoch unit; there is nothing to verify",
        )
    mismatched = {
        market: (declared, resolved_epoch_unit_by_market.get(market))
        for market, declared in declared_epoch_unit_by_market.items()
        if resolved_epoch_unit_by_market.get(market) != declared
    }
    if mismatched:
        return AuditFinding(
            check=AuditCheck.TIMESTAMP_ALIGNMENT,
            status=AuditStatus.FAIL,
            evidence=(
                f"epoch unit mismatched declared vs resolved for {sorted(mismatched)}: {mismatched}"
            ),
        )
    return AuditFinding(
        check=AuditCheck.TIMESTAMP_ALIGNMENT,
        status=AuditStatus.PASS,
        evidence=(
            f"resolved epoch unit matched the declaration for every market: "
            f"{sorted(declared_epoch_unit_by_market)}"
        ),
    )


def check_fill_optimism(
    *, submitted_order_count: int, filled_order_count: int, rejected_order_count: int
) -> AuditFinding:
    """FAIL a 100% fill rate with zero venue rejections.

    The mission's own sanity threshold, applied literally: "100% of limit orders filled
    and zero venue rejections are certainly defective." A queue model that never lets an
    order lose the race, or a book that never runs out of depth, is a market that does not
    exist.
    """
    if submitted_order_count <= 0:
        return AuditFinding(
            check=AuditCheck.FILL_OPTIMISM,
            status=AuditStatus.INCONCLUSIVE,
            evidence=f"submitted_order_count={submitted_order_count}; no order was placed to check",
        )
    if filled_order_count > submitted_order_count:
        return AuditFinding(
            check=AuditCheck.FILL_OPTIMISM,
            status=AuditStatus.FAIL,
            evidence=(
                f"filled_order_count={filled_order_count} exceeds "
                f"submitted_order_count={submitted_order_count}; the fill ledger and the "
                f"order ledger disagree, which is a bookkeeping defect worse than optimism"
            ),
        )
    fill_rate = Decimal(filled_order_count) / Decimal(submitted_order_count)
    if fill_rate == _ONE and rejected_order_count == 0:
        return AuditFinding(
            check=AuditCheck.FILL_OPTIMISM,
            status=AuditStatus.FAIL,
            evidence=(
                f"{filled_order_count}/{submitted_order_count} limit orders filled with "
                f"0 venue rejections; a backtest whose passive orders all fill is trading "
                f"against a market that does not exist"
            ),
        )
    return AuditFinding(
        check=AuditCheck.FILL_OPTIMISM,
        status=AuditStatus.PASS,
        evidence=(
            f"{filled_order_count}/{submitted_order_count} limit orders filled "
            f"(rate={fill_rate}), rejected_order_count={rejected_order_count}"
        ),
    )


def check_survivorship(
    *, universe_symbols: Sequence[str], universe_resolved_as_of: bool
) -> AuditFinding:
    """FAIL a universe that was not resolved point-in-time.

    `docs/rules/no-lookahead.md`: "the symbol universe is point-in-time... never from
    today's symbol list." `universe_resolved_as_of` names whether the caller queried
    `universe_as_of(venue, as_of)` (true) or `load_markets()` / today's listing (false);
    the check does not re-derive listing dates itself, because that resolution belongs to
    the data layer's `universe_as_of` function, not to an audit reading its output.
    """
    if not universe_symbols:
        return AuditFinding(
            check=AuditCheck.SURVIVORSHIP,
            status=AuditStatus.INCONCLUSIVE,
            evidence="universe_symbols is empty; there is no universe to check",
        )
    if not universe_resolved_as_of:
        return AuditFinding(
            check=AuditCheck.SURVIVORSHIP,
            status=AuditStatus.FAIL,
            evidence=(
                f"universe of {len(universe_symbols)} symbols "
                f"({sorted(universe_symbols)}) was not resolved as-of the run window; a "
                f"universe drawn from today's listing is selection bias wearing a "
                f"universe filter"
            ),
        )
    return AuditFinding(
        check=AuditCheck.SURVIVORSHIP,
        status=AuditStatus.PASS,
        evidence=(
            f"universe of {len(universe_symbols)} symbols resolved point-in-time as of "
            f"the run window"
        ),
    )


def check_parity(*, backtest_decision_digest: str, paper_decision_digest: str) -> AuditFinding:
    """FAIL when `BacktestVenue` and `PaperVenue` disagree on the same strategy and window.

    Both digests are produced the same way the look-ahead probe produces its own --
    `fking.backtest._config.canonical_digest` over the run's decision trace -- so
    "identical" means byte-identical, not merely close.
    """
    if not backtest_decision_digest or not paper_decision_digest:
        return AuditFinding(
            check=AuditCheck.PARITY,
            status=AuditStatus.INCONCLUSIVE,
            evidence=(
                f"backtest_decision_digest={backtest_decision_digest!r} "
                f"paper_decision_digest={paper_decision_digest!r}; both are required to "
                f"compare parity"
            ),
        )
    if backtest_decision_digest != paper_decision_digest:
        return AuditFinding(
            check=AuditCheck.PARITY,
            status=AuditStatus.FAIL,
            evidence=(
                f"backtest_decision_digest={backtest_decision_digest} disagrees with "
                f"paper_decision_digest={paper_decision_digest} for the same strategy "
                f"and window; behaviour diverged between venues"
            ),
        )
    return AuditFinding(
        check=AuditCheck.PARITY,
        status=AuditStatus.PASS,
        evidence=f"backtest and paper decision digests agree: {backtest_decision_digest}",
    )


def check_sample_size(
    *, trade_count: int, per_fold_trade_counts: Sequence[int] = ()
) -> AuditFinding:
    """FAIL below 200 trades overall, or below `MINIMUM_PATH_TRADES` in any CPCV fold.

    `BACKTEST_ENGINE.md` section 6.7's two floors, both applied literally: `trade_count=199`
    fails exactly as `trade_count=2` does, and one thin fold voids the sample claim
    regardless of how the other folds look.
    """
    if trade_count < MIN_CREDIBLE_TRADE_COUNT:
        return AuditFinding(
            check=AuditCheck.SAMPLE_SIZE,
            status=AuditStatus.FAIL,
            evidence=(
                f"trade_count={trade_count} is below the {MIN_CREDIBLE_TRADE_COUNT}-trade "
                f"floor; the Sharpe's own standard error swamps the estimate below it"
            ),
        )
    thin_folds = tuple(count for count in per_fold_trade_counts if count < MINIMUM_PATH_TRADES)
    if thin_folds:
        return AuditFinding(
            check=AuditCheck.SAMPLE_SIZE,
            status=AuditStatus.FAIL,
            evidence=(
                f"{len(thin_folds)}/{len(per_fold_trade_counts)} CPCV test folds carry "
                f"fewer than {MINIMUM_PATH_TRADES} trades: {thin_folds}"
            ),
        )
    return AuditFinding(
        check=AuditCheck.SAMPLE_SIZE,
        status=AuditStatus.PASS,
        evidence=(
            f"trade_count={trade_count} >= {MIN_CREDIBLE_TRADE_COUNT}; "
            f"{len(per_fold_trade_counts)} CPCV folds all carry >= {MINIMUM_PATH_TRADES} trades"
        ),
    )
