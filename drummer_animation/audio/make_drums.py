"""Synthesize the drum soundtrack from src/beat.json (same timing source as the visuals).

Every sound is generated from scratch with numpy/scipy: no samples.
Output: public/drums.wav (48 kHz, 16-bit stereo).
"""
import json
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfilt, fftconvolve

ROOT = Path(__file__).resolve().parent.parent
SR = 48000
rng = np.random.default_rng(7)

beat = json.loads((ROOT / "src" / "beat.json").read_text())
DUR = beat["durationInFrames"] / beat["fps"]
N = int(round(DUR * SR))


def t_axis(sec):
    return np.arange(int(sec * SR)) / SR


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo, hi], btype="band", fs=SR, output="sos"), x)


def hp(x, f, order=2):
    return sosfilt(butter(order, f, btype="high", fs=SR, output="sos"), x)


def lp(x, f, order=2):
    return sosfilt(butter(order, f, btype="low", fs=SR, output="sos"), x)


def env(t, attack, dec):
    a = np.clip(t / max(attack, 1e-5), 0, 1)
    return a * np.exp(-t / dec)


def sweep_sine(t, f0, f1, k):
    """Sine whose pitch glides f0 -> f1 exponentially (rate k), phase-continuous."""
    f = f1 + (f0 - f1) * np.exp(-t * k)
    return np.sin(2 * np.pi * np.cumsum(f) / SR)


def kick(vel, var):
    t = t_axis(0.55)
    body = sweep_sine(t, 160 * var, 47 * var, 28) * env(t, 0.001, 0.30)
    click = hp(rng.standard_normal(len(t)), 2500) * env(t, 0.0002, 0.004) * 0.5
    knock = np.sin(2 * np.pi * 900 * t) * env(t, 0.0003, 0.006) * 0.25
    x = np.tanh((body + click + knock) * 1.6) / np.tanh(1.6)
    return x * vel * 0.95


def snare(vel, var):
    t = t_axis(0.42)
    tone = (np.sin(2 * np.pi * 186 * var * t) * 0.6 + np.sin(2 * np.pi * 332 * var * t) * 0.35) * env(t, 0.0008, 0.07)
    wires = bp(rng.standard_normal(len(t)), 1800, 9500) * env(t, 0.0005, 0.16)
    crack = hp(rng.standard_normal(len(t)), 4000) * env(t, 0.0002, 0.012) * 0.8
    x = tone * 0.7 + wires * 0.75 + crack
    return np.tanh(x * 1.3) * vel * 0.8


METAL = np.array([205.3, 304.4, 369.6, 522.7, 540.0, 800.0])  # classic inharmonic ratios


def metallic(t, scale):
    x = np.zeros_like(t)
    for f in METAL * scale:
        x += np.sign(np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28)))
    return x / len(METAL)


def hihat(vel, var):
    t = t_axis(0.25)
    m = metallic(t, 1.0 * var)
    n = rng.standard_normal(len(t)) * 0.6
    x = hp(bp(m + n, 6000, 16000), 7000) * env(t, 0.0003, 0.028 + 0.03 * vel)
    tick = hp(rng.standard_normal(len(t)), 9000) * env(t, 0.0001, 0.003) * 0.6
    return (x * 1.6 + tick) * vel * 0.42


def crash(vel, var):
    t = t_axis(2.6)
    partials = np.zeros_like(t)
    for _ in range(70):
        f = rng.uniform(380, 9500) * var
        partials += np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28)) * rng.uniform(0.3, 1.0)
    partials /= 70 ** 0.5
    shimmer = 1 + 0.12 * np.sin(2 * np.pi * 5.3 * t)
    wash = hp(rng.standard_normal(len(t)), 3500) * 0.9
    body = (hp(partials, 1200) * 0.8 + wash) * env(t, 0.002, 0.85) * shimmer
    burst = hp(rng.standard_normal(len(t)), 2000) * env(t, 0.0005, 0.03) * 1.2
    return lp(body + burst, 14000) * vel * 0.34


def tom(f0, f1, dec):
    def make(vel, var):
        t = t_axis(0.9)
        body = sweep_sine(t, f0 * var, f1 * var, 9) * env(t, 0.0008, dec)
        over = sweep_sine(t, f0 * 1.6 * var, f1 * 1.55 * var, 9) * env(t, 0.0008, dec * 0.35) * 0.25
        hit = bp(rng.standard_normal(len(t)), 800, 5000) * env(t, 0.0003, 0.02) * 0.5
        return np.tanh((body + over + hit) * 1.4) * vel * 0.75

    return make


SYNTH = {
    "kick": kick,
    "snare": snare,
    "hihat": hihat,
    "crash_r": crash,
    "crash_l": crash,
    "tom1": tom(205, 168, 0.26),
    "tom2": tom(165, 135, 0.30),
    "floor": tom(108, 84, 0.42),
}

GAIN_DB = {"kick": 0, "snare": -1.5, "hihat": -7, "crash_r": -3.5, "crash_l": -3.5, "tom1": -2.5, "tom2": -2.5, "floor": -2}


def place(bus, sig, start, pan):
    """Constant-power pan, sample-accurate onset."""
    i = int(round(start * SR))
    if i >= N:
        return
    sig = sig[: N - i]
    a = (pan + 1) * np.pi / 4
    bus[i : i + len(sig), 0] += sig * np.cos(a)
    bus[i : i + len(sig), 1] += sig * np.sin(a)


def main():
    dry = np.zeros((N, 2))
    for k, h in enumerate(beat["hits"]):
        piece = h["piece"]
        var = 1 + rng.uniform(-0.012, 0.012)  # tiny per-hit pitch drift: human, not machine-gun
        vel = h["vel"] * (1 + rng.uniform(-0.04, 0.04))
        sig = SYNTH[piece](vel, var) * 10 ** (GAIN_DB[piece] / 20)
        place(dry, sig, h["t"], beat["pieces"][piece]["pan"])

    # Stick whoosh as both sticks swing up after the final crash.
    t = t_axis(0.28)
    whoosh = bp(rng.standard_normal(len(t)), 700, 3200) * np.sin(np.pi * np.clip(t / 0.28, 0, 1)) ** 2 * 0.05
    place(dry, whoosh, 3.74, -0.3)
    place(dry, whoosh[::-1] * 0.9, 3.74, 0.3)

    # Small room: decorrelated stereo noise IR, darkened.
    ir_t = t_axis(0.7)
    ir = np.stack([lp(rng.standard_normal(len(ir_t)), 6000) * np.exp(-ir_t / 0.16) for _ in range(2)], axis=1)
    ir[: int(0.012 * SR)] = 0  # pre-delay
    ir /= np.sqrt((ir ** 2).sum(axis=0))
    wet = np.stack([fftconvolve(dry[:, c], ir[:, c])[:N] for c in range(2)], axis=1)
    mix = dry + wet * 0.22

    # Quiet room tone so silence never sounds digital.
    room = lp(rng.standard_normal((N, 2)), 400, order=1) * 10 ** (-58 / 20)
    mix += room

    # Gentle bus compression (RMS follower) + soft clip.
    rms = np.sqrt(lp(np.mean(mix ** 2, axis=1), 25, order=1).clip(1e-9))
    thr = 0.25
    gain = np.where(rms > thr, (thr / rms) ** (1 - 1 / 2.5), 1.0)
    mix *= gain[:, None]
    mix = np.tanh(mix * 1.1) / 1.1

    # Fades: 4 ms in, 150 ms out (the crash rings to the very end).
    fi = int(0.004 * SR)
    fo = int(0.15 * SR)
    mix[:fi] *= np.linspace(0, 1, fi)[:, None]
    mix[-fo:] *= (np.cos(np.linspace(0, np.pi, fo)) * 0.5 + 0.5)[:, None]

    peak = np.abs(mix).max()
    mix *= 10 ** (-1.5 / 20) / peak  # normalize to -1.5 dBFS (headroom for AAC)
    out = np.clip(mix + rng.uniform(-0.5, 0.5, mix.shape) / 32768, -1, 1)  # TPDF-ish dither
    dst = ROOT / "public" / "drums.wav"
    wavfile.write(dst, SR, (out * 32767).astype(np.int16))

    # Report onset alignment: the loudest rise near each hit must sit at its timestamp.
    mono = np.abs(out).mean(axis=1)
    errs = []
    for h in beat["hits"]:
        i = int(round(h["t"] * SR))
        seg = mono[max(0, i - 480) : i + 480]
        on = np.argmax(seg > seg.max() * 0.5) + max(0, i - 480) - i
        errs.append(on / SR * 1000)
    print(f"wrote {dst} {DUR:.2f}s, peak -1.5 dBFS, onset error max {max(map(abs, errs)):.1f} ms")


if __name__ == "__main__":
    main()
