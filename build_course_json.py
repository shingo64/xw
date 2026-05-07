#!/usr/bin/env python3
"""KML(course.kml) → app/data/course.json 変換。

course.json 構造:
{
  "name": "...",
  "total_km": 99.57,
  "start": {"lat":..., "lon":..., "name":"スタート"},
  "goal":  {"lat":..., "lon":..., "name":"GOAL"},
  "checkpoints": [{ "name":"CP1","km":21,"lat":...,"lon":...,"venue":"..." }, ...],
  "toilets":     [{ "name":"...", "lat":..., "lon":... }, ...],
  "km_markers":  [{ "km":1, "lat":..., "lon":... }, ...],
  "course": [ [lat,lon], [lat,lon], ... ]   # メインコースを start→goal で連結した1本のポリライン
}
"""
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"k": "http://www.opengis.net/kml/2.2"}
ROOT = Path(__file__).parent
KML = ROOT / "data" / "course.kml"
OUT = ROOT / "app" / "data" / "course.json"

CP_VENUE = {
    "CP1": "湘南海岸公園（平塚市）",
    "CP2": "湘南海岸公園 水の広場（藤沢市）",
    "CP3": "横浜市児童遊園地（横浜市保土ケ谷区）",
    "CP4": "ポートサイド公園（横浜市神奈川区）",
    "CP5": "鈴ヶ森道路児童遊園（東京都品川区）",
}
CP_KM = {"CP1": 21, "CP2": 33, "CP3": 54, "CP4": 67, "CP5": 86}


def parse_coords(text: str):
    pts = []
    for tok in text.strip().split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        lon = float(parts[0])
        lat = float(parts[1])
        pts.append([lat, lon])
    return pts


def short_cp(name: str) -> str:
    m = re.match(r"CP(\d)", name)
    if m:
        return f"CP{m.group(1)}"
    return name


def main():
    tree = ET.parse(KML)
    root = tree.getroot()

    checkpoints = []
    toilets = []
    km_markers = []
    start_pt = None
    goal_pt = None
    segments = {}  # name -> [[lat,lon],...]

    for folder in root.iterfind(".//k:Folder", NS):
        fname = folder.findtext("k:name", "", NS)
        for pm in folder.findall("k:Placemark", NS):
            name = pm.findtext("k:name", "", NS).strip()
            coord_text = (pm.findtext(".//k:coordinates", "", NS) or "").strip()
            pts = parse_coords(coord_text) if coord_text else []
            is_point = pm.find(".//k:Point", NS) is not None
            is_line = pm.find(".//k:LineString", NS) is not None

            if fname == "CP" and is_point and pts:
                key = short_cp(name)
                lat, lon = pts[0]
                checkpoints.append({
                    "name": key,
                    "km": CP_KM.get(key),
                    "lat": lat,
                    "lon": lon,
                    "venue": CP_VENUE.get(key, name),
                    "raw_name": name,
                })
            elif fname == "1㎞ごとポイント" and is_point and pts:
                lat, lon = pts[0]
                if "スタート" in name or "start" in name.lower():
                    start_pt = {"name": "スタート", "lat": lat, "lon": lon}
                else:
                    m = re.search(r"(\d+)\s*[㎞km]", name)
                    if m:
                        km_markers.append({"km": int(m.group(1)), "lat": lat, "lon": lon})
            elif fname == "トイレ" and is_point and pts:
                lat, lon = pts[0]
                toilets.append({"name": name, "lat": lat, "lon": lon})
            elif "⇒" in fname and is_line and pts:
                segments[fname] = pts

    # メインコースを START⇒CP1, CP1⇒CP2, ... CP5⇒GOAL の順で連結
    seg_order = ["START⇒CP1", "CP1⇒CP2", "CP2⇒CP3", "CP3⇒CP4", "CP4⇒CP5", "CP5⇒GOAL"]
    course = []
    for sname in seg_order:
        if sname not in segments:
            raise RuntimeError(f"missing segment: {sname}")
        seg = segments[sname]
        if course and seg:
            # 重複しがちな結合点を除く
            if course[-1] == seg[0]:
                course.extend(seg[1:])
            else:
                course.extend(seg)
        else:
            course.extend(seg)

    # ゴール座標 = コース末尾
    if course:
        goal_pt = {"name": "GOAL", "lat": course[-1][0], "lon": course[-1][1]}
    # スタート座標が km_markers に紛れてなかったらコース先頭
    if start_pt is None and course:
        start_pt = {"name": "スタート", "lat": course[0][0], "lon": course[0][1]}

    checkpoints.sort(key=lambda c: c["km"] or 0)
    km_markers.sort(key=lambda c: c["km"])

    out = {
        "name": "第12回東京エクストリームウォーク100",
        "total_km": round(_polyline_length_km(course), 3),
        "start": start_pt,
        "goal": goal_pt,
        "checkpoints": checkpoints,
        "toilets": toilets,
        "km_markers": km_markers,
        "course": course,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    print(f"  total: {out['total_km']:.2f} km, course pts: {len(course)}")
    print(f"  checkpoints: {len(checkpoints)}, toilets: {len(toilets)}, km_markers: {len(km_markers)}")


def _polyline_length_km(pts):
    from math import radians, sin, cos, asin, sqrt
    R = 6371.0
    total = 0.0
    for i in range(1, len(pts)):
        la1, lo1 = pts[i - 1]
        la2, lo2 = pts[i]
        p1, p2 = radians(la1), radians(la2)
        dp = radians(la2 - la1)
        dl = radians(lo2 - lo1)
        a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
        total += 2 * R * asin(sqrt(a))
    return total


if __name__ == "__main__":
    main()
