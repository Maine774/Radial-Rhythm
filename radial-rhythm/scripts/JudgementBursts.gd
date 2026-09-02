extends CanvasLayer
# Port of main.py _spawn_judge / _draw_judge_bursts (float-up, scale pop, fade).
# Attach to Game or Main scene. Call spawn("PERFECT", Color) from gameplay.

func spawn(text: String, color: Color) -> void:
	var lbl := Label.new()
	lbl.text = text
	lbl.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	lbl.custom_minimum_size = Vector2(400, 30)
	lbl.size = Vector2(400, 30)
	lbl.add_theme_font_size_override("font_size", 28 if text == "PERFECT" else 24)
	if text.begins_with("FC"):
		lbl.add_theme_font_size_override("font_size", 18)
	lbl.add_theme_color_override("font_color", color)
	lbl.position = Vector2(440, 360 + 76) # 640-200 centered
	add_child(lbl)
	# pop + float-up + fade — mirrors main.py 0.9s burst
	var tween := create_tween()
	tween.set_parallel(true)
	# rise 54px over 0.9s
	tween.tween_property(lbl, "position:y", lbl.position.y - 54, 0.9).set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_OUT)
	# fade
	tween.tween_property(lbl, "modulate:a", 0.0, 0.9).set_trans(Tween.TRANS_LINEAR)
	# scale pop (1.0 -> 1.14 -> 1.0 via sine)
	lbl.scale = Vector2.ONE
	tween.tween_property(lbl, "scale", Vector2(1.14, 1.14), 0.15).set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_OUT)
	tween.chain().tween_property(lbl, "scale", Vector2.ONE, 0.35).set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_IN)
	tween.finished.connect(func(): lbl.queue_free())

func spawn_with_shake(text: String, color: Color, shake: Vector2 = Vector2.ZERO) -> void:
	spawn(text, color)
	if shake != Vector2.ZERO:
		# brief offset for the burst itself
		pass
