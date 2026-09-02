extends Control
# Mirrors main.py _adjust_range_setting with audible test tones.
# Sliders call Settings.adjust_range and then play a preview at the new volume.

@onready var music_slider: HSlider = $VBox/MusicRow/MusicSlider
@onready var fx_slider: HSlider = $VBox/FxRow/FxSlider
@onready var fx_player: AudioStreamPlayer = $FxPlayer
@onready var music_player: AudioStreamPlayer = $MusicPlayer
@onready var music_val: Label = $VBox/MusicRow/MusicVal
@onready var fx_val: Label = $VBox/FxRow/FxVal

func _ready() -> void:
	# wire sliders to Settings (main.py:RANGE_CFG step 0.05)
	music_slider.min_value = 0.0; music_slider.max_value = 1.0; music_slider.step = 0.05
	fx_slider.min_value = 0.0; fx_slider.max_value = 1.0; fx_slider.step = 0.05
	music_slider.value = float(Settings.settings.get("music_volume", 0.9))
	fx_slider.value = float(Settings.settings.get("fx_volume", 0.7))
	_update_labels()
	# preload streams if not already set in editor
	if fx_player.stream == null:
		fx_player.stream = load("res://assets/sfx/clickfx.mp3")
	if music_player.stream == null:
		music_player.stream = load("res://assets/sfx/_test_tone.wav")

func _update_labels() -> void:
	music_val.text = "%d%%" % int(round(music_slider.value * 100))
	fx_val.text = "%d%%" % int(round(fx_slider.value * 100))

func _on_music_slider_value_changed(value: float) -> void:
	Settings.settings["music_volume"] = value
	Settings.save_config()
	_update_labels()
	# click-test tone at music volume (mirrors main.py _play_test_tone)
	music_player.volume_db = linear_to_db(clampf(value, 0.0, 1.0)) if value > 0.001 else -80.0
	music_player.play()

func _on_fx_slider_value_changed(value: float) -> void:
	Settings.settings["fx_volume"] = value
	Settings.save_config()
	_update_labels()
	fx_player.volume_db = linear_to_db(clampf(value, 0.0, 1.0)) if value > 0.001 else -80.0
	fx_player.play()

func _on_back_pressed() -> void:
	GameManager.change_state(GameManager.State.MENU)
	get_tree().change_scene_to_file("res://scenes/Main.tscn")

func _on_music_slider_drag_ended(_v: bool) -> void: pass
