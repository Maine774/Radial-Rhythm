extends Node

# State machine — mirrors main.py state in plan.md:4
# menu | song_select | difficulty_select | analyzing | playing | paused | results | settings | keybinds
enum State { MENU, SONG_SELECT, DIFFICULTY_SELECT, ANALYZING, PLAYING, PAUSED, RESULTS, SETTINGS, KEYBINDS }

var state: State = State.MENU
var pending_song_path: String = ""
var difficulty: String = "easy" # easy | medium | hard

# scoring — mirrors main.py:1216
var score: int = 0
var combo: int = 0
var max_combo: int = 0
var fc: int = 0
var max_fc: int = 0
var hits: Dictionary = {"perfect": 0, "good": 0, "meh": 0, "miss": 0}

signal state_changed(new_state: State)
signal score_changed

func change_state(s: State) -> void:
	state = s
	state_changed.emit(s)

func reset_play_state() -> void:
	score = 0; combo = 0; max_combo = 0; fc = 0; max_fc = 0
	hits = {"perfect": 0, "good": 0, "meh": 0, "miss": 0}
	score_changed.emit()

func break_fc() -> void:
	fc = 0

#grade helper mirrors main.py:2225 — use float division to avoid GDScript int-div warning
func max_possible_score(beat_count: int) -> int:
	var sc := 0; var c := 0
	for i in beat_count:
		var mult := 1.0 + mini(int(c / 8.0), 4) * 0.25
		sc += int(300 * mult); c += 1
	return maxi(sc, 1)

func grade(beat_count: int) -> Array:
	var max_sc := max_possible_score(beat_count)
	var pct := float(score) / float(max_sc) * 100.0 if max_sc > 0 else 0.0
	if pct >= 90.0: return ["A", pct]
	if pct >= 70.0: return ["B", pct]
	if pct >= 50.0: return ["C", pct]
	return ["D", pct]

var _result_is_new_best: bool = false

func record_result(media_path: String, beat_count: int) -> void:
	# mirrors main.py _record_result — stores grade/FC into cache history
	_result_is_new_best = false
	if media_path.is_empty(): return
	var g: Array = grade(beat_count)
	var hist: Array = Beatmap.load_history(media_path, difficulty)
	var old_best: String = Beatmap.best_grade_from_history(hist)
	var total: int = hits["perfect"] + hits["good"] + hits["meh"] + hits["miss"]
	var acc: float = 0.0
	if total > 0:
		acc = (hits["perfect"]*1.0 + hits["good"]*0.85 + hits["meh"]*0.6) / float(total) * 100.0
	var entry := {
		"date": Time.get_datetime_string_from_system(false, true),
		"grade": g[0], "pct": snappedf(g[1], 0.1),
		"score": score, "max_fc": max_fc, "max_combo": max_combo,
		"acc": snappedf(acc, 0.1), "diff": difficulty
	}
	Beatmap.add_history_entry(media_path, difficulty, entry)
	var cur_best: String = Beatmap.best_grade_from_history(Beatmap.load_history(media_path, difficulty))
	_result_is_new_best = (cur_best != old_best and not cur_best.is_empty())
