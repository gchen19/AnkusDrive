"""Render the advanced-toy relationships as figures — the *visual* companion to
``tests/test_toys_advanced.py``.

Each advanced toy asserts an exact closed-form limit (an invariant, a scaling law,
a reciprocity, a conservation identity, or a regression edge). The asserts pin a
single number; this script sweeps the *same* pure-Python analysis functions across
a parameter range and plots the underlying curve, so you can see the law the toy
gates on. No FreeCAD, no external solver — just ``driftpin.analysis`` + matplotlib.

Five figures, one per batch, written to ``examples/results/``:
  * toys_batch1.png — pure-math cores   (tolerance · materials · cfd)
  * toys_batch2.png — scaling laws      (machine_elements · vibration · durability)
  * toys_batch3.png — M6 newcomers      (cht · em)
  * toys_batch4.png — design-for-X      (dfx · cost · slicing · kinematics)
  * toys_batch5.png — round-out         (topology · optics · thermal)

Run:  .venv/bin/python examples/plot_advanced_toys.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")                                   # headless: save PNGs, no display
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import cfd                       # noqa: E402
from driftpin.analysis import cht                       # noqa: E402
from driftpin.analysis import cost                      # noqa: E402
from driftpin.analysis import dfx                       # noqa: E402
from driftpin.analysis import durability as du          # noqa: E402
from driftpin.analysis import em                        # noqa: E402
from driftpin.analysis import kinematics                # noqa: E402
from driftpin.analysis import machine_elements as me    # noqa: E402
from driftpin.analysis import materials as mat          # noqa: E402
from driftpin.analysis import optics as op              # noqa: E402
from driftpin.analysis import slicing as sl             # noqa: E402
from driftpin.analysis import thermal as th             # noqa: E402
from driftpin.analysis import tolerance as tol          # noqa: E402
from driftpin.analysis import topology as topo          # noqa: E402
from driftpin.analysis import vibration as vib          # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
_N_PMMA = 1.49062
_FIT = dict(color="#c0392b", lw=1.4, ls="--", zorder=1)        # closed-form overlay
_PTS = dict(color="#1f4e79", zorder=3)                         # sampled tool output (plot/scatter safe)
_LINE = dict(color="#1f4e79", lw=2.0, zorder=3)


def _law(ax, text):
    """Stamp the pinned identity in a corner box."""
    ax.text(0.04, 0.94, text, transform=ax.transAxes, fontsize=8.5, va="top",
            bbox=dict(boxstyle="round", fc="#fdf6e3", ec="#b58900", alpha=0.9))


# ============================ Batch 1 — pure-math cores =======================

def plot_batch1():
    fig, ax = plt.subplots(2, 2, figsize=(10.0, 7.2))

    # (a) tolerance: worst-case (N·2t) vs RSS (√N·2t) → ratio ≡ √N
    Ns = [1, 2, 3, 4, 6, 9, 12, 16]
    wc, rss = [], []
    for n in Ns:
        r = tol.stackup([{"nominal": 10.0, "tol": 0.1}] * n, "rss")
        wc.append(r["worstcase"]["spread"])
        rss.append(r["rss"]["max_3s"] - r["rss"]["min_3s"])
    a = ax[0, 0]
    a.plot(Ns, wc, "o-", **{**_LINE, "color": "#c0392b"}, label="worst-case  = N·2t")
    a.plot(Ns, rss, "s-", **_LINE, label="RSS (3σ)  = √N·2t")
    a.set_xlabel("number of identical ±t links  N"); a.set_ylabel("stack spread  (mm)")
    a.set_title("tolerance — why you stack statistically", weight="bold", fontsize=10)
    a.legend(fontsize=8.5, loc="upper left")
    _law(a, "worstcase / RSS ≡ √N  (exact)")

    # (b) cfd: laminar Darcy friction factor — f·Re ≡ 64
    Re, ff = [], []
    for v in np.linspace(0.01, 0.22, 16):
        r = cfd.pipe_pressure_drop(diameter_mm=10, length_mm=1000, velocity_m_s=float(v),
                                   fluid="water-20c")
        if r["regime"] == "laminar":
            Re.append(r["reynolds"]); ff.append(r["friction_factor"])
    a = ax[0, 1]
    a.loglog(Re, ff, "o", **_PTS, label="pipe_pressure_drop")
    xx = np.linspace(min(Re), max(Re), 50)
    a.loglog(xx, 64.0 / xx, **_FIT, label="64 / Re")
    a.set_xlabel("Reynolds number  Re"); a.set_ylabel("Darcy friction factor  f")
    a.set_title("cfd — the Poiseuille invariant", weight="bold", fontsize=10)
    a.legend(fontsize=8.5)
    _law(a, "f · Re ≡ 64  (any fluid / D / v)")

    # (c) cfd: creeping-sphere drag — Cd·Re ≡ 24
    Re2, cd = [], []
    for v in np.logspace(-4.0, -2.6, 14):
        r = cfd.stokes_sphere_drag(diameter_mm=2, velocity_m_s=float(v), fluid="glycerin-20c")
        if r["stokes_valid"]:
            Re2.append(r["reynolds"]); cd.append(r["cd"])
    a = ax[1, 0]
    a.loglog(Re2, cd, "o", **_PTS, label="stokes_sphere_drag")
    xx = np.linspace(min(Re2), max(Re2), 50)
    a.loglog(xx, 24.0 / xx, **_FIT, label="24 / Re")
    a.set_xlabel("Reynolds number  Re ≪ 1"); a.set_ylabel("drag coefficient  Cd")
    a.set_title("cfd — Stokes creeping flow", weight="bold", fontsize=10)
    a.legend(fontsize=8.5)
    _law(a, "Cd · Re ≡ 24")

    # (d) materials: Ashby map, specific strength = yield / density iso-lines
    cands = mat.select(rank_by="specific_strength")["candidates"]
    cats = sorted({c["category"] for c in cands})
    cmap = plt.get_cmap("tab10")
    colors = {c: cmap(i % 10) for i, c in enumerate(cats)}
    a = ax[1, 1]
    for c in cands:
        a.scatter(c["density_g_cc"], c["yield_mpa"], color=colors[c["category"]],
                  s=40, edgecolor="k", lw=0.4, zorder=3)
    best = cands[0]
    a.scatter(best["density_g_cc"], best["yield_mpa"], s=180, facecolor="none",
              edgecolor="#c0392b", lw=2.0, zorder=4, label=f"top: {best['name']}")
    dens = np.linspace(0.9, 8.2, 50)
    for k in (50, 100, 200):                                  # specific-strength iso-lines
        a.plot(dens, k * dens, color="#999", lw=0.8, ls=":")
        xl = min(7.9, 1250.0 / k)                             # keep the label inside the y-range
        a.text(xl, k * xl, f"{k}", fontsize=7, color="#777", ha="right", va="bottom", clip_on=True)
    a.set_xlim(0.8, 8.3); a.set_ylim(10, 1300)
    a.set_xlabel("density  (g/cc)"); a.set_ylabel("yield strength  (MPa)")
    a.set_title("materials — Ashby ranking (σy / ρ)", weight="bold", fontsize=10)
    handles = [plt.Line2D([], [], marker="o", ls="", color=colors[c], label=c) for c in cats]
    a.legend(handles=handles + a.get_legend_handles_labels()[0], fontsize=7, loc="lower right")
    _law(a, "score ≡ σy / ρ, ranked best-first")

    fig.suptitle("Advanced toys — Batch 1: pure-math cores  (tolerance · materials · cfd)",
                 weight="bold", fontsize=12.5)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(RESULTS / "toys_batch1.png", dpi=130)
    plt.close(fig)


# ============================ Batch 2 — scaling laws ==========================

def plot_batch2():
    fig, ax = plt.subplots(2, 3, figsize=(13.4, 7.4))

    # (a) bearing ISO 281: L10 = (C/P)^p, p = 3 (ball) / 10/3 (roller)
    cps = np.logspace(0.0, 1.0, 18)
    ball = [me.bearing_life(2500 * float(r), 2500, 1500, kind="ball")["l10_million_rev"] for r in cps]
    roll = [me.bearing_life(2500 * float(r), 2500, 1500, kind="roller")["l10_million_rev"] for r in cps]
    a = ax[0, 0]
    a.loglog(cps, ball, "o-", **_LINE, label="ball  (p = 3)")
    a.loglog(cps, roll, "s-", color="#c0392b", lw=2.0, label="roller  (p = 10/3)")
    a.set_xlabel("load ratio  C / P"); a.set_ylabel("rating life  L10  (10⁶ rev)")
    a.set_title("bearing_life — ISO 281 power law", weight="bold", fontsize=10)
    a.legend(fontsize=8.5); _law(a, "L10 = (C/P)ᵖ : 2×C → 2ᵖ×life")

    # (b) belt Eytelwein: T1/T2 = e^(μθ) — doubling μ squares the ratio
    mus = np.linspace(0.1, 0.6, 24)
    ratio = [me.belt_drive(1000, 100, 200, 300, 1500, friction_coef=float(m))["tension_ratio"] for m in mus]
    a = ax[0, 1]
    a.plot(mus, ratio, **_LINE)
    for m0 in (0.3,):
        r0 = me.belt_drive(1000, 100, 200, 300, 1500, friction_coef=m0)["tension_ratio"]
        r1 = me.belt_drive(1000, 100, 200, 300, 1500, friction_coef=2 * m0)["tension_ratio"]
        a.scatter([m0, 2 * m0], [r0, r1], **{**_PTS, "color": "#c0392b", "s": 45, "zorder": 4})
        a.annotate(f"μ→2μ : {r0:.2f}→{r1:.2f}  (≈ {r0:.2f}²)", (2 * m0, r1),
                   textcoords="offset points", xytext=(-150, -6), fontsize=8, color="#c0392b")
    a.set_xlabel("friction coefficient  μ"); a.set_ylabel("tension ratio  T1 / T2")
    a.set_title("belt_drive — capstan/Eytelwein", weight="bold", fontsize=10)
    _law(a, "T1/T2 = e^(μθ) : μ→2μ ⇒ ratio²")

    # (c) beam f ∝ 1/L²
    Ls = np.linspace(120, 420, 22)
    f1 = [vib.beam_natural_frequencies(float(L), 20, 10, boundary="cantilever", n_modes=1,
                                       youngs_gpa=200, density_kg_m3=7850)["first_mode_hz"] for L in Ls]
    a = ax[0, 2]
    a.loglog(Ls, f1, "o", **_PTS, label="beam_natural_frequencies")
    a.loglog(Ls, f1[0] * (Ls[0] / Ls) ** 2, **_FIT, label="∝ 1/L²")
    a.set_xlabel("beam length  L  (mm)"); a.set_ylabel("first mode  f₁  (Hz)")
    a.set_title("vibration — cantilever frequency", weight="bold", fontsize=10)
    a.legend(fontsize=8.5); _law(a, "f ∝ 1/L² : 2×L → ¼ f")

    # (d) simply-supported f_n ∝ n²
    ss = vib.beam_natural_frequencies(300, 25, 12, boundary="simply_supported", n_modes=5,
                                      youngs_gpa=200, density_kg_m3=7850)
    f = np.array(ss["frequencies_hz"]); n = np.arange(1, 6)
    a = ax[1, 0]
    a.plot(n, f / f[0], "o", **_PTS, ms=8, label="frequencies_hz / f₁")
    a.plot(n, n ** 2, **_FIT, label="n²")
    a.set_xlabel("mode number  n"); a.set_ylabel("f_n / f₁")
    a.set_title("vibration — pinned βL ≡ nπ", weight="bold", fontsize=10)
    a.set_xticks(n); a.legend(fontsize=8.5); _law(a, "f_n ∝ n²  (2,3,4 → 4,9,16)")

    # (e) durability Goodman diagram
    fc = du.fatigue_check(stress_range_mpa=400, mean_stress_mpa=0, material="AL6061-T6")
    se, uts = fc["endurance_mpa"], fc["uts_mpa"]
    sm = np.linspace(0, uts, 50)
    a = ax[1, 1]
    a.plot(sm, se * (1 - sm / uts), **_LINE, label="Goodman: σa/σe+σm/σuts=1")
    a.axhline(se, color="#c0392b", ls="--", lw=1.2, label=f"σe = {se:.0f} MPa  (σm=0 → σa=σe)")
    a.scatter([0], [se], **{**_PTS, "color": "#c0392b", "s": 50, "zorder": 4})
    a.set_xlabel("mean stress  σm  (MPa)"); a.set_ylabel("alternating  σa  (MPa)")
    a.set_title("durability — Goodman line (AL6061-T6)", weight="bold", fontsize=10)
    a.legend(fontsize=8); _law(a, "σm=0 ⇒ equiv ≡ σa")

    # (f) S-N Basquin line: 2× amplitude → fixed life ratio
    life, amp = [], []
    for sr in np.logspace(np.log10(196), np.log10(556), 24):
        r = du.fatigue_check(stress_range_mpa=float(sr), mean_stress_mpa=0,
                             material="AL6061-T6", cycles=1)
        if r["life_cycles"] and r["governing_mode"] == "fully_reversed_fatigue":
            life.append(r["life_cycles"]); amp.append(r["stress_amplitude_mpa"])
    a = ax[1, 2]
    a.loglog(life, amp, "o-", **_LINE)
    a.set_xlabel("life  N  (cycles)"); a.set_ylabel("stress amplitude  σa  (MPa)")
    a.set_title("durability — S-N (Basquin) slope", weight="bold", fontsize=10)
    _law(a, "log-log line: 2×σa → 2^(1/b)× life")

    fig.suptitle("Advanced toys — Batch 2: scaling laws  (machine_elements · vibration · durability)",
                 weight="bold", fontsize=12.5)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(RESULTS / "toys_batch2.png", dpi=130)
    plt.close(fig)


# ============================ Batch 3 — M6 newcomers ==========================

def plot_batch3():
    fig, ax = plt.subplots(2, 2, figsize=(10.0, 7.2))

    # (a) cht composite-wall temperature profile (interface temps walk the drops)
    layers = [{"thickness_mm": 8, "k": 30.0}, {"thickness_mm": 4, "k": 0.5}]
    h_in, h_out, t_in, t_out = 500.0, 20.0, 180.0, 20.0
    w = cht.composite_wall(layers, t_in, t_out, h_in=h_in, h_out=h_out)
    xs = np.cumsum([0] + [ly["thickness_mm"] for ly in layers])    # 0, 8, 12
    a = ax[0, 0]
    a.plot(xs, w["interface_temps_c"], "o-", **_LINE, label="solid wall  T(x)")
    a.plot([-3, 0], [t_in, w["interface_temps_c"][0]], ":", color="#c0392b", lw=1.6)
    a.plot([xs[-1], xs[-1] + 3], [w["interface_temps_c"][-1], t_out], ":", color="#c0392b",
           lw=1.6, label="convective film drop")
    a.axhline(t_in, color="#999", ls="--", lw=0.8); a.axhline(t_out, color="#999", ls="--", lw=0.8)
    for x, t in zip(xs, w["interface_temps_c"]):
        a.annotate(f"{t:.0f}°", (x, t), textcoords="offset points", xytext=(2, 6), fontsize=8)
    a.set_xlabel("position through wall  (mm)"); a.set_ylabel("temperature  (°C)")
    a.set_title("cht — series resistance T-profile", weight="bold", fontsize=10)
    a.legend(fontsize=8); _law(a, f"q ≡ U·ΔT = {w['q_w_m2']:.0f} W/m²,  R = Σrᵢ")

    # (b) em skin depth ∝ 1/√f  (and surface resistance ∝ √f)
    fr = np.logspace(1, 5, 40)
    delta = [em.skin_depth(float(x), conductivity_s_m=5.8e7)["skin_depth_m"] * 1e3 for x in fr]
    rs = [em.skin_depth(float(x), conductivity_s_m=5.8e7)["surface_resistance_ohm"] * 1e3 for x in fr]
    a = ax[0, 1]
    a.loglog(fr, delta, "o", **_PTS, label="skin_depth  (mm)")
    a.loglog(fr, delta[0] * (fr[0] / fr) ** 0.5, **_FIT, label="∝ f^(−1/2)")
    a.set_xlabel("frequency  (Hz)"); a.set_ylabel("skin depth δ  (mm)", color="#1f4e79")
    a2 = a.twinx()
    a2.loglog(fr, rs, "s", color="#2e8b57", ms=4, label="Rs (mΩ/□)")
    a2.set_ylabel("surface resistance  Rs  (mΩ/□)", color="#2e8b57")
    a.set_title("em — copper skin effect", weight="bold", fontsize=10)
    a.legend(fontsize=8, loc="lower left"); _law(a, "δ ∝ 1/√f ,  Rs ∝ √f")

    # (c) em straight-wire field B ∝ 1/r
    rr = np.logspace(0, 2, 28)                                     # 1..100 mm
    b = [em.wire_field(100.0, float(x))["b_mt"] for x in rr]
    a = ax[1, 0]
    a.loglog(rr, b, "o", **_PTS, label="wire_field (I=100 A)")
    a.loglog(rr, b[0] * (rr[0] / rr), **_FIT, label="∝ 1/r")
    a.set_xlabel("distance from wire  r  (mm)"); a.set_ylabel("flux density  B  (mT)")
    a.set_title("em — Ampère straight wire", weight="bold", fontsize=10)
    a.legend(fontsize=8.5); _law(a, "B·r invariant : 2×r → ½ B")

    # (d) em solenoid interior field B = μ₀ n I  (linear, radius-independent)
    ns = np.linspace(100, 3000, 30)
    bs = [em.solenoid_field(float(x), 2.0)["b_t"] * 1e3 for x in ns]
    a = ax[1, 1]
    a.plot(ns, bs, "o", **_PTS, label="solenoid_field (I=2 A)")
    a.plot(ns, np.array(bs)[0] / ns[0] * ns, **_FIT, label="∝ n  (μ₀ n I)")
    a.set_xlabel("turns per metre  n"); a.set_ylabel("interior B  (mT)")
    a.set_title("em — long solenoid (uniform)", weight="bold", fontsize=10)
    a.legend(fontsize=8.5); _law(a, "B = μ₀·n·I  (linear, radius-free)")

    fig.suptitle("Advanced toys — Batch 3: M6 newcomers  (cht · em)", weight="bold", fontsize=12.5)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(RESULTS / "toys_batch3.png", dpi=130)
    plt.close(fig)


# ============================ Batch 4 — design-for-X ==========================

def plot_batch4():
    fig, ax = plt.subplots(2, 3, figsize=(13.4, 7.4))

    # (a) cost: unit cost = floor + fixed/qty → 1/qty asymptote
    qs = np.unique(np.logspace(0, 5, 30).astype(int))
    uc = [cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6", tooling_usd=5000,
                             quantity=int(q), setup_min=10)["unit_cost"] for q in qs]
    r0 = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6", tooling_usd=5000,
                            quantity=1, setup_min=10)
    floor = r0["material_cost"] + r0["breakdown"]["machining_cost"]
    a = ax[0, 0]
    a.semilogx(qs, uc, "o-", **_LINE, label="unit_cost")
    a.axhline(floor, **_FIT, label=f"floor = mat+proc = ${floor:.0f}")
    a.set_xlabel("quantity"); a.set_ylabel("unit cost  ($)")
    a.set_title("cost — tooling amortization", weight="bold", fontsize=10)
    a.legend(fontsize=8.5); _law(a, "unit = floor + fixed/qty → floor")

    # (b) slicing: filament linear in infill
    inf = np.linspace(0, 1, 21)
    fil = [sl.slice_estimate(1e5, [50, 40, 30], infill_fraction=float(f), wall_fraction=0.35,
                             density_g_cc=1.25)["filament_g"] for f in inf]
    a = ax[0, 1]
    a.plot(inf, fil, "o", **_PTS, label="filament_g")
    a.plot(inf, np.polyval(np.polyfit(inf, fil, 1), inf), **_FIT, label="linear fit")
    a.set_xlabel("infill fraction"); a.set_ylabel("filament mass  (g)")
    a.set_title("slicing — infill is linear", weight="bold", fontsize=10)
    a.legend(fontsize=8.5); _law(a, "dep = V·(wall+infill·(1−wall))")

    # (c) slicing: layer_count = ceil(bbox_z / layer_height)
    z = np.linspace(8.0, 13.0, 260)
    lc = [sl.slice_estimate(1000, [10, 10, float(zz)], layer_height_mm=1.0)["layer_count"] for zz in z]
    a = ax[0, 2]
    a.step(z, lc, where="post", **_LINE)
    a.scatter([9.99, 10.01], [10, 11], **{**_PTS, "color": "#c0392b", "s": 45, "zorder": 4})
    a.set_xlabel("part height  bbox_z  (mm)"); a.set_ylabel("layer_count")
    a.set_title("slicing — the ceil() boundary", weight="bold", fontsize=10)
    _law(a, "≡ ⌈z / layer⌉  (9.99→10, 10.01→11)")

    # (d) kinematics: slider stroke independent of conrod L (inline) vs offset decay
    Ls = np.linspace(22, 200, 40)
    inline = [kinematics.slider_crank(crank_mm=15, conrod_mm=float(L))["stroke_mm"] for L in Ls]
    offset = [kinematics.slider_crank(crank_mm=15, conrod_mm=float(L), wrist_offset_mm=5)["stroke_mm"]
              for L in Ls]
    a = ax[1, 0]
    a.plot(Ls, inline, **_LINE, label="inline (e=0)")
    a.plot(Ls, offset, color="#c0392b", lw=2.0, label="offset (e=5)")
    a.axhline(30.0, color="#999", ls=":", lw=1.0, label="2R = 30")
    a.set_xlabel("conrod length  L  (mm)"); a.set_ylabel("stroke  (mm)")
    a.set_title("kinematics — slider-crank stroke", weight="bold", fontsize=10)
    a.legend(fontsize=8.5); _law(a, "inline: stroke ≡ 2R  ∀ L")

    # (e) kinematics: Grübler single-loop DOF = n − 3
    ns = list(range(3, 9))
    dof = [kinematics.gruebler_dof(n, ["revolute"] * n) for n in ns]
    a = ax[1, 1]
    a.bar(ns, dof, color="#1f4e79", width=0.6, zorder=3)
    a.plot(ns, [n - 3 for n in ns], **_FIT, label="n − 3")
    for n, d in zip(ns, dof):
        a.annotate(str(d), (n, d), textcoords="offset points", xytext=(0, 3 if d >= 0 else -12),
                   ha="center", fontsize=8)
    a.axhline(0, color="k", lw=0.6)
    a.set_xlabel("links  n  (single loop, revolute)"); a.set_ylabel("mobility  DOF")
    a.set_title("kinematics — Grübler sequence", weight="bold", fontsize=10)
    a.legend(fontsize=8.5); _law(a, "4-bar→1, 5-bar→2, triangle→0")

    # (f) dfx: DfA assembly score monotone non-increasing in part count
    parts = list(range(2, 13))
    score = [dfx.dfa_check(p, 0)["assembly_score"] for p in parts]
    a = ax[1, 2]
    a.plot(parts, score, "o-", **_LINE)
    a.set_xlabel("part count"); a.set_ylabel("assembly_score")
    a.set_title("dfx — DfA penalizes part count", weight="bold", fontsize=10)
    _law(a, "score monotone ↓ as parts rise")

    fig.suptitle("Advanced toys — Batch 4: design-for-X & throughput  (dfx · cost · slicing · kinematics)",
                 weight="bold", fontsize=12.5)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(RESULTS / "toys_batch4.png", dpi=130)
    plt.close(fig)


# ============================ Batch 5 — round-out =============================

def plot_batch5():
    fig, ax = plt.subplots(2, 2, figsize=(10.4, 7.4))

    # (a) topology: SIMP penalty E(x) = Emin + x^p (E0−Emin), sublinear for p>1
    x = np.linspace(0, 1, 120)
    E0, Emin = 1.0, 1e-9
    a = ax[0, 0]
    for p, col in zip((1, 2, 3), ("#999", "#2e8b57", "#1f4e79")):
        a.plot(x, Emin + x ** p * (E0 - Emin), lw=2.0, color=col, label=f"p = {p}")
        a.scatter([0.5], [Emin + 0.5 ** p * (E0 - Emin)], color=col, s=30, zorder=4)
    a.plot([0, 1], [Emin, E0], ls="--", color="#c0392b", lw=1.2, label="chord (p=1)")
    a.set_xlabel("element density  x"); a.set_ylabel("normalized stiffness  E(x)")
    a.set_title("topology — SIMP penalty", weight="bold", fontsize=10)
    a.legend(fontsize=8.5); _law(a, "p>1 ⇒ E(0.5) < midpoint (gray costly)")

    # (b) topology: initial compliance ∝ 1/keep_fraction (penal=1, real solve)
    kfs = np.linspace(0.2, 0.8, 13)
    c0 = [topo.simp_topology_2d(nelx=8, nely=4, keep_fraction=float(k), penal=1.0,
                                max_iter=1)["compliance_initial"] for k in kfs]
    a = ax[0, 1]
    a.plot(kfs, c0, "o", **_PTS, label="compliance_initial")
    a.plot(kfs, c0[0] * kfs[0] / kfs, **_FIT, label="∝ 1/keep_fraction")
    a.set_xlabel("keep_fraction"); a.set_ylabel("initial compliance  (stiffer ↓)")
    a.set_title("topology — more material, stiffer", weight="bold", fontsize=10)
    a.legend(fontsize=8.5); _law(a, "penal=1: c₀·kf invariant")

    # (c) optics: TIR — transmittance drops to 0 exactly at the critical angle
    ang = np.linspace(0, 89, 360)
    T = [op.fresnel_reflectance(float(t), _N_PMMA, 1.0)["transmittance"] for t in ang]
    R = [op.fresnel_reflectance(float(t), _N_PMMA, 1.0)["reflectance"] for t in ang]
    theta_c = op.critical_angle(_N_PMMA, 1.0)
    a = ax[1, 0]
    a.plot(ang, T, **_LINE, label="transmittance")
    a.plot(ang, R, color="#c0392b", lw=2.0, label="reflectance")
    a.axvline(theta_c, color="#b58900", ls="--", lw=1.4)
    a.annotate(f"θc = {theta_c:.2f}°", (theta_c, 0.5), textcoords="offset points",
               xytext=(6, 0), fontsize=8.5, color="#b58900")
    a.set_xlabel("incidence angle  (°)  PMMA → air"); a.set_ylabel("power fraction")
    a.set_title("optics — total internal reflection", weight="bold", fontsize=10)
    a.legend(fontsize=8.5, loc="center left"); _law(a, "T > 0 below θc,  ≡ 0 above")

    # (d) thermal: lumped capacitance reaches 1−1/e at t = τ
    base = th.thermal_lumped(mass_g=120, power_w=15, h_conv=12, area_mm2=20000, c_p=900,
                             t_ambient_c=25)
    tau = base["time_constant_s"]
    ts = np.linspace(0, 4 * tau, 60)
    frac = []
    for t in ts:
        r = th.thermal_lumped(mass_g=120, power_w=15, h_conv=12, area_mm2=20000, c_p=900,
                              t_ambient_c=25, duration_s=float(t))
        frac.append((r["t_final_c"] - r["t_ambient_c"]) / r["delta_t_steady_k"])
    a = ax[1, 1]
    a.plot(ts / tau, frac, **_LINE, label="lumped rise")
    a.plot(ts / tau, 1 - np.exp(-ts / tau), **_FIT, label="1 − e^(−t/τ)")
    a.axhline(1 - 1 / np.e, color="#999", ls=":", lw=1.0)
    a.axvline(1.0, color="#999", ls=":", lw=1.0)
    a.scatter([1.0], [1 - 1 / np.e], **{**_PTS, "color": "#c0392b", "s": 55, "zorder": 4})
    a.annotate("63.2 % at t = τ", (1.0, 1 - 1 / np.e), textcoords="offset points",
               xytext=(10, -14), fontsize=8.5, color="#c0392b")
    a.set_xlabel("elapsed time  t / τ"); a.set_ylabel("fraction of steady rise")
    a.set_title("thermal — RC time constant", weight="bold", fontsize=10)
    a.legend(fontsize=8.5, loc="lower right"); _law(a, "t = τ ⇒ exactly 1 − 1/e")

    fig.suptitle("Advanced toys — Batch 5: round-out  (topology · optics · thermal)",
                 weight="bold", fontsize=12.5)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(RESULTS / "toys_batch5.png", dpi=130)
    plt.close(fig)


def main():
    RESULTS.mkdir(exist_ok=True)
    for fn in (plot_batch1, plot_batch2, plot_batch3, plot_batch4, plot_batch5):
        fn()
        print(f"  wrote  {RESULTS.name}/{fn.__name__.replace('plot_', 'toys_')}.png")
    print(f"\n5 figures → {RESULTS}")


if __name__ == "__main__":
    main()
