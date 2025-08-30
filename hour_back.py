from flask import Flask, request, render_template, jsonify, make_response
import os, json, time, re, io, zipfile
from datetime import datetime, timedelta
import pandas as pd

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

app = Flask(__name__)

# ====== 기준값 / 설정 ======
top30   = 168
avg_ref = 182.7
bottom70= 194
START_DATE = os.environ.get("START_DATE", "2025-03-22")

# ====== 경로 ======
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data", "seed"))
CACHE_DIR  = os.environ.get("CACHE_DIR", os.path.join(BASE_DIR, "data", "runtime"))

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# 런타임 캐시 파일들(실제 읽고/쓰는 대상)
RUNTIME_CACHE_FILE   = os.path.join(CACHE_DIR, "runtime_cache.json")
SCHEDULE_CACHE_FILE  = os.path.join(CACHE_DIR, "schedule_index.json")

# 깃허브에 커밋해서 이미지에 포함할 씨드(두 개만 사용)
SEED_RUNTIME_FILE    = os.path.join(DATA_DIR, "runtime_cache.json")
SEED_SCHEDULE_FILE   = os.path.join(DATA_DIR, "schedule_index.json")

# ====== JSON 유틸 ======
def _safe_json_load(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def _safe_json_save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _file_info(path):
    if not os.path.exists(path):
        return {"exists": False}
    st = os.stat(path)
    return {
        "exists": True,
        "size_bytes": st.st_size,
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        "path": os.path.abspath(path),
    }

# ====== 씨드 → 런타임 캐시 초기화 (두 파일만) ======
def _warm_cache_from_seed_if_empty():
    if not os.path.exists(RUNTIME_CACHE_FILE):
        seed_runtime = _safe_json_load(SEED_RUNTIME_FILE, {})
        if isinstance(seed_runtime, dict) and seed_runtime:
            _safe_json_save(RUNTIME_CACHE_FILE, seed_runtime)
        else:
            _safe_json_save(RUNTIME_CACHE_FILE, {})  # 빈 파일이라도 생성

    if not os.path.exists(SCHEDULE_CACHE_FILE):
        seed_schedule = _safe_json_load(SEED_SCHEDULE_FILE, {})
        if isinstance(seed_schedule, dict) and seed_schedule:
            _safe_json_save(SCHEDULE_CACHE_FILE, seed_schedule)
        else:
            _safe_json_save(SCHEDULE_CACHE_FILE, {})

_warm_cache_from_seed_if_empty()

# ====== 캐시 Accessor ======
def get_runtime_cache():
    return _safe_json_load(RUNTIME_CACHE_FILE, {})

def get_schedule_cache():
    return _safe_json_load(SCHEDULE_CACHE_FILE, {})

def set_runtime_cache(key, runtime_min):
    cache = get_runtime_cache()
    cache[key] = {"runtime_min": runtime_min}
    _safe_json_save(RUNTIME_CACHE_FILE, cache)

def set_schedule_cache_for_date(date_str, games_minimal_list):
    cache = get_schedule_cache()
    cache[date_str] = games_minimal_list
    _safe_json_save(SCHEDULE_CACHE_FILE, cache)

def make_runtime_key(game_id: str, game_date: str) -> str:
    return f"{game_id}_{game_date}"

# ====== Selenium ======
def make_driver():
    options = Options()
    options.binary_location = os.environ.get("CHROME_BIN", "/usr/bin/google-chrome")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,1200")
    options.add_argument("--lang=ko-KR")
    options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    options.page_load_strategy = "eager"
    return webdriver.Chrome(options=options)

# ====== 오늘 카드 ======
def get_today_cards(driver):
    wait = WebDriverWait(driver, 15)
    today = datetime.today().strftime("%Y%m%d")
    url = f"https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={today}"
    driver.get(url)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div#contents")))
    time.sleep(0.4)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    return soup.select("li.game-cont") or soup.select("li[class*='game-cont']")

def extract_match_info_from_card(card_li):
    home_nm = card_li.get("home_nm")
    away_nm = card_li.get("away_nm")
    g_id = card_li.get("g_id")
    g_dt = card_li.get("g_dt")

    if not (home_nm and away_nm):
        home_alt = card_li.select_one(".team.home .emb img")
        away_alt = card_li.select_one(".team.away .emb img")
        if away_alt and not away_nm: away_nm = (away_alt.get("alt") or "").strip() or None
        if home_alt and not home_nm: home_nm = (home_alt.get("alt") or "").strip() or None

    if not (home_nm and away_nm):
        txt = card_li.get_text(" ", strip=True)
        m = re.search(r"([A-Za-z가-힣]+)\s*vs\s*([A-Za-z가-힣]+)", txt, re.I)
        if m:
            a, b = m.group(1), m.group(2)
            away_nm = away_nm or a
            home_nm = home_nm or b

    if not (g_id and g_dt):
        a = card_li.select_one("a[href*='GameCenter/Main.aspx'][href*='gameId='][href*='gameDate=']")
        if a and a.has_attr("href"):
            href = a["href"]
            gm = re.search(r"gameId=([A-Z0-9]+)", href)
            dm = re.search(r"gameDate=(\d{8})", href)
            if gm: g_id = g_id or gm.group(1)
            if dm: g_dt = g_dt or dm.group(1)

    return {"home": home_nm, "away": away_nm, "g_id": g_id, "g_dt": g_dt}

def find_today_matches_for_team(driver, my_team):
    cards = get_today_cards(driver)
    results = []
    for li in cards:
        info = extract_match_info_from_card(li)
        h, a = info["home"], info["away"]
        if not (h and a):
            continue
        if my_team in {h, a}:
            rival = h if a == my_team else a
            info["rival"] = rival
            results.append(info)
    return results

# ====== 날짜 스케줄(최소필드 캐시) ======
def get_games_for_date(driver, date_str):
    cache = get_schedule_cache()
    if date_str in cache:
        return cache[date_str]

    wait = WebDriverWait(driver, 15)
    url = f"https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={date_str}"
    driver.get(url)
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div#contents")))
    except Exception:
        set_schedule_cache_for_date(date_str, [])
        return []

    time.sleep(0.3)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    cards = soup.select("li.game-cont") or soup.select("li[class*='game-cont']")

    out = []
    for li in cards:
        info = extract_match_info_from_card(li)
        if all([info.get("home"), info.get("away"), info.get("g_id"), info.get("g_dt")]):
            out.append({"home": info["home"], "away": info["away"], "g_id": info["g_id"], "g_dt": info["g_dt"]})

    set_schedule_cache_for_date(date_str, out)
    return out

# ====== 리뷰 런타임 ======
def open_review_and_get_runtime(driver, game_id, game_date):
    today_str = datetime.today().strftime("%Y%m%d")
    use_cache = (game_date != today_str)
    key = make_runtime_key(game_id, game_date)

    if use_cache:
        rc = get_runtime_cache()
        hit = rc.get(key)
        if hit and isinstance(hit, dict) and "runtime_min" in hit:
            return hit["runtime_min"]

    wait = WebDriverWait(driver, 12)
    base = f"https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameId={game_id}&gameDate={game_date}"
    driver.get(base)
    try:
        tab = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), '리뷰')]")))
        tab.click()
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.record-etc")))
    except Exception:
        driver.get(base + "&section=REVIEW")
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.record-etc")))
        except Exception:
            pass

    time.sleep(0.3)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    run_time_min = None
    box = soup.select_one("div.record-etc")
    if box:
        span = box.select_one("span#txtRunTime")
        if span:
            txt = span.get_text(strip=True)
            m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", txt)
            if m:
                h, mn = int(m.group(1)), int(m.group(2))
                run_time_min = h * 60 + mn

    if use_cache and run_time_min is not None:
        set_runtime_cache(key, run_time_min)
    return run_time_min

# ====== 평균 계산 (단순: 캐시 우선, 없으면 그때만 Selenium) ======
def collect_history_avg_runtime(my_team, rival_set, start_date=START_DATE):
    # 날짜 목록: START_DATE ~ 어제
    yesterday = (datetime.today() - timedelta(days=1)).strftime("%Y%m%d")
    if "-" in start_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    else:
        start_dt = datetime.strptime(start_date, "%Y%m%d")
    dates = [dt.strftime("%Y%m%d") for dt in pd.date_range(start=start_dt, end=datetime.strptime(yesterday, "%Y%m%d"))]

    # 1) 스케줄 캐시만으로 먼저 시도
    sch = get_schedule_cache()
    targets = []
    for d in dates:
        games = sch.get(d)
        if not games:
            continue
        for g in games:
            if my_team not in {g["home"], g["away"]}:
                continue
            opp = g["home"] if g["away"] == my_team else g["away"]
            if rival_set and opp not in rival_set:
                continue
            targets.append(g)

    # 2) 스케줄이 하나도 없으면 그때만 Selenium으로 채움
    driver = None
    created = False
    if not targets:
        driver = make_driver(); created = True
        for d in dates:
            get_games_for_date(driver, d)  # 내부에서 캐시 미스만 네트워크 접근
        # 다시 타겟 구성
        sch = get_schedule_cache()
        for d in dates:
            for g in sch.get(d, []):
                if my_team not in {g["home"], g["away"]}:
                    continue
                opp = g["home"] if g["away"] == my_team else g["away"]
                if rival_set and opp not in rival_set:
                    continue
                targets.append(g)

    # 3) 런타임 캐시 먼저, 없으면 그때만 리뷰 탭 접근
    run_times = []
    # 먼저 캐시 조회
    rc = get_runtime_cache()
    today_str = datetime.today().strftime("%Y%m%d")
    missing = []
    for g in targets:
        key = make_runtime_key(g["g_id"], g["g_dt"])
        if g["g_dt"] != today_str and key in rc and "runtime_min" in rc[key]:
            run_times.append(rc[key]["runtime_min"])
        else:
            missing.append(g)

    if missing:
        if driver is None:
            driver = make_driver(); created = True
        for g in missing:
            rt = open_review_and_get_runtime(driver, g["g_id"], g["g_dt"])
            if rt is not None:
                run_times.append(rt)

    if created and driver is not None:
        try: driver.quit()
        except: pass

    if run_times:
        return round(sum(run_times) / len(run_times), 1), run_times
    return None, []

# ====== 공통 처리 ======
def compute_for_team(team_name):
    if not team_name:
        return dict(result="팀을 선택해주세요.", avg_time=None, css_class="", msg="",
                    selected_team=None, top30=top30, avg_ref=avg_ref, bottom70=bottom70)

    # 오늘 매치업 확인 (이 단계는 드라이버 필요)
    d = make_driver()
    try:
        today_matches = find_today_matches_for_team(d, team_name)
    finally:
        try: d.quit()
        except: pass

    if not today_matches:
        return dict(result=f"{team_name}의 오늘 경기를 찾지 못했습니다.",
                    avg_time=None, css_class="", msg="",
                    selected_team=team_name, top30=top30, avg_ref=avg_ref, bottom70=bottom70)

    rivals_today = {m["rival"] for m in today_matches if m.get("rival")}
    rivals_str = ", ".join(sorted(rivals_today)) if rivals_today else ""

    try:
        avg_time, _ = collect_history_avg_runtime(team_name, rivals_today)
    except Exception:
        avg_time = None

    css_class = ""; msg = ""
    if avg_time is not None:
        if avg_time < top30:        css_class, msg = "fast", "빠르게 끝나는 경기입니다"
        elif avg_time < avg_ref:    css_class, msg = "normal", "일반적인 경기 소요 시간입니다"
        elif avg_time < bottom70:   css_class, msg = "bit-long", "조금 긴 편이에요"
        else:                       css_class, msg = "long", "시간 오래 걸리는 매치업입니다"
        result = f"오늘 {team_name}의 상대팀은 {rivals_str}입니다.<br>과거 {team_name} vs {rivals_str} 평균 경기시간: {avg_time}분"
    else:
        result = f"오늘 {team_name}의 상대팀은 {rivals_str}입니다.<br>과거 경기 데이터가 없습니다."

    return dict(result=result, avg_time=avg_time, css_class=css_class, msg=msg,
                selected_team=team_name, top30=top30, avg_ref=avg_ref, bottom70=bottom70)

# ====== 라우트 ======
@app.route("/", methods=["GET","POST"])
@app.route("/hour", methods=["GET","POST"])
def hour_index():
    try:
        team = (request.args.get("myteam") or request.form.get("myteam") or "").strip()
        ctx = compute_for_team(team) if team else dict(
            result=None, avg_time=None, css_class="", msg="",
            selected_team=None, top30=top30, avg_ref=avg_ref, bottom70=bottom70
        )
        return render_template("hour.html", **ctx)
    except Exception as e:
        return f"오류가 발생했습니다: {type(e).__name__}: {str(e)}", 200

# ====== 헬스/캐시 유틸 ======
@app.route("/healthz")
def healthz():
    return "ok", 200

@app.route("/cache/status")
def cache_status():
    return jsonify({
        "BASE_DIR": os.path.abspath(BASE_DIR),
        "DATA_DIR": os.path.abspath(DATA_DIR),
        "CACHE_DIR": os.path.abspath(CACHE_DIR),
        "runtime_cache": _file_info(RUNTIME_CACHE_FILE),
        "schedule_cache": _file_info(SCHEDULE_CACHE_FILE),
        "seed_runtime": _file_info(SEED_RUNTIME_FILE),
        "seed_schedule": _file_info(SEED_SCHEDULE_FILE),
    })

@app.route("/cache/clear", methods=["POST"])
def cache_clear():
    deleted = []
    for p in [RUNTIME_CACHE_FILE, SCHEDULE_CACHE_FILE]:
        if os.path.exists(p):
            try:
                os.remove(p); deleted.append(os.path.basename(p))
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
    _warm_cache_from_seed_if_empty()
    return jsonify({"ok": True, "deleted": deleted})

# ====== 씨드 Export (선택) ======
@app.route("/cache/export")
def cache_export():
    runtime = _safe_json_load(RUNTIME_CACHE_FILE, {})
    schedule= _safe_json_load(SCHEDULE_CACHE_FILE, {})
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("runtime_cache.json", json.dumps(runtime, ensure_ascii=False, indent=2))
        z.writestr("schedule_index.json", json.dumps(schedule, ensure_ascii=False, indent=2))
    mem.seek(0)
    resp = make_response(mem.read())
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = "attachment; filename=hour_cache_seed.zip"
    return resp

if __name__ == "__main__":
    app.run(debug=True, port=5002, use_reloader=False)
