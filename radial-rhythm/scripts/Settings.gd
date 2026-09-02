extends Node

# Port of main.py:1087 settings + config.json persistence.
# Values match the Pyglet defaults so existing config.json stays compatible.

const CONFIG_PATH := "user://config.json"
# also check res://config.json for migration from the Pyglet build (C:/Users/LOK0008/rhythmgame/config.json)
const LEGACY_PATH := "res://config.json"

var settings: Dictionary = {
	"fullscreen": false,
	"input_latency": 0.0,
	"music_volume": 0.9,
	"fx_volume": 0.7,
	"video_brightness": 0.30,
	"lane_alpha": 0.85,
}
# keybinds: lane -> keycode (mirrors LANES[*]['key'] in main.py:93)
var keybinds: Dictionary = {
	"d": KEY_D,
	"f": KEY_F,
	"j": KEY_J,
	"k": KEY_K,
}

const RANGE_CFG := {
	"input_latency": {"min": -0.20, "max": 0.20, "step": 0.01},
	"music_volume": {"min": 0.0, "max": 1.0, "step": 0.05},
	"fx_volume": {"min": 0.0, "max": 1.0, "step": 0.05},
	"video_brightness": {"min": 0.0, "max": 1.0, "step": 0.05},
	"lane_alpha": {"min": 0.2, "max": 1.0, "step": 0.05},
}

func _ready() -> void:
	load_config()

func load_config() -> void:
	var data: Dictionary = {}
	# try user:// first, then legacy res://, then Pyglet folder
	for path in [CONFIG_PATH, LEGACY_PATH]:
		if FileAccess.file_exists(path):
			var f := FileAccess.open(path, FileAccess.READ)
			if f:
				var parsed = JSON.parse_string(f.get_as_text())
				if parsed is Dictionary:
					data = parsed
					break
	# also try the original Pyglet project for one-time migration (path kept for docs)
	var _pyglet_cfg := "C:/Users/LOK0008/rhythmgame/config.json"
	# FileAccess can't read absolute Windows path, so leave it for manual copy — _pyglet_cfg is reference only
	if data.is_empty():
		return
	for k in settings.keys():
		if data.has(k) and data[k] != null:
			settings[k] = data[k]
	if data.has("keybinds") and data["keybinds"] is Dictionary:
		_apply_keybinds(data["keybinds"])

func save_config() -> void:
	var out := settings.duplicate()
	out["keybinds"] = keybinds.duplicate()
	var f := FileAccess.open(CONFIG_PATH, FileAccess.WRITE)
	if f:
		f.store_string(JSON.stringify(out, "\t"))

func _apply_keybinds(data: Dictionary) -> void:
	for lane in keybinds.keys():
		if data.has(lane):
			var v = data[lane]
			# Pyglet stored (key, label) tuples; Godot stores int keycode
			if v is Array and v.size() >= 1 and v[0] is float:
				keybinds[lane] = int(v[0])
			elif v is float or v is int:
				keybinds[lane] = int(v)

func get_lane_key(lane: String) -> int:
	return keybinds.get(lane, KEY_D)

func adjust_range(key: String, dir: int) -> void:
	var cfg: Dictionary = RANGE_CFG.get(key, {"min": 0.0, "max": 1.0, "step": 0.05})
	var cur: float = float(settings.get(key, 0.0))
	if key == "input_latency":
		cur = snappedf(cur + dir * 0.01, 0.001)
		cur = clampf(cur, -0.20, 0.20)
	else:
		cur = snappedf(cur + dir * float(cfg["step"]), 0.01)
		cur = clampf(cur, float(cfg["min"]), float(cfg["max"]))
	settings[key] = cur
	save_config()
