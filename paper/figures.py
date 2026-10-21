"""Build the paper's figures from results/.

    uv run --with matplotlib python figures.py

Reads the newest results/memory/*.json. Every series has its own marker, so the figure reads in grayscale.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
OUT = HERE / "fig"
BLUE, ORANGE, AQUA, VIOLET, RED = "#2a78d6", "#eb6834", "#1baf7a", "#7d5bd6", "#c8423b"
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"

plt.rcParams.update(
    {
        "font.family": "serif",
        "font.size": 7.5,
        "axes.edgecolor": MUTED,
        "axes.labelcolor": INK,
        "axes.linewidth": 0.6,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.5,
        "axes.axisbelow": True,
        "legend.frameon": False,
    }
)

SERIES = [  # (configuration, label, color, marker)
    ("full context", "full history", MUTED, "s"),
    ("episodic", "episodic search", BLUE, "o"),
    ("derived, dreaming", "notes (rule extractor)", ORANGE, "^"),
    ("episodic + derived", "episodic + notes", AQUA, "D"),
    ("episodic + derived, oracle", "episodic + notes (oracle)", VIOLET, "v"),
]


def memory() -> None:
    path = sorted((HERE.parent / "results" / "memory").glob("*.json"))[-1]
    data = json.loads(path.read_text(encoding="utf-8"))["sessions"]
    sessions = sorted(data, key=int)
    fig, (left, right) = plt.subplots(1, 2, figsize=(7.0, 2.1))
    for name, label, color, marker in SERIES:
        acc = [next(r for r in data[s] if r["name"] == name)["accuracy"] for s in sessions]
        words = [next(r for r in data[s] if r["name"] == name)["context_words"] for s in sessions]
        left.plot([int(s) for s in sessions], acc, color=color, marker=marker, ms=4, lw=1.1, label=label)
        right.plot([int(s) for s in sessions], words, color=color, marker=marker, ms=4, lw=1.1)
    left.set_xlabel("sessions per user")
    left.set_ylabel("accuracy")
    left.set_ylim(0.4, 1.03)
    left.legend(loc="lower right", fontsize=6.5)
    right.set_xlabel("sessions per user")
    right.set_ylabel("words of context per question")
    right.set_yscale("log")
    for ax in (left, right):
        ax.set_xticks([8, 32, 64])
    fig.tight_layout(pad=0.4)
    fig.savefig(OUT / "memory.pdf")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    memory()
