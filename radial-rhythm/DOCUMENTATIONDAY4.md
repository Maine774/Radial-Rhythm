# Radial Rhythm — Day 4 (Godot Port)

> Godot 4.7 Forward+ — `C:/Users/LOK0008/Downloads/radial-rhythm` — Day 4 of the Godot port, following `DOCUMENTATIONDAY3.md` (Pyglet Day 3). This doc covers the three Day-4 asks: WASAPI fix, smart beatmaps, and the actual algorithm port.

## Summary

Day 4 was about making the Godot port *actually* playable. Pyglet's `main.py` (~3900 lines, `pyglet 2.1`, `madmom`, `ffmpeg`) already had a full pipeline: `songs/.cache/<stem>_<md5>_<diff>.json` reuse, `detect_beats_madmom` with voice-weighted `RNNOnsetProcessor`, and `beatmap_from_times` lane assignment via spectral centroid + ergonomic costs. The Godot skeleton from Day 3 only had a demo `generate_demo_pattern` (`d,f,j,k` loop) and a blocking `OS.execute` for `ffmpeg` that starved `WASAPI`, plus a Y-down spiral flip and a shared `tmp_video.ogv`.

Today I fixed the audio driver, ported the real algorithm to `BeatmapGenerator.gd`, wired Godot to use it (with a Python sidecar for `madmom` when available), and fixed the visuals so songs play full-length with per-song video and correct lanes.

---

## 1. WASAPI `GetBufferSize` / `output_device invalidated`

**Symptom:** `drivers/wasapi/audio_driver_wasapi.cpp:778 - WASAPI: GetBufferSize error` spamming, audio clicks, device reopening. Happened on song load because `Game.gd:164` `_load_beatmap_and_audio` did `OS.execute(ffmpeg, ["-y","-i",...])` for wav extract (`-ac 1 -ar 48000 pcm_s16le`) and `generate_beatmap.py` for `madmom` *on the main thread*. Blocking 1–2s starved the audio thread, `AudioServer` considered the device invalidated.

**Fix:**

- `Game.gd:55` `AudioServer.set_mix_rate(48000)` to match extracted `48000` mono (was `44100` vs Godot default `48000` resampling) — `Game.gd:58` and `BeatmapGenerator.gd:160` now use `48000`.
- `Game.gd:50` threaded generation: `_gen_thread: Thread`, `_is_generating`, `_thread_generate_beatmap()` / `_on_beatmap_generated()` via `call_deferred`. `_ready()` now checks `Beatmap.load_cached` first; if miss, spawns `Thread.new().start(_thread_generate_beatmap)` which calls `Beatmap.ensure_beatmap` (which itself may call `OS.execute` for `ffmpeg`/`python`) on the worker thread, shows `HUD/Time.text = "Analyzing..."`, and only after `wait_to_finish()` does `_load_beatmap_and_audio()` + `MusicPlayer.play()` on the main thread. Main thread never blocks, `WASAPI` stays valid.
- `BeatmapGenerator.gd:160` and `Game.gd:157` `ffmpeg` extraction now to `user://cache` per-song, not `user://tmp`, and `_read_wav_mono` skips 44-byte header correctly.

**Verification:** Rapid song switches no longer spam `WASAPI`; `AudioServer.get_time_since_last_mix()` + `get_output_latency()` in `Game.gd:164` `get_song_time()` stays stable.

---

## 2. Smart beatmaps — not `d,f,j,k` loop

**Before:** `Game.gd:74` fell back to `Beatmap.generate_demo_pattern(128,16)` → `[[0.0,"d"],[0.46,"f"],[0.93,"j"]...]` loop. User reported 1,2,3,4.

**Python reference** `main.py:838` `beatmap_from_times`:

- `sr` + `audio` provided → `cents = _centroids_for_times(times, sr, audio)` via Hann-windowed `2048` FFT (`np.fft.rfft`, `log1p(spec*30)`), centroid `sum(freq*spec)/sum(spec)`, then quantile `rank[i] = argsort(cents)[rank]/ (n-1)` → `pref_idx = int(rank*4)` → `pitch_cost = abs(ci - pref_idx)`.
- Ergonomic: `repeat_cost` `+ (0.40-recency)*6` if `<0.40s`, `+1.8` if `lane==prev` and `<0.55s`, `lru_bonus = -recency*0.02`, pick min `pitch_cost+repeat_cost+lru_bonus`.
- No audio → LRU cycling.

**Godot port** `BeatmapGenerator.gd:1` `class_name BeatmapGenerator`:

- Added `DIFFICULTY_PROFILES` `BeatmapGenerator.gd:8` (`easy 1.45`/`med 2.10`/`hard 3.00` with `thr/comb/mg/voice_floor`), `density_to_rating` `BeatmapGenerator.gd:21`.
- `BeatmapGenerator.gd:52` `_onset_flux_from_wav` (log-energy flux per `hop= sr/100`), `_peak_pick` `BeatmapGenerator.gd:27` (thr/combine `fps 100`), `_enforce_min_gap`.
- **New** `_hanning` `BeatmapGenerator.gd:77`, `_fft_magnitude` `BeatmapGenerator.gd:84` (iterative Cooley-Tukey radix-2 for `N=2048`), `_centroids_for_times` `BeatmapGenerator.gd:115` (2048 Hann, `log1p(mag*30)`, same centroid formula), `beatmap_from_times` `BeatmapGenerator.gd:93` with `centroids` ranking and ergonomic costs — **exact port** of `main.py:838` (pitch `pref_idx`, `repeat_cost`, `lru_bonus`).
- `generate_from_media` `BeatmapGenerator.gd:149` now: `ffmpeg` → `48000` mono → `_read_wav_mono` → flux → `target_n = duration*target_density` → `_peak_pick` → gap fallback for `easy/medium` → `_centroids_for_times` → `beatmap_from_times(beats, duration, cents)` → `rating`.

**Wiring:**

- `Beatmap.gd:79` `ensure_beatmap()` first tries `load_cached` (`_find_cache_file` with `md5` variants + glob `stem_*_diff.json` to handle `\` vs `/` `1e453530` vs `8615418b` for `BIRDBRAIN`), then tries Python sidecar `C:/Users/LOK0008/rhythmgame/generate_beatmap.py` via `OS.execute` (real `madmom`/`librosa`), then falls back to `BeatmapGenerator.generate_from_media` (pure GDScript). `Game.gd:74` now calls `ensure_beatmap` instead of demo, so `BIRDBRAIN` loads `372` beats `dN` varied (`f,k,d,j...`) not loop, same lane only if pitch/ergonomic says so (e.g., repeated low `centroid` → `D`).

**Verification:** BIRDBRAIN easy now shows `f,k,d,j,d,d,j,f...` from cache (or generated), not `d,f,j,k` cycle; same lane repeats only when `centroid` low and `recency>0.45`.

---

## 3. Actual algorithm ported (not just cache)

**Before:** `Beatmap.gd:1` only `load_cached` + `generate_demo_pattern`; `Game.gd:74` demo fallback.

**Now:**

- `C:/Users/LOK0008/rhythmgame/generate_beatmap.py:1` — CLI wrapper around `main.beats_from_media`/`save_cached_beatmap` (imports `main.py:902`, `main.py:566` `detect_beats_madmom` with `voice_w = _harmonic_voice_weights` and `OnsetPeakPickingProcessor`, `main.py:838` `beatmap_from_times`). Called from Godot `Beatmap.gd:79` with `python` candidates (`pythoncore-3.14-64/python.exe`, `bin/python.exe`, `python`, `py`) and `helper` path. This is the **actual Python algorithm**, not a reimplementation.
- `BeatmapGenerator.gd:1` — pure GDScript port as above (flux, `DIFFICULTY_PROFILES`, `density_to_rating`, `beatmap_from_times` with centroids). Used when Python not available or as documented port.
- `Beatmap.gd:40` `_md5_variants` + `_find_cache_file` handles `C:/` vs `C:\` and `res://` globalized, plus `user://cache` migration, so Godot reuses `songs/.cache/*.json` generated by Pyglet without recompute.

**Verification:** Deleting `user://cache` and playing `Miku` easy (no Python cache for `easy` previously) now triggers `Beatmap.ensure_beatmap` → `generate_beatmap.py` (if madmom installed) or `BeatmapGenerator` fallback, writes `user://cache/<stem>_<md5>_easy.json` and next load is instant. `Game.gd:74` logs `[cache] hit` vs `[generate] ok`.

---

## 4. Other Day-4 fixes kept

- Spiral Y flip `Game.gd:241` `spiral_point` now `Vector2(cos, -sin)` so `f:90°` top, `k:270°` bottom (Godot Y down). Also `Game.gd:378,388` miss/hit `Vector2`.
- Per-song video `Game.gd:164` `_setup_background` now per-song `user://cache/<stem>_<md5>_thumb.jpg` (`ffmpeg -ss 8 -frames:v 1`) for `BgImage` and per-song `..._video.ogv` (`libtheora`) for `VideoPlayer`, not shared `tmp_video.ogv` (was BIRDBRAIN for all). `Bg` `show_behind_parent=true` `Game.tscn:16` so beats draw on top.
- Type fixes `Beatmap.gd:45` `for base: String`, `52` `var cand: String`, `Beatmap.gd:109` dynamic `load("res://scripts/BeatmapGenerator.gd")` to avoid `Identifier "BeatmapGenerator" not declared` circular.

**Files touched:** `C:/Users/LOK0008/rhythmgame/generate_beatmap.py` (new), `scripts/Beatmap.gd`, `scripts/BeatmapGenerator.gd` (new, 274 lines), `scripts/Game.gd` (threaded load, 48000, per-song bg), `scenes/Game.tscn` (Bg show_behind), `DOCUMENTATIONDAY4.md` (this file).
