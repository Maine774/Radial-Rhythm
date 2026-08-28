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

import math
import os
import sys
import time
import tempfile
import subprocess
import wave
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

def detect_beats_energy(sr, audio, sensitivity=1.0):
    hop = 512
    n_frames = 1 + (len(audio) - 1024) // hop
    if n_frames <= 0:
        return []
    energies = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        start = i * hop
        chunk = audio[start:start+1024]
        energies[i] = np.sqrt(np.mean(chunk * chunk) + 1e-10)
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

def beats_from_media(media_path, sensitivity=1.0, use_librosa=True):
    if use_librosa:
        try:
            import librosa
            y, sr = librosa.load(str(media_path), sr=22050, mono=True)
            tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units='time')
            if len(beat_frames) < 20:
                onset_env = librosa.onset.onset_strength(y=y, sr=sr)
                onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, units='time')
                beat_frames = sorted(set(list(beat_frames) + list(onsets)))
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
            return lane_pattern, float(duration), float(tempo) if hasattr(tempo, '__float__') else 120.0
        except ImportError:
            pass
        except Exception as e:
            print(f"[librosa] failed {e}, falling back to numpy method")
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
        if len(times) < 8:
            print("[detect] too few onsets, generating BPM grid")
            bpm = 128
            beat_interval = 60.0 / bpm
            times = [i * beat_interval for i in range(int(duration / beat_interval))]
        beatmap = []
        for idx, t in enumerate(times):
            # cycle lanes to ensure all 4 colours appear (D F J K)
            lane = LANE_ORDER[idx % 4]
            beatmap.append((float(t), lane))
            if idx % 16 == 7 and idx+1 < len(times) and times[idx+1] - t > 0.4:
                other = LANE_ORDER[(LANE_ORDER.index(lane)+2)%4]
                beatmap.append((float(t), other))
        beatmap = sorted(beatmap, key=lambda x: x[0])
        return beatmap, float(duration), 120.0
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

# ------------------------------------------------------------
# Game Window
# ------------------------------------------------------------
class RhythmGame(pyglet.window.Window):
    def __init__(self):
        super().__init__(width=WINDOW_W, height=WINDOW_H, caption="Radial Rhythm - Pyglet  |  Main Menu", resizable=False)
        self.batch = pyglet.graphics.Batch()
        pyglet.gl.glClearColor(10/255, 10/255, 18/255, 1.0)

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

        pyglet.clock.schedule_interval(self.update, 1/120)

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
        # quick draw to show analysing text before blocking
        try:
            self.dispatch_event('on_draw')
            self.flip()
        except: pass
        print(f"[load] analysing {path} sensitivity={self.sensitivity}")
        try:
            beatmap, duration, tempo = beats_from_media(path, sensitivity=self.sensitivity, use_librosa=True)
            self.beatmap = beatmap
            self.duration = duration
            print(f"[load] got {len(beatmap)} beats, duration {duration:.1f}s tempo {tempo}")
            try:
                if self.media_player:
                    try: self.media_player.delete()
                    except: pass
                    self.media_player = None
                self.media_source = pyglet.media.load(str(path), streaming=True)
                self.media_player = pyglet.media.Player()
                self.media_player.queue(self.media_source)
                print(f"[media] loaded duration {self.media_source.duration}")
            except Exception as e:
                print(f"[media] pyglet load failed: {e}")
                self.media_source = None
                self.media_player = None
            self.reset_play_state()
            self.feedback_text = f"Ready: {len(beatmap)} beats | ENTER to play | tempo ~{int(tempo)}"
            self.feedback_color = (100, 255, 150, 255)
            self.feedback_time = time.time()
            self.is_media_mode = False
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
        self.state = "playing"
        self.start_time = time.time()
        if self.media_player:
            try:
                self.media_player.seek(0)
                self.media_player.play()
                self.start_time = time.time()
            except Exception as e:
                print(f"player play failed {e}")
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
            if bt - song_t <= TRAVEL_TIME + 0.05:
                if bt >= song_t - HIT_WINDOW_OK:
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
        if self.state not in ("playing",):
            for k in self.lane_flash:
                if self.lane_flash[k] > 0:
                    self.lane_flash[k] = max(0, self.lane_flash[k] - dt*3)
            if self.hit_pulse > 0:
                self.hit_pulse = max(0, self.hit_pulse - dt*4)
            return
        if not self.is_playing:
            return
        song_t = self.get_song_time()
        self.spawn_beats(song_t)
        still_active = []
        for b in self.active_beats:
            delta = song_t - b['time']
            if not b['hit'] and delta > HIT_WINDOW_OK:
                self.hits['miss'] += 1
                self.combo = 0
                self.feedback_text = "MISS"
                self.feedback_color = (255, 80, 80, 255)
                self.feedback_time = time.time()
                self.lane_flash[b['lane']] = 1.0
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
            if b['lane'] != lane_char or b['hit']:
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
        mult = 1 + min(self.combo // 8, 4) * 0.25
        self.score += int(pts * mult)
        self.feedback_time = time.time()
        self.lane_flash[lane_char] = 1.2
        self.hit_pulse = 1.0

    # --------------------------------------------------------
    # Input
    # --------------------------------------------------------
    def on_key_press(self, symbol, modifiers):
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
                    self.load_media(str(chosen))
                    # auto start after load if beatmap ready
                    if self.beatmap:
                        self.start_media()
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
                self.load_media(str(chosen))
                if self.beatmap:
                    self.start_media()
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
            if symbol in KEY_TO_LANE:
                lane = KEY_TO_LANE[symbol]
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
        if self.state == "playing":
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
        # Wrapper to avoid bold kwarg issue
        lbl = pyglet.text.Label(text, x=x, y=y, font_name=font_name, font_size=size, weight=weight, italic=italic, color=color, anchor_x=anchor_x, anchor_y=anchor_y)
        lbl.draw()
        return lbl

    def _draw_center_target(self, cx, cy, pulse):
        for lane_key, info in LANES.items():
            ang = math.radians(info['angle'])
            x1 = cx + math.cos(ang) * TARGET_RADIUS
            y1 = cy + math.sin(ang) * TARGET_RADIUS
            x2 = cx + math.cos(ang) * SPAWN_RADIUS
            y2 = cy + math.sin(ang) * SPAWN_RADIUS
            col = info['color']
            flash = self.lane_flash[lane_key]
            alpha = 60 + int(flash * 180)
            alpha = min(255, alpha)
            width = 2 + flash * 4
            line = pyglet.shapes.Line(x1, y1, x2, y2, thickness=width, color=(*col, alpha))
            line.draw()
            sx = cx + math.cos(ang) * SPAWN_RADIUS
            sy = cy + math.sin(ang) * SPAWN_RADIUS
            sc = pyglet.shapes.Circle(sx, sy, 14 + flash*6, color=(*col, 90))
            sc.opacity = 90 + int(flash*100)
            sc.draw()
            outer = pyglet.shapes.Circle(sx, sy, 10, color=col)
            outer.draw()
            # label - fixed bold issue
            self._draw_label(info['label'], x=sx, y=sy, size=11, color=(255,255,255,255), anchor_x='center', anchor_y='center', weight='bold')
        for r, col in [(TARGET_RADIUS+18, (30,30,45)), (TARGET_RADIUS+8, (50,50,75))]:
            c = pyglet.shapes.Circle(cx, cy, r, color=col)
            c.opacity = 90
            c.draw()
        pulse_r = TARGET_RADIUS + pulse * 22
        c_outer = pyglet.shapes.Circle(cx, cy, int(pulse_r), color=(255,255,255))
        c_outer.opacity = int(30 + pulse*40)
        c_outer.draw()
        c_main = pyglet.shapes.Circle(cx, cy, TARGET_RADIUS, color=(22,22,34))
        c_main.draw()
        inner = pyglet.shapes.Circle(cx, cy, TARGET_RADIUS-6, color=(40,40,60))
        inner.opacity = 200
        inner.draw()
        dot = pyglet.shapes.Circle(cx, cy, 8 + pulse*6, color=(255,255,255))
        dot.opacity = 180
        dot.draw()
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

    def _draw_beats(self, song_t):
        cx, cy = CENTER
        for b in self.active_beats:
            if b['hit']:
                delta = song_t - b['time'] if self.is_playing else 0
                if delta < 0: delta = 0
                prog = delta / 0.25
                if prog > 1: continue
                ang = math.radians(b['angle'])
                x = cx + math.cos(ang) * (TARGET_RADIUS + prog*30)
                y = cy + math.sin(ang) * (TARGET_RADIUS + prog*30)
                alpha = int(255 * (1 - prog))
                sz = 28 * (1 - prog*0.6)
                col = LANES[b['lane']]['color']
                c = pyglet.shapes.Circle(x, y, sz, color=col)
                c.opacity = max(0, alpha)
                c.draw()
                continue
            raw = (song_t - (b['time'] - TRAVEL_TIME)) / TRAVEL_TIME if self.is_playing else 0.0
            if not self.is_playing:
                continue
            if raw < 0: raw = 0
            if raw > 1.2: continue
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
            tail = pyglet.shapes.Line(x, y, tx, ty, thickness=8, color=(*col, 90))
            tail.draw()
            circle = pyglet.shapes.Circle(x, y, sz, color=col)
            circle.draw()
            inner_c = pyglet.shapes.Circle(x, y, sz*0.55, color=(255,255,255))
            inner_c.opacity = 200
            inner_c.draw()

    def _draw_hud(self, song_t):
        bar_h = 46
        bar = pyglet.shapes.Rectangle(0, WINDOW_H - bar_h, WINDOW_W, bar_h, color=(18,18,30))
        bar.opacity = 220
        bar.draw()
        self._draw_label(f"Score {self.score:06d}   Combo x{self.combo} (max {self.max_combo})", x=16, y=WINDOW_H-16, size=14, color=(240,240,255,255), anchor_x='left', anchor_y='top', weight='bold')
        self._draw_label(f"P:{self.hits['perfect']}  G:{self.hits['good']}  OK:{self.hits['ok']}  M:{self.hits['miss']}", x=16, y=WINDOW_H-33, size=11, color=(180,180,200,255), anchor_x='left', anchor_y='top', font_name='Consolas')
        if self.is_playing:
            prog = song_t / self.duration if self.duration else 0
            prog = max(0, min(1, prog))
            pw = WINDOW_W * prog
            pyglet.shapes.Rectangle(0, 6, WINDOW_W, 4, color=(40,40,50)).draw()
            pyglet.shapes.Rectangle(0, 6, pw, 4, color=(100,255,160)).draw()
            self._draw_label(f"{int(song_t//60):01d}:{int(song_t%60):02d} / {int(self.duration//60):01d}:{int(self.duration%60):02d}", x=WINDOW_W-12, y=WINDOW_H-18, size=10, color=(180,220,255,255), anchor_x='right', anchor_y='top', font_name='Consolas')
        else:
            mode = f"Media: {Path(self.media_path).name}" if self.media_path else "DEMO MODE"
            self._draw_label(mode, x=WINDOW_W-12, y=WINDOW_H-32, size=10, color=(150,170,200,255), anchor_x='right', anchor_y='top', font_name='Consolas')
        if self.feedback_text and (time.time() - self.feedback_time) < 1.6:
            age = time.time() - self.feedback_time
            alpha = int(255 * (1 - age/1.6))
            alpha = max(0, min(255, alpha))
            scale = 1.0 + max(0, 0.25 - age*0.5)
            cx, cy = CENTER
            fsize = 28 if "PERFECT" in self.feedback_text else 24
            self._draw_label(self.feedback_text, x=cx, y=cy+110 + int((scale-1)*20), size=fsize, color=(*self.feedback_color[:3], alpha), anchor_x='center', anchor_y='center', weight='bold')

    # --------------------------------------------------------
    # Main draw dispatcher
    # --------------------------------------------------------
    def on_draw(self):
        self.clear()
        cx, cy = CENTER
        if self.state in ("playing", "paused", "results"):
            # game bg
            pulse = self.hit_pulse
            self._draw_center_target(cx, cy, pulse)
            if self.state == "playing":
                song_t = self.get_song_time()
                self._draw_beats(song_t)
            elif self.state == "paused":
                # still show beats frozen at pause time? skip
                pass
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

        if self.state == "menu":
            # title
            self._draw_label("RADIAL RHYTHM", x=WINDOW_W//2, y=WINDOW_H - 120, size=40, color=(255,255,255,255), anchor_x='center', anchor_y='center', weight='bold')
            self._draw_label("beats converge to the centre  •  D  F  J  K", x=WINDOW_W//2, y=WINDOW_H - 155, size=11, color=(140,200,255,255), anchor_x='center', anchor_y='center', font_name='Consolas')
            # show songs folder hint
            self._draw_label(f"songs in  ./songs/  ({len(get_songs_in_folder())} found)  •  add mp4 / mp3 / wav and press SONGS", x=WINDOW_W//2, y=WINDOW_H - 180, size=9, color=(130,140,160,255), anchor_x='center', anchor_y='center', font_name='Consolas')

            # menu options as boxes
            for idx, opt in enumerate(self.menu_options):
                y = 360 - idx*60
                x = WINDOW_W//2
                selected = idx == self.menu_index
                w, h = 420, 44
                # background
                bg_col = (55, 55, 90) if selected else (28, 28, 42)
                rect = pyglet.shapes.Rectangle(x - w//2, y - h//2, w, h, color=bg_col)
                rect.draw()
                # border highlight for selected
                if selected:
                    # draw border lines (simple 1px border via enlarged rect)
                    border = pyglet.shapes.Rectangle(x - w//2 -1, y - h//2 -1, w+2, h+2, color=(120,180,255))
                    # need to draw border behind: we already drew bg, so draw again bg inset
                    border.draw()
                    rect.draw()
                    # left color accent
                    accent = pyglet.shapes.Rectangle(x - w//2, y - h//2, 6, h, color=(100,255,160))
                    accent.draw()
                # lane colors hint for PLAY DEMO
                txt_col = (255,255,120,255) if selected else (220,220,240,255)
                weight = 'bold' if selected else 'normal'
                self._draw_label(opt, x=x, y=y, size=16, color=(*txt_col[:3],255), anchor_x='center', anchor_y='center', weight=weight)

            # footer
            self._draw_label("UP/DOWN or W/S • ENTER/SPACE to select • O open external file • ESC quit", x=WINDOW_W//2, y=70, size=9, color=(110,120,150,255), anchor_x='center', anchor_y='center', font_name='Consolas')
            # lane legend
            y0 = 30
            for i, lane in enumerate(LANE_ORDER):
                col = LANES[lane]['color']
                xs = WINDOW_W//2 - 160 + i*90
                c = pyglet.shapes.Circle(xs+16, y0, 10, color=col)
                c.draw()
                self._draw_label(f"{lane.upper()}", x=xs+34, y=y0, size=9, color=(200,200,220,255), anchor_x='left', anchor_y='center', font_name='Consolas')
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
