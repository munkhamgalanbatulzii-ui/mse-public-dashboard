#!/usr/bin/env python3
import asyncio, json, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

URLS = [
    "https://new.mse.mn/todays-trade",
    "https://new.mse.mn/trade-daily-report",
]
OUT = Path(__file__).parent / "data" / "latest.json"
UB = timezone(timedelta(hours=8))

def num(s):
    if s is None or isinstance(s, bool):
        return None
    s = str(s).strip().replace("₮", "").replace("%", "").replace(",", "")
    s = s.replace("−", "-").replace("–", "-").replace("—", "")
    s = re.sub(r"\s+", "", s)
    if not s or s.lower() in {"-", "n/a", "null", "none"}:
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group()) if m else None

def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()

def nk(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())

ALIASES = {
    "symbol": ["symbol","ticker","code","securitycode","symbolcode","seccode","shortname"],
    "open": ["open","openprice","openingprice"],
    "high": ["high","highprice","highestprice","maxprice"],
    "low": ["low","lowprice","lowestprice","minprice"],
    "last": ["last","lastprice","lasttradedprice","lasttradeprice"],
    "prev_close": ["prevclose","previousclose","previouscloseprice","prevcloseprice","yesterdayclose"],
    "close": ["close","closeprice","closingprice"],
    "change": ["change","pricechange","changediff"],
    "change_pct": ["changepct","changepercent","changepercentage","percentchange","changepercentvalue","percentagechange"],
    "volume": ["volume","qty","quantity","tradevolume","totalvolume","tradedvolume","totalqty","tradeqty"],
    "turnover": ["turnover","amount","tradeamount","totalamount","value","totalvalue","tradedvalue","dealamount","tradevalue"],
    "category": ["category","board","market","type","securitytype","instrumenttype"],
}
ALIAS_N = {k: {nk(v) for v in vals} for k, vals in ALIASES.items()}

def get_alias(record, field):
    if not isinstance(record, dict):
        return None
    for k, v in record.items():
        if nk(k) in ALIAS_N[field]:
            return v
    return None

def record_to_security(record, category_hint="Market"):
    sym = clean(get_alias(record, "symbol"))
    if not sym or len(sym) > 80 or not re.fullmatch(r"[A-Za-z0-9._/-]+", sym):
        return None
    close = num(get_alias(record, "close"))
    last = num(get_alias(record, "last"))
    prev = num(get_alias(record, "prev_close"))
    chg = num(get_alias(record, "change"))
    pct = num(get_alias(record, "change_pct"))
    if pct is None and prev not in (None, 0) and (close is not None or last is not None):
        px = close if close is not None else last
        pct = (px / prev - 1) * 100
    if chg is None and prev is not None and (close is not None or last is not None):
        px = close if close is not None else last
        chg = px - prev
    cat = clean(get_alias(record, "category")) or category_hint
    return {
        "category": cat,
        "symbol": sym,
        "open": num(get_alias(record, "open")),
        "high": num(get_alias(record, "high")),
        "low": num(get_alias(record, "low")),
        "last": last,
        "prev_close": prev,
        "close": close,
        "change": chg,
        "change_pct": pct,
        "volume": num(get_alias(record, "volume")),
        "turnover": num(get_alias(record, "turnover")),
    }

def discover_records(obj, path="root", depth=0):
    if depth > 8:
        return []
    found = []
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        mapped = [record_to_security(x, path.split(".")[-1]) for x in dicts]
        mapped = [x for x in mapped if x]
        if mapped:
            found.extend(mapped)
        for i, x in enumerate(obj[:30]):
            if isinstance(x, (dict, list)):
                found.extend(discover_records(x, path + f"[{i}]", depth + 1))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                found.extend(discover_records(v, path + "." + str(k), depth + 1))
    return found

async def extract_tables(page):
    return await page.evaluate(r"""
    () => {
      function headingFor(table) {
        let node = table;
        while (node && node !== document.body) {
          let prev = node.previousElementSibling;
          while (prev) {
            if (/^H[1-6]$/.test(prev.tagName)) return prev.innerText.trim();
            const hs = prev.querySelectorAll ? prev.querySelectorAll('h1,h2,h3,h4,h5,h6') : [];
            if (hs.length) return hs[hs.length - 1].innerText.trim();
            prev = prev.previousElementSibling;
          }
          node = node.parentElement;
        }
        return '';
      }
      const dateInput = [...document.querySelectorAll('input')]
        .map(x => x.value || '')
        .find(v => /^\d{4}-\d{2}-\d{2}$/.test(v)) || '';
      return {
        dateInput,
        tables: [...document.querySelectorAll('table')].map((t, i) => ({
          index: i,
          category: headingFor(t),
          rows: [...t.querySelectorAll('tbody tr')].map(tr =>
            [...tr.querySelectorAll('td')].map(td => td.innerText.trim())
          )
        }))
      };
    }
    """)

def securities_from_tables(tables):
    out = []
    for t in tables:
        cat = clean(t.get("category")) or f"Table {t.get('index', 0)+1}"
        for r in t.get("rows", []):
            r = [clean(x) for x in r]
            if len(r) < 11:
                continue
            symbol = r[0]
            if not symbol or not re.fullmatch(r"[A-Za-z0-9._/-]{1,80}", symbol):
                continue
            out.append({
                "category": cat, "symbol": symbol,
                "open": num(r[1]), "high": num(r[2]), "low": num(r[3]),
                "last": num(r[4]), "prev_close": num(r[5]), "close": num(r[6]),
                "change": num(r[7]), "change_pct": num(r[8]),
                "volume": num(r[9]), "turnover": num(r[10]),
            })
    return out

async def scrape():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        all_network = []
        best_date = ""
        title = ""
        source_url = URLS[0]

        for url in URLS:
            page = await browser.new_page(
                viewport={"width": 1440, "height": 1200},
                locale="mn-MN",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
            )
            tasks = []

            async def capture(resp):
                try:
                    ct = (resp.headers.get("content-type") or "").lower()
                    u = resp.url.lower()
                    if "json" in ct or any(w in u for w in ("api", "trade", "market", "security", "stock")):
                        data = await resp.json()
                        all_network.append((resp.url, data))
                except Exception:
                    pass

            page.on("response", lambda resp: tasks.append(asyncio.create_task(capture(resp))))
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=25000)
                except Exception:
                    pass
                await page.wait_for_timeout(5000)
                extracted = await extract_tables(page)
                title = await page.title()
                best_date = extracted.get("dateInput") or best_date
                table_sec = securities_from_tables(extracted.get("tables", []))
                if table_sec:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await page.close()
                    await browser.close()
                    return table_sec, best_date, title, url, all_network
            except Exception as e:
                print(f"WARN page {url}: {e}")
            await asyncio.gather(*tasks, return_exceptions=True)
            await page.close()

        await browser.close()

    network_sec = []
    for url, payload in all_network:
        got = discover_records(payload, "api")
        if got:
            print(f"JSON candidate: {url} -> {len(got)} securities")
            network_sec.extend(got)
    return network_sec, best_date, title, source_url, all_network

async def main():
    securities, source_date, title, source_url, network = await scrape()

    dedup = {}
    for x in securities:
        key = x["symbol"]
        old = dedup.get(key)
        score = sum(v is not None for k, v in x.items() if k not in ("symbol","category"))
        oldscore = -1 if old is None else sum(v is not None for k, v in old.items() if k not in ("symbol","category"))
        if old is None or score > oldscore or (x.get("turnover") or 0) > (old.get("turnover") or 0):
            dedup[key] = x
    securities = list(dedup.values())

    if not securities:
        print("Captured JSON endpoints:")
        for u, _ in network[:40]:
            print(" -", u)
        raise RuntimeError("МХБ-ийн public page/API response-оос үнэт цаасны дата илрээгүй.")

    traded = [x for x in securities if (x.get("volume") or 0) > 0 or (x.get("turnover") or 0) > 0]
    if not traded:
        traded = [x for x in securities if x.get("close") is not None or x.get("last") is not None]
    if not traded:
        raise RuntimeError("Ханш/арилжааны утгатай үнэт цаас илрээгүй.")

    adv = sum(1 for x in traded if (x.get("change_pct") or 0) > 0)
    dec = sum(1 for x in traded if (x.get("change_pct") or 0) < 0)
    flat = sum(1 for x in traded if x.get("change_pct") is not None and x.get("change_pct") == 0)
    total_turnover = sum((x.get("turnover") or 0) for x in traded)
    total_volume = sum((x.get("volume") or 0) for x in traded)

    def top(field, reverse=True, limit=10):
        xs = [x for x in traded if x.get(field) is not None]
        return sorted(xs, key=lambda z: z.get(field) or 0, reverse=reverse)[:limit]

    now = datetime.now(UB)
    source_date = clean(source_date) or now.date().isoformat()
    payload = {
        "source": source_url,
        "source_title": title,
        "updated_at": now.isoformat(timespec="seconds"),
        "date": source_date,
        "summary": {
            "securities": len(traded), "advancers": adv, "decliners": dec,
            "unchanged": flat, "volume": total_volume, "turnover": total_turnover,
        },
        "top_gainers": top("change_pct", True),
        "top_losers": top("change_pct", False),
        "top_turnover": top("turnover", True),
        "securities": sorted(traded, key=lambda x: (-(x.get("turnover") or 0), x["symbol"])),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {source_date} | {len(traded)} securities | turnover={total_turnover:,.0f}")

if __name__ == "__main__":
    asyncio.run(main())
