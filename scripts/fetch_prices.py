"""
지연시세 스크래퍼 → Supabase prices 테이블 upsert
------------------------------------------------------
- 국내주식/ETF: alphasquare.co.kr (분 단위 기준시각 제공), 실패 시 stockanalysis.com
- 미국주식: Google Finance (프리마켓/애프터마켓 있으면 우선 반영)
- 결과를 Supabase prices 테이블에 저장 (service_role 키 사용)

실행 환경:
  * GitHub Actions에서 10분마다 실행 (장중에만 실제 수집)
  * 또는 본인 컴퓨터에서 수동 실행

필요 환경변수:
  SUPABASE_URL            - 프로젝트 URL
  SUPABASE_SERVICE_KEY    - service_role 키 (절대 공개 금지, GitHub Secrets에 저장)
  KIWOOM_APP_KEY (선택)   - 키움 REST API 조회용. 있으면 국내 시세를 키움으로 대체
  KIWOOM_APP_SECRET (선택)
  KIWOOM_ENV (선택)       - 'mock'(모의) 또는 'real'(실전), 기본 mock

의존성: pip install requests beautifulsoup4
"""
import os, re, json, sys
import requests
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo   # Python 3.9+
except ImportError:
    ZoneInfo = None

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY  = os.environ["SUPABASE_SERVICE_KEY"]

# 추적 종목은 DB(tickers 테이블)에서 동적으로 읽어옵니다.
# 앱에서 새 종목을 매수하면 tickers에 자동 등록되므로 이 파일은 수정할 필요가 없습니다.
OWNER_UID = os.environ.get("SUPABASE_OWNER_UID")   # RLS 우회(service_role) 시 필요


def sb_get(path, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    h = {"apikey": SERVICE_KEY, "authorization": f"Bearer {SERVICE_KEY}"}
    r = requests.get(url, headers=h, params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


def load_tickers():
    """tickers 테이블 → (국내 dict, 해외 dict). market 컬럼으로 구분."""
    params = {"select": "ticker,name,market"}
    if OWNER_UID:
        params["owner"] = f"eq.{OWNER_UID}"
    rows = sb_get("tickers", params)
    kr, us = {}, {}
    for row in rows:
        t, nm, mk = row["ticker"], row.get("name") or row["ticker"], (row.get("market") or "")
        (us if mk == "US" else kr)[t] = nm
    return kr, us

HEADERS = {"User-Agent": "Mozilla/5.0 (price-fetch bot)"}


def kr_market_open():
    """국내장: 평일 09:00~15:35 KST. FORCE_FETCH=1 이면 무조건 True."""
    if os.environ.get("FORCE_FETCH") == "1":
        return True
    if ZoneInfo is None:
        return True  # 시간대 판별 불가하면 그냥 수집
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    if now.weekday() >= 5:            # 토(5)/일(6)
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 <= mins <= 15 * 60 + 35


def us_market_open():
    """미국장: 평일 프리장~애프터장 (미 동부시간 04:00~20:00, 서머타임 자동).
    금요일 애프터장까지 포함(한국시간 토요일 새벽). FORCE_FETCH=1 이면 무조건 True."""
    if os.environ.get("FORCE_FETCH") == "1":
        return True
    if ZoneInfo is None:
        return True
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:            # 미 동부 기준 토/일
        return False
    mins = now.hour * 60 + now.minute
    return 4 * 60 <= mins <= 20 * 60


def num(s):
    if s is None: return None
    s = re.sub(r"[^\d.\-]", "", str(s))
    try: return float(s)
    except: return None


def fetch_kr_alphasquare(code):
    """alphasquare 종목요약 페이지에서 현재가/변동률/기준시각 파싱."""
    url = f"https://alphasquare.co.kr/home/stock-summary?code={code}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    html = r.text
    # meta description에 "현재 주가는 15,210원입니다" 형태
    m = re.search(r"현재 주가는\s*([\d,]+)\s*원", html)
    price = num(m.group(1)) if m else None
    chg = re.search(r"변동률[^\-\d]*(-?[\d.]+)%", html)
    change = num(chg.group(1)) if chg else None
    ts = re.search(r"기준 시각[^\d]*([\d.]{8,10}\s*[\d:]{5,8})", html)
    as_of = ts.group(1).strip() if ts else None
    return {"price": price, "change_pct": change, "as_of": as_of, "source": "alphasquare"} if price else None


def fetch_kr_stockanalysis(code):
    url = f"https://stockanalysis.com/quote/krx/{code}/"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    m = re.search(r'"price"\s*:\s*([\d.]+)', r.text) or re.search(r'>([\d,]+)</div>\s*<div[^>]*>[+\-]', r.text)
    price = num(m.group(1)) if m else None
    return {"price": price, "change_pct": None, "as_of": "stockanalysis", "source": "stockanalysis"} if price else None


def fetch_us_google(ticker):
    """Google Finance. 프리마켓/애프터마켓 값이 있으면 우선 사용."""
    url = f"https://www.google.com/finance/quote/{ticker}:NASDAQ"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    html = r.text
    prices = re.findall(r'data-last-price="([\d.]+)"', html)
    # 프리마켓/애프터 텍스트가 페이지에 있으면 두 번째 가격(있을 경우)이 연장거래가인 경우가 많음
    regular = num(prices[0]) if prices else None
    ext = None
    if ("Pre-market" in html or "After hours" in html) and len(prices) >= 2:
        ext = num(prices[1])
    price = ext if ext else regular
    label = "pre/after" if ext else "regular"
    return {"price": price, "change_pct": None, "as_of": f"google({label})", "source": "google"} if price else None


def fetch_usd_krw():
    """Google Finance에서 USD/KRW 환율."""
    url = "https://www.google.com/finance/quote/USD-KRW"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    m = re.search(r'data-last-price="([\d.]+)"', r.text)
    return num(m.group(1)) if m else None


def fetch_kr_kiwoom(codes):
    """(선택) 키움 REST API로 국내 현재가 조회. 키가 있을 때만.
    - 토큰: POST {host}/oauth2/token  (grant_type=client_credentials, appkey, secretkey)
    - 시세: POST {host}/api/dostk/stkinfo  헤더 api-id=ka10001, 바디 {"stk_cd": 코드}
      (ka10001 = 주식기본정보요청)
    응답 필드명이 환경에 따라 다를 수 있어, 후보 키를 순서대로 탐색하고
    실패 시 응답 키 목록을 출력해 맞춰나갈 수 있게 했습니다."""
    app_key = os.environ.get("KIWOOM_APP_KEY"); app_secret = os.environ.get("KIWOOM_APP_SECRET")
    if not app_key or not app_secret: return {}
    env = os.environ.get("KIWOOM_ENV", "mock")
    base = "https://mockapi.kiwoom.com" if env == "mock" else "https://api.kiwoom.com"
    # --- 토큰 발급 ---
    try:
        tr = requests.post(f"{base}/oauth2/token",
                           json={"grant_type": "client_credentials", "appkey": app_key, "secretkey": app_secret},
                           headers={"content-type": "application/json;charset=UTF-8"}, timeout=15).json()
        tok = tr.get("token") or tr.get("access_token")
        if not tok:
            print(f"키움 토큰 발급 실패: {tr}", file=sys.stderr); return {}
    except Exception as e:
        print(f"키움 토큰 요청 실패: {e}", file=sys.stderr); return {}

    price_keys = ["cur_prc", "stck_prpr", "prpr", "cur_pric", "base_pric"]
    chg_keys = ["flu_rt", "fluc_rt", "prdy_ctrt", "chg_rt"]
    out = {}
    for code in codes:
        try:
            h = {"authorization": f"Bearer {tok}", "api-id": "ka10001",
                 "content-type": "application/json;charset=UTF-8", "cont-yn": "N", "next-key": ""}
            r = requests.post(f"{base}/api/dostk/stkinfo", headers=h, json={"stk_cd": code}, timeout=15).json()
            price = next((num(r[k]) for k in price_keys if r.get(k) not in (None, "")), None)
            chg = next((num(r[k]) for k in chg_keys if r.get(k) not in (None, "")), None)
            if price:
                out[code] = {"price": abs(price), "change_pct": chg, "as_of": "kiwoom", "source": "kiwoom"}
            else:
                print(f"키움 {code}: 현재가 필드 못 찾음. 응답 키들 → {list(r.keys())}", file=sys.stderr)
        except Exception as e:
            print(f"키움 {code} 실패: {e}", file=sys.stderr)
    return out


def upsert(rows):
    if OWNER_UID:
        for row in rows:
            row["owner"] = OWNER_UID
    url = f"{SUPABASE_URL}/rest/v1/prices?on_conflict=owner,ticker"
    h = {"apikey": SERVICE_KEY, "authorization": f"Bearer {SERVICE_KEY}",
         "content-type": "application/json", "prefer": "resolution=merge-duplicates"}
    r = requests.post(url, headers=h, data=json.dumps(rows), timeout=20)
    r.raise_for_status()
    print(f"시세 upsert 완료: {len(rows)}건")


def upsert_history(rows):
    """pf_price_history 에 당일 봉을 기록/갱신.
    같은 날 여러 번 실행되면 high/low 를 확장합니다 (MAE/MFE 계산용)."""
    if not rows:
        return
    try:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat() if ZoneInfo \
                else datetime.now(timezone.utc).date().isoformat()
    except Exception:
        today = datetime.now(timezone.utc).date().isoformat()

    params = {"select": "ticker,high,low,open", "d": f"eq.{today}"}
    if OWNER_UID:
        params["owner"] = f"eq.{OWNER_UID}"
    try:
        existing = {r["ticker"]: r for r in sb_get("pf_price_history", params)}
    except Exception as e:
        print(f"이력 조회 실패(신규로 처리): {e}", file=sys.stderr)
        existing = {}

    out = []
    for r in rows:
        t, px, ccy = r["ticker"], r["price"], r.get("currency", "KRW")
        prev = existing.get(t)
        if prev:
            hi = max(float(prev.get("high") or px), px)
            lo = min(float(prev.get("low") or px), px)
            op = prev.get("open") or px
        else:
            hi = lo = op = px
        row = {"ticker": t, "d": today, "open": op, "high": hi, "low": lo,
               "close": px, "currency": ccy}
        if OWNER_UID:
            row["owner"] = OWNER_UID
        out.append(row)

    url = f"{SUPABASE_URL}/rest/v1/pf_price_history?on_conflict=owner,ticker,d"
    h = {"apikey": SERVICE_KEY, "authorization": f"Bearer {SERVICE_KEY}",
         "content-type": "application/json", "prefer": "resolution=merge-duplicates"}
    resp = requests.post(url, headers=h, data=json.dumps(out), timeout=20)
    resp.raise_for_status()
    print(f"시세 이력 기록: {len(out)}건 ({today})")


def upsert_setting(key, value):
    row = {"key": key, "value": value}
    if OWNER_UID:
        row["owner"] = OWNER_UID
    url = f"{SUPABASE_URL}/rest/v1/settings?on_conflict=owner,key"
    h = {"apikey": SERVICE_KEY, "authorization": f"Bearer {SERVICE_KEY}",
         "content-type": "application/json", "prefer": "resolution=merge-duplicates"}
    r = requests.post(url, headers=h, data=json.dumps([row]), timeout=20)
    r.raise_for_status()
    print(f"설정 갱신: {key} = {value}")


def main():
    rows = []
    do_kr = kr_market_open()
    do_us = us_market_open()
    print(f"장 상태 → 국내: {'열림' if do_kr else '닫힘(건너뜀)'}, 미국: {'열림' if do_us else '닫힘(건너뜀)'}")

    if not do_kr and not do_us:
        print("두 시장 모두 장외 시간이라 수집을 건너뜁니다.")
        return

    try:
        KR_TICKERS, US_TICKERS = load_tickers()
    except Exception as e:
        print(f"[치명] tickers 테이블 로드 실패: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"추적 종목 → 국내 {len(KR_TICKERS)}개, 해외 {len(US_TICKERS)}개")

    if do_kr and KR_TICKERS:
        kiwoom = fetch_kr_kiwoom(list(KR_TICKERS.keys()))
        for code, name in KR_TICKERS.items():
            data = kiwoom.get(code)
            for fn in (fetch_kr_alphasquare, fetch_kr_stockanalysis):
                if data and data.get("price"):
                    break
                try:
                    data = fn(code)
                except Exception as e:
                    print(f"{fn.__name__} {code} 실패: {e}", file=sys.stderr)
                    data = None
            if data and data.get("price"):
                rows.append({"ticker": code, "name": name, "price": data["price"],
                             "change_pct": data.get("change_pct"), "currency": "KRW",
                             "source": data["source"], "as_of": data.get("as_of"),
                             "updated_at": datetime.now(timezone.utc).isoformat()})
            else:
                print(f"[경고] {name}({code}) 시세 수집 실패 — 이전 값 유지", file=sys.stderr)

    if do_us and US_TICKERS:
        for ticker, name in US_TICKERS.items():
            try:
                data = fetch_us_google(ticker)
            except Exception as e:
                print(f"google {ticker} 실패: {e}", file=sys.stderr)
                data = None
            if data and data.get("price"):
                rows.append({"ticker": ticker, "name": name, "price": data["price"],
                             "change_pct": data.get("change_pct"), "currency": "USD",
                             "source": data["source"], "as_of": data.get("as_of"),
                             "updated_at": datetime.now(timezone.utc).isoformat()})
            else:
                print(f"[경고] {name}({ticker}) 시세 수집 실패 — 이전 값 유지", file=sys.stderr)

    # 환율은 장 시간과 무관하게 갱신 (해외자산 원화 환산에 항상 필요)
    try:
        fx = fetch_usd_krw()
        if fx and 500 < fx < 3000:
            upsert_setting("fx_usd_krw", fx)
        else:
            print(f"[경고] 환율 값이 이상함: {fx} — 갱신 건너뜀", file=sys.stderr)
    except Exception as e:
        print(f"환율 수집 실패: {e}", file=sys.stderr)

    if rows:
        upsert(rows)
        try:
            upsert_history(rows)
        except Exception as e:
            print(f"이력 기록 실패(시세는 정상 저장됨): {e}", file=sys.stderr)
    else:
        print("수집된 시세가 없습니다.", file=sys.stderr)


if __name__ == "__main__":
    main()
