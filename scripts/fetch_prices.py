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
  FORCE_FETCH=1           (선택) 장 시간 무시하고 강제 수집
  KIWOOM_APP_KEY/SECRET   (선택) 국내 실시간 시세

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
def _force():
    return os.environ.get("FORCE_FETCH") == "1"


def kr_market_open():
    if _force() or ZoneInfo is None:
        return True
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 <= t <= 15 * 60 + 35


def us_market_open():
    if _force() or ZoneInfo is None:
        return True
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    return 4 <= now.hour < 20


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
def fetch_kr_kiwoom(codes):
    key, sec = os.environ.get("KIWOOM_APP_KEY"), os.environ.get("KIWOOM_APP_SECRET")
    if not key or not sec:
        return {}
    base = ("https://api.kiwoom.com" if os.environ.get("KIWOOM_ENV") == "real"
            else "https://mockapi.kiwoom.com")
    try:
        t = requests.post(f"{base}/oauth2/token",
                          json={"grant_type": "client_credentials",
                                "appkey": key, "secretkey": sec},
                          timeout=15)
        t.raise_for_status()
        token = t.json().get("token") or t.json().get("access_token")
        if not token:
            return {}
    except Exception as e:
        print(f"키움 토큰 실패: {e}", file=sys.stderr)
        return {}

    out = {}
    for code in codes:
        try:
            r = requests.post(f"{base}/api/dostk/stkinfo",
                              headers={"authorization": f"Bearer {token}",
                                       "api-id": "ka10001",
                                       "content-type": "application/json"},
                              json={"stk_cd": code}, timeout=15)
            r.raise_for_status()
            px = num(str(r.json().get("cur_prc", "")).lstrip("+-"))
            if px:
                out[code] = {"price": px, "change_pct": None,
                             "as_of": "kiwoom", "source": "kiwoom"}
        except Exception:
            pass
    return out


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
    do_kr, do_us = kr_market_open(), us_market_open()
    print(f"장 상태 → 국내 {'열림' if do_kr else '닫힘'}, 미국 {'열림' if do_us else '닫힘'}"
          + ("  (FORCE_FETCH)" if _force() else ""))
    if not do_kr and not do_us:
        print("두 시장 모두 장외 시간이라 건너뜁니다.")
        return

    try:
        KR, US = load_holdings()
    except Exception as e:
        print(f"[치명] 보유 종목 로드 실패: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"보유 종목 → 국내 {len(KR)}개, 해외 {len(US)}개\n")

    rows = []
    if do_kr and KR:
        print("── 국내 ──")
        rows += collect(KR, KR_SOURCES, "KRW", fetch_kr_kiwoom(list(KR)))
    if do_us and US:
        print("── 해외 ──")
        rows += collect(US, US_SOURCES, "USD")

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
