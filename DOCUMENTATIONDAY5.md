# Radial Rhythm — Day 5 (Godot Port)

> Godot 4.7 Forward+ — `C:/Users/LOK0008/Downloads/radial-rhythm` — Day 5 of the Godot port,
> `03/09/2026` → `05/09/2026`, following `DOCUMENTATIONDAY4.md` (Day 4). Day 4 made the port
> playable (WASAPI, smart beatmaps, threaded analysis, per-song video/thumb). Day 5 turned that
> core into the full app: the osu!-style shell, per-song previews, a startup pre-compile pass,
> honest scoring + per-song history, both settings screens, the admin cheat gate, the unified
> note-count difficulty rating, and — finally — the fix that made every song's backdrop video
> actually play in gameplay.

## Summary

- **09/03 (committed, `65eef62…13a6541`):** osu!-style Main menu (PLAY pill + animated lane
  rings) and Game HUD health bar; SongSelect vertical carousel with expand-on-focus difficulty
  tabs; per-song video/audio previews (threaded, 10 s ogv clips, accent colours, fullscreen
  cover); plus a batch of parse/inference fixes the editor's strict typing forced.
- **03 → 05/09 (currently uncommitted):** Startup pre-compile screen (`Startup.tscn` is the new
  main scene), the complete Game loop (analyze → countdown → play → results), judgement/FC/
  grades/health/screen-shake, in-game pause overlay with live settings, per-song history in the
  cache JSON, a dedicated Settings screen, secret admin gate (auto-play / no-death cheats), the
  unified `dN` note-count difficulty rating, and the GameManager-hosted detached backdrop-video
  encoder.

`project.godot:14` now boots `res://scenes/Startup.tscn`; autoloads are `GameManager`, `Settings`,
`Ui` (`project.godot:21-23`), and the input map carries `lane_d/f/j/k`, `pause` (SPACE) and
`ui_back` (B / ESC, `project.godot:34-67`).

---

## 1. osu!-style shell — Main menu, SongSelect, HUD health (`65eef62`)

**Main menu** (`scripts/Main.gd`, `scenes/Main.tscn`): a big glowing **PLAY pill** with small
secondary buttons below (`_style_menu:158`, pill treatment at `:166`), animated lane-colour
pulsing rings behind the title (`_draw:193`), a mouse-parallax background (`_process:183`),
keyboard nav **UP/DOWN/W/S**, `ENTER/SPACE` activate, `F11` toggles fullscreen live and persists
(`_input:84`), and ESC quits only when PLAY isn't focused (`_input:90`). The subtitle counts
songs and claims "osu! • Forward+ 60fps" (`Refresh:40`). **OPEN FILE** opens a `FileDialog`
(ACCESS_FILESYSTEM) that routes the picked path straight into song select (`_open_file_dialog:119`),
with a guard that grabs focus instead of spawning a second dialog (`:121-124` — was flickering in
the earlier builds). `_activate:102` drives the `GameManager.State` transitions.

**Game HUD** (`scenes/Game.tscn`): top bar with `Score` (score, combo, max combo), `Hits`
(P/G/MEH/M), `Grade`, a 6px osu!-style `HealthBar`, right-aligned `Time`, bottom `Progress`, hint
and a huge centred `Countdown` label. `BgImage`, `VideoPlayer` and dim `Bg` ColorRect all render
with `show_behind_parent=true` so beats draw on top.

**Shared UI helpers** (`scripts/Ui.gd`, new autoload): static button-click SFX routing
(`set_click_player:14`/`wire:19`/`play:24` — each scene registers its own in-tree player so
`AudioServer` actually processes it), a tween-based `fade_in:44` (modulate + `offset_transform`
slide, used everywhere a card shows), `make_vignette:74`/`attach_vignette:119` (radial gradient
darkening shared across screens) and a flat rounded `style_button:95` used by every menu screen.

---

## 2. SongSelect carousel + difficulty tabs (`65eef62`, `79ef0cf`, `8082ee4`, `599adac`)

`scripts/SongSelect.gd` builds a vertical **carousel of song cards** (`_build_carousel:93`) at
`CARD_GAP 100`, each card `360×84` with title, ext/size meta and a hidden `DiffBox`. On focus the
selected card **expands to 200 px** (`EXPAND_H 8`) and reveals the four difficulty buttons while
the others collapse; expansion animates with `move_toward(delta*ANIM_SPEED)` in `_process:253`
and the whole column glides via exponential scroll easing (`SCROLL_SPEED 12`, `_update_scroll_target:271`,
`_apply_card_layout:283`). Layout uses one cached `StyleBoxFlat` per card (`_card_sbs`) — no
per-frame `.duplicate()`.

- Focused card: accent-coloured border (song accent), brightened bg, `z_index 10`, slight left
  nudge, near-cards fade by distance (`_apply_card_layout:283-321`).
- **Difficulty tabs** (`_update_diff_buttons:323`): each button renders
  `EASY • d4 • 372 • A` — rating from `Beatmap.notes_to_rating(beat_count)` (Section 12), beat
  count, and best cached grade. The selected difficulty gets the accent-tinted style; the diff
  box fades in with the expand progress.
- Input: **UP/DOWN/W/S** focus, **LEFT/RIGHT/A/D** cycle difficulty, **1/2/3/4** jump straight to
  a difficulty, **ENTER/SPACE** play, **ESC/B** back (`_input:193`), mouse wheel anywhere in the
  carousel browses (`_on_carousel_input:179`), clicking a focused card plays it
  (`_on_card_input:165`).
- Left column shows the focused song title + `EXT • 31.4 MB | E d12 479` meta
  (`_update_selection:222-241`).

**Accent colours:** `_ensure_accent:365` + `_compute_accent_thread:379` average one downscaled
ffmpeg frame (`48:27` raw) and apply a brightness boost + saturation lift
(`_compute_accent:383`), cached in `user://cache/song_colors.json`
(`_on_accent_computed:415`). Used for the focused card border and the selected diff button —
mirrors the Pyglet song-colour tabs.

---

## 3. Per-song video/audio preview (`a642bdc`, `b5af1c4`, `599adac`, `d988723`, `07c5b4f`, `e1f3e4c`, `24f76d4`, `13a6541`)

The SongSelect backdrop previews each focused song's video+song audio, threaded so the UI never
blocks:

- `_ensure_video_cache:542` encodes a **10 s Theora preview clip** at FULL native resolution
  (`-ss 0 -t 10 -c:v libtheora -q:v 7 -g:v 30 -an`) into `user://cache/<stem>_<md5>_preview_k30.ogv`,
  reusing the Python build's cache layout; `_load_preview_audio:572` extracts a per-song mono
  wav for the audio half.
- `_prep_video_thread`/`_prep_audio_thread:496/506`: runs on `Thread`s; `_try_start_preview:520`
  waits for **both** to finish before starting them together (`_apply_preview_*` guard against a
  song change mid-load). If no video exists it falls back to the still thumbnail
  (`_show_thumb_now:486`).
- The preview covers the screen via a `SubViewport`+`TextureRect` fullscreen fill (fixes the
  stretched/greyed preview, `d988723`) and **a fresh `VideoStreamPlayer` node is created per
  song** (`_free_preview_player:446`) so switching songs can't show the previous song's video
  (`24f76d4`).
- `_process` re-links and loops the preview once loaded and **retries a throttled load** so a
  fast-scrolled song still gets its preview (`_preview_pending`, `_process:255`, `13a6541`).
- Preview volume fixed at `0.35`, muted while not focused.

**Asset/host cleanup (`b5af1c4`):** songs, backgrounds, SFX and `ffmpeg_shared` moved to local
`res://` paths (the project no longer depends on the Pyglet folder, which remains only as a
fallback scan source), and every ffmpeg call goes through `_find_ffmpeg`
(`SongSelect.gd:431`, `Game.gd:236`, `GameManager.gd:165`, `Startup.gd:159`) which prefers
`res://ffmpeg_shared/<ver>/bin/ffmpeg.exe` before absolute paths/PATH.

---

## 4. Strict-typing fix commits (`53c91ad`, `2857033`, `1053d82`, `f36e99d`, `b242542`)

The editor treats `inferred_declaration` as an error, which made the new screens a litter of
parse failures on 09/03:

- `:=` shorthand swapped for explicit `=` / typed `for` vars and Lambda params
  (`2857033`, `53c91ad`).
- Null-tree safeguards on scene changes (`1053d82`), `FileDialog` exclusive-window spam fixed
  (`f36e99d`), and a removed `AudioServer.set_mix_rate` call that isn't a
  `GDScriptNativeClass` API (`b242542`). These are the standing rule for every later edit too:
  **always type the variable** (`for base: String in …`).

---

## 5. Startup pre-compile screen (`scenes/Startup.tscn`, `scripts/Startup.gd`, uncommitted)

`project.godot:14` boots the new **Startup** screen so the app is warm before the menu: it scans
`songs/` (`Startup.gd:62`) and queues one job per song for whatever is still missing
(`_build_queue:44`):

1. the 10 s video preview ogv + still `thumb.jpg` (`_compile_preview:174`), if the `*_preview_k30.ogv`
   cache is absent (`_preview_cached:80`),
2. the song accent colour (`_compile_accent:196`, frame-average + enhance, stored via
   `_store_accent:232`),
3. cached beatmaps for any difficulty ticked in Settings → "Compile beatmaps for new songs"
   (`Settings.get_compile_difficulties`, `_run_job:116` → `Beatmap.ensure_beatmap`).

Jobs run **one at a time on a `Thread` worker** (`_advance:95`, deferred `_on_job_done:126`), the
card shows song name + running status + `n/N` progress bar, and a **SKIP** button lets the player
continue (`_skip:148`, scene change deferred until the current job returns). A warm launch finds
everything cached and goes straight to the menu with no flash (`_ready:30-36`). `_exit_tree`
joins the worker so nothing dangles across the scene change.

---

## 6. Game loop completion — analyze → countdown → play → results

**Analyze screen** (`Game.gd _show_analyze_screen:457`, `scenes/Game.tscn` `AnalyzeLayer`):
spinner (`SPINNER_FRAMES`), pseudo-progress ramp capping at 95 %, and a `MAX_GEN_WAIT` (600 s)
watchdog that abandons and starts with whatever is cached or a demo pattern (`_process:534-549`).
On a cache miss `_ready:102` spawns the static worker `_thread_generate_beatmap:393` which runs
`Beatmap.ensure_beatmap` + `_ensure_backdrop_media` (thumb + per-song wav only — the full ogv is
NOT part of the gate, `:112-114`), then pushes its result through
`GameManager.queue_gen_result`. `GameManager._process` (autoload, survives scene changes)
reaps orphaned threads and delivers the result to the live Game node by
`call_deferred("_on_beatmap_generated")` with a **tag guarding stale results** from a previous
scene (`_on_beatmap_generated:401`). `_exit_tree:1210` orphans the worker when the player
ESC-leaves mid-analysis instead of joining (joining used to block on the old video conversion).

**Load order** (`_load_beatmap_and_audio:152`): thread result → cache → `ensure_beatmap`
(GDScript port first, Python `generate_beatmap.py` sidecar second) → demo fallback; the
watchdog path (`skip_generate`) never re-runs generation on the main thread.

**Countdown:** 3-2-1-GO (`_start_playback:427`, `_finish_countdown:447`); first beats are
pre-spawned during the countdown (`_process:531`) so the player sees the approach. `get_song_time:472`
compensates with `AudioServer.get_time_since_last_mix()/get_output_latency()` plus the saved
`input_latency`.

**Spiral + section flips:** `spiral_point:485` (Y-flipped for Godot), and `spawn_beats:496` flips
`_spiral_cw` at every new section (gap > `SECTION_GAP 2.0 s`) — all beats in a section share the
flipped direction, and the faint 18-segment lane guides are drawn in the current direction
(`_draw:651`).

---

## 7. Judgement, FC, grading, health, feedback

- **Hit windows** (`Game.gd:11-13`): PERFECT ≤ 0.13 s → 300, GOOD ≤ 0.26 s → 200, MEH ≤ 0.35 s
  → 100; everything else is a MISS. `_try_hit:769` picks the nearest un-hit beat in the lane,
  awards score `pts * (1 + min(combo/8, 4)*0.25)` (`:823`), heals, bursts, flashes the lane.
  Off-beat presses break combo and damage health (`:782-789`).
- **FC:** any PERFECT bumps `fc` (tracked to `max_fc`); anything else calls `GameManager.break_fc:31`.
  Every 10 FC fires a "FC x n" burst + ring + shake (`_fc_milestone:855`).
- **Health:** osu!-style slow drain (`-1.8/s`, `_process:601`), healed by hits, `-8…-10` per
  miss/whiff; at 0 HP the song fails and jumps to results (`:602`).
- **Grades** (`GameManager.grade:42`, `max_possible_score:35`): A ≥ 90 %, B 70–89 %, C 50–69 %,
  D < 50 % vs. the simulated all-PERFECT score. HUD shows score/combo/grade/hits/time/progress
  and a colour-shifting health bar (`_update_hud:622`).
- **Judgement bursts** (`scripts/JudgementBursts.gd`, a day-4-style CanvasLayer): float-up +
  fade + scale-pop labels spawned from `_try_hit`, `_autoplay_tick`, `_trigger_miss:850`,
  plus expanding rings (`_spawn_ring:863`) and camera shake (`_trigger_shake:860`), ticked in
  `_tick_fx:611`.
- **Keypress SFX** via `_play_click:764` on every lane press (respects `fx_volume`).

---

## 8. Pause overlay = in-game settings (`Game.gd _build_pause_overlay:875`)

Bonus feature for gameplay: **ESC/B pauses into a settings card** (code-built CanvasLayer, layer
60) with the same rows as the Settings screen — fullscreen toggle, input latency / music / FX /
video brightness / lane alpha sliders, per-lane **keybinds** (press a new key to rebind, captured
in `_input:1096`, saved + InputMap-rebuilt live), "Compile beatmaps for new songs" checkboxes
(easy/medium on, hard/extreme off), the **Admin** password gate, a hint, Back and **Quit to song
select**. Pausing freezes the music (`stream_paused`) and records the song position so
`get_song_time:473` reports exactly the paused moment. ESC/B semantics are always: close bind →
close admin submenu → close overlay (`_unhandled_input:728-739`).

---

## 9. Results + per-song history

`_on_song_finished:1190` stops playback, records the result and fades in the results card with
grade, score + % of max, max combo + FC, hit breakdown and accuracy
(`(P*1.0 + G*0.85 + MEH*0.6)/total`, `:1203-1206`). **ENTER/SPACE/ESC** back to song select,
**R** replays (`_unhandled_input:721-727`).

**History** lives in the beatmap cache JSON (`Beatmap.gd:121-155`, `HISTORY_MAX 10` entries):
`GameManager.record_result:52` stores `{date, grade, pct, score, max_fc, max_combo, acc, diff}`
and tracks whether this run is a **new best**; `best_grade_from_history:157` /
`best_max_fc` / `best_score` feed the SongSelect diff buttons and the left meta.

---

## 10. Settings screen (`scripts/SettingsScreen.gd`, `scripts/Settings.gd`)

`Settings` (autoload) is the single source of truth: `settings` dict with the Pyglet-compatible
defaults (`Settings.gd:15`), persisted to `user://config.json` (`save_config:129`), read from
`user://` then legacy `res://config.json` (`load_config:107`, including Pyglet tuple-format
keybind migration `_apply_keybinds:136`). `apply_keybinds_to_input:50` rewrites the `lane_*`
InputMap actions from the stored keycode so gameplay rebinds immediately.

`SettingsScreen` renders the same seven rows as the pause overlay (fullscreen toggle, latency,
music/FX volume, video brightness, lane alpha), per-lane keybind buttons, **Clear song cache**
(`Settings.clear_song_cache:67` — recursive wipe so beatmaps/previews rebuild), the compile
checkboxes, and the **Admin** gate (Section 11). `RANGE_CFG` (`Settings.gd:38`) drives every
slider; live changes apply immediately (`_after_range_change:261`).

---

## 11. Admin gate + auto-play / no-death cheats

A password-protected **ADMIN** submenu in both the Settings screen (`SettingsScreen.gd:106-153,
328-346`) and the pause overlay reaches two cheat toggles:

- **Auto-play**: `_autoplay_tick:828` scores every beat as a PERFECT the instant its time
  arrives (combo/FC/health/bursts/milestones all fire) and stray lane presses are ignored
  (`_unhandled_input:759`).
- **No-death**: the 0-HP fail check is skipped (`_process:602`).

The gate is one secret `LineEdit` (Enter submits), verified against `check_admin_password:159`
(default `"admin"`; the stored `admin_password` is persisted, `admin_unlocked` is session-only).
While **both** cheats are on (`cheats_active:171`), `record_result:56` skips saving and the
results card appends " (not ranked)" (`_on_song_finished:1207`).

---

## 12. Unified difficulty rating — note count → `dN`

The rating shown everywhere is now **note-count based**: `Beatmap.notes_to_rating:184` maps total
beat count to the 1–20 scale with `~40 notes per step`
(`clampi(round(beats/40) + 1, 1, 20)`), so any difficulty's number is directly comparable — a 420
note extreme shows `d12`, a ~760 note one `d20`. It drives the SongSelect diff buttons
(`_update_diff_buttons:334`) and the left meta (`_update_selection:240`). The generator's
`density_to_rating` (BeatmapGenerator.gd:17) remains for NPS-based maps, and
`BeatmapGenerator.gd:4` gained a fourth profile — **`extreme`** `[0.24, 0.07, 0.10, 8.0, 0.70]`
(target ~8/s) which keeps every detected onset instead of thinning toward a density cap
(`generate_from_media:308`). SongSelect carries the full four-tier order
(easy/medium/hard/extreme, keys 1–4).

---

## 13. Gameplay backdrop video — the encode that finally survives (`GameManager`-hosted)

**The bug:** for ~half the songs the gameplay background showed a **still** (thumb) while the
song-menu preview always worked. The menu preview is a tiny 10 s clip; gameplay needed the
**full-length** Theora ogv. The old encoder lived on the Game scene and was **killed in the Game
scene's `_exit_tree` on every scene change**, so the encode virtually never finished: only 4 of
13 songs ever got a `*_video_k30.ogv` in the cache, and each new play restarted the doomed race.

**The fix (uncommitted):** the encoder moved to `GameManager` (autoload → survives every scene
change):

- `start_bg_video:193` is idempotent (skip if running / cached / not a video), discards a stale
  `.tmp` left by a killed run, and launches **detached** ffmpeg
  `-vf scale=-2:540 -c:v libtheora -q:v 4 -g:v 30 -an` into `…_video_k30.ogv.tmp`.
- `GameManager._process:97` reaps the finished process, promotes the `.tmp` to the final cache
  name (`_finish_bg_video:224`) and hot-attaches it to the live game node
  (`_attach_bg_video:233` → deferred `Game._on_bg_video_done:375`), swapping the thumbnail for
  video **mid-run** if the song is already playing. A partial `.tmp` with no tracked process is
  never promoted.
- Killed **only at app exit** (`_exit_tree:146`); `Game._exit_tree:1220` intentionally does NOT
  kill it, so leaving the song caches the ogv for next time.
- **Pre-started from song select** the moment a video song is focused (`SongSelect.gd:246-247`),
  so by the time you play the ogv is usually cached → video rolls with the music from the end of
  the countdown.

Two measured reasons for the 540p downscale + `-g:v 30`: full-native 720p encodes at only
**~1.6× realtime** (30 s clip ≈ 19.3 s), while 540p runs **~2.1× realtime** (30 s ≈ 14.1 s, ~half
the bitrate) — the backdrop sits under a heavy dim so native res is wasted; and
`-g:v 30` forces a keyframe every ~1 s to dodge the classic Theora stutter ~20 s in
(`godotengine/godot#66331`). The `_k30` cache tag forces regeneration of the old stalled encodes,
and the 4 songs that already had a 720p ogv keep playing (no cache-name change).

---

## Files touched

- **New:** `scenes/Startup.tscn`, `scripts/Startup.gd`, `scripts/Ui.gd`, `scripts/JudgementBursts.gd`,
  `DOCUMENTATIONDAY5.md` (this file).
- **09/03 commits:** `scripts/Main.gd`, `scripts/SongSelect.gd`, `scenes/Main.tscn`,
  `scenes/SongSelect.tscn`, `scenes/Game.tscn` (HUD health), plus the 14 fix commits
  (`65eef62`, `53c91ad`, `2857033`, `1053d82`, `f36e99d`, `b242542`, `79ef0cf`, `8082ee4`,
  `5bad179`, `a642bdc`, `b5af1c4`, `599adac`, `d988723`, `07c5b4f`, `e1f3e4c`, `24f76d4`,
  `13a6541`).
- **Uncommitted (03→05/09):** `scripts/GameManager.gd` (detached encode, result plumbing),
  `scripts/Game.gd` (loop, judgement, pause overlay, results, autoplay/no-death),
  `scripts/Settings.gd`, `scripts/SettingsScreen.gd`, `scripts/Beatmap.gd` (history,
  `notes_to_rating`), `scripts/BeatmapGenerator.gd` (extreme profile), `scripts/Main.gd`,
  `scripts/SongSelect.gd`, all `Project settings`/scenes.

**Current state / next:** everything above is implemented and code-reviewed; the admin cheats and
the newly-reworked backdrop-video encode still need an in-editor playthrough (first run of an
uncached song may still hot-swap mid-intro; subsequent plays are cached and start with the song).