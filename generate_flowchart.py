#!/usr/bin/env python3
from PIL import Image, ImageDraw, ImageFont
import math, os

W, H = 1600, 1150
BG = (255,255,255)
BLUE = (189, 215, 238)  # start/end fill like example
GREY = (217, 217, 217)  # user action
WHITE = (255,255,255)
BLACK = (0,0,0)
OUTLINE = (0,0,0)

# try to load a nice font, fallback to default
def load_font(size, bold=False):
    candidates = [
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    bold_cands = [
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\calibrib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    try:
        path = (bold_cands if bold else candidates)
        for p in path:
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    except: pass
    return ImageFont.load_default()

f_title = load_font(28, bold=True)
f_sub = load_font(13, bold=False)
f_sub_small = load_font(10, bold=False)
f_node = load_font(11, bold=False)
f_node_small = load_font(9, bold=False)
f_note = load_font(9, bold=False)
f_key_title = load_font(15, bold=True)
f_key = load_font(11, bold=False)

img = Image.new("RGB", (W,H), BG)
d = ImageDraw.Draw(img)

def text_center(draw, text, cx, cy, font, fill=BLACK, align="center"):
    # multiline
    lines = text.split("\n")
    # measure total height
    widths = []
    heights = []
    for line in lines:
        bbox = draw.textbbox((0,0), line, font=font)
        widths.append(bbox[2]-bbox[0])
        heights.append(bbox[3]-bbox[1])
    total_h = sum(heights) + (len(lines)-1)*2
    y = cy - total_h/2
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0,0), line, font=font)
        w = bbox[2]-bbox[0]
        h = bbox[3]-bbox[1]
        x = cx - w/2 if align=="center" else cx
        draw.text((x, y), line, font=font, fill=fill)
        y += h + 2

def draw_rect(cx, cy, w, h, fill, outline=OUTLINE, width=2):
    x0, y0 = cx - w/2, cy - h/2
    x1, y1 = cx + w/2, cy + h/2
    d.rectangle([x0,y0,x1,y1], fill=fill, outline=outline, width=width)
    return (x0,y0,x1,y1)

def draw_diamond(cx, cy, w, h, fill, outline=OUTLINE, width=2):
    pts = [(cx, cy - h/2), (cx + w/2, cy), (cx, cy + h/2), (cx - w/2, cy)]
    d.polygon(pts, fill=fill, outline=outline, width=width)
    return pts

def draw_circle(cx, cy, r, fill, outline=OUTLINE, width=2):
    d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=fill, outline=outline, width=width)

def arrow(draw, x0,y0,x1,y1, width=2, dash=False, color=BLACK):
    # simple line + triangle head
    if dash:
        # dashed
        length = math.hypot(x1-x0, y1-y0)
        if length == 0: return
        dash_len = 8
        gap = 6
        steps = int(length/(dash_len+gap))
        for i in range(steps+1):
            t0 = i*(dash_len+gap)/length
            t1 = min(1, t0 + dash_len/length)
            sx = x0 + (x1-x0)*t0
            sy = y0 + (y1-y0)*t0
            ex = x0 + (x1-x0)*t1
            ey = y0 + (y1-y0)*t1
            draw.line([sx,sy,ex,ey], fill=color, width=width)
        # head at end
        ang = math.atan2(y1-y0, x1-x0)
        sz = 8
        p1 = (x1 - sz*math.cos(ang - math.pi/6), y1 - sz*math.sin(ang - math.pi/6))
        p2 = (x1 - sz*math.cos(ang + math.pi/6), y1 - sz*math.sin(ang + math.pi/6))
        draw.polygon([ (x1,y1), p1, p2 ], fill=color)
    else:
        draw.line([x0,y0,x1,y1], fill=color, width=width)
        ang = math.atan2(y1-y0, x1-x0)
        sz = 8
        p1 = (x1 - sz*math.cos(ang - math.pi/6), y1 - sz*math.sin(ang - math.pi/6))
        p2 = (x1 - sz*math.cos(ang + math.pi/6), y1 - sz*math.sin(ang + math.pi/6))
        draw.polygon([ (x1,y1), p1, p2 ], fill=color)

def poly_arrow(draw, points, width=2, dash=False, color=BLACK):
    for i in range(len(points)-1):
        is_last = (i == len(points)-2)
        x0,y0 = points[i]
        x1,y1 = points[i+1]
        if is_last:
            arrow(draw, x0,y0,x1,y1, width=width, dash=dash, color=color)
        else:
            if dash:
                # dashed segment without head
                length = math.hypot(x1-x0, y1-y0)
                dash_len = 8; gap=6
                steps = int(length/(dash_len+gap))
                for j in range(steps+1):
                    t0 = j*(dash_len+gap)/length
                    t1 = min(1, t0 + dash_len/length)
                    sx = x0 + (x1-x0)*t0; sy = y0 + (y1-y0)*t0
                    ex = x0 + (x1-x0)*t1; ey = y0 + (y1-y0)*t1
                    draw.line([sx,sy,ex,ey], fill=color, width=width)
            else:
                draw.line([x0,y0,x1,y1], fill=color, width=width)

# --- Title ---
d.text((980, 28), "User Flow Example: Radial Rhythm", font=f_title, fill=BLACK)
d.text((980, 68), "MHS Year 9 DT", font=f_sub, fill=BLACK)
d.text((980, 88), "Kevan", font=f_sub_small, fill=(80,80,80))

# --- Nodes ---
# Top row y=150
y_top = 150
# Start
sx, sy = 75, y_top
draw_circle(sx, sy, 52, BLUE, width=2)
text_center(d, "Start\nLaunch App", sx, sy, f_node)

# Screen 1 Intro/Menu
s1x, s1y = 235, y_top
draw_rect(s1x, s1y, 150, 72, WHITE, width=2)
text_center(d, "Screen 1:\nMain Menu\nPlay / Open / Settings", s1x, s1y, f_node_small)

# Decision What now?
d1x, d1y = 430, y_top
draw_diamond(d1x, d1y, 125, 78, WHITE, width=2)
text_center(d, "Decision\nWhat now?", d1x, d1y, f_node_small)

# Screen 2 Settings/Rules
s2x, s2y = 635, y_top
draw_rect(s2x, s2y, 150, 72, WHITE, width=2)
text_center(d, "Screen 2: Settings\nRead how to\ntoggle fullscreen", s2x, s2y, f_node_small)

# --- Core row y=360 ---
y_mid = 360
# SYSTEM Scan/Analyse
sysx, sysy = 135, y_mid
draw_rect(sysx, sysy, 155, 62, GREY, width=2)
text_center(d, "SYSTEM:\nScan songs +\nLoad / Analyse", sysx, sysy, f_node_small)

# Screen 3 Song Select
s3x, s3y = 340, y_mid
draw_rect(s3x, s3y, 175, 78, WHITE, width=2)
text_center(d, "Screen 3:\nSong Select\nCarousel + Preview\n+ difficulty row", s3x, s3y, f_node_small)

# Decision Song+Difficult chosen? (3 possible)
d2x, d2y = 585, y_mid
draw_diamond(d2x, d2y, 150, 85, WHITE, width=2)
text_center(d, "Decision\nSong + Difficulty\nchosen?", d2x, d2y, f_node_small)

# Difficulty detail row y=520
y_diff = 520
s3bx, s3by = 340, y_diff
draw_rect(s3bx, s3by, 175, 72, WHITE, width=2)
text_center(d, "Screen 3b:\nDifficulty Select\nEASY / MEDIUM /\nHARD (1–20 rating)", s3bx, s3by, f_node_small)

syscx, syscy = 585, y_diff
draw_rect(syscx, syscy, 155, 62, GREY, width=2)
text_center(d, "SYSTEM:\nCheck cache v4\nor Analyse (madmom)", syscx, syscy, f_node_small)

# Gameplay row y=680
y_game = 680
s4x, s4y = 340, y_game
draw_rect(s4x, s4y, 175, 78, WHITE, width=2)
text_center(d, "Screen 4:\nGameplay\nRing — notes\nconverge to centre", s4x, s4y, f_node_small)

d3x, d3y = 585, y_game
draw_diamond(d3x, d3y, 150, 85, WHITE, width=2)
text_center(d, "Decision\nHit D/F/J/K\nin window?", d3x, d3y, f_node_small)

# Action row y=840
y_act = 840
ax, ay = 165, y_act
draw_rect(ax, ay, 150, 62, GREY, width=2)
text_center(d, "User Action\nHIT\nPERFECT/GOOD", ax, ay, f_node_small)

bx, by = 365, y_act
draw_rect(bx, by, 150, 62, GREY, width=2)
text_center(d, "System\nScoring\n+ combo update", bx, by, f_node_small)

cx, cy = 585, y_act
draw_rect(cx, cy, 150, 62, GREY, width=2)
text_center(d, "User Action\nMISS / HOLD\n(ESC = pause)", cx, cy, f_node_small)

# Check diamond
chx, chy = 365, 960
draw_diamond(chx, chy, 145, 82, WHITE, width=2)
text_center(d, "Check\nSong ended?", chx, chy, f_node_small)

# Results
s5x, s5y = 580, 980
draw_rect(s5x, s5y, 150, 72, WHITE, width=2)
text_center(d, "Screen 5:\nResults\nScore + Grade\nPlay again?", s5x, s5y, f_node_small)

# Play again decision
d4x, d4y = 780, 980
draw_diamond(d4x, d4y, 130, 80, WHITE, width=2)
text_center(d, "Decision\nPlay again?", d4x, d4y, f_node_small)

# End
ex, ey = 985, 980
draw_circle(ex, ey, 52, BLUE, width=2)
text_center(d, "End\nQuit", ex, ey, f_node)

# --- Arrows (solid) ---
# Start -> Screen1
arrow(d, sx+52, sy, s1x-75, s1y, width=2)
# Screen1 -> Decision
arrow(d, s1x+75, s1y, d1x-62, d1y, width=2)
# Decision -> Settings
arrow(d, d1x+62, d1y, s2x-75, s2y, width=2)
# SYSTEM -> Screen3 (auto, no input)
arrow(d, sysx+77, sysy, s3x-87, s3y, width=2)
# Screen3 -> Decision diff
arrow(d, s3x+87, s3y, d2x-75, d2y, width=2)
# vertical spine: Decision What now? -> Song Select (core)
poly_arrow(d, [(d1x, d1y+39), (d1x, 260), (s3x, 260), (s3x, s3y-39)], width=2)

# Song diff decision -> Difficulty screen
poly_arrow(d, [(d2x, d2y+42), (d2x, 430), (s3bx, 430), (s3bx, s3by-36)], width=2)
# Difficulty screen -> System cache
arrow(d, s3bx+87, s3by, syscx-77, syscy, width=2)
# System cache -> Gameplay
poly_arrow(d, [(syscx, syscy+31), (syscx, 600), (s4x, 600), (s4x, s4y-39)], width=2)

# Gameplay -> Hit decision
arrow(d, s4x+87, s4y, d3x-75, d3y, width=2)

# Hit decision -> 3 actions
poly_arrow(d, [(d3x, d3y+42), (d3x, 760), (ax, 760), (ax, ay-31)], width=2)  # Hit left
arrow(d, d3x, d3y+42, bx, by-31, width=2)  # middle
poly_arrow(d, [(d3x, d3y+42), (cx, 725), (cx, cy-31)], width=2)  # right (Hold/Miss)

# Small labels on branches
d.text((420, 700), "Hit", font=f_note, fill=BLACK)
d.text((510, 700), "Good/OK", font=f_note, fill=BLACK)
d.text((610, 700), "Miss/Hold", font=f_note, fill=BLACK)

# Actions -> Check
arrow(d, ax+60, ay+31, chx-10, chy-41, width=2)
arrow(d, bx, by+31, chx, chy-41, width=2)
arrow(d, cx-60, cy+31, chx+30, chy-41, width=2)

# Check -> Results (Yes, ended)
arrow(d, chx+72, chy, s5x-75, s5y, width=2)
d.text((445, 955), "Yes, ended", font=f_note, fill=BLACK)

# Results -> Play again
arrow(d, s5x+75, s5y, d4x-65, d4y, width=2)

# Play again -> End (No)
arrow(d, d4x+65, d4y, ex-52, ey, width=2)
d.text((850, 945), "No", font=f_note, fill=BLACK)

# Play again Yes -> back to Song Select (new game) - bottom loop
poly_arrow(d, [(d4x, d4y+40), (d4x, 1125), (55, 1125), (55, sysy), (sysx-77, sysy)], width=2, dash=False)
d.text((480, 1115), "Yes, new game", font=f_note, fill=BLACK)

# Check No -> keep going (loop back to Gameplay) - left loop
poly_arrow(d, [(chx-72, chy), (105, chy), (105, y_game+25), (s4x-87, s4y+12)], width=2)
d.text((115, 900), "No -> keep going", font=f_note, fill=BLACK)

# --- Dashed Quit/Exit paths ---
# Settings -> Start Game (back to menu)
poly_arrow(d, [(s2x-45, s2y+36), (s2x-45, 205), (s1x+45, 205), (s1x+45, s1y+36)], width=2, dash=True, color=(80,80,80))
d.text((505, 215), "Start Game", font=f_note, fill=(80,80,80))

# Decision What now? Quit -> End (across top)
poly_arrow(d, [(d1x+50, d1y-20), (1020, 50), (1020, 920), (ex, 920)], width=2, dash=True, color=(80,80,80))
d.text((860, 45), "Quit", font=f_note, fill=(80,80,80))

# Miss Hold pause dashed? Example had Split disabled dashed
d.text((430, 875), "If miss, combo resets", font=f_note, fill=(80,80,80))
d.text((15, 295), "Auto, no input\nbeatmap generated", font=f_note, fill=(80,80,80))
d.text((185, 235), "core game mechanics:\nhit notes at centre\n(D/F/J/K vs timing)", font=f_note, fill=(80,80,80))
d.text((500, 285), "3 possible\nuser actions", font=f_note, fill=(80,80,80))
d.text((15, 610), "If outside\n350ms = auto\nMISS (no choice)", font=f_note, fill=(80,80,80))
d.text((620, 1020), "Shows final\nscore + grade", font=f_note, fill=(80,80,80))

# Arrow label Quit on top dashed near start
# --- Key box ---
kx, ky = 1180, 620
kw, kh = 320, 360
draw_rect(kx+kw//2-40, ky+kh//2-20, kw, kh, WHITE, width=2)
d.text((kx+30, ky+15), "User Flow Key", font=f_key_title, fill=BLACK)
# entries
# Start/End
draw_circle(kx+45, ky+55, 22, BLUE, width=2)
d.text((kx+80, ky+50), "Start/End", font=f_key, fill=BLACK)
# Screen
draw_rect(kx+45, ky+100, 44, 28, WHITE, width=2)
d.text((kx+80, ky+95), "Screen", font=f_key, fill=BLACK)
# Decision
draw_diamond(kx+45, ky+145, 44, 28, WHITE, width=2)
d.text((kx+80, ky+140), "Decision", font=f_key, fill=BLACK)
# User Action
draw_rect(kx+45, ky+190, 44, 28, GREY, width=2)
d.text((kx+80, ky+185), "User Action", font=f_key, fill=BLACK)
# Quit dashed
d.line([kx+23, ky+235, kx+67, ky+235], fill=BLACK, width=2)
for i in range(0, 44, 10):
    d.line([kx+23+i, ky+235, kx+23+i+6, ky+235], fill=WHITE, width=2)
# actually draw dashed manually
for i in range(5):
    x0 = kx+23 + i*9
    d.line([x0, ky+235, x0+6, ky+235], fill=BLACK, width=2)
d.text((kx+80, ky+228), "Quit/Exit Path", font=f_key, fill=BLACK)
# System note
d.text((kx+15, ky+270), "Grey = System / Auto", font=f_key, fill=(80,80,80))
d.text((kx+15, ky+290), "Blue = Start / End", font=f_key, fill=(80,80,80))
d.text((kx+15, ky+310), "White = Player screen / choice", font=f_key, fill=(80,80,80))

out = "C:\\Users\\LOK0008\\rhythmgame\\flowchart.png"
img.save(out, dpi=(300,300))
print(f"saved to {out}  {W}x{H}")
