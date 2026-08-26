"""
시세 수집 → Supabase pf_prices / pf_price_history / pf_settings

핵심 원칙
---------
1. **보유 중인 종목만 수집한다.**
   pf_baselines(시작 보유) + pf_trades(매수/매도)로 현재 수량을 계산해서
   수량이 0보다 큰 종목만 대상으로 한다. 판 종목은 자동으로 빠진다.

2. **소스를 여러 개 순차 시도하고, 실패하면 이유를 남긴다.**
   무료 소스는 수시로 막히거나 구조가 바뀐다. 어떤 소스가 살아있는지
   로그로 알 수 있어야 다음에 고칠 수 있다.

필요 환경변수
  SUPABASE_URL            프로젝트 URL
  SUPABASE_SERVICE_KEY    service_role(또는 sb_secret_) 키
  SUPABASE_OWNER_UID      본인 UUID
  KIWOOM_APP_KEY/SECRET   (선택) 키움 시세. 없으면 무료 소스만 씁니다.
  KIWOOM_ENV              (선택) real | mock — 시세 조회는 real 이어야 합니다.
  KIWOOM_US_PATH          (선택) 해외 현재가 경로 재정의
  KIWOOM_US_API_ID        (선택) 해외 현재가 api-id 재정의

의존성: pip install requests
"""
import os, re, json, sys
import requests
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY  = os.environ["SUPABASE_SERVICE_KEY"]
OWNER_UID    = os.environ.get("SUPABASE_OWNER_UID")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
      "Accept": "application/json,text/html,*/*",
      "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}

# 소스별 성공/실패 집계 (마지막에 요약 출력)
TALLY = {}


def tally(src, ok):
    d = TALLY.setdefault(src, {"성공": 0, "실패": 0})
    d["성공" if ok else "실패"] += 1


# ══════════════════════════════════════════════════════════
#  Supabase
# ══════════════════════════════════════════════════════════
def sb_get(path, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    h = {"apikey": SERVICE_KEY, "authorization": f"Bearer {SERVICE_KEY}"}
    r = requests.get(url, headers=h, params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


def sb_upsert(path, rows, on_conflict):
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/{path}?on_conflict={on_conflict}"
    h = {"apikey": SERVICE_KEY, "authorization": f"Bearer {SERVICE_KEY}",
         "content-type": "application/json", "prefer": "resolution=merge-duplicates"}
    r = requests.post(url, headers=h, data=json.dumps(rows), timeout=30)
    r.raise_for_status()


def owner_param(p=None):
    p = dict(p or {})
    if OWNER_UID:
        p["owner"] = f"eq.{OWNER_UID}"
    return p


# ══════════════════════════════════════════════════════════
#  보유 종목 산출 — 이게 이 파일의 핵심
# ══════════════════════════════════════════════════════════
def load_holdings():
    """현재 보유 중인 종목만 반환. {ticker: (name, market)}

    baseline(로그 시작 시점 보유) + 매수 − 매도 = 현재 수량.
    수량이 0 이하인 종목(= 전량 매도한 종목)은 제외한다.
    """
    qty = {}

    for r in sb_get("pf_baselines", owner_param({"select": "ticker,qty"})):
        t = r.get("ticker")
        if t:
            qty[t] = qty.get(t, 0) + float(r.get("qty") or 0)

    for r in sb_get("pf_trades", owner_param({"select": "ticker,side,qty"})):
        t, side, q = r["ticker"], r["side"], float(r.get("qty") or 0)
        qty[t] = qty.get(t, 0) + (q if side == "매수" else -q)

    held = {t for t, q in qty.items() if q > 1e-9}

    meta = {}
    for r in sb_get("pf_tickers", owner_param({"select": "ticker,name,market"})):
        meta[r["ticker"]] = (r.get("name") or r["ticker"], r.get("market") or "")

    kr, us = {}, {}
    for t in sorted(held):
        name, market = meta.get(t, (t, ""))
        (us if market == "US" else kr)[t] = (name, market)

    sold = sorted(set(qty) - held)
    if sold:
        print(f"제외(전량매도): {', '.join(sold)}")
    return kr, us


# ══════════════════════════════════════════════════════════
#  장 시간
# ══════════════════════════════════════════════════════════
def now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul")) if ZoneInfo else datetime.now(timezone.utc)


def market_note():
    """지금이 장중인지 장외인지 — 로그에 남기기만 하고 수집은 항상 한다.

    장이 닫혀 있어도 소스는 '마지막 종가'를 돌려준다. 종가를 반영하지 않으면
    주말·야간 내내 대시보드가 옛날 값을 보여주게 된다. 실행 빈도는
    스크립트가 아니라 워크플로 cron에서 조절한다.
    """
    if ZoneInfo is None:
        return "시간대 판별 불가"
    kr = datetime.now(ZoneInfo("Asia/Seoul"))
    us = datetime.now(ZoneInfo("America/New_York"))
    kr_open = kr.weekday() < 5 and 9 * 60 <= kr.hour * 60 + kr.minute <= 15 * 60 + 30
    us_open = us.weekday() < 5 and 4 <= us.hour < 20
    return (f"KST {kr:%m-%d %H:%M} 국내 {'장중' if kr_open else '장외(종가)'} · "
            f"ET {us:%m-%d %H:%M} 미국 {'장중/연장' if us_open else '장외(종가)'}")


def num(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════
#  국내 시세 소스
# ══════════════════════════════════════════════════════════
def kr_naver_mobile(code, market=""):
    """네이버 증권 모바일 API. 국내 주식·ETF 모두 지원."""
    r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/basic",
                     headers=UA, timeout=15)
    r.raise_for_status()
    j = r.json()
    price = num(j.get("closePrice"))
    if not price:
        return None
    return {"price": price,
            "change_pct": num(str(j.get("fluctuationsRatio") or "").replace("%", "")),
            "as_of": j.get("localTradedAt") or j.get("stockEndType") or "naver",
            "source": "naver"}


def kr_naver_polling(code, market=""):
    """네이버 실시간 폴링 API (모바일 API 실패 시 대안)."""
    r = requests.get(
        f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}",
        headers=UA, timeout=15)
    r.raise_for_status()
    datas = (r.json().get("datas") or [])
    if not datas:
        return None
    d = datas[0]
    price = num(d.get("closePrice"))
    if not price:
        return None
    return {"price": price,
            "change_pct": num(str(d.get("fluctuationsRatio") or "").replace("%", "")),
            "as_of": d.get("localTradedAt") or "naver-polling",
            "source": "naver-polling"}


def kr_yahoo(code, market=""):
    """야후 파이낸스. 코스피 .KS / 코스닥 .KQ."""
    sufs = [".KS", ".KQ"]
    if market == "KOSDAQ":
        sufs = [".KQ", ".KS"]
    for suf in sufs:
        try:
            v = yahoo_chart(code + suf)
            if v:
                v["source"] = "yahoo"
                return v
        except Exception:
            continue
    return None


def kr_daum(code, market=""):
    """다음 금융 API."""
    h = dict(UA)
    h["referer"] = f"https://finance.daum.net/quotes/A{code}"
    r = requests.get(f"https://finance.daum.net/api/quotes/A{code}",
                     headers=h, timeout=15)
    r.raise_for_status()
    j = r.json()
    price = num(j.get("tradePrice"))
    if not price:
        return None
    cr = j.get("changeRate")
    pct = round(float(cr) * 100, 2) if cr is not None else None
    if j.get("change") == "FALL" and pct:
        pct = -pct
    return {"price": price, "change_pct": pct,
            "as_of": j.get("date") or "daum", "source": "daum"}


KR_SOURCES = [kr_naver_mobile, kr_naver_polling, kr_daum, kr_yahoo]


# ══════════════════════════════════════════════════════════
#  해외 시세 소스
# ══════════════════════════════════════════════════════════
def yahoo_chart(symbol):
    """야후 chart API 공통. 프리/애프터장 값이 있으면 우선."""
    for host in ("query1", "query2"):
        try:
            r = requests.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"interval": "1d", "range": "1d"}, headers=UA, timeout=15)
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            price = num(meta.get("postMarketPrice")) or num(meta.get("preMarketPrice")) \
                    or num(meta.get("regularMarketPrice"))
            prev = num(meta.get("chartPreviousClose")) or num(meta.get("previousClose"))
            if not price:
                return None
            pct = round((price - prev) / prev * 100, 2) if prev else None
            return {"price": price, "change_pct": pct,
                    "as_of": "yahoo", "source": "yahoo"}
        except Exception:
            continue
    return None


def us_yahoo(ticker):
    return yahoo_chart(ticker)


def us_stooq(ticker):
    """stooq CSV. 형식: Symbol,Date,Time,Open,High,Low,Close,Volume"""
    r = requests.get("https://stooq.com/q/l/",
                     params={"s": f"{ticker.lower()}.us", "f": "sd2t2ohlcv",
                             "h": "", "e": "csv"},
                     headers=UA, timeout=15)
    r.raise_for_status()
    lines = [l for l in r.text.strip().split("\n") if l]
    if len(lines) < 2:
        return None
    c = lines[1].split(",")
    price = num(c[6]) if len(c) > 6 else None
    if not price:
        return None
    return {"price": price, "change_pct": None,
            "as_of": f"stooq {c[1]} {c[2]}", "source": "stooq"}


def us_google(ticker):
    for ex in ("NASDAQ", "NYSE"):
        try:
            r = requests.get(f"https://www.google.com/finance/quote/{ticker}:{ex}",
                             headers=UA, timeout=15)
            r.raise_for_status()
            prices = re.findall(r'data-last-price="([\d.]+)"', r.text)
            if not prices:
                continue
            ext = None
            if ("Pre-market" in r.text or "After hours" in r.text) and len(prices) >= 2:
                ext = num(prices[1])
            return {"price": ext or num(prices[0]), "change_pct": None,
                    "as_of": "google", "source": "google"}
        except Exception:
            continue
    return None


US_SOURCES = [us_yahoo, us_stooq, us_google]


# ══════════════════════════════════════════════════════════
#  환율
# ══════════════════════════════════════════════════════════
def fx_yahoo():
    v = yahoo_chart("KRW=X")
    return v["price"] if v else None


def fx_erapi():
    r = requests.get("https://open.er-api.com/v6/latest/USD", headers=UA, timeout=15)
    r.raise_for_status()
    return num(r.json().get("rates", {}).get("KRW"))


def fx_naver():
    r = requests.get(
        "https://m.stock.naver.com/front-api/marketIndex/prices",
        params={"category": "exchange", "reutersCode": "FX_USDKRW", "page": 1},
        headers=UA, timeout=15)
    r.raise_for_status()
    j = r.json()
    rows = j.get("result") or j.get("datas") or []
    if isinstance(rows, dict):
        rows = rows.get("prices") or []
    return num(rows[0].get("closePrice")) if rows else None


FX_SOURCES = [fx_yahoo, fx_erapi, fx_naver]


# ══════════════════════════════════════════════════════════
#  키움 (선택)
# ══════════════════════════════════════════════════════════
# ── 설정 ────────────────────────────────────────────────────
# 국내 현재가 (문서 확인된 값)
KIWOOM_KR_PATH   = "/api/dostk/stkinfo"
KIWOOM_KR_API_ID = "ka10001"

# 해외 현재가 — ⚠️ 아직 실제 값으로 확인하지 못했습니다.
#   키움 개발자센터 문서에서 '해외주식 현재가' api-id 를 찾아 아래 두 줄만 바꾸면 됩니다.
#   값이 틀려도 무료 소스(야후 등)로 자동 대체되므로 시세가 끊기지는 않습니다.
KIWOOM_US_PATH   = os.environ.get("KIWOOM_US_PATH",   "/api/dostk/ovsstkinfo")
KIWOOM_US_API_ID = os.environ.get("KIWOOM_US_API_ID", "ka20001")

# 가격이 담겨 있을 법한 필드 후보 (응답 스펙이 확인되면 정리)
PRICE_KEYS = ("cur_prc", "last", "prpr", "stck_prpr", "price", "close",
              "ovrs_prpr", "last_prc", "trad_prc")

_TOKEN = None   # 한 번 받아서 재사용


def kiwoom_base():
    env = os.environ.get("KIWOOM_ENV", "mock")
    return ("https://api.kiwoom.com" if env == "real"
            else "https://mockapi.kiwoom.com"), env


def kiwoom_token():
    """토큰을 한 번만 발급받아 재사용. 키가 없으면 None."""
    global _TOKEN
    if _TOKEN is not None:
        return _TOKEN or None

    key, sec = os.environ.get("KIWOOM_APP_KEY"), os.environ.get("KIWOOM_APP_SECRET")
    if not key or not sec:
        print("키움: 키 없음 → 무료 소스만 사용")
        _TOKEN = ""
        return None

    base, env = kiwoom_base()
    print(f"키움: {env} 환경 ({base})")
    if env != "real":
        print("  ⚠️ 모의(mock) 서버는 시세 조회가 제한됩니다. KIWOOM_ENV 를 real 로 하세요.")

    try:
        t = requests.post(f"{base}/oauth2/token",
                          json={"grant_type": "client_credentials",
                                "appkey": key, "secretkey": sec}, timeout=15)
        if t.status_code != 200:
            print(f"  ✗ 토큰 발급 실패 HTTP {t.status_code}: {t.text[:200]}", file=sys.stderr)
            _TOKEN = ""
            return None
        j = t.json()
        tok = j.get("token") or j.get("access_token")
        if not tok:
            print(f"  ✗ 응답에 토큰 없음: {t.text[:200]}", file=sys.stderr)
            _TOKEN = ""
            return None
        print("  ✓ 토큰 발급 성공")
        _TOKEN = tok
        return tok
    except Exception as e:
        print(f"  ✗ 토큰 요청 예외: {type(e).__name__} {e}", file=sys.stderr)
        _TOKEN = ""
        return None


def pick_price(j):
    """응답 dict에서 가격처럼 보이는 값을 찾아낸다."""
    if not isinstance(j, dict):
        return None
    for k in PRICE_KEYS:
        if k in j:
            v = num(str(j[k]).lstrip("+-"))
            if v:
                return v
    # 한 겹 안쪽에 들어있는 경우 (output / data 등)
    for k in ("output", "output1", "data", "result"):
        v = j.get(k)
        if isinstance(v, dict):
            got = pick_price(v)
            if got:
                return got
        if isinstance(v, list) and v and isinstance(v[0], dict):
            got = pick_price(v[0])
            if got:
                return got
    return None


def kiwoom_quote(codes, path, api_id, body_key, label, extra=None):
    """키움 시세 조회 공통. 실패해도 예외를 밖으로 내지 않는다."""
    token = kiwoom_token()
    if not token:
        return {}
    base, _ = kiwoom_base()
    h = {"authorization": f"Bearer {token}", "api-id": api_id,
         "content-type": "application/json"}

    out, shown = {}, False
    for code in codes:
        body = {body_key: code}
        if extra:
            body.update(extra)
        try:
            r = requests.post(f"{base}{path}", headers=h, json=body, timeout=15)
            if r.status_code != 200:
                if not shown:
                    print(f"  ✗ {label} HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
                    shown = True
                continue
            j = r.json()
            px = pick_price(j)
            if px:
                out[code] = {"price": px, "change_pct": None,
                             "as_of": "kiwoom", "source": "kiwoom"}
            elif not shown:
                # 응답은 왔는데 가격 필드를 못 찾음 → 구조를 통째로 남긴다
                print(f"  ✗ {label} 가격 필드 못 찾음. 응답 구조:", file=sys.stderr)
                print(f"     {json.dumps(j, ensure_ascii=False)[:500]}", file=sys.stderr)
                shown = True
        except Exception as e:
            if not shown:
                print(f"  ✗ {label} 예외: {type(e).__name__} {e}", file=sys.stderr)
                shown = True

    if out:
        print(f"  ✓ {label} {len(out)}종목 수집")
    return out


def fetch_kr_kiwoom(codes):
    """국내 — 키움 (키 없으면 빈 dict)."""
    if not codes:
        return {}
    return kiwoom_quote(codes, KIWOOM_KR_PATH, KIWOOM_KR_API_ID,
                        "stk_cd", "키움 국내")


def fetch_us_kiwoom(codes):
    """해외 — 키움. api-id/경로가 아직 미확인이라 실패하면 조용히 야후로 넘어간다.

    ⚠️ 정확한 값을 알게 되면 KIWOOM_US_PATH / KIWOOM_US_API_ID 만 고치면 됩니다.
       GitHub Secrets 에 같은 이름으로 넣어도 덮어쓸 수 있습니다.
    """
    if not codes or not os.environ.get("KIWOOM_APP_KEY"):
        return {}
    return kiwoom_quote(codes, KIWOOM_US_PATH, KIWOOM_US_API_ID,
                        "stk_cd", "키움 해외", extra={"exchange": "NASDAQ"})


# ══════════════════════════════════════════════════════════
#  저장
# ══════════════════════════════════════════════════════════
def today_kst():
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat() if ZoneInfo \
               else datetime.now(timezone.utc).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def save_prices(rows):
    for r in rows:
        if OWNER_UID:
            r["owner"] = OWNER_UID
    sb_upsert("pf_prices", rows, "owner,ticker")
    print(f"→ pf_prices 저장 {len(rows)}건")


def save_history(rows):
    if not rows:
        return
    d = today_kst()
    try:
        prev = {r["ticker"]: r for r in
                sb_get("pf_price_history",
                       owner_param({"select": "ticker,high,low,open", "d": f"eq.{d}"}))}
    except Exception as e:
        print(f"이력 조회 실패(신규로 처리): {e}", file=sys.stderr)
        prev = {}

    out = []
    for r in rows:
        t, px = r["ticker"], r["price"]
        p = prev.get(t)
        hi = max(float(p.get("high") or px), px) if p else px
        lo = min(float(p.get("low") or px), px) if p else px
        op = (p.get("open") if p else None) or px
        row = {"ticker": t, "d": d, "open": op, "high": hi, "low": lo,
               "close": px, "currency": r.get("currency", "KRW")}
        if OWNER_UID:
            row["owner"] = OWNER_UID
        out.append(row)
    sb_upsert("pf_price_history", out, "owner,ticker,d")
    print(f"→ pf_price_history 저장 {len(out)}건 ({d})")


def save_setting(key, value):
    row = {"key": key, "value": value}
    if OWNER_UID:
        row["owner"] = OWNER_UID
    sb_upsert("pf_settings", [row], "owner,key")
    print(f"→ 설정 저장 {key} = {value}")


# ══════════════════════════════════════════════════════════
#  수집 루프
# ══════════════════════════════════════════════════════════
def collect(items, sources, currency, preset=None):
    """items: {ticker: (name, market)} → 시세 row 리스트"""
    rows, failed = [], []
    for code, (name, market) in items.items():
        got = (preset or {}).get(code)
        if got:
            tally("kiwoom", True)
        else:
            errs = []
            for fn in sources:
                try:
                    v = fn(code, market) if currency == "KRW" else fn(code)
                    if v and v.get("price"):
                        got = v
                        tally(v.get("source", fn.__name__), True)
                        break
                    errs.append(f"{fn.__name__}: 값없음")
                    tally(fn.__name__, False)
                except Exception as e:
                    errs.append(f"{fn.__name__}: {type(e).__name__} {str(e)[:60]}")
                    tally(fn.__name__, False)
        if got:
            rows.append({"ticker": code, "name": name, "price": got["price"],
                         "change_pct": got.get("change_pct"), "currency": currency,
                         "source": got.get("source"), "as_of": got.get("as_of")})
            print(f"  ✓ {name}({code}) {got['price']:,} [{got.get('source')}]")
        else:
            failed.append((name, code, errs))
    for name, code, errs in failed:
        print(f"  ✗ {name}({code}) 실패")
        for e in errs:
            print(f"      {e}")
    return rows


def main():
    print(market_note())
    print("장외라도 마지막 종가를 가져옵니다.\n")

    try:
        KR, US = load_holdings()
    except Exception as e:
        print(f"[치명] 보유 종목 로드 실패: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"보유 종목 → 국내 {len(KR)}개, 해외 {len(US)}개\n")

    rows = []
    if KR:
        print("── 국내 ──")
        rows += collect(KR, KR_SOURCES, "KRW", fetch_kr_kiwoom(list(KR)))
    if US:
        print("── 해외 ──")
        rows += collect(US, US_SOURCES, "USD", fetch_us_kiwoom(list(US)))

    print("\n── 환율 ──")
    fx = None
    for fn in FX_SOURCES:
        try:
            fx = fn()
            if fx and 500 < fx < 3000:
                print(f"  ✓ USD/KRW {fx:,} [{fn.__name__}]")
                break
            print(f"  ✗ {fn.__name__}: 값이 이상함 ({fx})")
            fx = None
        except Exception as e:
            print(f"  ✗ {fn.__name__}: {type(e).__name__} {str(e)[:60]}")

    print("\n── 소스별 집계 ──")
    for src, d in sorted(TALLY.items()):
        print(f"  {src:20s} 성공 {d['성공']:2d} / 실패 {d['실패']:2d}")

    if not rows and not fx:
        print("\n[치명] 아무것도 수집하지 못했습니다.", file=sys.stderr)
        sys.exit(1)

    print()
    if rows:
        save_prices(rows)
        save_history(rows)
    if fx:
        save_setting("fx_usd_krw", fx)
    print("\n완료.")


if __name__ == "__main__":
    main()
