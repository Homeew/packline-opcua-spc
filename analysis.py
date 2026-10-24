"""SPC, OEE and data-validation analysis on the collected shift.

Reads PostgreSQL with the queries in sql/analysis_queries.sql, then:
  1. checks data completeness (database rows vs. the checkweigher's own counter)
  2. builds X-bar / R control charts from a stable baseline and flags rule violations
  3. compares continuous SPC with today's 30-minute manual sampling
  4. computes OEE (availability x performance x quality) and a downtime Pareto
  5. validates the checkweigher against QA's manual reference-scale readings
  6. computes giveaway (product shipped above the 500 g label)

Writes charts to output/*.png and every number to output/summary.json.

Run:  python analysis.py
"""
import json
import re
from datetime import timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg
from scipy import stats

import config as C

ROOT = Path(__file__).parent
OUT = ROOT / "output"

# X-bar/R chart constants for subgroup size n
SPC_CONSTANTS = {5: dict(A2=0.577, D3=0.0, D4=2.114, d2=2.326)}

# chart colours (light theme)
INK, INK2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, AQUA, CRITICAL, SURFACE = "#2a78d6", "#eb6834", "#1baf7a", "#d03b3b", "#fcfcfb"


# --------------------------------------------------------------------------- data access
def load_queries():
    text = (ROOT / "sql" / "analysis_queries.sql").read_text()
    blocks = re.split(r"^-- name: (\w+)\s*$", text, flags=re.M)
    return {blocks[i]: blocks[i + 1].strip().rstrip(";") for i in range(1, len(blocks), 2)}


def query(conn, sql, **params):
    with conn.cursor() as cur:
        cur.execute(sql, params or None)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


# --------------------------------------------------------------------------- SPC
def control_limits(sub, n):
    k = SPC_CONSTANTS[n]
    base = sub[sub.minute < C.SHIFT_START + timedelta(minutes=C.BASELINE_END_MINUTE)]
    xbb, rbar = base.xbar.mean(), base.r.mean()
    sigma = rbar / k["d2"]                     # pack-to-pack standard deviation
    s_xbar = sigma / np.sqrt(n)                # standard deviation of a subgroup mean
    return dict(center=xbb, ucl=xbb + k["A2"] * rbar, lcl=xbb - k["A2"] * rbar,
                rbar=rbar, r_ucl=k["D4"] * rbar, r_lcl=k["D3"] * rbar,
                sigma=sigma, sigma_xbar=s_xbar, baseline_subgroups=len(base))


def rule_violations(x, cl, s):
    """Western Electric rules on a series of subgroup means. Returns {index: rule}."""
    z = (np.asarray(x) - cl) / s
    hits = {}
    for i in range(len(z)):
        if abs(z[i]) > 3:
            hits[i] = "1: beyond 3 sigma"
            continue
        if i >= 2:
            w = z[i - 2:i + 1]
            for sign in (1, -1):
                if (sign * w > 2).sum() >= 2 and sign * z[i] > 2:
                    hits[i] = "2: 2 of 3 beyond 2 sigma"
        if i >= 7 and i not in hits:
            w = z[i - 7:i + 1]
            if (w > 0).all() or (w < 0).all():
                hits[i] = "3: 8 in a row on one side"
    return hits


# --------------------------------------------------------------------------- main
def main():
    OUT.mkdir(exist_ok=True)
    q = load_queries()
    conn = psycopg.connect(C.PG_DSN)
    n = C.SUBGROUP_SIZE

    comp = query(conn, q["completeness"]).iloc[0]
    sub = query(conn, q["subgroups"], n=n)
    pm = query(conn, q["per_minute"])
    stops = query(conn, q["stops"])
    win = query(conn, q["shift_window"]).iloc[0]
    tot = query(conn, q["totals"], nominal=C.NOMINAL_G).iloc[0]
    val = query(conn, q["manual_vs_checkweigher"])
    unmatched = int(query(conn, q["manual_unmatched"]).iloc[0, 0])
    conn.close()

    # ---- 1. SPC --------------------------------------------------------------------
    lim = control_limits(sub, n)
    hits = rule_violations(sub.xbar.values, lim["center"], lim["sigma_xbar"])
    sub["signal"] = [hits.get(i, "") for i in range(len(sub))]
    sub["r_signal"] = sub.r > lim["r_ucl"]
    after_base = sub.minute >= C.SHIFT_START + timedelta(minutes=C.BASELINE_END_MINUTE)

    # reject-rate spike: first minute where the rolling 5-minute reject rate reaches 10 %
    pm = pm.set_index("minute")
    roll = pm[["packs", "rejects"]].rolling(5, min_periods=3).sum()
    pm["reject_pct_5min"] = 100 * roll.rejects / roll.packs
    pm["reject_pct"] = 100 * pm.rejects / pm.packs
    spike = pm.index[pm.reject_pct_5min >= 10]
    spike_time = spike[0] if len(spike) else None

    episode = None
    if spike_time is not None:
        lookback = sub[(sub.signal != "") & after_base &
                       (sub.minute >= spike_time - timedelta(minutes=60)) & (sub.minute <= spike_time)]
        if len(lookback):
            first = lookback.iloc[0]
            episode = dict(first_alarm=first.minute, rule=first.signal, spike=spike_time,
                           lead_min=(spike_time - first.minute).total_seconds() / 60)

    # what 30-minute manual sampling would have seen (same packs, checkweigher readings)
    val["check"] = (val.ts.diff() > pd.Timedelta(minutes=5)).cumsum()
    periodic = val.groupby("check").agg(ts=("ts", "first"), xbar=("checkweigher_g", "mean"))
    periodic["out"] = (periodic.xbar < lim["lcl"]) | (periodic.xbar > lim["ucl"])
    periodic_first = None
    if episode:
        cands = periodic[periodic.out & (periodic.ts >= episode["first_alarm"] - timedelta(minutes=30))]
        periodic_first = cands.ts.iloc[0] if len(cands) else None

    cpl = (lim["center"] - C.LOWER_REJECT_G) / (3 * lim["sigma"])

    # ---- 2. OEE --------------------------------------------------------------------
    shift_min = (win.shift_end - win.shift_start).total_seconds() / 60
    planned_min = stops.loc[stops.planned, "minutes"].sum()
    unplanned = stops[~stops.planned].copy()
    unplanned["group"] = np.where(unplanned.reason.str.startswith("Minor stop"), "Minor stops (<5 min)",
                                  unplanned.reason)
    downtime_min = unplanned.minutes.sum()
    planned_time = shift_min - planned_min
    run_time = planned_time - downtime_min
    total, rejects = int(tot.total_packs), int(tot.rejects)
    availability = run_time / planned_time
    performance = (total / C.IDEAL_RATE_PPM) / run_time
    quality = (total - rejects) / total
    oee = availability * performance * quality
    pareto = (unplanned.groupby("group").agg(minutes=("minutes", "sum"), stops=("minutes", "size"))
              .sort_values("minutes", ascending=False))
    # packs lost to each OEE loss, at the ideal rate
    lost = dict(downtime=downtime_min * C.IDEAL_RATE_PPM,
                speed=run_time * C.IDEAL_RATE_PPM - total,
                rejects=rejects)

    # ---- 3. checkweigher vs manual reference scale ---------------------------------
    d = val.diff_g.values
    bias, sd_d, n_val = d.mean(), d.std(ddof=1), len(d)
    half = stats.t.ppf(0.975, n_val - 1) * sd_d / np.sqrt(n_val)
    t_stat = bias / (sd_d / np.sqrt(n_val))
    p_val = 2 * stats.t.sf(abs(t_stat), n_val - 1)

    # ---- 4. giveaway ---------------------------------------------------------------
    accepted = total - rejects
    giveaway_kg = float(tot.giveaway_kg)
    giveaway_true_kg = giveaway_kg - bias * accepted / 1000     # checkweigher reads high

    summary = dict(
        shift=dict(start=str(win.shift_start), end=str(win.shift_end), minutes=shift_min),
        completeness=dict(equipment_packs=int(comp.equipment_pack_count), collected=int(comp.rows_collected),
                          equipment_rejects=int(comp.equipment_reject_count),
                          collected_rejects=int(comp.rejects_collected), missing=int(comp.missing_packs),
                          manual_checks=n_val, manual_unmatched=unmatched),
        spc=dict(subgroup_size=n, subgroups=len(sub), baseline_subgroups=lim["baseline_subgroups"],
                 center=lim["center"], ucl=lim["ucl"], lcl=lim["lcl"], rbar=lim["rbar"], r_ucl=lim["r_ucl"],
                 sigma=lim["sigma"], cpl_baseline=cpl,
                 signals=int((sub.signal != "").sum()), r_signals=int(sub.r_signal.sum()),
                 episode=None if not episode else dict(
                     first_alarm=episode["first_alarm"].strftime("%H:%M"), rule=episode["rule"],
                     reject_spike=episode["spike"].strftime("%H:%M"), lead_min=episode["lead_min"],
                     periodic_detect=None if periodic_first is None else periodic_first.strftime("%H:%M"))),
        oee=dict(shift_min=shift_min, planned_stop_min=planned_min, planned_time_min=planned_time,
                 downtime_min=downtime_min, run_time_min=run_time, ideal_ppm=C.IDEAL_RATE_PPM,
                 total_packs=total, rejects=rejects, availability=availability, performance=performance,
                 quality=quality, oee=oee, lost_packs=lost,
                 pareto=[dict(reason=k, minutes=float(v.minutes), stops=int(v.stops)) for k, v in pareto.iterrows()]),
        validation=dict(n=n_val, bias_g=bias, ci95=[bias - half, bias + half], sd_g=sd_d, t=t_stat, p=p_val),
        weight=dict(nominal=C.NOMINAL_G, setpoint=C.SETPOINT_G, reject_limit=C.LOWER_REJECT_G,
                    mean=float(tot.mean_weight), sd=float(tot.sd_weight),
                    giveaway_kg=giveaway_kg, giveaway_true_kg=giveaway_true_kg,
                    giveaway_g_per_pack=giveaway_kg * 1000 / accepted),
        series=dict(
            subgroups=[dict(t=m.strftime("%H:%M"), x=round(x, 2), r=round(r, 2), s=s)
                       for m, x, r, s in zip(sub.minute, sub.xbar, sub.r, sub.signal)],
            reject_pct=[dict(t=m.strftime("%H:%M"), p=round(p, 2)) for m, p in pm.reject_pct_5min.dropna().items()],
            stops=[dict(start=s.strftime("%H:%M"), end=e.strftime("%H:%M"), reason=r, planned=bool(p))
                   for s, e, r, p in zip(stops.start_ts, stops.end_ts, stops.reason, stops.planned)],
            periodic=[dict(t=t.strftime("%H:%M"), x=round(x, 2), out=bool(o))
                      for t, x, o in zip(periodic.ts, periodic.xbar, periodic.out)],
            validation=[round(float(v), 1) for v in d],
        ),
    )
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=float))

    make_charts(sub, pm, stops, lim, episode, periodic, pareto, d, bias, half)
    export_tables(summary, sub, pm, stops, periodic, pareto, val)
    print_report(summary)


# --------------------------------------------------------------------------- charts
def export_tables(s, sub, pm, stops, periodic, pareto, val):
    """Flat CSV/JSON files for the dashboard (output/dashboard_data/)."""
    d = OUT / "dashboard_data"
    d.mkdir(exist_ok=True)
    o, sp, v, c, w = s["oee"], s["spc"], s["validation"], s["completeness"], s["weight"]
    ep = sp["episode"] or {}
    kpis = dict(
        oee=o["oee"], availability=o["availability"], performance=o["performance"], quality=o["quality"],
        total_packs=o["total_packs"], rejects=o["rejects"], downtime_min=o["downtime_min"],
        planned_time_min=o["planned_time_min"], run_time_min=o["run_time_min"],
        equipment_packs=c["equipment_packs"], collected_packs=c["collected"], missing_packs=c["missing"],
        capture_rate=c["collected"] / c["equipment_packs"],
        ucl=sp["ucl"], center=sp["center"], lcl=sp["lcl"], rbar=sp["rbar"], r_ucl=sp["r_ucl"],
        sigma=sp["sigma"], cpl=sp["cpl_baseline"],
        first_alarm=ep.get("first_alarm"), reject_spike=ep.get("reject_spike"),
        lead_min=ep.get("lead_min"), periodic_detect=ep.get("periodic_detect"),
        periodic_lag_min=None,
        bias_g=v["bias_g"], bias_ci_lo=v["ci95"][0], bias_ci_hi=v["ci95"][1], n_checks=v["n"],
        giveaway_kg=w["giveaway_kg"], giveaway_g_per_pack=w["giveaway_g_per_pack"],
    )
    if ep.get("periodic_detect"):
        h1, m1 = map(int, ep["first_alarm"].split(":"))
        h2, m2 = map(int, ep["periodic_detect"].split(":"))
        kpis["periodic_lag_min"] = (h2 * 60 + m2) - (h1 * 60 + m1)
    (d / "kpis.json").write_text(json.dumps([kpis], indent=2, default=float))

    sub.assign(minute=sub.minute.dt.strftime("%Y-%m-%d %H:%M"), xbar=sub.xbar.round(2), r=sub.r.round(2))[
        ["minute", "xbar", "r", "signal"]].to_csv(d / "subgroups.csv", index=False)
    pm.reject_pct_5min.dropna().round(2).rename_axis("minute").reset_index().assign(
        minute=lambda x: x.minute.dt.strftime("%Y-%m-%d %H:%M")).to_csv(d / "reject_rate.csv", index=False)
    stops.assign(start_ts=stops.start_ts.dt.strftime("%Y-%m-%d %H:%M:%S"),
                 end_ts=stops.end_ts.dt.strftime("%Y-%m-%d %H:%M:%S"), minutes=stops.minutes.round(1))[
        ["start_ts", "end_ts", "reason", "planned", "minutes"]].to_csv(d / "stops.csv", index=False)
    pareto.reset_index().rename(columns={"group": "reason"}).assign(minutes=lambda x: x.minutes.round(1)).to_csv(
        d / "downtime.csv", index=False)
    periodic.assign(ts=periodic.ts.dt.strftime("%Y-%m-%d %H:%M"), xbar=periodic.xbar.round(2))[
        ["ts", "xbar", "out"]].rename(columns={"ts": "check_time"}).to_csv(d / "periodic_checks.csv", index=False)
    val.assign(ts=val.ts.dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3])[
        ["ts", "manual_g", "checkweigher_g", "diff_g"]].round(2).rename(columns={"ts": "pack_time"}).to_csv(
        d / "manual_vs_checkweigher.csv", index=False)


def style(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def with_gaps(times, values, max_gap_min=1.5):
    """Insert NaN where the line was stopped so plots don't draw across the gap."""
    t = pd.Series(pd.to_datetime(list(times)))
    v = pd.Series(np.asarray(values, dtype=float))
    gap = t.diff() > pd.Timedelta(minutes=max_gap_min)
    out_t, out_v = [], []
    for ti, vi, g in zip(t, v, gap):
        if g:
            out_t.append(ti - pd.Timedelta(seconds=1)); out_v.append(np.nan)
        out_t.append(ti); out_v.append(vi)
    return out_t, out_v


def shade_stops(ax, stops):
    for s, e, p in zip(stops.start_ts, stops.end_ts, stops.planned):
        ax.axvspan(s, e, color="#f0efec" if p else "#e8e6df", lw=0, zorder=0)


def make_charts(sub, pm, stops, lim, episode, periodic, pareto, d, bias, half):
    plt.rcParams.update({"font.family": "Inter", "text.color": INK, "axes.labelcolor": INK2})
    hhmm = mdates.DateFormatter("%H:%M")

    # Control chart story: X-bar, R and reject rate share one time axis
    fig, axes = plt.subplots(3, 1, figsize=(11, 8.5), sharex=True,
                             gridspec_kw=dict(height_ratios=[3, 1.4, 1.6], hspace=0.25), facecolor=SURFACE)
    ax, axr, axp = axes
    for a in axes:
        style(a)
        shade_stops(a, stops)
    ax.plot(*with_gaps(sub.minute, sub.xbar), color=BLUE, lw=1.2, label="Subgroup mean (5 packs/min)")
    for y, lab in ((lim["ucl"], "UCL"), (lim["center"], "CL"), (lim["lcl"], "LCL")):
        ax.axhline(y, color=INK2 if lab == "CL" else MUTED, lw=1, ls="-" if lab == "CL" else "--")
        ax.text(1.01, y, f"{lab} {y:.1f}", va="center", fontsize=8.5, color=INK2,
                transform=ax.get_yaxis_transform())
    sig = sub[sub.signal != ""]
    ax.scatter(sig.minute, sig.xbar, s=26, color=CRITICAL, zorder=3, edgecolor=SURFACE, lw=1.2,
               label="SPC rule violation")
    ax.scatter(periodic.ts, periodic.xbar, s=46, marker="D", facecolor="none", edgecolor=ORANGE, lw=1.6,
               zorder=4, label="What a 30-min manual check sees")
    if episode:
        ax.annotate(f"First SPC alarm {episode['first_alarm']:%H:%M}\n"
                    f"{episode['lead_min']:.0f} min before rejects spike",
                    xy=(episode["first_alarm"], sub.set_index("minute").xbar[episode["first_alarm"]]),
                    xytext=(-170, -55), textcoords="offset points", fontsize=9, color=INK,
                    arrowprops=dict(arrowstyle="-", color=INK2, lw=0.8))
    ax.set_ylabel("Pack weight (g)")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=8.5, frameon=False, ncol=3)
    ax.set_title("Checkweigher X-bar / R control charts, one simulated shift", loc="left",
                 fontsize=13, fontweight="semibold", color=INK, pad=30)

    axr.plot(*with_gaps(sub.minute, sub.r), color=BLUE, lw=1)
    axr.axhline(lim["r_ucl"], color=MUTED, lw=1, ls="--")
    axr.axhline(lim["rbar"], color=INK2, lw=1)
    axr.set_ylabel("Range (g)")

    rt, rv = with_gaps(pm.index, pm.reject_pct_5min)
    axp.fill_between(rt, rv, color=CRITICAL, alpha=0.25, lw=0)
    axp.plot(rt, rv, color=CRITICAL, lw=1.2)
    axp.axhline(10, color=MUTED, lw=1, ls="--")
    axp.set_ylabel("Rejects (%, 5-min)")
    axp.xaxis.set_major_formatter(hhmm)
    fig.text(0.125, 0.04, "Grey bands: line stopped (lighter = planned break or lunch)", fontsize=8.5, color=MUTED)
    fig.savefig(OUT / "control_charts.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    # Downtime Pareto
    fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=SURFACE)
    style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    names = pareto.index[::-1]
    ax.barh(names, pareto.minutes[::-1], color=BLUE, height=0.6)
    for y, (m, k) in enumerate(zip(pareto.minutes[::-1], pareto.stops[::-1])):
        ax.text(m + 0.4, y, f"{m:.0f} min ({k} stop{'s' if k > 1 else ''})", va="center", fontsize=9, color=INK2)
    ax.set_xlabel("Unplanned downtime (minutes)")
    ax.set_title("Where availability was lost", loc="left", fontsize=13, fontweight="semibold", pad=10)
    ax.tick_params(axis="y", colors=INK2)
    fig.savefig(OUT / "downtime_pareto.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    # Checkweigher vs manual scale
    fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=SURFACE)
    style(ax)
    bins = np.arange(d.min() - 0.15, d.max() + 0.25, 0.1)
    ax.hist(d, bins=bins, color=BLUE, edgecolor=SURFACE, lw=1.5)
    ax.axvline(0, color=INK2, lw=1)
    ax.axvline(bias, color=CRITICAL, lw=1.5)
    ax.axvspan(bias - half, bias + half, color=CRITICAL, alpha=0.15, lw=0, zorder=0)
    ax.text(0.98, 0.95,
            f"Mean bias +{bias:.2f} g\n95% CI {bias - half:.2f} to {bias + half:.2f} g",
            fontsize=9, color=INK, va="top", ha="right", transform=ax.transAxes)
    ax.set_xlabel("Checkweigher minus manual reference scale (g), same packs")
    ax.set_ylabel("Packs")
    ax.set_title("Validating live data against manual measurements", loc="left",
                 fontsize=13, fontweight="semibold", pad=10)
    fig.savefig(OUT / "checkweigher_validation.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def print_report(s):
    c, o, v, w, sp = s["completeness"], s["oee"], s["validation"], s["weight"], s["spc"]
    ep = sp["episode"]
    print(f"""
DATA COMPLETENESS
  Checkweigher counted {c['equipment_packs']} packs; database captured {c['collected']} (missing {c['missing']}).
  Manual checks matched to packs: {c['manual_checks']} (unmatched {c['manual_unmatched']}).

SPC (subgroups of {sp['subgroup_size']}, limits from {sp['baseline_subgroups']} baseline subgroups)
  CL {sp['center']:.2f} g, UCL {sp['ucl']:.2f}, LCL {sp['lcl']:.2f}; pack sigma {sp['sigma']:.2f} g; Cpl {sp['cpl_baseline']:.2f}
  Rule violations: {sp['signals']} (X-bar), {sp['r_signals']} (R)""")
    if ep:
        print(f"  First alarm {ep['first_alarm']} (rule {ep['rule']}); rejects passed 10 % at {ep['reject_spike']}"
              f" -> {ep['lead_min']:.0f} min warning. 30-min manual check would catch it at {ep['periodic_detect']}.")
    print(f"""
OEE
  Planned time {o['planned_time_min']:.0f} min, downtime {o['downtime_min']:.0f} min, run {o['run_time_min']:.0f} min
  Availability {o['availability']:.1%} x Performance {o['performance']:.1%} x Quality {o['quality']:.1%} = OEE {o['oee']:.1%}
  Top loss: {o['pareto'][0]['reason']} ({o['pareto'][0]['minutes']:.0f} min)

CHECKWEIGHER VALIDATION (n = {v['n']})
  Bias +{v['bias_g']:.2f} g (95% CI {v['ci95'][0]:.2f} to {v['ci95'][1]:.2f}), t = {v['t']:.1f}, p = {v['p']:.1g}

GIVEAWAY
  {w['giveaway_kg']:.1f} kg over label as measured ({w['giveaway_g_per_pack']:.1f} g/pack); about {w['giveaway_true_kg']:.1f} kg after correcting the bias.
Charts and summary.json written to {OUT}
""")


if __name__ == "__main__":
    main()
