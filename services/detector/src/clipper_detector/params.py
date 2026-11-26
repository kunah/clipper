"""Every tunable of the detector in one model (DESIGN §18), with the §18 defaults.

The defaults are provisional first guesses: build item 4 tunes them by backtest against viewer
clips. A TOML file may override any of them (flat keys, the field names below; unknown keys are
an error, so a typo can't silently keep a default):

    z_open = 5.0
    cooldown_s = 90

`detector_version(params, lexicon)` = scorer code version + a hash of the effective scoring
params + the lexicon's version and file hash: stored on every candidate and in
`DetectOfflinePayload`, so a backtest or an offline re-run can tell exactly which settings
produced a row. Live-loop settings that don't change what the rows score (`NON_SCORING`) are left
out of the hash, so tuning them doesn't start a new version (and a new offline pass).

Times are whole seconds: the scorer works on per-second rows.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clipper_detector import SCORER_VERSION

# Fields of the live loop: when a second is final, the restart replay, the dedupe distance.
NON_SCORING = frozenset({"final_lag_s", "restart_replay_s", "restart_emit_s", "dedupe_s"})


class DetectorParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # ------------------------------------------------------------------ stage 1: activity
    # A(t) = sum of unique_chatters over the last `activity_window_s` seconds.
    activity_window_s: int = Field(default=10, gt=0)
    # Baseline B(t) = median of A over [t - baseline_window_s, t - baseline_exclude_s): the last
    # minute is left out so a moment doesn't raise its own bar.
    baseline_window_s: int = Field(default=1200, gt=0)
    baseline_exclude_s: int = Field(default=60, ge=0)
    # Fewer own baseline values than this (stream start, after a long outage): not scored at all,
    # prior or not (addition to §18: a median of the first seconds of a ramp, or a prior from a
    # quieter stream alone, makes ordinary chat look like a spike).
    min_baseline_values: int = Field(default=300, gt=0)
    # Scale S = max(1.4826 * MAD, sqrt(B'), scale_floor) (Poisson floor).
    scale_floor: float = Field(default=2.0, gt=0)
    # Viewers normalise, never trigger: B' = B * clamp(V_now / V_median, min, max).
    viewer_ratio_min: float = Field(default=0.67, gt=0)
    viewer_ratio_max: float = Field(default=2.0, gt=0)
    # rho = A / max(B', ratio_min_baseline).
    ratio_min_baseline: float = Field(default=3.0, gt=0)
    # Floor: A >= max(floor_activity, floor_viewer_fraction * V_now).
    floor_activity: int = Field(default=8, ge=0)
    floor_viewer_fraction: float = Field(default=0.005, ge=0)

    # ------------------------------------------------------------------ stage 1: episodes
    z_open: float = 4.0
    rho_open: float = 2.0
    z_stay: float = 2.0  # stays open while z >= z_stay
    close_after_s: int = Field(default=10, gt=0)  # closes after this long below (bursts merge)
    force_close_s: int = Field(default=180, gt=0)
    # Suspect: starts stage 2 early (the live service prefetches raw chat).
    z_suspect: float = 3.0
    rho_suspect: float = 1.5

    # ------------------------------------------------------------------ stage 1: time of stream
    warmup_stream_s: int = Field(default=600, ge=0)  # after streams.started_at
    warmup_toggle_s: int = Field(default=300, ge=0)  # after a slow/emote/subs-only toggle
    z_open_warmup: float = 6.0
    rho_open_warmup: float = 3.0
    # Until this much in-stream history exists: B = max(own, prior), prior = median A of the
    # channel's previous stream x viewer ratio.
    prior_until_s: int = Field(default=1200, ge=0)
    # A zero run at least this long while B >= missing_chat_min_baseline is missing data (not
    # fed to the baseline).
    missing_chat_s: int = Field(default=60, gt=0)
    missing_chat_min_baseline: float = Field(default=5.0, ge=0)

    # ------------------------------------------------------------------ peak and onset (§18.5)
    # Peak = argmax of the centred mean of unique_chatters over this many seconds.
    peak_smooth_s: int = Field(default=3, gt=0)
    # Onset = first second in [open - onset_lookback_s, peak] where that mean reaches
    # baseline + onset_fraction * rise.
    onset_lookback_s: int = Field(default=10, ge=0)
    onset_fraction: float = Field(default=0.25, gt=0, le=1)

    # ------------------------------------------------------------------ live loop (build item 3)
    # A second is final at ts < now - final_lag_s: chat-ingest's 5 s grace + 1 s flush + margin
    # (8 s left ~1 s; the live loop also stops at the newest row it has seen, `service`).
    final_lag_s: int = Field(default=10, ge=0)
    restart_replay_s: int = Field(default=1260, gt=0)  # replay [now - 21 min, final]
    restart_emit_s: int = Field(default=900, gt=0)  # ...emitting episodes peaking in the last 15
    dedupe_s: int = Field(default=60, ge=0)  # against any candidate of the stream within +- this

    # ------------------------------------------------------------------ lexicon
    pattern_weight: float = Field(default=0.5, ge=0, le=1)  # a `[patterns]` match counts this

    # ------------------------------------------------------------------ stage 2 (raw chat)
    # Raw messages are read over [onset - before, peak + after] (through the end of that second),
    # chat_events over [onset - events_before, peak + after].
    stage2_before_onset_s: int = Field(default=20, ge=0)
    stage2_after_peak_s: int = Field(default=5, ge=0)
    events_before_onset_s: int = Field(default=60, ge=0)
    # Shares and moment-type votes count the messages of [onset - this, peak + after]: the burst
    # itself. With the 20 s before the onset (they still serve for events) a 15 s x4 burst of one
    # copypasta had a dup share of 0.28-0.42 up to the peak (threshold 0.4), on the burst 0.60-0.73.
    shares_before_onset_s: int = Field(default=0, ge=0)
    z_clean_emit: float = 4.0
    z_clean_emit_warmup: float = 6.0
    # score = z_clean * (score_base + score_reaction_weight * min(reaction, score_reaction_cap))
    #         + boosts
    score_base: float = 0.6
    score_reaction_weight: float = 0.8
    score_reaction_cap: float = Field(default=0.5, ge=0, le=1)
    boost_clip_mentions: float = 2.0
    boost_viewer_clip: float = 1.0
    # ------------------------------------------------------------------ explicit signals (§18.4)
    # "Clip it": >= max(mention_min_users, mention_min_share x clean uniques) users mention a clip
    # within mention_window_s -> a signal at the first mention - mention_lead_s (chat time).
    mention_window_s: int = Field(default=30, gt=0)
    mention_min_users: int = Field(default=3, gt=0)
    mention_min_share: float = Field(default=0.03, ge=0, le=1)
    mention_lead_s: int = Field(default=5, ge=0)
    # Viewer clips: moment ~ [created_at - duration - latency, created_at - latency] (video time);
    # >= clip_min_creators creators whose moments lie within clip_cluster_s of each other
    # (clip_busy_creators when the channel made more than clip_busy_per_hour clips in the last
    # hour) -> a signal. A clip link in chat counts as a clip made when it was posted, this long.
    clip_latency_s: float = Field(default=5.0, ge=0)
    clip_cluster_s: float = Field(default=90.0, ge=0)
    clip_min_creators: int = Field(default=2, gt=0)
    clip_busy_creators: int = Field(default=3, gt=0)
    clip_busy_per_hour: int = Field(default=20, gt=0)
    clip_link_duration_s: float = Field(default=30.0, gt=0)
    # A signal within this distance of a candidate (chat time) is attached to it: boost, features,
    # and a suppressed candidate is re-emitted. Without one it becomes a candidate of its own once
    # the scorer is this far past it and no episode is open.
    signal_attach_s: int = Field(default=60, ge=0)
    # A Helix clip whose vod_offset is set but further than this from where a live clip would
    # sit (created_at - stream start - duration) was cut from the VOD later: not a live signal.
    clip_vod_tolerance_s: float = Field(default=120.0, ge=0)
    # Moment type: top category share >= moment_top_share, else mixed; < moment_min_votes votes:
    # unknown.
    moment_min_votes: float = Field(default=5.0, ge=0)
    moment_top_share: float = Field(default=0.4, gt=0, le=1)

    # ------------------------------------------------------------------ fake-spike rules (§18.3)
    raid_lookback_s: int = Field(default=30, ge=0)  # a raid from onset - this
    raid_keep_z_clean: float = 8.0  # ...unless z_clean >= this and reaction >= raid_keep_reaction
    raid_keep_reaction: float = Field(default=0.5, ge=0, le=1)
    subs_mass_gift_min: int = Field(default=5, gt=0)  # submysterygift mass-gift-count
    subs_events_min: int = Field(default=10, gt=0)  # sub/resub/subgift events
    bits_share: float = Field(default=0.3, gt=0, le=1)  # cheering messages / messages
    bits_feature_min: int = Field(default=1000, ge=0)  # total bits: a feature only
    commands_share: float = Field(default=0.3, gt=0, le=1)
    vote_share: float = Field(default=0.4, gt=0, le=1)
    copypasta_share: float = Field(default=0.4, gt=0, le=1)  # dup
    bots_share: float = Field(default=0.3, gt=0, le=1)
    greeting_share: float = Field(default=0.4, gt=0, le=1)
    mode_toggle_lookback_s: int = Field(default=10, ge=0)  # roomstate from onset - this
    spam_wave_share: float = Field(default=0.3, gt=0, le=1)  # deleted or moderated messages

    # ------------------------------------------------------------------ merge (§18.5)
    default_delay_s: float = Field(default=8.0, ge=0)
    learned_delay_count: int = Field(default=5, gt=0)  # median of the last N valid estimates
    # Window [min(T - before, onset - d - onset_pad), max(T + after, close - d + close_pad)],
    # at most window_max_s.
    window_before_s: float = Field(default=90.0, ge=0)
    window_after_s: float = Field(default=30.0, ge=0)
    window_onset_pad_s: float = Field(default=30.0, ge=0)
    window_close_pad_s: float = Field(default=15.0, ge=0)
    window_max_s: float = Field(default=300.0, gt=0)
    cooldown_s: float = Field(default=60.0, ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> DetectorParams:
        if not self.viewer_ratio_min <= 1 <= self.viewer_ratio_max:
            raise ValueError("viewer_ratio_min <= 1 <= viewer_ratio_max is required")
        if self.baseline_exclude_s >= self.baseline_window_s:
            raise ValueError("baseline_exclude_s must be shorter than baseline_window_s")
        if not self.z_stay <= self.z_suspect <= self.z_open <= self.z_open_warmup:
            raise ValueError("z_stay <= z_suspect <= z_open <= z_open_warmup is required")
        if not self.rho_suspect <= self.rho_open <= self.rho_open_warmup:
            raise ValueError("rho_suspect <= rho_open <= rho_open_warmup is required")
        if self.window_before_s + self.window_after_s > self.window_max_s:
            raise ValueError("window_before_s + window_after_s must fit in window_max_s")
        return self

    # ------------------------------------------------------------------ loading, version

    @classmethod
    def from_toml(cls, path: str | Path) -> DetectorParams:
        with Path(path).open("rb") as f:
            return cls.from_mapping(tomllib.load(f))

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> DetectorParams:
        return cls.model_validate(values)

    def digest(self) -> str:
        """8 hex chars of a sha256 over the effective scoring values (a file that only restates
        defaults hashes like the defaults; `NON_SCORING` fields are left out)."""
        values = self.model_dump(mode="json", exclude=set(NON_SCORING))
        canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:8]


def load_params(path: str | Path | None = None) -> DetectorParams:
    """The code defaults, or a TOML file's overrides of them."""
    return DetectorParams() if path is None else DetectorParams.from_toml(path)


class _LexiconTag(Protocol):
    @property
    def tag(self) -> str: ...  # "<version>.<6 hex of the lexicon file's sha256>"


def detector_version(params: DetectorParams, lexicon: _LexiconTag) -> str:
    """`d<scorer>-p<params hash>-l<lexicon version>.<lexicon file hash>`, e.g.
    `d1-p3f9a0c1d-l1.dea5e8` (no `:`, so it can sit in a dedupe key like
    `detect_offline:12:<version>`). The file hash shows a lexicon edited without a version bump."""
    return f"d{SCORER_VERSION}-p{params.digest()}-l{lexicon.tag}"
