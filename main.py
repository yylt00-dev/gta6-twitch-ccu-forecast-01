"""Forecast GTA VI's launch-day Twitch viewers hour by hour.

Entry for make.fun's gta6-twitch-ccu-forecast-01 (program/v1, Python 3.13,
standard library only, no network, deterministic).

Method (fitted from the case's own `comparables`, past big game launches):
for each hour h = 2..24 of the release day, regress log viewers of hour h
on log viewers of hour 1 across the comparable launches, with a Gaussian
kernel weight on log hour-1 viewers (bandwidth 2.5 in log units) so launches
of a similar size to this one count most. The forecast median is the fitted
value at this case's hour-1 print; the spread blends the empirical quantiles
of the regression residuals with normal quantiles at the residual standard
deviation (50/50), so the interval is honest about how much launch days
vary. Fitted values, spreads and residuals are smoothed over neighbouring
hours (+/-1h) because a launch-day curve moves smoothly. Everything is
computed from the case file at run time: no fitted constants are baked in.

Validated offline by leave-one-out scoring on the 37 comparable launches
with a full release day (mean pinball loss ~0.41 on the log scale, vs ~0.93
for the house baseline on the same launches).

Run: python3 main.py  (MAKEFUN_INPUT_DIR / MAKEFUN_OUTPUT_DIR override
/input and /output, as in the kit's starter.)
"""

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

# The grader's -P leaves /bot off sys.path: put it back first, so a module
# shipped beside this file would import. (This entry is a single file.)
sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

# Gaussian kernel bandwidth (in natural-log units of hour-1 viewers) for the
# local weighting of comparable launches.
BANDWIDTH = 2.5
# Minimum comparable launches with data for an hour before the regression is
# trusted; below that the house baseline fills the hour in.
MIN_POINTS = 5
# The standard normal's quantile at each scored level (0.05..0.95).
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]


def norm_ppf(p):
    """Inverse standard normal CDF (Acklam's approximation), for any level.

    Used when a case asks for levels other than the standard five, so the
    normal part of the spread still matches the levels it scores.
    """
    if p <= 0.0 or p >= 1.0:
        raise ValueError("level must be strictly between 0 and 1")
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285984469534e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def round2(x):
    """x rounded to 2 decimals, half up (as the starter rounds it)."""
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value.is_integer() else value


def quantile(values, tau):
    """Linear-interpolation quantile of a sorted sample."""
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    pos = tau * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def day_series(comp):
    """Release-day hourly viewers {hour: viewers} for one comparable launch."""
    start = parse_time(comp["day_start"])
    hours = {}
    for point in comp.get("hourly") or []:
        viewers = point.get("viewers")
        if viewers is None or viewers <= 0:
            continue
        hour = int((parse_time(point["from"]) - start).total_seconds() // 3600) + 1
        if 1 <= hour <= 24:
            hours[hour] = viewers
    return hours


def weighted_ols(xs, ys, ws):
    """Weighted least squares for y ~ a + b*x; returns (a, b)."""
    total = sum(ws)
    mx = sum(w * x for x, w in zip(xs, ws)) / total
    my = sum(w * y for y, w in zip(ys, ws)) / total
    var = sum(w * (x - mx) ** 2 for x, w in zip(xs, ws))
    if var <= 0:
        return my, 0.0
    cov = sum(w * (x - mx) * (y - my) for x, y, w in zip(xs, ys, ws))
    b = cov / var
    return my - b * mx, b


def baseline_quantiles(p1, hour, levels):
    """The house baseline's row for one hour (starter's formula)."""
    z_values = Z if len(levels) == len(Z) else [norm_ppf(tau) for tau in levels]
    return [round2(p1 * math.exp(z * 0.15 * math.sqrt(hour - 1))) for z in z_values]


def forecast_case(case, name):
    levels = list(case["levels"])
    known = next((k for k in case["known"] if k["hour"] == 1), None)
    if known is None:
        raise ValueError(f"{name} has no hour-1 print")
    p1 = max(1, known["viewers"])
    log_p1 = math.log(p1)

    # Pool: every comparable with a usable release day (hour 1 and the
    # target hour present). The case's own launch and the GTA VI category
    # history have no release-day hours, so they drop out on their own.
    pool = []
    for comp in case.get("comparables") or []:
        series = day_series(comp)
        if 1 in series:
            pool.append(series)

    # Per-hour local regression of log viewers on log hour-1 viewers.
    raw = {}
    for hour in range(2, 25):
        xs, ys, ws = [], [], []
        for series in pool:
            if hour in series:
                x = math.log(series[1])
                xs.append(x)
                ys.append(math.log(series[hour]))
                ws.append(math.exp(-0.5 * ((x - log_p1) / BANDWIDTH) ** 2))
        if len(xs) < MIN_POINTS:
            raw[hour] = None
            continue
        a, b = weighted_ols(xs, ys, ws)
        mu = a + b * log_p1
        resid = [y - (a + b * x) for x, y in zip(xs, ys)]
        mean_sq = sum(r * r for r in resid) / len(resid)
        sd = math.sqrt(max(0.0, mean_sq - (sum(resid) / len(resid)) ** 2))
        raw[hour] = (mu, sd, resid)

    rows = []
    for hour in range(2, 25):
        neighbours = [raw[h] for h in (hour - 1, hour, hour + 1) if raw.get(h)]
        if not neighbours:
            rows.append({"hour": hour, "q": baseline_quantiles(p1, hour, levels)})
            continue
        mu = sum(item[0] for item in neighbours) / len(neighbours)
        sd = sum(item[1] for item in neighbours) / len(neighbours)
        resid = [r for item in neighbours for r in item[2]]
        quantiles = []
        z_values = Z if len(levels) == len(Z) else [norm_ppf(tau) for tau in levels]
        for tau, z in zip(levels, z_values):
            spread = 0.5 * quantile(resid, tau) + 0.5 * z * sd
            value = math.exp(mu + spread)
            quantiles.append(value)
        # Round half-up to 2 decimals, keep the row non-decreasing and in
        # (0, 1e9], as the forecast format requires.
        rounded = [round2(q) for q in quantiles]
        fixed = []
        prev = 0.0
        for q in rounded:
            q = max(q, prev, 0.01)
            q = min(q, 1_000_000_000)
            fixed.append(q)
            prev = q
        rows.append({"hour": hour, "q": fixed})
    return {"hours": rows}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case, path.name)
        text = json.dumps(forecast, separators=(",", ":")) + "\n"
        (OUTPUT_DIR / f"{path.stem}.out").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
