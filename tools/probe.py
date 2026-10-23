import os, requests, json
T=os.environ["TP_TOKEN"]; H={"X-Access-Token":T,"Accept-Encoding":"gzip"}
def g(path,**p):
    r=requests.get("https://api.travelpayouts.com"+path,params=p,headers=H,timeout=30)
    d=r.json() if r.headers.get("content-type","").startswith("application/json") else r.text[:300]
    return r.status_code,d
tests=[
 ("/aviasales/v3/get_latest_prices",dict(origin="ICN",destination="DAD",period_type="year",beginning_of_period="2025-01-01",one_way="false",currency="krw",limit=1000,sorting="price")),
 ("/aviasales/v3/get_latest_prices",dict(origin="ICN",destination="DAD",period_type="month",beginning_of_period="2026-01-01",one_way="false",currency="krw",limit=1000)),
 ("/v2/prices/month-matrix",dict(origin="ICN",destination="DAD",month="2026-01-01",currency="krw",show_to_affiliates="false")),
 ("/aviasales/v3/prices_for_dates",dict(origin="ICN",destination="DAD",departure_at="2026-01",one_way="false",currency="krw",limit=100)),
 ("/v1/prices/calendar",dict(origin="ICN",destination="DAD",depart_date="2026-01",calendar_type="departure_date",currency="krw")),
 ("/aviasales/v3/grouped_prices",dict(origin="ICN",destination="DAD",group_by="departure_at",departure_at="2026-01",currency="krw")),
]
for path,p in tests:
    try:
        s,d=g(path,**p)
        items = d.get("data") if isinstance(d,dict) else None
        n = len(items) if isinstance(items,(list,dict)) else None
        sample = (list(items.values())[:2] if isinstance(items,dict) else items[:2]) if items else d
        print("=====",path,p.get("beginning_of_period") or p.get("month") or p.get("departure_at") or p.get("depart_date"),s,"count",n)
        print(json.dumps(sample,ensure_ascii=False)[:700])
    except Exception as e: print("ERR",path,e)
