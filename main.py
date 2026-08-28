"""
Radial Rhythm Game - Pyglet
Beats converge from outside -> centre.
4 lanes: D (red, left), F (blue, up), J (green, right), K (yellow, down)
- Provide an MP4 to auto-sync beats (ffmpeg + numpy onset detection, or librosa if available)
- No song -> demo pattern

Controls:
  O - open MP4 / video / audio file
  SPACE - play demo pattern
  P - play loaded media (if paused)
  D/F/J/K - hit beats when they reach centre ring
  +/- or [/] - adjust audio sensitivity
  ESC - quit
"""

import math
import os
import sys
import time
import tempfile
import subprocess
import wave
import struct
from pathlib import Path

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
TRAVEL_TIME = 1.6  # seconds for beat to travel spawn -> centre
HIT_WINDOW_PERFECT = 0.13
HIT_WINDOW_GOOD = 0.26
HIT_WINDOW_OK = 0.35  # beyond = miss

LANES = {
    'd': {'angle': 180, 'color': (255, 74, 74),  'key': key.D, 'label': 'D'},
    'f': {'angle': 90,  'color': (74, 144, 255), 'key': key.F, 'label': 'F'},
    'j': {'angle': 0,   'color': (74, 255, 138), 'key': key.J, 'label': 'J'},
    'k': {'angle': 270, 'color': (255, 215, 74), 'key': key.K, 'label': 'K'},
}
LANE_ORDER = ['d', 'f', 'j', 'k']
KEY_TO_LANE = {v['key']: k for k, v in LANES.items()}
# also allow lowercase char mapping via on_text
CHAR_TO_LANE = {'d': 'd', 'f': 'f', 'j': 'j', 'k': 'k'}

# ------------------------------------------------------------
# Demo pattern
# ------------------------------------------------------------
def generate_demo_pattern(bpm=128, bars=16, duration=None):
    """
    Pre-planned test pattern.
    If duration given, fill that length; otherwise use bars.
    """
    beat_interval = 60.0 / bpm
    pattern = []
    # bar-level ideas: 4 beats per bar
    # Create interesting but testable pattern:
    # bar 0-2: steady quarter notes alternating
    # bar 3-4: eighths
    # bar 5: syncopation
    # repeat
    t = 0.0
    # 16 bars = 64 beats at 128bpm = 30 seconds
    total_beats = bars * 4
    if duration:
        total_beats = int(duration / beat_interval) + 4

    for i in range(total_beats):
        bar = i // 4
        pos_in_bar = i % 4
        # choose lanes
        if bar % 4 == 0:
            # cycle D-F-J-K
            lane = LANE_ORDER[pos_in_bar % 4]
            pattern.append((t, lane))
        elif bar % 4 == 1:
            # eighths on half beats
            lane = LANE_ORDER[(i) % 4]
            pattern.append((t, lane))
            # add offbeat
            if pos_in_bar in (1, 3):
                off = t + beat_interval * 0.5
                lane2 = LANE_ORDER[(i + 2) % 4]
                pattern.append((off, lane2))
        elif bar % 4 == 2:
            # doubles
            lane = LANE_ORDER[pos_in_bar % 2 * 2 + (bar % 2)]
            pattern.append((t, lane))
            if pos_in_bar % 2 == 0:
                pattern.append((t + beat_interval * 0.25, LANE_ORDER[(pos_in_bar+1)%4]))
                pattern.append((t + beat_interval * 0.5, LANE_ORDER[(pos_in_bar+2)%4]))
        else:
            # randomish but deterministic
            lane = LANE_ORDER[(i * 3) % 4]
            pattern.append((t, lane))
        t += beat_interval

    # add a final flourish of 8 quick notes
    for i in range(8):
        pattern.append((t + i * beat_interval * 0.5, LANE_ORDER[i % 4]))

    pattern = sorted(pattern, key=lambda x: x[0])
    # quantize slightly and deduplicate close times
    filtered = []
    for tm, ln in pattern:
        if filtered and abs(tm - filtered[-1][0]) < 0.08 and ln == filtered[-1][1]:
            continue
        filtered.append((tm, ln))
    return filtered

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
        # normalize to -1..1
        if sampwidth == 2:
            audio /= 32768.0
        elif sampwidth == 4:
            audio /= 2147483648.0
        return framerate, audio

def detect_beats_energy(sr, audio, sensitivity=1.0):
    """
    Lightweight onset / beat detection without librosa.
    Returns list of times in seconds.
    """
    # Hop and window for envelope
    hop = 512
    n_frames = 1 + (len(audio) - 1024) // hop
    if n_frames <= 0:
        return []
    # compute RMS energy per frame
    energies = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        start = i * hop
        chunk = audio[start:start+1024]
        # pre-emphasis + window could help, but simple RMS
        energies[i] = np.sqrt(np.mean(chunk * chunk) + 1e-10)

    # optional spectral flux would be better; RMS with adaptive threshold is OK
    # smooth slightly
    kernel = np.ones(3)/3
    energies_smooth = np.convolve(energies, kernel, mode='same')

    # adaptive threshold
    # local mean over ~1 sec window (~86 frames at 512 hop, 44.1k)
    win = int(0.6 * sr / hop)  # ~51 frames
    if win < 5:
        win = 5
    # compute moving average via convolution
    local_mean = np.convolve(energies_smooth, np.ones(win)/win, mode='same')
    local_std = np.zeros_like(local_mean)
    # quick local std via sliding
    # for speed use uniform approximation: we compute with stride
    for i in range(len(energies_smooth)):
        lo = max(0, i - win//2)
        hi = min(len(energies_smooth), i + win//2)
        local_std[i] = np.std(energies_smooth[lo:hi])

    global_mean = np.mean(energies_smooth)
    # threshold factor tuned; sensitivity scales it
    # higher sensitivity -> lower threshold -> more beats
    base_factor = 1.55 - (sensitivity - 1.0) * 0.35  # 1.0 => 1.55, 1.5 => 1.37 etc
    base_factor = np.clip(base_factor, 1.15, 2.0)
    offset = np.clip(0.12 - (sensitivity-1.0)*0.04, 0.04, 0.18)
    threshold = local_mean * base_factor + local_std * 0.35 + offset * 0.1
    # also ensure above global mean * factor
    threshold = np.maximum(threshold, global_mean * (1.1 - (sensitivity-1.0)*0.15))

    # peak picking
    min_dist_frames = int(0.18 * sr / hop)  # 180ms min between beats
    if min_dist_frames < 4:
        min_dist_frames = 4
    peaks = []
    last_peak = -min_dist_frames*2
    for i in range(2, len(energies_smooth)-2):
        if i - last_peak < min_dist_frames:
            continue
        e = energies_smooth[i]
        if e > threshold[i] and e >= energies_smooth[i-1] and e >= energies_smooth[i+1]:
            # check is local max in +/-2
            if e >= np.max(energies_smooth[i-2:i+3]):
                peaks.append(i)
                last_peak = i
    times = [p * hop / sr for p in peaks]
    return times

def beats_from_media(media_path, sensitivity=1.0, use_librosa=True):
    """
    Try librosa if available and requested, otherwise fallback to ffmpeg+numpy.
    Returns list of (time, lane) and duration estimate.
    """
    # Attempt librosa path first if available
    if use_librosa:
        try:
            import librosa
            y, sr = librosa.load(str(media_path), sr=22050, mono=True)
            tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units='time')
            # also add onset detection for denser map
            # if too sparse, supplement with onsets
            if len(beat_frames) < 20:
                onset_env = librosa.onset.onset_strength(y=y, sr=sr)
                onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, units='time')
                beat_frames = sorted(set(list(beat_frames) + list(onsets)))
            # filter very close
            filtered = []
            for t in sorted(beat_frames):
                if not filtered or t - filtered[-1] > 0.14:
                    filtered.append(float(t))
            # estimate duration
            duration = librosa.get_duration(y=y, sr=sr)
            # assign lanes cyclically with some variation by energy
            # use energy to choose lane brightness / lane index
            lane_pattern = []
            for idx, t in enumerate(filtered):
                # pick lane by beat strength or just cycle through different schemes
                # for musicality: strong beats on D/J, weak on F/K
                if idx % 8 < 2:
                    lane = LANE_ORDER[idx % 4]
                elif idx % 4 == 2:
                    lane = LANE_ORDER[(idx*2) % 4]
                else:
                    lane = LANE_ORDER[idx % 4]
                lane_pattern.append((float(t), lane))
            return lane_pattern, float(duration), float(tempo) if hasattr(tempo, '__float__') else 120.0
        except ImportError:
            pass
        except Exception as e:
            print(f"[librosa] failed {e}, falling back to numpy method")

    # fallback: ffmpeg + numpy
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav_path = tf.name
        sr = 44100
        extract_wav_with_ffmpeg(media_path, wav_path, sr=sr)
        sr_read, audio = read_wav_mono(wav_path)
        try:
            os.unlink(wav_path)
        except: pass
        duration = len(audio) / sr_read
        times = detect_beats_energy(sr_read, audio, sensitivity=sensitivity)
        # if too few beats, generate grid at estimated BPM
        if len(times) < 8:
            print("[detect] too few onsets, generating BPM grid")
            # estimate BPM via autocorrelation of energies (simple)
            # fallback to 128
            bpm = 128
            beat_interval = 60.0 / bpm
            times = [i * beat_interval for i in range(int(duration / beat_interval))]
        # assign lanes: use time + pseudo randomness for variety
        # also split high-energy peaks to different lane
        beatmap = []
        for idx, t in enumerate(times):
            # simple deterministic but varied
            # use hash of time to pick
            lane = LANE_ORDER[(idx * 7 + int(t*2)) % 4]
            # occasionally double (chord) for emphasis: every ~16 beats add simultaneous
            beatmap.append((float(t), lane))
            if idx % 16 == 7 and idx+1 < len(times) and times[idx+1] - t > 0.4:
                # add simultaneous second lane 0.0s offset (chord)
                other = LANE_ORDER[(LANE_ORDER.index(lane)+2)%4]
                beatmap.append((float(t), other))
        beatmap = sorted(beatmap, key=lambda x: x[0])
        return beatmap, float(duration), 120.0
    except Exception as e:
        print(f"[ffmpeg/numpy] beat detection failed: {e}")
        # ultimate fallback: grid
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

def make_chord_spread(beatmap):
    """Ensure chord notes (same time) are kept together but don't overlap detection too tightly."""
    return beatmap

# ------------------------------------------------------------
# Game Window
# ------------------------------------------------------------
class RhythmGame(pyglet.window.Window):
    def __init__(self):
        super().__init__(width=WINDOW_W, height=WINDOW_H, caption="Radial Rhythm - Pyglet  |  O:open  SPACE:demo  D/F/J/K:hit", resizable=False)
        self.batch = pyglet.graphics.Batch()
        self.bg_color = (10, 10, 18)
        pyglet.gl.glClearColor(10/255, 10/255, 18/255, 1.0)

        self.beatmap = []  # list of (time, lane)
        self.duration = 30.0
        self.active_beats = []  # list of dict {time, lane, angle, hit, spawn_time}
        self.next_index = 0
        self.start_time = None
        self.is_playing = False
        self.is_media_mode = False
        self.media_player = None
        self.media_source = None
        self.media_path = None
        self.sensitivity = 1.0

        # scoring
        self.score = 0
        self.combo = 0
        self.max_combo = 0
        self.hits = {'perfect': 0, 'good': 0, 'ok': 0, 'miss': 0}
        self.feedback_text = ""
        self.feedback_time = 0
        self.feedback_color = (255,255,255,255)

        # visual pulse for hit
        self.hit_pulse = 0.0
        self.lane_flash = {lane: 0.0 for lane in LANE_ORDER}

        # preload demo
        self.load_demo()

        pyglet.clock.schedule_interval(self.update, 1/120)

        # labels
        self.hud_labels = {}

    def load_demo(self):
        self.beatmap = generate_demo_pattern(bpm=128, bars=16)
        self.duration = self.beatmap[-1][0] + 2.0 if self.beatmap else 30.0
        self.is_media_mode = False
        if self.media_player:
            try:
                self.media_player.pause()
            except: pass
        self.reset_play_state()
        self.feedback_text = "DEMO READY - press SPACE"
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
        if not self.beatmap:
            self.load_demo()
        self.reset_play_state()
        self.start_time = time.time()
        self.is_playing = True
        self.is_media_mode = False
        self.feedback_text = "GO!"
        self.feedback_color = (74, 255, 138, 255)
        self.feedback_time = time.time()

    def open_media_dialog(self):
        # use tkinter filedialog if available, else console input
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
            # fallback: try console
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

    def load_media(self, path):
        if not os.path.exists(path):
            self.feedback_text = f"File not found: {path}"
            self.feedback_color = (255, 80, 80, 255)
            self.feedback_time = time.time()
            return
        self.media_path = path
        self.feedback_text = f"Analysing {Path(path).name} ..."
        self.feedback_color = (255, 220, 100, 255)
        self.feedback_time = time.time()
        # force draw once
        self.dispatch_event('on_draw')
        # pyglet needs to process events quickly; but analysis may take 1-3s
        print(f"[load] analysing {path} sensitivity={self.sensitivity}")
        try:
            beatmap, duration, tempo = beats_from_media(path, sensitivity=self.sensitivity, use_librosa=True)
            self.beatmap = beatmap
            self.duration = duration
            print(f"[load] got {len(beatmap)} beats, duration {duration:.1f}s tempo {tempo}")
            # try to load pyglet media for playback
            try:
                if self.media_player:
                    self.media_player.delete()
                    self.media_player = None
                self.media_source = pyglet.media.load(str(path), streaming=True)
                self.media_player = pyglet.media.Player()
                self.media_player.queue(self.media_source)
                # do not autoplay yet
                print(f"[media] loaded duration {self.media_source.duration}")
            except Exception as e:
                print(f"[media] pyglet load failed: {e}")
                self.media_source = None
                self.media_player = None
            self.reset_play_state()
            self.feedback_text = f"Ready: {len(beatmap)} beats | SPACE to play | tempo ~{int(tempo)}"
            self.feedback_color = (100, 255, 150, 255)
            self.feedback_time = time.time()
            self.is_media_mode = False  # will become true on play
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.feedback_text = f"Analysis failed: {e}"
            self.feedback_color = (255, 80, 80, 255)
            self.feedback_time = time.time()

    def start_media(self):
        if not self.beatmap or not self.media_path:
            self.feedback_text = "No media loaded - O to open"
            self.feedback_color = (255,200,100,255)
            self.feedback_time = time.time()
            return
        self.reset_play_state()
        self.is_media_mode = True
        self.is_playing = True
        self.start_time = time.time()
        if self.media_player:
            try:
                # seek to 0
                self.media_player.seek(0)
                self.media_player.play()
                # sync start_time to player time
                # pyglet player time starts at 0; we align
                self.start_time = time.time()  # player started now
            except Exception as e:
                print(f"player play failed {e}")
        self.feedback_text = "PLAYING ♫"
        self.feedback_color = (74, 255, 138, 255)
        self.feedback_time = time.time()

    def get_song_time(self):
        if not self.is_playing or self.start_time is None:
            return 0.0
        if self.is_media_mode and self.media_player:
            try:
                # pyglet player time is authoritative if available
                pt = self.media_player.time
                # pt may be 0 initially; fallback to wall clock
                if pt is not None and pt > 0.05:
                    return float(pt)
            except:
                pass
        return time.time() - self.start_time

    def spawn_beats(self, song_t):
        # spawn beats whose time is within TRAVEL_TIME ahead
        while self.next_index < len(self.beatmap):
            bt, lane = self.beatmap[self.next_index]
            # spawn time = bt - TRAVEL_TIME
            if bt - song_t <= TRAVEL_TIME + 0.05:
                # spawn
                if bt >= song_t - HIT_WINDOW_OK:  # not already missed by far
                    ang = LANES[lane]['angle']
                    self.active_beats.append({
                        'time': bt,
                        'lane': lane,
                        'angle': ang,
                        'hit': False,
                        'spawn_t': song_t,
                    })
                self.next_index += 1
            else:
                break

    def update(self, dt):
        if not self.is_playing:
            # still decay flashes
            for k in self.lane_flash:
                if self.lane_flash[k] > 0:
                    self.lane_flash[k] = max(0, self.lane_flash[k] - dt*3)
            if self.hit_pulse > 0:
                self.hit_pulse = max(0, self.hit_pulse - dt*4)
            return

        song_t = self.get_song_time()
        self.spawn_beats(song_t)

        # check misses (beats that passed hit window without hit)
        still_active = []
        for b in self.active_beats:
            delta = song_t - b['time']
            if not b['hit'] and delta > HIT_WINDOW_OK:
                # miss
                self.hits['miss'] += 1
                self.combo = 0
                self.feedback_text = "MISS"
                self.feedback_color = (255, 80, 80, 255)
                self.feedback_time = time.time()
                self.lane_flash[b['lane']] = 1.0
                # keep briefly for miss animation? remove
                continue
            if delta > 1.0 and b['hit']:
                continue
            if not b['hit'] and delta < 1.0:
                still_active.append(b)
            elif b['hit']:
                # keep hit beats for a little explosion
                if delta < 0.25:
                    still_active.append(b)
                else:
                    continue
            else:
                still_active.append(b)
        self.active_beats = still_active

        # decay flashes
        for k in self.lane_flash:
            if self.lane_flash[k] > 0:
                self.lane_flash[k] = max(0, self.lane_flash[k] - dt*4)
        if self.hit_pulse > 0:
            self.hit_pulse = max(0, self.hit_pulse - dt*5)

        # end condition
        if song_t > self.duration + 1.0 and self.next_index >= len(self.beatmap) and not self.active_beats:
            self.is_playing = False
            if self.media_player:
                try: self.media_player.pause()
                except: pass
            self.feedback_text = f"FINISH! Score {self.score}  Max combo x{self.max_combo}"
            self.feedback_color = (255, 255, 100, 255)
            self.feedback_time = time.time()

        # handle media ended
        if self.is_media_mode and self.media_player:
            try:
                if self.media_source and song_t >= (self.media_source.duration or self.duration) - 0.1:
                    # let update finish handle
                    pass
            except: pass

    def try_hit(self, lane_char):
        if not self.is_playing:
            # if not playing, start demo on D/F/J/K as well
            if self.beatmap and not self.is_media_mode:
                # allow start
                pass
            self.lane_flash[lane_char] = 1.0
            return
        song_t = self.get_song_time()
        # find closest active beat in this lane within window
        best = None
        best_delta = 999
        for b in self.active_beats:
            if b['lane'] != lane_char or b['hit']:
                continue
            delta = abs(song_t - b['time'])
            if delta < best_delta:
                best_delta = delta
                best = b
        if best is None:
            # wrong hit - break combo slightly but not miss
            self.combo = max(0, self.combo - 1)
            self.feedback_text = "MISS"
            self.feedback_color = (255, 120, 80, 255)
            self.feedback_time = time.time()
            self.lane_flash[lane_char] = 0.9
            return
        # evaluate
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
            # too far, count as miss
            self.hits['miss'] += 1
            self.combo = 0
            self.feedback_text = "MISS"
            self.feedback_color = (255, 80, 80, 255)
            self.feedback_time = time.time()
            self.lane_flash[lane_char] = 1.0
            return
        best['hit'] = True
        self.combo += 1
        self.max_combo = max(self.max_combo, self.combo)
        # combo multiplier
        mult = 1 + min(self.combo // 8, 4) * 0.25
        self.score += int(pts * mult)
        self.feedback_time = time.time()
        self.lane_flash[lane_char] = 1.2
        self.hit_pulse = 1.0

    # --------------------------------------------------------
    # Input
    # --------------------------------------------------------
    def on_key_press(self, symbol, modifiers):
        if symbol == key.ESCAPE:
            pyglet.app.exit()
            return
        if symbol == key.O:
            self.open_media_dialog()
            return
        if symbol == key.SPACE:
            # if media loaded and not yet playing media mode, start media; otherwise demo
            if self.media_path and not self.is_playing:
                # prefer media playback if we have a analysed map
                self.start_media()
            else:
                # toggle demo
                if self.is_playing:
                    self.is_playing = False
                    if self.media_player:
                        try: self.media_player.pause()
                        except: pass
                    self.feedback_text = "PAUSED"
                    self.feedback_color = (200,200,200,255)
                    self.feedback_time = time.time()
                else:
                    self.start_demo()
            return
        if symbol == key.P and self.media_path:
            self.start_media()
            return
        if symbol in (key.PLUS, key.EQUAL, key.NUM_ADD):
            self.sensitivity = min(2.0, self.sensitivity + 0.1)
            self.feedback_text = f"Sensitivity {self.sensitivity:.1f}"
            self.feedback_color = (200,220,255,255)
            self.feedback_time = time.time()
            return
        if symbol in (key.MINUS, key.UNDERSCORE, key.NUM_SUBTRACT):
            self.sensitivity = max(0.6, self.sensitivity - 0.1)
            self.feedback_text = f"Sensitivity {self.sensitivity:.1f}"
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

        # lane hits - use key symbol mapping
        if symbol in KEY_TO_LANE:
            lane = KEY_TO_LANE[symbol]
            self.try_hit(lane)
            return

    def on_text(self, text):
        t = text.lower()
        if t in CHAR_TO_LANE:
            self.try_hit(CHAR_TO_LANE[t])

    # --------------------------------------------------------
    # Drawing
    # --------------------------------------------------------
    def on_draw(self):
        self.clear()
        # background gradient-ish via rect
        # darker bg
        # Draw radial grid lines
        cx, cy = CENTER

        # --- center target ---
        # pulsing outer ring based on hit
        pulse = self.hit_pulse

        # draw lanes spokes
        for lane_key, info in LANES.items():
            ang = math.radians(info['angle'])
            # outer point at SPAWN_RADIUS, inner at TARGET_RADIUS
            x1 = cx + math.cos(ang) * TARGET_RADIUS
            y1 = cy + math.sin(ang) * TARGET_RADIUS
            x2 = cx + math.cos(ang) * SPAWN_RADIUS
            y2 = cy + math.sin(ang) * SPAWN_RADIUS
            col = info['color']
            # lane flash intensity
            flash = self.lane_flash[lane_key]
            # lane line alpha
            alpha = 60 + int(flash * 180)
            alpha = min(255, alpha)
            # glow if flash
            width = 2 + flash * 4
            # Use pyglet shapes Line
            line = pyglet.shapes.Line(x1, y1, x2, y2, thickness=width, color=(*col, alpha))
            # we can't batch line with alpha easily; just draw
            line.draw()
            # outer spawn ring marker small
            sx = cx + math.cos(ang) * SPAWN_RADIUS
            sy = cy + math.sin(ang) * SPAWN_RADIUS
            sc = pyglet.shapes.Circle(sx, sy, 14 + flash*6, color=(*col, 90))
            sc.opacity = 90 + int(flash*100)
            sc.draw()
            outer = pyglet.shapes.Circle(sx, sy, 10, color=col)
            outer.draw()
            # lane key diamond near outer
            label = pyglet.text.Label(info['label'],
                                      font_name='Arial', font_size=11, bold=True,
                                      x=sx, y=sy, anchor_x='center', anchor_y='center',
                                      color=(*[255,255,255], 255))
            label.draw()

        # target circles
        # shadow
        for r, col in [(TARGET_RADIUS+18, (30,30,45)), (TARGET_RADIUS+8, (50,50,75))]:
            c = pyglet.shapes.Circle(cx, cy, r, color=col)
            c.opacity = 90
            c.draw()
        # main target
        pulse_r = TARGET_RADIUS + pulse * 22
        c_outer = pyglet.shapes.Circle(cx, cy, int(pulse_r), color=(255,255,255))
        c_outer.opacity = int(30 + pulse*40)
        c_outer.draw()
        c_main = pyglet.shapes.Circle(cx, cy, TARGET_RADIUS, color=(22,22,34))
        c_main.draw()
        inner = pyglet.shapes.Circle(cx, cy, TARGET_RADIUS-6, color=(40,40,60))
        inner.opacity = 200
        inner.draw()
        # inner ring
        ring = pyglet.shapes.Circle(cx, cy, TARGET_RADIUS, color=(255,255,255))
        ring.opacity = 60
        # pyglet circle has no border only fill; simulate with two circles subtract? just draw outer and inner
        # draw border via Line loop or just leave fill
        # center dot
        dot = pyglet.shapes.Circle(cx, cy, 8 + pulse*6, color=(255,255,255))
        dot.opacity = 180
        dot.draw()
        # lane quadrants small arcs: draw 4 small circles at target edge per lane
        for lane_key, info in LANES.items():
            ang = math.radians(info['angle'])
            ex = cx + math.cos(ang) * TARGET_RADIUS
            ey = cy + math.sin(ang) * TARGET_RADIUS
            flash = self.lane_flash[lane_key]
            sz = 16 + flash*10
            col = info['color']
            cc = pyglet.shapes.Circle(ex, ey, sz, color=col)
            cc.opacity = 200 + int(flash*55)
            cc.draw()
            if flash > 0.1:
                glow = pyglet.shapes.Circle(ex, ey, sz+12, color=col)
                glow.opacity = int(flash*70)
                glow.draw()

        # --- beats ---
        song_t = self.get_song_time() if self.is_playing else 0
        for b in self.active_beats:
            if b['hit']:
                # explode animation
                delta = song_t - b['time'] if self.is_playing else 0
                if delta < 0: delta = 0
                prog = delta / 0.25
                if prog > 1: continue
                ang = math.radians(b['angle'])
                x = cx + math.cos(ang) * (TARGET_RADIUS + prog*30)
                y = cy + math.sin(ang) * (TARGET_RADIUS + prog*30)
                # shrinking and fading
                alpha = int(255 * (1 - prog))
                sz = 28 * (1 - prog*0.6)
                col = LANES[b['lane']]['color']
                c = pyglet.shapes.Circle(x, y, sz, color=col)
                c.opacity = max(0, alpha)
                c.draw()
                # particle ring
                r = pyglet.shapes.Circle(cx, cy, TARGET_RADIUS + prog*60, color=col)
                r.opacity = int(90*(1-prog))
                # simulate ring via low opacity fill; not ideal but okay
                # just draw as circle outline approximation using pyglet line? skip
                continue
            # position along radius
            # t_progress = (song_t - (b['time'] - TRAVEL_TIME)) / TRAVEL_TIME
            raw = (song_t - (b['time'] - TRAVEL_TIME)) / TRAVEL_TIME if self.is_playing else 0.0
            # if not playing, demo placement: show first few beats static radial
            if not self.is_playing:
                # show preview positions distributed: use index vs time offset from 0
                idx = self.beatmap.index((b['time'], b['lane'])) if (b['time'], b['lane']) in self.beatmap else 0
                # preview not accurate when not playing; instead keep them hidden
                continue
            # clamp
            if raw < 0: raw = 0
            if raw > 1.2: continue
            # eased pos? linear
            radius = SPAWN_RADIUS - raw * (SPAWN_RADIUS - TARGET_RADIUS)
            # clamp to target
            if radius < TARGET_RADIUS:
                radius = TARGET_RADIUS
            ang = math.radians(b['angle'])
            x = cx + math.cos(ang) * radius
            y = cy + math.sin(ang) * radius
            # tail behind
            tail_len = 18
            tx = cx + math.cos(ang) * (radius + tail_len)
            ty = cy + math.sin(ang) * (radius + tail_len)
            col = LANES[b['lane']]['color']
            # approach scale grows slightly as it nears centre
            scale = 0.9 + 0.35 * raw
            sz = 22 * scale
            # glow tail
            tail = pyglet.shapes.Line(x, y, tx, ty, thickness=8, color=(*col, 90))
            tail.draw()
            # beat circle
            circle = pyglet.shapes.Circle(x, y, sz, color=col)
            circle.draw()
            inner_c = pyglet.shapes.Circle(x, y, sz*0.55, color=(255,255,255))
            inner_c.opacity = 200
            inner_c.draw()
            # time indicator for near centre: shrinking ring
            if radius < TARGET_RADIUS + 60:
                ring_sz = sz + (1-raw)*10
                # miss indicator
                pass

        # --- HUD ---
        # top bar
        bar_h = 46
        bar = pyglet.shapes.Rectangle(0, WINDOW_H - bar_h, WINDOW_W, bar_h, color=(18,18,30))
        bar.opacity = 220
        bar.draw()
        # scores
        score_lbl = pyglet.text.Label(f"Score {self.score:06d}   Combo x{self.combo} (max {self.max_combo})",
                                      font_name='Arial', font_size=14, bold=True,
                                      x=16, y=WINDOW_H-16, anchor_x='left', anchor_y='top',
                                      color=(240,240,255,255))
        score_lbl.draw()
        hits_lbl = pyglet.text.Label(f"P:{self.hits['perfect']}  G:{self.hits['good']}  OK:{self.hits['ok']}  M:{self.hits['miss']}",
                                     font_name='Consolas', font_size=11,
                                     x=16, y=WINDOW_H-33, anchor_x='left', anchor_y='top',
                                     color=(180,180,200,255))
        hits_lbl.draw()

        # song time / progress
        if self.is_playing:
            prog = song_t / self.duration if self.duration else 0
            prog = max(0, min(1, prog))
            # progress bar bottom
            pw = WINDOW_W * prog
            prog_bg = pyglet.shapes.Rectangle(0, 6, WINDOW_W, 4, color=(40,40,50))
            prog_bg.draw()
            prog_fg = pyglet.shapes.Rectangle(0, 6, pw, 4, color=(100,255,160))
            prog_fg.draw()
            time_lbl = pyglet.text.Label(f"{int(song_t//60):01d}:{int(song_t%60):02d} / {int(self.duration//60):01d}:{int(self.duration%60):02d}",
                                         font_name='Consolas', font_size=10,
                                         x=WINDOW_W-12, y=WINDOW_H-18, anchor_x='right', anchor_y='top',
                                         color=(180,220,255,255))
            time_lbl.draw()
        else:
            mode = f"Media: {Path(self.media_path).name}" if self.media_path else "DEMO MODE"
            mode_lbl = pyglet.text.Label(mode,
                                         font_name='Consolas', font_size=10,
                                         x=WINDOW_W-12, y=WINDOW_H-32, anchor_x='right', anchor_y='top',
                                         color=(150,170,200,255))
            mode_lbl.draw()

        # feedback centre
        if self.feedback_text and (time.time() - self.feedback_time) < 1.6:
            age = time.time() - self.feedback_time
            alpha = int(255 * (1 - age/1.6))
            alpha = max(0, min(255, alpha))
            # pop scale
            scale = 1.0 + max(0, 0.25 - age*0.5)
            # choose font size by feedback
            fsize = 28 if "PERFECT" in self.feedback_text else 24
            fb = pyglet.text.Label(self.feedback_text, font_name='Arial', font_size=fsize, bold=True,
                                   x=cx, y=cy+110, anchor_x='center', anchor_y='center',
                                   color=(*self.feedback_color[:3], alpha))
            # pyglet Label doesn't support scaling easily; adjust y offset
            fb.y += int((scale-1)*20)
            fb.draw()

        # bottom instructions
        instr = "O: open mp4  SPACE: play/pause  D F J K: hit  +/- sensitivity  ESC: quit"
        if not self.is_playing:
            instr += "  |  Press SPACE to start"
        il = pyglet.text.Label(instr, font_name='Consolas', font_size=9,
                               x=WINDOW_W//2, y=18, anchor_x='center', anchor_y='center',
                               color=(130,130,160,255))
        il.draw()

        # lane legend bottom-left when not playing: show colors
        if not self.is_playing:
            y0 = 60
            for i, lane in enumerate(LANE_ORDER):
                col = LANES[lane]['color']
                xs = 20 + i*90
                c = pyglet.shapes.Circle(xs+16, y0, 12, color=col)
                c.draw()
                l = pyglet.text.Label(f"{lane.upper()} -> {LANES[lane]['label']}", font_name='Consolas', font_size=9,
                                      x=xs+34, y=y0, anchor_x='left', anchor_y='center',
                                      color=(200,200,220,255))
                l.draw()


def main():
    # enable high dpi? not needed
    winsound_msg = ""
    # check ffmpeg available
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except:
        print("WARNING: ffmpeg not found in PATH - mp4 analysis will fail. Install ffmpeg.")
    game = RhythmGame()
    # handle command-line file arg
    if len(sys.argv) > 1:
        p = sys.argv[1]
        if os.path.exists(p):
            game.load_media(p)
    pyglet.app.run()

if __name__ == "__main__":
    main()
