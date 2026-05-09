#!/usr/bin/env python3
"""中山道（日本橋〜蕨宿）コースデータを Google MyMaps の中山道KML(全長版)から生成。

「中山道 - mid=1BW-orQ6PfqOZumwPDj1LQAM1M08」というユーザー作成の旧中山道
（京都〜江戸）をなぞった全行程KMLから、日本橋〜蕨宿区間に該当する2本の
LineStringを抽出 → 反転して 日本橋→蕨宿 の順に接続 → OSRM徒歩で密化、
の流れで自然な歩行ポリラインを生成する。

出力:
  app/data/course-nakasendo.json   - course.json と同じスキーマ（5/10/15/20km地点をCP化）
  app/data/cutoffs-nakasendo.json  - START / CP1〜CP4 / GOAL の関門時刻
"""
import json
import math
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parent
OUT_COURSE = ROOT / "app" / "data" / "course-nakasendo.json"
OUT_CUTOFFS = ROOT / "app" / "data" / "cutoffs-nakasendo.json"

# 中山道（京都〜江戸）全行程をなぞった Google MyMaps
NAKASENDO_KML_URL = "https://www.google.com/maps/d/kml?mid=1BW-orQ6PfqOZumwPDj1LQAM1M08&forcekml=1"
KML_CACHE = Path("/tmp/nakasendo_full.kml")
# bbox に収まる LineString のうち、日本橋〜巣鴨と巣鴨〜浦和方面のもの
TOKYO_LAT_RANGE = (35.66, 35.95)
TOKYO_LON_RANGE = (139.55, 139.78)

OSRM = "https://router.project-osrm.org/route/v1/foot/"

NIHONBASHI = (35.6840553, 139.7744893)
WARABI_HONJIN = (35.8254578, 139.6784243)


def haversine_m(la1, lo1, la2, lo2):
    R = 6371000
    p1 = math.radians(la1); p2 = math.radians(la2)
    dp = math.radians(la2 - la1); dl = math.radians(lo2 - lo1)
    a = math.sin(dp/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def download_kml():
    if not KML_CACHE.exists() or KML_CACHE.stat().st_size < 1000:
        subprocess.check_call(["curl", "-sLo", str(KML_CACHE), NAKASENDO_KML_URL], timeout=60)
    return KML_CACHE


def parse_coords(text):
    pts = []
    for c in text.strip().split():
        parts = c.split(",")
        pts.append((float(parts[1]), float(parts[0])))
    return pts


def extract_tokyo_polyline():
    """KMLから日本橋〜浦和方面の2本のLineStringを抽出して接続"""
    tree = ET.parse(KML_CACHE)
    root = tree.getroot()
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    lines = root.findall(".//k:LineString/k:coordinates", ns)
    print(f"  KML LineStrings: {len(lines)}")
    candidates = []
    for line in lines:
        pts = parse_coords(line.text)
        lats = [p[0] for p in pts]; lons = [p[1] for p in pts]
        if (TOKYO_LAT_RANGE[0] <= min(lats) and max(lats) <= TOKYO_LAT_RANGE[1] and
                TOKYO_LON_RANGE[0] <= min(lons) and max(lons) <= TOKYO_LON_RANGE[1]):
            candidates.append(pts)
    print(f"  matching segments: {len(candidates)}")
    if not candidates:
        raise RuntimeError("KMLから東京区間が抽出できなかった")
    # 端点同士が近いもの同士を順番に繋ぐ。日本橋スタート → 蕨方向。
    # 各候補について、日本橋に近い端点で並び替え
    def dist_to_nb(seg):
        return min(haversine_m(seg[0][0], seg[0][1], *NIHONBASHI),
                   haversine_m(seg[-1][0], seg[-1][1], *NIHONBASHI))
    candidates.sort(key=dist_to_nb)
    # 1本目: 日本橋に最も近い端点があるセグメントを採用、日本橋側を起点に
    seg1 = candidates[0]
    if haversine_m(seg1[0][0], seg1[0][1], *NIHONBASHI) > haversine_m(seg1[-1][0], seg1[-1][1], *NIHONBASHI):
        seg1 = list(reversed(seg1))
    polyline = list(seg1)
    used = {0}
    # 残りを greedy NN で連結（蕨に到達するまで）
    while True:
        cur = polyline[-1]
        d_to_warabi = haversine_m(cur[0], cur[1], *WARABI_HONJIN)
        if d_to_warabi < 500:
            break
        best_i, best_d, best_rev = -1, float("inf"), False
        for i, seg in enumerate(candidates):
            if i in used: continue
            d_h = haversine_m(cur[0], cur[1], seg[0][0], seg[0][1])
            d_t = haversine_m(cur[0], cur[1], seg[-1][0], seg[-1][1])
            if d_h < best_d: best_d = d_h; best_i = i; best_rev = False
            if d_t < best_d: best_d = d_t; best_i = i; best_rev = True
        if best_i < 0 or best_d > 2000:
            break
        seg = candidates[best_i]
        if best_rev: seg = list(reversed(seg))
        polyline.extend(seg[1:])  # skip first point to dedupe
        used.add(best_i)
    print(f"  joined polyline: {len(polyline)} pts")
    return polyline


def trim_to_warabi(polyline):
    """蕨宿本陣に最も近い点までトリム + 蕨宿座標で終端"""
    best_i, best_d = 0, float("inf")
    for i, p in enumerate(polyline):
        d = haversine_m(p[0], p[1], *WARABI_HONJIN)
        if d < best_d: best_d = d; best_i = i
    trimmed = polyline[:best_i + 1]
    trimmed.append(WARABI_HONJIN)
    return trimmed


def osrm_densify(waypoints):
    """waypointsをOSRM徒歩で繋いで密なpolylineにする。waypointが多い場合はチャンク分割"""
    CHUNK = 90  # 公開OSRMの実用上限あたり
    full = []
    i = 0
    while i < len(waypoints) - 1:
        chunk = waypoints[i:i + CHUNK]
        coords = ";".join(f"{lon},{lat}" for lat, lon in chunk)
        url = OSRM + coords + "?overview=full&geometries=geojson"
        out = subprocess.check_output(["curl", "-sf", url], timeout=120)
        d = json.loads(out.decode("utf-8"))
        if d.get("code") != "Ok":
            raise RuntimeError(f"OSRM error in chunk {i}: {d}")
        geom = d["routes"][0]["geometry"]["coordinates"]
        seg = [(p[1], p[0]) for p in geom]
        if full:
            full.extend(seg[1:])  # dedupe boundary
        else:
            full.extend(seg)
        i += CHUNK - 1  # overlap by 1 to ensure continuity
    return full


def interpolate(polyline, max_seg_m=50):
    """セグメントが max_seg_m を超える場合、その間を補間して密化"""
    out = [polyline[0]]
    for i in range(1, len(polyline)):
        a, b = polyline[i-1], polyline[i]
        d = haversine_m(a[0], a[1], b[0], b[1])
        if d > max_seg_m:
            n = int(d // max_seg_m)
            for k in range(1, n + 1):
                t = k / (n + 1)
                out.append((a[0] + (b[0]-a[0]) * t, a[1] + (b[1]-a[1]) * t))
        out.append(b)
    return out


def fetch_polyline():
    """KMLから抽出した日本橋〜蕨polylineを補間で密化"""
    download_kml()
    raw = extract_tokyo_polyline()
    raw = trim_to_warabi(raw)
    print(f"  raw trimmed: {len(raw)} pts")
    densified = interpolate(raw, max_seg_m=50)
    print(f"  densified (50m間隔): {len(densified)} pts")
    return densified


def total_km(coords):
    s = 0.0
    for i in range(1, len(coords)):
        s += haversine_m(coords[i-1][0], coords[i-1][1], coords[i][0], coords[i][1])
    return s / 1000.0


def km_markers(coords, total):
    markers = []
    cum = 0.0
    next_km = 1
    for i in range(1, len(coords)):
        seg = haversine_m(coords[i-1][0], coords[i-1][1], coords[i][0], coords[i][1])
        while cum + seg >= next_km * 1000 and next_km < int(total):
            t = (next_km * 1000 - cum) / seg
            lat = coords[i-1][0] + (coords[i][0] - coords[i-1][0]) * t
            lon = coords[i-1][1] + (coords[i][1] - coords[i-1][1]) * t
            markers.append({"km": next_km, "lat": lat, "lon": lon})
            next_km += 1
        cum += seg
    return markers


def locate_at_km(coords, target_km):
    """polyline coords 上で target_km 地点の (lat, lon) を補間で返す"""
    cum = 0.0
    target_m = target_km * 1000
    for i in range(1, len(coords)):
        seg = haversine_m(coords[i-1][0], coords[i-1][1], coords[i][0], coords[i][1])
        if cum + seg >= target_m:
            t = (target_m - cum) / seg if seg > 0 else 0.0
            lat = coords[i-1][0] + (coords[i][0] - coords[i-1][0]) * t
            lon = coords[i-1][1] + (coords[i][1] - coords[i-1][1]) * t
            return lat, lon
        cum += seg
    return coords[-1][0], coords[-1][1]


def main():
    print("KML + OSRM で中山道polyline生成中...")
    polyline = fetch_polyline()
    total = total_km(polyline)
    print(f"  total: {total:.2f} km, points: {len(polyline)}")

    # 5/10/15/20km地点に練習用CPを設定（4km/hペースで関門設定）
    base_date = "2026-05-10"
    start_hour, start_min = 8, 0
    cp_kms = [5, 10, 15]
    pace_kmh = 4.0

    def cutoff_iso(km):
        secs = int(km / pace_kmh * 3600)
        h = start_hour + secs // 3600
        m = start_min + (secs % 3600) // 60
        return f"{base_date}T{h:02d}:{m:02d}:00+09:00"

    cps_for_course = []
    cps_for_cutoffs = [
        {"name": "START", "km": 0, "venue": "日本橋",
         "open": f"{base_date}T07:00:00+09:00", "cutoff": f"{base_date}T08:30:00+09:00"},
    ]
    for km in cp_kms:
        lat, lon = locate_at_km(polyline, km)
        cps_for_course.append({"name": f"CP{cp_kms.index(km) + 1}", "km": km, "lat": lat, "lon": lon, "venue": f"{km}km地点（中山道沿い）"})
        cps_for_cutoffs.append({
            "name": f"CP{cp_kms.index(km) + 1}", "km": km, "venue": f"{km}km地点",
            "open": f"{base_date}T{start_hour:02d}:{start_min:02d}:00+09:00",
            "cutoff": cutoff_iso(km),
        })
    cps_for_cutoffs.append({
        "name": "GOAL", "km": round(total, 2), "venue": "蕨宿",
        "open": f"{base_date}T{start_hour:02d}:{start_min:02d}:00+09:00",
        "cutoff": cutoff_iso(total),
    })

    course = {
        "name": "中山道（日本橋〜蕨宿）練習用",
        "total_km": round(total, 2),
        "start": {"lat": polyline[0][0], "lon": polyline[0][1], "name": "日本橋"},
        "goal": {"lat": polyline[-1][0], "lon": polyline[-1][1], "name": "蕨宿"},
        "checkpoints": cps_for_course,
        "toilets": [],
        "km_markers": km_markers(polyline, total),
        "course": [[lat, lon] for lat, lon in polyline],
    }
    OUT_COURSE.write_text(json.dumps(course, ensure_ascii=False, indent=2))
    print(f"wrote {OUT_COURSE}")

    cutoffs = {
        "event": "中山道練習（日本橋〜蕨宿）",
        "user_start": f"{base_date}T{start_hour:02d}:{start_min:02d}:00+09:00",
        "checkpoints": cps_for_cutoffs,
    }
    OUT_CUTOFFS.write_text(json.dumps(cutoffs, ensure_ascii=False, indent=2))
    print(f"wrote {OUT_CUTOFFS}")
    for c in cps_for_cutoffs:
        print(f"  {c['name']:5} {c['km']:>6}km cutoff {c['cutoff']}")


if __name__ == "__main__":
    main()
