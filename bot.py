"""
가족 항공권 특가 알림 봇 (GitHub Actions, 1시간마다 실행)

- 성수기 = 네이버 데이터랩에서 최근 2년간 "○○ 여행/날씨/항공권"이 많이 검색된 달 (목적지별)
- Travelpayouts(Aviasales) 무료 API로 성수기 출발 직항 왕복 최저가(성인 1인)를 수집
- 노선별 '성수기 가격끼리' 비교해 하위 N% 후보를 고르고,
  SerpApi(구글 항공권, 성인2·소아3·직항)로 다시 조회해 구글이 '낮음'이라고 할 때만 알림
- 한 번 알린 (노선, 출발일, 귀국일)은 다시 알리지 않음
- 알릴 게 없으면 아무것도 보내지 않음
"""
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
HISTORY_FILE = DATA / "history.json"
SENT_FILE = DATA / "sent.json"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

TP_TOKEN = os.environ.get("TP_TOKEN", "")
NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
SERP_KEY = os.environ.get("SERPAPI_KEY", "")
PEAK_FILE = DATA / "peak.json"
SERP_FILE = DATA / "google_checks.json"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_IDS = [c.strip() for c in os.environ.get("CHAT_ID", "").split(",") if c.strip()]

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).date()
DOW = "월화수목금토일"


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ───────── 성수기 (네이버 검색량) ─────────
def peak_months():
    """{목적지코드: [성수기 달...]} — 최근 24개월 네이버 검색량을 달별로 평균내서 상위 달. 일주일에 한 번 갱신"""
    cached = load(PEAK_FILE, {})
    if cached.get("_updated", "") >= str(TODAY - timedelta(days=7)) and cached.get("peaks"):
        return {k: v for k, v in cached["peaks"].items()}
    if not (NAVER_ID and NAVER_SECRET):
        print("! 네이버 키가 없어서 성수기 계산 생략 (기피 시기 외 전체 달 사용)")
        return cached.get("peaks", {})
    end = TODAY.replace(day=1) - timedelta(days=1)
    start = date(end.year - 2, end.month, 1) + timedelta(days=31)
    start = start.replace(day=1)
    groups = []
    for code, name in CFG["destinations"].items():
        n = name.split("(")[0].strip()
        groups.append((code, {"groupName": code, "keywords": [f"{n} 여행", f"{n} 날씨", f"{n} 항공권", f"{n} 가족여행"]}))
    peaks, monthly = {}, {}
    for i in range(0, len(groups), 5):
        batch = groups[i:i + 5]
        body = {"startDate": str(start), "endDate": str(end), "timeUnit": "month",
                "keywordGroups": [g for _, g in batch]}
        try:
            r = requests.post("https://naverapihub.apigw.ntruss.com/search-trend/v1/search", json=body, timeout=30,
                              headers={"X-NCP-APIGW-API-KEY-ID": NAVER_ID, "X-NCP-APIGW-API-KEY": NAVER_SECRET,
                                       "Content-Type": "application/json"})
            r.raise_for_status()
            for res in r.json()["results"]:
                by_m = {}
                for x in res["data"]:
                    by_m.setdefault(int(x["period"][5:7]), []).append(x["ratio"])
                avg = {m: sum(v) / len(v) for m, v in by_m.items()}
                monthly[res["title"]] = avg
        except Exception as e:
            print(f"! 데이터랩 오류: {e}")
        time.sleep(0.3)
    for code, avg in monthly.items():
        if not avg:
            continue
        ranked = sorted(avg, key=avg.get, reverse=True)
        top = max(avg.values())
        pk = [m for m in ranked if avg[m] >= top * CFG.get("peak_ratio", 0.75)][: CFG.get("peak_max_months", 5)]
        pk = [m for m in pk if m not in avoid_months(code)] or ranked[:3]
        peaks[code] = sorted(pk)
    save(PEAK_FILE, {"_updated": str(TODAY), "peaks": peaks,
                     "monthly": {k: {str(m): round(v, 1) for m, v in a.items()} for k, a in monthly.items()}})
    print("성수기:", {CFG["destinations"][k]: v for k, v in peaks.items()})
    return peaks


PEAKS = {}


def in_peak(dest, dep):
    pk = PEAKS.get(dest)
    return True if not pk else dep.month in pk


# ───────── 가격 수집 ─────────
def months_to_search():
    y, m = TODAY.year, TODAY.month
    out = []
    for _ in range(CFG["months_ahead"] + 1):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def avoid_months(dest):
    return set(CFG.get("avoid", {}).get(dest, {}).get("months", []))


def in_avoid(dest, dep, ret):
    """여행 기간 중 하루라도 기피 시기에 걸리면 True"""
    bad = avoid_months(dest)
    if not bad:
        return False
    d = dep
    while d <= ret:
        if d.month in bad:
            return True
        d += timedelta(days=1)
    return False


def fetch_route(origin, dest):
    """해당 노선의 직항 왕복 최저가 목록 (출발일·귀국일·1인 가격), 기피 시기 제외"""
    fares = {}
    bad = avoid_months(dest)
    for month in months_to_search():
        if int(month[5:]) in bad or (PEAKS.get(dest) and int(month[5:]) not in PEAKS[dest]):
            continue  # 기피 시기 / 성수기가 아닌 달은 조회 생략
        params = {
            "origin": origin,
            "destination": dest,
            "departure_at": month,
            "one_way": "false",
            "direct": "true" if CFG["direct_only"] else "false",
            "currency": "krw",
            "market": "kr",
            "sorting": "price",
            "unique": "false",
            "limit": 1000,
            "token": TP_TOKEN,
        }
        try:
            r = requests.get(
                "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
                params=params, headers={"Accept-Encoding": "gzip, deflate"}, timeout=30,
            )
            if r.status_code == 429:
                time.sleep(10)
                continue
            r.raise_for_status()
            items = r.json().get("data") or []
        except Exception as e:
            print(f"  ! {origin}-{dest} {month}: {e}", file=sys.stderr)
            continue

        for it in items:
            try:
                dep = date.fromisoformat(it["departure_at"][:10])
                ret = date.fromisoformat(it["return_at"][:10])
            except Exception:
                continue
            if CFG["direct_only"] and (it.get("transfers", 0) or it.get("return_transfers", 0)):
                continue
            nights = (ret - dep).days
            if not (CFG["min_nights"] <= nights <= CFG["max_nights"]) or dep <= TODAY:
                continue
            if in_avoid(dest, dep, ret) or not in_peak(dest, dep):
                continue
            key = f"{dep}|{ret}"
            price = int(it["price"])
            if key not in fares or price < fares[key]["price"]:
                fares[key] = {
                    "dep": str(dep), "ret": str(ret), "nights": nights, "price": price,
                    "airline": it.get("airline", ""), "link": it.get("link", ""),
                }
        time.sleep(0.4)
    return list(fares.values())


# ───────── 기록 & 판단 ─────────
def update_history(hist, route, fares):
    """기록 항목: [출발일, 귀국일, 1인가격, 처음본날, 마지막본날]"""
    rows = hist.setdefault(route, [])
    index = {(r[0], r[1], r[2]): r for r in rows}
    t = str(TODAY)
    for f in fares:
        k = (f["dep"], f["ret"], f["price"])
        if k in index:
            index[k][4] = t
        else:
            row = [f["dep"], f["ret"], f["price"], t, t]
            rows.append(row)
            index[k] = row
    cutoff = str(TODAY - timedelta(days=CFG["history_days"]))
    dest = route.split("-")[1]
    hist[route] = [r for r in rows if r[4] >= cutoff
                   and not in_avoid(dest, date.fromisoformat(r[0]), date.fromisoformat(r[1]))
                   and in_peak(dest, date.fromisoformat(r[0]))]


def percentile(values, p):
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def rank_pct(values, x):
    return 100 * sum(1 for v in values if v < x) / len(values)


def enough_data(rows):
    if len(rows) < CFG["min_samples"]:
        return False
    first = min(r[3] for r in rows)
    return (TODAY - date.fromisoformat(first)).days >= CFG["min_history_days"]


# ───────── 구글 항공권으로 크로스체크 (SerpApi, 무료 월 250회) ─────────
def google_check(o, d, f, checks):
    """구글 항공권: 성인2·소아3·직항·이코노미 왕복. {'level','total','range'} 또는 None"""
    key = f"{o}-{d}|{f['dep']}|{f['ret']}"
    old = checks.get(key)
    if old and old.get("day", "") >= str(TODAY - timedelta(days=3)):
        return old  # 3일 안에 확인한 건 다시 조회 안 함
    month = str(TODAY)[:7]
    if checks.get("_used", {}).get(month, 0) >= CFG.get("google_monthly_limit", 240):
        return None
    if checks.get("_today", {}).get(str(TODAY), 0) >= CFG.get("google_daily_limit", 8):
        return None
    p = CFG["passengers"]
    params = {"engine": "google_flights", "departure_id": "ICN" if o == "ICN" else o, "arrival_id": d,
              "outbound_date": f["dep"], "return_date": f["ret"], "type": 1, "travel_class": 1, "stops": 1,
              "adults": p["adult"], "children": p["child"], "currency": "KRW", "hl": "ko", "gl": "kr",
              "api_key": SERP_KEY}
    try:
        r = requests.get("https://serpapi.com/search.json", params=params, timeout=60)
        j = r.json()
    except Exception as e:
        print(f"! 구글 조회 실패 {key}: {e}")
        return None
    checks.setdefault("_used", {})[month] = checks.get("_used", {}).get(month, 0) + 1
    checks.setdefault("_today", {})[str(TODAY)] = checks.get("_today", {}).get(str(TODAY), 0) + 1
    pi = j.get("price_insights") or {}
    flights = (j.get("best_flights") or []) + (j.get("other_flights") or [])
    total = pi.get("lowest_price") or min((x.get("price", 10**12) for x in flights), default=None)
    res = {"day": str(TODAY), "level": pi.get("price_level"), "total": total,
           "range": pi.get("typical_price_range"), "error": j.get("error")}
    checks[key] = res
    print(f"구글 {key}: {res}")
    return res


LEVEL = {"low": "낮음 ✅", "typical": "보통", "high": "높음"}


# ───────── 링크 & 메시지 ─────────
def naver_link(o, d, f):
    dep, ret = f["dep"].replace("-", ""), f["ret"].replace("-", "")
    p = CFG["passengers"]
    return (f"https://flight.naver.com/flights/international/{o}-{d}-{dep}/{d}-{o}-{ret}"
            f"?adult={p['adult']}&child={p['child']}&fareType=Y")


def fmt_date(s):
    d = date.fromisoformat(s)
    return f"{d.month}/{d.day}({DOW[d.weekday()]})"


def won(n):
    return f"{n:,}원"


def build_message(deals):
    p = CFG["passengers"]
    people = p["adult"] + p["child"]
    lines = [f"✈️ <b>[항공권 봇] 성수기 특가 {len(deals)}건</b> (직항·왕복·이코노미)\n"]
    for d in deals:
        f = d["fare"]
        lines.append(
            f"<b>{d['oname']} → {d['dname']}</b> · {f['nights']}박{f['nights']+1}일\n"
            f"📅 {fmt_date(f['dep'])} ~ {fmt_date(f['ret'])}"
            + (f" · {f['airline']}" if f["airline"] else "") + "\n"
            + (f"💰 {people}인 총액 {won(int(g['total']))} (구글 항공권)\n" if (g := d.get("google")) and g.get("total")
               else f"💰 1인 {won(f['price'])} → {people}명 약 {won(f['price'] * people)}\n")
            + (f"📊 구글: 이 날짜 보통 {won(int(g['range'][0]))}~{won(int(g['range'][1]))} → {LEVEL.get(g['level'], g['level'])}\n"
               if g and g.get("range") else (f"📊 구글: {LEVEL.get(g['level'], g['level'])}\n" if g and g.get("level") else ""))
            + f"📉 성수기({d['peak']}) 가격 중 하위 {max(d['rank'], 1):.0f}% · 성수기 중간값 1인 {won(int(d['median']))}\n"
            f"🔎 <a href=\"{naver_link(d['o'], d['d'], f)}\">네이버 항공권 (성인{p['adult']}·소아{p['child']})</a>"
            + (f" · <a href=\"https://www.aviasales.com{f['link']}\">Aviasales</a>" if f["link"] else "")
            + "\n"
            + (f"   └ 같은 노선 기준가 미만 날짜 {d['more']}건 더 있음\n" if d.get("more") else "")
        )
    lines.append("※ 구글 항공권이 '낮음'이라고 판단한 것만 보내요. 좌석은 링크에서 확인하세요.")
    return "\n".join(lines)


def send(text):
    for cid in CHAT_IDS:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
        if not r.ok:
            print(f"텔레그램 전송 실패({cid}): {r.text}", file=sys.stderr)


def chunks(deals, size=6):
    for i in range(0, len(deals), size):
        yield deals[i:i + size]


def send_sample():
    """지금까지 모은 실제 가격으로 알림 양식 예시를 보냄 (기록·발송내역에는 반영 안 함)"""
    hist = load(HISTORY_FILE, {})
    deals = []
    routes = sorted((r for r in hist.items() if len(r[1]) >= 5), key=lambda x: -len(x[1]))[:3]
    for route, rows in routes:
        o, d = route.split("-")
        prices = [r[2] for r in rows]
        cheapest = min(rows, key=lambda r: r[2])
        dep, ret = date.fromisoformat(cheapest[0]), date.fromisoformat(cheapest[1])
        fare = {"dep": cheapest[0], "ret": cheapest[1], "nights": (ret - dep).days,
                "price": cheapest[2], "airline": "", "link": ""}
        deals.append({"o": o, "d": d, "oname": CFG["origins"].get(o, o),
                      "dname": CFG["destinations"].get(d, d), "fare": fare,
                      "threshold": percentile(prices, CFG["percentile"]),
                      "median": percentile(prices, 50), "rank": rank_pct(prices, cheapest[2]), "peak": "예시"})
    if not deals:
        send("아직 예시를 만들 만큼 기록이 없어요. 몇 시간 뒤에 다시 시도해 주세요.")
        return
    send("🧪 <b>[예시] 실제 알림은 이렇게 와요</b>\n(지금까지 모은 가격으로 만든 샘플이에요)\n\n" + build_message(deals))


def main():
    if os.environ.get("TEST_MESSAGE") == "true":
        hist = load(HISTORY_FILE, {})
        n = sum(len(v) for v in hist.values())
        pk = load(PEAK_FILE, {}).get("peaks", {})
        if pk:
            send("📅 <b>[항공권 봇] 목적지별 성수기</b> (최근 2년 네이버 검색량 기준)\n" + "\n".join(
                f"· {CFG['destinations'][k]}: {', '.join(f'{m}월' for m in v)}" for k, v in pk.items()))
        send(f"✅ <b>항공권 봇 연결 확인</b>\n가격 기록 {n}건 수집 중이에요.\n노선별로 기록이 충분히 쌓이면(약 1주) 특가가 뜰 때 알려드릴게요.")
        return
    if os.environ.get("SAMPLE_MESSAGE") == "true":
        send_sample()
        return
    if not TP_TOKEN:
        sys.exit("TP_TOKEN(Travelpayouts 토큰)이 없어요.")

    global PEAKS
    PEAKS = peak_months()
    hist = load(HISTORY_FILE, {})
    sent = load(SENT_FILE, {})
    checks = load(SERP_FILE, {})
    deals = []

    for o, oname in CFG["origins"].items():
        for d, dname in CFG["destinations"].items():
            route = f"{o}-{d}"
            fares = fetch_route(o, d)
            update_history(hist, route, fares)
            rows = hist[route]
            print(f"{route}: 이번 {len(fares)}건 / 누적 {len(rows)}건")
            if not fares or not enough_data(rows):
                continue

            prices = [r[2] for r in rows]
            threshold = percentile(prices, CFG["percentile"])
            median = percentile(prices, 50)
            picks = []
            for f in sorted(fares, key=lambda x: x["price"]):
                key = f"{route}|{f['dep']}|{f['ret']}"
                if f["price"] >= threshold or key in sent:
                    continue
                picks.append({"o": o, "d": d, "oname": oname, "dname": dname, "fare": f,
                              "threshold": threshold, "median": median,
                              "rank": rank_pct(prices, f["price"]), "key": key})
            if not picks:
                continue
            peak_txt = ",".join(f"{m}월" for m in PEAKS.get(d, [])) or "전체"
            # 가장 싼 후보부터 구글로 확인 → '낮음'인 것만 (노선당 최대 N건)
            confirmed = []
            for pk in picks[: CFG.get("google_checks_per_route", 2)]:
                pk["peak"] = peak_txt
                if not SERP_KEY:
                    break  # 구글 확인 없이 보내지 않음 (SERPAPI_KEY 등록 전엔 기록만 쌓음)
                g = google_check(o, d, pk["fare"], checks)
                if not g or g.get("level") != "low":
                    continue
                pk["google"] = g
                confirmed.append(pk)
                if len(confirmed) >= CFG["max_alerts_per_route"]:
                    break
            deals.extend(confirmed)

    if deals:
        deals.sort(key=lambda x: x["rank"])
        if BOT_TOKEN and CHAT_IDS:
            for part in chunks(deals):
                send(build_message(part))
        for dl in deals:
            sent[dl["key"]] = str(TODAY)
        print(f"알림 {len(deals)}건 전송")
    else:
        print("조건에 맞는 특가 없음 — 전송 안 함")

    # 지난 출발일의 '보낸 기록'은 정리
    sent = {k: v for k, v in sent.items() if k.split("|")[1] >= str(TODAY)}
    save(HISTORY_FILE, hist)
    save(SENT_FILE, sent)
    checks = {k: v for k, v in checks.items() if k.startswith("_") or k.split("|")[1] >= str(TODAY)}
    save(SERP_FILE, checks)


if __name__ == "__main__":
    main()
