#!/usr/bin/env python3
import asyncio, json, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

URL = "https://new.mse.mn/todays-trade"
OUT = Path(__file__).parent / "data" / "latest.json"
UB = timezone(timedelta(hours=8))

def num(s):
    if s is None:
        return None
    s = str(s).strip().replace("₮", "").replace("%", "").replace(",", "")
    s = s.replace("−", "-").replace("–", "-").replace("—", "")
    s = re.sub(r"\s+", "", s)
    if not s or s in {"-", "N/A", "null", "None"}:
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group()) if m else None

def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

async def extract_tables(page):
    return await page.evaluate("""
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
          headers: [...t.querySelectorAll('thead th')].map(th => th.innerText.trim()),
          rows: [...t.querySelectorAll('tbody tr')].map(tr =>
            [...tr.querySelectorAll('td')].map(td => td.innerText.trim())
          )
        }))
      };
    }
    """)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            viewport={"width": 1440, "height": 1200},
            locale="mn-MN",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
        )
        await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        extracted = None
        for _ in range(20):
            extracted = await extract_tables(page)
            tables = extracted.get("tables", [])
            if any(any(clean(c) for c in r) for t in tables for r in t.get("rows", [])):
                break
            await page.wait_for_timeout(1500)

        title = await page.title()
        await browser.close()

    tables = (extracted or {}).get("tables", [])
    securities = []
    other_tables = []

    for t in tables:
        cat = clean(t.get("category")) or f"Хүснэгт {t.get('index', 0)+1}"
        headers = [clean(h) for h in t.get("headers", [])]
        raw_rows = []

        for r in t.get("rows", []):
            r = [clean(x) for x in r]
            if not any(r):
                continue
            raw_rows.append(r)

            # MSE regular security tables have 15 body cells:
            # 0 symbol, 1 open, 2 high, 3 low, 4 last, 5 prev close,
            # 6 close, 7 change, 8 change %, 9 volume, 10 turnover,
            # 11 bid qty, 12 bid price, 13 ask qty, 14 ask price.
            if len(r) < 11:
                continue

            symbol = r[0]
            if not symbol or not re.fullmatch(r"[A-Za-z0-9._/-]{1,80}", symbol):
                continue

            securities.append({
                "category": cat,
                "symbol": symbol,
                "open": num(r[1]),
                "high": num(r[2]),
                "low": num(r[3]),
                "last": num(r[4]),
                "prev_close": num(r[5]),
                "close": num(r[6]),
                "change": num(r[7]),
                "change_pct": num(r[8]),
                "volume": num(r[9]),
                "turnover": num(r[10]),
            })

        if raw_rows:
            other_tables.append({"category": cat, "headers": headers, "rows": raw_rows})

    dedup = {}
    for x in securities:
        key = (x["category"], x["symbol"])
        old = dedup.get(key)
        if old is None or (x.get("turnover") or 0) > (old.get("turnover") or 0):
            dedup[key] = x
    securities = list(dedup.values())

    if not securities:
        raise RuntimeError("МХБ-ийн өнөөдрийн арилжааны хүснэгтээс үнэт цаасны мөр илрээгүй.")

    traded = [x for x in securities if (x.get("volume") or 0) > 0 or (x.get("turnover") or 0) > 0]
    if not traded:
        raise RuntimeError("Арилжаатай үнэт цаас илрээгүй.")

    adv = sum(1 for x in traded if (x.get("change_pct") or 0) > 0)
    dec = sum(1 for x in traded if (x.get("change_pct") or 0) < 0)
    flat = sum(1 for x in traded if x.get("change_pct") is not None and x.get("change_pct") == 0)
    total_turnover = sum((x.get("turnover") or 0) for x in traded)
    total_volume = sum((x.get("volume") or 0) for x in traded)

    def top(field, reverse=True, limit=10):
        xs = [x for x in traded if x.get(field) is not None]
        return sorted(xs, key=lambda z: z.get(field) or 0, reverse=reverse)[:limit]

    now = datetime.now(UB)
    source_date = clean((extracted or {}).get("dateInput")) or now.date().isoformat()
    payload = {
        "source": URL,
        "source_title": title,
        "updated_at": now.isoformat(timespec="seconds"),
        "date": source_date,
        "summary": {
            "securities": len(traded),
            "advancers": adv,
            "decliners": dec,
            "unchanged": flat,
            "volume": total_volume,
            "turnover": total_turnover,
        },
        "top_gainers": top("change_pct", True),
        "top_losers": top("change_pct", False),
        "top_turnover": top("turnover", True),
        "securities": sorted(traded, key=lambda x: (-(x.get("turnover") or 0), x["symbol"])),
        "raw_tables": other_tables,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {source_date} | {len(traded)} traded securities | turnover={total_turnover:,.0f}")

if __name__ == "__main__":
    asyncio.run(main())
