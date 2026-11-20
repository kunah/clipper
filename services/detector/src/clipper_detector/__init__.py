"""Detector (DESIGN §18): chat spikes -> candidate moments.

Pure parts (no database, no clock; time comes from the rows):
- `params`: every tunable (`DetectorParams`) and `detector_version`.
- `lexicon`: emote/word categories from the versioned `data/lexicon.toml`.
- `scorer`: stage 1, `ChannelScorer.step(second)` over per-second chat stats -> episodes.
- `stage2`: raw chat of an episode -> clean uniques, shares, score, moment type, fake-spike rules.
- `merge`: episode + stage 2 -> candidate draft (delay, peak/window in video time, cooldown,
  budget).
- `pipeline`: the three wired together over in-memory rows (tests, backtest).
"""

# Bump when the scoring code changes its output for the same rows, params and lexicon: it is
# part of `detector_version`, so backtests and offline re-runs tell the versions apart.
SCORER_VERSION = 1
