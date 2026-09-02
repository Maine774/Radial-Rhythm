extends Control
# Playable port of main.py — minimal but functional.
# Loads cached beatmap (or demo), plays audio via ffmpeg-extracted wav, spawns spiral beats.

const WINDOW_W := 1280
const WINDOW_H := 720
const CENTER := Vector2(640, 360)
const TARGET_RADIUS := 70.0
const SPAWN_RADIUS := 520.0
const TRAVEL_TIME := 1.6
const HIT_PERFECT := 0.13
const HIT_GOOD := 0.26
const HIT_OK := 0.35
const SECTION_GAP := 2.0

const LANE_ORDER := ["d","f","j","k"]
const LANE_ANGLES := {"d": 180.0, "f": 90.0, "j": 0.0, "k": 270.0}
const LANE_COLORS := {
	"d": Color(1, 0.29, 0.29),
	"f": Color(0.29, 0.565, 1),
	"j": Color(0.29, 1, 0.541),
	"k": Color(1, 0.843, 0.29),
}

@onready var center_node: Node2D = $Center
@onready var bursts: CanvasLayer = $JudgementBursts
@onready var music_player: AudioStreamPlayer = $MusicPlayer
@onready var click_player: AudioStreamPlayer = $ClickPlayer
@onready var top_score: Label = $HUD/Score
@onready var top_hits: Label = $HUD/Hits
@onready var top_grade: Label = $HUD/Grade
@onready var top_time: Label = $HUD/Time
@onready var progress: ProgressBar = $HUD/Progress
@onready var results_panel: Control = $Results
@onready var bg_image: TextureRect = $BgImage
@onready var video_player: VideoStreamPlayer = $VideoPlayer
@onready var bg_dim: ColorRect = $Bg

var beatmap: Array = [] # [[time, lane], ...]
var duration: float = 30.0
var active_beats: Array = []
var next_index: int = 0
var start_time: float = 0.0
var is_playing: bool = false
var beat_offset: float = 0.0
var hit_pulse: float = 0.0
var lane_flash: Dictionary = {"d":0.0,"f":0.0,"j":0.0,"k":0.0}

# fx
var _shake_t: float = 0.0
var _shake_dur: float = 0.0
var _shake_mag: float = 0.0
var _rings: Array = []
var _gen_thread: Thread
var _gen_pending_path: String = ""
var _gen_pending_diff: String = ""
var _is_generating: bool = false

func _ready() -> void:
	center_node.position = CENTER
	beat_offset = float(Settings.settings.get("input_latency", 0.0))
	AudioServer.set_mix_rate(48000)
	# Try cache first; if miss, generate in background thread to avoid WASAPI block
	var path: String = GameManager.pending_song_path
	var diff: String = GameManager.difficulty
	var data = Beatmap.load_cached(path, diff) if not path.is_empty() else null
	if data == null and not path.is_empty():
		_is_generating = true
		_gen_pending_path = path
		_gen_pending_diff = diff
		# show loading
		if has_node("HUD/Time"):
			$HUD/Time.text = "Analyzing..."
		_gen_thread = Thread.new()
		_gen_thread.start(_thread_generate_beatmap)
	else:
		_load_beatmap_and_audio()
		_setup_background(path)
	GameManager.reset_play_state()
	start_time = Time.get_ticks_msec() / 1000.0
	is_playing = not _is_generating
	# apply volumes
	var mv := float(Settings.settings.get("music_volume", 0.9))
	var fv := float(Settings.settings.get("fx_volume", 0.7))
	music_player.volume_db = linear_to_db(clampf(mv, 0.001, 1.0)) if mv > 0.001 else -80
	click_player.volume_db = linear_to_db(clampf(fv, 0.001, 1.0)) if fv > 0.001 else -80
	if click_player.stream == null:
		click_player.stream = load("res://assets/sfx/clickfx.mp3")
	if music_player.stream != null:
		music_player.play()
		# also start video if available
		if video_player.stream != null:
			video_player.play()
	set_process(true)

func _load_beatmap_and_audio() -> void:
	var path: String = GameManager.pending_song_path
	var diff: String = GameManager.difficulty
	if path.is_empty():
		beatmap = Beatmap.generate_demo_pattern(128, 16)
		duration = beatmap[-1][0] + 2.0 if beatmap.size() > 0 else 30.0
		return
	# try cached beatmap, then actual algorithm (Python madmom/librosa) then GDScript port
	var data = Beatmap.load_cached(path, diff)
	if data != null and data is Dictionary and data.has("beatmap"):
		var bm = data.get("beatmap", [])
		beatmap = []
		for e in bm:
			if e is Array and e.size() >= 2:
				beatmap.append([float(e[0]), str(e[1])])
			elif e is Dictionary:
				beatmap.append([float(e.get("time", e.get("t", 0))), str(e.get("lane", "d"))])
		duration = float(data.get("duration", 30.0))
		print("[cache] hit %s [%s] -> %d beats" % [path.get_file(), diff, beatmap.size()])
	else:
		print("[cache] miss %s [%s] -> generating via actual algorithm..." % [path.get_file(), diff])
		var ensured = Beatmap.ensure_beatmap(path, diff)
		if ensured != null and ensured is Dictionary and ensured.has("beatmap"):
			var bm2 = ensured.get("beatmap", [])
			beatmap = []
			for e in bm2:
				if e is Array and e.size() >= 2:
					beatmap.append([float(e[0]), str(e[1])])
			duration = float(ensured.get("duration", 30.0))
			print("[generate] ok %s [%s] -> %d beats" % [path.get_file(), diff, beatmap.size()])
		else:
			print("[generate] fallback demo pattern")
			beatmap = Beatmap.generate_demo_pattern(128, 16)
			duration = beatmap[-1][0] + 2.0 if beatmap.size() > 0 else 30.0
	# audio
	var stream := _load_audio_stream(path)
	if stream != null:
		music_player.stream = stream
		print("[audio] loaded %s" % path.get_file())
	else:
		print("[audio] no stream for %s — silent demo timing" % path.get_file())
		music_player.stream = null

func _load_audio_stream(path: String) -> AudioStream:
	var ext := path.get_extension().to_lower()
	if ext in ["mp3"]:
		if FileAccess.file_exists(path):
			var s = AudioStreamMP3.load_from_file(path)
			if s != null: return s
	elif ext in ["wav"]:
		if FileAccess.file_exists(path):
			var s2 = AudioStreamWAV.load_from_file(path)
			if s2 != null: return s2
	elif ext in ["ogg"]:
		if FileAccess.file_exists(path):
			var s3 = AudioStreamOggVorbis.load_from_file(path)
			if s3 != null: return s3
	# for mp4/m4a/mov etc, extract wav via ffmpeg
	if ext in ["mp4","m4v","mov","avi","mkv","m4a","flac","webm","mp4"]:
		var tmp_wav := "user://tmp_audio.wav"
		if _extract_wav(path, ProjectSettings.globalize_path(tmp_wav)):
			var wav_path: String = ProjectSettings.globalize_path(tmp_wav)
			var s4 = AudioStreamWAV.load_from_file(wav_path)
			if s4 != null: return s4
	# also try res://songs copy
	var res_path := "res://songs/" + path.get_file()
	if ResourceLoader.exists(res_path):
		var res = load(res_path)
		if res is AudioStream: return res
	return null

func _find_ffmpeg() -> String:
	var candidates: Array[String] = []
	var base := "C:/Users/LOK0008/rhythmgame/ffmpeg_shared"
	var dir := DirAccess.open(base)
	if dir:
		dir.list_dir_begin()
		var f := dir.get_next()
		while f != "":
			if dir.current_is_dir():
				var cand := base.path_join(f).path_join("bin/ffmpeg.exe")
				if FileAccess.file_exists(cand): return cand
				var cand2 := base.path_join(f).path_join("bin/ffmpeg")
				if FileAccess.file_exists(cand2): return cand2
			f = dir.get_next()
	# try PATH
	var p1 := "ffmpeg.exe"
	var p2 := "ffmpeg"
	# OS.execute will search PATH anyway, so return generic
	return "ffmpeg"

func _extract_wav(media_path: String, wav_path: String) -> bool:
	var ffmpeg := _find_ffmpeg()
	var args := ["-y", "-i", media_path, "-vn", "-ac", "1", "-ar", "48000", "-acodec", "pcm_s16le", wav_path]
	var out: Array = []
	var exit := OS.execute(ffmpeg, args, out, true)
	return exit == 0

func _setup_background(path: String) -> void:
	# bg image fallback
	if bg_image:
		var tex = load("res://assets/backgrounds/bg01.jpeg")
		if tex: bg_image.texture = tex
		var brt := clampf(float(Settings.settings.get("video_brightness", 0.30)), 0.0, 1.0)
		bg_image.modulate = Color(1,1,1, 0.3 + brt * 0.6)
		bg_image.visible = true
	if bg_dim:
		var brt2 := clampf(float(Settings.settings.get("video_brightness", 0.30)), 0.0, 1.0)
		var dim_a := (0.65 - brt2 * 0.45)
		bg_dim.color = Color(0.039, 0.039, 0.07, dim_a)
	# per-song thumbnail — so each song shows its own frame, not generic bg01
	if not path.is_empty() and path.get_extension().to_lower() in ["mp4","m4v","mov","avi","mkv","webm"]:
		var stem_t := path.get_file().get_basename()
		var hv_t := path.replace("/", "\\").md5_text().substr(0, 8)
		var thumb := "user://cache/%s_%s_thumb.jpg" % [stem_t, hv_t]
		var thumb_path := ProjectSettings.globalize_path(thumb)
		DirAccess.make_dir_recursive_absolute("user://cache")
		if not FileAccess.file_exists(thumb):
			var ff2 := _find_ffmpeg()
			var t_args := ["-y", "-ss", "8", "-i", path, "-frames:v", "1", "-q:v", "2", thumb_path]
			var tout: Array = []
			OS.execute(ff2, t_args, tout, true)
		if FileAccess.file_exists(thumb):
			var img := Image.load_from_file(thumb_path)
			if img != null:
				var tex2 := ImageTexture.create_from_image(img)
				bg_image.texture = tex2
	# video background
	if path.is_empty():
		if video_player: video_player.visible = false
		return
	var ext := path.get_extension().to_lower()
	var is_video := ext in ["mp4","m4v","mov","avi","mkv","webm"]
	if not is_video:
		if video_player: video_player.visible = false
		return
	if path.get_extension().to_lower() in ["ogv","webm"]:
		var vs = load(path)
		if vs is VideoStream:
			video_player.stream = vs
			video_player.visible = true
			bg_image.visible = false
			return
	# for mp4, try per-song ogv cache
	var stem := path.get_file().get_basename()
	var hv := path.replace("/", "\\").md5_text().substr(0, 8)
	var tmp_ogv := "user://cache/%s_%s_video.ogv" % [stem, hv]
	var ogv_path := ProjectSettings.globalize_path(tmp_ogv)
	DirAccess.make_dir_recursive_absolute("user://cache")
	var do_convert := true
	if FileAccess.file_exists(tmp_ogv):
		do_convert = false
	if do_convert:
		var ffmpeg := _find_ffmpeg()
		var args2 := ["-y", "-i", path, "-c:v", "libtheora", "-q:v", "7", "-an", ogv_path]
		var out2: Array = []
		var ec := OS.execute(ffmpeg, args2, out2, true)
		if ec != 0 or not FileAccess.file_exists(tmp_ogv):
			if video_player: video_player.visible = false
			return
	var vs2 = null
	if FileAccess.file_exists(tmp_ogv):
		vs2 = load(tmp_ogv)
		if vs2 == null:
			vs2 = null
			if video_player: video_player.visible = false
			return
	if vs2 is VideoStream:
		video_player.stream = vs2
		video_player.visible = true
		bg_image.visible = false
	else:
		if video_player: video_player.visible = false

func _thread_generate_beatmap() -> void:
	var res = Beatmap.ensure_beatmap(_gen_pending_path, _gen_pending_diff)
	call_deferred("_on_beatmap_generated", res)

func _on_beatmap_generated(res: Variant) -> void:
	_is_generating = false
	if _gen_thread:
		_gen_thread.wait_to_finish()
		_gen_thread = null
	_load_beatmap_and_audio()
	_setup_background(_gen_pending_path)
	GameManager.reset_play_state()
	start_time = Time.get_ticks_msec() / 1000.0
	is_playing = true
	if music_player.stream != null:
		music_player.play()
		if video_player and video_player.stream != null:
			video_player.play()

func get_song_time() -> float:
	if is_playing and music_player.playing and music_player.stream != null:
		var p := music_player.get_playback_position()
		# AudioServer latency compensation
		p += AudioServer.get_time_since_last_mix()
		# OS delay + output latency
		p -= AudioServer.get_output_latency()
		return maxf(0.0, p + beat_offset)
	# fallback wall clock (demo)
	return maxf(0.0, Time.get_ticks_msec() / 1000.0 - start_time + beat_offset)

func spiral_point(hit_ang: float, cw: bool, raw: float) -> Vector2:
	var radius := SPAWN_RADIUS - raw * (SPAWN_RADIUS - TARGET_RADIUS)
	if radius < TARGET_RADIUS: radius = TARGET_RADIUS
	var ang: float
	if cw:
		ang = (hit_ang + 90.0) - raw * 90.0
	else:
		ang = (hit_ang - 90.0) + raw * 90.0
	# Y flipped vs Pyglet (Godot Y down) — so blue (90°) stays top, yellow (270°) bottom
	return Vector2(cos(deg_to_rad(ang)) * radius, -sin(deg_to_rad(ang)) * radius)

func spawn_beats(song_t: float) -> void:
	while next_index < beatmap.size():
		var bt: float = float(beatmap[next_index][0])
		var lane: String = str(beatmap[next_index][1])
		var bt_eff := bt + beat_offset
		if bt_eff - song_t <= TRAVEL_TIME + 0.05:
			if bt_eff >= song_t - HIT_OK:
				var hit_ang: float = float(LANE_ANGLES.get(lane, 0.0))
				var prev_t: Variant = null
				if active_beats.size() > 0:
					prev_t = active_beats[-1]["time"]
				var is_new_section: bool = (prev_t == null) or (bt_eff - float(prev_t) > SECTION_GAP)
				var cw: bool = not is_new_section
				var start_ang: float = fmod(hit_ang + 90.0, 360.0) if cw else fmod(hit_ang - 90.0 + 360.0, 360.0)
				active_beats.append({
					"time": bt_eff, "lane": lane, "angle": hit_ang, "start_ang": start_ang, "cw": cw,
					"hit": false, "missed": false, "spawn_t": song_t
				})
			next_index += 1
		else:
			break

func _process(delta: float) -> void:
	if not is_playing:
		# still decay visuals
		_tick_fx(delta)
		queue_redraw()
		_update_hud(get_song_time())
		return
	var song_t := get_song_time()
	spawn_beats(song_t)
	# handle misses
	var still: Array = []
	for b in active_beats:
		var d: float = song_t - float(b["time"])
		if b.get("missed", false):
			if song_t - float(b.get("miss_time", 0.0)) < 0.45:
				still.append(b)
			continue
		if not b["hit"] and d > HIT_OK:
			GameManager.hits["miss"] += 1
			GameManager.combo = 0
			GameManager.break_fc()
			_trigger_miss()
			if lane_flash.has(b["lane"]): lane_flash[b["lane"]] = 1.0
			b["missed"] = true
			b["miss_time"] = song_t
			still.append(b)
			continue
		if b["hit"] and d > 1.0:
			continue
		if not b["hit"] and d < 1.0:
			still.append(b)
		elif b["hit"]:
			if d < 0.25:
				still.append(b)
		else:
			still.append(b)
	active_beats = still
	for k in lane_flash.keys():
		if lane_flash[k] > 0:
			lane_flash[k] = maxf(0.0, lane_flash[k] - delta * 4.0)
	if hit_pulse > 0:
		hit_pulse = maxf(0.0, hit_pulse - delta * 5.0)
	_tick_fx(delta)
	_update_hud(song_t)
	queue_redraw()
	# end check
	if song_t > duration + 1.0 and next_index >= beatmap.size() and active_beats.is_empty():
		_on_song_finished()

func _tick_fx(delta: float) -> void:
	if _shake_t > 0:
		_shake_t = maxf(0.0, _shake_t - delta)
		var k := _shake_t / _shake_dur if _shake_dur > 0 else 0.0
		var m := _shake_mag * k
		center_node.position = Vector2(CENTER.x + randf_range(-m, m), CENTER.y + randf_range(-m, m))
		if _shake_t == 0:
			center_node.position = CENTER
	var now := Time.get_ticks_msec() / 1000.0
	_rings = _rings.filter(func(r): return now - float(r["t"]) < float(r["dur"]))

func _update_hud(song_t: float) -> void:
	var g: Array = GameManager.grade(beatmap.size())
	top_score.text = "Score %06d   Combo x%d (max %d)" % [GameManager.score, GameManager.combo, GameManager.max_combo]
	top_hits.text = "P:%d  G:%d  MEH:%d  M:%d" % [GameManager.hits["perfect"], GameManager.hits["good"], GameManager.hits["meh"], GameManager.hits["miss"]]
	top_grade.text = "Grade %s" % g[0]
	var cols := {"A": Color(0.47,1,0.59), "B": Color(0.55,0.86,1), "C": Color(1,0.86,0.47), "D": Color(1,0.31,0.31)}
	top_grade.modulate = cols.get(g[0], Color.WHITE)
	top_time.text = "%d:%02d / %d:%02d" % [int(song_t/60), int(fmod(song_t,60)), int(duration/60), int(fmod(duration,60))]
	var prog: float = clampf(song_t / maxf(duration, 0.001), 0.0, 1.0)
	progress.value = prog * 100.0

func _draw() -> void:
	# centre
	var pulse_r: float = TARGET_RADIUS + hit_pulse * 22.0
	draw_arc(center_node.position, pulse_r, 0, TAU, 64, Color(1,1,1,0.08), 2.0)
	draw_circle(center_node.position, 8, Color(1,1,1,0.8))
	# lane spiral guides (faint)
	for lane in LANE_ORDER:
		var ang: float = LANE_ANGLES[lane]
		var col: Color = LANE_COLORS[lane]
		col.a = 0.18 + lane_flash.get(lane, 0.0) * 0.3
		# draw 18 segments of clockwise spiral guide
		var pts: PackedVector2Array = []
		for i in range(19):
			var raw: float = float(i) / 18.0
			var p: Vector2 = spiral_point(ang, true, raw) + CENTER
			# apply shake offset already in center_node? we draw at CENTER, not center_node pos for guides — keep guides static
			# shift to actual draw center (with shake)
			p += center_node.position - CENTER
			pts.append(p)
		for i in range(pts.size()-1):
			draw_line(pts[i], pts[i+1], col, 2.0 + lane_flash.get(lane,0.0)*4.0)
	# beats
	var la: float = clampf(float(Settings.settings.get("lane_alpha", 0.85)), 0.2, 1.0)
	var song_t := get_song_time() if is_playing else 0.0
	for b in active_beats:
		var col: Color = LANE_COLORS.get(str(b["lane"]), Color.WHITE)
		if b.get("missed", false):
			var elapsed: float = song_t - float(b["miss_time"])
			var prog: float = elapsed / 0.45
			if prog >= 1: continue
			var a: float = 160 * (1.0 - prog)
			var sz: float = 22 * (1.0 - prog * 0.3)
			var ang: float = float(b["angle"])
			var pos: Vector2 = CENTER + Vector2(cos(deg_to_rad(ang)) * TARGET_RADIUS, -sin(deg_to_rad(ang)) * TARGET_RADIUS)
			pos += center_node.position - CENTER
			var dim: Color = Color(col.r*0.45+0.176, col.g*0.45+0.176, col.b*0.45+0.176, a/255.0 * la)
			draw_circle(pos, sz, dim)
			continue
		if b["hit"]:
			var d: float = song_t - float(b["time"])
			var prog2: float = d / 0.25
			if prog2 > 1: continue
			var ang2: float = float(b["angle"])
			var pos2: Vector2 = CENTER + Vector2(cos(deg_to_rad(ang2)) * (TARGET_RADIUS + prog2*30), -sin(deg_to_rad(ang2)) * (TARGET_RADIUS + prog2*30))
			pos2 += center_node.position - CENTER
			var a2: float = 255 * (1.0 - prog2)
			var sz2: float = 28 * (1.0 - prog2*0.6)
			var c2: Color = col; c2.a = a2/255.0 * la
			draw_circle(pos2, sz2, c2)
			continue
		# active approaching
		var raw: float = (song_t - (float(b["time"]) - TRAVEL_TIME)) / TRAVEL_TIME if is_playing else 0.0
		raw = clampf(raw, 0.0, 1.2)
		if raw > 1.2: continue
		var p: Vector2 = spiral_point(float(b["angle"]), bool(b["cw"]), raw) + CENTER
		p += center_node.position - CENTER
		var scale: float = 0.9 + 0.35 * raw
		var sz3: float = 22 * scale
		# pulse towards centre
		draw_circle(p, sz3, Color(col.r, col.g, col.b, la))
		draw_circle(p, 12*scale, Color(1,1,1,0.9 * la))
	# rings
	var now := Time.get_ticks_msec() / 1000.0
	for r in _rings:
		var age: float = now - float(r["t"])
		var t: float = age / float(r["dur"])
		var radius: float = 12 + t * float(r["max_r"])
		var alpha: float = 0.75 * (1.0 - t)
		var c: Color = r["color"]; c.a = alpha
		draw_arc(center_node.position, radius, 0, TAU, 48, c, 3.0)

func _unhandled_input(event: InputEvent) -> void:
	if results_panel.visible and event is InputEventKey and event.pressed and not event.echo:
		if event.keycode in [KEY_ENTER, KEY_KP_ENTER, KEY_SPACE, KEY_ESCAPE]:
			get_tree().change_scene_to_file("res://scenes/Main.tscn")
			return
		if event.keycode == KEY_R:
			get_tree().reload_current_scene()
			return
	if event is InputEventKey and event.pressed and not event.echo:
		var lane: String = ""
		if event.is_action_pressed("lane_d"): lane = "d"
		elif event.is_action_pressed("lane_f"): lane = "f"
		elif event.is_action_pressed("lane_j"): lane = "j"
		elif event.is_action_pressed("lane_k"): lane = "k"
		elif event.keycode == KEY_SPACE:
			if results_panel.visible:
				get_tree().change_scene_to_file("res://scenes/Main.tscn")
				return
			_toggle_pause()
			return
		elif event.keycode == KEY_ESCAPE:
			get_tree().change_scene_to_file("res://scenes/Main.tscn")
			return
		if lane != "":
			_play_click()
			_try_hit(lane)

func _play_click() -> void:
	var fv := float(Settings.settings.get("fx_volume", 0.7))
	click_player.volume_db = linear_to_db(clampf(fv, 0.001, 1.0)) if fv > 0.001 else -80
	click_player.play()

func _try_hit(lane: String) -> void:
	if not is_playing: 
		if lane_flash.has(lane): lane_flash[lane] = 1.0
		return
	var song_t := get_song_time()
	var best: Variant = null
	var best_delta: float = 999.0
	for b in active_beats:
		if str(b["lane"]) != lane or bool(b["hit"]) or bool(b.get("missed", false)): continue
		var d: float = absf(song_t - float(b["time"]))
		if d < best_delta:
			best_delta = d
			best = b
	if best == null:
		GameManager.combo = maxi(0, GameManager.combo - 1)
		GameManager.break_fc()
		bursts.spawn("MISS", Color(1,0.31,0.31))
		_trigger_miss()
		if lane_flash.has(lane): lane_flash[lane] = 0.9
		return
	var pts: int = 0
	if best_delta <= HIT_PERFECT:
		pts = 300
		GameManager.hits["perfect"] += 1
		GameManager.fc += 1
		GameManager.max_fc = maxi(GameManager.max_fc, GameManager.fc)
		bursts.spawn("PERFECT", Color(1,0.94,0.31))
		if GameManager.fc > 0 and GameManager.fc % 10 == 0:
			_fc_milestone(GameManager.fc)
	elif best_delta <= HIT_GOOD:
		pts = 200
		GameManager.hits["good"] += 1
		GameManager.break_fc()
		bursts.spawn("GOOD", Color(0.39,1,0.59))
	elif best_delta <= HIT_OK:
		pts = 100
		GameManager.hits["meh"] += 1
		GameManager.break_fc()
		bursts.spawn("MEH", Color(0.39,0.78,1))
	else:
		GameManager.combo = maxi(0, GameManager.combo - 1)
		GameManager.break_fc()
		bursts.spawn("MISS", Color(1,0.31,0.31))
		_trigger_miss()
		if lane_flash.has(lane): lane_flash[lane] = 1.0
		return
	best["hit"] = true
	GameManager.combo += 1
	GameManager.max_combo = maxi(GameManager.max_combo, GameManager.combo)
	var mult: float = 1.0 + mini(int(GameManager.combo / 8.0), 4) * 0.25
	GameManager.score += int(pts * mult)
	lane_flash[lane] = 1.2
	hit_pulse = 1.0

func _trigger_miss() -> void:
	bursts.spawn("MISS", Color(1,0.31,0.31))
	_spawn_ring(Color(1,0.31,0.31), 88)
	_trigger_shake(7.0, 0.26)

func _fc_milestone(fc: int) -> void:
	bursts.spawn("FC x%d" % fc, Color(0.545,0.86,1))
	_spawn_ring(Color(0.47,0.86,1), 108)
	_trigger_shake(3.2, 0.20)

func _trigger_shake(mag: float, dur: float) -> void:
	_shake_mag = mag; _shake_t = dur; _shake_dur = dur

func _spawn_ring(col: Color, max_r: int) -> void:
	_rings.append({"t": Time.get_ticks_msec()/1000.0, "dur": 0.48, "max_r": max_r, "color": col})

func _toggle_pause() -> void:
	if is_playing:
		is_playing = false
		music_player.stream_paused = true
	else:
		is_playing = true
		start_time += 0 # keep wall clock? for simplicity keep song_t via playback pos
		music_player.stream_paused = false

func _on_song_finished() -> void:
	is_playing = false
	if music_player.playing: music_player.stop()
	GameManager.record_result(GameManager.pending_song_path, beatmap.size())
	# show results
	results_panel.visible = true
	var g: Array = GameManager.grade(beatmap.size())
	$Results/Card/VBox/GradeBig.text = "Grade %s" % g[0]
	$Results/Card/VBox/GradeBig.modulate = {"A": Color(0.47,1,0.59), "B": Color(0.55,0.86,1), "C": Color(1,0.86,0.47), "D": Color(1,0.31,0.31)}.get(g[0], Color.WHITE)
	$Results/Card/VBox/ScoreRes.text = "Score %06d    %.0f%% of max" % [GameManager.score, g[1]]
	$Results/Card/VBox/ComboRes.text = "Max Combo  x%d    Perfect Combo  x%d" % [GameManager.max_combo, GameManager.max_fc]
	$Results/Card/VBox/HitsRes.text = "PERFECT %d   GOOD %d   MEH %d   MISS %d" % [GameManager.hits["perfect"], GameManager.hits["good"], GameManager.hits["meh"], GameManager.hits["miss"]]
	var total: int = GameManager.hits["perfect"] + GameManager.hits["good"] + GameManager.hits["meh"] + GameManager.hits["miss"]
	var acc: float = 0.0
	if total > 0: acc = (GameManager.hits["perfect"]*1.0 + GameManager.hits["good"]*0.85 + GameManager.hits["meh"]*0.6) / float(total) * 100.0
	$Results/Card/VBox/AccRes.text = "Accuracy  %.1f%%" % acc
