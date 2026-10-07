"""Tests for the anti-collision donor pool LRU (Option E).

Three layers:

1. Unit stability — peek_donor_pick is deterministic and distinct seeds
   produce distinct picks.

2. Contract drift — a full ghost_file run over a fixture JPEG produces
   metadata whose Make/Model matches peek_donor_pick's prediction for
   the same seed. If anyone adds another derive_child_seed step in
   ghostcli's metadata chain, this test fails loudly.

3. Rejection sampling — given a specific exclude_set, the scheduler's
   rejection sampling loop picks a seed whose predicted donor is NOT in
   the set. This is the load-bearing anti-collision invariant.
"""
from __future__ import annotations

import hashlib
import json
import random
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("ghostcli", reason="Optional external Ghost engine is not bundled with this release")


# ---------------------------------------------------------------------------
# Test 1: peek_donor_pick unit stability
# ---------------------------------------------------------------------------

def test_peek_donor_pick_stable_across_calls():
    """Same seed, same pick. Two calls in a row must agree."""
    from ghostcli.profiles import peek_donor_pick
    a = peek_donor_pick(42)
    b = peek_donor_pick(42)
    assert a == b, f"peek_donor_pick(42) not stable: {a} vs {b}"
    # Not the sentinel — pool is populated in test environment
    assert a[0] != "", f"Got empty-pool sentinel: {a}"
    assert a[1] >= 0, f"Got negative index: {a}"


def test_peek_donor_pick_distinct_seeds_give_distinct_picks():
    """Five random seeds should produce at least 4 distinct picks
    (149-row pool → birthday paradox probability of 5 seeds all
    different is ~97%).
    """
    from ghostcli.profiles import peek_donor_pick
    seeds = [1, 100, 1000, 10_000, 100_000]
    picks = [peek_donor_pick(s) for s in seeds]
    unique = set(picks)
    assert len(unique) >= 4, (
        f"Expected ≥4 distinct picks from 5 seeds, got {len(unique)}: {picks}"
    )


def test_peek_donor_pick_returns_valid_pool_entry():
    """Predicted (model_key, row_index) must be a real position in the
    loaded donor pool.
    """
    from ghostcli.profiles import peek_donor_pick, _load_donor_pool
    pool = _load_donor_pool()
    assert pool, "Donor pool is empty — cannot verify peek output"
    for seed in [0, 1, 42, 2**20, 2**30]:
        model_key, row_idx = peek_donor_pick(seed)
        assert model_key in pool, f"seed={seed} → unknown model_key {model_key!r}"
        assert 0 <= row_idx < len(pool[model_key]), (
            f"seed={seed} → row_idx {row_idx} out of range for model {model_key!r} "
            f"(pool size {len(pool[model_key])})"
        )


# ---------------------------------------------------------------------------
# Test 2: contract drift — peek vs actual ghost output
# ---------------------------------------------------------------------------

@pytest.fixture
def fixture_jpeg(tmp_path: Path) -> Path:
    """Synthesize a tiny JPEG on disk for ghost_file to consume."""
    img = Image.new("RGB", (128, 128), color=(200, 50, 50))
    path = tmp_path / "fixture.jpg"
    img.save(path, format="JPEG", quality=90)
    return path


def test_peek_donor_pick_matches_ghost_file_output(fixture_jpeg: Path, tmp_path: Path):
    """Run full ghost_file over a fixture image with a known seed,
    parse the resulting JPEG's EXIF Make+Model, and assert they match
    the donor row that peek_donor_pick predicted.

    This is the CONTRACT DRIFT detector. If someone adds another
    derive_child_seed step in ghostcli's metadata chain,
    `peek_donor_pick`'s seed path diverges from `pick_donor_row`'s
    seed path and the predicted donor no longer matches reality.
    The assertion fails loudly.

    Skipped on environments without exiftool (Windows dev box by
    default). Production VPS has exiftool installed so CI will
    exercise this test there.
    """
    import shutil as _shutil
    if _shutil.which("exiftool") is None:
        pytest.skip("exiftool not in PATH — contract drift test requires full ghost pipeline")

    from ghostcli.pipeline import ghost_file
    from ghostcli.profiles import peek_donor_pick, _load_donor_pool

    pool = _load_donor_pool()
    assert pool, "Donor pool is empty — cannot run contract drift test"

    # Deterministic seed — shared between peek and the actual ghost run
    shared_meta_seed = 0xDEADBEEF & 0x7FFFFFFF

    # Prediction
    predicted_model, predicted_idx = peek_donor_pick(shared_meta_seed)
    assert predicted_model in pool
    predicted_row = pool[predicted_model][predicted_idx]

    # Actual ghost run
    file_seed = 0xCAFEBABE & 0x7FFFFFFF
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result_path = ghost_file(
        fixture_jpeg,
        output_dir,
        file_seed,
        ssim_threshold=0.85,  # forgiving — fixture is a flat red square
        shared_meta_seed=shared_meta_seed,
    )
    assert result_path.exists(), f"ghost_file did not produce output: {result_path}"

    # Parse EXIF from the ghosted output
    actual_make = None
    actual_model = None
    try:
        import piexif
        exif = piexif.load(str(result_path))
        ifd0 = exif.get("0th", {})
        raw_make = ifd0.get(piexif.ImageIFD.Make, b"")
        raw_model = ifd0.get(piexif.ImageIFD.Model, b"")
        actual_make = raw_make.decode("utf-8", errors="replace").rstrip("\x00")
        actual_model = raw_model.decode("utf-8", errors="replace").rstrip("\x00")
    except ImportError:
        pytest.skip("piexif not available for EXIF parse")

    expected_make = predicted_row.get("EXIF:Make", "")
    expected_model = predicted_row.get("EXIF:Model", "")

    assert actual_make == expected_make, (
        f"CONTRACT DRIFT: peek predicted Make={expected_make!r} "
        f"but ghost output has Make={actual_make!r}. "
        f"The derive_child_seed chain in ghostcli has diverged from "
        f"peek_donor_pick's mirror. Fix peek_donor_pick to match the "
        f"new chain (see ghostcli/profiles.py::generate_metadata_fields)."
    )
    assert actual_model == expected_model, (
        f"CONTRACT DRIFT: peek predicted Model={expected_model!r} "
        f"but ghost output has Model={actual_model!r}. Same root cause "
        f"as Make mismatch — fix peek_donor_pick."
    )


# ---------------------------------------------------------------------------
# Test 3: rejection sampling logic
# ---------------------------------------------------------------------------

def _simulate_rejection_sampling(
    video_id: int,
    account: str,
    exclude_set: set[tuple[str, int]],
    attempts_count: int = 8,
) -> tuple[tuple[str, int], int]:
    """Reproduce the scheduler.py rejection loop in pure form.

    Returns (picked_donor, carousel_meta_seed).
    """
    from ghostcli.profiles import peek_donor_pick
    shuffle_mat = f"{video_id}:{account}".encode("utf-8")
    shuffle_seed = int.from_bytes(
        hashlib.blake2b(shuffle_mat, digest_size=8).digest(), "big"
    ) & 0x7FFFFFFF
    attempts = list(range(attempts_count))
    random.Random(shuffle_seed).shuffle(attempts)

    carousel_meta_seed = None
    picked_donor: tuple[str, int] = ("", -1)
    for attempt_idx in attempts:
        material = f"{video_id}:{account}:{attempt_idx}".encode("utf-8")
        trial_seed = int.from_bytes(
            hashlib.blake2b(material, digest_size=8).digest(), "big"
        ) & 0x7FFFFFFF
        trial_donor = peek_donor_pick(trial_seed)
        if carousel_meta_seed is None:
            carousel_meta_seed = trial_seed
            picked_donor = trial_donor
        if trial_donor not in exclude_set:
            carousel_meta_seed = trial_seed
            picked_donor = trial_donor
            break
    assert carousel_meta_seed is not None
    return picked_donor, carousel_meta_seed


def test_rejection_sampling_avoids_singleton_exclude_set():
    """If the donor that would be picked by attempt_0 is in the exclude
    set, rejection sampling must walk forward to a different donor.
    """
    # Probe to find what attempt_0 would pick for this (video, account),
    # then exclude exactly that donor and verify the sampler walks away.
    video_id = 12345
    account = "pa1peach"
    natural_pick, _seed = _simulate_rejection_sampling(video_id, account, set())

    excluded = {natural_pick}
    actual_pick, _ = _simulate_rejection_sampling(video_id, account, excluded)
    assert actual_pick != natural_pick, (
        f"Rejection sampling did NOT avoid the excluded donor {natural_pick}. "
        f"Got {actual_pick} instead."
    )
    assert actual_pick not in excluded


def test_rejection_sampling_deterministic_on_same_inputs():
    """Same (video_id, account, exclude_set) must produce the same pick
    across calls — retry must be idempotent.
    """
    video_id = 99
    account = "pa3peach"
    exclude_set: set[tuple[str, int]] = set()
    a, a_seed = _simulate_rejection_sampling(video_id, account, exclude_set)
    b, b_seed = _simulate_rejection_sampling(video_id, account, exclude_set)
    assert a == b
    assert a_seed == b_seed


def test_rejection_sampling_diverges_across_accounts():
    """Two sibling accounts on the same device with the same empty
    exclude_set must pick different donors because they have different
    video.ids (autoincrement PK per Video row) AND different account
    names (Codex Q5 defensive mixing).

    This validates that the blake2b shuffle seed produces distinct
    attempt orderings for (video_a, acc_a) vs (video_b, acc_b).
    """
    # Simulate sibling carousel posts: same photo set, different
    # video_ids (sequential), different accounts.
    pick_1, _ = _simulate_rejection_sampling(1001, "pa1peach", set())
    pick_2, _ = _simulate_rejection_sampling(1002, "pa2peach", set())
    pick_3, _ = _simulate_rejection_sampling(1003, "pa3peach", set())

    picks = {pick_1, pick_2, pick_3}
    # With a 149-row pool and 3 random draws, probability of all 3
    # being distinct is ~98%. This test is statistical but the failure
    # rate is acceptable for a sanity check.
    assert len(picks) >= 2, (
        f"Expected sibling picks to diverge, got all-same: {pick_1}"
    )


def test_rejection_sampling_handles_exhausted_exclude_set():
    """If all 8 attempts collide with the exclude set, the loop must
    still return the first shuffled trial (graceful degradation).
    """
    video_id = 777
    account = "pa_exhausted"

    # Run once with empty exclude to learn all 8 donors the shuffle
    # would walk through.
    donors_seen = set()
    from ghostcli.profiles import peek_donor_pick
    shuffle_mat = f"{video_id}:{account}".encode("utf-8")
    shuffle_seed = int.from_bytes(
        hashlib.blake2b(shuffle_mat, digest_size=8).digest(), "big"
    ) & 0x7FFFFFFF
    attempts = list(range(8))
    random.Random(shuffle_seed).shuffle(attempts)
    for attempt_idx in attempts:
        material = f"{video_id}:{account}:{attempt_idx}".encode("utf-8")
        trial_seed = int.from_bytes(
            hashlib.blake2b(material, digest_size=8).digest(), "big"
        ) & 0x7FFFFFFF
        donors_seen.add(peek_donor_pick(trial_seed))

    # Exclude all of them — rejection is impossible.
    picked, _ = _simulate_rejection_sampling(video_id, account, donors_seen)
    # Should still return a valid donor (the first shuffled trial's donor)
    assert picked[0] != "" and picked[1] >= 0, (
        f"Exhausted exclude should still return a valid donor, got {picked}"
    )
    # It MUST be one of the 8 we saw (graceful fallback to first shuffled)
    assert picked in donors_seen
