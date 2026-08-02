"""Strategy lifecycle, scoring and mutation. Knows about populations and survival.

Generating strategies is easy and mostly produces garbage; the hard part is rejecting
them correctly. The overfitting defences live here -- the global monotone trial
counter, the deflated Sharpe, and the promotion gate that requires forward
out-of-sample evidence rather than the validation performance that was selected on.
"""

__all__: tuple[str, ...] = ()
