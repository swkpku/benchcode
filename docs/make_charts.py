"""Draw the README charts from benchcode result rows. Standard library only; writes SVG, one light and one dark file each.

    python3 docs/make_charts.py docs/runs/2026-11-09-m1-max/*.json

Reads each row's per-depth reading rate and agent-step time and draws them against context depth on a log axis,
one line per server-and-model pair: colour is the server, dash is the model."""
import json, math, os, sys

COLOURS = {"kapitsa": "#d9480f", "omlx": "#1c7ed6", "mlx-serve": "#2f9e44", "ollama": "#7048e8", "tensorfold": "#0ca678"}
DASHES = {"27B": "", "35B-A3B": "7 4", "Flash-Next": "2 4"}
THEMES = {"light": {"bg": "#ffffff", "fg": "#1f2328", "grid": "#d0d7de", "muted": "#59636e"},
          "dark": {"bg": "#0d1117", "fg": "#e6edf3", "grid": "#30363d", "muted": "#9198a1"}}


def pair(row):
    """(server, model) labels from a result row: the server from its process name, the model from its id."""
    proc = (row.get("server") or {}).get("process") or ""
    mid = str(row.get("model") or "")
    server = ("kapitsa" if "kapitsa" in proc else "ollama" if "ollama" in proc else "mlx-serve" if "mlx-serve" in proc
              else "tensorfold" if proc.startswith("tensorfol") else "omlx" if "python" in proc else proc)
    model = "Flash-Next" if "flash" in mid.lower() else "35B-A3B" if "35b" in mid.lower() else "27B" if "27b" in mid.lower() else mid
    if server == "tensorfold" and (row.get("window") or {}).get("declared", 0) > 100000:
        model += " (139K ctx)"
    return server, model


def series(rows, pick):
    out = []
    for row in rows:
        pts = []
        for k, d in sorted(((int(k), v) for k, v in row["depth"].items()), key=lambda x: x[0]):
            v = pick(d)
            if v:
                pts.append((k, v))
        if pts:
            out.append((pair(row), pts))
    return out


def chart(path, data, title, unit, ylog, theme):
    t = THEMES[theme]
    W, H, L, R, T, B = 760, 420, 64, 210, 44, 48
    xs = sorted({k for _, pts in data for k, _ in pts})
    ys = [v for _, pts in data for _, v in pts]
    x0, x1 = math.log2(min(xs)), math.log2(max(xs))
    if ylog:
        y0, y1 = math.floor(math.log10(min(ys)) * 2) / 2, math.ceil(math.log10(max(ys)) * 2) / 2
        ticks = [10 ** e for e in range(int(math.floor(y0)), int(math.ceil(y1)) + 1)]
        ticks = sorted({v * m for v in ticks for m in (1, 2, 5) if y0 <= math.log10(v * m) <= y1})
        fy = lambda v: T + (H - T - B) * (1 - (math.log10(v) - y0) / (y1 - y0))
    else:
        step = 10 ** math.floor(math.log10(max(ys))) / 2
        y0, y1 = 0, math.ceil(max(ys) / step) * step
        ticks = [y0 + i * step for i in range(int((y1 - y0) / step) + 1)]
        fy = lambda v: T + (H - T - B) * (1 - (v - y0) / (y1 - y0))
    fx = lambda k: L + (W - L - R) * (math.log2(k) - x0) / (x1 - x0)
    font = "font-family='-apple-system, Segoe UI, Helvetica, Arial, sans-serif'"
    o = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{W}' height='{H}' viewBox='0 0 {W} {H}' {font} font-size='12'>",
         f"<rect width='{W}' height='{H}' fill='{t['bg']}'/>",
         f"<text x='{L}' y='24' font-size='15' font-weight='600' fill='{t['fg']}'>{title}</text>"]
    for v in ticks:
        y = fy(v)
        o.append(f"<line x1='{L}' x2='{W - R}' y1='{y:.1f}' y2='{y:.1f}' stroke='{t['grid']}' stroke-width='1'/>")
        o.append(f"<text x='{L - 8}' y='{y + 4:.1f}' text-anchor='end' fill='{t['muted']}'>{v:g}</text>")
    for k in xs:
        x = fx(k)
        o.append(f"<text x='{x:.1f}' y='{H - B + 18}' text-anchor='middle' fill='{t['muted']}'>{k // 1000}K</text>")
    o.append(f"<text x='{(L + W - R) / 2:.0f}' y='{H - 12}' text-anchor='middle' fill='{t['muted']}'>context tokens before the request</text>")
    o.append(f"<text x='{L}' y='{T - 8}' fill='{t['muted']}'>{unit}</text>")
    for i, ((server, model), pts) in enumerate(data):
        c, d = COLOURS.get(server, t["fg"]), DASHES.get(model, "")
        path_d = " ".join(f"{'M' if j == 0 else 'L'}{fx(k):.1f},{fy(v):.1f}" for j, (k, v) in enumerate(pts))
        o.append(f"<path d='{path_d}' fill='none' stroke='{c}' stroke-width='2.2' stroke-dasharray='{d}' stroke-linejoin='round'/>")
        for k, v in pts:
            o.append(f"<circle cx='{fx(k):.1f}' cy='{fy(v):.1f}' r='3' fill='{c}'/>")
        ly = T + 8 + i * 19
        o.append(f"<line x1='{W - R + 16}' x2='{W - R + 44}' y1='{ly}' y2='{ly}' stroke='{c}' stroke-width='2.2' stroke-dasharray='{d}'/>")
        o.append(f"<text x='{W - R + 52}' y='{ly + 4}' fill='{t['fg']}'>{server} {model}</text>")
    o.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(o))


def main(files):
    rows = [json.load(open(f)) for f in files]
    order = {"kapitsa": 0, "omlx": 1, "mlx-serve": 2, "ollama": 3, "tensorfold": 4}
    reading = sorted(series(rows, lambda d: d.get("prefill_tps")), key=lambda s: (order.get(s[0][0], 9), s[0][1]))
    step = sorted(series(rows, lambda d: (d.get("step") or {}).get("total_s")), key=lambda s: (order.get(s[0][0], 9), s[0][1]))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    os.makedirs(out, exist_ok=True)
    for theme in THEMES:
        chart(os.path.join(out, f"reading-{theme}.svg"), reading, "Reading speed by context depth (cold prompt, thinking off)", "tok/s, log scale", True, theme)
        chart(os.path.join(out, f"agent-step-{theme}.svg"), step, "Agent step by context depth (a 4K tool result, then 64 tokens)", "seconds, log scale", True, theme)
    print(f"wrote 4 charts to {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
