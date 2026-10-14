"""
가족 항공권 특가 알림 봇 (GitHub Actions, 1시간마다 실행)

- Travelpayouts(Aviasales) 무료 API로 직항 왕복 최저가(성인 1인 기준)를 수집
- 노선별로 지난 180일 동안 모은 가격 기록에서 하위 30% 미만이면 텔레그램 알림
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
        if int(month[5:]) in bad:
            continue  # 출발 월 자체가 기피 시기면 조회 생략
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
            if in_avoid(dest, dep, ret):
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
                   and not in_avoid(dest, date.fromisoformat(r[0]), date.fromisoformat(r[1]))]


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
    lines = [f"✈️ <b>항공권 특가 {len(deals)}건</b> (직항·왕복·이코노미)\n"]
    for d in deals:
        f = d["fare"]
        lines.append(
            f"<b>{d['oname']} → {d['dname']}</b> · {f['nights']}박{f['nights']+1}일\n"
            f"📅 {fmt_date(f['dep'])} ~ {fmt_date(f['ret'])}"
            + (f" · {f['airline']}" if f["airline"] else "") + "\n"
            f"💰 1인 {won(f['price'])} → {people}명 약 {won(f['price'] * people)}\n"
            f"📉 최근 기록 하위 {d['rank']:.0f}% (기준가 {won(int(d['threshold']))} 미만, 평소 중간값 {won(int(d['median']))})\n"
            f"🔎 <a href=\"{naver_link(d['o'], d['d'], f)}\">네이버 항공권 (성인{p['adult']}·소아{p['child']})</a>"
            + (f" · <a href=\"https://www.aviasales.com{f['link']}\">Aviasales</a>" if f["link"] else "")
            + "\n"
            + (f"   └ 같은 노선 기준가 미만 날짜 {d['more']}건 더 있음\n" if d.get("more") else "")
        )
    lines.append("※ 가격은 성인 1인 기준 최근 검색 캐시예요. 소아 요금·좌석은 링크에서 확인하세요.")
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


def main():
    if not TP_TOKEN:
        sys.exit("TP_TOKEN(Travelpayouts 토큰)이 없어요.")

    hist = load(HISTORY_FILE, {})
    sent = load(SENT_FILE, {})
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
            # 노선당 가장 싼 몇 건만 보여주고, 나머지는 '외 N건'으로 묶어서 한 번에 처리
            shown = picks[: CFG["max_alerts_per_route"]]
            shown[-1]["more"] = len(picks) - len(shown)
            deals.extend(shown)
            for pk in picks[len(shown):]:
                sent[pk["key"]] = str(TODAY)

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


if __name__ == "__main__":
    main()
