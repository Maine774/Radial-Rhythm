# Radial Rhythm — Development Documentation (Day 3)

> Day 3 — `01/09/2026` → `02/09/2026` | Codebase: `main.py:1` (~3590 lines) | Continues `DOCUMENTATIONDAY2.md` (Day 2)

Day 3 was a performance + polish + persistence day. I had three strands running: a **60fps
shape‑pool refactor** (`ShapeBank`) that fixed the classic "rects painted over text" z‑order bug,
a **deep settings expansion** (latency, music/FX volume, video brightness, lane alpha, editable
keybinds — all persisted to `config.json`), and finally a **gameplay‑feel overhaul**: keypress
SFX, a new **miss/meh/good/perfect** hit system with **perfect‑combo (FC)** and **A–D grading**,
a top‑left **score meter**, and menu **background images**.

---

## Iteration 11 — ShapeBank pool + z‑order fix

**Date:** Day 3 morning (running at 60fps; shapes were already pooled per screen, but I wanted one
shared pool)

**Problem:** Every screen built its own stack of `pyglet.shapes.RoundedRectangle` objects or
scattered them across the render path. Worse, rectangles drawn *after* text painted over the
labels — a recurring bug I kept fixing screen‑by‑screen.

**Changes:**
- I added one shared **`ShapeBank`** (`main.py:1033`) holding `n=256` pool‑allocated
  `RoundedRectangle`s in a single `pyglet.graphics.Batch`. The pattern everywhere is now:
  `S = self._shapes; S.reset(); S.rect(...); ...; S.draw()` then *all* `_draw_label` calls.
- `S.rect(x, y, w, h, color, radius=None, opacity=255)` (`:1050`) grabs the next free slot,
  repositions it and marks it visible; `reset()` (`:1045`) hides every slot each frame; `draw()`
  (`:1068`) flushes the one batch.
- I migrated **every screen** to the pattern: menu, settings, song_select, difficulty_select,
  analyzing, results + paused overlays, and the video overlay.
- **Critical z‑order rule (documented in code):** all shapes flush via a single `S.draw()`
  **before** any text labels, so rectangles never cover text again.
- `S.rect` returns `None` if the bank is exhausted (256 slots) so the game degrades gracefully
  instead of throwing if a screen ever needs more.

**Verification:** all screen‑states render cleanly at 60fps; smoke‑tested every state with the new
pool.

---

## Iteration 12 — Label cache fix (disappearing text)

**Date:** Day 3 morning (right after the ShapeBank refactor)

**Problem:** `_draw_label` (`main.py:2598`) cached labels by style only, so two different texts
with the same style shared one `pyglet.text.Label` and the second call silently replaced the
first — text "disappeared".

**Changes:**
- The cache key became `(font_name, int(size*10), weight, italic, anchor_x, anchor_y, text)`
  — **text added to the key** — so each unique text+style gets its own persistent label and the
  cache still prevents per‑frame allocation.

**Verification:** two same‑style, different‑text labels now get distinct objects (reproduced and
fixed in a direct test).

---

## Iteration 13 — New settings: latency, volumes, brightness, lane alpha

**Date:** Day 3 afternoon (add latency/volume/brightness/lane‑alpha settings)

**Changes (the settings engine):**
- `self.settings` (`main.py:1087`) gained `input_latency 0.0`, `music_volume 0.9`,
  `fx_volume 0.7`, `video_brightness 0.30`, `lane_alpha 0.85`. `settings_rows = [...]`
  (`:1112`) is now **7 rows of 3‑tuples** `(key, label, type)` where
  `type ∈ toggle | range | submenu`.
- **Range config** `_RANGE_CFG` (`:1392`) drives every slider row: `input_latency` (−0.20..0.20,
  step 0.01 s), `music_volume`/`fx_volume`, `video_brightness` (0..1, step 0.05), `lane_alpha`
  (0.2..1.0, step 0.05).
- Helpers after `_apply_fullscreen` (`:1382`): `_toggle_setting` (`:1400`),
  `_adjust_range_setting` (`:1408`), `_apply_settings_to_playback` (`:1427`).

**Playback wiring:**
- `input_latency` feeds `beat_offset` on `start_media` (positive = notes hit later); stored
  separately from the live ±0.05 tuning keys (`,`/`.`).
- `music_volume` applies to the media player on start, and the song‑select preview runs at
  `music_volume * 0.5`. Changing it live calls `_apply_settings_to_playback`.
- **Video brightness** (`_draw_video_background`, `:2905`): `brt = video_brightness` drives a
  sprite opacity of `int(60 + brt*170)` and a dim overlay of `int(200 − brt*160)` — higher =
  brighter video.
- **Lane alpha** (`_draw_beats`): `la` in 0.2..1.0 from `lane_alpha`, stored on
  `self._lane_alpha_mult` and applied to active notes (`255*la`), miss fade, hit burst and the
  new‑section ghost arcs (`90*la`).

**Settings screen draw** was rebuilt on `ShapeBank` with a dynamic card:
`row_pitch=52`, `list_top=scy+66`, title at `scy+190`; each row renders per‑type: fullscreen
`ON/OFF`, latency as `{ms:+d} ms`, ranges as `{pct}%`, submenu as `OPEN >` (this also fixed a
`ValueError` from stale 3‑tuple unpacking).

**Verification:** settings page renders all 7 rows; global defs smoke‑tested; config round‑trips.

---

## Iteration 14 — Editable keybinds (+ new screen)

**Date:** Day 3 afternoon (let me remap keys)

**Changes:**
- Added a **`keybinds` state** with its own `on_key_press` branch (`:2320`): `UP/DOWN` to pick a
  lane, `ENTER` to begin rebinding (`binding_target` set), the next keypress is captured via
  `_assign_keybind` (`:1443`), `B`/`ESC` back to settings.
- `_open_keybinds` (`:1438`) resets `binding_target` and enters the screen.
- `_rebuild_key_to_lane` (`:1466`) rebuilds the **global `KEY_TO_LANE`** (`:100`) so rebinds take
  effect immediately in gameplay; `_save_keybinds` (`:1474`) persists the map to `config.json`;
  `_apply_keybinds(data)` (`:1483`) restores and rebuilds on load.
- Keybinds draw block (`:3285`) shows the 4 lanes with current keys; the lane being rebound
  flashes a "press any key…" hint (`binding_target == lane` highlight, `:3315`).

**Verification:** pick lane → ENTER → press new key → gameplay uses the new bind → survives
restart via config read‑back.

---

## Iteration 15 — Menu background images

**Date:** Day 3 evening (add a background on every menu; I dropped `Backgrounds/bg01.jpeg` into the
project and plan more later)

**Changes:**
- Added `_draw_background` (`main.py:2971`): lazily loads `Backgrounds/bg01.jpeg` once into
  `self._bg_sprite` (`:2987`, opacity **130**), then scales it to **cover** the window using the
  *texture's* base dimensions (`max(w/h ratios)`, `:2989-`), centred.
- Fallback: if `bg01.jpeg` is missing it uses the first `.jpeg/.jpg/.png` found in `Backgrounds/`;
  any failure returns `False` and the caller's solid vignette stays.
- `on_draw` calls it for **`menu`, `settings`, `keybinds`, `difficulty_select`, `analyzing`**
  only — **not** song_select (which shows the live video preview) and **not** gameplay.
- The solid fallback rects for those states dropped to `opacity=0` (menu/settings/keybinds) or
  became a dim layer `opacity=175` (difficulty_select/analyzing) so the image shows through.
- Side effect fixed along the way: an early edit had accidentally restructured **song_select's**
  `else` branch — I restored its `(10,10,18)` fill.

**Verification:** bg renders scaled‑to‑cover behind menu/settings/keybinds/difficulty/analyzing;
song_select and gameplay unaffected.

---

## Iteration 16 — Keypress SFX + GC fix

**Date:** Day 3 night (add a hit sound; I dropped `SFX/clickfx.mp3` into the project)

**Changes:**
- `_play_clickfx` (`main.py:2121`) loads `SFX/clickfx.mp3` once (`streaming=False`) and on each
  call creates a `pyglet.media.Player`, queues the source, sets
  `volume = fx_volume`, and plays. `try_hit` (`:2145`) calls it at the top **for every lane key
  press during a song, hit or miss**.
- **Bug found during smoke test:** the `Player` was a local — CPython GC'd it before the click
  finished, so *nothing was heard even at FX volume 100%*. Fix: keep every live player in
  `self._clickfx_players` and prune finished ones on the next play
  (`self._clickfx_players = [p for p in ... if p.playing]`, `:2139-2141`).

**Verification:** load + play no exceptions headless; players stay referenced until finished so the
click is audible on every press.

---

## Iteration 17 — Hit categories, FC, grading, score meter

**Date:** Day 3 night (make the judgement granular: miss/meh/good/perfect, grades, perfect combo)

**Changes:**
- **Hit categories** replace the old `OK`: `self.hits = {perfect, good, meh, miss}` (`:1166`,
  reset `:1727`). `try_hit` awards **perfect 300 → good 200 → meh 100 → miss 0**
  (windowed like before, meh = the widest landing zone). Old `'ok'` keys removed everywhere
  (HUD + results).
- **Perfect combo (FC):** new `self.fc` / `self.max_fc` (`:1164`, reset `:1725`). Any
  **perfect** does `fc += 1` (tracked to `max_fc`); any **meh/good/miss**, a whiff, or a
  beat‑timeout in `update` calls `_break_fc` (`:2206`, sets `fc = 0`). Off‑hits reset FC the
  instant they're registered.
- **Grading:** `_max_possible_score` (`:2210`) simulates an all‑perfect run through the same
  combo‑multiplier formula (`1 + min(combo//8,4)*0.25`, 300/beat) to get the **max attainable
  score for that song**; `grade()` (`:2225`) maps the ratio to **A ≥90%, B 70–89%, C 50–69%,
  D <50%**.
- **HUD score meter (top‑left):** under the score line, a persistent 220×8 bar
  (`_hud_meter_bg/_fg`, `:1276`) filling `score/max_possible`, plus a colour‑coded **live grade**
  and **perfect‑combo** readout.
- **Results screen** (`:3042`): big colour‑coded **Grade**, `Score + % of max`, max combo +
  **perfect combo**, and a **PERFECT / GOOD / MEH / MISS** breakdown with accuracy
  `(perfect*1.0 + good*0.85 + meh*0.6)/total`.

**Verification (headless smoke test):**
- Grades: 95% → **A**, 75% → **B**, 55% → **C**, 30% → **D**. Max‑score sim on a 100‑beat map =
  **54000** (base 30000 + multiplier gains).
- All 9 draw states (`menu, settings, keybinds, song_select, difficulty_select, analyzing,
  results, paused, playing`) render with no errors after every edit.

---

## Day 3 Summary

| Iteration | What changed | Verified |
|-----------|--------------|----------|
| 11 | **`ShapeBank`** pooled rounded‑rects (256 slots) + shapes‑before‑text z‑order rule | 60fps, no text overpaint |
| 12 | label cache **key includes text** | distinct labels, no disappearing text |
| 13 | settings engine + 5 new settings (latency/volumes/brightness/lane alpha) wired to playback | rows render + config round‑trip |
| 14 | **editable keybinds** (`keybinds` state + `KEY_TO_LANE` rebuild + persistence) | rebind immediate + survives restart |
| 15 | **menu background images** (`_draw_background`, bg01.jpeg, cover scale) | all non‑songselect menus show it |
| 16 | **keypress SFX** + player GC fix (`_clickfx_players`) | audible on every lane press, hit or miss |
| 17 | **miss/meh/good/perfect**, **FC**, **A–D grading**, top‑left score meter, results upgrade | thresholds + all states smoke‑OK |

**Files touched:** `main.py` (~3100 → ~3590 lines), `config.json` (new settings keys + `keybinds`
dict), `DOCUMENTATIONDAY3.md` (this file), `Backgrounds/bg01.jpeg` (added by me), `SFX/clickfx.mp3`
(added by me).

**Next ideas:**
- Per‑song high scores / grade history (read best grade + FC off the cached beatmap).
- Visual judgement bursts (PERFECT/GOOD/MEH float‑up animations) to match the new categories.
- Volume sliders with click‑test tones inside the settings screen itself.
- An FC / miss shake + fx on the centre target.
- Second background (bg02…) + a shuffle/cycle option in settings.