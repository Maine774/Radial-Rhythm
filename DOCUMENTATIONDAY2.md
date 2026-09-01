# Radial Rhythm — Development Documentation (Day 2)

> Day 2 — `30/08/2026` → `31/08/2026` | Codebase: `main.py:1` (~3000 lines) | Continues `DOCUMENTATION.md` (Day 1)

On Day 2 I built on top of the Day‑1 voice‑first difficulty system: I added a third
difficulty and a **1–20 rating scale**, then did a full **osu!‑style UI redesign** (menu +
song select carousel + live video preview + song‑colour accent tabs), added a **persisted
fullscreen setting**, and finally replaced straight‑line beat approach with a **spiral
approach** with a new‑section telegraph.

---

## Iteration 6 — Three Difficulties + Monostar 1–20 Rating

**Date:** Day 2 morning

**Problem:** Two difficulties (easy/hard) didn't give enough spread; I wanted a graded
scale and a visible "how hard is this song" number.

**Changes:**
- `DIFFICULTY_PROFILES` (`main.py:87`) now holds three runnable profiles as tuples
  `(threshold, combine, min_gap, target_density, voice_floor, label, desc, key)`:
  | name | threshold | combine | min_gap | target/s | voice_floor | label | key |
  |------|-----------|---------|---------|----------|-------------|-------|-----|
  | easy | 0.32 | 0.12 | 0.38 | 1.45 | 0.35 | EASY | `_1` |
  | medium | 0.30 | 0.11 | 0.26 | 2.10 | 0.45 | MEDIUM | `_2` |
  | hard | 0.28 | 0.10 | 0.18 | 3.00 | 0.55 | HARD | `_3` |
- I added `DIFFICULTY_ORDER = ["easy","medium","hard"]` (`:93`); all modes stay voice‑first —
  the harmonic/percussive weight now uses a rising base floor so harder songs keep a little more
  percussion: `acts_w = acts * (voice_floor + (1-voice_floor)*voice_w)`.
- **Rating scale:** `RATING_MIN_NPS/RATING_MAX_NPS = 1.45, 3.00` (`:96`) map linearly to a
  **1–20 monostar** rating via `density_to_rating(nps)` (`:106`); `rating_marker(rating)` (`:113`)
  renders it as a compact `dN` (d1–d3, then d6/d9/d12/d15/d18/d20 tiers).
- `clamp_difficulty(diff)` (`:100`) normalises any string to easy/medium/hard (default `easy`),
  so every load path shares one guard.
- **Cache v4** (`CACHE_VERSION = 4`): `songs/.cache/{stem}_{md5[:8]}_{easy|medium|hard}.json`
  now stores `rating`; `get_cache_path`/`load_cached_beatmap`/`save_cached_beatmap` and the
  analysis thread all carry the rating (4th return value from
  `detect_beats_madmom`/`beats_from_media`).
- `difficulty_options = ["EASY","MEDIUM","HARD"]`; `song_select` maps `difficulty_index` through
  `DIFFICULTY_ORDER.index()`; `1/2/3` quick‑select sets the difficulty directly.

**Measurements (verified on Day 2):**
- BIRDBRAIN 255.7s: easy → `1.45–1.46/s` → **rating 1**; medium → `2.10–2.11/s` → **rating 9**;
  hard → `3.0/s` → **rating 20**.
- Timing ~10–16 s per difficulty; cache hit makes reloads instant.

---

## Iteration 7 — osu!‑style UI: Menu + Song Select Carousel + Live Preview

**Date:** Day 2 afternoon (make it look way sexier, like OSU… remove the Songs tab, just a
Play tab that opens a vertical carousel that pulls out the selected card, with a preview of the
video and song)

**Menu changes:**
- I switched `menu_options = ["PLAY","OPEN FILE","SETTINGS","QUIT"]` (`:1029`).
- I rebuilt the menu draw (`:2689`) around a big **PLAY pill** (glowing when selected) with small
  secondary buttons below, animated lane‑colour rings behind the logo.
- I updated the mouse hit‑zones to match the new geometry.

**Song select — full redesign** (`:2775`):
- **Full‑bleed live video preview** as the backdrop (`_draw_song_preview` `:1431`, dimmed by an
  overlay for readability).
- **Right vertical carousel**: song cards stacked 112 px apart; the **selected card is pulled out**
  (scaled ~1.16×, nudged left, bright, accent edge) while adjacent cards shrink/fade with distance.
  It's animated via `sc_scroll` (smooth snap to `song_index`) + `sc_selected_pull`.
- **Left column**: selected song title + meta (size/ext/cached beatmaps) and the three
  **difficulty buttons** showing their cached `dN` rating + beat count; `1/2/3` or `←/→` pick
  a difficulty live, `ENTER` plays.
- **Fixed carousel direction**: index 0 now renders at the top so **UP moves visually up**.
- Preview lifecycle: `_set_preview` (`:1369`, seeks ~30% + volume 0.35), `_tick_preview`
  (`:1399`, seeks once then loops + **pulls the video texture every frame for smooth ≥30 fps**),
  `_stop_preview` (`:1228`) on transitions.

**Measurements:** Verified live render + key/mouse nav, distinct per‑song previews, no errors.

---

## Iteration 8 — Song‑Colour Selection Tabs

**Date:** Day 2 late afternoon (use the colours of the song for its selection tab)

**Problem:** Sampling a live video texture via `get_image_data()` returned black (video textures
aren't CPU‑readback friendly), and framebuffer `glReadPixels` was also unreliable headless.

**Changes:**
- I dropped both GPU paths and used **ffmpeg to extract one frame's average colour**:
  `_compute_song_accent(path)` (`:1310`) runs
  `ffmpeg -ss 8 -i <path> -frames:v 1 -vf scale=48:27 -f rawvideo -` and averages the RGB bytes.
- `_ensure_accent(path)` (`:1338`) checks a cache (`self.song_accents`, persisted to
  `songs/.cache/__song_colors__...json` via `_load_song_colors`/`_save_song_colors`), otherwise
  kicks off a **background thread** so the UI never blocks; when it lands and the song is still
  selected it tints the tab.
- `_enhance_accent` brightens dark content + boosts saturation so the accent is always a visible,
  lively "song colour".
- The accent now colours the **selected difficulty button** and the **selected carousel card**
  (accent side bar, top/bottom edges, tinted background).

**Measurements:** BIRDBRAIN → pink `(201,152,174)`, Miku → light blue `(152,195,222)`; audio‑only
files / files without a frame fall back to the default accent.

---

## Iteration 9 — Fullscreen Setting (Settings screen + persisted)

**Date:** Day 2 evening (add a fullscreen setting)

**Changes:**
- **Config**: I persist `config.json` next to `main.py` via `_load_config`/`_save_config`;
  `_apply_fullscreen(on)` (`:1260`) sets `is_fullscreen`, persists, and (on a real display) calls
  `set_fullscreen`.
- **Startup**: `__init__` loads config and enters fullscreen if requested.
- **Settings screen**: I added a `settings` state (`:1976` input, draw block) — a `SETTINGS` menu
  option (`:1029`) opens a card UI with a **Fullscreen ON/OFF** row
  (`settings_rows = [("fullscreen",…)]` `:1032`); `UP/DOWN` navigate, `ENTER`/`←/→` toggle,
  `B`/`ESC` back to menu.
- `F11` toggles fullscreen at any time and now also persists via `_apply_fullscreen`.

**Verification:** menu → SETTINGS → toggle ON/OFF → config.json written correctly
(`{"fullscreen": false}`); startup reads the value back. (Fullscreen switch itself is
environment‑dependent — headless test hosts can't enter fullscreen, but the persistence logic and
window init path are correct on a real display.)

---

## Iteration 10 — Spiral Approach + New‑Section Telegraph

**Date:** Day 2 night (make the beats come in a spiral instead of straight lines)

**Problem:** Beats came in along straight lines, 90° from each other. I wanted each note to
start on the side of the lane before it, then curve around in a spiral and reach the centre on
its own side.

**Changes:**
- I defined a clockwise chain `yellow → red → blue → green → yellow`; each lane's notes now
  **start at the predecessor lane's side** (`start_ang = (hit_ang + 90) % 360`) and **wind 90°
  inward** to land on their own lane's side. I used a simple Archimedean spiral:
  `radius = SPAWN_RADIUS − raw*(SPAWN−TARGET)`, `angle = start_ang ∓ raw*90` as `raw` goes 0→1.
- **New‑section telegraph:** when the gap to the previous note is bigger than
  `SECTION_GAP_THRESHOLD = 2.0s` (`main.py:66`), that note **coils counterclockwise** instead
  (`start_ang = (hit_ang − 90) % 360`) to announce the new phrase — and I draw a **faint ghost
  spiral arc** behind it (`_draw_spiral_ghost`) so the direction is clearly visible.
- I added shared helpers `spiral_point`/`spiral_guide_points` (`:67`) so the note motion
  (`_spiral_xy` `:2362`) and the lane rendering use identical math.
- **Physical lanes now match the new path**: each lane's guide is a curved 18‑segment spiral
  polyline instead of a straight radial line, and each **outer spawn ring moved to the
  predecessor's spawn side** (e.g. red's ring is now at the bottom, where its spiral begins).
- I extended the pooled beat slots with ghost segments and added `_hide_beat_slot` so every
  render path (miss fade, hit burst, active) cleans up the ghost correctly.

**Measuring/verification:** Red's spiral starts at the bottom (270°) and lands on the left
(180°); the first note of a run is treated as a new section and coils counterclockwise;
consecutive notes wind clockwise along the drawn guide. Smoke‑tested on the live display.

---

## Day 2 Summary

| Iteration | What changed | Verified |
|-----------|--------------|----------|
| 6 | 3 difficulties (easy/medium/hard) + monostar **1–20 rating** + cache v4 | hard = rating 20, medium = 9, easy = 1 |
| 7 | osu! menu + song‑select **carousel with pull‑out**, live video preview | render + nav OK |
| 8 | **song‑colour accent tabs** via ffmpeg frame average (threaded, cached) | pink / blue per song |
| 9 | **fullscreen setting** (settings screen + `config.json`) | persisted + read back |
| 10 | **spiral approach** + new‑section CCW telegraph + ghost arc | red 270°→180°, lanes match |

**Files touched:** `main.py`, `README.md` (rewritten for the new UI/settings/spiral), `config.json`
(new), `DOCUMENTATIONDAY2.md` (this file), `songs/.cache/*` (v4 beatmaps + song colours).

**Next ideas:** heavy vocal separator (spleeter/demucs) vs harmonic weighting, per‑song density
sliders, replay from results screen, song‑select search bar, difficulty_select screen restyle to
match the new osu! look, maybe a subtle ghost arc on clockwise notes too.
