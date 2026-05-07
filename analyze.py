#!/usr/bin/env python3
"""Analyze GPX walking practice file."""
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from math import radians, sin, cos, asin, sqrt

JST = timezone(timedelta(hours=9))
NS = {"g": "http://www.topografix.com/GPX/1/1"}


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * R * asin(sqrt(a))


def parse(path):
    tree = ET.parse(path)
    root = tree.getroot()
    pts = []
    for tp in root.iterfind(".//g:trkpt", NS):
        lat = float(tp.get("lat"))
        lon = float(tp.get("lon"))
        ele = float(tp.findtext("g:ele", "0", NS))
        t = datetime.fromisoformat(tp.findtext("g:time", "", NS).replace("Z", "+00:00"))
        pts.append((t, lat, lon, ele))
    return pts


def fmt_hms(seconds):
    s = int(seconds)
    return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def fmt_pace(sec_per_km):
    if sec_per_km is None or sec_per_km != sec_per_km:
        return "-"
    m = int(sec_per_km // 60)
    s = int(sec_per_km % 60)
    return f"{m}'{s:02d}\"/km"


def kmh(sec_per_km):
    if sec_per_km is None or sec_per_km <= 0:
        return 0
    return 3600.0 / sec_per_km


def main():
    pts = parse("/Users/takahatashingo/Documents/prod/xw/data/ウォーキングの練習.gpx")
    n = len(pts)
    t0 = pts[0][0]
    tn = pts[-1][0]
    total_elapsed = (tn - t0).total_seconds()

    cum_dist = [0.0]
    seg_dist = [0.0]
    seg_dt = [0.0]
    elev_gain = 0.0
    elev_loss = 0.0
    moving_seconds = 0.0
    moving_distance = 0.0
    stops = []  # list of (start_t, end_t, lat, lon, duration)

    stop_threshold_speed = 0.5  # m/s, below this counts as stopped
    stop_min_duration = 30  # seconds

    cur_stop_start = None
    cur_stop_lat_lon = None

    last_ele = pts[0][3]

    for i in range(1, n):
        t_prev, la_p, lo_p, e_p = pts[i - 1]
        t, la, lo, e = pts[i]
        d = haversine(la_p, lo_p, la, lo)
        dt = (t - t_prev).total_seconds()
        cum_dist.append(cum_dist[-1] + d)
        seg_dist.append(d)
        seg_dt.append(dt)
        # elevation
        de = e - last_ele
        if de > 0:
            elev_gain += de
        else:
            elev_loss += -de
        last_ele = e
        # moving / stop
        spd = d / dt if dt > 0 else 0
        if spd >= stop_threshold_speed:
            moving_seconds += dt
            moving_distance += d
            if cur_stop_start is not None:
                dur = (t_prev - cur_stop_start).total_seconds()
                if dur >= stop_min_duration:
                    stops.append((cur_stop_start, t_prev, cur_stop_lat_lon[0], cur_stop_lat_lon[1], dur))
                cur_stop_start = None
        else:
            if cur_stop_start is None:
                cur_stop_start = t_prev
                cur_stop_lat_lon = (la_p, lo_p)
    if cur_stop_start is not None:
        dur = (pts[-1][0] - cur_stop_start).total_seconds()
        if dur >= stop_min_duration:
            stops.append((cur_stop_start, pts[-1][0], cur_stop_lat_lon[0], cur_stop_lat_lon[1], dur))

    total_dist = cum_dist[-1]
    avg_pace_total = total_elapsed / (total_dist / 1000) if total_dist > 0 else None
    avg_pace_moving = moving_seconds / (moving_distance / 1000) if moving_distance > 0 else None

    print("=" * 60)
    print("【概況】")
    print("=" * 60)
    print(f"スタート: {t0.astimezone(JST).strftime('%Y-%m-%d %H:%M:%S')} JST")
    print(f"フィニッシュ: {tn.astimezone(JST).strftime('%Y-%m-%d %H:%M:%S')} JST")
    print(f"トラックポイント数: {n:,}")
    print(f"総距離: {total_dist/1000:.2f} km")
    print(f"総経過時間: {fmt_hms(total_elapsed)} ({total_elapsed/3600:.2f}h)")
    print(f"動いてた時間: {fmt_hms(moving_seconds)} ({moving_seconds/3600:.2f}h)")
    print(f"停止時間: {fmt_hms(total_elapsed - moving_seconds)}")
    print(f"平均ペース(総合): {fmt_pace(avg_pace_total)}  /  {kmh(avg_pace_total):.2f} km/h")
    print(f"平均ペース(歩行のみ): {fmt_pace(avg_pace_moving)}  /  {kmh(avg_pace_moving):.2f} km/h")
    print(f"累積標高: +{elev_gain:.0f}m / -{elev_loss:.0f}m")

    # 1km ごとのスプリット
    print()
    print("=" * 60)
    print("【1kmごとのスプリット】")
    print("=" * 60)
    splits = []
    last_idx = 0
    last_dist = 0
    last_time = pts[0][0]
    next_mark = 1000
    for i, d in enumerate(cum_dist):
        while d >= next_mark and next_mark <= total_dist:
            # interpolate time at boundary
            d_prev = cum_dist[i - 1] if i > 0 else 0
            t_prev = pts[i - 1][0] if i > 0 else pts[0][0]
            t_cur = pts[i][0]
            if d > d_prev:
                frac = (next_mark - d_prev) / (d - d_prev)
                t_at = t_prev + (t_cur - t_prev) * frac
            else:
                t_at = t_cur
            split_sec = (t_at - last_time).total_seconds()
            splits.append((next_mark / 1000, split_sec))
            last_time = t_at
            next_mark += 1000

    print(f"{'km':>4}  {'split':>10}  {'pace':>12}  {'km/h':>6}  {'累計':>10}")
    cum = 0
    for km, sec in splits:
        cum += sec
        print(f"{int(km):>4}  {fmt_hms(sec):>10}  {fmt_pace(sec):>12}  {kmh(sec):>6.2f}  {fmt_hms(cum):>10}")

    if splits:
        first_half = splits[: len(splits) // 2]
        second_half = splits[len(splits) // 2:]
        avg_first = sum(s for _, s in first_half) / len(first_half)
        avg_second = sum(s for _, s in second_half) / len(second_half)
        print()
        print(f"前半{len(first_half)}km平均: {fmt_pace(avg_first)} ({kmh(avg_first):.2f} km/h)")
        print(f"後半{len(second_half)}km平均: {fmt_pace(avg_second)} ({kmh(avg_second):.2f} km/h)")
        delta = avg_second - avg_first
        print(f"後半の落ち: {'+' if delta>0 else ''}{int(delta)}秒/km ({(delta/avg_first*100):+.1f}%)")

    # stops
    print()
    print("=" * 60)
    print(f"【停止箇所】(連続{stop_min_duration}秒以上、{len(stops)}箇所)")
    print("=" * 60)
    stops_sorted = sorted(stops, key=lambda s: s[4], reverse=True)
    for i, (s_t, e_t, la, lo, dur) in enumerate(stops_sorted[:15]):
        print(f"{i+1:2d}. {s_t.astimezone(JST).strftime('%H:%M:%S')}〜{e_t.astimezone(JST).strftime('%H:%M:%S')}  "
              f"{fmt_hms(dur)}  ({la:.5f},{lo:.5f})")
    total_stop = sum(s[4] for s in stops)
    print(f"\n停止合計: {fmt_hms(total_stop)} / {len(stops)}回")


if __name__ == "__main__":
    main()
