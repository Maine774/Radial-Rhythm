extends Control

# Entry point — mirrors main.py RhythmGame.__init__ + on_draw dispatcher.
# This is a skeleton you can expand; it already wires Settings + GameManager
# and shows that the project boots with the same 1280×720 / 4-lane contract.

@onready var title: Label = $VBox/Title
@onready var subtitle: Label = $VBox/Subtitle
@onready var menu: VBoxContainer = $VBox/Menu

func _ready() -> void:
	# keep Window settings in sync with Settings (main.py:1382 _apply_fullscreen)
	_apply_fullscreen(Settings.settings.get("fullscreen", false))
	GameManager.state_changed.connect(_on_state_changed)
	Refresh()

func Refresh() -> void:
	title.text = "RADIAL RHYTHM"
	subtitle.text = "Godot 4.7  •  Forward+  •  %d songs in res://songs  •  D F J K to hit" % _song_count()
	# map GameManager state to menu highlight (stub — port the full menu from main.py:2266 later)
	queue_redraw()

func _song_count() -> int:
	var n := 0
	var dir := DirAccess.open("res://songs")
	if dir:
		dir.list_dir_begin()
		var f := dir.get_next()
		while f != "":
			if not dir.current_is_dir() and f.get_extension().to_lower() in ["mp4","m4v","mov","avi","mkv","mp3","wav","m4a","ogg","flac","webm"]:
				n += 1
			f = dir.get_next()
	return n

func _apply_fullscreen(on: bool) -> void:
	if on:
		DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_FULLSCREEN)
	else:
		DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)

func _input(event: InputEvent) -> void:
	if event.is_action_pressed("ui_cancel") and DisplayServer.window_get_mode() == DisplayServer.WINDOW_MODE_FULLSCREEN:
		Settings.settings["fullscreen"] = false
		Settings.save_config()
		_apply_fullscreen(false)
	if event is InputEventKey and event.pressed and event.keycode == KEY_F11:
		var fs := DisplayServer.window_get_mode() == DisplayServer.WINDOW_MODE_FULLSCREEN
		Settings.settings["fullscreen"] = not fs
		Settings.save_config()
		_apply_fullscreen(not fs)

func _on_state_changed(_s) -> void:
	Refresh()

# Placeholder radial visual — proves the viewport matches main.py:57 (1280×720, CENTER 640,360)
func _draw() -> void:
	var center := Vector2(640, 360)
	# faint lane rings (yellow→red→blue→green chain, same as plan.md:31)
	var colors := [Color(1,0.29,0.29), Color(0.29,0.565,1), Color(0.29,1,0.541), Color(1,0.843,0.29)]
	for i in 4:
		draw_arc(center, 70 + i * 18, 0, TAU, 64, colors[i] * Color(1,1,1,0.18), 2.0)

func _on_Play_pressed() -> void:
	GameManager.change_state(GameManager.State.SONG_SELECT)
	get_tree().change_scene_to_file("res://scenes/SongSelect.tscn")

func _on_Settings_pressed() -> void:
	GameManager.change_state(GameManager.State.SETTINGS)
	get_tree().change_scene_to_file("res://scenes/Settings.tscn")

func _on_Quit_pressed() -> void:
	get_tree().quit()
