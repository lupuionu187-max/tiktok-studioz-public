"""Score + sound design for the canvas shorts, synthesized from scratch.

Usage: python audio/make_story_audio.py [cue json under src/story] [output wav under public]
Defaults to the 'Discipline' short (story.json -> story.wav). The cue file is the same one
the visuals read. 48 kHz, 16-bit stereo. No samples: numpy/scipy only.
"""
import json
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, fftconvolve, sosfilt

ROOT = Path(__file__).resolve().parent.parent
SR = 48000
rng = np.random.default_rng(11)
CUE = sys.argv[1] if len(sys.argv) > 1 else "story.json"
OUT = sys.argv[2] if len(sys.argv) > 2 else "story.wav"
story = json.loads((ROOT / "src" / "story" / CUE).read_text())
OPTS = {"rise_full_at": 52.0, "rise_big_at": 71.0, "cymbals": [52.0, 71.0, 78.4], "drone": [13.0, 26.5], "hush_rising": False}
OPTS.update(story.get("music_opts", {}))
DUR = float(story["duration"])
N = int(DUR * SR)


# ----------------------------------------------------------------- helpers
def ta(sec):
    return np.arange(int(sec * SR)) / SR


def filt(x, kind, f, order=2):
    return sosfilt(butter(order, f, btype=kind, fs=SR, output="sos"), x)


def env(t, a, d):
    return np.clip(t / max(a, 1e-5), 0, 1) * np.exp(-t / d)


def adsr(n, a, r, sus=1.0):
    e = np.ones(n) * sus
    na, nr = int(a * SR), int(r * SR)
    na = min(na, n)
    e[:na] = np.linspace(0, sus, na)
    if nr > 0 and nr < n:
        e[-nr:] *= np.linspace(1, 0, nr)
    return e


def noise(n):
    return rng.standard_normal(n)


def sweep(t, f0, f1, curve="exp"):
    if curve == "exp":
        f = f0 * (f1 / f0) ** (t / t[-1])
    else:
        f = f0 + (f1 - f0) * t / t[-1]
    return np.sin(2 * np.pi * np.cumsum(f) / SR)


def bandsweep(n, f0, f1):
    """Noise through a band that glides f0 -> f1 (block-wise)."""
    x = noise(n)
    out = np.zeros(n)
    blocks = 24
    for b in range(blocks):
        i0, i1 = n * b // blocks, n * (b + 1) // blocks
        f = f0 * (f1 / f0) ** (b / (blocks - 1))
        lo, hi = max(40, f * 0.6), min(SR / 2 - 100, f * 1.6)
        seg = filt(x[max(0, i0 - 2000) : i1], "band", [lo, hi])
        out[i0:i1] = seg[-(i1 - i0) :]
    return out


def midi(m):
    return 440.0 * 2 ** ((m - 69) / 12)


class Bus:
    def __init__(self):
        self.x = np.zeros((N, 2))

    def add(self, sig, t0, gain=1.0, pan=0.0):
        i = int(round(t0 * SR))
        if i >= N or len(sig) == 0:
            return
        if i < 0:
            sig = sig[-i:]
            i = 0
        sig = sig[: N - i] * gain
        a = (pan + 1) * np.pi / 4
        self.x[i : i + len(sig), 0] += sig * np.cos(a)
        self.x[i : i + len(sig), 1] += sig * np.sin(a)


music = Bus()
sfx = Bus()

# ----------------------------------------------------------------- instruments
def piano(f, dur=2.5, vel=0.6):
    t = ta(dur)
    x = np.zeros_like(t)
    for h, amp in [(1, 1.0), (2, 0.45), (3, 0.22), (4, 0.12), (5, 0.06)]:
        x += amp * np.sin(2 * np.pi * f * h * t * (1 + 0.0004 * h * h)) * np.exp(-t * (1.4 + 0.9 * h))
    hammer = filt(noise(len(t)), "band", [800, 4000]) * env(t, 0.0005, 0.01) * 0.15
    return (x * env(t, 0.004, 1.6) + hammer) * vel


def pad(freqs, dur, gain=0.18, bright=1400, attack=1.0, release=1.2):
    t = ta(dur)
    x = np.zeros_like(t)
    for f in freqs:
        for det in (-0.12, 0.0, 0.11):
            ph = rng.uniform(0, 1)
            saw = 2 * ((f * (1 + det / 100 * 6) * t + ph) % 1) - 1
            x += saw
    x = filt(x / (len(freqs) * 3), "low", bright)
    return x * adsr(len(t), attack, release) * gain


def bass(f, dur, gain=0.35):
    t = ta(dur)
    saw = 2 * ((f * t) % 1) - 1
    sub = np.sin(2 * np.pi * f * t)
    x = filt(saw, "low", 500) * 0.6 + sub * 0.7
    return np.tanh(x * 1.5) * env(t, 0.004, dur * 0.9) * gain


def pluck(f, dur=0.5, gain=0.18):
    t = ta(dur)
    x = np.sign(np.sin(2 * np.pi * f * t)) * 0.5 + np.sin(2 * np.pi * f * 2 * t) * 0.3
    return filt(x, "low", 2600) * env(t, 0.002, 0.16) * gain


def lead(f, dur, gain=0.12):
    t = ta(dur)
    vib = 1 + 0.004 * np.sin(2 * np.pi * 5.2 * t) * np.clip(t / 0.4, 0, 1)
    ph = np.cumsum(f * vib) / SR
    x = (2 * (ph % 1) - 1) + 0.5 * (2 * ((ph * 1.003) % 1) - 1)
    return filt(x, "low", 2800) * adsr(len(t), 0.12, 0.3) * gain


def kick(gain=0.9):
    t = ta(0.5)
    body = sweep(t, 140, 45) * env(t, 0.001, 0.22)
    click = filt(noise(len(t)), "high", 3000) * env(t, 0.0002, 0.004) * 0.4
    return np.tanh((body + click) * 1.5) * gain


def clap(gain=0.5):
    t = ta(0.4)
    x = np.zeros_like(t)
    for d in (0, 0.009, 0.018):
        tt = np.clip(t - d, 0, None)
        x += filt(noise(len(t)), "band", [900, 5000]) * env(tt, 0.0005, 0.03) * (t >= d)
    tail = filt(noise(len(t)), "band", [1000, 4000]) * env(t, 0.002, 0.12) * 0.5
    return (x + tail) * gain


def hat(gain=0.18):
    t = ta(0.12)
    return filt(noise(len(t)), "high", 7500) * env(t, 0.0003, 0.025) * gain


def taiko(gain=0.8):
    t = ta(1.2)
    body = sweep(t, 110, 55) * env(t, 0.002, 0.45)
    skin = filt(noise(len(t)), "band", [200, 1200]) * env(t, 0.001, 0.05) * 0.5
    return np.tanh((body + skin) * 1.4) * gain


def cymbal(dur=2.5, gain=0.25):
    t = ta(dur)
    x = filt(noise(len(t)), "high", 4000) * env(t, 0.002, 0.9)
    return x * gain


# ----------------------------------------------------------------- music
CH_MIN = {  # Am F C G (A minor), voiced around octave 3-4
    "Am": [57, 60, 64],
    "F": [53, 57, 60],
    "C": [52, 55, 60],
    "G": [55, 59, 62],
}
ROOT_NOTE = {"Am": 45, "F": 41, "C": 48, "G": 43}
PROG = ["Am", "F", "C", "G"]


def sec(name):
    s = next(m for m in story["music"] if m["section"] == name)
    return s["t0"], s["t1"]


# Sad: slow felt piano + soft pad, 72 bpm, one chord per bar.
t0, t1 = sec("sad")
bar = 4 * 60 / 72
melody = [76, 74, 72, 71, 72, 69, 67, 71]
b = 0
t = t0 + 0.4
while t < t1 - 0.5:
    ch = PROG[b % 4]
    notes = CH_MIN[ch]
    music.add(pad([midi(n) for n in notes], bar + 1.4, gain=0.15, bright=1100), t, pan=0)
    beat = bar / 4
    music.add(piano(midi(ROOT_NOTE[ch] + 12), 3.0, 0.32), t, pan=-0.1)
    arp = [notes[0] + 12, notes[1] + 12, notes[2] + 12, notes[1] + 12]
    for i, n in enumerate(arp):
        music.add(piano(midi(n), 2.2, 0.17), t + i * beat + beat * 0.5, pan=0.15)
    if b >= 2:
        music.add(piano(midi(melody[b % len(melody)]), 2.6, 0.2), t + 0.02, pan=0.05)
    t += bar
    b += 1

# Shadow drone under the dark scenes.
dr = ta(OPTS["drone"][1])
drone = (np.sin(2 * np.pi * 55 * dr) + 0.6 * np.sin(2 * np.pi * 82.4 * dr + 0.3) + 0.4 * filt(noise(len(dr)), "low", 220))
drone *= adsr(len(dr), 3.0, 2.5) * (0.6 + 0.4 * np.clip((dr - 16) / 8, 0, 1))
music.add(drone * 0.12, OPTS["drone"][0])

# Hush: one long, quiet chord.
t0, t1 = sec("hush")
music.add(pad([midi(n) for n in CH_MIN["Am"]], t1 - t0 + 1.5, gain=0.08, bright=700, attack=1.5, release=1.5), t0)
if OPTS["hush_rising"]:
    # Something growing in the dark: a swelling pad and a slow, quickening heartbeat pulse.
    music.add(pad([midi(n) for n in [45, 52, 57, 64]], t1 - t0 + 0.5, gain=0.16, bright=900, attack=t1 - t0, release=0.3), t0)
    tt = t0 + 0.8
    gap = 1.0
    while tt < t1 - 0.2:
        music.add(taiko(0.35 + 0.4 * (tt - t0) / (t1 - t0)), tt)
        tt += gap
        gap = max(0.35, gap * 0.86)

# Rise: 100 bpm, building drums + bass + plucks, then lead.
t0, t1 = sec("rise")
beat = 60 / 100
bar = 4 * beat
lead_mel = [69, 72, 76, 74, 72, 71, 72, 74, 76, 79, 77, 76]
b = 0
t = t0
while t < t1 - 0.05:
    ch = PROG[b % 4]
    notes = CH_MIN[ch]
    full = t >= OPTS["rise_full_at"] - 0.01
    big = t >= OPTS["rise_big_at"] - 0.01
    music.add(pad([midi(n) for n in notes] + ([midi(notes[0] + 12)] if big else []), bar + 0.6, gain=0.13 if not big else 0.17, bright=1800 if big else 1300, attack=0.3, release=0.5), t)
    for i in range(4):
        tb = t + i * beat
        if tb >= t1:
            break
        if full:
            music.add(kick(0.85), tb)
            if i % 2 == 1:
                music.add(clap(0.42), tb, pan=0.05)
            for e in range(2):
                music.add(hat(0.16 if e else 0.11), tb + e * beat / 2, pan=0.3)
            for e in range(2):
                music.add(bass(midi(ROOT_NOTE[ch] - 12 + (12 if e else 0)), beat / 2, 0.3), tb + e * beat / 2)
            for e in range(2):
                n = notes[(i * 2 + e) % 3] + 12 + (12 if (i + e) % 4 == 3 else 0)
                music.add(pluck(midi(n), 0.4, 0.12), tb + e * beat / 2, pan=-0.25 if e else 0.25)
        else:
            if i in (0, 2):
                music.add(taiko(0.7), tb)
            music.add(bass(midi(ROOT_NOTE[ch] - 12), beat, 0.22), tb)
    if big:
        music.add(lead(midi(lead_mel[(b * 2) % len(lead_mel)]), bar * 0.5, 0.11), t, pan=0.1)
        music.add(lead(midi(lead_mel[(b * 2 + 1) % len(lead_mel)]), bar * 0.5, 0.11), t + bar * 0.5, pan=0.1)
    t += bar
    b += 1
for i, ct in enumerate(OPTS["cymbals"]):
    music.add(cymbal(3.0, 0.24), ct, pan=-0.2 if i % 2 == 0 else 0.2)

# Dawn: drums drop, major-feeling progression C G Am F with high piano.
t0, t1 = sec("dawn")
dawn = ["C", "G", "Am", "F"]
bar = 1.5
for i, ch in enumerate(dawn):
    tt = t0 + i * bar
    music.add(pad([midi(n + 12) for n in CH_MIN[ch]] + [midi(ROOT_NOTE[ch])], bar + 1.2, gain=0.16, bright=2200, attack=0.4, release=0.8), tt)
    for k, n in enumerate(CH_MIN[ch]):
        music.add(piano(midi(n + 24), 2.0, 0.16), tt + k * 0.18, pan=0.2 - k * 0.2)

# End: held chord after the boom.
t0, t1 = sec("end")
music.add(pad([midi(n) for n in [45, 52, 57, 60, 64]], t1 - t0 + 0.5, gain=0.2, bright=1600, attack=0.05, release=1.6), t0)
music.add(piano(midi(57), 4.0, 0.35), t0 + 0.02)
music.add(piano(midi(69), 4.0, 0.25), t0 + 0.02)

# ----------------------------------------------------------------- SFX
def s_alarm(dur):
    out = np.zeros(int(dur * SR))
    group = 0.62
    t = 0.0
    while t < dur:
        for k in range(4):
            tt = t + k * 0.105
            if tt + 0.07 >= dur:
                break
            bt = ta(0.07)
            beep = np.sign(np.sin(2 * np.pi * 2350 * bt)) * adsr(len(bt), 0.003, 0.01)
            i = int(tt * SR)
            out[i : i + len(bt)] += filt(beep, "low", 6000) * 0.5
        t += group
    return out


def s_click(f=2200, nlen=0.012):
    t = ta(0.12)
    return filt(noise(len(t)), "high", 1500) * env(t, 0.0002, nlen) * 0.6 + np.sin(2 * np.pi * f * t) * env(t, 0.0005, 0.01) * 0.3


def s_thump(f0=110, f1=55, d=0.16, g=1.0):
    t = ta(0.6)
    return (sweep(t, f0, f1) * env(t, 0.002, d) + filt(noise(len(t)), "low", 900) * env(t, 0.001, 0.03) * 0.3) * g


def s_whoosh(dur=0.45, f0=300, f1=2600):
    n = int(dur * SR)
    x = bandsweep(n, f0, f1)
    t = ta(dur)
    return x * np.sin(np.pi * np.clip(t / dur, 0, 1)) ** 2


def s_darkwhoosh():
    x = s_whoosh(1.0, 900, 140) * 1.2
    t = ta(1.0)
    x += np.sin(2 * np.pi * 42 * t) * env(t, 0.25, 0.4) * 0.6
    return x


def s_whisper(dur):
    n = int(dur * SR)
    t = ta(dur)
    breath = filt(noise(n), "band", [1400, 6500])
    # Syllable-like amplitude bumps.
    syl = np.zeros(n)
    tt = 0.05
    while tt < dur - 0.1:
        L = rng.uniform(0.12, 0.26)
        i0, i1 = int(tt * SR), min(n, int((tt + L) * SR))
        syl[i0:i1] += np.sin(np.linspace(0, np.pi, i1 - i0)) ** 1.5 * rng.uniform(0.6, 1.0)
        tt += L + rng.uniform(0.02, 0.09)
    growl = filt(2 * ((65 * t) % 1) - 1, "low", 300) * (0.5 + 0.5 * np.sin(2 * np.pi * 9 * t))
    x = breath * syl * 0.8 + growl * syl * 0.35
    return x * adsr(n, 0.05, 0.25)


def s_ticks(dur):
    out = np.zeros(int(dur * SR))
    t = 0.0
    while t < dur - 0.02:
        rate = 14 * (1 - t / dur) + 1.5
        c = s_click(3200, 0.004) * 0.5
        i = int(t * SR)
        seg = c[: len(out) - i]
        out[i : i + len(seg)] += seg
        t += 1 / rate
    return out


def s_drone(dur):
    t = ta(dur)
    x = np.sin(2 * np.pi * 36.7 * t) + 0.5 * np.sin(2 * np.pi * 55 * t * (1 + 0.002 * np.sin(t)))
    x += filt(noise(len(t)), "low", 160) * 0.8
    return x * adsr(len(t), dur * 0.7, 0.6) * 0.5


def s_crack():
    t = ta(1.2)
    x = filt(noise(len(t)), "high", 1800) * env(t, 0.0002, 0.03) * 1.4
    for _ in range(40):
        f = rng.uniform(2200, 9500)
        x += np.sin(2 * np.pi * f * t + rng.uniform(0, 6)) * env(t, 0.0003, rng.uniform(0.02, 0.18)) * 0.08
    for _ in range(16):  # tinkles
        d = rng.uniform(0.05, 0.7)
        tt = np.clip(t - d, 0, None)
        x += np.sin(2 * np.pi * rng.uniform(3000, 8000) * tt) * env(tt, 0.0002, 0.02) * (t >= d) * 0.12
    x += sweep(t, 90, 40) * env(t, 0.002, 0.25) * 0.6
    return x


def s_heartbeat():
    out = s_thump(70, 45, 0.11, 1.0)
    dub = s_thump(60, 40, 0.13, 0.8)
    i = int(0.2 * SR)
    out[i : i + len(dub) - i] += dub[: len(out) - i]
    return filt(out, "low", 400)


def s_slam():
    x = s_click(1800, 0.02) * 1.2
    th = s_thump(150, 60, 0.12, 1.1)
    out = np.zeros(int(0.7 * SR))
    out[: len(x)] += x
    out[: len(th)] += th[: len(out)]
    t = ta(0.7)
    out += filt(noise(len(t)), "band", [2000, 6000]) * env(t, 0.01, 0.08) * 0.25 * (0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 30 * t)))
    return out


def s_swish():
    return s_whoosh(0.22, 1800, 6000) * 0.8


def s_steps(dur, rate):
    out = np.zeros(int(dur * SR))
    t = 0.0
    k = 0
    while t < dur - 0.05:
        st = s_thump(130 + rng.uniform(-10, 10), 70, 0.05, 0.9)
        sc = filt(noise(int(0.08 * SR)), "band", [1500, 5000]) * env(ta(0.08), 0.002, 0.02) * 0.25
        i = int(t * SR)
        seg = st[: len(out) - i]
        out[i : i + len(seg)] += seg * (0.85 + 0.15 * (k % 2))
        seg = sc[: len(out) - i]
        out[i : i + len(seg)] += seg
        t += rate
        k += 1
    return out


def s_punch(big=False):
    t = ta(1.4 if big else 0.5)
    body = sweep(t, 160 if big else 120, 45) * env(t, 0.001, 0.2 if big else 0.08)
    slap = filt(noise(len(t)), "band", [700, 4500]) * env(t, 0.0003, 0.035 if big else 0.02)
    x = np.tanh((body * 1.2 + slap * 0.9) * (2.0 if big else 1.4))
    if big:
        x += sweep(t, 70, 28) * env(t, 0.005, 0.5) * 0.7
    return x


def s_shatter():
    t = ta(1.6)
    x = filt(noise(len(t)), "high", 900) * env(t, 0.003, 0.35)
    x += bandsweep(len(t), 2500, 200) * env(t, 0.05, 0.6) * 0.6
    return x * 0.8


def s_swell(dur):
    return pad([midi(n) for n in [60, 64, 67, 72]], dur, gain=0.9, bright=2600, attack=dur * 0.8, release=0.4)


def s_wind(dur):
    n = int(dur * SR)
    t = ta(dur)
    x = filt(noise(n), "low", 700) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.35 * t))
    x += filt(noise(n), "band", [900, 1400]) * 0.25 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.21 * t + 1))
    return x * adsr(n, 0.8, 1.0)


def s_riser(dur):
    n = int(dur * SR)
    t = ta(dur)
    x = bandsweep(n, 300, 7000) * (t / dur) ** 2
    x += sweep(t, 200, 900) * (t / dur) ** 2 * 0.3
    return x


def s_boom():
    t = ta(4.0)
    x = sweep(t, 60, 26) * env(t, 0.003, 1.2) * 1.2
    x += filt(noise(len(t)), "low", 1500) * env(t, 0.002, 0.25) * 0.6
    x += filt(noise(len(t)), "high", 3000) * env(t, 0.001, 0.05) * 0.4
    return np.tanh(x * 1.3)


def s_birds(dur):
    out = np.zeros(int(dur * SR))
    tt = 0.2
    while tt < dur - 0.4:
        for k in range(int(rng.integers(2, 5))):
            t = ta(0.09)
            f0 = rng.uniform(2600, 4200)
            chirp = np.sin(2 * np.pi * np.cumsum(f0 + 1800 * np.sin(np.pi * t / 0.09)) / SR) * np.sin(np.pi * t / 0.09) ** 2
            i = int((tt + k * 0.11) * SR)
            seg = chirp[: max(0, len(out) - i)]
            out[i : i + len(seg)] += seg * 0.3
        tt += rng.uniform(0.6, 1.4)
    return out


def s_pour(dur):
    n = int(dur * SR)
    t = ta(dur)
    x = filt(noise(n), "band", [700, 5000]) * 0.5
    bubbles = np.zeros(n)
    tt = 0.0
    while tt < dur:
        bt = ta(0.03)
        f = rng.uniform(500, 1400)
        b = np.sin(2 * np.pi * np.cumsum(f * (1 + 2 * bt / 0.03)) / SR) * env(bt, 0.001, 0.008)
        i = int(tt * SR)
        seg = b[: max(0, n - i)]
        bubbles[i : i + len(seg)] += seg * 0.4
        tt += rng.uniform(0.015, 0.05)
    return (x + bubbles) * adsr(n, 0.15, 0.3)


def s_chatter(dur):
    n = int(dur * SR)
    t = ta(dur)
    out = np.zeros(n)
    for voice, (lo, hi) in enumerate([(300, 1400), (450, 2000)]):
        syl = np.zeros(n)
        tt = 0.05 + voice * 0.3
        while tt < dur - 0.1:
            L = rng.uniform(0.09, 0.2)
            i0, i1 = int(tt * SR), min(n, int((tt + L) * SR))
            syl[i0:i1] += np.sin(np.linspace(0, np.pi, i1 - i0)) * rng.uniform(0.5, 1.0)
            tt += L + rng.uniform(0.03, 0.25 if rng.uniform() > 0.2 else 0.5)
        f0 = 140 + voice * 90
        buzz = 2 * ((f0 * (1 + 0.08 * np.sin(2 * np.pi * 3 * t)) * t) % 1) - 1
        out += filt(buzz * 0.6 + noise(n) * 0.4, "band", [lo, hi]) * syl
    return filt(out, "low", 1800) * adsr(n, 0.6, 0.8)


def s_rain(dur):
    n = int(dur * SR)
    x = filt(noise(n), "band", [400, 9000]) * 0.5 + filt(noise(n), "low", 600) * 0.4
    drops = np.zeros(n)
    for _ in range(int(dur * 40)):
        i = int(rng.uniform(0, dur) * SR)
        d = filt(noise(300), "high", 3000) * np.exp(-np.arange(300) / 40)
        drops[i : i + 300] += d[: max(0, n - i)] * rng.uniform(0.1, 0.4)
    return (x + drops) * adsr(n, 0.8, 0.8)


def s_thunder():
    t = ta(3.5)
    x = filt(noise(len(t)), "low", 300) * env(t, 0.02, 1.2) * 1.4
    x += filt(noise(len(t)), "band", [300, 2500]) * env(t, 0.002, 0.15) * 0.7
    return x * (0.7 + 0.3 * np.sin(2 * np.pi * 3.1 * t))


def s_rumble(dur):
    t = ta(dur)
    x = filt(noise(len(t)), "low", 120) * 1.6 + np.sin(2 * np.pi * 38 * t) * 0.4
    return x * adsr(len(t), min(1.0, dur * 0.4), 0.4)


def s_grow(dur):
    n = int(dur * SR)
    t = ta(dur)
    x = s_riser(dur) * 0.6
    for _ in range(int(dur * 6)):  # woody creaks
        i = int(rng.uniform(0, dur - 0.2) * SR)
        ct = ta(0.15)
        cr = filt(np.sign(np.sin(2 * np.pi * rng.uniform(70, 160) * ct)) * env(ct, 0.01, 0.06), "band", [200, 2500])
        x[i : i + len(cr)] += cr * 0.35
    return x


def s_groundcrack():
    t = ta(1.2)
    x = filt(noise(len(t)), "low", 2500) * env(t, 0.001, 0.08) * 1.5
    x += sweep(t, 80, 35) * env(t, 0.002, 0.3)
    for _ in range(10):
        d = rng.uniform(0.0, 0.25)
        tt = np.clip(t - d, 0, None)
        x += filt(noise(len(t)), "band", [600, 3000]) * env(tt, 0.0005, 0.02) * (t >= d) * 0.4
    return x


def s_pop():
    t = ta(0.12)
    return np.sin(2 * np.pi * np.cumsum(300 + 1400 * (t / 0.12)) / SR) * env(t, 0.001, 0.05) + s_click(1800, 0.006)[: len(t)] * 0.5


def s_leaves(dur):
    n = int(dur * SR)
    t = ta(dur)
    x = filt(noise(n), "band", [1500, 8000]) * (0.4 + 0.6 * np.abs(np.sin(2 * np.pi * 1.3 * t)) ** 3)
    return x * adsr(n, 0.4, 0.6) * 0.5


def s_dig():
    t = ta(0.5)
    scrape = filt(noise(len(t)), "band", [300, 2500]) * env(t, 0.01, 0.12)
    thump = s_thump(120, 60, 0.06, 0.6)[: len(t)]
    dirt = np.zeros(len(t))
    for _ in range(12):
        d = rng.uniform(0.15, 0.45)
        tt = np.clip(t - d, 0, None)
        dirt += filt(noise(len(t)), "band", [800, 3500]) * env(tt, 0.001, 0.015) * (t >= d) * 0.25
    return scrape * 0.7 + thump + dirt


def s_clank():
    t = ta(0.8)
    x = np.zeros_like(t)
    for f, d in [(1870, 0.09), (2930, 0.06), (4410, 0.04), (760, 0.12)]:
        x += np.sin(2 * np.pi * f * t) * env(t, 0.0003, d) * 0.3
    x += filt(noise(len(t)), "high", 2000) * env(t, 0.0003, 0.02) * 0.9
    th = s_thump(150, 70, 0.05, 0.5)[: len(t)]
    x[: len(th)] += th
    return x


def s_pebbles():
    t = ta(1.4)
    x = np.zeros_like(t)
    for _ in range(18):
        d = rng.uniform(0, 1.2)
        tt = np.clip(t - d, 0, None)
        x += filt(noise(len(t)), "band", [1500, 6000]) * env(tt, 0.0005, 0.01) * (t >= d) * rng.uniform(0.2, 0.6)
    return x


def s_drop():
    x = s_clank() * 0.6
    y = np.zeros(int(0.6 * SR))
    i = int(0.18 * SR)
    seg = (s_clank() * 0.3)[: len(y) - i]
    y[i : i + len(seg)] += seg
    out = np.zeros(max(len(x), len(y)))
    out[: len(x)] += x
    out[: len(y)] += y
    return out


def s_rewind(dur):
    n = int(dur * SR)
    t = ta(dur)
    warble = np.sin(2 * np.pi * np.cumsum(900 + 500 * np.sin(2 * np.pi * 7 * t) + 1200 * t / dur) / SR)
    hiss = filt(noise(n), "band", [2000, 9000]) * 0.5
    chirps = np.sign(np.sin(2 * np.pi * 23 * t)) * filt(noise(n), "band", [600, 3000]) * 0.3
    return (warble * 0.35 + hiss + chirps) * adsr(n, 0.05, 0.2)


def s_chime():
    t = ta(2.0)
    x = np.zeros_like(t)
    for f in [1318.5, 1975.5, 2637.0, 3951.0]:
        x += np.sin(2 * np.pi * f * t) * env(t, 0.002, 0.7) * 0.25
    return x


def s_chimes(dur):
    out = np.zeros(int(dur * SR))
    tt = 0.0
    notes = [1318.5, 1567.98, 1975.5, 2637.0, 3135.96]
    while tt < dur - 0.3:
        t = ta(1.2)
        f = notes[int(rng.integers(0, len(notes)))]
        c = np.sin(2 * np.pi * f * t) * env(t, 0.002, 0.5) * 0.2 + np.sin(2 * np.pi * f * 2.01 * t) * env(t, 0.002, 0.25) * 0.08
        i = int(tt * SR)
        seg = c[: len(out) - i]
        out[i : i + len(seg)] += seg
        tt += rng.uniform(0.06, 0.18)
    return out * adsr(len(out), 0.02, 0.8)


def s_collapse():
    t = ta(2.2)
    x = filt(noise(len(t)), "low", 900) * env(t, 0.005, 0.5) * 1.3
    x += sweep(t, 70, 30) * env(t, 0.003, 0.6) * 0.8
    for _ in range(30):
        d = rng.uniform(0.05, 1.4)
        tt = np.clip(t - d, 0, None)
        x += filt(noise(len(t)), "band", [500, 3000]) * env(tt, 0.0008, 0.03) * (t >= d) * rng.uniform(0.1, 0.4)
    return x


SFX = {
    "alarm": lambda c: s_alarm(c["dur"]),
    "snooze": lambda c: s_click(2000, 0.02) + np.pad(s_thump(120, 70, 0.05, 0.4), (0, 0))[: int(0.12 * SR)],
    "tap": lambda c: s_click(4000, 0.004) * 0.6,
    "darkwhoosh": lambda c: s_darkwhoosh(),
    "whisper": lambda c: s_whisper(c["dur"]),
    "ticks": lambda c: s_ticks(c["dur"]),
    "thud": lambda c: s_thump(95, 55, 0.12, 0.9),
    "drone": lambda c: s_drone(c["dur"]),
    "crack": lambda c: s_crack(),
    "heartbeat": lambda c: s_heartbeat(),
    "slam": lambda c: s_slam(),
    "whoosh": lambda c: s_whoosh(),
    "swish": lambda c: s_swish(),
    "thump": lambda c: s_thump(100, 55, 0.08, 0.8),
    "steps": lambda c: s_steps(c["dur"], c["rate"]),
    "punch": lambda c: s_punch(False),
    "bigpunch": lambda c: s_punch(True),
    "shatter": lambda c: s_shatter(),
    "swell": lambda c: s_swell(c["dur"]),
    "wind": lambda c: s_wind(c["dur"]),
    "riser": lambda c: s_riser(c["dur"]),
    "boom": lambda c: s_boom(),
    "hit": lambda c: s_punch(False) * 0.7,
    "birds": lambda c: s_birds(c["dur"]),
    "seeddrop": lambda c: s_click(2600, 0.004) * 0.6 + np.pad(s_thump(200, 120, 0.02, 0.3), (0, 0))[: int(0.12 * SR)],
    "pat": lambda c: filt(noise(int(0.12 * SR)), "low", 700) * env(ta(0.12), 0.002, 0.03) + s_thump(110, 70, 0.04, 0.4)[: int(0.12 * SR)],
    "pour": lambda c: s_pour(c["dur"]),
    "chatter": lambda c: s_chatter(c["dur"]),
    "rain": lambda c: s_rain(c["dur"]),
    "thunder": lambda c: s_thunder(),
    "rumble": lambda c: s_rumble(c["dur"]),
    "grow": lambda c: s_grow(c["dur"]),
    "groundcrack": lambda c: s_groundcrack(),
    "pop": lambda c: s_pop(),
    "leaves": lambda c: s_leaves(c["dur"]),
    "dig": lambda c: s_dig(),
    "clank": lambda c: s_clank(),
    "pebbles": lambda c: s_pebbles(),
    "drop": lambda c: s_drop(),
    "rewind": lambda c: s_rewind(c["dur"]),
    "chime": lambda c: s_chime(),
    "chimes": lambda c: s_chimes(c["dur"]),
    "collapse": lambda c: s_collapse(),
}

for c in story["sfx"]:
    sig = SFX[c["name"]](c)
    sfx.add(sig, c["t"], gain=c.get("vol", 0.6), pan=c.get("pan", 0.0))

# ----------------------------------------------------------------- mix
def reverb(x, secs, damp, mix):
    t = ta(secs)
    ir = np.stack([filt(noise(len(t)), "low", damp) * np.exp(-t / (secs / 5)) for _ in range(2)], axis=1)
    ir[: int(0.015 * SR)] = 0
    ir /= np.sqrt((ir ** 2).sum(axis=0))
    wet = np.stack([fftconvolve(x[:, c], ir[:, c])[:N] for c in range(2)], axis=1)
    return x + wet * mix


m = reverb(music.x, 2.4, 5000, 0.35)
s = reverb(sfx.x, 0.9, 6000, 0.18)

# Duck the music a little under the inner voice.
duck = np.ones(N)
for c in story["sfx"]:
    if c["name"] == "whisper":
        i0, i1 = int((c["t"] - 0.2) * SR), int((c["t"] + c["dur"] + 0.3) * SR)
        duck[max(0, i0) : i1] = 0.6
duck = filt(duck, "low", 3, order=1)
mix = m * duck[:, None] + s

# Gentle bus compression + soft clip, then loudness/peak normalization.
rms = np.sqrt(filt(np.mean(mix ** 2, axis=1), "low", 8, order=1).clip(1e-10))
thr = 0.12
gain = np.where(rms > thr, (thr / rms) ** (1 - 1 / 2.0), 1.0)
mix *= gain[:, None]
mix = np.tanh(mix * 1.2) / 1.2
fi, fo = int(0.01 * SR), int(0.4 * SR)
mix[:fi] *= np.linspace(0, 1, fi)[:, None]
mix[-fo:] *= np.linspace(1, 0, fo)[:, None]
mix *= 10 ** (-1.5 / 20) / np.abs(mix).max()
out = np.clip(mix + rng.uniform(-0.5, 0.5, mix.shape) / 32768, -1, 1)
dst = ROOT / "public" / OUT
wavfile.write(dst, SR, (out * 32767).astype(np.int16))
print(f"wrote {dst} ({DUR:.0f}s), {len(story['sfx'])} sfx cues")
