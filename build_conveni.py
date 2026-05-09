#!/usr/bin/env python3
"""コース沿いのコンビニを OpenStreetMap Overpass API から抽出。

- コースのバウンディングボックス内で shop=convenience を全部取得
- コースラインから BUFFER_M メートル以内に絞り込み
- ブランド推定（セブン/ファミマ/ローソン/ミニストップ/デイリー等）
- コース上の射影kmも計算
- app/data/conveni.json に出力
"""
import json
import sys
import urllib.request
import urllib.parse
import math
from pathlib import Path

ROOT = Path(__file__).parent
# 第1引数でコースIDを切替（"xw100" がデフォルト = course.json/conveni.json）
COURSE_ID = sys.argv[1] if len(sys.argv) > 1 else "xw100"
if COURSE_ID == "xw100":
    COURSE_JSON = ROOT / "app" / "data" / "course.json"
    OUT = ROOT / "app" / "data" / "conveni.json"
else:
    COURSE_JSON = ROOT / "app" / "data" / f"course-{COURSE_ID}.json"
    OUT = ROOT / "app" / "data" / f"conveni-{COURSE_ID}.json"
BUFFER_M = 120  # コースから何m以内を「沿い」とみなすか

OVERPASS = "https://overpass-api.de/api/interpreter"

BRAND_MAP = [
    (["seven", "セブン", "7-eleven", "7‐eleven"], "セブン-イレブン", "7"),
    (["familymart", "ファミリーマート", "ファミマ"], "ファミリーマート", "F"),
    (["lawson", "ローソン"], "ローソン", "L"),
    (["ministop", "ミニストップ"], "ミニストップ", "M"),
    (["daily", "デイリー", "ヤマザキ", "yamazaki"], "デイリーヤマザキ", "Y"),
    (["newdays", "ニューデイズ"], "ニューデイズ", "N"),
    (["poplar", "ポプラ"], "ポプラ", "P"),
    (["seicomart", "セイコーマート"], "セイコーマート", "S"),
]


def haversine_m(la1, lo1, la2, lo2):
    R = 6371000.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1)
    dl = math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def project_to_course(lat, lon, course, cum_km):
    """点をコースラインへ射影。返り値: (km, offset_m, [lat,lon])"""
    cos_lat = math.cos(math.radians(lat))
    M = 111320.0

    def to_xy(la, lo):
        return (lo * cos_lat * M, la * M)

    qx, qy = to_xy(lat, lon)
    best = (None, float("inf"), None)
    for i in range(1, len(course)):
        ax, ay = to_xy(course[i - 1][0], course[i - 1][1])
        bx, by = to_xy(course[i][0], course[i][1])
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        if seg_len2 == 0:
            t = 0.0
        else:
            t = ((qx - ax) * dx + (qy - ay) * dy) / seg_len2
            t = max(0.0, min(1.0, t))
        fx, fy = ax + dx * t, ay + dy * t
        d2 = (qx - fx) ** 2 + (qy - fy) ** 2
        if d2 < best[1]:
            seg_km = haversine_m(course[i - 1][0], course[i - 1][1], course[i][0], course[i][1]) / 1000.0
            km = cum_km[i - 1] + seg_km * t
            lat2 = fy / M
            lon2 = fx / (cos_lat * M)
            best = (km, d2, [lat2, lon2])
    return best[0], math.sqrt(best[1]), best[2]


def detect_brand(tags):
    cand = " ".join([
        tags.get("name", ""),
        tags.get("name:ja", ""),
        tags.get("name:en", ""),
        tags.get("brand", ""),
        tags.get("brand:ja", ""),
        tags.get("brand:en", ""),
        tags.get("operator", ""),
    ]).lower()
    for keywords, label, code in BRAND_MAP:
        for kw in keywords:
            if kw.lower() in cand:
                return label, code
    return tags.get("name") or "コンビニ", "?"


def main():
    course_data = json.loads(COURSE_JSON.read_text())
    course = course_data["course"]

    # 累積距離
    cum = [0.0]
    for i in range(1, len(course)):
        cum.append(cum[-1] + haversine_m(course[i - 1][0], course[i - 1][1], course[i][0], course[i][1]) / 1000.0)

    lats = [p[0] for p in course]
    lons = [p[1] for p in course]
    pad = 0.005  # 約500mマージン
    bbox = (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)
    print(f"bbox: {bbox}")

    # Overpass クエリ
    q = f"""
[out:json][timeout:60];
(
  node["shop"="convenience"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
  way["shop"="convenience"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
);
out center tags;
"""
    print("fetching from Overpass...")
    data = urllib.parse.urlencode({"data": q}).encode()
    req = urllib.request.Request(OVERPASS, data=data, headers={"User-Agent": "xw100-prep/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        result = json.loads(r.read())

    elements = result.get("elements", [])
    print(f"raw elements: {len(elements)}")

    items = []
    for e in elements:
        tags = e.get("tags") or {}
        if e["type"] == "node":
            la, lo = e["lat"], e["lon"]
        else:
            c = e.get("center")
            if not c:
                continue
            la, lo = c["lat"], c["lon"]
        km, off_m, _ = project_to_course(la, lo, course, cum)
        if off_m > BUFFER_M:
            continue
        brand, code = detect_brand(tags)
        items.append({
            "lat": la,
            "lon": lo,
            "km": round(km, 3),
            "off_m": round(off_m, 1),
            "brand": brand,
            "code": code,
            "name": tags.get("name") or brand,
        })

    items.sort(key=lambda x: x["km"])
    out = {"buffer_m": BUFFER_M, "count": len(items), "items": items}
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {len(items)} stores)")
    # ブランド別件数
    from collections import Counter
    cnt = Counter(x["brand"] for x in items)
    for k, v in cnt.most_common():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
