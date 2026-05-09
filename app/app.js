// XW100 サポートアプリ
// - GPSをコースラインに射影してコース上累積距離を算出
// - 各CPまでの距離・予想到着時刻・関門に対する貯金/借金を計算
// - 直近ペース等のStrava重複機能は持たない

const LS = {
  startISO: "xw.start_iso",
  targetHours: "xw.target_hours",
  lastFix: "xw.last_fix",
  history: "xw.history", // [{t, km}] 過去の累積距離履歴。直近平均ペース算出用
  started: "xw.started", // "1" なら「今をスタートにする」を押下済み（誤タップ防止のためボタン非表示）
};

const DEFAULT_START = "2026-05-23T08:45:00+09:00";
const HISTORY_MAX = 20;
const URL_PARAMS = new URLSearchParams(location.search);
const TEST_MODE = URL_PARAMS.get("test") === "1";
// ?course=nakasendo で練習用コース（中山道）を読み込む。デフォルトはXW100本番コース
const COURSE_ID = URL_PARAMS.get("course") || "xw100";
const IS_XW100 = COURSE_ID === "xw100";

// プランC: 本番前モードでの予想時刻計算用（4.55km/h平均ではなく区間別ペース）
const PLAN_C = {
  zones: [
    { maxKm: 30, kmh: 5.0 },   // 0-30km
    { maxKm: 60, kmh: 4.5 },   // 30-60km
    { maxKm: 1e9, kmh: 4.2 },  // 60km-
  ],
  sento: { km: 57.2, minutes: 60, name: "深川温泉常盤湯" },
};

const state = {
  course: null,
  cutoffs: null,
  cumDistKm: null, // course[i] の0からの累積距離(km)
  totalKm: 0,
  startMs: null,
  targetHours: null,
  curFix: null, // {lat, lon, accuracy, t}
  curKm: null,
  paceKmh: null, // 直近の実効ペース(km/h, 履歴から推定)
  map: null,
  mapLayers: {},
  meMarker: null,
};

// ---------- ユーティリティ ----------
const haversineKm = (la1, lo1, la2, lo2) => {
  const R = 6371;
  const p1 = la1 * Math.PI / 180, p2 = la2 * Math.PI / 180;
  const dp = (la2 - la1) * Math.PI / 180;
  const dl = (lo2 - lo1) * Math.PI / 180;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
};

const fmtHMS = (sec) => {
  if (sec == null || !isFinite(sec)) return "--:--:--";
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
};
const fmtHM = (sec) => {
  if (sec == null || !isFinite(sec)) return "--:--";
  const sign = sec < 0 ? "-" : "";
  const s = Math.abs(Math.round(sec));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return `${sign}${h}:${String(m).padStart(2, "0")}`;
};
const fmtClock = (date) => {
  if (!date) return "--:--";
  const d = new Date(date);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};
const fmtCountdown = (ms) => {
  if (ms <= 0) return "0日 0時間 0分";
  const totalMin = Math.floor(ms / 60000);
  const days = Math.floor(totalMin / (24 * 60));
  const hours = Math.floor((totalMin % (24 * 60)) / 60);
  const mins = totalMin % 60;
  return `${days}日 ${hours}時間 ${mins}分`;
};

// プランC: 区間別ペース + 銭湯休憩でkmまでの到達時刻を計算
function planCEtaMs(km, startMs) {
  let walkingHours = 0;
  let prev = 0;
  for (const z of PLAN_C.zones) {
    if (km <= prev) break;
    const segEnd = Math.min(km, z.maxKm);
    walkingHours += (segEnd - prev) / z.kmh;
    prev = segEnd;
  }
  const breakMs = (km > PLAN_C.sento.km) ? PLAN_C.sento.minutes * 60000 : 0;
  return startMs + walkingHours * 3600000 + breakMs;
}

const fmtClockWithDay = (date, baseDate) => {
  if (!date) return "--:--";
  const d = new Date(date);
  const b = new Date(baseDate || state.startMs || Date.now());
  const sameDay = d.toDateString() === b.toDateString();
  const prefix = sameDay ? "" : `+${Math.round((d - new Date(b.toDateString())) / 86400000)}d `;
  return `${prefix}${fmtClock(d)}`;
};

// ---------- コースへの射影 ----------
function buildCumulative() {
  const c = state.course.course;
  const cum = [0];
  for (let i = 1; i < c.length; i++) {
    cum.push(cum[i - 1] + haversineKm(c[i - 1][0], c[i - 1][1], c[i][0], c[i][1]));
  }
  state.cumDistKm = cum;
  state.totalKm = cum[cum.length - 1];
}

// 緯度経度を「平面っぽく」近似してセグメントへの射影点を求める
function projectToCourse(lat, lon) {
  const c = state.course.course;
  const cum = state.cumDistKm;
  // ローカル平面化（中心でメートル換算）
  const cosLat = Math.cos(lat * Math.PI / 180);
  const M = 111320; // 1度=約111.32km
  const px = (la, lo) => [(lo) * cosLat * M, (la) * M];
  const [qx, qy] = px(lat, lon);
  let bestKm = null, bestD2 = Infinity, bestPt = null, bestI = 0;
  for (let i = 1; i < c.length; i++) {
    const [ax, ay] = px(c[i - 1][0], c[i - 1][1]);
    const [bx, by] = px(c[i][0], c[i][1]);
    const dx = bx - ax, dy = by - ay;
    const segLen2 = dx * dx + dy * dy;
    let t = 0;
    if (segLen2 > 0) {
      t = ((qx - ax) * dx + (qy - ay) * dy) / segLen2;
      t = Math.max(0, Math.min(1, t));
    }
    const fx = ax + dx * t, fy = ay + dy * t;
    const d2 = (qx - fx) * (qx - fx) + (qy - fy) * (qy - fy);
    if (d2 < bestD2) {
      bestD2 = d2;
      const segDistKm = haversineKm(c[i - 1][0], c[i - 1][1], c[i][0], c[i][1]);
      bestKm = cum[i - 1] + segDistKm * t;
      bestI = i - 1;
      // 射影点を緯度経度に戻す
      const lat2 = (fy / M);
      const lon2 = (fx / (cosLat * M));
      bestPt = [lat2, lon2];
    }
  }
  return { km: bestKm, point: bestPt, segIndex: bestI, offsetMeters: Math.sqrt(bestD2) };
}

// ---------- ペース推定 ----------
// 履歴 [{t, km}] から直近の歩行ペース(km/h)を推定。十分データがなければ null
function estimatePaceKmh() {
  const hist = JSON.parse(localStorage.getItem(LS.history) || "[]");
  if (hist.length < 2) return null;
  const last = hist[hist.length - 1];
  // 過去30分以内のうち最も古いものを基準に（短すぎると誤差）
  const cutoff = last.t - 30 * 60 * 1000;
  let base = null;
  for (let i = hist.length - 2; i >= 0; i--) {
    if (hist[i].t < cutoff) { base = hist[i]; break; }
    base = hist[i];
  }
  if (!base) return null;
  const dt = (last.t - base.t) / 1000;
  const dkm = last.km - base.km;
  if (dt < 60) return null; // 1分未満は信頼しない
  if (dkm < 0.05) return 0; // ほぼ動いてない
  return (dkm / dt) * 3600;
}

function pushHistory(t, km) {
  const hist = JSON.parse(localStorage.getItem(LS.history) || "[]");
  // 同位置スパムを防ぐ
  if (hist.length && Math.abs(km - hist[hist.length - 1].km) < 0.005) return;
  hist.push({ t, km });
  while (hist.length > HISTORY_MAX) hist.shift();
  localStorage.setItem(LS.history, JSON.stringify(hist));
}

// ---------- 関門計算 ----------
// mode: 'race' = 実績ペース / GPSベース。'plan' = プランC基準（本番前用）
function buildCheckpointStatus(nowMs, mode) {
  const cps = state.cutoffs.checkpoints.filter((c) => c.name !== "START");
  const cur = state.curKm == null ? null : state.curKm;
  const paceKmh = state.paceKmh && state.paceKmh > 0.5 ? state.paceKmh : null;
  const goalCutoffMs = new Date(state.cutoffs.checkpoints.find((c) => c.name === "GOAL").cutoff).getTime();
  const remainingHours = (goalCutoffMs - nowMs) / 3600000;
  const remainingKm = state.totalKm - (cur || 0);
  const inferredKmh = remainingHours > 0 && remainingKm > 0 ? remainingKm / remainingHours : null;
  const usePace = paceKmh || inferredKmh || 4.5;

  // race モードかつスタート未押下&ペース履歴なしなら、ETAを出さない（--:--表示）
  const raceHasData = state.started || state.paceKmh != null;

  const etaForKm = (km) => {
    if (mode === "plan") return planCEtaMs(km, state.startMs);
    if (!raceHasData) return null;
    return nowMs + ((km - (cur || 0)) / usePace) * 3600000;
  };

  const rows = [];
  let sentoInserted = false;
  for (const cp of cps) {
    // 銭湯入店/出発の行を 57.2km < cp.km の手前で挿入（XW100コースのみ自然に該当）
    if (!sentoInserted && cp.km > PLAN_C.sento.km) {
      const sentoPassed = state.started && cur != null && cur >= PLAN_C.sento.km - 0.05;
      let arriveMs = null, departMs = null;
      if (!sentoPassed) {
        if (mode === "plan") {
          arriveMs = planCEtaMs(PLAN_C.sento.km, state.startMs);
        } else if (raceHasData) {
          arriveMs = nowMs + ((PLAN_C.sento.km - (cur || 0)) / usePace) * 3600000;
        }
        departMs = arriveMs ? arriveMs + PLAN_C.sento.minutes * 60000 : null;
      }
      const distKm = PLAN_C.sento.km - (cur || 0);
      rows.push({ kind: "sento", name: "銭湯入店", km: PLAN_C.sento.km, etaMs: arriveMs, passed: sentoPassed, distKm });
      rows.push({ kind: "sento", name: "銭湯出発", km: PLAN_C.sento.km, etaMs: departMs, passed: sentoPassed, distKm });
      sentoInserted = true;
    }

    const passed = state.started && cur != null && cur >= cp.km - 0.05;
    const distKm = cur == null ? cp.km : cp.km - cur;
    const etaMs = passed ? null : etaForKm(cp.km);
    const cutoffMs = new Date(cp.cutoff).getTime();
    const marginSec = etaMs ? (cutoffMs - etaMs) / 1000 : null;
    rows.push({
      kind: "cp",
      cp, passed, distKm, etaMs, cutoffMs, marginSec, name: cp.name, km: cp.km,
    });
  }
  return rows;
}

// ---------- 描画: ペース画面 ----------
function renderPace() {
  const now = Date.now();
  const isStarted = state.started;
  // PlanCモードはXW100コースかつ未スタートの時のみ。練習コースはPlanCを使わない
  const isPlanMode = !isStarted && IS_XW100;
  const mode = isPlanMode ? "plan" : "race";

  // 経過時間 / 本番までのカウントダウン
  const elapsedLabelEl = document.getElementById("elapsed-label");
  const elapsedEl = document.getElementById("elapsed");
  if (isStarted) {
    elapsedLabelEl.textContent = "経過時間";
    const elapsed = state.startMs ? (now - state.startMs) / 1000 : null;
    elapsedEl.textContent = elapsed != null && elapsed >= 0 ? fmtHMS(elapsed) : "--:--:--";
  } else {
    elapsedLabelEl.textContent = IS_XW100 ? "本番まで" : "スタートまで";
    elapsedEl.textContent = (state.startMs && state.startMs > now) ? fmtCountdown(state.startMs - now) : "0日 0時間 0分";
  }

  // スタート時刻表示（ペース画面トップ）
  const startEl = document.getElementById("start-display");
  if (startEl) {
    if (state.startMs) {
      const d = new Date(state.startMs);
      const pad = (n) => String(n).padStart(2, "0");
      startEl.textContent = `スタート: ${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    } else {
      startEl.textContent = "スタート: --";
    }
  }
  // 「今をスタートにする」ボタンは未押下時のみ表示（誤タップ事故防止）
  const startNowBtn = document.getElementById("set-start-now");
  if (startNowBtn) {
    startNowBtn.style.display = isStarted ? "none" : "";
  }

  // 本番前注釈はPlanCモード(XW100の本番前)のみ表示
  document.querySelectorAll(".pre-race-note").forEach((n) => { n.hidden = !isPlanMode; });

  // 各カードのラベル切替（PlanCモードのみ「プランC基準」と注記）
  document.getElementById("cp-arrival-th").textContent = isPlanMode ? "プランC" : "到着";
  document.getElementById("cp-table-label").textContent = isPlanMode ? "全関門・銭湯（プランC基準）" : (IS_XW100 ? "全関門・銭湯" : "全行程");
  document.getElementById("next-cp-label").textContent = isPlanMode ? "次の関門（プランC基準）" : (IS_XW100 ? "次の関門" : "次の地点");
  document.getElementById("next-cp-eta-label").textContent = isPlanMode ? "プランC到着" : "到着予想";
  document.getElementById("goal-eta-label").textContent = isPlanMode ? "ゴール到着予想（プランC）" : "ゴール到着予想";

  // データ未ロード時はここまで
  if (!state.course || !state.cutoffs) {
    document.getElementById("cur-detail").textContent = "コースデータ読込中...";
    return;
  }

  // 現在位置
  const curKm = state.curKm;
  document.getElementById("cur-km").textContent = curKm == null ? "--.--" : curKm.toFixed(2);
  const detail = (() => {
    if (curKm == null) return "「現在地を更新」を押すと表示されます";
    const remaining = state.totalKm - curKm;
    const acc = state.curFix?.accuracy ? ` (GPS誤差±${Math.round(state.curFix.accuracy)}m)` : "";
    const paceStr = state.paceKmh != null ? ` / ペース ${state.paceKmh.toFixed(1)} km/h` : "";
    return `ゴールまであと ${remaining.toFixed(2)} km${paceStr}${acc}`;
  })();
  document.getElementById("cur-detail").textContent = detail;

  // コース外表示
  const offEl = document.getElementById("cur-offcourse");
  if (state.curFix && state.courseOffsetM != null) {
    if (state.courseOffsetM > 100) {
      offEl.textContent = `※コースから ${(state.courseOffsetM / 1000).toFixed(2)} km 離れています（一番近いコース上の地点を表示中）`;
      offEl.style.color = "var(--warn)";
    } else {
      offEl.textContent = "";
    }
  } else {
    offEl.textContent = "";
  }

  // 関門 + 銭湯
  const rows = buildCheckpointStatus(now, mode);
  const tbody = document.getElementById("cp-tbody");
  tbody.innerHTML = "";
  let nextCp = null;
  for (const s of rows) {
    if (s.kind === "cp" && !s.passed && !nextCp) nextCp = s;
    const tr = document.createElement("tr");
    if (s.passed) tr.classList.add("passed");
    if (s.kind === "sento") tr.classList.add("sento-row");

    const arrivalCell = s.passed ? "通過" : (s.etaMs ? fmtClockWithDay(s.etaMs) : "--:--");
    const cutoffCell = s.kind === "sento" ? "—" : fmtClockWithDay(s.cutoffMs);
    const marginCell = s.kind === "sento" ? "—" :
      (s.passed ? "✓" : (s.marginSec != null ? fmtHM(s.marginSec) : "--"));
    const marginCls = s.kind === "sento" ? "" :
      (s.passed ? "" : (s.marginSec == null ? "" : (s.marginSec > 1800 ? "margin-ok" : (s.marginSec > 0 ? "margin-warn" : "margin-bad"))));

    tr.innerHTML = `
      <td>${s.name}</td>
      <td>${s.km}</td>
      <td>${arrivalCell}</td>
      <td>${cutoffCell}</td>
      <td class="${marginCls}">${marginCell}</td>
    `;
    tbody.appendChild(tr);
  }

  // 次の関門詳細
  if (nextCp) {
    document.getElementById("next-cp-name").textContent = `${nextCp.cp.name} ${nextCp.cp.km}km`;
    document.getElementById("next-cp-dist").textContent = `${nextCp.distKm.toFixed(2)} km`;
    document.getElementById("next-cp-eta").textContent = nextCp.etaMs ? fmtClockWithDay(nextCp.etaMs) : "--:--";
    document.getElementById("next-cp-cutoff").textContent = fmtClockWithDay(nextCp.cutoffMs);
    const marginEl = document.getElementById("next-cp-margin");
    marginEl.textContent = nextCp.marginSec != null ? fmtHM(nextCp.marginSec) : "--";
    marginEl.className = "sub-value mono " + (nextCp.marginSec == null ? "" : (nextCp.marginSec > 1800 ? "margin-ok" : (nextCp.marginSec > 0 ? "margin-warn" : "margin-bad")));
  } else {
    document.getElementById("next-cp-name").textContent = "全関門通過済み";
    document.getElementById("next-cp-dist").textContent = "--";
    document.getElementById("next-cp-eta").textContent = "--:--";
    document.getElementById("next-cp-cutoff").textContent = "--:--";
    document.getElementById("next-cp-margin").textContent = "--";
  }

  // ゴール予想
  const goal = rows.find((s) => s.kind === "cp" && s.cp.name === "GOAL");
  if (goal) {
    document.getElementById("goal-eta").textContent = goal.passed ? "ゴール!" : (goal.etaMs ? fmtClockWithDay(goal.etaMs) : "--:--");
    const margin = goal.marginSec;
    document.getElementById("goal-margin").textContent = goal.passed ? "" : (margin != null ? `制限まで ${fmtHM(margin)}` : "");
  }
}

// ---------- 描画: マップ画面 ----------
function initMap() {
  if (state.map) return;
  const c = state.course;
  const map = L.map("map", { zoomControl: true, attributionControl: true });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
  }).addTo(map);

  // コースライン（常時表示）
  const courseLine = L.polyline(c.course, { color: "#5dd39e", weight: 4, opacity: 0.85 }).addTo(map);

  // レイヤーグループ（フィルタ単位）
  const layers = {
    cp: L.layerGroup(),
    toilet: L.layerGroup(),
    seven: L.layerGroup(),
    fm: L.layerGroup(),
    lawson: L.layerGroup(),
    "conveni-other": L.layerGroup(),
    sento: L.layerGroup(),
    km: L.layerGroup(),
  };

  // CP + スタート/ゴール（CPレイヤー）
  for (const cp of c.checkpoints) {
    L.marker([cp.lat, cp.lon], {
      icon: L.divIcon({
        className: "",
        html: `<div style="background:#ef476f;color:#fff;font-size:11px;font-weight:700;padding:3px 6px;border-radius:4px;border:2px solid #fff;white-space:nowrap;box-shadow:0 1px 3px rgba(0,0,0,.5)">${cp.name} ${cp.km}km</div>`,
        iconSize: [60, 22],
        iconAnchor: [30, 11],
      }),
    }).bindPopup(`<b>${cp.name} (${cp.km}km)</b><br>${cp.venue}`).addTo(layers.cp);
  }
  for (const sg of [c.start, c.goal]) {
    if (!sg) continue;
    L.marker([sg.lat, sg.lon], {
      icon: L.divIcon({
        className: "",
        html: `<div style="background:#ffd166;color:#0b1410;font-size:11px;font-weight:800;padding:3px 8px;border-radius:4px;border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.5)">${sg.name}</div>`,
        iconSize: [60, 22],
        iconAnchor: [30, 11],
      }),
    }).addTo(layers.cp);
  }

  // トイレ
  for (const t of c.toilets) {
    L.circleMarker([t.lat, t.lon], { radius: 5, color: "#3da9fc", fillColor: "#3da9fc", fillOpacity: 0.85, weight: 1 })
      .bindPopup(t.name).addTo(layers.toilet);
  }

  // コンビニ
  const brandStyle = {
    "セブン-イレブン":   { col: "#ff6b00", fg: "#fff", letter: "7", layer: "seven" },
    "ファミリーマート":  { col: "#3aa647", fg: "#fff", letter: "F", layer: "fm" },
    "ローソン":          { col: "#0050a5", fg: "#fff", letter: "L", layer: "lawson" },
    "ミニストップ":      { col: "#1f8a3b", fg: "#fff", letter: "M", layer: "conveni-other" },
    "デイリーヤマザキ":  { col: "#c83b1d", fg: "#fff", letter: "D", layer: "conveni-other" },
    "ニューデイズ":      { col: "#e6411e", fg: "#fff", letter: "N", layer: "conveni-other" },
    "ポプラ":            { col: "#1d913a", fg: "#fff", letter: "P", layer: "conveni-other" },
    "セイコーマート":    { col: "#0a8a52", fg: "#fff", letter: "S", layer: "conveni-other" },
  };
  if (state.conveni && state.conveni.items) {
    for (const ci of state.conveni.items) {
      const s = brandStyle[ci.brand] || { col: "#888", fg: "#fff", letter: "?", layer: "conveni-other" };
      L.marker([ci.lat, ci.lon], {
        icon: L.divIcon({
          className: "conveni-marker",
          html: `<div style="width:18px;height:18px;border-radius:4px;background:${s.col};color:${s.fg};font:700 11px/18px -apple-system,sans-serif;text-align:center;border:1.5px solid #fff;box-shadow:0 1px 2px rgba(0,0,0,.5)">${s.letter}</div>`,
          iconSize: [18, 18],
          iconAnchor: [9, 9],
        }),
      }).bindPopup(`<b>${ci.brand}</b><br>${ci.km.toFixed(1)} km地点 (コースから ${Math.round(ci.off_m)}m)`).addTo(layers[s.layer]);
    }
  }

  // 銭湯（♨️）
  if (state.sento && state.sento.items) {
    for (const sn of state.sento.items) {
      const link = sn.website
        ? `<a href="${sn.website}" target="_blank" rel="noopener">公式サイト</a>`
        : `<a href="${sn.search}" target="_blank" rel="noopener">Google検索</a>`;
      // ビジネス情報ページに飛ばすため店名で検索（座標は使わない）
      const mapsUrl = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(sn.name)}`;
      const popup = `<b>♨️ ${sn.name}</b><br>${sn.km.toFixed(1)} km地点 (コースから ${Math.round(sn.off_m)}m)` +
        (sn.opening_hours ? `<br>営業: ${sn.opening_hours}` : "") +
        (sn.phone ? `<br>📞 ${sn.phone}` : "") +
        `<br>${link} / <a href="${mapsUrl}" target="_blank" rel="noopener">Google Mapsで開く</a>`;
      L.marker([sn.lat, sn.lon], {
        icon: L.divIcon({
          className: "sento-marker",
          html: `<div style="font-size:24px;line-height:1;text-shadow:0 0 3px #fff,0 0 6px #fff,0 0 10px rgba(0,0,0,.3);filter:drop-shadow(0 1px 1px rgba(0,0,0,.5))">♨️</div>`,
          iconSize: [26, 26],
          iconAnchor: [13, 13],
        }),
      }).bindPopup(popup).addTo(layers.sento);
    }
  }

  // 1kmマーカー
  for (const k of c.km_markers) {
    if (k.km % 5 === 0) {
      L.circleMarker([k.lat, k.lon], { radius: 4, color: "#9bb3a4", fillColor: "#9bb3a4", fillOpacity: 0.7, weight: 0 })
        .bindTooltip(`${k.km}km`, { permanent: true, direction: "right", className: "km-label" }).addTo(layers.km);
    } else {
      L.circleMarker([k.lat, k.lon], { radius: 2, color: "#9bb3a4", fillColor: "#9bb3a4", fillOpacity: 0.5, weight: 0 }).addTo(layers.km);
    }
  }

  // 全レイヤーをデフォルトでマップに乗せる
  for (const k of Object.keys(layers)) layers[k].addTo(map);

  state.map = map;
  state.mapLayers = { course: courseLine, ...layers };
  map.fitBounds(courseLine.getBounds(), { padding: [20, 20] });

  // フィルタボタン配線（一度だけ）
  setupFilterButtons();
}

function setupFilterButtons() {
  if (state._filtersBound) return;
  state._filtersBound = true;

  const allBtn = document.querySelector('.filter[data-layer="all"]');
  const layerBtns = Array.from(document.querySelectorAll('.filter[data-layer]')).filter((b) => b.dataset.layer !== "all");

  const applyVisibility = () => {
    for (const b of layerBtns) {
      const key = b.dataset.layer;
      const layer = state.mapLayers[key];
      if (!layer) continue;
      const on = b.classList.contains("on");
      if (on) layer.addTo(state.map);
      else state.map.removeLayer(layer);
    }
    // 全部onなら「全て」もon
    const allOn = layerBtns.every((b) => b.classList.contains("on"));
    allBtn.classList.toggle("on", allOn);
  };

  // 個別ボタン: クリックでそれだけON、それ以外OFF。再クリックで全ON
  for (const b of layerBtns) {
    b.addEventListener("click", () => {
      if (!state.map) return;
      const key = b.dataset.layer;
      const aloneSelected = b.classList.contains("on") && layerBtns.every((x) => (x === b) === x.classList.contains("on"));
      if (aloneSelected) {
        // すでに「これだけ選択」状態 → 全ONに戻す
        for (const x of layerBtns) x.classList.add("on");
      } else {
        // クリックしたものだけON
        for (const x of layerBtns) x.classList.toggle("on", x === b);
      }
      applyVisibility();
    });
  }

  // 「全て」ボタン: 常に全ONに戻す
  allBtn.addEventListener("click", () => {
    if (!state.map) return;
    for (const x of layerBtns) x.classList.add("on");
    applyVisibility();
  });
}

function updateMyMarker() {
  if (!state.map || !state.curFix) return;
  const ll = [state.curFix.lat, state.curFix.lon];
  if (state.meMarker) {
    state.meMarker.setLatLng(ll);
  } else {
    state.meMarker = L.circleMarker(ll, { radius: 9, color: "#ffd166", fillColor: "#ffd166", fillOpacity: 0.95, weight: 2 })
      .bindPopup("実際の現在地（GPS）").addTo(state.map);
  }
  // コースに射影した位置（コース上のどこにいる扱いか）
  if (state.projPoint) {
    if (state.projMarker) {
      state.projMarker.setLatLng(state.projPoint);
    } else {
      state.projMarker = L.circleMarker(state.projPoint, { radius: 7, color: "#5dd39e", fillColor: "#5dd39e", fillOpacity: 0.9, weight: 2 })
        .bindPopup("コース上の現在位置（射影）").addTo(state.map);
    }
    // 実位置と射影位置をつなぐ線
    if (state.projLine) {
      state.projLine.setLatLngs([ll, state.projPoint]);
    } else {
      state.projLine = L.polyline([ll, state.projPoint], { color: "#ffd166", weight: 2, opacity: 0.6, dashArray: "4 4" }).addTo(state.map);
    }
  }
}

// ---------- GPS ----------
function setGpsState(s) {
  const el = document.getElementById("gps-indicator");
  el.className = "gps " + s;
}

async function refreshGPS() {
  if (!("geolocation" in navigator)) {
    alert("このブラウザはGPSに対応していません");
    return;
  }
  setGpsState("stale");
  try {
    const pos = await new Promise((resolve, reject) => {
      navigator.geolocation.getCurrentPosition(resolve, reject, {
        enableHighAccuracy: true,
        timeout: 15000,
        maximumAge: 5000,
      });
    });
    const fix = {
      lat: pos.coords.latitude,
      lon: pos.coords.longitude,
      accuracy: pos.coords.accuracy,
      t: pos.timestamp || Date.now(),
    };
    state.curFix = fix;
    localStorage.setItem(LS.lastFix, JSON.stringify(fix));
    const proj = projectToCourse(fix.lat, fix.lon);
    state.curKm = proj.km;
    state.courseOffsetM = proj.offsetMeters;
    state.projPoint = proj.point;
    pushHistory(fix.t, proj.km);
    state.paceKmh = estimatePaceKmh();
    updateMyMarker();
    renderPace();
    setGpsState("live");
  } catch (e) {
    console.warn("GPS error:", e);
    setGpsState("off");
    alert("GPS取得に失敗: " + (e.message || e.code));
  }
}

// ---------- 設定 ----------
function loadSettings() {
  // localStorage > cutoffs.user_start > DEFAULT_START の順で初期値を決める
  const cutoffsStart = state.cutoffs && state.cutoffs.user_start;
  const startISO = localStorage.getItem(LS.startISO) || cutoffsStart || DEFAULT_START;
  const targetH = localStorage.getItem(LS.targetHours);
  state.startMs = new Date(startISO).getTime();
  state.targetHours = targetH ? parseFloat(targetH) : null;
  state.started = localStorage.getItem(LS.started) === "1";
  // <input type=datetime-local> はローカルタイム
  const d = new Date(startISO);
  const pad = (n) => String(n).padStart(2, "0");
  document.getElementById("set-start").value =
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (targetH) document.getElementById("set-target-hours").value = targetH;
  // 直近のGPS復帰
  const last = localStorage.getItem(LS.lastFix);
  if (last) {
    try {
      state.curFix = JSON.parse(last);
      const proj = projectToCourse(state.curFix.lat, state.curFix.lon);
      state.curKm = proj.km;
      state.courseOffsetM = proj.offsetMeters;
      state.projPoint = proj.point;
      state.paceKmh = estimatePaceKmh();
    } catch {}
  }
}

function saveSettings() {
  const v = document.getElementById("set-start").value;
  if (v) {
    const local = new Date(v); // ローカルタイムとして解釈
    localStorage.setItem(LS.startISO, local.toISOString());
    state.startMs = local.getTime();
  }
  const th = document.getElementById("set-target-hours").value;
  if (th) {
    localStorage.setItem(LS.targetHours, th);
    state.targetHours = parseFloat(th);
  } else {
    localStorage.removeItem(LS.targetHours);
    state.targetHours = null;
  }
  document.getElementById("save-status").textContent = "保存しました";
  setTimeout(() => { document.getElementById("save-status").textContent = ""; }, 2000);
  renderPace();
}

function resetSettings() {
  if (!confirm("スタート時刻・目標タイム・履歴を初期化します。よろしいですか？")) return;
  localStorage.removeItem(LS.startISO);
  localStorage.removeItem(LS.targetHours);
  localStorage.removeItem(LS.history);
  localStorage.removeItem(LS.lastFix);
  localStorage.removeItem(LS.started);
  loadSettings();
  state.curFix = null;
  state.curKm = null;
  state.paceKmh = null;
  if (state.meMarker) { state.map.removeLayer(state.meMarker); state.meMarker = null; }
  renderPace();
  document.getElementById("save-status").textContent = "リセットしました";
  setTimeout(() => { document.getElementById("save-status").textContent = ""; }, 2000);
}

// ---------- タブ切り替え ----------
function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "map") {
    initMap();
    setTimeout(() => state.map?.invalidateSize(), 50);
    updateMyMarker();
  }
}

// ---------- 起動 ----------
function showFatal(msg) {
  console.error(msg);
  let bar = document.getElementById("fatal-bar");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "fatal-bar";
    bar.style.cssText = "position:fixed;bottom:0;left:0;right:0;background:#ef476f;color:#fff;padding:10px 14px;font-size:13px;z-index:9999;white-space:pre-wrap;max-height:40vh;overflow:auto";
    document.body.appendChild(bar);
  }
  bar.textContent = "エラー: " + (msg && msg.stack ? msg.stack : String(msg));
}

// テストモード: 指定kmのコース上座標を補間で求める
function locateOnCourseByKm(km) {
  const cum = state.cumDistKm;
  const c = state.course.course;
  const total = cum[cum.length - 1];
  const target = Math.max(0, Math.min(km, total));
  if (target <= 0) return [c[0][0], c[0][1]];
  if (target >= total) return [c[c.length - 1][0], c[c.length - 1][1]];
  for (let i = 1; i < cum.length; i++) {
    if (cum[i] >= target) {
      const t = (target - cum[i - 1]) / (cum[i] - cum[i - 1]);
      return [c[i - 1][0] + (c[i][0] - c[i - 1][0]) * t, c[i - 1][1] + (c[i][1] - c[i - 1][1]) * t];
    }
  }
  return [c[c.length - 1][0], c[c.length - 1][1]];
}

function applyTestKm() {
  if (!state.course || !state.cumDistKm) {
    alert("コースデータ読込中。少し待ってから再度押して。");
    return;
  }
  const v = parseFloat(document.getElementById("test-km").value);
  if (!isFinite(v)) { alert("kmを入力して"); return; }
  const [lat, lon] = locateOnCourseByKm(v);
  // テスト時はlocalStorageに書かない（実セッション汚染防止）
  state.curFix = { lat, lon, accuracy: 0, t: Date.now() };
  state.curKm = v;
  state.courseOffsetM = 0;
  state.projPoint = [lat, lon];
  state.paceKmh = null;
  if (state.map) updateMyMarker();
  setGpsState("live");
  renderPace();
}

function clearTestKm() {
  state.curFix = null;
  state.curKm = null;
  state.courseOffsetM = null;
  state.projPoint = null;
  state.paceKmh = null;
  if (state.meMarker && state.map) { state.map.removeLayer(state.meMarker); state.meMarker = null; }
  if (state.projMarker && state.map) { state.map.removeLayer(state.projMarker); state.projMarker = null; }
  if (state.projLine && state.map) { state.map.removeLayer(state.projLine); state.projLine = null; }
  setGpsState("off");
  renderPace();
}

function setStartToNow() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const label = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  if (!confirm(`スタート時刻を ${label} に設定します。よろしい？`)) return;
  // 秒以下は切り捨ててローカルタイムで保存（既存の保存形式に合わせる）
  const local = new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours(), now.getMinutes());
  localStorage.setItem(LS.startISO, local.toISOString());
  localStorage.setItem(LS.started, "1");
  state.startMs = local.getTime();
  state.started = true;
  // 設定タブの input も同期
  const setStartEl = document.getElementById("set-start");
  if (setStartEl) {
    setStartEl.value = `${local.getFullYear()}-${pad(local.getMonth() + 1)}-${pad(local.getDate())}T${pad(local.getHours())}:${pad(local.getMinutes())}`;
  }
  renderPace();
}

function setupUI() {
  // 練習コース読込時はヘッダーにコース名バッジを出す
  if (!IS_XW100) {
    const banner = document.getElementById("course-banner");
    if (banner) {
      banner.textContent = COURSE_ID === "nakasendo" ? "中山道" : COURSE_ID;
      banner.hidden = false;
    }
  }
  // イベントリスナーは即座に付ける（データ読み込みの成否に関係なく動かす）
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  document.getElementById("refresh-gps").addEventListener("click", () => {
    refreshGPS().catch((e) => showFatal(e));
  });
  document.getElementById("set-start-now").addEventListener("click", () => {
    try { setStartToNow(); } catch (e) { showFatal(e); }
  });
  document.getElementById("save-settings").addEventListener("click", () => {
    try { saveSettings(); } catch (e) { showFatal(e); }
  });
  document.getElementById("reset-settings").addEventListener("click", () => {
    try { resetSettings(); } catch (e) { showFatal(e); }
  });
  if (TEST_MODE) {
    document.querySelectorAll(".test-only").forEach((e) => { e.hidden = false; });
    document.getElementById("test-banner").hidden = false;
    document.getElementById("apply-test-km").addEventListener("click", () => {
      try { applyTestKm(); } catch (e) { showFatal(e); }
    });
    document.getElementById("clear-test-km").addEventListener("click", () => {
      try { clearTestKm(); } catch (e) { showFatal(e); }
    });
    console.log("[XW100] TEST MODE enabled");
  }
  console.log("[XW100] UI handlers attached");
}

async function loadData() {
  try {
    const courseFile = IS_XW100 ? "data/course.json" : `data/course-${COURSE_ID}.json`;
    const cutoffsFile = IS_XW100 ? "data/cutoffs.json" : `data/cutoffs-${COURSE_ID}.json`;
    const conveniFile = IS_XW100 ? "data/conveni.json" : `data/conveni-${COURSE_ID}.json`;
    const [course, cutoffs, conveni, sento] = await Promise.all([
      fetch(courseFile).then((r) => { if (!r.ok) throw new Error(courseFile + " " + r.status); return r.json(); }),
      fetch(cutoffsFile).then((r) => { if (!r.ok) throw new Error(cutoffsFile + " " + r.status); return r.json(); }),
      fetch(conveniFile).then((r) => r.ok ? r.json() : { items: [] }).catch(() => ({ items: [] })),
      // 銭湯はXW100専用（夜間休憩用）
      IS_XW100 ? fetch("data/sento.json").then((r) => r.ok ? r.json() : { items: [] }).catch(() => ({ items: [] })) : Promise.resolve({ items: [] }),
    ]);
    state.course = course;
    state.cutoffs = cutoffs;
    state.conveni = conveni;
    state.sento = sento;
    buildCumulative();
    loadSettings();
    renderPace();
    setInterval(() => {
      try { renderPace(); } catch (e) { showFatal(e); }
    }, 1000);
    console.log("[XW100] data loaded:", course.total_km, "km");
  } catch (e) {
    showFatal(e);
  }
}

async function registerSW() {
  if ("serviceWorker" in navigator) {
    try {
      await navigator.serviceWorker.register("sw.js");
    } catch (e) {
      console.warn("SW register failed", e);
    }
  }
}

window.addEventListener("error", (ev) => showFatal(ev.error || ev.message));
window.addEventListener("unhandledrejection", (ev) => showFatal(ev.reason));

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => { setupUI(); loadData(); registerSW(); });
} else {
  setupUI(); loadData(); registerSW();
}
