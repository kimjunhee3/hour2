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

# 평균 집계 시작일(문자열: YYYY-MM-DD 또는 YYYYMMDD)
START_DATE = os.environ.get("START_DATE", "2025-03-22")

# 씨드가 비어있을 때 최초 보충은 최근 N일만(너무 멀리 과거까지 한 번에 긁지 않도록)
HISTORY_DAYS  = int(os.environ.get("HISTORY_DAYS", "45"))

# 한 요청에서 리뷰 탭을 최대 몇 경기까지 열지(안전장치)
MAX_REVIEW_PER_REQUEST = int(os.environ.get("MAX_REVIEW_PER_REQUEST", "60"))

# ✅ 캐시만 사용 스위치(기본 ON: 절대 크롤링하지 않음)
USE_CACHE_ONLY = os.environ.get("USE_CACHE_ONLY", "1") == "1"

# ====== 팀 별칭 → 정규화 매핑 ======
def _norm_key(s: str) -> str:
    return re.sub(r'[\s\-_\/]+', '', (s or '').strip().lower())

_ALIAS = {
    'SSG': ['SSG','SSG랜더스','SSG 랜더스','랜더스','ssg landers','landers','sk','sk와이번스','sk 와이번스','와이번스'],
    'KIA': ['KIA','kia','기아','KIA타이거즈','KIA 타이거즈','타이거즈'],
    '한화': ['한화','한화이글스','한화 이글스','이글스','hanwha','hanhwa'],
    '롯데': ['롯데','롯데자이언츠','롯데 자이언츠','자이언츠','lotte'],
    '두산': ['두산','두산베어스','두산 베어스','베어스','doosan'],
    'LG'  : ['LG','lg','엘지','LG트윈스','LG 트윈스','트윈스'],
    '삼성': ['삼성','삼성라이온즈','삼성 라이온즈','라이온즈','samsung'],
    'KT'  : ['KT','kt','KT위즈','KT 위즈','위즈'],
    'NC'  : ['NC','nc','NC다이노스','NC 다이노스','다이노스'],
    '키움': ['키움','키움히어로즈','키움 히어로즈','히어로즈','넥센','넥센히어로즈','넥센 히어로즈','heroes','kiwoom'],
}
ALIAS_TO_CANON = { _norm_key(n): canon for canon, names in _ALIAS.items() for n in names }

def canon_team(s: str) -> str | None:
    if not s: return None
    return ALIAS_TO_CANON.get(_norm_key(s), s.strip())

# ====== 경로 ======
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data", "seed"))
CACHE_DIR  = os.environ.get("CACHE_DIR", os.path.join(BASE_DIR, "data", "runtime"))
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# 런타임 캐시 파일(실제 읽고/쓰기)
RUNTIME_CACHE_FILE   = os.path.join(CACHE_DIR, "runtime_cache.json")
SCHEDULE_CACHE_FILE  = os.path.join(CACHE_DIR, "schedule_index.json")

# 씨드 후보(둘 다 지원; 스크린샷처럼 .json만 있어도 OK)
SEED_RUNTIME_CANDIDATES  = [
    os.path.join(DATA_DIR, "runtime_cache.json"),
    os.path.join(DATA_DIR, "runtime_cache.seed.json"),
]
SEED_SCHEDULE_CANDIDATES = [
    os.path.join(DATA_DIR, "schedule_index.json"),
    os.path.join(DATA_DIR, "schedule_index.seed.json"),
]

# ====== JSON 유틸 ======
def _safe_json_load(path, default):
    if path and os.path.exists(path):
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

def _first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None

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

# ====== 날짜 유틸 ======
def _asof_or_today(asof: str | None = None) -> str:
    """YYYYMMDD 문자열(asof)이 오면 그걸, 없으면 오늘 날짜 반환"""
    if asof:
        x = asof.strip().replace("-", "")
        datetime.strptime(x, "%Y%m%d")
        return x
    return datetime.today().strftime("%Y%m%d")

# ====== 메모리 캐시(부팅 시 1회 로드) ======
RUNTIME_MEM = None
SCHEDULE_MEM = None

def _warm_cache_from_seed_if_empty():
    """런타임 파일이 있으면 우선 사용, 없으면 seed에서 로드 → 파일로 써두고 메모리에 유지"""
    global RUNTIME_MEM, SCHEDULE_MEM

    # 런타임(실측 시간 캐시)
    RUNTIME_MEM = _safe_json_load(RUNTIME_CACHE_FILE, None)
    # ✅ 빈 dict도 씨드로 대체
    if not isinstance(RUNTIME_MEM, dict) or not RUNTIME_MEM:
        seedp = _first_existing(SEED_RUNTIME_CANDIDATES)
        RUNTIME_MEM = _safe_json_load(seedp, {})
        _safe_json_save(RUNTIME_CACHE_FILE, RUNTIME_MEM)

    # 스케줄(일자→경기 목록)
    SCHEDULE_MEM = _safe_json_load(SCHEDULE_CACHE_FILE, None)
    # ✅ 빈 dict도 씨드로 대체
    if not isinstance(SCHEDULE_MEM, dict) or not SCHEDULE_MEM:
        seedp = _first_existing(SEED_SCHEDULE_CANDIDATES)
        SCHEDULE_MEM = _safe_json_load(seedp, {})
        _safe_json_save(SCHEDULE_CACHE_FILE, SCHEDULE_MEM)

_warm_cache_from_seed_if_empty()

# === 메모리 캐시에 접근 ===
def get_runtime_cache():   return RUNTIME_MEM
def get_schedule_cache():  return SCHEDULE_MEM

def set_runtime_cache(key, runtime_min):
    RUNTIME_MEM[key] = {"runtime_min": runtime_min}
    _safe_json_save(RUNTIME_CACHE_FILE, RUNTIME_MEM)

def set_schedule_cache_for_date(date_str, games_minimal_list):
    SCHEDULE_MEM[date_str] = games_minimal_list
    _safe_json_save(SCHEDULE_CACHE_FILE, SCHEDULE_MEM)

def make_runtime_key(game_id: str, game_date: str) -> str:
    return f"{game_id}_{game_date}"

# ====== Selenium (캐시 전용 모드에선 호출되지 않음) ======
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

# ====== 날짜 스케줄(캐시→미스만 보충) ======
def _extract_match_info_from_card(li):
    home_nm = li.get("home_nm"); away_nm = li.get("away_nm")
    g_id = li.get("g_id"); g_dt = li.get("g_dt")

    if not (home_nm and away_nm):
        home_alt = li.select_one(".team.home .emb img")
        away_alt = li.select_one(".team.away .emb img")
        if away_alt and not away_nm: away_nm = (away_alt.get("alt") or "").strip() or None
        if home_alt and not home_nm: home_nm = (home_alt.get("alt") or "").strip() or None

    if not (g_id and g_dt):
        a = li.select_one("a[href*='GameCenter/Main.aspx'][href*='gameId='][href*='gameDate=']")
        if a and a.has_attr("href"):
            href = a["href"]
            gm = re.search(r"gameId=([A-Z0-9]+)", href)
            dm = re.search(r"gameDate=(\d{8})", href)
            if gm: g_id = g_id or gm.group(1)
            if dm: g_dt = g_dt or dm.group(1)

    return {"home": home_nm, "away": away_nm, "g_id": g_id, "g_dt": g_dt}

def get_games_for_date(driver, date_str):
    # 캐시에 있으면 바로 반환
    if date_str in SCHEDULE_MEM:
        return SCHEDULE_MEM[date_str]

    # ✅ 캐시 전용 모드면 절대 크롤링 안 하고, 빈 리스트로 기록/반환
    if USE_CACHE_ONLY:
        set_schedule_cache_for_date(date_str, [])
        return []

    wait = WebDriverWait(driver, 8)
    url = f"https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={date_str}"
    driver.get(url)
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div#contents")))
    except Exception:
        set_schedule_cache_for_date(date_str, [])
        return []

    time.sleep(0.2)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    cards = soup.select("li.game-cont") or soup.select("li[class*='game-cont']")

    out = []
    for li in cards:
        info = _extract_match_info_from_card(li)
        if all([info.get("home"), info.get("away"), info.get("g_id"), info.get("g_dt")]):
            out.append(info)

    set_schedule_cache_for_date(date_str, out)
    return out

def ensure_schedule_for_dates(dates):
    # ✅ 캐시 전용 모드면 스킵
    if USE_CACHE_ONLY:
        return
    miss = [d for d in dates if d not in SCHEDULE_MEM]
    if not miss:
        return
    d = make_driver()
    try:
        for dt in miss:
            get_games_for_date(d, dt)
    finally:
        try: d.quit()
        except: pass

# ====== 오늘(또는 as-of) 매치업: 캐시에서만 조회 ======
def find_today_matches_for_team_from_cache(my_team, date_str: str | None = None):
    my_can = canon_team(my_team)
    today = _asof_or_today(date_str)
    games = SCHEDULE_MEM.get(today, [])
    results = []
    for g in games:
        h_can, a_can = canon_team(g["home"]), canon_team(g["away"])
        if my_can in {h_can, a_can}:
            rival = h_can if a_can == my_can else a_can
            info = dict(g); info["rival"] = rival
            results.append(info)
    return results

# ====== 리뷰 런타임 ======
def open_review_and_get_runtime(driver, game_id, game_date):
    today_str = datetime.today().strftime("%Y%m%d")
    key = make_runtime_key(game_id, game_date)

    # 오늘 경기가 아니면 캐시 우선
    if game_date != today_str:
        hit = RUNTIME_MEM.get(key)
        if hit and "runtime_min" in hit:
            return hit["runtime_min"]

    # ✅ 캐시 전용 모드면 크롤링 금지
    if USE_CACHE_ONLY:
        return None

    wait = WebDriverWait(driver, 8)
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

    time.sleep(0.2)
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

    if (game_date != today_str) and (run_time_min is not None):
        set_runtime_cache(key, run_time_min)
    return run_time_min

# ====== 날짜 리스트 유틸 ======
def _daterange_list(start_date: str, end_date: str):
    if "-" in start_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    else:
        start_dt = datetime.strptime(start_date, "%Y%m%d")
    end_dt = datetime.strptime(end_date, "%Y%m%d")
    return [dt.strftime("%Y%m%d") for dt in pd.date_range(start=start_dt, end=end_dt)]

def _last_n_days_list(n: int, end_date_yyyymmdd: str | None = None):
    end_str = end_date_yyyymmdd or (datetime.today() - timedelta(days=1)).strftime("%Y%m%d")
    end_dt = datetime.strptime(end_str, "%Y%m%d")
    start_dt = end_dt - timedelta(days=n-1)
    return [dt.strftime("%Y%m%d") for dt in pd.date_range(start=start_dt, end=end_dt)]

# ====== 평균 계산 (캐시 우선, 부족분은 건너뛰기) ======
def collect_history_avg_runtime(my_team, rival_set, start_date=START_DATE, asof: str | None = None):
    my_can = canon_team(my_team)
    ref = _asof_or_today(asof)
    # as-of의 전날까지 평균을 계산
    ref_yesterday_dt = datetime.strptime(ref, "%Y%m%d") - timedelta(days=1)
    yesterday = ref_yesterday_dt.strftime("%Y%m%d")

    # 날짜 풀 만들기 (as-of에 맞춰)
    if SCHEDULE_MEM:
        date_pool = set(_daterange_list(start_date, yesterday))
        dates = sorted(list(date_pool.intersection(set(SCHEDULE_MEM.keys()))))
        if not dates:
            dates = _last_n_days_list(HISTORY_DAYS, yesterday)
            if not USE_CACHE_ONLY:
                ensure_schedule_for_dates(dates)
    else:
        dates = _last_n_days_list(HISTORY_DAYS, yesterday)
        if not USE_CACHE_ONLY:
            ensure_schedule_for_dates(dates)

    # 1) 대상 경기 수집
    targets = []
    rival_norm_set = {canon_team(x) for x in (rival_set or [])} if rival_set else None
    for d in dates:
        for g in SCHEDULE_MEM.get(d, []):
            h_can, a_can = canon_team(g["home"]), canon_team(g["away"])
            if my_can not in {h_can, a_can}:
                continue
            opp_can = h_can if a_can == my_can else a_can
            if rival_norm_set and opp_can not in rival_norm_set:
                continue
            targets.append(g)

    # 2) 런타임 캐시 우선
    run_times, missing = [], []
    for g in targets:
        key = make_runtime_key(g["g_id"], g["g_dt"])
        hit = RUNTIME_MEM.get(key)
        if hit and "runtime_min" in hit:
            run_times.append(hit["runtime_min"])
        else:
            missing.append(g)

    # 3) 부족분 처리
    if missing:
        # ✅ 캐시 전용 모드면 보충하지 않고 건너뜀
        if USE_CACHE_ONLY:
            return (round(sum(run_times)/len(run_times), 1), run_times) if run_times else (None, [])
        # (크롤링 허용일 때만) 보충
        if len(missing) > MAX_REVIEW_PER_REQUEST:
            missing = missing[:MAX_REVIEW_PER_REQUEST]
        d = make_driver()
        try:
            need_dates = sorted({g["g_dt"] for g in missing if g.get("g_dt")})
            ensure_schedule_for_dates(need_dates)
            for g in missing:
                rt = open_review_and_get_runtime(d, g["g_id"], g["g_dt"])
                if rt is not None:
                    run_times.append(rt)
        finally:
            try: d.quit()
            except: pass

    if run_times:
        return round(sum(run_times) / len(run_times), 1), run_times
    return None, []

# ====== 공통 처리 ======
def compute_for_team(team_name, asof: str | None = None):
    if not team_name:
        return dict(result="팀을 선택해주세요.", avg_time=None, css_class="", msg="",
                    selected_team=None, top30=top30, avg_ref=avg_ref, bottom70=bottom70)

    selected_can = canon_team(team_name)
    ref = _asof_or_today(asof)
    is_today = (ref == datetime.today().strftime("%Y%m%d"))
    no_game_text = (f"오늘 {selected_can}의 경기가 없습니다."
                    if is_today else f"{ref} {selected_can} 경기가 없습니다.")

    # as-of 날짜의 매치업을 캐시에서 찾기
    today_matches = find_today_matches_for_team_from_cache(selected_can, ref)

    if not today_matches:
        # 캐시 전용이 아니고, 캐시에 ref가 없으면 보충 시도
        if not USE_CACHE_ONLY:
            ensure_schedule_for_dates([ref])
            today_matches = find_today_matches_for_team_from_cache(selected_can, ref)

    if not today_matches:
        return dict(result=no_game_text,
                    avg_time=None, css_class="", msg="",
                    selected_team=selected_can, top30=top30, avg_ref=avg_ref, bottom70=bottom70)

    rivals_today = {m["rival"] for m in today_matches if m.get("rival")}
    rivals_str = ", ".join(sorted(rivals_today)) if rivals_today else ""

    try:
        avg_time, _ = collect_history_avg_runtime(selected_can, rivals_today, start_date=START_DATE, asof=ref)
    except Exception:
        avg_time = None

    css_class = ""; msg = ""
    if avg_time is not None:
        if avg_time < top30:        css_class, msg = "fast", "빠르게 끝나는 경기입니다"
        elif avg_time < avg_ref:    css_class, msg = "normal", "일반적인 경기 소요 시간입니다"
        elif avg_time < bottom70:   css_class, msg = "bit-long", "조금 긴 편이에요"
        else:                       css_class, msg = "long", "시간 오래 걸리는 매치업입니다"
        result = f"{ref} {selected_can}의 상대팀은 {rivals_str}입니다.<br>과거 {selected_can} vs {rivals_str} 평균 경기시간: {avg_time}분"
    else:
        # 평균이 없으면 문구 통일
        result = no_game_text

    return dict(result=result, avg_time=avg_time, css_class=css_class, msg=msg,
                selected_team=selected_can, top30=top30, avg_ref=avg_ref, bottom70=bottom70)

# ====== 라우트 ======
@app.route("/", methods=["GET","POST"])
@app.route("/hour", methods=["GET","POST"])
def hour_index():
    try:
        team = (request.args.get("myteam") or request.form.get("myteam") or "").strip()
        asof = (request.args.get("asof") or request.form.get("asof") or "").strip() or None  # ✅ 추가
        ctx = compute_for_team(team, asof=asof) if team else dict(
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
        "seed_candidates": {
            "runtime": [ _file_info(p) for p in SEED_RUNTIME_CANDIDATES ],
            "schedule": [ _file_info(p) for p in SEED_SCHEDULE_CANDIDATES ],
        },
        "mem_sizes": {
            "runtime_keys": len(RUNTIME_MEM or {}),
            "schedule_days": len(SCHEDULE_MEM or {}),
        },
        "config": {
            "START_DATE": START_DATE,
            "HISTORY_DAYS": HISTORY_DAYS,
            "MAX_REVIEW_PER_REQUEST": MAX_REVIEW_PER_REQUEST,
            "USE_CACHE_ONLY": USE_CACHE_ONLY,
        }
    })

@app.route("/cache/clear", methods=["POST"])
def cache_clear():
    global RUNTIME_MEM, SCHEDULE_MEM
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
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("runtime_cache.json", json.dumps(get_runtime_cache(), ensure_ascii=False, indent=2))
        z.writestr("schedule_index.json", json.dumps(get_schedule_cache(), ensure_ascii=False, indent=2))
    mem.seek(0)
    resp = make_response(mem.read())
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = "attachment; filename=hour_cache_seed.zip"
    return resp

if __name__ == "__main__":
    app.run(debug=True, port=5002, use_reloader=False)
