#!/usr/bin/env python3
"""Plot a HackRF peak log made while testing the iqlab tone.

Log format (one line per sweep bin-peak): "<seconds> <peak MHz> <peak dB>",
produced by:

    hackrf_sweep -f 2434:2442 -w 10000 -l 24 -g 30 | python3 watch.py

where watch.py writes the strongest bin of each CSV row. Usage:

    plot_tone_log.py watch.log out.png [--center 2437]

The plot shows detected tone segments (frequency vs time) and the peak power.
The known HackRF artifact near 2440.05 MHz is ignored.
"""
import argparse
import collections

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

STRONG_DB = -45.0
ARTIFACT = (2439.9, 2440.2)
MIN_HITS = 100  # hits in one second to count as a steady tone


def load(path, lo, hi):
    rows = []
    with open(path) as fh:
        for line in fh:
            t, f, d = line.split()
            t, f, d = float(t), float(f), float(d)
            if lo < f < hi and not ARTIFACT[0] < f < ARTIFACT[1]:
                rows.append((t, f, d))
    return rows


def segments(rows):
    """Steady tone segments as (start_s, end_s, freq_MHz, max_dB)."""
    per_sec = collections.defaultdict(list)
    for t, f, d in rows:
        if d > STRONG_DB:
            per_sec[int(t)].append((round(f, 2), d))
    segs = []
    for sec in sorted(per_sec):
        freq, hits = collections.Counter(f for f, _ in per_sec[sec]).most_common(1)[0]
        if hits < MIN_HITS:
            continue
        peak = max(d for f, d in per_sec[sec] if f == freq)
        if segs and segs[-1][2] == freq and sec - segs[-1][1] <= 2:
            segs[-1][1] = sec + 1
            segs[-1][3] = max(segs[-1][3], peak)
        else:
            segs.append([sec, sec + 1, freq, peak])
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("png")
    ap.add_argument("--center", type=float, default=2437.0, help="channel centre, MHz")
    ap.add_argument("--span", type=float, default=8.0, help="plot +/- MHz around centre")
    args = ap.parse_args()

    lo, hi = args.center - args.span, args.center + args.span
    rows = load(args.log, lo, hi)
    strong = [r for r in rows if r[2] > STRONG_DB]
    segs = segments(rows)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1.4]})
    ax1.scatter([r[0] for r in strong], [r[1] for r in strong], s=3, alpha=0.25, color="#7F77DD")
    ax1.axhline(args.center, color="#888780", lw=0.8, ls="--")
    ax1.text(0.5, args.center + 0.12, "channel centre %.0f MHz" % args.center, fontsize=9, color="#5F5E5A")
    for start, end, freq, peak in segs:
        ax1.hlines(freq, start, end, color="#D85A30", lw=3)
        ax1.annotate("%.2f MHz\n(%+.2f)" % (freq, freq - args.center), (start, freq),
                     xytext=(3, 6), textcoords="offset points", fontsize=8, color="#993C1D")
    ax1.set_ylabel("peak frequency (MHz)")
    ax1.set_ylim(lo, hi)
    ax1.set_title("iqlab tone test: HackRF peak log (%d tone segments)" % len(segs))
    ax1.grid(alpha=0.2)

    ax2.scatter([r[0] for r in rows], [r[2] for r in rows], s=2, alpha=0.2, color="#378ADD")
    ax2.axhline(STRONG_DB, color="#888780", lw=0.8, ls=":")
    ax2.set_ylabel("peak power (dB)")
    ax2.set_xlabel("time (s)")
    ax2.grid(alpha=0.2)

    fig.tight_layout()
    fig.savefig(args.png, dpi=140)
    print("segments:")
    for start, end, freq, peak in segs:
        print("  %4ds-%4ds  %.2f MHz (%+.2f)  max %.1f dB" % (start, end, freq, freq - args.center, peak))
    print("saved", args.png)


if __name__ == "__main__":
    main()
