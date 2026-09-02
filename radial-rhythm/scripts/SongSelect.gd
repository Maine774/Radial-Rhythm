extends Control
# Mirrors song_select carousel in main.py:2390 — shows cached beatmap + best history.

@onready var list: VBoxContainer = $VBox/List

func _ready() -> void:
	var songs := _get_songs()
	if songs.is_empty():
		var lbl := Label.new()
		lbl.text = "No songs found — drop mp4/mp3/wav into res://songs or C:/Users/LOK0008/rhythmgame/songs"
		lbl.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		list.add_child(lbl)
		var demo_btn := Button.new()
		demo_btn.text = "PLAY DEMO"
		demo_btn.pressed.connect(_on_play.bind("", "easy"))
		list.add_child(demo_btn)
		return
	for path in songs:
		var row := HBoxContainer.new()
		var name_lbl := Label.new()
		name_lbl.text = path.get_file()
		name_lbl.custom_minimum_size = Vector2(340, 0)
		name_lbl.add_theme_font_size_override("font_size", 11)
		row.add_child(name_lbl)
		for diff in ["easy","medium","hard"]:
			var data = Beatmap.load_cached(path, diff)
			var hist: Array = Beatmap.load_history(path, diff)
			var best: String = Beatmap.best_grade_from_history(hist)
			var fc: int = Beatmap.best_max_fc_from_history(hist)
			var txt := "-"
			if data:
				var rating: int = int(data.get("rating", 1))
				var beats: int = int(data.get("beatmap", []).size())
				txt = "d%d %d" % [rating, beats]
				if not best.is_empty():
					txt += " %s" % best
					if fc > 0: txt += " FC%d" % fc
			var btn := Button.new()
			btn.text = "%s: %s" % [diff.substr(0,1).to_upper(), txt]
			btn.custom_minimum_size = Vector2(150, 0)
			btn.add_theme_font_size_override("font_size", 10)
			if not best.is_empty():
				btn.add_theme_color_override("font_color", Color(0.55, 0.9, 0.67))
			btn.pressed.connect(_on_play.bind(path, diff))
			row.add_child(btn)
		list.add_child(row)

func _on_play(path: String, diff: String) -> void:
	GameManager.pending_song_path = path
	GameManager.difficulty = diff
	GameManager.change_state(GameManager.State.PLAYING)
	get_tree().change_scene_to_file("res://scenes/Game.tscn")

func _get_songs() -> Array:
	var out: Array = []
	for dir_path in ["res://songs", "C:/Users/LOK0008/rhythmgame/songs"]:
		var d := DirAccess.open(dir_path)
		if d:
			d.list_dir_begin()
			var f := d.get_next()
			while f != "":
				if not d.current_is_dir() and f.get_extension().to_lower() in ["mp4","m4v","mov","avi","mkv","mp3","wav","m4a","ogg","flac","webm"]:
					out.append(dir_path.path_join(f))
				f = d.get_next()
			if out.size() > 0:
				break
	return out

func _on_back_pressed() -> void:
	GameManager.change_state(GameManager.State.MENU)
	get_tree().change_scene_to_file("res://scenes/Main.tscn")
