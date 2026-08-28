"""
Radial Rhythm Game - Pyglet
Beats converge from outside -> centre.
4 lanes: D (red, left), F (blue, up), J (green, right), K (yellow, down)
- Provide an MP4 to auto-sync beats (ffmpeg + numpy onset detection, or librosa if available)
- No song -> demo pattern

Songs folder: ./songs/  - put mp4/mp3/wav etc there, pick from Songs menu
Main menu -> Songs browser -> Play

Controls (state-dependent):
  Menu:      UP/DOWN or W/S navigate, ENTER/SPACE select
  Songs:     UP/DOWN select, ENTER play, B/ESC back, R refresh, O open external
  Playing:   D/F/J/K hit, SPACE pause/resume, ESC -> menu
  Global:    O open file, ESC quit from menu
"""

import json
import math
import os
import sys
import time
import tempfile
import subprocess
import wave
import threading
import hashlib
import ctypes
from pathlib import Path

# Find bundled FFmpeg shared DLLs so pyglet can decode mp4 natively.
# If present, prepend its bin dir to PATH *before* pyglet imports its ffmpeg bindings.
_BUNDLED_FFMPEG_BIN = None
for _d in [Path(__file__).parent / "ffmpeg_shared", Path(__file__).resolve().parent / "ffmpeg_shared"]:
    if _d.is_dir():
        for _sub in sorted(_d.iterdir(), reverse=True):
            if (_sub / "bin").is_dir() and any((_sub / "bin").glob("avcodec-*.dll")):
                _BUNDLED_FFMPEG_BIN = str(_sub / "bin")
                break
        if _BUNDLED_FFMPEG_BIN:
            break
if _BUNDLED_FFMPEG_BIN:
    os.environ["PATH"] = _BUNDLED_FFMPEG_BIN + os.pathsep + os.environ.get("PATH", "")
    os.environ["PYGLET_FFMPEG_LOCATION"] = _BUNDLED_FFMPEG_BIN
    try:
        ctypes.windll.kernel32.SetDllDirectoryW(_BUNDLED_FFMPEG_BIN)
    except Exception:
        pass

import pyglet
from pyglet.window import key
import numpy as np

# ------------------------------------------------------------
# Config
# ------------------------------------------------------------
WINDOW_W, WINDOW_H = 1280, 720
CENTER = (WINDOW_W // 2, WINDOW_H // 2)
TARGET_RADIUS = 70
SPAWN_RADIUS = 520
TRAVEL_TIME = 1.6
HIT_WINDOW_PERFECT = 0.13
HIT_WINDOW_GOOD = 0.26
HIT_WINDOW_OK = 0.35

LANES = {
    'd': {'angle': 180, 'color': (255, 74, 74),  'key': key.D, 'label': 'D'},
    'f': {'angle': 90,  'color': (74, 144, 255), 'key': key.F, 'label': 'F'},
    'j': {'angle': 0,   'color': (74, 255, 138), 'key': key.J, 'label': 'J'},
    'k': {'angle': 270, 'color': (255, 215, 74), 'key': key.K, 'label': 'K'},
}
LANE_ORDER = ['d', 'f', 'j', 'k']
KEY_TO_LANE = {v['key']: k for k, v in LANES.items()}
CHAR_TO_LANE = {'d': 'd', 'f': 'f', 'j': 'j', 'k': 'k'}

SONGS_DIR = Path(__file__).resolve().parent / "songs"
SUPPORTED_EXTS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm"}
CACHE_DIR = SONGS_DIR / ".cache"
# predetermined cache for demo/example (so no wait)
PREDETERMINED = {}  # filled below after functions

# ------------------------------------------------------------
# Demo pattern
# ------------------------------------------------------------
def generate_demo_pattern(bpm=128, bars=16, duration=None):
    beat_interval = 60.0 / bpm
    pattern = []
    t = 0.0
    total_beats = bars * 4
    if duration:
        total_beats = int(duration / beat_interval) + 4
    for i in range(total_beats):
        bar = i // 4
        pos_in_bar = i % 4
        if bar % 4 == 0:
            lane = LANE_ORDER[pos_in_bar % 4]
            pattern.append((t, lane))
        elif bar % 4 == 1:
            lane = LANE_ORDER[(i) % 4]
            pattern.append((t, lane))
            if pos_in_bar in (1, 3):
                off = t + beat_interval * 0.5
                lane2 = LANE_ORDER[(i + 2) % 4]
                pattern.append((off, lane2))
        elif bar % 4 == 2:
            lane = LANE_ORDER[pos_in_bar % 2 * 2 + (bar % 2)]
            pattern.append((t, lane))
            if pos_in_bar % 2 == 0:
                pattern.append((t + beat_interval * 0.25, LANE_ORDER[(pos_in_bar+1)%4]))
                pattern.append((t + beat_interval * 0.5, LANE_ORDER[(pos_in_bar+2)%4]))
        else:
            lane = LANE_ORDER[(i * 3) % 4]
            pattern.append((t, lane))
        t += beat_interval
    for i in range(8):
        pattern.append((t + i * beat_interval * 0.5, LANE_ORDER[i % 4]))
    pattern = sorted(pattern, key=lambda x: x[0])
    filtered = []
    for tm, ln in pattern:
        if filtered and abs(tm - filtered[-1][0]) < 0.08 and ln == filtered[-1][1]:
            continue
        filtered.append((tm, ln))
    return filtered

# ------------------------------------------------------------
# Songs folder helpers
# ------------------------------------------------------------
def get_songs_in_folder(songs_dir=None):
    if songs_dir is None:
        songs_dir = SONGS_DIR
    try:
        songs_dir.mkdir(parents=True, exist_ok=True)
    except: pass
    files = []
    try:
        for p in songs_dir.iterdir():
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                files.append(p)
    except Exception as e:
        print(f"[songs] scan failed {e}")
    # sort by name case-insensitive
    files.sort(key=lambda x: x.name.lower())
    return files

# ------------------------------------------------------------
# Beatmap cache (predetermined for demo/example, progress bar)
# ------------------------------------------------------------
def get_cache_path(media_path):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except: pass
    # use stem + hash to avoid collisions
    stem = Path(media_path).stem
    h = hashlib.md5(str(Path(media_path).resolve()).encode()).hexdigest()[:8]
    return CACHE_DIR / f"{stem}_{h}.json"

def load_cached_beatmap(media_path, sensitivity=1.0):
    cp = get_cache_path(media_path)
    if not cp.exists():
        return None
    try:
        with open(cp, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # validate mtime and sensitivity
        src = Path(media_path)
        if not src.exists():
            return None
        cached_mtime = data.get('mtime', 0)
        if abs(cached_mtime - src.stat().st_mtime) > 1:
            return None
        if abs(data.get('sensitivity', 1.0) - sensitivity) > 0.01:
            return None
        bm = data.get('beatmap', [])
        duration = data.get('duration', 30.0)
        tempo = data.get('tempo', 120.0)
        # convert beatmap back to list of tuples
        beatmap = [(float(t), str(lane)) for t, lane in bm]
        return beatmap, float(duration), float(tempo)
    except Exception as e:
        print(f"[cache] load failed {e}")
        return None

def save_cached_beatmap(media_path, beatmap, duration, tempo, sensitivity=1.0):
    try:
        cp = get_cache_path(media_path)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            'beatmap': [[float(t), str(lane)] for t, lane in beatmap],
            'duration': float(duration),
            'tempo': float(tempo),
            'sensitivity': float(sensitivity),
            'mtime': Path(media_path).stat().st_mtime if Path(media_path).exists() else 0,
            'version': 2
        }
        with open(cp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        print(f"[cache] saved {cp} ({len(beatmap)} beats)")
    except Exception as e:
        print(f"[cache] save failed {e}")

# ------------------------------------------------------------
# Audio analysis via ffmpeg + numpy
# ------------------------------------------------------------
def extract_wav_with_ffmpeg(media_path, wav_path, sr=44100):
    cmd = [
        "ffmpeg", "-y",
        "-i", str(media_path),
        "-vn",
        "-ac", "1",
        "-ar", str(sr),
        "-acodec", "pcm_s16le",
        "-loglevel", "error",
        str(wav_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")
    return wav_path

def read_wav_mono(wav_path):
    with wave.open(str(wav_path), 'rb') as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
        if sampwidth == 2:
            dtype = np.int16
        elif sampwidth == 4:
            dtype = np.int32
        else:
            dtype = np.int16
        audio = np.frombuffer(raw, dtype=dtype).astype(np.float32)
        if n_channels > 1:
            audio = audio.reshape(-1, n_channels).mean(axis=1)
        if sampwidth == 2:
            audio /= 32768.0
        elif sampwidth == 4:
            audio /= 2147483648.0
        return framerate, audio

def detect_beats_energy(sr, audio, sensitivity=1.0, progress_cb=None):
    hop = 512
    n_frames = 1 + (len(audio) - 1024) // hop
    if n_frames <= 0:
        return []
    energies = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        start = i * hop
        chunk = audio[start:start+1024]
        energies[i] = np.sqrt(np.mean(chunk * chunk) + 1e-10)
        if progress_cb and i % 8000 == 0:
            progress_cb(0.45 + 0.25 * i / max(1, n_frames))
    kernel = np.ones(3)/3
    energies_smooth = np.convolve(energies, kernel, mode='same')
    win = int(0.6 * sr / hop)
    if win < 5:
        win = 5
    local_mean = np.convolve(energies_smooth, np.ones(win)/win, mode='same')
    local_std = np.zeros_like(local_mean)
    for i in range(len(energies_smooth)):
        lo = max(0, i - win//2)
        hi = min(len(energies_smooth), i + win//2)
        local_std[i] = np.std(energies_smooth[lo:hi])
    global_mean = np.mean(energies_smooth)
    base_factor = 1.55 - (sensitivity - 1.0) * 0.35
    base_factor = np.clip(base_factor, 1.15, 2.0)
    offset = np.clip(0.12 - (sensitivity-1.0)*0.04, 0.04, 0.18)
    threshold = local_mean * base_factor + local_std * 0.35 + offset * 0.1
    threshold = np.maximum(threshold, global_mean * (1.1 - (sensitivity-1.0)*0.15))
    min_dist_frames = int(0.18 * sr / hop)
    if min_dist_frames < 4:
        min_dist_frames = 4
    peaks = []
    last_peak = -min_dist_frames*2
    for i in range(2, len(energies_smooth)-2):
        if i - last_peak < min_dist_frames:
            continue
        e = energies_smooth[i]
        if e > threshold[i] and e >= energies_smooth[i-1] and e >= energies_smooth[i+1]:
            if e >= np.max(energies_smooth[i-2:i+3]):
                peaks.append(i)
                last_peak = i
    times = [p * hop / sr for p in peaks]
    return times

def onset_envelope_sfx(sr, audio, hop=512, n_fft=1024):
    """Onset strength envelope via spectral flux (log-compressed, half-wave rectified)."""
    audio = audio.astype(np.float32)
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak * 0.99
    n_frames = 1 + (len(audio) - n_fft) // hop
    if n_frames <= 4:
        return np.zeros(4)
    window = np.hanning(n_fft).astype(np.float32)
    n_freq = n_fft // 2 + 1
    spec = np.empty((n_frames, n_freq), dtype=np.float32)
    for i in range(n_frames):
        start = i * hop
        frame = audio[start:start + n_fft]
        if len(frame) < n_fft:
            frame = np.pad(frame, (0, n_fft - len(frame)))
        spec[i] = np.abs(np.fft.rfft(frame * window))
    spec = np.log1p(spec * 50.0)
    diff = spec[1:] - spec[:-1]
    diff[diff < 0] = 0
    flux = np.sum(diff, axis=1)
    flux = np.concatenate([[0.0], flux])
    k = 3
    kernel = np.ones(k) / k
    flux = np.convolve(flux, kernel, mode='same')
    return flux

def estimate_tempo_autocorr(sr, flux, hop=512, bpm_min=55, bpm_max=200):
    """Tempo via autocorrelation of onset envelope, preferring higher tempos (smallest strong lag)."""
    fps = sr / hop
    ac = np.correlate(flux, flux, mode='full')
    ac = ac[len(flux)-1:]
    if ac[0] > 0:
        ac = ac / (ac[0] + 1e-6)
    lag_min = int(fps * 60.0 / bpm_max)
    lag_max = int(fps * 60.0 / bpm_min)
    if lag_max + 1 > len(ac):
        lag_max = len(ac) // 2 - 1
    if lag_max <= lag_min:
        return 120.0, int(fps * 60.0 / 120.0)
    region = ac[lag_min:lag_max]
    thresh = 0.45 * np.max(region)
    best_lag = lag_min
    best_val = -1
    for lag in range(lag_min, min(lag_max, len(ac))):
        v = ac[lag]
        lo = max(lag_min, lag - 4)
        hi = min(lag_max, lag + 5)
        if v < np.max(ac[lo:hi]):
            continue
        if v > thresh and v > best_val:
            best_val = v
            best_lag = lag
    while best_lag // 2 >= lag_min:
        half = best_lag // 2
        if ac[half] >= 0.85 * best_val:
            best_lag = half
            best_val = ac[half]
        else:
            break
    bpm = 60.0 * fps / best_lag
    return bpm, best_lag

def dp_beat_track(flux, fps, bpm):
    """Ellis-style dynamic-programming beat tracking. Returns beat times in seconds."""
    n = len(flux)
    if n < 10:
        return []
    beat_interval = max(4.0, fps / (bpm / 60.0))
    dp_score = np.zeros(n)
    backlink = -np.ones(n, dtype=int)
    i0 = int(beat_interval)
    for s in range(i0):
        dp_score[s] = flux[s]
    for s in range(i0, n):
        lo = max(1, int(s - 2 * beat_interval))
        hi = max(lo + 2, int(s - beat_interval / 2))
        window = dp_score[lo:hi]
        k = int(np.argmax(window))
        best_p = lo + k
        expected = s - beat_interval
        penalty = abs(best_p - expected) / beat_interval
        penalty = 0.05 * penalty * penalty
        dp_score[s] = flux[s] + window[k] * (1.0 / (1.0 + penalty))
        backlink[s] = best_p
    tail_start = max(0, n - int(3 * beat_interval))
    end_idx = int(np.argmax(dp_score[tail_start:])) + tail_start
    beats = []
    cur = end_idx
    guard = 0
    while cur > 0 and backlink[cur] >= 0 and guard < n:
        guard += 1
        beats.append(cur)
        nxt = backlink[cur]
        if nxt >= cur:
            break
        cur = nxt
    if not beats:
        beats = [end_idx]
    beats.reverse()
    refined = []
    for b in beats:
        lo = max(1, b - 3)
        hi = min(n - 1, b + 3)
        window = flux[lo:hi + 1]
        b_local = int(np.argmax(window)) + lo
        if not refined or b_local - refined[-1] >= int(beat_interval * 0.25):
            refined.append(b_local)
    times = [r / fps for r in refined]
    return times

def detect_beats_sfx(sr, audio, sensitivity=1.0, progress_cb=None):
    """Improved beat detection: spectral flux onset envelope + autocorrelation tempo + DP tracking.
    Returns (beat_times, bpm)."""
    hop = 512
    def prog(v):
        if progress_cb:
            try: progress_cb(v)
            except: pass
    prog(0.05)
    flux = onset_envelope_sfx(sr, audio, hop=hop)
    prog(0.35)
    bpm, _ = estimate_tempo_autocorr(sr, flux, hop=hop)
    prog(0.5)
    beats = dp_beat_track(flux, sr / hop, bpm)
    prog(0.8)
    if len(beats) < 8:
        # fallback: strong peaks from flux
        peaks = np.argsort(flux)[::-1]
        min_dist = int(0.2 * sr / hop)
        sel = []
        last = -min_dist
        for p in peaks:
            if p - last >= min_dist:
                sel.append(p)
                last = p
            if len(sel) >= 100:
                break
        beats = sorted(s / (sr / hop) for s in sel)
    prog(1.0)
    return [float(t) for t in beats], float(bpm)

def detect_beats_madmom(sr, audio, sensitivity=1.0, progress_cb=None):
    """madmom RNN onset detection - sparse *main-beat* mode.
    Targets melody/voice (playable density ~1.0-1.6/s) instead of every
    16th.  Sensitivity still maps to density for future difficulty:
    0.5=very sparse, 1.0=normal (playable), 2.0=dense.
    Returns (beat_times, tempo)."""
    import numpy as _np
    from madmom.features.onsets import RNNOnsetProcessor, OnsetPeakPickingProcessor
    def prog(v):
        if progress_cb:
            try: progress_cb(v)
            except: pass
    s = max(0.2, min(2.0, float(sensitivity)))
    # sparse main-beat params: higher threshold + larger combine + larger min_gap
    # at s=1.0 -> th~0.55, comb~0.28, mg~0.38  => ~1.3/s (tested)
    # at s=2.0 -> th~0.30, comb~0.10, mg~0.18 => ~3-4/s (dense for hard)
    threshold = float(0.80 - 0.25 * s)          # 0.55 @1.0, 0.30 @2.0
    threshold = max(0.28, min(0.75, threshold))
    combine = float(0.40 - 0.12 * s)           # 0.28 @1.0, 0.16 @2.0
    combine = max(0.05, min(0.40, combine))
    min_gap = float(0.52 - 0.14 * s)           # 0.38 @1.0, 0.24 @2.0
    min_gap = max(0.14, min(0.50, min_gap))
    prog(0.1)
    acts = RNNOnsetProcessor()(audio.astype(_np.float32, copy=False), sample_rate=sr)
    prog(0.8)
    peak = OnsetPeakPickingProcessor(threshold=threshold, combine=combine, fps=100)
    beats = [float(t) for t in peak(acts)]
    kept = []
    for t in beats:
        if not kept or t - kept[-1] >= min_gap:
            kept.append(t)
    beats = kept
    # estimate musical tempo via spectral-flux autocorrelation (not note density)
    bpm = 120.0
    try:
        _flux = onset_envelope_sfx(sr, audio, hop=512)
        bpm, _ = estimate_tempo_autocorr(sr, _flux, hop=512)
        if not (30 < bpm < 240):
            bpm = 120.0
    except Exception:
        bpm = 120.0
    prog(1.0)
    return beats, float(bpm)

def _centroids_for_times(times, sr, audio):
    """Spectral centroid per onset (pitch proxy). Returns list[float]."""
    if sr is None or audio is None or not times:
        return [0.0]*len(times)
    cents = []
    n = len(audio)
    for t in times:
        c = int(t*sr)
        half = 1024
        lo = max(0, c-half)
        hi = min(n, c+half)
        win = audio[lo:hi]
        if len(win) < 256:
            cents.append(0.0)
            continue
        # hann + zero-pad to 2048 for stable freq bins
        w = np.hanning(len(win)).astype(np.float32)
        win = (win * w).astype(np.float32)
        N = 2048
        if len(win) < N:
            win = np.pad(win, (0, N-len(win)))
        else:
            win = win[:N]
        spec = np.abs(np.fft.rfft(win))
        freqs = np.fft.rfftfreq(N, 1.0/sr)
        # log-compress like flux so centroid isn't dominated by loud bass
        spec = np.log1p(spec*30.0)
        s = np.sum(spec)
        if s < 1e-6:
            cents.append(0.0)
        else:
            cents.append(float(np.sum(freqs*spec)/s))
    return cents

def beatmap_from_times(times, duration, sr=None, audio=None):
    """Build lane-assigned beatmap: pitch-aware + ergonomic.
    - Spectral centroid (pitch) maps low->D, high->K so lane reflects melody.
    - Quantile ranking ensures balanced lane use but preserves pitch order.
    - Ergonomic: avoid same lane <0.35s, avoid same-hand repeats for close notes,
      prefer opposite side for consecutive notes when pitch is ambiguous.
    - For future difficulty: twin notes only on strong gaps (not for normal).
    """
    if not times:
        return []
    times = sorted(float(t) for t in times)
    # pure fallback (no audio) -> ergonomic cycling
    if sr is None or audio is None:
        beatmap = []
        last_used = {l: -999 for l in LANE_ORDER}
        prev = None
        for t in times:
            # pick least-recently-used lane (round-robin with ergonomic gap)
            cands = sorted(LANE_ORDER, key=lambda l: last_used[l])
            chosen = cands[0]
            # if same as prev and very close, pick next
            if chosen == prev and cands[1] and t - last_used[chosen] < 0.45:
                chosen = cands[1]
            beatmap.append((float(t), chosen))
            last_used[chosen] = t
            prev = chosen
        return sorted(beatmap, key=lambda x: x[0])

    cents = _centroids_for_times(times, sr, audio)
    # rank 0..1 (quantile) preserves pitch order while balancing lanes
    order = np.argsort(cents)
    rank = [0.0]*len(times)
    for r, idx in enumerate(order):
        rank[idx] = r / max(1, len(times)-1)

    beatmap = []
    last_used = {l: -999.0 for l in LANE_ORDER}
    prev = None
    for i, t in enumerate(times):
        pref_idx = int(rank[i]*4)
        if pref_idx > 3: pref_idx = 3
        # score each lane: pitch distance + ergonomic penalty
        best_lane = None
        best_score = 1e9
        for ci, lane in enumerate(LANE_ORDER):
            pitch_cost = abs(ci - pref_idx) * 1.0  # 0..3
            # ergonomic: penalize recently used lane, strongly if same as prev
            recency = t - last_used[lane]
            repeat_cost = 0.0
            if recency < 0.40:
                repeat_cost += (0.40 - recency) * 6.0  # up to 2.4
            if lane == prev and i>0 and t - times[i-1] < 0.55:
                repeat_cost += 1.8  # discourage immediate repeat on close notes
            # slight tie-breaker: prefer least-recently-used among equals
            lru_bonus = - (recency * 0.02)  # older lane slightly preferred
            score = pitch_cost + repeat_cost + lru_bonus
            if score < best_score:
                best_score = score
                best_lane = lane
        beatmap.append((float(t), best_lane))
        last_used[best_lane] = t
        prev = best_lane
    return sorted(beatmap, key=lambda x: x[0])

def beats_from_media(media_path, sensitivity=1.0, use_librosa=True, progress_cb=None):
    def prog(v):
        if progress_cb:
            try:
                progress_cb(max(0.0, min(1.0, v)))
            except:
                pass
    prog(0.05)
    if use_librosa:
        try:
            import librosa
            prog(0.1)
            # for video, extract wav first for better librosa support (mp4 often fails direct)
            temp_wav = None
            try:
                if Path(media_path).suffix.lower() in {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm"}:
                    temp_wav = tempfile.NamedTemporaryFile(delete=False, suffix='.wav').name
                    extract_wav_with_ffmpeg(media_path, temp_wav, sr=22050)
                    y, sr = librosa.load(temp_wav, sr=22050, mono=True)
                else:
                    y, sr = librosa.load(str(media_path), sr=22050, mono=True)
            finally:
                if temp_wav and os.path.exists(temp_wav):
                    try: os.unlink(temp_wav)
                    except: pass
            prog(0.35)
            tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units='time')
            prog(0.55)
            if len(beat_frames) < 20:
                onset_env = librosa.onset.onset_strength(y=y, sr=sr)
                onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, units='time')
                beat_frames = sorted(set(list(beat_frames) + list(onsets)))
            prog(0.75)
            filtered = []
            for t in sorted(beat_frames):
                if not filtered or t - filtered[-1] > 0.14:
                    filtered.append(float(t))
            duration = librosa.get_duration(y=y, sr=sr)
            lane_pattern = []
            for idx, t in enumerate(filtered):
                if idx % 8 < 2:
                    lane = LANE_ORDER[idx % 4]
                elif idx % 4 == 2:
                    lane = LANE_ORDER[(idx*2) % 4]
                else:
                    lane = LANE_ORDER[idx % 4]
                lane_pattern.append((float(t), lane))
            prog(1.0)
            lane_pattern = fill_beat_gaps(lane_pattern, float(duration), float(tempo) if hasattr(tempo, '__float__') else 120.0)
            return lane_pattern, float(duration), float(tempo) if hasattr(tempo, '__float__') else 120.0
        except ImportError:
            pass
        except Exception as e:
            print(f"[librosa] failed {e}, falling back to numpy method")
    try:
        prog(0.12)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav_path = tf.name
        sr = 44100
        prog(0.18)
        extract_wav_with_ffmpeg(media_path, wav_path, sr=sr)
        prog(0.35)
        sr_read, audio = read_wav_mono(wav_path)
        try:
            os.unlink(wav_path)
        except: pass
        duration = len(audio) / sr_read
        # madmom preferred when installed (most accurate beat tracking)
        madmom_times = None
        try:
            import madmom
            prog(0.4)
            madmom_times, _bpm = detect_beats_madmom(sr_read, audio, sensitivity=sensitivity,
                progress_cb=lambda p: prog(0.4 + 0.32*p))
            if len(madmom_times) < 8:
                madmom_times = None
        except Exception as e:
            print(f"[madmom] unavailable ({e}), using numpy detector")
            madmom_times = None
        if madmom_times is not None:
            times = madmom_times
            detected_bpm = _bpm
        else:
            prog(0.42)
            times, detected_bpm = detect_beats_sfx(sr_read, audio, sensitivity=sensitivity, progress_cb=lambda p: prog(0.42 + 0.4*p))
        if len(times) < 8:
            print("[detect] too few onsets, generating BPM grid")
            bpm = 128
            beat_interval = 60.0 / bpm
            times = [i * beat_interval for i in range(int(duration / beat_interval))]
        is_main = madmom_times is not None
        if is_main:
            beatmap = beatmap_from_times(times, duration, sr=sr_read, audio=audio)
            beatmap = fill_beat_gaps(beatmap, float(duration), detected_bpm, sparse=True)
        else:
            beatmap = beatmap_from_times(times, duration)
            beatmap = fill_beat_gaps(beatmap, float(duration), detected_bpm)
        prog(0.92)
        # also handle librosa return prog
        prog(1.0)
        return beatmap, float(duration), detected_bpm
    except Exception as e:
        print(f"[ffmpeg/numpy] beat detection failed: {e}")
        try:
            import pyglet.media
            src = pyglet.media.load(str(media_path))
            duration = float(src.duration) if src.duration else 30.0
        except:
            duration = 30.0
        bpm = 120
        interval = 60.0/bpm
        beatmap = [(i*interval, LANE_ORDER[i%4]) for i in range(int(duration/interval))]
        return beatmap, duration, bpm

def fill_beat_gaps(beatmap, duration, tempo_hint=120, sparse=False):
    """Fill large gaps with interpolated beats. For sparse (main-beat) mode
    gaps are left alone so rests stay rests - only extreme >2.5x gaps are
    lightly filled, and no 0.5s grid fallback."""
    if not beatmap:
        return beatmap
    import numpy as np
    times = [t for t,_ in beatmap]
    if len(times) < 2:
        return beatmap
    intervals = [times[i+1]-times[i] for i in range(len(times)-1)]
    try:
        avg = float(np.median(intervals))
    except:
        avg = 60.0/tempo_hint if tempo_hint>0 else 0.5
    if avg < 0.2: avg = 0.4
    if avg > 1.0: avg = 0.6
    if sparse:
        # main-beat: do not densify; keep rests. Only dedup.
        dedup = []
        for t,l in sorted(beatmap, key=lambda x: x[0]):
            if dedup and abs(t-dedup[-1][0]) < 0.09:
                continue
            dedup.append((t,l))
        return dedup
    filled = []
    for i in range(len(beatmap)):
        filled.append(beatmap[i])
        if i+1 < len(beatmap):
            gap = beatmap[i+1][0] - beatmap[i][0]
            if gap > avg*1.7 and gap < 4.0:
                n_fill = int(round(gap/avg)) - 1
                if n_fill > 0 and n_fill < 8:
                    for k in range(1, n_fill+1):
                        t = beatmap[i][0] + avg*k
                        if t < beatmap[i+1][0] - 0.12:
                            lane = LANE_ORDER[(i+k) % 4]
                            filled.append((t, lane))
        elif beatmap[i][0] < duration - avg:
            gap = duration - beatmap[i][0]
            if gap > avg*1.7:
                n_fill = int(round(gap/avg)) - 1
                for k in range(1, min(n_fill+1, 6)):
                    t = beatmap[i][0] + avg*k
                    if t < duration - 0.1:
                        lane = LANE_ORDER[(i+k) % 4]
                        filled.append((t, lane))
    filled = sorted(filled, key=lambda x: x[0])
    dedup = []
    for t,l in filled:
        if dedup and abs(t-dedup[-1][0]) < 0.09:
            continue
        dedup.append((t,l))
    if len(dedup) < duration*0.8:
        grid = []
        for i in range(int(duration*2)):
            t = i*0.5
            if not any(abs(t-existing[0]) < 0.22 for existing in dedup):
                grid.append((t, LANE_ORDER[i%4]))
        dedup = sorted(dedup+grid, key=lambda x: x[0])
    return dedup

# ------------------------------------------------------------
# Game Window
# ------------------------------------------------------------
class RhythmGame(pyglet.window.Window):
    def __init__(self):
        super().__init__(width=WINDOW_W, height=WINDOW_H, caption="Radial Rhythm - Pyglet  |  Main Menu", resizable=False, vsync=True)
        self.batch = pyglet.graphics.Batch()
        # Batch for shapes to reduce draw calls
        self.shape_batch = pyglet.graphics.Batch()
        pyglet.gl.glClearColor(10/255, 10/255, 18/255, 1.0)
        # cap max fps display
        self._fps_display = pyglet.window.FPSDisplay(self)
        self._fps_display.label.color = (120,120,130,180)
        self.is_fullscreen = False

        # game state
        self.state = "menu"  # menu / song_select / playing / paused / results
        self.menu_index = 0
        self.menu_options = ["PLAY DEMO", "SONGS", "QUIT"]

        self.song_files = get_songs_in_folder()
        self.song_index = 0
        self.songs_scroll = 0

        self.beatmap = []
        self.duration = 30.0
        self.active_beats = []
        self.next_index = 0
        self.start_time = None
        self.is_playing = False
        self.is_media_mode = False
        self.media_player = None
        self.media_source = None
        self.media_path = None
        self.sensitivity = 1.0
        self.beat_offset = 0.0  # manual sync offset (seconds) - ,/. to adjust

        # scoring
        self.score = 0
        self.combo = 0
        self.max_combo = 0
        self.hits = {'perfect': 0, 'good': 0, 'ok': 0, 'miss': 0}
        self.feedback_text = ""
        self.feedback_time = 0
        self.feedback_color = (255,255,255,255)
        self.hit_pulse = 0.0
        self.lane_flash = {lane: 0.0 for lane in LANE_ORDER}

        # preload demo so menu can show beat count
        self.demo_beatmap = generate_demo_pattern(bpm=128, bars=16)
        self.beatmap = self.demo_beatmap
        self.duration = self.demo_beatmap[-1][0] + 2.0 if self.demo_beatmap else 30.0

        # label cache for performance (reuse Label objects to avoid glyph rebuild)
        self._label_cache = {}
        self._last_key_hit = 0
        self._last_refresh = 0
        self.show_fps = False
        # for vsync / 60fps
        pyglet.clock.schedule_interval(self.update, 1/60)

        # ---- pooled shapes for 60fps (reuse + batch) ----
        self.game_batch = pyglet.graphics.Batch()
        self._lane_line_shapes = {}
        self._lane_outer_bg_shapes = {}
        self._lane_outer_shapes = {}
        cx, cy = CENTER
        for lane_key, info in LANES.items():
            ang = math.radians(info['angle'])
            x1 = cx + math.cos(ang) * TARGET_RADIUS
            y1 = cy + math.sin(ang) * TARGET_RADIUS
            x2 = cx + math.cos(ang) * SPAWN_RADIUS
            y2 = cy + math.sin(ang) * SPAWN_RADIUS
            col = info['color']
            self._lane_line_shapes[lane_key] = pyglet.shapes.Line(x1, y1, x2, y2, thickness=2, color=(*col, 60), batch=self.game_batch)
            sx = cx + math.cos(ang) * SPAWN_RADIUS
            sy = cy + math.sin(ang) * SPAWN_RADIUS
            self._lane_outer_bg_shapes[lane_key] = pyglet.shapes.Circle(sx, sy, 14, color=(*col, 90), batch=self.game_batch)
            self._lane_outer_shapes[lane_key] = pyglet.shapes.Circle(sx, sy, 10, color=col, batch=self.game_batch)
        # center target pooled (batched) - hide non-essential for 60fps
        self._center_shadow1 = pyglet.shapes.Circle(cx, cy, TARGET_RADIUS+18, color=(30,30,45), batch=self.game_batch)
        self._center_shadow1.opacity = 90
        self._center_shadow1.visible = False
        self._center_shadow2 = pyglet.shapes.Circle(cx, cy, TARGET_RADIUS+8, color=(50,50,75), batch=self.game_batch)
        self._center_shadow2.opacity = 90
        self._center_shadow2.visible = False
        self._center_outer = pyglet.shapes.Circle(cx, cy, TARGET_RADIUS, color=(255,255,255), batch=self.game_batch)
        self._center_outer.opacity = 30
        self._center_main = pyglet.shapes.Circle(cx, cy, TARGET_RADIUS, color=(22,22,34), batch=self.game_batch)
        self._center_inner = pyglet.shapes.Circle(cx, cy, TARGET_RADIUS-6, color=(40,40,60), batch=self.game_batch)
        self._center_inner.opacity = 200
        self._center_inner.visible = False
        self._center_dot = pyglet.shapes.Circle(cx, cy, 8, color=(255,255,255), batch=self.game_batch)
        self._center_dot.opacity = 180
        self._center_lane_dots = {}
        self._center_lane_glows = {}
        for lane_key, info in LANES.items():
            col = info['color']
            self._center_lane_dots[lane_key] = pyglet.shapes.Circle(cx, cy, 16, color=col, batch=self.game_batch)
            self._center_lane_glows[lane_key] = pyglet.shapes.Circle(cx, cy, 28, color=col, batch=self.game_batch)
            self._center_lane_glows[lane_key].opacity = 0
            self._center_lane_glows[lane_key].visible = False
        # beat pool: 36 beats max visible (batched) - use visible False to skip batch draw
        self._beat_pool = []
        for _ in range(36):
            circ = pyglet.shapes.Circle(0, 0, 22, color=(255,255,255), batch=self.game_batch)
            circ.visible = False
            inner = pyglet.shapes.Circle(0, 0, 12, color=(255,255,255), batch=self.game_batch)
            inner.visible = False
            tail = pyglet.shapes.Line(0, 0, 0, 0, thickness=8, color=(255,255,255,90), batch=self.game_batch)
            tail.visible = False
            hit_circ = pyglet.shapes.Circle(0, 0, 28, color=(255,255,255), batch=self.game_batch)
            hit_circ.visible = False
            self._beat_pool.append({'circle': circ, 'inner': inner, 'tail': tail, 'hit': hit_circ, 'in_use': False})

        # ---- persistent labels for 60fps (avoid _draw_label cache lookup per frame) ----
        self._lane_text_labels = {}
        for lane_key, info in LANES.items():
            ang = math.radians(info['angle'])
            sx = cx + math.cos(ang) * SPAWN_RADIUS
            sy = cy + math.sin(ang) * SPAWN_RADIUS
            lbl = pyglet.text.Label(info['label'], x=sx, y=sy, font_name='Arial', font_size=11, weight='bold', color=(255,255,255,255), anchor_x='center', anchor_y='center')
            self._lane_text_labels[lane_key] = lbl
        # HUD persistent labels
        self._hud_score_lbl = pyglet.text.Label("", x=16, y=WINDOW_H-16, font_name='Arial', font_size=14, weight='bold', color=(240,240,255,255), anchor_x='left', anchor_y='top')
        self._hud_hits_lbl = pyglet.text.Label("", x=16, y=WINDOW_H-33, font_name='Consolas', font_size=11, color=(180,180,200,255), anchor_x='left', anchor_y='top')
        self._hud_time_lbl = pyglet.text.Label("", x=WINDOW_W-12, y=WINDOW_H-18, font_name='Consolas', font_size=10, color=(180,220,255,255), anchor_x='right', anchor_y='top')
        self._hud_mode_lbl = pyglet.text.Label("", x=WINDOW_W-12, y=WINDOW_H-32, font_name='Consolas', font_size=10, color=(150,170,200,255), anchor_x='right', anchor_y='top')
        self._hud_feedback_lbl = pyglet.text.Label("", x=CENTER[0], y=CENTER[1]+110, font_name='Arial', font_size=24, weight='bold', color=(255,255,255,255), anchor_x='center', anchor_y='center')
        self._hud_instr_lbl = pyglet.text.Label("", x=WINDOW_W//2, y=18, font_name='Consolas', font_size=9, color=(130,130,160,255), anchor_x='center', anchor_y='center')
        # HUD shapes (reuse)
        self._hud_top_bar = pyglet.shapes.Rectangle(0, WINDOW_H-46, WINDOW_W, 46, color=(18,18,30))
        self._hud_top_bar.opacity = 220
        self._hud_prog_bg = pyglet.shapes.Rectangle(0, 6, WINDOW_W, 4, color=(40,40,50))
        self._hud_prog_fg = pyglet.shapes.Rectangle(0, 6, 0, 4, color=(100,255,160))

        # ---- persistent menu shapes for 60fps ----
        self._menu_batch = pyglet.graphics.Batch()
        self._menu_bg_rects = []
        self._menu_border_rects = []
        self._menu_accent_rects = []
        for idx in range(3):
            y = 360 - idx*60
            x = WINDOW_W//2
            w, h = 420, 44
            bg = pyglet.shapes.Rectangle(x - w//2, y - h//2, w, h, color=(28,28,42))
            border = pyglet.shapes.Rectangle(x - w//2 -1, y - h//2 -1, w+2, h+2, color=(120,180,255))
            accent = pyglet.shapes.Rectangle(x - w//2, y - h//2, 6, h, color=(100,255,160))
            border.visible = False
            accent.visible = False
            self._menu_bg_rects.append(bg)
            self._menu_border_rects.append(border)
            self._menu_accent_rects.append(accent)
        self._menu_lane_circles = []
        for i, lane in enumerate(LANE_ORDER):
            col = LANES[lane]['color']
            xs = WINDOW_W//2 - 160 + i*90
            c = pyglet.shapes.Circle(xs+16, 30, 10, color=col)
            self._menu_lane_circles.append(c)
        # persistent menu labels (3 options + static texts)
        self._menu_title_lbl = pyglet.text.Label("RADIAL RHYTHM", x=WINDOW_W//2, y=WINDOW_H - 120, font_name='Arial', font_size=40, weight='bold', color=(255,255,255,255), anchor_x='center', anchor_y='center')
        self._menu_sub_lbl = pyglet.text.Label("beats converge to the centre  •  D  F  J  K", x=WINDOW_W//2, y=WINDOW_H - 155, font_name='Consolas', font_size=11, color=(140,200,255,255), anchor_x='center', anchor_y='center')
        self._menu_songs_hint_lbl = pyglet.text.Label("", x=WINDOW_W//2, y=WINDOW_H - 180, font_name='Consolas', font_size=9, color=(130,140,160,255), anchor_x='center', anchor_y='center')
        self._menu_option_lbls = []
        for opt in self.menu_options:
            lbl = pyglet.text.Label(opt, x=WINDOW_W//2, y=0, font_name='Arial', font_size=16, weight='bold', color=(220,220,240,255), anchor_x='center', anchor_y='center')
            self._menu_option_lbls.append(lbl)
        self._menu_footer_lbl = pyglet.text.Label("UP/DOWN or W/S • ENTER/SPACE to select • O open external file • ESC quit", x=WINDOW_W//2, y=70, font_name='Consolas', font_size=9, color=(110,120,150,255), anchor_x='center', anchor_y='center')
        self._menu_lane_lbls = []
        for lane in LANE_ORDER:
            lbl = pyglet.text.Label(lane.upper(), x=0, y=30, font_name='Consolas', font_size=9, color=(200,200,220,255), anchor_x='left', anchor_y='center')
            self._menu_lane_lbls.append(lbl)
        # song select persistent
        self._song_panel = pyglet.shapes.Rectangle(200, 100, 880, 460, color=(18,18,30))
        # video background sprite (for MP4)
        self._video_sprite = None
        self._av_container = None
        self._av_last_frame_image = None
        self._temp_audio_wav = None
        self.analysis_progress = 0.0
        self.analysis_msg = ""
        self._analysis_done = False
        self._analysis_result = None
        self._analysis_error = None
        self._analysis_path = None
        self._analysis_autoplay = False

    # ---------- helpers ----------
    def refresh_song_list(self):
        self.song_files = get_songs_in_folder()
        if self.song_index >= len(self.song_files):
            self.song_index = max(0, len(self.song_files)-1)
        self.songs_scroll = 0

    def load_demo(self):
        self.beatmap = self.demo_beatmap
        self.duration = self.beatmap[-1][0] + 2.0 if self.beatmap else 30.0
        self.is_media_mode = False
        if self.media_player:
            try: self.media_player.pause()
            except: pass
        self.reset_play_state()
        self.feedback_text = "DEMO READY"
        self.feedback_color = (120, 220, 255, 255)

    def reset_play_state(self):
        self.active_beats.clear()
        self.next_index = 0
        self.score = 0
        self.combo = 0
        self.max_combo = 0
        self.hits = {'perfect': 0, 'good': 0, 'ok': 0, 'miss': 0}
        self.start_time = None
        self.is_playing = False
        self.hit_pulse = 0
        for k in self.lane_flash:
            self.lane_flash[k] = 0

    def start_demo(self):
        self.load_demo()
        self.reset_play_state()
        self.start_time = time.time()
        self.is_playing = True
        self.is_media_mode = False
        self.state = "playing"
        self.feedback_text = "GO!"
        self.feedback_color = (74, 255, 138, 255)
        self.feedback_time = time.time()
        self.set_caption("Radial Rhythm - DEMO")

    def open_media_dialog(self):
        path = None
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            p = filedialog.askopenfilename(
                title="Select MP4 / Video / Audio",
                filetypes=[("Media", "*.mp4 *.m4v *.mov *.avi *.mkv *.mp3 *.wav *.m4a *.ogg *.flac"), ("All", "*.*")]
            )
            root.destroy()
            if p:
                path = p
        except Exception as e:
            print(f"tk dialog failed {e}")
        if not path:
            print("Enter path to mp4/media file (or drag & drop):")
            try:
                path = input("> ").strip().strip('"').strip("'")
                if not path or not os.path.exists(path):
                    self.feedback_text = "No file selected"
                    self.feedback_color = (255, 100, 100, 255)
                    self.feedback_time = time.time()
                    return
            except:
                return
        self.load_media(path)

    def _prepare_media_player(self, path):
        # (re)create pyglet player for video/audio playback
        # Cleanup previous ffmpeg source / av video objects
        if getattr(self, '_av_container', None):
            try:
                self._av_container.close()
            except: pass
            self._av_container = None
            self._av_last_frame_image = None
        if getattr(self, '_temp_audio_wav', None) and os.path.exists(self._temp_audio_wav):
            try:
                os.unlink(self._temp_audio_wav)
            except: pass
            self._temp_audio_wav = None
        try:
            if self.media_player:
                try: self.media_player.delete()
                except: pass
                self.media_player = None
            self.media_source = pyglet.media.load(str(path), streaming=True)
            self.media_player = pyglet.media.Player()
            self.media_player.queue(self.media_source)
            # reset video sprite so it picks up new texture
            self._video_sprite = None
            has_video = bool(getattr(self.media_source, 'video_format', None))
            print(f"[media] loaded duration {self.media_source.duration} (has video: {has_video})")
            return True
        except Exception as e:
            print(f"[media] pyglet load failed: {e}")
            self.media_source = None
            self.media_player = None
            return False

    def _on_analysis_done(self, path, beatmap, duration, tempo, error=None, autoplay=False):
        # called from main thread via clock
        if error:
            self.state = "song_select" if self.song_files else "menu"
            self.feedback_text = f"Analysis failed: {error}"
            self.feedback_color = (255, 80, 80, 255)
            self.feedback_time = time.time()
            self.analysis_progress = 0
            return
        self.beatmap = beatmap
        self.duration = duration
        self.media_path = path
        # also prepare player
        self._prepare_media_player(path)
        self.reset_play_state()
        self.analysis_progress = 1.0
        # save cache for next time (predetermined)
        try:
            save_cached_beatmap(path, beatmap, duration, tempo, self.sensitivity)
        except: pass
        self.feedback_text = f"Ready: {len(beatmap)} beats | ENTER to play | tempo ~{int(tempo)}"
        self.feedback_color = (100, 255, 150, 255)
        self.feedback_time = time.time()
        self.is_media_mode = False
        # if autoplay (from song select), start immediately - but only if still analyzing (not cancelled)
        if autoplay and self.state == "analyzing":
            self.start_media()
        elif self.state == "analyzing":
            # stay in song_select/menu but show ready
            self.state = "song_select" if self.song_files else "menu"

    def _analysis_thread_func(self, path, sensitivity, autoplay):
        # runs in background thread - set flag, main thread picks up in update()
        try:
            def prog_cb(p):
                self.analysis_progress = max(0.0, min(1.0, p))
                self.analysis_msg = f"Analysing {Path(path).name} {int(p*100)}%"
            beatmap, duration, tempo = beats_from_media(path, sensitivity=sensitivity, use_librosa=False, progress_cb=prog_cb)
            self._analysis_result = (beatmap, duration, tempo)
            self._analysis_error = None
            self._analysis_autoplay = autoplay
            self._analysis_done = True
            self._analysis_path = path
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._analysis_error = str(e)
            self._analysis_done = True
            self._analysis_path = path
            self._analysis_autoplay = False

    def _av_thread_func(self):
        # (removed - pyglet FFmpeg provides synced video via player.texture)
        pass

    def _start_av_thread(self):
        pass

    def _stop_av_thread(self):
        pass

    def load_media(self, path, autoplay=False):
        if not os.path.exists(path):
            self.feedback_text = f"File not found: {path}"
            self.feedback_color = (255, 80, 80, 255)
            self.feedback_time = time.time()
            return
        # check cache first (predetermined for demo/example)
        # special predetermined for _example_beats.wav - if no cache, create one instantly
        p = Path(path)
        if p.name == "_example_beats.wav" and not get_cache_path(path).exists():
            # predetermined: use known click track at ~128bpm (the file we generated)
            # we know its duration ~20s and beats every 0.468s, just use generate pattern
            try:
                # try to get duration via pyglet or assume 20
                dur = 20.0
                try:
                    import wave
                    with wave.open(str(path), 'rb') as wf:
                        dur = wf.getnframes() / wf.getframerate()
                except:
                    pass
                # generate a 128bpm grid that matches the click track for instant
                bpm = 128
                interval = 60.0 / bpm
                # use actual detection for this file is 44 beats, but we can mimic
                # for predetermined, use simple 4-lane cycle
                beatmap = [(i*interval, LANE_ORDER[i%4]) for i in range(int(dur/interval))]
                # save as cache so next time is also instant
                save_cached_beatmap(path, beatmap, dur, bpm, self.sensitivity)
                print(f"[predetermined] _example_beats.wav -> {len(beatmap)} beats (instant)")
            except Exception as e:
                print(f"[predetermined] failed {e}")

        cached = load_cached_beatmap(path, sensitivity=self.sensitivity)
        if cached:
            beatmap, duration, tempo = cached
            print(f"[cache] hit {Path(path).name} -> {len(beatmap)} beats (instant)")
            self.beatmap = beatmap
            self.duration = duration
            self.media_path = path
            self._prepare_media_player(path)
            self.reset_play_state()
            self.feedback_text = f"Ready (cached): {len(beatmap)} beats | ENTER to play"
            self.feedback_color = (100, 255, 150, 255)
            self.feedback_time = time.time()
            self.is_media_mode = False
            self.analysis_progress = 1.0
            if autoplay:
                self.start_media()
            return

        # not cached -> need analysis with progress bar
        self.media_path = path
        self.analysis_progress = 0.0
        self.analysis_msg = f"Analysing {Path(path).name} ..."
        self.feedback_text = self.analysis_msg
        self.feedback_color = (255, 220, 100, 255)
        self.feedback_time = time.time()
        self.state = "analyzing"
        self._analysis_done = False
        self._analysis_result = None
        self._analysis_error = None
        self._analysis_path = path
        self._analysis_autoplay = autoplay
        self._pending_autoplay = autoplay
        print(f"[load] analysing {path} sensitivity={self.sensitivity} (threaded)")
        # start thread
        t = threading.Thread(target=self._analysis_thread_func, args=(path, self.sensitivity, autoplay), daemon=True)
        t.start()

    def start_media(self):
        if not self.beatmap or not self.media_path:
            self.feedback_text = "No media loaded - O to open"
            self.feedback_color = (255,200,100,255)
            self.feedback_time = time.time()
            return
        self.reset_play_state()
        self.is_media_mode = True
        self.is_playing = True
        self.state = "playing"
        self.start_time = time.time()
        if self.media_player:
            try:
                self.media_player.seek(0)
                self.media_player.play()
                self.start_time = time.time()
            except Exception as e:
                print(f"player play failed {e}")
        # (video background handled by player.texture in on_draw - FFmpeg syncs it to audio)
        self.feedback_text = "PLAYING"
        self.feedback_color = (74, 255, 138, 255)
        self.feedback_time = time.time()
        self.set_caption(f"Radial Rhythm - {Path(self.media_path).name}")

    def get_song_time(self):
        if not self.is_playing or self.start_time is None:
            return 0.0
        if self.is_media_mode and self.media_player:
            try:
                pt = self.media_player.time
                if pt is not None and pt > 0.05:
                    return float(pt)
            except:
                pass
        return time.time() - self.start_time

    def spawn_beats(self, song_t):
        while self.next_index < len(self.beatmap):
            bt, lane = self.beatmap[self.next_index]
            bt_eff = bt + self.beat_offset
            if bt_eff - song_t <= TRAVEL_TIME + 0.05:
                if bt_eff >= song_t - HIT_WINDOW_OK:
                    ang = LANES[lane]['angle']
                    self.active_beats.append({
                        'time': bt_eff,
                        'lane': lane,
                        'angle': ang,
                        'hit': False,
                        'spawn_t': song_t,
                    })
                self.next_index += 1
            else:
                break

    def update(self, dt):
        if self.state not in ("playing",):
            for k in self.lane_flash:
                if self.lane_flash[k] > 0:
                    self.lane_flash[k] = max(0, self.lane_flash[k] - dt*3)
            if self.hit_pulse > 0:
                self.hit_pulse = max(0, self.hit_pulse - dt*4)
            # handle async analysis completion
            if self.state == "analyzing" and getattr(self, '_analysis_done', False):
                # grab result on main thread
                self._analysis_done = False
                if self._analysis_error:
                    self._on_analysis_done(self._analysis_path, [], 30.0, 120.0, error=self._analysis_error, autoplay=False)
                else:
                    bm, dur, tempo = self._analysis_result if self._analysis_result else ([], 30.0, 120.0)
                    self._on_analysis_done(self._analysis_path, bm, dur, tempo, error=None, autoplay=self._analysis_autoplay)
                # clear
                self._analysis_result = None
                self._analysis_error = None
                return
            # keep song count fresh in menu/song_select (without IO every frame)
            if self.state in ("menu", "song_select"):
                if not hasattr(self, '_last_refresh') or time.time() - self._last_refresh > 1.5:
                    self.song_files = get_songs_in_folder()
                    self._last_refresh = time.time()
            return
        if not self.is_playing:
            return
        song_t = self.get_song_time()
        self.spawn_beats(song_t)
        still_active = []
        for b in self.active_beats:
            delta = song_t - b['time']
            # if already in miss fade, keep fading for 0.45s
            if b.get('missed'):
                if song_t - b['miss_time'] < 0.45:
                    still_active.append(b)
                continue
            if not b['hit'] and delta > HIT_WINDOW_OK:
                self.hits['miss'] += 1
                self.combo = 0
                # don't overwrite a recent hit's feedback (e.g., PERFECT) immediately
                if time.time() - self.feedback_time > 0.35 or self.feedback_text == "MISS":
                    self.feedback_text = "MISS"
                    self.feedback_color = (255, 80, 80, 255)
                    self.feedback_time = time.time()
                self.lane_flash[b['lane']] = 1.0
                b['missed'] = True
                b['miss_time'] = song_t
                # keep for fade-out instead of instant remove
                still_active.append(b)
                continue
            if delta > 1.0 and b['hit']:
                continue
            if not b['hit'] and delta < 1.0:
                still_active.append(b)
            elif b['hit']:
                if delta < 0.25:
                    still_active.append(b)
                else:
                    continue
            else:
                still_active.append(b)
        self.active_beats = still_active
        for k in self.lane_flash:
            if self.lane_flash[k] > 0:
                self.lane_flash[k] = max(0, self.lane_flash[k] - dt*4)
        if self.hit_pulse > 0:
            self.hit_pulse = max(0, self.hit_pulse - dt*5)
        if song_t > self.duration + 1.0 and self.next_index >= len(self.beatmap) and not self.active_beats:
            self.is_playing = False
            if self.media_player:
                try: self.media_player.pause()
                except: pass
            self.state = "results"
            self.feedback_text = f"FINISH! Score {self.score}  Max combo x{self.max_combo}"
            self.feedback_color = (255, 255, 100, 255)
            self.feedback_time = time.time()
            self.set_caption("Radial Rhythm - Results")

    def try_hit(self, lane_char):
        if self.state != "playing" or not self.is_playing:
            self.lane_flash[lane_char] = 1.0
            return
        song_t = self.get_song_time()
        best = None
        best_delta = 999
        for b in self.active_beats:
            if b['lane'] != lane_char or b['hit'] or b.get('missed'):
                continue
            delta = abs(song_t - b['time'])
            if delta < best_delta:
                best_delta = delta
                best = b
        if best is None:
            self.combo = max(0, self.combo - 1)
            self.feedback_text = "MISS"
            self.feedback_color = (255, 120, 80, 255)
            self.feedback_time = time.time()
            self.lane_flash[lane_char] = 0.9
            return
        if best_delta <= HIT_WINDOW_PERFECT:
            pts = 300
            self.hits['perfect'] += 1
            self.feedback_text = "PERFECT!"
            self.feedback_color = (255, 240, 80, 255)
        elif best_delta <= HIT_WINDOW_GOOD:
            pts = 150
            self.hits['good'] += 1
            self.feedback_text = "GOOD"
            self.feedback_color = (100, 255, 150, 255)
        elif best_delta <= HIT_WINDOW_OK:
            pts = 50
            self.hits['ok'] += 1
            self.feedback_text = "OK"
            self.feedback_color = (100, 200, 255, 255)
        else:
            # too far - don't double-count miss (update will count timeout)
            # just show MISS feedback without incrementing miss yet
            self.combo = max(0, self.combo - 1)
            self.feedback_text = "MISS"
            self.feedback_color = (255, 80, 80, 255)
            self.feedback_time = time.time()
            self.lane_flash[lane_char] = 1.0
            return
        best['hit'] = True
        self.combo += 1
        self.max_combo = max(self.max_combo, self.combo)
        mult = 1 + min(self.combo // 8, 4) * 0.25
        self.score += int(pts * mult)
        self.feedback_time = time.time()
        self.lane_flash[lane_char] = 1.2
        self.hit_pulse = 1.0

    # --------------------------------------------------------
    # Input
    # --------------------------------------------------------
    def on_key_press(self, symbol, modifiers):
        # Global F11 for fullscreen
        if symbol == key.F11:
            self.is_fullscreen = not self.is_fullscreen
            try:
                self.set_fullscreen(self.is_fullscreen)
            except:
                pass
            return
        # ESC exits fullscreen first
        if symbol == key.ESCAPE and self.is_fullscreen:
            self.is_fullscreen = False
            try:
                self.set_fullscreen(False)
            except:
                pass
            return
        # Global F1 for FPS toggle
        if symbol == key.F1:
            self.show_fps = not self.show_fps
            return
        # Global O for open
        if symbol == key.O:
            # allow opening from any state
            prev_state = self.state
            self.open_media_dialog()
            # if we loaded something, stay where we are but update feedback
            return

        if self.state == "menu":
            if symbol in (key.UP, key.W):
                self.menu_index = (self.menu_index - 1) % len(self.menu_options)
                return
            if symbol in (key.DOWN, key.S):
                self.menu_index = (self.menu_index + 1) % len(self.menu_options)
                return
            if symbol in (key.ENTER, key.SPACE, key.NUM_ENTER):
                sel = self.menu_options[self.menu_index]
                if sel == "PLAY DEMO":
                    self.start_demo()
                elif sel == "SONGS":
                    self.refresh_song_list()
                    self.state = "song_select"
                    self.song_index = 0
                    self.songs_scroll = 0
                elif sel == "QUIT":
                    pyglet.app.exit()
                return
            if symbol == key.ESCAPE:
                pyglet.app.exit()
                return

        elif self.state == "song_select":
            if symbol in (key.UP, key.W):
                if self.song_files:
                    self.song_index = (self.song_index - 1) % len(self.song_files)
                    # adjust scroll
                    if self.song_index < self.songs_scroll:
                        self.songs_scroll = self.song_index
                return
            if symbol in (key.DOWN, key.S):
                if self.song_files:
                    self.song_index = (self.song_index + 1) % len(self.song_files)
                    max_visible = 10
                    if self.song_index >= self.songs_scroll + max_visible:
                        self.songs_scroll = self.song_index - max_visible + 1
                return
            if symbol in (key.ENTER, key.SPACE, key.NUM_ENTER):
                if self.song_files:
                    chosen = self.song_files[self.song_index]
                    self.load_media(str(chosen), autoplay=True)
                else:
                    self.feedback_text = "No songs - add files to songs/ folder"
                    self.feedback_color = (255,180,80,255)
                    self.feedback_time = time.time()
                return
            if symbol == key.R:
                self.refresh_song_list()
                self.feedback_text = f"Refreshed - {len(self.song_files)} songs"
                self.feedback_color = (120,220,255,255)
                self.feedback_time = time.time()
                return
            if symbol in (key.ESCAPE, key.B):
                self.state = "menu"
                return
            if symbol == key.P and self.song_files:
                chosen = self.song_files[self.song_index]
                self.load_media(str(chosen), autoplay=True)
                return

        elif self.state == "analyzing":
            if symbol in (key.ESCAPE, key.B):
                # cancel - go back (thread will finish but result ignored)
                self.state = "song_select"
                self.analysis_progress = 0
                return
            return

        elif self.state == "playing":
            if symbol == key.ESCAPE:
                # pause and go to menu (stop music)
                self.is_playing = False
                self.state = "menu"
                if self.media_player:
                    try: self.media_player.pause()
                    except: pass
                self.feedback_text = "Paused - ESC to menu"
                self.feedback_color = (200,200,200,255)
                self.feedback_time = time.time()
                self.set_caption("Radial Rhythm - Pyglet  |  Main Menu")
                return
            if symbol == key.SPACE:
                # pause toggle
                if self.is_playing:
                    self.is_playing = False
                    if self.media_player:
                        try: self.media_player.pause()
                        except: pass
                    self.state = "paused"
                    self.feedback_text = "PAUSED - SPACE to resume, ESC menu"
                    self.feedback_color = (200,200,200,255)
                    self.feedback_time = time.time()
                else:
                    self.is_playing = True
                    self.state = "playing"
                    if self.media_player:
                        try: self.media_player.play()
                        except: pass
                    # adjust start_time to keep sync after pause: shift start_time
                    # we use wall clock normally, so compensate by reducing elapsed
                    # simpler: reset start_time based on current song_t before pause
                    # for media mode we rely on player.time anyway
                return
            if symbol in (key.PLUS, key.EQUAL, key.NUM_ADD):
                self.sensitivity = min(2.0, self.sensitivity + 0.1)
                self.feedback_text = f"Sensitivity {self.sensitivity:.1f} (re-open song to apply)"
                self.feedback_color = (200,220,255,255)
                self.feedback_time = time.time()
                return
            if symbol in (key.MINUS, key.UNDERSCORE, key.NUM_SUBTRACT):
                self.sensitivity = max(0.6, self.sensitivity - 0.1)
                self.feedback_text = f"Sensitivity {self.sensitivity:.1f} (re-open song to apply)"
                self.feedback_color = (200,220,255,255)
                self.feedback_time = time.time()
                return
            if symbol == key.BRACKETLEFT:
                self.sensitivity = max(0.6, self.sensitivity - 0.1)
                self.feedback_text = f"Sensitivity {self.sensitivity:.1f}"
                self.feedback_color = (200,220,255,255)
                self.feedback_time = time.time()
                return
            if symbol == key.BRACKETRIGHT:
                self.sensitivity = min(2.0, self.sensitivity + 0.1)
                self.feedback_text = f"Sensitivity {self.sensitivity:.1f}"
                self.feedback_color = (200,220,255,255)
                self.feedback_time = time.time()
                return
            if symbol == key.COMMA:
                self.beat_offset = max(-0.5, self.beat_offset - 0.05)
                self.feedback_text = f"Offset {self.beat_offset:+.2f}s (earlier)"
                self.feedback_color = (180, 220, 255, 255)
                self.feedback_time = time.time()
                return
            if symbol == key.PERIOD:
                self.beat_offset = min(0.5, self.beat_offset + 0.05)
                self.feedback_text = f"Offset {self.beat_offset:+.2f}s (later)"
                self.feedback_color = (180, 220, 255, 255)
                self.feedback_time = time.time()
                return
            if symbol in KEY_TO_LANE:
                lane = KEY_TO_LANE[symbol]
                self._last_key_hit = time.time()
                self.try_hit(lane)
                return

        elif self.state == "paused":
            if symbol == key.SPACE or symbol == key.P:
                self.is_playing = True
                self.state = "playing"
                if self.media_player:
                    try: self.media_player.play()
                    except: pass
                return
            if symbol == key.ESCAPE:
                self.is_playing = False
                self.state = "menu"
                if self.media_player:
                    try: self.media_player.pause()
                    except: pass
                self.set_caption("Radial Rhythm - Pyglet  |  Main Menu")
                return

        elif self.state == "results":
            if symbol in (key.ENTER, key.SPACE, key.ESCAPE):
                self.state = "menu"
                self.set_caption("Radial Rhythm - Pyglet  |  Main Menu")
                if self.media_player:
                    try: self.media_player.pause()
                    except: pass
                return
            if symbol == key.R:
                # replay same
                if self.media_path:
                    self.start_media()
                else:
                    self.start_demo()
                return

        # lane hits as text fallback for paused/menu etc handled via on_text

    def on_text(self, text):
        # Disabled: lane hits are handled in on_key_press to avoid double trigger
        # which caused PERFECT to be immediately overwritten by MISS.
        # Keep for fallback only if not handled via key symbol (e.g., IME).
        # Only process if no recent key hit (debounce 30ms)
        if self.state != "playing":
            return
        # If we already handled via on_key_press, ignore duplicate.
        # Check last hit time to debounce.
        if hasattr(self, '_last_key_hit') and time.time() - self._last_key_hit < 0.05:
            return
        t = text.lower()
        if t in CHAR_TO_LANE:
            self.try_hit(CHAR_TO_LANE[t])

    def on_mouse_press(self, x, y, button, modifiers):
        # simple click handling for menu / song select
        if self.state == "menu":
            # menu items are centered at y ~ 360,300,240
            # approximate hit zones
            centers = [(WINDOW_W//2, 360), (WINDOW_W//2, 300), (WINDOW_W//2, 240)]
            for idx, (cx, cy) in enumerate(centers):
                if abs(x - cx) < 210 and abs(y - cy) < 26:
                    self.menu_index = idx
                    # simulate enter
                    self.on_key_press(key.ENTER, 0)
                    break
        elif self.state == "song_select":
            # song list area: x 220-1060, y from 520 down
            if 220 <= x <= 1060 and 120 <= y <= 550:
                # compute index from y
                row_h = 36
                top_y = 520
                rel = top_y - y
                idx = int(rel // row_h) + self.songs_scroll
                if 0 <= idx < len(self.song_files):
                    self.song_index = idx
                    # double click would play; single click selects
                    # if click near bottom play button? just select
                    pass

    # --------------------------------------------------------
    # Drawing helpers
    # --------------------------------------------------------
    def _draw_label(self, text, x, y, size=12, color=(255,255,255,255), anchor_x='left', anchor_y='baseline', font_name='Arial', weight='normal', italic=False):
        # Wrapper to avoid bold kwarg issue + cache for 60fps performance
        # Reuse Label objects per style key to avoid glyph rebuild each frame
        key = (font_name, int(size*10), weight, italic, anchor_x, anchor_y)
        lbl = self._label_cache.get(key)
        if lbl is None:
            lbl = pyglet.text.Label(text, x=x, y=y, font_name=font_name, font_size=size, weight=weight, italic=italic, color=color, anchor_x=anchor_x, anchor_y=anchor_y)
            self._label_cache[key] = lbl
        else:
            # update in-place; pyglet handles layout invalidation
            if lbl.text != text:
                lbl.text = text
            lbl.x = x
            lbl.y = y
            # font changes are rare; only set if different
            if lbl.font_name != font_name:
                lbl.font_name = font_name
            if lbl.font_size != size:
                lbl.font_size = size
            if lbl.weight != weight:
                lbl.weight = weight
            if lbl.italic != italic:
                lbl.italic = italic
            if lbl.color != color:
                lbl.color = color
            # anchor changes require recreate? pyglet allows set
            if lbl.anchor_x != anchor_x:
                lbl.anchor_x = anchor_x
            if lbl.anchor_y != anchor_y:
                lbl.anchor_y = anchor_y
        lbl.draw()
        return lbl

    def _draw_center_target(self, cx, cy, pulse):
        # Fast idle check - skip all if no pulse/flash (common case)
        max_flash = max(self.lane_flash.values()) if self.lane_flash else 0
        if pulse < 0.01 and max_flash < 0.01:
            # ensure hidden shadows stay hidden, but skip updates
            return
        # Optimized: only update if flash/pulse changed (saves ~70% updates when idle)
        if not hasattr(self, '_center_last_pulse'):
            self._center_last_pulse = -1
            self._center_last_flash = {k: -1 for k in LANES}
        # keep essential visible
        self._center_shadow1.visible = False
        self._center_shadow2.visible = False
        self._center_inner.visible = False
        self._center_main.visible = True
        if abs(pulse - self._center_last_pulse) > 0.005:
            pulse_r = TARGET_RADIUS + pulse * 22
            self._center_outer.radius = int(pulse_r)
            self._center_outer.opacity = int(30 + pulse*40)
            self._center_dot.radius = 8 + pulse*6
            self._center_last_pulse = pulse
        for lane_key, info in LANES.items():
            flash = self.lane_flash[lane_key]
            last = self._center_last_flash[lane_key]
            if abs(flash - last) < 0.015 and flash < 0.015:
                continue
            self._center_last_flash[lane_key] = flash
            col = info['color']
            alpha = 60 + int(flash * 180)
            alpha = min(255, alpha)
            width = 2 + flash * 4
            line = self._lane_line_shapes[lane_key]
            line.thickness = width
            line.color = (*col, alpha)
            bg = self._lane_outer_bg_shapes[lane_key]
            bg.radius = 14 + flash*6
            bg.opacity = 90 + int(flash*100)
            sz = 16 + flash*10
            dot = self._center_lane_dots[lane_key]
            dot.radius = sz
            dot.opacity = 200 + int(flash*55)
            glow = self._center_lane_glows[lane_key]
            if flash > 0.1:
                glow.radius = sz+12
                glow.opacity = int(flash*70)
                glow.visible = True
            else:
                if glow.visible:
                    glow.visible = False
                    glow.opacity = 0

    def _draw_beats(self, song_t):
        cx, cy = CENTER
        # Visible toggle is faster than opacity 0 (batch skips invisible)
        # Hide unused from previous frame only
        prev_count = getattr(self, '_beat_last_count', 0)
        pool_idx = 0
        for b in self.active_beats:
            if pool_idx >= len(self._beat_pool):
                break
            slot = self._beat_pool[pool_idx]
            circ = slot['circle']
            inner_c = slot['inner']
            tail = slot['tail']
            hit_circ = slot['hit']
            if b.get('missed'):
                elapsed = song_t - b['miss_time']
                prog = elapsed / 0.45
                if prog >= 1:
                    circ.visible = False
                    inner_c.visible = False
                    tail.visible = False
                    hit_circ.visible = False
                    pool_idx += 1
                    continue
                ang = math.radians(b['angle'])
                x = cx + math.cos(ang) * TARGET_RADIUS
                y = cy + math.sin(ang) * TARGET_RADIUS
                alpha = int(160 * (1 - prog))
                sz = 22 * (1 - prog*0.3)
                col = LANES[b['lane']]['color']
                # dim to grey-ish for miss fade
                dim_col = tuple(int(c*0.45 + 45) for c in col)
                circ.x = x; circ.y = y; circ.radius = sz; circ.color = dim_col
                circ.opacity = max(0, alpha)
                circ.visible = True
                inner_c.visible = False
                tail.visible = False
                hit_circ.visible = False
                pool_idx += 1
                continue
            if b['hit']:
                delta = song_t - b['time'] if self.is_playing else 0
                if delta < 0: delta = 0
                prog = delta / 0.25
                if prog > 1:
                    circ.visible = False
                    inner_c.visible = False
                    tail.visible = False
                    hit_circ.visible = False
                    pool_idx += 1
                    continue
                ang = math.radians(b['angle'])
                x = cx + math.cos(ang) * (TARGET_RADIUS + prog*30)
                y = cy + math.sin(ang) * (TARGET_RADIUS + prog*30)
                alpha = int(255 * (1 - prog))
                sz = 28 * (1 - prog*0.6)
                col = LANES[b['lane']]['color']
                hit_circ.x = x; hit_circ.y = y; hit_circ.radius = sz; hit_circ.color = col
                hit_circ.opacity = max(0, alpha)
                hit_circ.visible = True
                circ.visible = False
                inner_c.visible = False
                tail.visible = False
                pool_idx += 1
                continue
            raw = (song_t - (b['time'] - TRAVEL_TIME)) / TRAVEL_TIME if self.is_playing else 0.0
            if not self.is_playing:
                circ.visible = False; inner_c.visible = False; tail.visible = False; hit_circ.visible = False
                pool_idx += 1
                continue
            if raw < 0: raw = 0
            if raw > 1.2:
                circ.visible = False; inner_c.visible = False; tail.visible = False; hit_circ.visible = False
                pool_idx += 1
                continue
            radius = SPAWN_RADIUS - raw * (SPAWN_RADIUS - TARGET_RADIUS)
            if radius < TARGET_RADIUS:
                radius = TARGET_RADIUS
            ang = math.radians(b['angle'])
            x = cx + math.cos(ang) * radius
            y = cy + math.sin(ang) * radius
            tail_len = 18
            tx = cx + math.cos(ang) * (radius + tail_len)
            ty = cy + math.sin(ang) * (radius + tail_len)
            col = LANES[b['lane']]['color']
            scale = 0.9 + 0.35 * raw
            sz = 22 * scale
            # tail hidden for 60fps (saves 1 shape per beat)
            tail.visible = False
            circ.x = x; circ.y = y; circ.radius = sz; circ.color = col
            circ.opacity = 255
            circ.visible = True
            inner_c.visible = False
            hit_circ.visible = False
            pool_idx += 1
        # hide leftover slots that were visible last frame
        for idx in range(pool_idx, prev_count):
            slot = self._beat_pool[idx]
            slot['circle'].visible = False
            slot['inner'].visible = False
            slot['tail'].visible = False
            slot['hit'].visible = False
        self._beat_last_count = pool_idx

    def _draw_hud(self, song_t):
        # update for fullscreen (dynamic size)
        self._hud_top_bar.width = self.width
        self._hud_top_bar.y = self.height - 46
        self._hud_score_lbl.y = self.height - 16
        self._hud_hits_lbl.y = self.height - 33
        self._hud_time_lbl.y = self.height - 18
        self._hud_mode_lbl.y = self.height - 32
        self._hud_prog_bg.width = self.width
        self._hud_prog_bg.y = 6
        self._hud_prog_fg.y = 6
        # reuse persistent HUD shapes/labels - zero alloc, only update if changed
        self._hud_top_bar.draw()
        # score - only update text if changed to avoid layout rebuild
        new_score = f"Score {self.score:06d}   Combo x{self.combo} (max {self.max_combo})"
        if self._hud_score_lbl.text != new_score:
            self._hud_score_lbl.text = new_score
        self._hud_score_lbl.draw()
        new_hits = f"P:{self.hits['perfect']}  G:{self.hits['good']}  OK:{self.hits['ok']}  M:{self.hits['miss']}"
        if self._hud_hits_lbl.text != new_hits:
            self._hud_hits_lbl.text = new_hits
        self._hud_hits_lbl.draw()
        if self.is_playing:
            prog = song_t / self.duration if self.duration else 0
            prog = max(0, min(1, prog))
            pw = int(self.width * prog)
            # update bg width for fullscreen
            self._hud_prog_bg.width = self.width
            self._hud_prog_bg.draw()
            if self._hud_prog_fg.width != pw:
                self._hud_prog_fg.width = pw
            self._hud_prog_fg.draw()
            new_time = f"{int(song_t//60):01d}:{int(song_t%60):02d} / {int(self.duration//60):01d}:{int(self.duration%60):02d}"
            if self._hud_time_lbl.text != new_time:
                self._hud_time_lbl.text = new_time
            # keep time label at right edge for fullscreen
            self._hud_time_lbl.x = self.width - 12
            self._hud_time_lbl.draw()
            self._hud_mode_lbl.x = self.width - 12
        else:
            mode = f"Media: {Path(self.media_path).name}" if self.media_path else "DEMO MODE"
            if self._hud_mode_lbl.text != mode:
                self._hud_mode_lbl.text = mode
            self._hud_mode_lbl.x = self.width - 12
            self._hud_mode_lbl.draw()
        if self.feedback_text and (time.time() - self.feedback_time) < 1.6:
            age = time.time() - self.feedback_time
            alpha = int(255 * (1 - age/1.6))
            alpha = max(0, min(255, alpha))
            scale = 1.0 + max(0, 0.25 - age*0.5)
            cx, cy = self.width // 2, self.height // 2
            fsize = 28 if "PERFECT" in self.feedback_text else 24
            self._hud_feedback_lbl.text = self.feedback_text
            self._hud_feedback_lbl.font_size = fsize
            self._hud_feedback_lbl.x = cx
            self._hud_feedback_lbl.y = cy+110 + int((scale-1)*20)
            self._hud_feedback_lbl.color = (*self.feedback_color[:3], alpha)
            self._hud_feedback_lbl.draw()

    # --------------------------------------------------------
    # Main draw dispatcher
    # --------------------------------------------------------
    def _draw_video_background(self):
        # Draw MP4 video frame behind game - pyglet FFmpeg syncs texture to audio clock
        if not self.media_player:
            return False
        tex = None
        try:
            # pyglet 2.1: player.texture, older: get_texture()
            if hasattr(self.media_player, 'texture'):
                tex = self.media_player.texture
            if tex is None and hasattr(self.media_player, 'get_texture'):
                try:
                    tex = self.media_player.get_texture()
                except:
                    tex = None
        except:
            tex = None
        if tex is None or tex.width == 0 or tex.height == 0:
            return False
        # Create or update sprite
        if not hasattr(self, '_video_sprite') or self._video_sprite is None:
            try:
                self._video_sprite = pyglet.sprite.Sprite(tex, x=0, y=0)
            except:
                return False
        else:
            try:
                # Update image if changed
                if self._video_sprite.image != tex:
                    self._video_sprite.image = tex
            except:
                pass
        # Scale to cover window (like CSS background-size: cover)
        try:
            scale = max(self.width / tex.width, self.height / tex.height)
            self._video_sprite.scale = scale
            # need to set scale before position? sprite scale affects width/height
            new_w = tex.width * scale
            new_h = tex.height * scale
            self._video_sprite.x = (self.width - new_w) // 2
            self._video_sprite.y = (self.height - new_h) // 2
            self._video_sprite.opacity = 110  # dim so game visible
            self._video_sprite.draw()
            # dim overlay for readability
            overlay = pyglet.shapes.Rectangle(0, 0, self.width, self.height, color=(6, 6, 14))
            overlay.opacity = 140
            overlay.draw()
            return True
        except Exception as e:
            # fallback blit
            try:
                tex.blit(0, 0, width=self.width, height=self.height)
                overlay = pyglet.shapes.Rectangle(0, 0, self.width, self.height, color=(6, 6, 14))
                overlay.opacity = 140
                overlay.draw()
                return True
            except:
                return False

    def on_draw(self):
        self.clear()
        # dynamic center for fullscreen
        cx, cy = self.width // 2, self.height // 2
        # also handle video background scaling via self.width/height
        if self.state in ("playing", "paused", "results"):
            # video background for MP4 (behind everything)
            if self.state in ("playing", "paused") and self.is_media_mode:
                self._draw_video_background()
            # game bg - update batched shapes
            pulse = self.hit_pulse
            self._draw_center_target(cx, cy, pulse)
            if self.state == "playing":
                song_t = self.get_song_time()
                self._draw_beats(song_t)
            elif self.state == "paused":
                # still show beats frozen at pause time? skip (keep last positions)
                pass
            # draw all batched game shapes in one call (60fps)
            try:
                self.game_batch.draw()
            except:
                pass
            # lane outer labels (persistent, on top of batch)
            for lbl in self._lane_text_labels.values():
                lbl.draw()
            # overlay for paused / results
            if self.state == "paused":
                # dim
                dim = pyglet.shapes.Rectangle(0, 0, WINDOW_W, WINDOW_H, color=(0,0,0))
                dim.opacity = 120
                dim.draw()
                self._draw_label("PAUSED", x=WINDOW_W//2, y=WINDOW_H//2 + 40, size=36, color=(255,255,255,255), anchor_x='center', anchor_y='center', weight='bold')
                self._draw_label("SPACE to resume  |  ESC for menu", x=WINDOW_W//2, y=WINDOW_H//2 -10, size=12, color=(200,220,255,255), anchor_x='center', anchor_y='center', font_name='Consolas')
                # fall through to HUD? not needed
            elif self.state == "results":
                dim = pyglet.shapes.Rectangle(0, 0, WINDOW_W, WINDOW_H, color=(0,0,0))
                dim.opacity = 130
                dim.draw()
                card_w, card_h = 560, 360
                card_x = (WINDOW_W - card_w)//2
                card_y = (WINDOW_H - card_h)//2
                bg = pyglet.shapes.Rectangle(card_x, card_y, card_w, card_h, color=(22,22,34))
                bg.draw()
                border = pyglet.shapes.Rectangle(card_x, card_y, card_w, card_h, color=(60,60,90))
                # fake border via 4 lines
                # title
                self._draw_label("RESULTS", x=WINDOW_W//2, y=card_y+card_h-40, size=22, color=(255,255,120,255), anchor_x='center', anchor_y='center', weight='bold')
                self._draw_label(f"Score  {self.score:06d}", x=WINDOW_W//2, y=card_y+card_h-90, size=18, color=(255,255,255,255), anchor_x='center', anchor_y='center', weight='bold')
                self._draw_label(f"Max Combo  x{self.max_combo}    Combo {self.combo}", x=WINDOW_W//2, y=card_y+card_h-120, size=12, color=(180,220,255,255), anchor_x='center', anchor_y='center', font_name='Consolas')
                # hits breakdown
                total = sum(self.hits.values()) or 1
                acc = (self.hits['perfect']*1.0 + self.hits['good']*0.7 + self.hits['ok']*0.4) / total * 100
                self._draw_label(f"PERFECT {self.hits['perfect']}   GOOD {self.hits['good']}   OK {self.hits['ok']}   MISS {self.hits['miss']}", x=WINDOW_W//2, y=card_y+card_h-160, size=11, color=(220,220,240,255), anchor_x='center', anchor_y='center', font_name='Consolas')
                self._draw_label(f"Accuracy  {acc:.1f}%", x=WINDOW_W//2, y=card_y+card_h-190, size=14, color=(120,255,150,255), anchor_x='center', anchor_y='center', weight='bold')
                if self.media_path:
                    self._draw_label(Path(self.media_path).name, x=WINDOW_W//2, y=card_y+card_h-220, size=9, color=(150,170,200,255), anchor_x='center', anchor_y='center', font_name='Consolas')
                self._draw_label("ENTER / SPACE / ESC : back to menu    R : replay", x=WINDOW_W//2, y=card_y+30, size=10, color=(160,160,190,255), anchor_x='center', anchor_y='center', font_name='Consolas')
                # still show HUD? not needed
                return
            # normal playing HUD
            song_t = self.get_song_time() if self.is_playing else 0
            self._draw_hud(song_t)
            # bottom instructions
            if self.state == "playing":
                instr = "D F J K : hit    SPACE pause    ESC menu"
            else:
                instr = ""
            if instr:
                self._draw_label(instr, x=WINDOW_W//2, y=18, size=9, color=(130,130,160,255), anchor_x='center', anchor_y='center', font_name='Consolas')
            return

        if self.state == "analyzing":
            # analysis progress screen
            cx, cy = WINDOW_W//2, WINDOW_H//2
            # dim bg
            bg = pyglet.shapes.Rectangle(0, 0, WINDOW_W, WINDOW_H, color=(10, 10, 18))
            bg.draw()
            # card
            card_w, card_h = 700, 260
            card_x = (WINDOW_W - card_w)//2
            card_y = (WINDOW_H - card_h)//2
            card = pyglet.shapes.Rectangle(card_x, card_y, card_w, card_h, color=(22, 22, 34))
            card.draw()
            # title
            self._draw_label("ANALYSING", x=WINDOW_W//2, y=card_y+card_h-40, size=22, weight='bold', color=(255, 220, 100, 255), anchor_x='center', anchor_y='center')
            fname = Path(self.media_path).name if self.media_path else "song"
            if len(fname) > 48:
                fname = fname[:45] + "..."
            self._draw_label(fname, x=WINDOW_W//2, y=card_y+card_h-80, size=11, color=(180, 180, 210, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            self._draw_label(self.analysis_msg or "Extracting beats...", x=WINDOW_W//2, y=card_y+card_h-110, size=10, color=(140, 160, 190, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            # progress bar bg
            bar_w, bar_h = 520, 18
            bar_x = (WINDOW_W - bar_w)//2
            bar_y = card_y + 90
            bar_bg = pyglet.shapes.Rectangle(bar_x, bar_y, bar_w, bar_h, color=(40, 40, 60))
            bar_bg.draw()
            # progress fill
            prog = max(0.0, min(1.0, self.analysis_progress))
            # animate a little shimmer if progress stuck
            fill_w = int(bar_w * prog)
            if fill_w > 0:
                bar_fg = pyglet.shapes.Rectangle(bar_x, bar_y, fill_w, bar_h, color=(100, 220, 160))
                bar_fg.draw()
                # shimmer
                shimmer_w = 40
                t = time.time() * 2.5
                shimmer_x = bar_x + (int((t % 2.0) * (bar_w + shimmer_w)) - shimmer_w) if prog < 1.0 else bar_x
                # only draw shimmer inside fill
                if prog > 0.02 and prog < 0.99:
                    # clip shimmer to fill
                    sx = max(bar_x, min(bar_x+fill_w - shimmer_w, shimmer_x))
                    sh = pyglet.shapes.Rectangle(sx, bar_y, shimmer_w, bar_h, color=(160, 255, 190))
                    sh.opacity = 90
                    sh.draw()
            self._draw_label(f"{int(prog*100)}%", x=WINDOW_W//2, y=bar_y+bar_h//2, size=10, weight='bold', color=(255, 255, 255, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            # cached hint
            self._draw_label("First load analyses via ffmpeg • next load is instant (cached)", x=WINDOW_W//2, y=card_y+45, size=9, color=(110, 120, 150, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            self._draw_label("ESC to cancel", x=WINDOW_W//2, y=card_y+22, size=9, color=(140, 140, 170, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            # also draw video preview dim if available? skip
            return

        if self.state == "menu":
            # update for fullscreen (dynamic center)
            self._menu_title_lbl.x = self.width // 2
            self._menu_sub_lbl.x = self.width // 2
            self._menu_songs_hint_lbl.x = self.width // 2
            self._menu_footer_lbl.x = self.width // 2
            # title - persistent
            self._menu_title_lbl.draw()
            self._menu_sub_lbl.draw()
            self._menu_songs_hint_lbl.text = f"songs in  ./songs/  ({len(self.song_files)} found)  •  add mp4 / mp3 / wav and press SONGS"
            self._menu_songs_hint_lbl.draw()

            # menu options as boxes - reuse persistent shapes (no alloc)
            for idx, opt in enumerate(self.menu_options):
                y = 360 - idx*60 + (self.height - WINDOW_H)//2
                selected = idx == self.menu_index
                bg = self._menu_bg_rects[idx]
                bg.x = self.width // 2 - 210
                bg.y = y - 22
                bg.color = (55, 55, 90) if selected else (28, 28, 42)
                border = self._menu_border_rects[idx]
                border.x = self.width // 2 - 211
                border.y = y - 23
                accent = self._menu_accent_rects[idx]
                accent.x = self.width // 2 - 210
                accent.y = y - 22
                # draw order: border behind, bg, accent
                if selected:
                    border.visible = True
                    accent.visible = True
                    border.draw()
                    bg.draw()
                    accent.draw()
                else:
                    border.visible = False
                    accent.visible = False
                    bg.draw()
                # label
                lbl = self._menu_option_lbls[idx]
                lbl.y = y
                txt_col = (255,255,120,255) if selected else (220,220,240,255)
                lbl.color = (*txt_col, 255)
                lbl.weight = 'bold' if selected else 'normal'
                lbl.text = opt
                lbl.draw()

            # footer - persistent
            self._menu_footer_lbl.draw()
            # lane legend - reuse persistent circles/labels (dynamic for fullscreen)
            for i, lane in enumerate(LANE_ORDER):
                c = self._menu_lane_circles[i]
                xs = self.width // 2 - 160 + i*90
                c.x = xs + 16
                c.y = 30
                c.draw()
                lbl = self._menu_lane_lbls[i]
                lbl.x = xs+34
                lbl.y = 30
                lbl.text = lane.upper()
                lbl.draw()
            return

        if self.state == "song_select":
            self._draw_label("SELECT SONG", x=WINDOW_W//2, y=WINDOW_H - 50, size=26, color=(255,255,255,255), anchor_x='center', anchor_y='center', weight='bold')
            self._draw_label(f"./songs/  •  {len(self.song_files)} track(s)  •  R refresh  •  O open external  •  B/ESC back", x=WINDOW_W//2, y=WINDOW_H - 80, size=10, color=(140,160,190,255), anchor_x='center', anchor_y='center', font_name='Consolas')
            # panel
            panel_x, panel_y, panel_w, panel_h = 200, 100, 880, 460
            panel = pyglet.shapes.Rectangle(panel_x, panel_y, panel_w, panel_h, color=(18,18,30))
            panel.draw()
            # inner border
            # list
            if not self.song_files:
                self._draw_label("No songs found.", x=WINDOW_W//2, y=panel_y + panel_h//2 + 20, size=14, color=(255,220,120,255), anchor_x='center', anchor_y='center', weight='bold')
                self._draw_label("Drop .mp4 / .mp3 / .wav / .m4a / .ogg / .flac into  songs/  folder", x=WINDOW_W//2, y=panel_y + panel_h//2 -10, size=10, color=(180,180,210,255), anchor_x='center', anchor_y='center', font_name='Consolas')
                self._draw_label("then press  R  to refresh", x=WINDOW_W//2, y=panel_y + panel_h//2 -30, size=10, color=(180,180,210,255), anchor_x='center', anchor_y='center', font_name='Consolas')
                # show demo hint
                self._draw_label("or press  O  to open a file outside songs/", x=WINDOW_W//2, y=panel_y + 40, size=9, color=(120,140,170,255), anchor_x='center', anchor_y='center', font_name='Consolas')
            else:
                max_visible = 10
                start = self.songs_scroll
                end = min(len(self.song_files), start + max_visible)
                for i in range(start, end):
                    p = self.song_files[i]
                    rel = i - start
                    y = 520 - rel*36
                    selected = i == self.song_index
                    # row bg
                    if selected:
                        row = pyglet.shapes.Rectangle(panel_x+10, y-14, panel_w-20, 28, color=(48,48,82))
                        row.draw()
                        # accent bar
                        acc = pyglet.shapes.Rectangle(panel_x+10, y-14, 4, 28, color=(100,255,160))
                        acc.draw()
                    # icon
                    # file name shorten to 48 chars
                    name = p.name
                    if len(name) > 52:
                        name = name[:49] + "..."
                    # size / ext
                    try:
                        sz_mb = p.stat().st_size / (1024*1024)
                        sz_str = f"{sz_mb:.1f} MB"
                    except:
                        sz_str = ""
                    ext = p.suffix.lower()
                    icon_col = (255,74,74) if ext in (".mp4",".mov",".mkv",".avi",".webm",".m4v") else (100,200,255)
                    dot = pyglet.shapes.Circle(panel_x+26, y, 6, color=icon_col)
                    dot.draw()
                    col = (255,255,140,255) if selected else (220,220,240,255)
                    weight = 'bold' if selected else 'normal'
                    self._draw_label(name, x=panel_x+44, y=y, size=11, color=(*col[:3],255), anchor_x='left', anchor_y='center', font_name='Consolas', weight=weight)
                    self._draw_label(sz_str, x=panel_x+panel_w-20, y=y, size=9, color=(140,150,180,255), anchor_x='right', anchor_y='center', font_name='Consolas')
                # scroll indicator
                if len(self.song_files) > max_visible:
                    track_h = panel_h - 20
                    thumb_h = max(20, track_h * max_visible / len(self.song_files))
                    thumb_y = panel_y + 10 + (track_h - thumb_h) * (self.songs_scroll / max(1, len(self.song_files)-max_visible))
                    track = pyglet.shapes.Rectangle(panel_x+panel_w-8, panel_y+10, 4, track_h, color=(40,40,60))
                    track.draw()
                    thumb = pyglet.shapes.Rectangle(panel_x+panel_w-8, thumb_y, 4, thumb_h, color=(120,140,190))
                    thumb.draw()
                # footer inside panel
                self._draw_label(f"{self.song_index+1} / {len(self.song_files)}", x=panel_x+16, y=panel_y+14, size=9, color=(120,140,170,255), anchor_x='left', anchor_y='center', font_name='Consolas')
                self._draw_label("ENTER play  •  UP/DOWN navigate", x=panel_x+panel_w//2, y=panel_y+14, size=9, color=(120,140,170,255), anchor_x='center', anchor_y='center', font_name='Consolas')
            # feedback line at bottom
            if self.feedback_text and (time.time() - self.feedback_time) < 3.0:
                age = time.time() - self.feedback_time
                alpha = int(180 * (1 - age/3.0))
                self._draw_label(self.feedback_text, x=WINDOW_W//2, y=60, size=10, color=(*self.feedback_color[:3], max(0,alpha)), anchor_x='center', anchor_y='center', font_name='Consolas')
            return

    def on_close(self):
        # cleanup temp wav
        try:
            self._stop_av_thread()
        except: pass
        try:
            if getattr(self, '_temp_audio_wav', None) and os.path.exists(self._temp_audio_wav):
                os.unlink(self._temp_audio_wav)
        except: pass
        super().on_close()

def main():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except:
        print("WARNING: ffmpeg not found in PATH - mp4 analysis will fail. Install ffmpeg.")
    SONGS_DIR.mkdir(parents=True, exist_ok=True)
    # create a readme inside songs if empty so user knows where to put files
    readme = SONGS_DIR / "_put_songs_here.txt"
    if not readme.exists():
        try:
            readme.write_text("Put your mp4 / mp3 / wav / m4a / ogg / flac files in this folder.\nThey will appear in Songs menu.\nSupported: " + ", ".join(sorted(SUPPORTED_EXTS)) + "\n", encoding="utf-8")
        except: pass
    game = RhythmGame()
    if len(sys.argv) > 1:
        p = sys.argv[1]
        if os.path.exists(p):
            game.load_media(p)
            # if launched with file, go straight to playing? keep in song_select feedback then let user press play
            game.state = "song_select"
            game.refresh_song_list()
            # highlight that file if it's inside songs
            try:
                rp = Path(p).resolve()
                for idx, sf in enumerate(game.song_files):
                    if sf.resolve() == rp:
                        game.song_index = idx
                        break
            except: pass
    pyglet.app.run()

if __name__ == "__main__":
    main()
