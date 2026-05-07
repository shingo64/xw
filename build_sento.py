#!/usr/bin/env python3
"""コース40-60km区間の銭湯/温浴施設を OpenStreetMap Overpass API から抽出。

- amenity=public_bath, amenity=spa を取得
- コース上 40km 〜 60km の範囲に射影されるものを採用
- 距離制限（コースから BUFFER_M m 以内）で絞り込み
- website / contact:website / phone なども保持
- app/data/sento.json に出力
"""
import json
import urllib.request
import urllib.parse
import math
from pathlib import Path

ROOT = Path(__file__).parent
COURSE_JSON = ROOT / "app" / "data" / "course.json"
OUT = ROOT / "app" / "data" / "sento.json"
KM_MIN = 40.0
KM_MAX = 60.0
BUFFER_M = 800  # 銭湯は寄り道前提なので広めに

OVERPASS = "https://overpass-api.de/api/interpreter"

# OSM上の名前と実店舗名がずれてるケースを手動補正
# キー: OSM上の name にこの文字列が含まれていたら → 値で置換
OVERRIDES = {
    "第二常磐湯": {"name": "深川温泉常盤湯"},
}


def haversine_m(la1, lo1, la2, lo2):
    R = 6371000.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1)
    dl = math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def project_to_course(lat, lon, course, cum_km):
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
            best = (km, d2, [fy / M, fx / (cos_lat * M)])
    return best[0], math.sqrt(best[1])


def main():
    course_data = json.loads(COURSE_JSON.read_text())
    course = course_data["course"]
    cum = [0.0]
    for i in range(1, len(course)):
        cum.append(cum[-1] + haversine_m(course[i - 1][0], course[i - 1][1], course[i][0], course[i][1]) / 1000.0)

    # 40-60km区間の点でbboxを計算（バッファ込み）
    target_pts = []
    for i, p in enumerate(course):
        if KM_MIN - 1 <= cum[i] <= KM_MAX + 1:
            target_pts.append(p)
    lats = [p[0] for p in target_pts]
    lons = [p[1] for p in target_pts]
    pad = BUFFER_M / 111000.0 + 0.005
    bbox = (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)
    print(f"target km range: {KM_MIN}-{KM_MAX}, buffer {BUFFER_M}m")
    print(f"bbox: {bbox}")

    # Overpass: 銭湯・スパ・温泉
    q = f"""
[out:json][timeout:60];
(
  node["amenity"="public_bath"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
  way["amenity"="public_bath"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
  node["amenity"="spa"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
  way["amenity"="spa"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
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
        km, off_m = project_to_course(la, lo, course, cum)
        if not (KM_MIN <= km <= KM_MAX):
            continue
        if off_m > BUFFER_M:
            continue
        name = tags.get("name") or tags.get("name:ja") or tags.get("brand") or "銭湯"
        website = tags.get("website") or tags.get("contact:website") or tags.get("url") or ""
        # 手動オーバーライド適用
        for key, override in OVERRIDES.items():
            if key in name:
                if "name" in override:
                    name = override["name"]
                if "website" in override and not website:
                    website = override["website"]
                break
        # ウェブサイトURLが裸ドメインだったら http:// 補完
        if website and not website.startswith("http"):
            website = "http://" + website
        # 検索フォールバックURL
        search_url = f"https://www.google.com/search?q={urllib.parse.quote(name + ' 銭湯')}"
        items.append({
            "lat": la,
            "lon": lo,
            "km": round(km, 3),
            "off_m": round(off_m, 1),
            "name": name,
            "amenity": tags.get("amenity"),
            "website": website,
            "search": search_url,
            "phone": tags.get("phone") or tags.get("contact:phone") or "",
            "opening_hours": tags.get("opening_hours") or "",
        })

    items.sort(key=lambda x: x["km"])
    out = {
        "km_range": [KM_MIN, KM_MAX],
        "buffer_m": BUFFER_M,
        "count": len(items),
        "items": items,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {len(items)} items)")
    for x in items:
        web = x["website"][:50] if x["website"] else "(no site)"
        print(f"  {x['km']:.1f}km off{int(x['off_m'])}m  {x['name']}  | {x['amenity']} | {web}")


if __name__ == "__main__":
    main()
