class_name BeatmapGenerator
extends RefCounted
# GDScript port of main.py beatmap algorithm
const DIFFICULTY_PROFILES := {
	"easy": [0.32, 0.12, 0.38, 1.45, 0.35, "EASY"],
	"medium": [0.30, 0.11, 0.26, 2.10, 0.45, "MEDIUM"],
	"hard": [0.28, 0.10, 0.18, 3.00, 0.55, "HARD"],
}
const RATING_MIN_NPS := 1.45
const RATING_MAX_NPS := 3.00

static func clamp_difficulty(d: String) -> String:
	d = d.to_lower()
	if d in DIFFICULTY_PROFILES: return d
	return "easy"
static func density_to_rating(nps: float) -> int:
	var lo := RATING_MIN_NPS
	var hi := RATING_MAX_NPS
	var n := clampf(nps, lo, hi)
	return clampi(int(round(1 + (n - lo) / (hi - lo) * 19)), 1, 20)
static func _peak_pick(flux: PackedFloat32Array, thr: float, combine: float, fps: float) -> Array:
	var times: Array[float] = []
	var combine_frames := int(combine * fps)
	var last_peak := -100000
	for i in range(1, flux.size() - 1):
		if flux[i] > thr and flux[i] > flux[i-1] and flux[i] >= flux[i+1]:
			if i - last_peak >= combine_frames:
				times.append(float(i) / fps)
				last_peak = i
			else:
				if flux[i] > flux[last_peak]:
					times[times.size() - 1] = float(i) / fps
					last_peak = i
	return times
static func _enforce_min_gap(times: Array, mg: float) -> Array:
	var out: Array[float] = []
	for t in times:
		if out.is_empty() or t - out[-1] >= mg - 1e-6:
			out.append(float(t))
	return out
static func _onset_flux_from_wav(path: String, sr: int = 48000) -> PackedFloat32Array:
	var wav_path := ProjectSettings.globalize_path(path) if path.begins_with("user://") else path
	if path.begins_with("user://"):
		wav_path = ProjectSettings.globalize_path(path)
	var audio := _read_wav_mono(wav_path, sr)
	if audio.is_empty():
		return PackedFloat32Array()
	var hop := int(sr / 100)
	var flux := PackedFloat32Array()
	var prev_energy := 0.0
	for i in range(0, audio.size() - hop, hop):
		var energy := 0.0
		for j in range(hop):
			var s := float(audio[i + j])
			energy += s * s
		energy = sqrt(energy / hop)
		var log_e := log(energy + 1e-9)
		var diff := maxf(0.0, log_e - prev_energy)
		flux.append(float(diff))
		prev_energy = log_e
	return flux
static func _read_wav_mono(path: String, target_sr: int) -> PackedFloat32Array:
	var f := FileAccess.open(path, FileAccess.READ)
	if f == null: return PackedFloat32Array()
	f.seek(44)
	var bytes := f.get_buffer(f.get_length() - 44)
	f.close()
	var samples := PackedFloat32Array()
	samples.resize(bytes.size() / 2)
	for i in range(0, bytes.size(), 2):
		var v := int(bytes[i]) | (int(bytes[i+1]) << 8)
		if v >= 32768: v -= 65536
		samples[i/2] = float(v) / 32768.0
	return samples
# --- FFT + centroid (port of _centroids_for_times) ---
static func _hanning(n: int) -> PackedFloat32Array:
	var w := PackedFloat32Array()
	w.resize(n)
	for i in range(n):
		w[i] = 0.5 * (1.0 - cos(2.0 * PI * float(i) / float(n - 1)))
	return w
static func _fft_magnitude(win: PackedFloat32Array) -> PackedFloat32Array:
	var n := win.size()
	# iterative Cooley-Tukey radix-2
	var re := PackedFloat32Array()
	var im := PackedFloat32Array()
	re.resize(n)
	im.resize(n)
	for i in range(n):
		re[i] = win[i]
		im[i] = 0.0
	var j := 0
	for i in range(1, n):
		var bit := n >> 1
		while j & bit:
			j ^= bit
			bit >>= 1
		j ^= bit
		if i < j:
			var tr := re[i]; re[i] = re[j]; re[j] = tr
			var ti := im[i]; im[i] = im[j]; im[j] = ti
	var len2 := 2
	while len2 <= n:
		var ang := -2.0 * PI / float(len2)
		var wlen_re := cos(ang)
		var wlen_im := sin(ang)
		var k := 0
		while k < n:
			var w_re := 1.0
			var w_im := 0.0
			for t in range(len2/2):
				var u_re := re[k + t]
				var u_im := im[k + t]
				var v_re := re[k + t + len2/2] * w_re - im[k + t + len2/2] * w_im
				var v_im := re[k + t + len2/2] * w_im + im[k + t + len2/2] * w_re
				re[k + t] = u_re + v_re
				im[k + t] = u_im + v_im
				re[k + t + len2/2] = u_re - v_re
				im[k + t + len2/2] = u_im - v_im
				var nw_re := w_re * wlen_re - w_im * wlen_im
				var nw_im := w_re * wlen_im + w_im * wlen_re
				w_re = nw_re
				w_im = nw_im
			k += len2
		len2 <<= 1
	var mag := PackedFloat32Array()
	mag.resize(n/2 + 1)
	for i in range(n/2 + 1):
		mag[i] = sqrt(re[i]*re[i] + im[i]*im[i])
	return mag
static func _centroids_for_times(times: Array, sr: int, audio: PackedFloat32Array) -> Array:
	if times.is_empty() or audio.is_empty():
		var empty: Array[float] = []
		for t in times: empty.append(0.0)
		return empty
	var cents: Array[float] = []
	var n := audio.size()
	var N := 2048
	var hann := _hanning(N)
	for t in times:
		var c := int(float(t) * sr)
		var half := 1024
		var lo := maxi(0, c - half)
		var hi := mini(n, c + half)
		var win_len := hi - lo
		if win_len < 256:
			cents.append(0.0)
			continue
		var win := PackedFloat32Array()
		win.resize(N)
		for i in range(N):
			if i < win_len:
				win[i] = audio[lo + i] * hann[i]
			else:
				win[i] = 0.0
		var mag := _fft_magnitude(win)
		# freqs: rfftfreq
		var s_sum := 0.0
		var w_sum := 0.0
		for i in range(mag.size()):
			var freq := float(i) * float(sr) / float(N)
			# log compress like Python: log1p(spec*30)
			var v := log(1.0 + mag[i] * 30.0)
			s_sum += v
			w_sum += freq * v
		if s_sum < 1e-6:
			cents.append(0.0)
		else:
			cents.append(float(w_sum / s_sum))
	return cents
static func beatmap_from_times(times: Array, duration: float, centroids: Array = []) -> Array:
	if times.is_empty(): return []
	times = times.duplicate()
	times.sort()
	if centroids.is_empty() or centroids.size() != times.size():
		var beatmap: Array = []
		var last_used := {"d": -999.0, "f": -999.0, "j": -999.0, "k": -999.0}
		var prev: Variant = null
		for t in times:
			var cands: Array[String] = ["d","f","j","k"]
			cands.sort_custom(func(a,b): return last_used[a] < last_used[b])
			var chosen: String = cands[0]
			if chosen == prev and cands.size() > 1 and float(t) - last_used[chosen] < 0.45:
				chosen = cands[1]
			beatmap.append([float(t), chosen])
			last_used[chosen] = float(t)
			prev = chosen
		return beatmap
	var order: Array[int] = []
	for i in range(centroids.size()): order.append(i)
	order.sort_custom(func(a,b): return centroids[a] < centroids[b])
	var rank: Array[float] = []
	rank.resize(times.size())
	for r in range(order.size()):
		rank[order[r]] = float(r) / max(1, order.size()-1)
	var beatmap2: Array = []
	var last_used2 := {"d": -999.0, "f": -999.0, "j": -999.0, "k": -999.0}
	var prev2: Variant = null
	for i in range(times.size()):
		var t: float = float(times[i])
		var pref_idx := int(rank[i] * 4)
		if pref_idx > 3: pref_idx = 3
		var best_lane: String = "d"
		var best_score := 1e9
		for ci in range(4):
			var lane: String = ["d","f","j","k"][ci]
			var pitch_cost: float = float(abs(ci - pref_idx)) * 1.0
			var recency: float = t - last_used2[lane]
			var repeat_cost := 0.0
			if recency < 0.40:
				repeat_cost += (0.40 - recency) * 6.0
			if lane == prev2 and i > 0 and t - float(times[i-1]) < 0.55:
				repeat_cost += 1.8
			var lru_bonus: float = -recency * 0.02
			var score: float = pitch_cost + repeat_cost + lru_bonus
			if score < best_score:
				best_score = score
				best_lane = lane
		beatmap2.append([float(t), best_lane])
		last_used2[best_lane] = t
		prev2 = best_lane
	return beatmap2
func generate_from_media(media_path: String, difficulty: String) -> Variant:
	difficulty = clamp_difficulty(difficulty)
	var prof: Array = DIFFICULTY_PROFILES[difficulty]
	var thr: float = prof[0]
	var comb: float = prof[1]
	var mg: float = prof[2]
	var target_density: float = prof[3]
	var wav_tmp := "user://tmp_geng.wav"
	var wav_path := ProjectSettings.globalize_path(wav_tmp)
	var base_dir := "C:/Users/LOK0008/rhythmgame/ffmpeg_shared"
	var ffmpeg := "ffmpeg"
	var d := DirAccess.open(base_dir)
	if d:
		d.list_dir_begin()
		var f := d.get_next()
		while f != "":
			if d.current_is_dir():
				var cand := base_dir.path_join(f).path_join("bin/ffmpeg.exe")
				if FileAccess.file_exists(cand):
					ffmpeg = cand
					break
			f = d.get_next()
	DirAccess.make_dir_recursive_absolute("user://")
	var args: Array[String] = ["-y", "-i", media_path, "-vn", "-ac", "1", "-ar", "48000", "-acodec", "pcm_s16le", wav_path]
	var out: Array = []
	var ec := OS.execute(ffmpeg, args, out, true)
	if ec != 0 or not FileAccess.file_exists(wav_tmp):
		var demo: Array = generate_demo_pattern(128, 16)
		var dur2: float = float(demo[-1][0]) + 2.0 if demo.size() > 0 else 30.0
		return {"beatmap": demo, "duration": dur2, "tempo": 120.0, "rating": 1}
	var audio := _read_wav_mono(wav_path, 48000)
	var duration: float = float(audio.size()) / 48000.0
	if duration < 1.0: duration = 30.0
	var flux := _onset_flux_from_wav(wav_tmp, 48000)
	if flux.is_empty():
		var demo2: Array = generate_demo_pattern(128, 16)
		return {"beatmap": demo2, "duration": duration, "tempo": 120.0, "rating": 1}
	var target_n := int(round(duration * target_density))
	var beats: Array = _peak_pick(flux, thr, comb, 100.0)
	beats = _enforce_min_gap(beats, mg)
	if beats.size() > int(target_n * 1.25):
		var vals: Array[float] = []
		for t in beats:
			var idx := int(round(t * 100))
			idx = clampi(idx, 0, flux.size()-1)
			vals.append(float(flux[idx]))
		var order: Array[int] = []
		for i in range(beats.size()): order.append(i)
		order.sort_custom(func(a,b): return vals[a] > vals[b])
		var keep: Array[float] = []
		for i in range(mini(target_n, order.size())):
			keep.append(float(beats[order[i]]))
		keep.sort()
		beats = keep
	elif beats.size() > target_n:
		beats = beats.slice(0, target_n)
	if difficulty in ["easy","medium"] and beats.size() > 4:
		var gap_thr := 3.5
		var pool: Array = _peak_pick(flux, 0.30, 0.10, 100.0)
		pool = _enforce_min_gap(pool, 0.12)
		var sorted_beats: Array = beats.duplicate()
		sorted_beats.sort()
		var gaps: Array[Array] = []
		if sorted_beats[0] > 4.0:
			gaps.append([0.0, sorted_beats[0]])
		for i in range(sorted_beats.size()-1):
			if sorted_beats[i+1] - sorted_beats[i] > gap_thr:
				gaps.append([sorted_beats[i], sorted_beats[i+1]])
		if duration - sorted_beats[-1] > 4.0:
			gaps.append([sorted_beats[-1], duration])
		for g in gaps:
			var g0: float = g[0]
			var g1: float = g[1]
			var cands: Array[float] = []
			for t in pool:
				if t > g0 + 0.3 and t < g1 - 0.3:
					cands.append(float(t))
			if cands.is_empty(): continue
			var best_t: float = cands[0]
			var best_v: float = -1.0
			for t in cands:
				var idx2 := int(round(t*100))
				if idx2 >= 0 and idx2 < flux.size():
					var v := float(flux[idx2])
					if v > best_v:
						best_v = v
						best_t = float(t)
			var ok := true
			for b in sorted_beats:
				if abs(best_t - float(b)) < mg * 0.8:
					ok = false
					break
			if ok:
				beats.append(best_t)
		beats.sort()
		var tmp: Array[float] = []
		for t in beats:
			if tmp.is_empty() or t - tmp[-1] >= mg * 0.8:
				tmp.append(float(t))
		beats = tmp
	var bpm := 120.0
	var cents := _centroids_for_times(beats, 48000, audio)
	var beatmap: Array = beatmap_from_times(beats, duration, cents)
	var nps: float = float(beatmap.size()) / max(1.0, duration)
	var rating: int = density_to_rating(nps)
	if FileAccess.file_exists(wav_tmp):
		DirAccess.remove_absolute(wav_path)
	return {"beatmap": beatmap, "duration": duration, "tempo": bpm, "rating": rating}
static func generate_demo_pattern(bpm: float = 128.0, bars: int = 16) -> Array:
	var interval := 60.0 / bpm
	var beats: Array = []
	var lanes := ["d","f","j","k"]
	for i in range(int(bars * 4 * 60.0 / bpm / interval)):
		beats.append([i * interval, lanes[i % 4]])
	return beats
