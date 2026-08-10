"""The pinned reference benchmark for the backtest engine.

`make bench` runs `tools.bench` and prints wall clock, peak RSS and events/second for a
fixed workload; CI runs it with `--check` and fails the build when the wall clock exceeds
the committed budget by more than the tolerance. `PERFORMANCE_GUIDE.md` states the budget
and the machine it was measured on, and `docs/perf/` holds the profiling evidence the
optimisation work started from.

It lives under `tools/` rather than in `src/fking` because it is not part of the system:
nothing the platform runs imports it, and it must be free to construct a `SpecRegistration`
by hand -- which no production caller may do.
"""
