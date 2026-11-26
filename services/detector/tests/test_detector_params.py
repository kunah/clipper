"""DetectorParams: §18 defaults, TOML overrides, hash and detector_version."""

import re

import pytest
from pydantic import ValidationError

from clipper_detector import SCORER_VERSION
from clipper_detector.lexicon import Lexicon
from clipper_detector.params import NON_SCORING, DetectorParams, detector_version, load_params


def test_defaults_are_the_design_values():
    p = DetectorParams()
    assert (p.activity_window_s, p.baseline_window_s, p.baseline_exclude_s) == (10, 1200, 60)
    assert (p.viewer_ratio_min, p.viewer_ratio_max, p.scale_floor) == (0.67, 2.0, 2.0)
    assert (p.floor_activity, p.floor_viewer_fraction, p.ratio_min_baseline) == (8, 0.005, 3.0)
    assert (p.z_open, p.rho_open, p.z_stay, p.close_after_s, p.force_close_s) == (4, 2, 2, 10, 180)
    assert (p.z_suspect, p.rho_suspect) == (3.0, 1.5)
    assert (p.warmup_stream_s, p.warmup_toggle_s, p.z_open_warmup, p.rho_open_warmup) == (
        600,
        300,
        6.0,
        3.0,
    )
    assert (p.prior_until_s, p.missing_chat_s, p.missing_chat_min_baseline) == (1200, 60, 5.0)
    assert (p.stage2_before_onset_s, p.stage2_after_peak_s, p.events_before_onset_s) == (20, 5, 60)
    assert (p.z_clean_emit, p.z_clean_emit_warmup, p.pattern_weight) == (4.0, 6.0, 0.5)
    assert (p.score_base, p.score_reaction_weight, p.score_reaction_cap) == (0.6, 0.8, 0.5)
    assert (p.moment_min_votes, p.moment_top_share) == (5, 0.4)
    assert (p.raid_lookback_s, p.raid_keep_z_clean, p.raid_keep_reaction) == (30, 8.0, 0.5)
    assert (p.subs_mass_gift_min, p.subs_events_min, p.bits_share) == (5, 10, 0.3)
    assert (p.commands_share, p.copypasta_share, p.vote_share) == (0.3, 0.4, 0.4)
    assert (p.bots_share, p.greeting_share, p.spam_wave_share) == (0.3, 0.4, 0.3)
    assert p.mode_toggle_lookback_s == 10
    assert (p.default_delay_s, p.learned_delay_count) == (8.0, 5)
    assert (p.window_before_s, p.window_after_s, p.window_max_s) == (90, 30, 300)
    assert (p.window_onset_pad_s, p.window_close_pad_s) == (30, 15)
    assert (p.cooldown_s, p.budget_per_hour, p.budget_window_s) == (60, 8, 3600)
    assert (p.final_lag_s, p.restart_replay_s, p.restart_emit_s, p.dedupe_s) == (10, 1260, 900, 60)


def test_toml_overrides_and_rejects_typos(tmp_path):
    good = tmp_path / "p.toml"
    good.write_text("z_open = 5.0\nbudget_per_hour = 6\n", encoding="utf-8")
    p = load_params(good)
    assert (p.z_open, p.budget_per_hour, p.rho_open) == (5.0, 6, 2.0)
    assert load_params() == DetectorParams()

    typo = tmp_path / "typo.toml"
    typo.write_text("z_opn = 5.0\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_params(typo)

    bad = tmp_path / "bad.toml"
    bad.write_text("z_stay = 5.0\n", encoding="utf-8")  # above z_open
    with pytest.raises(ValidationError, match="z_stay"):
        load_params(bad)


def test_params_are_frozen():
    with pytest.raises(ValidationError):
        DetectorParams().z_open = 1.0


def test_digest_and_version(tmp_path):
    p = DetectorParams()
    assert p.digest() == DetectorParams().digest()
    assert re.fullmatch(r"[0-9a-f]{8}", p.digest())
    assert DetectorParams(z_open=4.5).digest() != p.digest()
    same = tmp_path / "same.toml"
    same.write_text("z_open = 4.0\n", encoding="utf-8")  # restates a default
    assert load_params(same).digest() == p.digest()

    lexicon = Lexicon.load()
    version = detector_version(p, lexicon)
    assert version == f"d{SCORER_VERSION}-p{p.digest()}-l{lexicon.version}.{lexicon.digest}"
    assert re.fullmatch(r"d\d+-p[0-9a-f]{8}-l\d+\.[0-9a-f]{6}", version)
    assert ":" not in version  # sits inside a dedupe key


def test_live_loop_fields_dont_change_the_version():
    p = DetectorParams()
    assert {"final_lag_s", "restart_replay_s", "restart_emit_s", "dedupe_s"} == NON_SCORING
    assert set(DetectorParams.model_fields) >= NON_SCORING
    live = DetectorParams(final_lag_s=3, restart_replay_s=2000, restart_emit_s=600, dedupe_s=30)
    assert live.digest() == p.digest()
    for field, value in [("z_open", 4.5), ("pattern_weight", 0.4), ("cooldown_s", 30.0)]:
        assert DetectorParams(**{field: value}).digest() != p.digest(), field
