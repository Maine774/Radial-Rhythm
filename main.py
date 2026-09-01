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
# Spiral approach: each note winds 90 degrees inward from its predecessor lane's
# side to its own lane's side. A gap larger than this to the previous note marks a
# new musical section, and that note coils counterclockwise as a telegraph.
SECTION_GAP_THRESHOLD = 2.0
SPIRAL_TURNS_DEG = 90.0

def spiral_point(hit_ang, cw, raw):
    """(x, y, deg_angle) of a note spiraling inward from its predecessor's side
    (raw=0, SPAWN_RADIUS) to its own lane side (raw=1, TARGET_RADIUS)."""
    radius = SPAWN_RADIUS - raw * (SPAWN_RADIUS - TARGET_RADIUS)
    if radius < TARGET_RADIUS:
        radius = TARGET_RADIUS
    if cw:
        ang = (hit_ang + 90.0) - raw * SPIRAL_TURNS_DEG
    else:
        ang = (hit_ang - 90.0) + raw * SPIRAL_TURNS_DEG
    a = math.radians(ang)
    return math.cos(a) * radius, math.sin(a) * radius

def spiral_guide_points(hit_ang, cw=True, segs=20):
    """Polyline points (in window coords) tracing the spiral lane path."""
    cx, cy = CENTER
    pts = []
    for i in range(segs + 1):
        x, y = spiral_point(hit_ang, cw, i / segs)
        pts.append((cx + x, cy + y))
    return pts

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

# Difficulty profiles (runnable order). Each maps to onset <threshold> (higher = fewer
# peaks), <combine> (peak merging window), <min_gap> (min seconds between notes) and a
# <target> density (notes/second). The same voice/melody focus applies to all modes;
# only the density dial is turned. The <voice_w> key is the bass-floor multiplier for
# percussive onsets (higher = less voice emphasis).
DIFFICULTY_PROFILES = {
    # name      : (threshold, combine, min_gap, target_density, voice_floor, label, desc, key)
    "easy":   (0.32, 0.12, 0.38, 1.45,  0.35, "EASY",   "melody & voice • ~1.5/s • low density",            key._1),
    "medium": (0.30, 0.11, 0.26, 2.10,  0.45, "MEDIUM", "melody + snare/bass • ~2.1/s • moderate density",   key._2),
    "hard":   (0.28, 0.10, 0.18, 3.00,  0.55, "HARD",   "full groove • ~3.0/s • high density (16ths/hi-hat)", key._3),
}
DIFFICULTY_ORDER = ["easy", "medium", "hard"]
# monostar scale 1..20 -> lane-mapped density; 1-3 okay but we floor at easy density
# so a 1 is not empty. 20 is the practical physical ceiling at 100ms min gap.
RATING_MIN_NPS, RATING_MAX_NPS = 1.45, 3.00
CACHE_VERSION = 4


def clamp_difficulty(diff):
    """Normalize a difficulty string to one of easy/medium/hard (or default 'easy')."""
    d = str(diff).lower() if diff else "easy"
    return d if d in DIFFICULTY_ORDER else "easy"


def density_to_rating(nps):
    """Map notes-per-second in [1.45, 3.00] to a monostar rating 1..20."""
    lo, hi = RATING_MIN_NPS, RATING_MAX_NPS
    n = max(lo, min(hi, float(nps)))
    return max(1, min(20, int(round(1 + (n - lo) / (hi - lo) * 19))))


def rating_marker(rating):
    """Render a 1..20 rating as a compact 'dx' marker (d3/d6/d9/d12/d15/d18/d20-ish)."""
    r = int(max(1, min(20, rating)))
    if r <= 3:
        return f"d{max(1, r)}"
    tiers = [6, 9, 12, 15, 18, 20]
    best = min(tiers, key=lambda t: abs(t - r))
    return f"d{best}"

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
def get_cache_path(media_path, difficulty="easy"):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except: pass
    stem = Path(media_path).stem
    h = hashlib.md5(str(Path(media_path).resolve()).encode()).hexdigest()[:8]
    diff = clamp_difficulty(difficulty)
    return CACHE_DIR / f"{stem}_{h}_{diff}.json"

def _legacy_cache_path(media_path):
    # previous version without difficulty suffix (v2)
    stem = Path(media_path).stem
    h = hashlib.md5(str(Path(media_path).resolve()).encode()).hexdigest()[:8]
    return CACHE_DIR / f"{stem}_{h}.json"

def load_cached_beatmap(media_path, sensitivity=1.0, difficulty="easy"):
    cp = get_cache_path(media_path, difficulty)
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
        # difficulty check for v3 caches
        if data.get('version', 2) >= 3:
            cached_diff = str(data.get('difficulty','easy')).lower()
            if cached_diff != str(difficulty).lower():
                return None
        bm = data.get('beatmap', [])
        duration = data.get('duration', 30.0)
        tempo = data.get('tempo', 120.0)
        rating = data.get('rating', 1)
        # convert beatmap back to list of tuples
        beatmap = [(float(t), str(lane)) for t, lane in bm]
        return beatmap, float(duration), float(tempo), int(rating)
    except Exception as e:
        print(f"[cache] load failed {e}")
        return None

def save_cached_beatmap(media_path, beatmap, duration, tempo, sensitivity=1.0, difficulty="easy", rating=1):
    try:
        cp = get_cache_path(media_path, difficulty)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            'beatmap': [[float(t), str(lane)] for t, lane in beatmap],
            'duration': float(duration),
            'tempo': float(tempo),
            'sensitivity': float(sensitivity),
            'difficulty': clamp_difficulty(difficulty),
            'rating': int(rating),
            'mtime': Path(media_path).stat().st_mtime if Path(media_path).exists() else 0,
            'version': CACHE_VERSION
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

def detect_beats_madmom(sr, audio, sensitivity=1.0, difficulty="easy", voice_focus=True, progress_cb=None):
    """madmom RNN onset detection with strong voice/melody focus.
    Harmonic/percussive flux weights madmom activation so voice/melody onsets
    dominate. Runs at a density dialled by difficulty profile:
      easy (~1.45/s) lowest threshold => mostly voice, gap-filled
      medium (~2.1/s) adds snare/bass hits
      hard (~3.0/s) full groove, captures 16ths/hi-hat/synth.
    Returns (times, bpm, rating) where rating is a monostar 1..20."""
    import numpy as _np
    from madmom.features.onsets import RNNOnsetProcessor, OnsetPeakPickingProcessor
    def prog(v):
        if progress_cb:
            try: progress_cb(v)
            except: pass
    prog(0.1)
    acts = RNNOnsetProcessor()(audio.astype(_np.float32, copy=False), sample_rate=sr)
    prog(0.55)
    # harmonic voice weights (0..1, high = harmonic/voice)
    voice_w = None
    if voice_focus:
        try:
            voice_w = _harmonic_voice_weights(sr, audio, len(acts), fps_target=100)
        except:
            voice_w = None
    # per-difficulty tuning table: (threshold, combine, min_gap, target_density, voice_floor)
    difficulty = clamp_difficulty(difficulty)
    if difficulty == "easy":
        thr, comb, mg, target_density, voice_floor = 0.32, 0.12, 0.38, 1.45, 0.35
    elif difficulty == "medium":
        thr, comb, mg, target_density, voice_floor = 0.30, 0.11, 0.26, 2.10, 0.45
    else:
        thr, comb, mg, target_density, voice_floor = 0.28, 0.10, 0.18, 3.00, 0.55
    # choose weighting per difficulty: all modes keep voice emphasis but the
    # percussive floor rises from easy -> hard
    if voice_w is not None and voice_focus:
        acts_w = acts * (voice_floor + (1.0 - voice_floor) * voice_w)
    else:
        acts_w = acts
    prog(0.65)
    target_n = int(round(len(audio)/float(sr) * target_density))
    peak = OnsetPeakPickingProcessor(threshold=thr, combine=comb, fps=100)
    beats = [float(t) for t in peak(acts_w)]
    # enforce min_gap
    tmp = []
    for t in beats:
        if not tmp or t - tmp[-1] >= mg - 1e-6:
            tmp.append(t)
    beats = tmp
    # if too many (dense), keep strongest by weighted activation
    if len(beats) > target_n * 1.25:
        vals = []
        for t in beats:
            idx = int(round(t*100))
            idx = max(0, min(len(acts_w)-1, idx))
            vals.append(float(acts_w[idx]))
        order = np.argsort(np.array(vals))[::-1]
        beats = sorted([beats[i] for i in order[:target_n]])
    elif len(beats) > target_n:
        beats = beats[:target_n]
    # gap fallback (easy + medium): if gaps >3.5s where no voice, insert best
    # original (unweighted) onset in gap so instrumental/break sections still play
    if difficulty in ("easy", "medium") and voice_focus and len(beats) > 4:
        gap_threshold = 3.5
        # build dense pool from original acts for fallback candidates
        peak_all = OnsetPeakPickingProcessor(threshold=0.30, combine=0.10, fps=100)
        pool_all = [float(t) for t in peak_all(acts)]
        tmp2=[]
        for t in pool_all:
            if not tmp2 or t-tmp2[-1] >= 0.12:
                tmp2.append(t)
        pool_all = tmp2
        # find large gaps
        beats_sorted = sorted(beats)
        gaps = []
        # also check start gap
        if beats_sorted[0] > 4.0:
            gaps.append((0.0, beats_sorted[0]))
        for i in range(len(beats_sorted)-1):
            if beats_sorted[i+1] - beats_sorted[i] > gap_threshold:
                gaps.append((beats_sorted[i], beats_sorted[i+1]))
        # tail gap
        dur = len(audio)/float(sr)
        if dur - beats_sorted[-1] > 4.0:
            gaps.append((beats_sorted[-1], dur))
        for g0,g1 in gaps:
            # best candidate inside gap (with margin)
            cand = [t for t in pool_all if g0+0.3 < t < g1-0.3]
            if not cand:
                continue
            # pick most energetic original
            best = max(cand, key=lambda t: float(acts[int(round(t*100))]) if 0 <= int(round(t*100)) < len(acts) else 0)
            # insert if not violating min_gap to neighbors
            if all(abs(best - b) >= mg*0.8 for b in beats_sorted):
                beats.append(best)
        beats = sorted(beats)
        # re-enforce min_gap after gap inserts (keep earliest)
        tmp=[]
        for t in beats:
            if not tmp or t-tmp[-1] >= mg*0.8:
                tmp.append(t)
        beats = tmp
    prog(0.85)
    # tempo from harmonic flux? use original flux for tempo (more stable)
    bpm = 120.0
    try:
        _flux = onset_envelope_sfx(sr, audio, hop=512)
        bpm, _ = estimate_tempo_autocorr(sr, _flux, hop=512)
        if not (30 < bpm < 240):
            bpm = 120.0
    except Exception:
        bpm = 120.0
    # rating from achieved density (monostar 1..20)
    dur = len(audio)/float(sr)
    nps = len(beats)/dur if dur > 0 else 0.0
    rating = density_to_rating(nps)
    prog(1.0)
    return [float(t) for t in beats], float(bpm), int(rating)

def _voice_ratio_for_time(t, sr, audio):
    """Voice band energy ratio 150-4000 Hz vs total - proxy for voice/melody presence."""
    try:
        N = 2048
        c = int(t*sr)
        half = 1024
        n = len(audio)
        lo = max(0, c-half)
        hi = min(n, c+half)
        win = audio[lo:hi]
        if len(win) < 256:
            return 0.0
        if len(win) < N:
            win = np.pad(win, (0, N-len(win)))
        else:
            win = win[:N]
        w = np.hanning(N).astype(np.float32)
        spec = np.abs(np.fft.rfft((win*w).astype(np.float32)))
        freqs = np.fft.rfftfreq(N, 1.0/sr)
        mask = (freqs >= 150) & (freqs <= 4000)
        tot = float(np.sum(spec) + 1e-9)
        return float(np.sum(spec[mask]) / tot)
    except:
        return 0.0

def _select_voice_focused(times, sr, audio, target_n, min_gap):
    """From dense pool, keep most voice-like beats respecting min_gap.
    Greedy by voice score (highest first)."""
    if not times or target_n <= 0:
        return []
    if len(times) <= target_n and min_gap <= 0.12:
        return sorted(times)
    # score each time
    scores = []
    for t in times:
        vr = _voice_ratio_for_time(t, sr, audio)
        # combine with tonality (1-flat) to prefer harmonic
        # compute flatness quickly for same window
        try:
            N = 2048
            c = int(t*sr)
            half = 1024
            n = len(audio)
            lo = max(0, c-half); hi = min(n, c+half)
            win = audio[lo:hi]
            if len(win) < N:
                win = np.pad(win, (0, N-len(win)))
            else:
                win = win[:N]
            w = np.hanning(N).astype(np.float32)
            spec = np.abs(np.fft.rfft((win*w).astype(np.float32)))
            spec = np.maximum(spec, 1e-8)
            gm = float(np.exp(np.mean(np.log(spec))))
            am = float(np.mean(spec))
            flat = gm/am if am>0 else 1.0
            tonal = max(0.0, 1.0-flat)
            score = vr * (0.6 + 0.4*tonal)
        except:
            score = vr
        scores.append(score)
    order = np.argsort(np.array(scores))[::-1]  # most voice first
    selected = []
    for idx in order:
        t = float(times[idx])
        # gap check vs already selected
        ok = True
        for s in selected:
            if abs(t - s) < min_gap - 1e-6:
                ok = False
                break
        if ok:
            selected.append(t)
            if len(selected) >= target_n:
                break
    selected = sorted(selected)
    # if we still under target (gaps too strict), fill with next best that least violates
    if len(selected) < min(target_n, len(times)):
        # second pass: allow closer but penalize
        for idx in order:
            t = float(times[idx])
            if t in selected:
                continue
            # find closest selected
            closest = min(abs(t-s) for s in selected) if selected else 1e9
            if closest >= min_gap*0.65:  # allow slightly closer
                selected.append(t)
                selected = sorted(selected)
                if len(selected) >= target_n:
                    break
    return sorted(selected[:target_n])

def _harmonic_voice_weights(sr, audio, n_target, fps_target=100):
    """Voice weight per frame aligned to madmom acts (n_target frames at fps_target).
    Harmonic vs percussive flux ratio - fast version hop 1024."""
    try:
        import librosa
        import scipy.ndimage
        n_fft = 1024
        hop = 1024
        D = np.abs(librosa.stft(audio, n_fft=n_fft, hop_length=hop))
        harm = scipy.ndimage.median_filter(D, size=(1,9))
        perc = scipy.ndimage.median_filter(D, size=(9,1))
        def flux(M):
            Ml = np.log1p(M*50.0)
            diff = Ml[:,1:] - Ml[:,:-1]
            diff[diff<0] = 0
            return np.concatenate([[0.0], np.sum(diff, axis=0)])
        flux_h = flux(harm)
        flux_p = flux(perc)
        voice_w = flux_h / (flux_h + flux_p + 1e-9)
        voice_w = np.clip(voice_w, 0, 1)
        voice_w = np.convolve(voice_w, np.ones(3)/3, mode='same')
        fps_flux = sr / hop
        t_flux = np.arange(len(voice_w)) / fps_flux
        t_target = np.arange(n_target) / fps_target
        interp = np.interp(t_target, t_flux, voice_w, left=voice_w[0], right=voice_w[-1])
        return interp
    except Exception:
        return None

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

def beats_from_media(media_path, difficulty="easy", sensitivity=1.0, use_librosa=True, progress_cb=None):
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
            nps = len(lane_pattern)/float(duration) if duration > 0 else 0.0
            return lane_pattern, float(duration), float(tempo) if hasattr(tempo, '__float__') else 120.0, density_to_rating(nps)
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
        _rating = 1
        try:
            import madmom
            prog(0.4)
            madmom_times, _bpm, _rating = detect_beats_madmom(sr_read, audio, sensitivity=sensitivity,
                difficulty=difficulty, voice_focus=True,
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
        rating = _rating if is_main else density_to_rating(
            len(beatmap)/float(duration) if duration > 0 else 0.0)
        prog(0.92)
        # also handle librosa return prog
        prog(1.0)
        return beatmap, float(duration), detected_bpm, int(rating)
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
        return beatmap, duration, bpm, density_to_rating(len(beatmap)/float(duration) if duration else 0.0)

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
# Reusable pooled rounded-rectangle bank for fast (60fps) UI drawing.
# Screens fill slots each frame and the batch is drawn once, avoiding
# the cost of constructing pyglet shapes per-frame in on_draw.
# ------------------------------------------------------------
class ShapeBank:
    def __init__(self, n=256, default_radius=12):
        self.batch = pyglet.graphics.Batch()
        self.slots = []
        self.used = 0
        self.default_radius = default_radius
        for _ in range(n):
            r = pyglet.shapes.RoundedRectangle(0, 0, 1, 1, radius=default_radius,
                                               color=(255, 255, 255), batch=self.batch)
            r.visible = False
            self.slots.append(r)

    def reset(self):
        for s in self.slots:
            s.visible = False
        self.used = 0

    def rect(self, x, y, w, h, color, radius=None, opacity=255, visible=True):
        """Grab the next free slot, position it and mark it visible.
        Returns None if the bank is exhausted."""
        if self.used >= len(self.slots):
            return None
        s = self.slots[self.used]
        self.used += 1
        s.x = x
        s.y = y
        s.width = w
        s.height = h
        if radius is not None:
            s.radius = radius
        s.color = color
        s.opacity = opacity
        s.visible = visible
        return s

    def draw(self):
        self.batch.draw()


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
        # ---- persisted settings ----
        self.settings = {
            "fullscreen": False,
            "input_latency": 0.0,        # seconds added to note timing (positive = later)
            "music_volume": 0.9,         # 0..1
            "fx_volume": 0.7,            # 0..1 (reserved for future FX hitsounds/effects)
            "video_brightness": 0.30,    # 0..1 (video/background dimming; higher = brighter video)
            "lane_alpha": 0.85,          # 0..1 opacity of lanes, beats and target ring
        }
        self._config_path = Path(__file__).resolve().parent / "config.json"
        self._load_config()
        if self.settings.get("fullscreen"):
            try:
                self.set_fullscreen(True)
                self.is_fullscreen = True
            except Exception:
                self.is_fullscreen = False

        # game state
        self.state = "menu"  # menu / song_select / playing / paused / results / keybinds
        self.menu_index = 0
        self.menu_options = ["PLAY", "OPEN FILE", "SETTINGS", "QUIT"]
        self.menu_pull = 0.0  # animated menu selection position
        self.settings_index = 0
        # each row: (settings_key, label, type) where type is toggle / range / submenu.
        # "submenu" opens a dedicated screen (e.g. keybinds) instead of adjusting a value.
        self.settings_rows = [
            ("fullscreen",       "Fullscreen",         "toggle"),
            ("input_latency",    "Input latency",      "range"),
            ("music_volume",     "Music volume",       "range"),
            ("fx_volume",        "FX volume",          "range"),
            ("video_brightness", "Video brightness",   "range"),
            ("lane_alpha",       "Lane / beat opacity","range"),
            ("keybinds",         "Keybinds",           "submenu"),
        ]
        self.keybind_index = 0          # selected row in the keybinds screen
        self.binding_target = None      # lane key currently waiting on a new keypress
        # song-select preview state
        self.sc_scroll = 0.0       # animated carousel offset (pixels)
        self.sc_selected_pull = 0.0  # animated pull-out amount 0..1
        self.preview_player = None
        self.preview_source = None
        self.preview_path = None
        self.preview_sprite = None
        self._preview_seek = 2.0
        self._preview_seek_done = True
        self.preview_accent = (100, 255, 160)   # avg color of selected song preview (for selection tab)
        self.song_accents = {}   # path -> (r,g,b) song accent colour (computed via ffmpeg)
        self._accent_path = None
        self.song_durations = {}   # path -> duration seconds (cached)
        self._load_song_colors()

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
        self.difficulty = "easy"
        self.difficulty_index = 0
        self.difficulty_options = [DIFFICULTY_PROFILES[k][5] for k in DIFFICULTY_ORDER]  # ["EASY","MEDIUM","HARD"]
        self.pending_song_path = None
        self.beatmap_rating = 1

        # scoring
        self.score = 0
        self.combo = 0
        self.max_combo = 0
        self.fc = 0          # current perfect-combo
        self.max_fc = 0      # longest perfect-combo
        self.hits = {'perfect': 0, 'good': 0, 'meh': 0, 'miss': 0}
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
            hit_ang = info['angle']
            col = info['color']
            # Curved lane guides: draw the clockwise spiral path each note follows,
            # from the predecessor lane's spawn side winding to this lane's side.
            pts = spiral_guide_points(hit_ang, cw=True, segs=18)
            segs = []
            for i in range(len(pts) - 1):
                seg = pyglet.shapes.Line(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1],
                                         thickness=2, color=(*col, 60), batch=self.game_batch)
                segs.append(seg)
            self._lane_line_shapes[lane_key] = segs
            # outer spawn ring sits where the spiral begins = predecessor lane's side
            sx = cx + math.cos(math.radians((hit_ang + 90.0) % 360.0)) * SPAWN_RADIUS
            sy = cy + math.sin(math.radians((hit_ang + 90.0) % 360.0)) * SPAWN_RADIUS
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
            # ghost spiral arc (telegraph for counterclockwise/new-section notes):
            # a faint multi-segment path showing the upcoming coil direction
            ghost_pts = 16
            ghost_segs = []
            for s in range(ghost_pts - 1):
                ln = pyglet.shapes.Line(0, 0, 0, 0, thickness=2, color=(220, 240, 255, 90), batch=self.game_batch)
                ln.visible = False
                ghost_segs.append(ln)
            self._beat_pool.append({'circle': circ, 'inner': inner, 'tail': tail, 'hit': hit_circ, 'ghost': ghost_segs, 'in_use': False})

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
        self._hud_grade_lbl = pyglet.text.Label("", x=16, y=WINDOW_H-52, font_name='Arial', font_size=16, weight='bold', color=(255,220,120,255), anchor_x='left', anchor_y='top')
        self._hud_fc_lbl = pyglet.text.Label("", x=16, y=WINDOW_H-70, font_name='Consolas', font_size=10, color=(140,220,255,255), anchor_x='left', anchor_y='top')
        self._hud_time_lbl = pyglet.text.Label("", x=WINDOW_W-12, y=WINDOW_H-18, font_name='Consolas', font_size=10, color=(180,220,255,255), anchor_x='right', anchor_y='top')
        self._hud_mode_lbl = pyglet.text.Label("", x=WINDOW_W-12, y=WINDOW_H-32, font_name='Consolas', font_size=10, color=(150,170,200,255), anchor_x='right', anchor_y='top')
        self._hud_feedback_lbl = pyglet.text.Label("", x=CENTER[0], y=CENTER[1]+110, font_name='Arial', font_size=24, weight='bold', color=(255,255,255,255), anchor_x='center', anchor_y='center')
        self._hud_instr_lbl = pyglet.text.Label("", x=WINDOW_W//2, y=18, font_name='Consolas', font_size=9, color=(130,130,160,255), anchor_x='center', anchor_y='center')
        # HUD shapes (reuse)
        self._hud_top_bar = pyglet.shapes.Rectangle(0, WINDOW_H-46, WINDOW_W, 46, color=(18,18,30))
        self._hud_top_bar.opacity = 220
        self._hud_prog_bg = pyglet.shapes.Rectangle(0, 6, WINDOW_W, 4, color=(40,40,50))
        self._hud_prog_fg = pyglet.shapes.Rectangle(0, 6, 0, 4, color=(100,255,160))
        # score meter (top-left): thin bar under the score text tracking score / max_score
        self._hud_meter_bg = pyglet.shapes.Rectangle(16, WINDOW_H-96, 220, 8, color=(30,30,45))
        self._hud_meter_fg = pyglet.shapes.Rectangle(16, WINDOW_H-96, 0, 8, color=(120,220,140))
        self._hud_meter_fg.visible = False

        # ---- persistent menu shapes for 60fps ----
        self._menu_batch = pyglet.graphics.Batch()
        self._shapes = ShapeBank()
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
        self._menu_footer_lbl = pyglet.text.Label("UP/DOWN or W/S : navigate   •   ENTER/SPACE : select   •   O : open external file   •   ESC : quit", x=WINDOW_W//2, y=70, font_name='Consolas', font_size=9, color=(110,120,150,255), anchor_x='center', anchor_y='center')
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
        self.song_durations = {}

    def _stop_preview(self):
        try:
            if self.preview_player:
                self.preview_player.pause()
                try: self.preview_player.delete()
                except: pass
        except: pass
        self.preview_player = None
        self.preview_source = None
        self.preview_path = None
        self.preview_sprite = None
        self.preview_accent = (100, 255, 160)

    def _load_config(self):
        try:
            if self._config_path.exists():
                import json as _js
                data = _js.load(open(self._config_path, encoding='utf-8'))
                if isinstance(data, dict):
                    # apply saved keys where present, keeping defaults otherwise
                    for k, default in self.settings.items():
                        if k in data and data[k] is not None:
                            self.settings[k] = data[k]
                    if "keybinds" in data:
                        self._apply_keybinds(data["keybinds"])
        except Exception:
            pass
        # keep live beat offset in sync after load (only if it already exists)
        try:
            if hasattr(self, 'beat_offset'):
                self.beat_offset = float(self.settings.get("input_latency", 0.0))
        except Exception:
            pass

    def _save_config(self):
        try:
            import json as _js
            with open(self._config_path, 'w', encoding='utf-8') as f:
                _js.dump(self.settings, f, indent=2)
        except Exception:
            pass

    def _apply_fullscreen(self, on):
        self.is_fullscreen = bool(on)
        self.settings["fullscreen"] = self.is_fullscreen
        try:
            self.set_fullscreen(self.is_fullscreen)
        except Exception:
            pass
        self._save_config()

    # ---- extended settings helpers ----
    _RANGE_CFG = {
        "input_latency":    (None, None, 0.01),   # (min, max, step); None = clamp
        "music_volume":     (0.0, 1.0, 0.05),
        "fx_volume":        (0.0, 1.0, 0.05),
        "video_brightness": (0.0, 1.0, 0.05),
        "lane_alpha":       (0.2, 1.0, 0.05),
    }

    def _toggle_setting(self, keyname):
        if keyname == "fullscreen":
            self._apply_fullscreen(not self.is_fullscreen)
            return
        cur = bool(self.settings.get(keyname, False))
        self.settings[keyname] = not cur
        self._save_config()

    def _adjust_range_setting(self, keyname, direction):
        mini, maxi, step = self._RANGE_CFG.get(keyname, (None, None, 0.05))
        cur = float(self.settings.get(keyname, 0.0))
        if keyname == "input_latency":
            # step = 1ms
            cur = round(cur + direction * 0.01, 3)
            cur = max(-0.20, min(0.20, cur))
            self.settings[keyname] = cur
            self.beat_offset = cur
        else:
            cur = round(cur + direction * step, 2)
            if mini is not None:
                cur = max(mini, cur)
            if maxi is not None:
                cur = min(maxi, cur)
            self.settings[keyname] = cur
        self._save_config()
        self._apply_settings_to_playback()

    def _apply_settings_to_playback(self):
        # keep any live players in sync with the current volume settings
        vol = max(0.0, min(1.0, float(self.settings.get("music_volume", 0.9))))
        for p in (getattr(self, 'media_player', None), getattr(self, 'preview_player', None)):
            if p is not None:
                try:
                    p.volume = vol
                except Exception:
                    pass

    # ---- keybinds ----
    def _open_keybinds(self):
        self.keybind_index = 0
        self.binding_target = None
        self.state = "keybinds"

    def _assign_keybind(self, lane, symbol):
        # refuse ESC/B as a bind (used to cancel)
        if symbol in (key.ESCAPE, key.B):
            self.feedback_text = "Keybind cancelled"
            self.feedback_color = (255, 180, 80, 255)
            self.feedback_time = time.time()
            self.binding_target = None
            return
        # drop a bind from another lane if this key is already used (avoid duplicates)
        for other, cfg in LANES.items():
            if other != lane and cfg.get('key') == symbol:
                LANES[other]['key'] = None
        # update the lane mapping and persist
        key_name = key.symbol_string(symbol) if hasattr(key, 'symbol_string') else str(symbol)
        LANES[lane]['key'] = symbol
        LANES[lane]['label'] = key_name
        self._rebuild_key_to_lane()
        self.binding_target = None
        self.feedback_text = f"{lane.upper()} -> {key_name}"
        self.feedback_color = (120, 255, 160, 255)
        self.feedback_time = time.time()
        self._save_keybinds()

    def _rebuild_key_to_lane(self):
        global KEY_TO_LANE
        new_map = {}
        for k, cfg in LANES.items():
            if cfg.get('key') is not None:
                new_map[cfg['key']] = k
        KEY_TO_LANE = new_map

    def _save_keybinds(self):
        try:
            self.settings["keybinds"] = {
                k: (cfg.get('key'), cfg.get('label')) for k, cfg in LANES.items()
            }
            self._save_config()
        except Exception:
            pass

    def _apply_keybinds(self, data):
        # restore lane key+label from saved config
        if not isinstance(data, dict):
            return
        for lane, val in data.items():
            if lane in LANES and isinstance(val, (list, tuple)) and len(val) == 2:
                ksym, label = val
                if isinstance(ksym, int) and ksym > 0:
                    LANES[lane]['key'] = ksym
                    if isinstance(label, str) and label:
                        LANES[lane]['label'] = label
        self._rebuild_key_to_lane()


    def _load_song_colors(self):
        try:
            cf = get_cache_path("__song_colors__", "all")
            if cf.exists():
                import json as _js
                data = _js.load(open(cf, encoding='utf-8'))
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, (list, tuple)) and len(v) == 3:
                            self.song_accents[k] = tuple(int(c) for c in v)
        except Exception:
            pass

    def _save_song_colors(self):
        try:
            cf = get_cache_path("__song_colors__", "all")
            cf.parent.mkdir(parents=True, exist_ok=True)
            import json as _js
            with open(cf, 'w', encoding='utf-8') as f:
                _js.dump(self.song_accents, f)
        except Exception:
            pass

    def _enhance_accent(self, r, g, b):
        # brighten dark content + boost saturation so the accent reads as a vivid "song colour"
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        if lum < 150:
            boost = min(2.2, 170.0 / max(1.0, lum))
            r = min(255, int(r * boost)); g = min(255, int(g * boost)); b = min(255, int(b * boost))
        max_c = max(r, g, b)
        min_c = min(r, g, b)
        if max_c > 0 and (max_c - min_c) < 40:
            avg = (r + g + b) / 3.0
            sat = 1.5
            r = int(max(0, min(255, avg + (r - avg) * sat)))
            g = int(max(0, min(255, avg + (g - avg) * sat)))
            b = int(max(0, min(255, avg + (b - avg) * sat)))
        if max(r, g, b) < 60:
            r = max(r, 90); g = max(g, 90); b = max(b, 90)
        return (r, g, b)

    def _compute_song_accent(self, path):
        # ffmpeg -> one downscaled frame -> average colour (deterministic, works headless)
        try:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", "8", "-i", str(path), "-frames:v", "1",
                   "-vf", "scale=48:27", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
            res = subprocess.run(cmd, capture_output=True)
            raw = res.stdout
            if not raw or len(raw) < 48 * 27 * 3:
                return None
            total = 48 * 27
            r = sum(raw[i] for i in range(0, len(raw), 3))
            # only sample as many complete pixels as decoded
            n = len(raw) // 3
            if n == 0:
                return None
            r = g = b = 0
            for i in range(0, n * 3, 3):
                r += raw[i]
            g = sum(raw[i] for i in range(1, n * 3, 3))
            b = sum(raw[i] for i in range(2, n * 3, 3))
            r, g, b = r // n, g // n, b // n
            if r == 0 and g == 0 and b == 0:
                return None
            return self._enhance_accent(r, g, b)
        except Exception:
            return None

    def _ensure_accent(self, path):
        # return a cached accent or kick off a background ffmpeg computation
        if not path:
            return
        ext = str(path).lower().rsplit('.', 1)[-1] if '.' in str(path) else ''
        if ext in ("mp3", "wav", "ogg", "flac", "m4a"):
            return  # no video frames to derive a colour from
        if path in self.song_accents:
            self.preview_accent = self.song_accents[path]
            return
        if self._accent_path == path:
            return  # already computing this song
        self._accent_path = path

        def worker():
            try:
                col = self._compute_song_accent(path)
            except Exception:
                col = None
            if col:
                self.song_accents[path] = col
                self._save_song_colors()
                if self.preview_path == path:
                    self.preview_accent = col
            if getattr(self, '_accent_path', None) == path:
                self._accent_path = None

        import threading
        threading.Thread(target=worker, daemon=True).start()


    def _set_preview(self, path):
        # (re)load preview player for the currently selected song (seek to ~30%).
        # Falls back gracefully if the media can't be loaded / has no video.
        if path == self.preview_path and self.preview_source is not None:
            return
        self._stop_preview()
        if not path:
            return
        self.preview_path = path
        try:
            src = pyglet.media.load(str(path), streaming=True)
            self.preview_source = src
            self.preview_player = pyglet.media.Player()
            self.preview_player.queue(src)
            self.preview_player.volume = max(0.0, min(1.0, float(self.settings.get("music_volume", 0.9)))) * 0.5
            try:
                dur = float(src.duration) if src.duration else 30.0
                self._preview_seek = min(max(dur * 0.3, 0.0), max(0.0, dur - 3.0))
            except:
                self._preview_seek = 2.0
            self._preview_seek_done = False
            try:
                self.preview_player.play()
            except: pass
            print(f"[preview] loaded {Path(path).name}")
        except Exception as e:
            print(f"[preview] failed {e}")
            self.preview_source = None
            self.preview_player = None

    def _tick_preview(self, dt):
        # called each frame in song_select; seeks preview once then loops
        if self.state != "song_select":
            return
        if self.preview_player is None or self.preview_source is None:
            return
        try:
            if not self._preview_seek_done:
                try:
                    self.preview_player.seek(self._preview_seek)
                    self._preview_seek_done = True
                except:
                    self._preview_seek_done = True
            # loop preview by seeking back when it ends (approx via source duration)
            try:
                dur = float(self.preview_source.duration) if self.preview_source.duration else 0
                pt = 0.0
                try:
                    pt = float(self.preview_player.time) if self.preview_player.time is not None else 0.0
                except: pass
                if dur and pt >= dur - 0.4:
                    self.preview_player.seek(self._preview_seek)
            except: pass
            # advance/pull the video so preview stays at app frame rate (>=30fps)
            try:
                if hasattr(self.preview_player, 'get_texture'):
                    self.preview_player.get_texture()
            except Exception:
                pass
        except Exception:
            pass

    def _draw_song_preview(self, x, y, w, h, song_path):
        # draws the live video preview for the selected song (or a themed placeholder)
        tex = None
        if self.preview_player is not None and self.preview_path == song_path:
            try:
                if hasattr(self.preview_player, 'texture'):
                    tex = self.preview_player.texture
                elif hasattr(self.preview_player, 'get_texture'):
                    tex = self.preview_player.get_texture()
            except:
                tex = None
        if tex is not None and getattr(tex, 'width', 0) > 0:
            try:
                if self.preview_sprite is None:
                    self.preview_sprite = pyglet.sprite.Sprite(tex, x=0, y=0)
                else:
                    try:
                        if self.preview_sprite.image != tex:
                            self.preview_sprite.image = tex
                    except: pass
                scale = max(w / tex.width, h / tex.height)
                self.preview_sprite.scale = scale
                nw = tex.width * scale
                nh = tex.height * scale
                self.preview_sprite.x = x + (w - nw) / 2
                self.preview_sprite.y = y + (h - nh) / 2
                self.preview_sprite.opacity = 255
                # clip to preview panel via a covering draw then overlay
                self.preview_sprite.draw()
                self._ensure_accent(self.preview_path)
                return True
            except Exception:
                pass
        return False

    def _load_song_difficulty_meta(self, path):
        # returns {diff: (rating, beats)} from cached beatmaps (fast, no analysis)
        meta = {}
        for d in DIFFICULTY_ORDER:
            try:
                cp = get_cache_path(path, d)
                if cp.exists():
                    import json as _js
                    data = _js.load(open(cp, encoding='utf-8'))
                    meta[d] = (data.get('rating', 1), len(data.get('beatmap', [])))
            except Exception:
                meta[d] = None
        return meta

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
        self.fc = 0
        self.max_fc = 0
        self.hits = {'perfect': 0, 'good': 0, 'meh': 0, 'miss': 0}
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
        rating = self._analysis_rating if hasattr(self, '_analysis_rating') else density_to_rating(
            len(beatmap)/float(duration) if duration else 0.0)
        self.beatmap = beatmap
        self.duration = duration
        self.media_path = path
        self.beatmap_rating = rating
        # also prepare player
        self._prepare_media_player(path)
        self.reset_play_state()
        self.analysis_progress = 1.0
        # save cache for next time (predetermined)
        try:
            save_cached_beatmap(path, beatmap, duration, tempo, self.sensitivity, self.difficulty, rating=rating)
        except: pass
        self.feedback_text = f"Ready [{self.difficulty.upper()} d{rating}]: {len(beatmap)} beats | ENTER to play | tempo ~{int(tempo)}"
        self.feedback_color = (100, 255, 150, 255)
        self.feedback_time = time.time()
        self.is_media_mode = False
        # if autoplay (from song select), start immediately - but only if still analyzing (not cancelled)
        if autoplay and self.state == "analyzing":
            self.start_media()
        elif self.state == "analyzing":
            # stay in song_select/menu but show ready
            self.state = "song_select" if self.song_files else "menu"

    def _analysis_thread_func(self, path, sensitivity, difficulty, autoplay):
        # runs in background thread - set flag, main thread picks up in update()
        try:
            def prog_cb(p):
                self.analysis_progress = max(0.0, min(1.0, p))
                self.analysis_msg = f"Analysing {Path(path).name} [{difficulty.upper()}] {int(p*100)}%"
            beatmap, duration, tempo, rating = beats_from_media(path, difficulty=difficulty, sensitivity=sensitivity, use_librosa=False, progress_cb=prog_cb)
            self._analysis_result = (beatmap, duration, tempo, rating)
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

    def load_media(self, path, difficulty=None, autoplay=False):
        self._stop_preview()
        if difficulty is None:
            difficulty = self.difficulty
        difficulty = clamp_difficulty(difficulty)
        self.difficulty = difficulty
        if not os.path.exists(path):
            self.feedback_text = f"File not found: {path}"
            self.feedback_color = (255, 80, 80, 255)
            self.feedback_time = time.time()
            return
        # check cache first (predetermined for demo/example)
        p = Path(path)
        if p.name == "_example_beats.wav" and not get_cache_path(path, difficulty).exists():
            try:
                dur = 20.0
                try:
                    import wave
                    with wave.open(str(path), 'rb') as wf:
                        dur = wf.getnframes() / wf.getframerate()
                except:
                    pass
                bpm = 128
                interval = 60.0 / bpm
                beatmap = [(i*interval, LANE_ORDER[i%4]) for i in range(int(dur/interval))]
                save_cached_beatmap(path, beatmap, dur, bpm, self.sensitivity, difficulty, rating=density_to_rating(len(beatmap)/dur if dur else 1.45))
                print(f"[predetermined] _example_beats.wav [{difficulty}] -> {len(beatmap)} beats (instant)")
            except Exception as e:
                print(f"[predetermined] failed {e}")

        cached = load_cached_beatmap(path, sensitivity=self.sensitivity, difficulty=difficulty)
        if cached:
            beatmap, duration, tempo, rating = cached
            self.beatmap_rating = int(rating)
            print(f"[cache] hit {Path(path).name} [{difficulty}] -> {len(beatmap)} beats (instant)")
            self.beatmap = beatmap
            self.duration = duration
            self.media_path = path
            self._prepare_media_player(path)
            self.reset_play_state()
            self.feedback_text = f"Ready (cached) [{difficulty.upper()} d{rating}]: {len(beatmap)} beats | ENTER to play"
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
        self.analysis_msg = f"Analysing {Path(path).name} [{difficulty.upper()}] ..."
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
        print(f"[load] analysing {path} [{self.difficulty}] sensitivity={self.sensitivity} (threaded)")
        # start thread
        t = threading.Thread(target=self._analysis_thread_func, args=(path, self.sensitivity, self.difficulty, autoplay), daemon=True)
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
        # apply persisted input latency to note timing
        try:
            self.beat_offset = float(self.settings.get("input_latency", 0.0))
        except Exception:
            pass
        if self.media_player:
            try:
                self.media_player.seek(0)
                self.media_player.volume = max(0.0, min(1.0, float(self.settings.get("music_volume", 0.9))))
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
                    hit_ang = LANES[lane]['angle']
                    # Clockwise spiral: start 90deg before (predecessor in the CW
                    # chain yellow->red->blue->green->yellow = +90deg) and wind inward.
                    prev_t = self.active_beats[-1]['time'] if self.active_beats else None
                    is_new_section = (
                        prev_t is None or (bt_eff - prev_t) > SECTION_GAP_THRESHOLD
                    )
                    cw = not is_new_section
                    if cw:
                        start_ang = (hit_ang + 90.0) % 360.0
                    else:
                        start_ang = (hit_ang - 90.0) % 360.0
                    self.active_beats.append({
                        'time': bt_eff,
                        'lane': lane,
                        'angle': hit_ang,
                        'start_ang': start_ang,
                        'cw': cw,
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
                    res = self._analysis_result
                    if res and len(res) >= 4:
                        bm, dur, tempo, rating = res
                    else:
                        bm, dur, tempo, rating = ([], 30.0, 120.0, 1)
                    self._analysis_rating = int(rating)
                    self._on_analysis_done(self._analysis_path, bm, dur, tempo, error=None, autoplay=self._analysis_autoplay)
                # clear
                self._analysis_result = None
                self._analysis_error = None
                return
            # keep song count fresh in menu/song_select/difficulty_select
            if self.state in ("menu", "song_select", "difficulty_select"):
                if not hasattr(self, '_last_refresh') or time.time() - self._last_refresh > 1.5:
                    self.song_files = get_songs_in_folder()
                    self._last_refresh = time.time()
            # song-select: animated carousel + live preview for the selected song
            if self.state == "song_select":
                # animate pull-out and carousel offset toward targets
                self.sc_selected_pull += (1.0 - self.sc_selected_pull) * min(1.0, dt * 12)
                target_scroll = +(self.song_index * 112.0)
                self.sc_scroll += (target_scroll - self.sc_scroll) * min(1.0, dt * 10)
                # load preview for selected song (throttled for quick up/down)
                if self.song_files:
                    try:
                        idx = max(0, min(len(self.song_files)-1, self.song_index))
                        sel = self.song_files[idx]
                        if sel != self.preview_path:
                            if not hasattr(self, '_prev_load_t') or time.time() - self._prev_load_t > 0.22:
                                self._prev_load_t = time.time()
                                self._set_preview(str(sel))
                        else:
                            self._tick_preview(dt)
                    except Exception:
                        pass
                else:
                    self._stop_preview()
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
                self._break_fc()
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

    def _play_clickfx(self):
        # play the keypress SFX - fires on EVERY lane key press during a song
        try:
            if getattr(self, '_clickfx_src', None) is None:
                p = Path(__file__).resolve().parent / "SFX" / "clickfx.mp3"
                if not p.exists():
                    return
                self._clickfx_src = pyglet.media.load(str(p), streaming=False)
            src = self._clickfx_src
            if src is None:
                return
            pl = pyglet.media.Player()
            pl.queue(src)
            pl.volume = max(0.0, min(1.0, float(self.settings.get("fx_volume", 0.7))))
            pl.play()
            # keep reference so it doesn't get GC'd before playing
            if not hasattr(self, '_clickfx_players'):
                self._clickfx_players = []
            self._clickfx_players.append(pl)
            # cleanup finished players
            self._clickfx_players = [p for p in self._clickfx_players if p.playing]
        except Exception:
            pass

    def try_hit(self, lane_char):
        if self.state != "playing" or not self.is_playing:
            self.lane_flash[lane_char] = 1.0
            return
        # keypress SFX always plays for this lane press (hit or not)
        self._play_clickfx()
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
            self._break_fc()
            self.feedback_text = "MISS"
            self.feedback_color = (255, 120, 80, 255)
            self.feedback_time = time.time()
            self.lane_flash[lane_char] = 0.9
            return
        if best_delta <= HIT_WINDOW_PERFECT:
            pts = 300
            self.hits['perfect'] += 1
            self.fc += 1
            self.max_fc = max(self.max_fc, self.fc)
            self.feedback_text = "PERFECT!"
            self.feedback_color = (255, 240, 80, 255)
        elif best_delta <= HIT_WINDOW_GOOD:
            pts = 200
            self.hits['good'] += 1
            self._break_fc()
            self.feedback_text = "GOOD"
            self.feedback_color = (100, 255, 150, 255)
        elif best_delta <= HIT_WINDOW_OK:
            pts = 100
            self.hits['meh'] += 1
            self._break_fc()
            self.feedback_text = "MEH"
            self.feedback_color = (100, 200, 255, 255)
        else:
            # too far - don't double-count miss (update will count timeout)
            self.combo = max(0, self.combo - 1)
            self._break_fc()
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

    def _break_fc(self):
        # any non-perfect hit breaks the perfect combo (FC)
        self.fc = 0

    def _max_possible_score(self):
        # theoretical maximum if every beat is a PERFECT hit (300 pts) with the
        # running combo multiplier applied, modelled the same way try_hit scores.
        score = 0
        combo = 0
        n = len(self.beatmap)
        for _ in range(n):
            mult = 1 + min(combo // 8, 4) * 0.25
            score += int(300 * mult)
            combo += 1
        return score or 1

    def _score_pct(self):
        return (self.score / self._max_possible_score()) * 100.0 if self._max_possible_score() > 0 else 0.0

    def grade(self):
        pct = self._score_pct()
        if pct >= 90.0:
            return "A", pct
        if pct >= 70.0:
            return "B", pct
        if pct >= 50.0:
            return "C", pct
        return "D", pct

    # --------------------------------------------------------
    # Input
    # --------------------------------------------------------
    def on_key_press(self, symbol, modifiers):
        # Global F11 for fullscreen
        if symbol == key.F11:
            self._apply_fullscreen(not self.is_fullscreen)
            return
        # ESC exits fullscreen first
        if symbol == key.ESCAPE and self.is_fullscreen:
            self._apply_fullscreen(False)
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
                if sel == "PLAY":
                    self.refresh_song_list()
                    self.state = "song_select"
                    self.song_index = 0
                    self.songs_scroll = 0
                    self.sc_scroll = 0.0
                elif sel == "OPEN FILE":
                    self.open_media_dialog()
                elif sel == "SETTINGS":
                    self.settings_index = 0
                    self.state = "settings"
                elif sel == "QUIT":
                    pyglet.app.exit()
                return
            if symbol == key.ESCAPE:
                pyglet.app.exit()
                return

        elif self.state == "settings":
            nrows = len(self.settings_rows)
            if symbol in (key.UP, key.W):
                self.settings_index = (self.settings_index - 1) % nrows
                return
            if symbol in (key.DOWN, key.S):
                self.settings_index = (self.settings_index + 1) % nrows
                return
            keyname = self.settings_rows[self.settings_index][0]
            row_type = self.settings_rows[self.settings_index][2] if len(self.settings_rows[self.settings_index]) > 2 else "toggle"
            if symbol in (key.ENTER, key.SPACE, key.NUM_ENTER):
                # ENTER: toggle toggles, submenu opens
                if row_type == "toggle":
                    self._toggle_setting(keyname)
                elif row_type == "submenu":
                    if keyname == "keybinds":
                        self._open_keybinds()
                return
            if row_type == "toggle":
                if symbol in (key.LEFT, key.A, key.RIGHT, key.D):
                    self._toggle_setting(keyname)
                    return
            elif row_type == "range":
                # LEFT/A decrease, RIGHT/D increase
                if symbol in (key.LEFT, key.A):
                    self._adjust_range_setting(keyname, -1)
                    return
                if symbol in (key.RIGHT, key.D):
                    self._adjust_range_setting(keyname, +1)
                    return
            if symbol in (key.ESCAPE, key.B):
                self.state = "menu"
                return

        elif self.state == "keybinds":
            if getattr(self, 'binding_target', None) is not None:
                # capture a fresh key for the currently selected lane bind
                self._assign_keybind(self.binding_target, symbol)
                return
            if symbol in (key.UP, key.W):
                self.keybind_index = (self.keybind_index - 1) % 4
                return
            if symbol in (key.DOWN, key.S):
                self.keybind_index = (self.keybind_index + 1) % 4
                return
            if symbol in (key.ENTER, key.SPACE, key.NUM_ENTER):
                self.binding_target = LANE_ORDER[self.keybind_index]
                return
            if symbol in (key.ESCAPE, key.B):
                self.state = "settings"
                self.settings_index = 0
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
                    self.pending_song_path = str(chosen)
                    self.difficulty_index = DIFFICULTY_ORDER.index(self.difficulty) if self.difficulty in DIFFICULTY_ORDER else 0
                    self._stop_preview()
                    self.state = "difficulty_select"
                else:
                    self.feedback_text = "No songs - add files to songs/ folder"
                    self.feedback_color = (255,180,80,255)
                    self.feedback_time = time.time()
                return
            if symbol in (key.LEFT, key.A):
                self.difficulty_index = (self.difficulty_index - 1) % len(self.difficulty_options)
                self.difficulty = self.difficulty_options[self.difficulty_index].lower()
                return
            if symbol in (key.RIGHT, key.D):
                self.difficulty_index = (self.difficulty_index + 1) % len(self.difficulty_options)
                self.difficulty = self.difficulty_options[self.difficulty_index].lower()
                return
            for _qk, _qd in ((key._1,"easy"),(key._2,"medium"),(key._3,"hard")):
                if symbol == _qk:
                    self.difficulty = _qd
                    self.difficulty_index = DIFFICULTY_ORDER.index(_qd)
                    return
            if symbol == key.R:
                self.refresh_song_list()
                self.feedback_text = f"Refreshed - {len(self.song_files)} songs"
                self.feedback_color = (120,220,255,255)
                self.feedback_time = time.time()
                return
            if symbol in (key.ESCAPE, key.B):
                self.state = "menu"
                self._stop_preview()
                return
            if symbol == key.P and self.song_files:
                chosen = self.song_files[self.song_index]
                self.pending_song_path = str(chosen)
                self.difficulty_index = DIFFICULTY_ORDER.index(self.difficulty) if self.difficulty in DIFFICULTY_ORDER else 0
                self._stop_preview()
                self.state = "difficulty_select"
                return

        elif self.state == "difficulty_select":
            if symbol in (key.UP, key.W, key.LEFT, key.A):
                self.difficulty_index = (self.difficulty_index - 1) % len(self.difficulty_options)
                self.difficulty = self.difficulty_options[self.difficulty_index].lower()
                return
            if symbol in (key.DOWN, key.S, key.RIGHT, key.D):
                self.difficulty_index = (self.difficulty_index + 1) % len(self.difficulty_options)
                self.difficulty = self.difficulty_options[self.difficulty_index].lower()
                return
            if symbol in (key.ENTER, key.SPACE, key.NUM_ENTER):
                if self.pending_song_path:
                    diff = self.difficulty_options[self.difficulty_index].lower()
                    self.difficulty = diff
                    self.load_media(self.pending_song_path, difficulty=diff, autoplay=True)
                return
            if symbol in (key.ESCAPE, key.B):
                self.state = "song_select"
                return
            # number keys quick select (1/2/3 = Easy/Medium/Hard)
            for _qk, _qd in ((key._1,"easy"),(key._2,"medium"),(key._3,"hard")):
                if symbol == _qk:
                    self.difficulty = _qd
                    self.difficulty_index = DIFFICULTY_ORDER.index(_qd)
                    if self.pending_song_path:
                        self.load_media(self.pending_song_path, difficulty=_qd, autoplay=True)
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
            # osu-style: PLAY pill at center, OPEN/QUIT below
            cyw = self.height // 2
            centers = [
                (self.width // 2, cyw, 170, 40),        # PLAY
                (self.width // 2, cyw - 130, 150, 22),  # OPEN FILE
                (self.width // 2, cyw - 176, 150, 22),  # SETTINGS
                (self.width // 2, cyw - 222, 150, 22),  # QUIT
            ]
            for idx, (cx, cy, hx, hy) in enumerate(centers):
                if abs(x - cx) < hx and abs(y - cy) < hy:
                    self.menu_index = idx
                    self.on_key_press(key.ENTER, 0)
                    break
        elif self.state == "song_select":
            # carousel cards live on the right; clicking selects that song
            if self.song_files and x > WINDOW_W - 430:
                base_y = self.height // 2
                approx = (base_y + self.sc_scroll - y) / 112.0
                idx = int(round(approx))
                if 0 <= idx < len(self.song_files):
                    self.song_index = idx
                    return
            # difficulty buttons on the left (x 90..410, y centres 500/424/348)
            if self.song_files and 90 <= x <= 410:
                so = []
                for idx in range(len(self.difficulty_options)):
                    so.append((500 - idx * 76, idx))
                for by, idx in so:
                    if abs(y - by) <= 28:
                        self.difficulty_index = idx
                        self.difficulty = self.difficulty_options[idx].lower()
                        return

    # --------------------------------------------------------
    # Drawing helpers
    # --------------------------------------------------------
    def _draw_label(self, text, x, y, size=12, color=(255,255,255,255), anchor_x='left', anchor_y='baseline', font_name='Arial', weight='normal', italic=False):
        # Wrapper to avoid bold kwarg issue + cache for 60fps performance
        # Reuse Label objects per style key to avoid glyph rebuild each frame
        # Cache key must include the TEXT itself: many labels share a style but
        # have different content, and reusing one Label object across different
        # texts would overwrite each other before draw() — making text vanish.
        # (Reuse per unique text+style keeps 60fps: no per-frame construction.)
        key = (font_name, int(size*10), weight, italic, anchor_x, anchor_y, text)
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
            for line in self._lane_line_shapes[lane_key]:
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

    def _spiral_xy(self, cx, cy, b, raw):
        px, py = spiral_point(b['angle'], b.get('cw', True), raw)
        return cx + px, cy + py

    def _draw_spiral_ghost(self, slot, b):
        segs = slot['ghost']
        cx, cy = CENTER
        n = len(segs) + 1
        la = max(0.2, min(1.0, float(getattr(self, '_lane_alpha_mult', 0.85))))
        prev = self._spiral_xy(cx, cy, b, 0.0)
        for i in range(len(segs)):
            q = (i + 1) / (n - 1)
            x, y = self._spiral_xy(cx, cy, b, q)
            ln = segs[i]
            ln.x = prev[0]; ln.y = prev[1]
            ln.x2 = x; ln.y2 = y
            ln.visible = True
            try:
                ln.opacity = int(90 * la)
            except Exception:
                pass
            prev = (x, y)

    def _hide_beat_slot(self, slot):
        slot['circle'].visible = False
        slot['inner'].visible = False
        slot['tail'].visible = False
        slot['hit'].visible = False
        for ln in slot['ghost']:
            ln.visible = False

    def _draw_beats(self, song_t):
        cx, cy = CENTER
        # lane/beat transparency (0..1) scales note opacity
        la = max(0.2, min(1.0, float(self.settings.get("lane_alpha", 0.85))))
        self._lane_alpha_mult = la
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
                    self._hide_beat_slot(slot)
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
                circ.opacity = max(0, int(alpha * la))
                circ.visible = True
                inner_c.visible = False
                tail.visible = False
                hit_circ.visible = False
                for ln in slot['ghost']:
                    ln.visible = False
                pool_idx += 1
                continue
            if b['hit']:
                delta = song_t - b['time'] if self.is_playing else 0
                if delta < 0: delta = 0
                prog = delta / 0.25
                if prog > 1:
                    self._hide_beat_slot(slot)
                    pool_idx += 1
                    continue
                ang = math.radians(b['angle'])
                x = cx + math.cos(ang) * (TARGET_RADIUS + prog*30)
                y = cy + math.sin(ang) * (TARGET_RADIUS + prog*30)
                alpha = int(255 * (1 - prog))
                sz = 28 * (1 - prog*0.6)
                col = LANES[b['lane']]['color']
                hit_circ.x = x; hit_circ.y = y; hit_circ.radius = sz; hit_circ.color = col
                hit_circ.opacity = max(0, int(alpha * la))
                hit_circ.visible = True
                circ.visible = False
                inner_c.visible = False
                tail.visible = False
                for ln in slot['ghost']:
                    ln.visible = False
                pool_idx += 1
                continue
            raw = (song_t - (b['time'] - TRAVEL_TIME)) / TRAVEL_TIME if self.is_playing else 0.0
            if not self.is_playing:
                self._hide_beat_slot(slot)
                pool_idx += 1
                continue
            if raw < 0: raw = 0
            if raw > 1.2:
                self._hide_beat_slot(slot)
                pool_idx += 1
                continue
            x, y = self._spiral_xy(cx, cy, b, raw)
            col = LANES[b['lane']]['color']
            scale = 0.9 + 0.35 * raw
            sz = 22 * scale
            # tail hidden for 60fps (saves 1 shape per beat)
            tail.visible = False
            circ.x = x; circ.y = y; circ.radius = sz; circ.color = col
            circ.opacity = int(255 * la)
            circ.visible = True
            inner_c.visible = False
            hit_circ.visible = False
            # ghost spiral arc: telegraphs counterclockwise / new-section notes
            if not b.get('cw', True):
                self._draw_spiral_ghost(slot, b)
            else:
                for ln in slot['ghost']:
                    ln.visible = False
            pool_idx += 1
        # hide leftover slots that were visible last frame
        for idx in range(pool_idx, prev_count):
            slot = self._beat_pool[idx]
            self._hide_beat_slot(slot)
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
        new_hits = f"P:{self.hits['perfect']}  G:{self.hits['good']}  MEH:{self.hits['meh']}  M:{self.hits['miss']}"
        if self._hud_hits_lbl.text != new_hits:
            self._hud_hits_lbl.text = new_hits
        self._hud_hits_lbl.draw()
        # score meter (top-left) showing score toward the max attainable score
        mx = self._max_possible_score()
        pct = max(0.0, min(1.0, (self.score / mx) if mx > 0 else 0.0))
        mw = int(220 * pct)
        self._hud_meter_bg.y = self.height - 96
        self._hud_meter_bg.width = 220
        self._hud_meter_bg.draw()
        if mw > 0:
            self._hud_meter_fg.y = self.height - 96
            self._hud_meter_fg.width = mw
            self._hud_meter_fg.visible = True
            self._hud_meter_fg.draw()
        # live grade + perfect combo
        gr, _ = self.grade()
        gr_col = {'A': (120, 255, 150), 'B': (140, 220, 255), 'C': (255, 220, 120), 'D': (255, 130, 130)}.get(gr, (255,255,255))
        self._hud_grade_lbl.y = self.height - 112
        gradetxt = f"Grade {gr}"
        if self._hud_grade_lbl.text != gradetxt:
            self._hud_grade_lbl.text = gradetxt
        self._hud_grade_lbl.color = (*gr_col, 255)
        self._hud_grade_lbl.draw()
        self._hud_fc_lbl.y = self.height - 130
        fctxt = f"Perfect combo x{self.fc}"
        if self._hud_fc_lbl.text != fctxt:
            self._hud_fc_lbl.text = fctxt
        self._hud_fc_lbl.draw()
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
            # video brightness drives sprite opacity + overlay darkness (0..1, higher = brighter)
            brt = max(0.0, min(1.0, float(self.settings.get("video_brightness", 0.30))))
            self._video_sprite.opacity = int(60 + brt * 170)   # 60..230
            self._video_sprite.draw()
            # dim overlay for readability (brighter video => lighter dim)
            S = self._shapes
            S.reset()
            S.rect(0, 0, self.width, self.height, (6, 6, 14), radius=0, opacity=int(200 - brt * 160))
            S.draw()
            return True
        except Exception as e:
            # fallback blit
            try:
                tex.blit(0, 0, width=self.width, height=self.height)
                brt = max(0.0, min(1.0, float(self.settings.get("video_brightness", 0.30))))
                S = self._shapes
                S.reset()
                S.rect(0, 0, self.width, self.height, (6, 6, 14), radius=0, opacity=int(200 - brt * 160))
                S.draw()
                return True
            except:
                return False

    # --------------------------------------------------------
    # Static menu background (Backgrounds/bg01.jpeg)
    # --------------------------------------------------------
    def _draw_background(self):
        # Load bg image lazily (once), then draw it scaled to cover the window,
        # dimmed so the UI stays readable.
        try:
            if not hasattr(self, '_bg_sprite') or self._bg_sprite is None:
                cand = Path(__file__).resolve().parent / "Backgrounds" / "bg01.jpeg"
                if not cand.exists():
                    # try any image in Backgrounds/ as a fallback
                    bgd = Path(__file__).resolve().parent / "Backgrounds"
                    if bgd.is_dir():
                        files = sorted(list(bgd.glob("*.jpeg")) + list(bgd.glob("*.jpg")) + list(bgd.glob("*.png")))
                        cand = files[0] if files else None
                if cand is None:
                    return False
                img = pyglet.image.load(str(cand))
                self._bg_texture = img
                self._bg_sprite = pyglet.sprite.Sprite(img)
                self._bg_sprite.opacity = 130
            spr = self._bg_sprite
            timg = self._bg_texture
            base_w = getattr(timg, 'width', spr.width)
            base_h = getattr(timg, 'height', spr.height)
            scale = max(self.width / max(base_w, 1), self.height / max(base_h, 1))
            spr.scale = scale
            new_w = base_w * scale
            new_h = base_h * scale
            spr.x = (self.width - new_w) // 2
            spr.y = (self.height - new_h) // 2
            spr.draw()
            return True
        except Exception:
            return False

    def on_draw(self):
        self.clear()
        # dynamic center for fullscreen
        cx, cy = self.width // 2, self.height // 2
        # static menu background in every menu EXCEPT song select and gameplay
        if self.state in ("menu", "settings", "keybinds", "difficulty_select", "analyzing"):
            self._draw_background()
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
                S = self._shapes
                S.reset()
                S.rect(0, 0, WINDOW_W, WINDOW_H, (0,0,0), radius=0, opacity=120)
                S.draw()
                self._draw_label("PAUSED", x=WINDOW_W//2, y=WINDOW_H//2 + 40, size=36, color=(255,255,255,255), anchor_x='center', anchor_y='center', weight='bold')
                self._draw_label("SPACE to resume  |  ESC for menu", x=WINDOW_W//2, y=WINDOW_H//2 -10, size=12, color=(200,220,255,255), anchor_x='center', anchor_y='center', font_name='Consolas')
                # fall through to HUD? not needed
            elif self.state == "results":
                S = self._shapes
                S.reset()
                S.rect(0, 0, WINDOW_W, WINDOW_H, (0,0,0), radius=0, opacity=130)
                card_w, card_h = 560, 360
                card_x = (WINDOW_W - card_w)//2
                card_y = (WINDOW_H - card_h)//2
                S.rect(card_x, card_y, card_w, card_h, (22,22,34), radius=18)
                S.rect(card_x, card_y, card_w, card_h, (60,60,90), radius=18, opacity=90)
                S.draw()
                # title
                self._draw_label("RESULTS", x=WINDOW_W//2, y=card_y+card_h-40, size=22, color=(255,255,120,255), anchor_x='center', anchor_y='center', weight='bold')
                # grade + score
                gr, pct = self.grade()
                gr_col = {'A': (120, 255, 150), 'B': (140, 220, 255), 'C': (255, 220, 120), 'D': (255, 130, 130)}.get(gr, (255,255,255))
                self._draw_label(f"Grade {gr}", x=WINDOW_W//2, y=card_y+card_h-78, size=26, color=(*gr_col,255), anchor_x='center', anchor_y='center', weight='bold')
                self._draw_label(f"Score  {self.score:06d}    {pct:.0f}% of max", x=WINDOW_W//2, y=card_y+card_h-110, size=15, color=(255,255,255,255), anchor_x='center', anchor_y='center', weight='bold')
                self._draw_label(f"Max Combo  x{self.max_combo}    Perfect Combo  x{self.max_fc}", x=WINDOW_W//2, y=card_y+card_h-136, size=12, color=(180,220,255,255), anchor_x='center', anchor_y='center', font_name='Consolas')
                # hits breakdown
                total = sum(self.hits.values()) or 1
                acc = (self.hits['perfect']*1.0 + self.hits['good']*0.85 + self.hits['meh']*0.6) / total * 100
                self._draw_label(f"PERFECT {self.hits['perfect']}   GOOD {self.hits['good']}   MEH {self.hits['meh']}   MISS {self.hits['miss']}", x=WINDOW_W//2, y=card_y+card_h-170, size=11, color=(220,220,240,255), anchor_x='center', anchor_y='center', font_name='Consolas')
                self._draw_label(f"Accuracy  {acc:.1f}%", x=WINDOW_W//2, y=card_y+card_h-198, size=14, color=(120,255,150,255), anchor_x='center', anchor_y='center', weight='bold')
                if self.media_path:
                    self._draw_label(Path(self.media_path).name, x=WINDOW_W//2, y=card_y+card_h-224, size=9, color=(150,170,200,255), anchor_x='center', anchor_y='center', font_name='Consolas')
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
            S = self._shapes
            S.reset()
            # dim bg + rounded card
            S.rect(0, 0, WINDOW_W, WINDOW_H, (8, 8, 16), radius=0, opacity=175)
            card_w, card_h = 700, 260
            card_x = (WINDOW_W - card_w)//2
            card_y = (WINDOW_H - card_h)//2
            S.rect(card_x, card_y, card_w, card_h, (22, 22, 34), radius=18)
            # progress bar bg + fill (rounded)
            bar_w, bar_h = 520, 18
            bar_x = (WINDOW_W - bar_w)//2
            bar_y = card_y + 90
            S.rect(bar_x, bar_y, bar_w, bar_h, (40, 40, 60), radius=9)
            prog = max(0.0, min(1.0, self.analysis_progress))
            fill_w = int(bar_w * prog)
            if fill_w > 0:
                S.rect(bar_x, bar_y, fill_w, bar_h, (100, 220, 160), radius=9)
            S.draw()
            # shimmer (transient, on top)
            if 0.02 < prog < 0.99 and fill_w > 3:
                shimmer_w = 40
                t = time.time() * 2.5
                shimmer_x = bar_x + (int((t % 2.0) * (bar_w + shimmer_w)) - shimmer_w)
                sx = max(bar_x, min(bar_x+fill_w - shimmer_w, shimmer_x))
                sh = pyglet.shapes.Rectangle(sx, bar_y, shimmer_w, bar_h, color=(160, 255, 190))
                sh.opacity = 90
                sh.draw()
            # title
            self._draw_label("ANALYSING", x=WINDOW_W//2, y=card_y+card_h-40, size=22, weight='bold', color=(255, 220, 100, 255), anchor_x='center', anchor_y='center')
            fname = Path(self.media_path).name if self.media_path else "song"
            if len(fname) > 48:
                fname = fname[:45] + "..."
            self._draw_label(fname, x=WINDOW_W//2, y=card_y+card_h-80, size=11, color=(180, 180, 210, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            self._draw_label(self.analysis_msg or "Extracting beats...", x=WINDOW_W//2, y=card_y+card_h-110, size=10, color=(140, 160, 190, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            self._draw_label(f"{int(prog*100)}%", x=WINDOW_W//2, y=bar_y+bar_h//2, size=10, weight='bold', color=(255, 255, 255, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            # cached hint
            self._draw_label("First load analyses via ffmpeg • next load is instant (cached)", x=WINDOW_W//2, y=card_y+45, size=9, color=(110, 120, 150, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            self._draw_label("ESC to cancel", x=WINDOW_W//2, y=card_y+22, size=9, color=(140, 140, 170, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            # also draw video preview dim if available? skip
            return

        if self.state == "menu":
            # ---- osu!-style main menu ----
            cxw, cyw = self.width // 2, self.height // 2
            t = time.time()
            S = self._shapes
            S.reset()
            # background vignette
            S.rect(0, 0, self.width, self.height, (8, 8, 16), radius=0, opacity=0)
            # faint converging-beat ring motif behind logo (drawn first, under all UI)
            for i, lane in enumerate(LANE_ORDER):
                col = LANES[lane]['color']
                cc = pyglet.shapes.Circle(cxw, cyw + 40, 150 - i*18, color=col)
                cc.opacity = int(16 + 12 * (0.5 + 0.5 * math.sin(t * 1.4 + i)))
                cc.draw()

            # ---- shapes first (background + buttons) ----
            # primary PLAY pill button (rounded)
            play_w, play_h = 320, 72
            px, py = cxw - play_w // 2, cyw - play_h // 2
            play_sel = self.menu_index == 0
            glow_op = int(60 + 30 * (0.5 + 0.5 * math.sin(t * 3.0)))
            if play_sel:
                S.rect(px - 12, py - 12, play_w + 24, play_h + 24, (100, 255, 160), radius=16, opacity=glow_op)
            S.rect(px, py, play_w, play_h, (70, 90, 130) if play_sel else (34, 42, 64), radius=12)
            S.rect(px, py, 10, play_h, (100, 255, 160), radius=5)
            # secondary buttons: OPEN FILE / SETTINGS / QUIT (rounded)
            for idx in (1, 2, 3):
                wy = cyw - 130 - (idx - 1) * 46
                selected = idx == self.menu_index
                w = 240
                S.rect(cxw - w // 2, wy - 18, w, 36, (46, 56, 84) if selected else (24, 28, 44), radius=10)
                if selected:
                    S.rect(cxw - w // 2, wy - 18, 6, 36, (120, 180, 255), radius=3)
            # all shapes drawn now, UNDER the text
            S.draw()

            # ---- text on top (shapes already drawn) ----
            # logo
            self._menu_title_lbl.x = cxw
            self._menu_title_lbl.y = cyw + 150
            self._menu_title_lbl.draw()
            # sub
            self._menu_sub_lbl.x = cxw
            self._menu_sub_lbl.y = cyw + 112
            self._menu_sub_lbl.draw()
            # song count hint
            self._menu_songs_hint_lbl.text = f"{len(self.song_files)} track(s) in  ./songs/  •  press O to open an external file"
            self._menu_songs_hint_lbl.x = cxw
            self._menu_songs_hint_lbl.y = cyw + 86
            self._menu_songs_hint_lbl.draw()
            # PLAY label (on the play pill)
            play_lbl = self._menu_option_lbls[0]
            play_lbl.text = "PLAY"
            play_lbl.x = cxw
            play_lbl.y = cyw
            play_lbl.font_size = 30
            play_lbl.color = (255, 255, 255, 255) if play_sel else (200, 215, 245, 255)
            play_lbl.draw()
            # secondary button labels
            for idx in (1, 2, 3):
                opt = self.menu_options[idx]
                wy = cyw - 130 - (idx - 1) * 46
                selected = idx == self.menu_index
                wlbl = self._menu_option_lbls[idx]
                wlbl.text = opt
                wlbl.x = cxw
                wlbl.y = wy
                wlbl.font_size = 15
                wlbl.color = (255, 255, 140, 255) if selected else (180, 190, 220, 255)
                wlbl.draw()
            # footer
            self._menu_footer_lbl.x = cxw
            self._menu_footer_lbl.y = 70
            self._menu_footer_lbl.draw()
            # lane legend
            for i, lane in enumerate(LANE_ORDER):
                c = self._menu_lane_circles[i]
                xs = cxw - 160 + i * 90
                c.x = xs + 16
                c.y = 30
                c.draw()
                lbl = self._menu_lane_lbls[i]
                lbl.x = xs + 34
                lbl.y = 30
                lbl.text = lane.upper()
                lbl.draw()
            return

        if self.state == "settings":
            # ---- settings screen (osu!-style) ----
            scx, scy = self.width // 2, self.height // 2
            S = self._shapes
            S.reset()
            S.rect(0, 0, self.width, self.height, (8, 8, 16), radius=0, opacity=0)
            # faint accent ring motif
            t = time.time()
            for i, lane in enumerate(LANE_ORDER):
                col = LANES[lane]['color']
                cc = pyglet.shapes.Circle(scx, scy + 60, 170 - i * 22, color=col)
                cc.opacity = int(14 + 10 * (0.5 + 0.5 * math.sin(t * 1.4 + i)))
                cc.draw()
            # layout for a variable number of rows
            nrows = len(self.settings_rows)
            row_pitch = 52
            list_top = scy + 66
            card_pad = 24
            card_top = list_top + card_pad
            card_bottom = list_top - (nrows - 1) * row_pitch - card_pad
            card_h = card_top - card_bottom
            card_w = 600
            card_x = scx - card_w // 2
            card_y = card_bottom
            # settings card (rounded)
            S.rect(card_x, card_y, card_w, card_h, (20, 24, 38), radius=16)
            # row boxes (shapes first, under text)
            for i, row in enumerate(self.settings_rows):
                ry = list_top - i * row_pitch
                selected = i == self.settings_index
                S.rect(card_x + 24, ry - 21, card_w - 48, 42,
                       (44, 58, 92) if selected else (26, 30, 46), radius=12)
                if selected:
                    S.rect(card_x + 24, ry - 21, 6, 42, (100, 255, 160), radius=3)
            # all shapes now, UNDER the text
            S.draw()

            # ---- text on top ----
            self._draw_label("SETTINGS", x=scx, y=scy + 190, size=34, color=(255, 255, 255, 255), anchor_x='center', anchor_y='center', weight='bold')
            for i, row in enumerate(self.settings_rows):
                keyname, label = row[0], row[1]
                row_type = row[2] if len(row) > 2 else "toggle"
                ry = list_top - i * row_pitch
                selected = i == self.settings_index
                # value for this setting (per row type)
                if row_type == "toggle":
                    val = "ON" if self.is_fullscreen else "OFF"
                    vcol = (140, 230, 160, 255) if val == "ON" else (200, 130, 130, 255)
                elif row_type == "submenu":
                    val = "OPEN >"
                    vcol = (140, 180, 255, 255)
                else:  # range
                    if keyname == "input_latency":
                        ms = int(round(float(self.settings.get(keyname, 0.0)) * 1000.0))
                        val = f"{ms:+d} ms"
                        vcol = (180, 200, 230, 255)
                    else:
                        pct = int(round(float(self.settings.get(keyname, 0.0)) * 100.0))
                        val = f"{pct}%"
                        vcol = (140, 230, 160, 255)
                self._draw_label(label, x=card_x + 60, y=ry, size=18,
                                 color=(255, 255, 255, 255) if selected else (200, 210, 235, 255),
                                 anchor_x='left', anchor_y='center', weight='bold' if selected else 'normal')
                self._draw_label(val, x=card_x + card_w - 40, y=ry, size=18,
                                 color=vcol,
                                 anchor_x='right', anchor_y='center', weight='bold')

            self._draw_label("UP/DOWN : navigate        ENTER / LEFT / RIGHT : change        B / ESC : back", x=scx, y=card_bottom - 14, size=10, color=(140, 150, 180, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            self._draw_label("(F11 also toggles fullscreen anytime)", x=scx, y=card_bottom - 34, size=9, color=(100, 110, 140, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            return

        if self.state == "keybinds":
            # ---- keybind remapping screen ----
            scx, scy = self.width // 2, self.height // 2
            S = self._shapes
            S.reset()
            S.rect(0, 0, self.width, self.height, (8, 8, 16), radius=0, opacity=0)
            t = time.time()
            for i, lane in enumerate(LANE_ORDER):
                col = LANES[lane]['color']
                cc = pyglet.shapes.Circle(scx, scy + 60, 170 - i * 22, color=col)
                cc.opacity = int(14 + 10 * (0.5 + 0.5 * math.sin(t * 1.4 + i)))
                cc.draw()
            card_w, card_h = 560, 300
            card_x = (self.width - card_w) // 2
            card_y = (self.height - card_h) // 2
            S.rect(card_x, card_y, card_w, card_h, (20, 24, 38), radius=16)
            lane_rows = []
            for idxp, lane in enumerate(LANE_ORDER):
                ry = scy + 20 - idxp * 66
                cfg = LANES[lane]
                col = cfg['color']
                lane_rows.append((lane, ry, cfg, col))
                selected = idxp == self.keybind_index
                S.rect(card_x + 30, ry - 26, card_w - 60, 52,
                       (55, 55, 90) if selected else (28, 28, 42), radius=12)
                if selected:
                    S.rect(card_x + 30, ry - 26, 6, 52, (100, 255, 160), radius=3)
            S.draw()
            self._draw_label("KEYBINDS", x=scx, y=scy + 185, size=30, color=(255, 255, 255, 255), anchor_x='center', anchor_y='center', weight='bold')
            for (lane, ry, cfg, col) in lane_rows:
                selected = lane == self.binding_target or LANE_ORDER.index(lane) == self.keybind_index
                # lane dot colour chip
                dot = pyglet.shapes.Circle(card_x + 58, ry, 7, color=col)
                dot.draw()
                self._draw_label(lane.upper(), x=card_x + 78, y=ry + 6, size=18, color=(240, 240, 255, 255), anchor_x='left', anchor_y='center', weight='bold')
                self._draw_label(f"lane {LANE_ORDER.index(lane)+1} of 4", x=card_x + 78, y=ry - 14, size=9, color=(150, 160, 190, 255), anchor_x='left', anchor_y='center', font_name='Consolas')
                # current key label (right side)
                klab = cfg.get('label') or "?"
                if self.binding_target == lane:
                    ktext = "Press a key..."
                    kcol = (255, 220, 120, 255)
                else:
                    ktext = klab
                    kcol = (140, 230, 160, 255) if selected else (180, 190, 215, 255)
                self._draw_label(ktext, x=card_x + card_w - 40, y=ry, size=20, color=kcol, anchor_x='right', anchor_y='center', weight='bold')
            self._draw_label("UP/DOWN select      ENTER rebind      B / ESC back", x=scx, y=card_y + 24, size=10, color=(140, 150, 180, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            if self.feedback_text and (time.time() - self.feedback_time) < 3.0:
                age = time.time() - self.feedback_time
                alpha = int(180 * (1 - age/3.0))
                self._draw_label(self.feedback_text, x=scx, y=card_y - 30, size=10, color=(*self.feedback_color[:3], max(0, alpha)), anchor_x='center', anchor_y='center', font_name='Consolas')
            return

        if self.state == "song_select":
            # ---- osu!-style song select: full-bleed preview + vertical carousel ----
            cx = WINDOW_W // 2
            base_y = WINDOW_H // 2
            sel = None
            if self.song_files:
                try:
                    si = max(0, min(len(self.song_files)-1, self.song_index))
                    sel = self.song_files[si]
                except Exception:
                    sel = None
            S = self._shapes
            S.reset()
            # 1) full-bleed dimmed preview of the selected song (live from preview player)
            if sel is not None:
                shot = self._draw_song_preview(0, 0, WINDOW_W, WINDOW_H, str(sel))
                if not shot:
                    # themed placeholder gradient-ish (layered rounded rects)
                    for i in range(5):
                        S.rect(0, 0, WINDOW_W, WINDOW_H, (10 + i*4, 12 + i*4, 26), radius=0)
            else:
                S.rect(0, 0, WINDOW_W, WINDOW_H, (10, 10, 18), radius=0)
            # dim overlay for readability
            S.rect(0, 0, WINDOW_W, WINDOW_H, (6, 6, 16), radius=0, opacity=170)
            S.draw()

            if not self.song_files:
                self._draw_label("No songs found.", x=cx, y=base_y + 40, size=18, color=(255, 220, 120, 255), anchor_x='center', anchor_y='center', weight='bold')
                self._draw_label("Drop mp4 / mp3 / wav / m4a / ogg / flac into  songs/  then press  R", x=cx, y=base_y - 5, size=11, color=(190, 200, 225, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
                self._draw_label("or press  O  to open a file outside  songs/", x=cx, y=base_y - 30, size=10, color=(130, 145, 175, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
                self._draw_label("B / ESC : back", x=cx, y=40, size=10, color=(150, 160, 190, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
                return

            # ---- left column: selected song details + difficulty selector ----
            acc_r, acc_g, acc_b = self.preview_accent
            diff_desc = {k: DIFFICULTY_PROFILES[k][6] for k in DIFFICULTY_ORDER}
            name = sel.name
            disp = name if len(name) <= 30 else name[:27] + "..."
            # subtitle: size / ext / cached count
            sub_parts = []
            try:
                sz_mb = sel.stat().st_size / (1024*1024)
                sub_parts.append(f"{sz_mb:.1f} MB")
            except: pass
            sub_parts.append(sel.suffix.lstrip('.').upper())
            meta = {}
            try:
                meta = self._load_song_difficulty_meta(str(sel))
            except: pass
            if meta:
                cached = sum(1 for v in meta.values() if v)
                sub_parts.append(f"{cached}/3 beatmaps")

            # difficulty buttons (rounded) - shapes first
            btn_w, btn_h = 320, 56
            bys = [500, 500 - 76, 500 - 152]
            for idx, opt in enumerate(self.difficulty_options):
                by = bys[idx]
                selected_d = idx == self.difficulty_index
                if selected_d:
                    bcol = (min(255,acc_r), min(255,acc_g), min(255,acc_b))
                else:
                    bcol = (26, 32, 52)
                S.rect(90, by - btn_h//2, btn_w, btn_h, bcol, radius=14)
                if selected_d:
                    # accent left edge + chevron for the active difficulty
                    S.rect(90, by - btn_h//2, 8, btn_h, (255, 255, 255), radius=4)
                    tri = pyglet.shapes.Triangle(90 + btn_w + 2, by + 10, 90 + btn_w + 2, by - 10, 90 + btn_w + 16, by, color=bcol)
                    tri.draw()

            # ---- right: vertical carousel of song cards (selected pulled out) ----
            play_pull = self.sc_selected_pull  # 0..1
            vis_half = 4
            i0 = max(0, self.song_index - vis_half)
            i1 = min(len(self.song_files) - 1, self.song_index + vis_half)
            cw, ch = 330, 92
            carousel = []  # (p, cy, x0, y0, w, h, selected, alpha)
            for i in range(i0, i1 + 1):
                p = self.song_files[i]
                rel = i - self.song_index
                cy = base_y + self.sc_scroll - i * 112.0
                selected = i == self.song_index
                if selected:
                    scale = 1.0 + 0.16 * play_pull
                    xoff = -46 * play_pull
                    alpha = 255
                else:
                    scale = 1.0
                    xoff = 0.0
                    dist = abs(rel)
                    alpha = max(55, 168 - dist * 50)
                w = cw * scale
                h = ch * scale
                x0 = WINDOW_W - w - 46 + xoff
                y0 = cy - h / 2
                # card panel (selected tinted with the song's accent color, rounded)
                if selected:
                    col_bg = (max(28, acc_r//3), max(28, acc_g//3), max(28, acc_b//3))
                else:
                    col_bg = (24, 30, 50)
                S.rect(x0, y0, w, h, col_bg, radius=16, opacity=alpha)
                if selected:
                    # rounded accent edge bar using the song's color
                    S.rect(x0 + 6, y0 + 6, 7, h - 12, (acc_r, acc_g, acc_b), radius=4, opacity=alpha)
                    S.rect(x0 + 6, y0 + 6, w - 12, 3, (acc_r, acc_g, acc_b), radius=2, opacity=alpha)
                    S.rect(x0 + 6, y0 + h - 9, w - 12, 3, (acc_r, acc_g, acc_b), radius=2, opacity=alpha)
                carousel.append((p, cy, x0, y0, w, h, selected, alpha))
            # all shapes (difficulty buttons + carousel cards) now, UNDER text
            S.draw()

            # ---- text on top ----
            # left: song name
            self._draw_label(disp, x=90, y=WINDOW_H - 108, size=30, color=(255, 255, 255, 255), anchor_x='left', anchor_y='center', weight='bold')
            self._draw_label("  •  ".join(sub_parts), x=90, y=WINDOW_H - 142, size=11, color=(180, 195, 220, 255), anchor_x='left', anchor_y='center', font_name='Consolas')
            # difficulty button labels
            for idx, opt in enumerate(self.difficulty_options):
                by = bys[idx]
                selected_d = idx == self.difficulty_index
                tcol = (255, 255, 255, 255) if selected_d else (205, 212, 232, 255)
                self._draw_label(opt, x=118, y=by + 6, size=18, color=tcol, anchor_x='left', anchor_y='center', weight='bold')
                # difficulty description + rating
                m = meta.get(opt.lower())
                if m and m[0]:
                    r, bts = m
                    detail = f"d{r}  •  {bts} beats"
                else:
                    detail = diff_desc.get(opt.lower(), "")
                self._draw_label(detail, x=118, y=by - 16, size=10, color=(235, 235, 255, 255) if selected_d else (150, 162, 190, 255), anchor_x='left', anchor_y='center', font_name='Consolas')
            # hint under difficulty buttons
            self._draw_label("1 / 2 / 3  or  LEFT / RIGHT : difficulty      •      ENTER : play this song", x=90, y=306, size=10, color=(150, 165, 195, 255), anchor_x='left', anchor_y='center', font_name='Consolas')
            # carousel card labels
            for (p, cy, x0, y0, w, h, selected, alpha) in carousel:
                nm = p.name
                if len(nm) > 27:
                    nm = nm[:24] + "..."
                tcol = (255, 255, 255, 255) if selected else (205, 214, 238, 255)
                self._draw_label(nm, x=x0 + 18, y=cy + 12, size=14, color=tcol, anchor_x='left', anchor_y='center', weight='bold' if selected else 'normal')
                try:
                    s2 = f"{p.stat().st_size/(1024*1024):.1f} MB"
                except:
                    s2 = p.suffix.lstrip('.').upper()
                self._draw_label(f"{p.suffix.lstrip('.').upper()}  •  {s2}", x=x0 + 18, y=cy - 20, size=9, color=(148, 162, 192, 255), anchor_x='left', anchor_y='center', font_name='Consolas')
            # scroll handle hint (index / total)
            self._draw_label(f"{self.song_index+1} / {len(self.song_files)}", x=WINDOW_W - 46, y=WINDOW_H - 42, size=12, color=(215, 222, 240, 255), anchor_x='right', anchor_y='center', weight='bold', font_name='Consolas')

            # bottom bar
            self._draw_label("UP / DOWN  browse      •      LEFT / RIGHT  difficulty      •      ENTER  play      •      R  refresh      •      B / ESC  back", x=cx, y=24, size=10, color=(150, 160, 190, 255), anchor_x='center', anchor_y='center', font_name='Consolas')
            # feedback
            if self.feedback_text and (time.time() - self.feedback_time) < 3.0:
                age = time.time() - self.feedback_time
                alpha = int(180 * (1 - age/3.0))
                self._draw_label(self.feedback_text, x=cx, y=64, size=11, color=(*self.feedback_color[:3], max(0, alpha)), anchor_x='center', anchor_y='center', font_name='Consolas')
            return


        if self.state == "difficulty_select":
            S = self._shapes
            S.reset()
            S.rect(0, 0, WINDOW_W, WINDOW_H, (8, 8, 16), radius=0, opacity=175)
            song_name = Path(self.pending_song_path).name if self.pending_song_path else "Unknown"
            if len(song_name) > 55:
                song_name = song_name[:52] + "..."
            # panel - tall enough for 3 difficulty rows (rounded)
            panel_x, panel_y, panel_w, panel_h = 320, 200, 640, 300
            S.rect(panel_x, panel_y, panel_w, panel_h, (18,18,30), radius=18)
            # options (EASY / MEDIUM / HARD) - shapes first
            rows = []
            for idx, opt in enumerate(self.difficulty_options):
                y = 470 - idx*100
                selected = idx == self.difficulty_index
                # box (70 tall, rounded)
                S.rect(panel_x+30, y-35, panel_w-60, 70, (55,55,90) if selected else (28,28,42), radius=14)
                if selected:
                    S.rect(panel_x+30, y-35, 6, 70, (100,255,160), radius=3)
                rows.append((idx, opt, y, selected))
            # all shapes now, UNDER the text
            S.draw()

            # ---- text on top ----
            self._draw_label("CHOOSE DIFFICULTY", x=WINDOW_W//2, y=WINDOW_H - 70, size=26, color=(255,255,255,255), anchor_x='center', anchor_y='center', weight='bold')
            self._draw_label(song_name, x=WINDOW_W//2, y=WINDOW_H - 105, size=11, color=(140,200,255,255), anchor_x='center', anchor_y='center', font_name='Consolas')
            for (idx, opt, y, selected) in rows:
                # label
                col = (255,255,140,255) if selected else (220,220,240,255)
                self._draw_label(opt, x=panel_x+60, y=y+12, size=18, color=(*col[:3],255), anchor_x='left', anchor_y='center', weight='bold')
                # difficulty profile desc + target rating marker
                prof = DIFFICULTY_PROFILES.get(opt.lower())
                desc = prof[6] if prof else ""
                dcol = (180,255,180,255) if selected else (160,160,190,255)
                self._draw_label(desc, x=panel_x+60, y=y-16, size=9, color=(*dcol[:3],255), anchor_x='left', anchor_y='center', font_name='Consolas')
                # target rating marker on the right (monostar 1..20 style)
                if prof:
                    mk = rating_marker(density_to_rating(prof[3]))
                    mcol = (255,220,120,255) if selected else (170,170,200,255)
                    self._draw_label(f"~{mk}", x=panel_x+panel_w-40, y=y+12, size=15, color=(*mcol[:3],255), anchor_x='right', anchor_y='center', weight='bold')
                # beats preview if cached (below desc)
                try:
                    p = self.pending_song_path
                    if p:
                        cp = get_cache_path(p, opt.lower())
                        if cp.exists():
                            import json as _js
                            d=_js.load(open(cp,encoding='utf-8'))
                            cr = d.get('rating', 1)
                            self._draw_label(f"d{cr} • {len(d.get('beatmap',[]))} beats", x=panel_x+panel_w-40, y=y-16, size=9, color=(120,200,120,255), anchor_x='right', anchor_y='center', font_name='Consolas')
                except:
                    pass
            self._draw_label("UP/DOWN choose • ENTER confirm • 1/2/3 quick • ESC back", x=WINDOW_W//2, y=panel_y+14, size=9, color=(120,140,170,255), anchor_x='center', anchor_y='center', font_name='Consolas')
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
