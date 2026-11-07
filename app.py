"""myAISky - AI Weather & Agent Monitoring Dashboard (FastAPI mini backend).

실행:
    uvicorn app:app --reload --host 0.0.0.0 --port 8000

환경 변수:
    KMA_SERVICE_KEY  기상청(공공데이터포털) 발급 서비스 키 (필수, 요청 serviceKey로 사용).
                     없거나 호출 실패 시 DEMO 데이터로 대체.
    KMA_NX / KMA_NY  기상청 격자 좌표 (기본: nx=55, ny=127).
"""
from __future__ import annotations

import math
import os
import random
import sqlite3
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
IMAGE_DIR = BASE_DIR / "images"
REPORT_DIR = BASE_DIR / "reports"
IMAGE_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)

KST = timezone(timedelta(hours=9), name="KST")  # 한국은 DST가 없어 고정 오프셋 사용 (Windows tzdata 불필요)

# 기상청 초단기실황 조회 API (공공데이터포털)
KMA_BASE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
KMA_URL = f"{KMA_BASE_URL}/getUltraSrtNcst"
KMA_SERVICE_KEY = os.getenv("KMA_SERVICE_KEY", "")
KMA_NX = int(os.getenv("KMA_NX", "55"))   # 서울 격자 X
KMA_NY = int(os.getenv("KMA_NY", "127"))  # 서울 격자 Y
CACHE_TTL_SEC = 600  # 기상 데이터 캐시 (10분)
SOURCE_NAME = "Korea Meteorological Administration (KMA)"  # 대시보드·로그에 표시되는 출처

PTY_LABELS = {
    0: "강수 없음",
    1: "비",
    2: "비/눈",
    3: "눈",
    5: "빗방울",
    6: "빗방울/눈날림",
    7: "눈날림",
}

app = FastAPI(title="myAISky", version="0.1.0")
app.mount("/images", StaticFiles(directory=IMAGE_DIR), name="images")

# 메모리 상태 (1교시: DB 없이 프로세스 메모리에 보관)
_cache: dict = {"data": None, "ts": 0.0}
history: deque[dict] = deque(maxlen=24)
agent_logs: deque[dict] = deque(maxlen=100)


def add_log(level: str, message: str) -> None:
    agent_logs.append({
        "time": datetime.now(KST).strftime("%H:%M:%S"),
        "level": level,
        "message": message,
    })


# ---------------------------------------------------------------------------
# SQLite 적재 (weather_logs.db → weather_logs 테이블)
# ---------------------------------------------------------------------------
DB_PATH = BASE_DIR / "weather_logs.db"


def init_db() -> None:
    """weather_logs 테이블이 없으면 생성한다."""
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                temperature REAL NOT NULL,
                condition   TEXT NOT NULL
            )
            """
        )


def save_weather_log(data: dict) -> None:
    """실측 기온·날씨 상태·시각을 weather_logs 테이블에 한 행 INSERT 한다."""
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            "INSERT INTO weather_logs (timestamp, temperature, condition) VALUES (?, ?, ?)",
            (data["timestamp"], data["temperature"], data["status"]),
        )


# ---------------------------------------------------------------------------
# 기상 데이터
# ---------------------------------------------------------------------------
def fetch_kma_current_weather() -> dict:
    """기상청 초단기실황 API로 현재 기온/습도/풍속/강수 상태를 가져온다."""
    if not KMA_SERVICE_KEY:
        raise RuntimeError("KMA_SERVICE_KEY 환경 변수가 설정되지 않았습니다.")

    # 초단기실황은 매시 정각 기준이며 발표 약 40분 뒤부터 조회 가능 → 그 전이면 한 시간 전 값 사용
    now = datetime.now(KST)
    base = now if now.minute >= 45 else now - timedelta(hours=1)

    params = {
        # 발급받은 키가 이미 URL 인코딩된 형태라면 requests가 이중 인코딩하므로 디코딩 키를 쓰세요.
        "serviceKey": KMA_SERVICE_KEY,
        "pageNo": 1,
        "numOfRows": 10,
        "dataType": "JSON",
        "base_date": base.strftime("%Y%m%d"),
        "base_time": base.strftime("%H00"),
        "nx": KMA_NX,
        "ny": KMA_NY,
    }
    resp = requests.get(KMA_URL, params=params, timeout=8)
    resp.raise_for_status()
    body = resp.json()["response"]
    if body["header"]["resultCode"] != "00":
        raise RuntimeError(f"기상청 API 오류: {body['header']['resultMsg']}")

    items = body["body"]["items"]["item"]
    values = {item["category"]: item["obsrValue"] for item in items}
    pty = int(float(values.get("PTY", 0)))

    return {
        "temperature": float(values["T1H"]),        # 기온 (°C)
        "humidity": float(values["REH"]),           # 습도 (%)
        "wind_speed": float(values["WSD"]),         # 풍속 (m/s)
        "precipitation_type": PTY_LABELS.get(pty, "알 수 없음"),
        "status": PTY_LABELS.get(pty, "알 수 없음"),
        "observed_at": base.strftime("%Y-%m-%d %H:%M KST"),
        "source": SOURCE_NAME,
        "is_demo": False,
    }


def demo_weather() -> dict:
    """API 키가 없거나 호출 실패 시 쓰는 시뮬레이션 데이터."""
    now = datetime.now(KST)
    temp = 18 + 6 * math.sin((now.hour - 9) / 24 * 2 * math.pi) + random.uniform(-0.8, 0.8)
    return {
        "temperature": round(temp, 1),
        "humidity": float(random.randint(40, 70)),
        "wind_speed": round(random.uniform(0.5, 6.0), 1),
        "precipitation_type": PTY_LABELS[0],
        "status": PTY_LABELS[0],
        "observed_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "source": "DEMO",
        "is_demo": True,
    }


def get_weather(force: bool = False) -> dict:
    """캐시를 우선 사용하고, 만료되었거나 force=True이면 새로 가져온다."""
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < CACHE_TTL_SEC:
        return _cache["data"]

    try:
        data = fetch_kma_current_weather()
    except Exception as exc:  # 네트워크/키/응답 오류 → 대시보드는 계속 동작
        add_log("WARN", f"실데이터 사용 불가, DEMO 데이터로 대체 ({exc})")
        data = demo_weather()

    data["timestamp"] = datetime.now(KST).isoformat(timespec="seconds")
    if not data["is_demo"]:  # DEMO(시뮬레이션) 값은 실측 로그에 적재하지 않는다
        try:
            save_weather_log(data)
        except sqlite3.Error as exc:
            add_log("WARN", f"DB 저장 실패 ({exc})")

    _cache.update(data=data, ts=now)
    history.append({
        "time": datetime.now(KST).strftime("%H:%M"),
        "temperature": data["temperature"],
    })
    add_log("SYNC", f"동기화 완료 · {data['temperature']}°C · {data['status']} · {data['source']}")
    return data


init_db()
add_log("BOOT", "myAISky 백엔드 시작")
add_log("AGENT", "모니터링 에이전트 대기 중")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class ReportRequest(BaseModel):
    title: str = "Daily Weather Report"
    include_history: bool = True


@app.get("/api/weather")
def api_weather() -> dict:
    """현재 기상 데이터 (JSON)."""
    return get_weather()


@app.get("/api/history")
def api_history() -> list[dict]:
    """최근 온도 추이 (차트용)."""
    return list(history)


@app.get("/api/logs")
def api_logs() -> list[dict]:
    """AI 에이전트 실시간 로그."""
    return list(agent_logs)


@app.post("/api/sync")
def api_sync() -> dict:
    """수동 제어: 기상 데이터 강제 동기화."""
    return get_weather(force=True)


@app.post("/api/report")
def api_report(req: ReportRequest = ReportRequest()) -> dict:
    """수동 제어: 현재 상태를 Markdown 리포트로 저장."""
    current = get_weather()
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# {req.title}",
        f"생성 시각: {now_str}",
        "",
        "## 현재 기상",
        f"- 기온: {current['temperature']}°C",
        f"- 습도: {current['humidity']}%",
        f"- 풍속: {current['wind_speed']} m/s",
        f"- 상태: {current['status']}",
        f"- 데이터 출처: {current['source']} ({current['observed_at']})",
    ]
    if req.include_history:
        lines += ["", "## 온도 추이"]
        lines += [f"- {p['time']}  {p['temperature']}°C" for p in history] or ["- 데이터 없음"]

    report = "\n".join(lines) + "\n"
    filename = f"report_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.md"
    (REPORT_DIR / filename).write_text(report, encoding="utf-8")
    add_log("REPORT", f"리포트 생성 완료: {filename}")
    return {"file": filename, "report": report}


# ---------------------------------------------------------------------------
# 대시보드 페이지
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>myAISky · Dashboard</title>
<style>
  :root {
    --cream: #F8F5EE;
    --ink: #2B2B2B;
    --muted: #7A7468;
    --accent: #C8924A;
    --sage: #5E8C7A;
    --charcoal: #1F2326;
    --line: #E6DFD0;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    min-height: 100vh;
    font-family: "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo", sans-serif;
    color: var(--ink);
    background-color: #9fb8c8;
    background-image: url('/images/sky.jpg');
    background-size: cover;
    background-position: center;
    background-attachment: fixed;
    background-repeat: no-repeat;
  }
  .container {
    max-width: 1200px;
    margin: 32px auto;
    padding: 32px;
    background: rgba(248, 245, 238, 0.94);
    border-radius: 24px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
  }
  header { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
  h1 { margin: 0; font-size: 32px; letter-spacing: -0.5px; }
  .subtitle { margin: 4px 0 0; color: var(--muted); font-size: 15px; }
  .badge {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 8px 14px; border-radius: 999px;
    background: #E9F3EC; color: #2F6B4F; font-weight: 600; font-size: 14px;
    white-space: nowrap;
  }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 28px 0 20px; }
  .card {
    background: rgba(255, 255, 255, 0.7);
    border: 1px solid var(--line);
    border-radius: 18px;
    padding: 20px;
  }
  .stat .label { color: var(--muted); font-size: 13px; }
  .stat .value { font-size: 28px; font-weight: 700; margin-top: 6px; }
  .grid-main { display: grid; grid-template-columns: 1.4fr 1fr; gap: 20px; }
  .card h2 { margin: 0 0 12px; font-size: 18px; }
  #analysis { color: var(--muted); line-height: 1.6; margin: 0 0 12px; }
  #trend { width: 100%; height: 200px; display: block; background: #FFFDF8; border-radius: 12px; }
  #trend-empty { color: var(--muted); font-size: 14px; text-align: center; padding: 60px 0; }
  .logs {
    background: var(--charcoal); color: #D9D4C7;
    border-radius: 18px; padding: 16px;
    font-family: Consolas, "D2Coding", monospace; font-size: 13px;
    height: 300px; overflow-y: auto;
  }
  .log-row { padding: 3px 0; border-bottom: 1px dashed #2f3538; word-break: break-all; }
  .lv-sync { color: #8FD1B0; }
  .lv-warn { color: #F2C27B; }
  .lv-report { color: #A9C8F5; }
  .lv-boot, .lv-agent { color: #BFB8A8; }
  .control { margin-top: 20px; }
  .control-row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
  button {
    font: inherit; font-weight: 600; cursor: pointer;
    padding: 12px 20px; border-radius: 12px; border: none;
    background: var(--accent); color: #fff;
    transition: transform 0.1s, opacity 0.2s;
  }
  button.secondary { background: var(--sage); }
  button:hover { opacity: 0.9; }
  button:active { transform: scale(0.98); }
  button:disabled { opacity: 0.5; cursor: wait; }
  #msg { color: var(--muted); font-size: 14px; }
  #report {
    margin: 14px 0 0; background: #FFFDF8; border: 1px solid var(--line);
    border-radius: 12px; padding: 14px; white-space: pre-wrap; font-size: 13px;
    display: none; max-height: 220px; overflow: auto;
  }
  footer { margin-top: 24px; color: var(--muted); font-size: 12px; text-align: center; }

  @media (max-width: 860px) {
    .container { margin: 16px; padding: 20px; border-radius: 20px; }
    .stats { grid-template-columns: repeat(2, 1fr); }
    .grid-main { grid-template-columns: 1fr; }
    h1 { font-size: 26px; }
  }
  @media (max-width: 480px) {
    .stats { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>myAISky</h1>
      <p class="subtitle">AI Weather &amp; Agent Monitoring Dashboard</p>
    </div>
    <span class="badge">🟢 24/7 Sandbox Active</span>
  </header>

  <section class="stats">
    <div class="card stat"><div class="label">현재 기온</div><div class="value" id="temp">--</div></div>
    <div class="card stat"><div class="label">날씨 상태</div><div class="value"><span id="icon" aria-hidden="true"></span> <span id="status">--</span></div></div>
    <div class="card stat"><div class="label">습도</div><div class="value" id="humidity">--</div></div>
    <div class="card stat"><div class="label">풍속</div><div class="value" id="wind">--</div></div>
  </section>

  <section class="grid-main">
    <div class="card">
      <h2>실시간 기상 분석 &amp; 온도 트렌드</h2>
      <p id="analysis">데이터를 불러오는 중…</p>
      <svg id="trend" viewBox="0 0 600 200" preserveAspectRatio="none"></svg>
      <div id="trend-empty" hidden>데이터 수집 중… 동기화를 두 번 이상 하면 추이가 표시됩니다.</div>
      <p id="source" style="color:var(--muted);font-size:13px;margin:10px 0 0;"></p>
    </div>

    <div class="card" style="padding:16px;">
      <h2 style="color:var(--ink);margin:4px 8px 12px;">AI 에이전트 실시간 로그</h2>
      <div class="logs" id="logs"></div>
    </div>
  </section>

  <section class="card control">
    <h2>Manual Control Panel</h2>
    <div class="control-row">
      <button id="btn-sync">데이터 동기화</button>
      <button id="btn-report" class="secondary">리포트 생성</button>
      <span id="msg"></span>
    </div>
    <pre id="report"></pre>
  </section>

  <footer>myAISky · FastAPI + Vanilla JS · 30초마다 자동 갱신</footer>
</div>

<script>
  const $ = (id) => document.getElementById(id);

  async function api(path, method = "GET") {
    const res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: method === "POST" ? "{}" : undefined,
    });
    if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
    return res.json();
  }

  function setMsg(text, isError = false) {
    const el = $("msg");
    el.textContent = text;
    el.style.color = isError ? "#B5533C" : "var(--muted)";
  }

  function iconFor(status) {
    if (status.includes("뇌우")) return "⛈️";
    if (status.includes("눈")) return "❄️";
    if (status.includes("비") || status.includes("소나기")) return "🌧️";
    if (status.includes("안개")) return "🌫️";
    if (status.includes("흐림")) return "☁️";
    if (status.includes("구름")) return "⛅";
    if (status.includes("맑음")) return "☀️";
    if (status.includes("강수 없음")) return "🌤️";
    return "🌡️";
  }

  function renderWeather(w) {
    $("temp").textContent = `${w.temperature.toFixed(1)}°C`;
    $("icon").textContent = iconFor(w.status);
    $("status").textContent = w.status;
    $("humidity").textContent = `${w.humidity}%`;
    $("wind").textContent = `${w.wind_speed} m/s`;
    const comfort = w.temperature >= 28 ? "더운 편이니 수분 섭취를 챙기세요."
      : w.temperature <= 5 ? "춥습니다. 따뜻하게 입으세요."
      : "활동하기 무난한 날씨입니다.";
    $("analysis").textContent = `현재 ${w.temperature.toFixed(1)}°C, 습도 ${w.humidity}%, 상태: ${w.status}. ${comfort}`;
    $("source").textContent = w.is_demo
      ? "DEMO 데이터 (기상청 API 키 미설정 또는 호출 실패)"
      : `출처: ${w.source} · 관측 ${w.observed_at}`;
  }

  function renderTrend(points) {
    const svg = $("trend");
    if (points.length < 2) {
      svg.innerHTML = "";
      $("trend-empty").hidden = false;
      return;
    }
    $("trend-empty").hidden = true;
    const W = 600, H = 200, P = 28;
    const temps = points.map((p) => p.temperature);
    const min = Math.min(...temps) - 1;
    const max = Math.max(...temps) + 1;
    const x = (i) => P + (i * (W - 2 * P)) / (points.length - 1);
    const y = (t) => H - P - ((t - min) / (max - min)) * (H - 2 * P);
    const line = points.map((p, i) => `${x(i).toFixed(1)},${y(p.temperature).toFixed(1)}`).join(" ");
    const dots = points.map((p, i) =>
      `<circle cx="${x(i).toFixed(1)}" cy="${y(p.temperature).toFixed(1)}" r="4" fill="#5E8C7A">` +
      `<title>${p.time} · ${p.temperature}°C</title></circle>`
    ).join("");
    svg.innerHTML = `<polyline fill="none" stroke="#C8924A" stroke-width="3" points="${line}"/>${dots}`;
  }

  function renderLogs(logs) {
    const box = $("logs");
    box.textContent = "";
    for (const l of logs.slice().reverse()) {
      const row = document.createElement("div");
      row.className = `log-row lv-${l.level.toLowerCase()}`;
      row.textContent = `[${l.time}] ${l.level.padEnd(6)} ${l.message}`;
      box.appendChild(row);
    }
  }

  async function refreshAll() {
    try {
      const [w, h, l] = await Promise.all([api("/api/weather"), api("/api/history"), api("/api/logs")]);
      renderWeather(w);
      renderTrend(h);
      renderLogs(l);
      setMsg(`마지막 갱신: ${new Date().toLocaleTimeString("ko-KR")}`);
    } catch (e) {
      setMsg(`⚠ ${e.message}`, true);
    }
  }

  async function withButton(btn, fn) {
    btn.disabled = true;
    try { await fn(); } finally { btn.disabled = false; }
  }

  $("btn-sync").addEventListener("click", (e) => withButton(e.currentTarget, async () => {
    setMsg("데이터 동기화 중…");
    try {
      await api("/api/sync", "POST");
      await refreshAll();
    } catch (err) {
      setMsg(`⚠ ${err.message}`, true);
    }
  }));

  $("btn-report").addEventListener("click", (e) => withButton(e.currentTarget, async () => {
    setMsg("리포트 생성 중…");
    try {
      const r = await api("/api/report", "POST");
      const box = $("report");
      box.textContent = r.report;
      box.style.display = "block";
      await refreshAll();
      setMsg(`리포트 저장 완료: ${r.file}`);
    } catch (err) {
      setMsg(`⚠ ${err.message}`, true);
    }
  }));

  refreshAll();
  setInterval(refreshAll, 30000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    """메인 대시보드 페이지."""
    return DASHBOARD_HTML
