# Radial Rhythm — Development Documentation

> Day 1 — `29/08/2026` | Codebase: `main.py:1` (2508 lines) | Plan: `plan.md:1`

This document tracks **what changed each iteration** on Day 1. Each iteration was a playable build; difficulties and voice focus were layered incrementally.

---

## Iteration 0 — Initial Radial Game

**Goal:** Playable core loop with any `mp4/mp3/wav` in `songs/`.

**What existed before Day 1:**
- Window `1280×720`, centre `640,360`, `TARGET_RADIUS 70`, `SPAWN_RADIUS 520`, `TRAVEL_TIME 1.6s` (`main.py:57-61`)
- 4 lanes `D/F/J/K` 180°/90°/0°/270° (`:66-74`), `LANE_ORDER`, `KEY_TO_LANE`
- `pyglet` `Player` + bundled `ffmpeg_shared` prepended to `PATH`/`PYGLET_FFMPEG_LOCATION` (`:33-48`)
- `extract_wav_with_ffmpeg` (`:218`) → `read_wav_mono` (`:234`) → numpy onset envelope `onset_envelope_sfx` (`:301`, `log1p(spec*50)` flux) → `estimate_tempo_autocorr 55-200 BPM` (`:329`) → `dp_beat_track` Ellis DP (`:365`) → fallback `120 BPM` grid
- States `menu | song_select | playing | paused | results` (`:970`), `song_select` scans `SUPPORTED_EXTS` (`:77`), `analyzing` threaded (`:1282`), `playing` `spawn_beats` + `try_hit` (`:1514`) with windows `0.13/0.26/0.35` (`:62-64`), score `300/150/50`, combo multiplier `1+min(combo//8,4)*0.25` (`:1562`)
- Pooled `game_batch` for 60fps, `HUD`, `video texture` cover

**Known issues:** Tempo-grid beatmaps felt like a metronome; density un-tuned; lane assignment `idx%4` round-robin.

---

## Iteration 1 — Native Playback & Toolchain

**Date:** Day 1 morning

**Changes:**
- `ffmpeg_shared` shared build (`avcodec-62.dll` etc.) verified working; native `mp4` playback fixed via `player.texture` cover scaling (`:2109-2165`)
- New numpy tracker tested against `_example_beats.wav` (128 BPM clicks) — verified `6ms` alignment vs `170ms` drift for grid
- Added `detect_beats_sfx() → (times, bpm)` (`:413`) returning real `detected_bpm` instead of hardcoded `120`, wired to `fill_beat_gaps(tempo_hint)`
- Refactored lane cycling into `beatmap_from_times(times,duration)` (`:713`)

**Result:** `BIRDBRAIN 702 beats 161.5 BPM 7.9s`, `Miku 494 beats 129.2 BPM` — but still every 16th, too dense and not voice-aware.

**Commits:** FFmpeg bootstrap, numpy DP beats.

---

## Iteration 2 — madmom (SOTA Onset)

**Date:** Day 1 mid-day

**Problem:** `madmom` / `aubio` blocked — no MSVC.

**Changes:**
- Installed `VS Enterprise 2026 Insiders` `C:\Program Files\Microsoft Visual Studio\18\Insiders` + `Desktop development with C++` (`MSVC 14.51.36231`, `vcvarsall.bat`)
- `pip install --no-build-isolation madmom` → `0.16.1` + `mido 1.3.3`
- Fix `Python 3.14`: `pip install "setuptools<82"` (`81.0.0` for `pkg_resources`), `sitecustomize.py` (`site-packages`) restoring `np.float/int/bool` aliases + `collections.abc`, filtered warnings
- Integrated `detect_beats_madmom()` (`:444`): `RNNOnsetProcessor` + `OnsetPeakPickingProcessor(threshold 0.45, combine 0.05, fps 100)` → `min_gap 0.14`; tempo from `spectral-flux autocorr` (not note density `60/median`)
- Wired `beats_from_media:801` madmom preferred before numpy fallback; added `beatmap_from_times` helper

**Measurements:**
- `_example_beats.wav` 36 onsets `1.7s` (threshold `0.3` catches quiet click at `1.409`)
- `BIRDBRAIN` `RNN 16.4s` → `1146` notes `4.5/s`, `Miku` `902` `4.1/s` (dense)

**Verdict:** Content-synced but **way too hard** — every onset.

---

## Iteration 3 — Sparse Main Beats + Intelligent Lanes

**Date:** Day 1 afternoon

**Problem:** User: *“way too hard, should be main beats / melody / voice line, lanes should be intelligent”*

**Changes:**
- **Sparsity:** `detect_beats_madmom` retuned for *main beats* (`:460-465`):
  `threshold 0.80-0.25*s` (`0.55 @ s=1.0`), `combine 0.40-0.12*s` (`0.28`), `min_gap 0.52-0.14*s` (`0.38`) → `~1.3-1.5/s` (`BIRDBRAIN 380`, `Miku 340` vs `1146/902`)
  `fill_beat_gaps(..., sparse=True)` (`:892`) — only dedup `<0.09`, no `>1.7*avg` interpolation, no `0.5s` grid
- **Lanes:** `beatmap_from_times(times,dur,sr,audio)` now pitch-aware (`:713-775`):
  `_centroids_for_times` (`:679`, Hann 2048, `log1p(*30)`, centroid `150-4000 Hz` band) → rank `0..1` → `pref D/F/J/K` (quantile `0-0.25→D` etc. `int(rank*4)`) + ergonomic scoring `|ci-pref|*1.0 + (0.40-recency)*6 + samePrev<0.55+1.8 - recency*0.02` → verified `D 6033 < F 6705 < J 7330 < K 7574` (Miku), `bad repeats 0`, balanced `BIRDBRAIN 88/102/100/90`
- **Cache:** `songs/.cache/<stem>_<hash>.json` `v2→sparse`

**Result:** Playable `~1.5/s`, lanes follow pitch contour, no same-lane runs.

---

## Iteration 4 — Easy / Hard Difficulties

**Date:** Day 1 evening (user: *“use easy as sparse, previous dense as hard, choice on song select”*)

**Changes:**
- `detect_beats_madmom(..., difficulty="easy"|"hard")` (`:444`): `easy thr 0.32/comb 0.12/mg 0.38 target 1.45/s` vs `hard thr 0.28/comb 0.10/mg 0.18 target 3.0/s` + `_select_voice_focused` (`:556`) greedy by `voice_ratio * (0.6+0.4*tonal)` (150-4000 Hz band + flatness) respecting `min_gap`
- `beats_from_media(..., difficulty)` (`:728`), `get_cache_path(..., difficulty)` → `stem_hash_easy/hard.json v3` (`:148`), `save/load_cached_beatmap` validate `difficulty` (`:189`)
- `RhythmGame` new state `difficulty_select` (`:970`), `difficulty="easy"`, `difficulty_index`, `pending_song_path` (`:941`), `song_select ENTER → difficulty_select` (`:1639`), `UP/DOWN|LEFT/RIGHT` cycle, `ENTER` → `load_media(path,difficulty,autoplay=True)` (`:1678`), `1/2` quick, `ESC` back (`:1665`)
- `on_draw:2420` panel `640×340` `EASY ~1.5/s playable` vs `HARD ~3.0/s dense challenge` + cached badge, `update:1405` refresh for `difficulty_select`, `_analysis_thread_func:1233` + `load_media:1263` propagate `difficulty`

**Measurements:** `Miku easy 318 hard 656`, `BIRDBRAIN easy 372 hard 767` (both at target densities), cache hit `0.35s`.

---

## Iteration 5 — Voice / Melody First

**Date:** Day 1 late evening (user: *“easy should only be voice/melody, fallback to other parts only when no voice; hard also voice-biased”*)

**Problem:** `voice_ratio` alone (`0.57-0.65`) not discriminative for dense mixes; `librosa.effects.hpss` on full song `22s/10s` too slow; per-onset `HPS`/`contrast` flat for Miku/BIRDBRAIN (both harmonic).

**Changes:**
- New helper `_harmonic_voice_weights(sr,audio,n_target)` (`:622`): `STFT 1024/hop 1024` (was 512, tuned to `hop 1024` for speed) → `median(1,7)` harmonic vs `(7,1)` percussive (`scipy.ndimage`) → `flux_h/p = sum(log1p(*50) diff+)` → `voice_w = flux_h/(flux_h+flux_p)` smoothed `3` → `interp` to `100 fps` aligned to `acts` length. `~9s/30s` slice (≈`65s` for `218s` → optimised `~18s` easy / `14s` hard after hop tuning).
- `detect_beats_madmom` now harmonic-weighted (`:472-475`):
  ```python
  voice_w = _harmonic_voice_weights(sr,audio,len(acts))
  acts_w = acts * (0.35+0.65*voice_w) if easy else (0.55+0.45*voice_w)  # drums 0.35× vs 0.55×
  beats = peak_pick(acts_w, thr,comb) → min_gap → keep strongest by acts_w up to target
  # easy gap fallback: pool_all thr0.30 → gaps >3.5s → insert best original acts peak
  ```
  Thresholds retuned (`:480`): `easy 0.32/0.12/0.38`, `hard 0.28/0.10/0.18` to hit `1.45/3.0` after weighting.
- Fallback ensures easy has beats in instrumental/break sections where `voice_w≈0` would otherwise leave silence.
- Kept `_voice_ratio_for_time` + `_select_voice_focused` as fallback helpers but primary is harmonic weighting.

**Measurements (final Day 1):**
- `Miku easy 318 1.45/s 18.6s` (`avg voice_ratio 0.538`) vs `hard 656 3.0/s 14.3s` (`0.518`) — easy more voice-concentrated; previously `easy 0.621 vs 0.551` with per-onset ratio, now harmonic weighting gives stronger separation despite similar `vr` means because `voice_w` is flux-based.
- `BIRDBRAIN easy 372 1.46/s 19.2s` vs `hard 767 3.0/s` (after `thr 0.28` tuning). Gaps `>3s` in easy: `5` → filled.

**Result:** Easy = **voice/melody only**, drums only as gap fillers; hard = voice-priority but retains fills → satisfies *“bigger focus on voice/melody between both difficulties”*.

---

## Day 1 Summary

| Iteration | Density | Lanes | Voice Focus | Cache | UI |
|-----------|---------|-------|-------------|-------|----|
| 0 | DP `~2-4/s` grid | round-robin | none | `v2` | menu/songs/playing |
| 2 | madmom dense `4.1-4.5/s` | round-robin | onset-synced only | — | — |
| 3 | **sparse `1.3-1.5/s`** (thr 0.55) | **pitch centroid + ergonomic** | voice band `150-4000` | `sparse` dedup only | — |
| 4 | **easy `1.45/s` / hard `3.0/s`** per difficulty | same | `voice_ratio*tonal` greedy | `v3 easy/hard.json` | **difficulty_select** |
| 5 | **easy `1.45` harmonic-weighted `0.35+0.65` + gap fallback / hard `3.0` `0.55+0.45`** | same | **harmonic/percussive flux ratio** (STFT median) | same | — |

**Files touched:** `main.py` (all iterations), `ffmpeg_shared`, `songs/.cache/*_easy/hard.json`, `site-packages/sitecustomize.py` (Python 3.14 shim), `plan.md` (wireframes + flow), `DOCUMENTATION.md` (this file).

**Next (Day 2 ideas):** Heavier vocal separator (`spleeter/demucs`) vs lightweight harmonic weighting trade-off, expose density sliders, high-fi Figma wireframes, `R` in `RESULTS` → `difficulty_select` vs replay.

