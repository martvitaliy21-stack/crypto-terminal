"""Генератор точечной карты мира для раздела «Мир»: docs/world.json = список [lon, lat] точек суши.
Точки расставлены равномерно по сфере (шаг по долготе растёт как 1/cos(широты)), Антарктида исключена.
Источник контуров: Natural Earth 1:110m land (public domain). Запуск один раз: python worldmap.py path/to/ne_110m_land.geojson
"""
import json
import math
import sys
import numpy as np

STEP = 1.45          # градусов между точками по широте
LAT_MIN, LAT_MAX = -56.0, 83.0


def inside(points: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Чётно-нечётный тест (ray casting), векторно, блоками по 2000 точек."""
    out = np.zeros(len(points), dtype=bool)
    x0, y0 = poly[:-1, 0][None, :], poly[:-1, 1][None, :]
    x1, y1 = poly[1:, 0][None, :], poly[1:, 1][None, :]
    dy = np.where(y1 - y0 == 0, 1e-12, y1 - y0)
    for i in range(0, len(points), 2000):
        px, py = points[i:i + 2000, 0][:, None], points[i:i + 2000, 1][:, None]
        cond = (y0 > py) != (y1 > py)
        xint = x0 + (py - y0) * (x1 - x0) / dy
        out[i:i + 2000] = ((cond & (px < xint)).sum(axis=1) % 2) == 1
    return out


def main(src: str, dst: str = "docs/world.json"):
    g = json.load(open(src))
    rings = []   # (outer, [holes])
    for f in g["features"]:
        geom = f["geometry"]
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        for p in polys:
            rings.append((np.array(p[0], dtype=float), [np.array(h, dtype=float) for h in p[1:]]))
    grid = []
    lat = LAT_MIN
    while lat <= LAT_MAX:
        lon_step = STEP / max(math.cos(math.radians(lat)), 0.12)
        lon = -180.0 + (lat / STEP % 1) * lon_step * 0.5   # лёгкий сдвиг рядов, чтобы не было строгой решётки
        while lon < 180.0:
            grid.append((lon, lat)); lon += lon_step
        lat += STEP
    pts = np.array(grid)
    land = np.zeros(len(pts), dtype=bool)
    for outer, holes in rings:
        bb = (pts[:, 0] >= outer[:, 0].min()) & (pts[:, 0] <= outer[:, 0].max()) & (pts[:, 1] >= outer[:, 1].min()) & (pts[:, 1] <= outer[:, 1].max())
        idx = np.where(bb & ~land)[0]
        if not len(idx):
            continue
        ins = inside(pts[idx], outer)
        for h in holes:
            ins &= ~inside(pts[idx], h)
        land[idx[ins]] = True
    dots = [[round(float(x), 2), round(float(y), 2)] for x, y in pts[land]]
    json.dump(dict(step=STEP, lat=[LAT_MIN, LAT_MAX], dots=dots), open(dst, "w"), separators=(",", ":"))
    print(f"grid {len(pts)} → land dots {len(dots)}; written {dst} ({len(json.dumps(dots)) // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "docs/world.json")
