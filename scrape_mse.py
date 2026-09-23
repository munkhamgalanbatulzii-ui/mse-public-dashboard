#!/usr/bin/env python3
import asyncio, json, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

URL = "https://new.mse.mn/trade-daily-report"
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


def norm_header(s):
    s = clean(s).lower()
    s = s.replace("(%)", "%")
    return s


HEADER_ALIASES = {
    "symbol": {"симбол", "symbol"},
    "open": {"нээлт", "нээлтийн ханш", "open"},
    "high": {"дээд", "дээд ханш", "high"},
    "low": {"доод", "доод ханш", "low"},
    "prev_close": {"өмнөх өдрийн хаалт", "өмнөх хаалт", "previous close", "prev close"},
    "close": {"хаалт", "хаалтын ханш", "close"},
    "change": {"өөрчлөлт", "change"},
    "change_pct": {"өөрчлөлт %", "өөрчлөлт (%)", "change %", "change (%)"},
    "volume": {"тоо ширхэг", "ширхэг", "volume", "qty", "quantity"},
    "turnover": {"үнийн дүн", "дүн", "turnover", "amount", "value"},
}


def header_index(headers, key):
    aliases = HEADER_ALIASES[key]
    for i, h in enumerate(headers):
        if norm_header(h) in aliases:
            return i
    return None


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
        .find(v => /^\\d{4}-\\d{2}-\\d{2}$/.test(v)) || '';
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
        page = await browser.new_page(viewport={"width": 1440, "height": 1200}, locale="mn-MN")
        await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        extracted = None
        for _ in range(15):
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

        idx = {k: header_index(headers, k) for k in HEADER_ALIASES}
        is_security_table = (
            idx["symbol"] is not None
            and idx["close"] is not None
            and idx["change_pct"] is not None
            and idx["volume"] is not None
            and idx["turnover"] is not None
        )

        for r in t.get("rows", []):
            r = [clean(x) for x in r]
            if not any(r):
                continue
            raw_rows.append(r)

            if not is_security_table:
                continue

            def cell(key):
                i = idx[key]
                return r[i] if i is not None and i < len(r) else None

            symbol = clean(cell("symbol"))
            if not symbol or not re.fullmatch(r"[A-Za-z0-9._/-]{1,60}", symbol):
                continue

            securities.append({
                "category": cat,
                "symbol": symbol,
                "open": num(cell("open")),
                "high": num(cell("high")),
                "low": num(cell("low")),
                "prev_close": num(cell("prev_close")),
                "close": num(cell("close")),
                "change": num(cell("change")),
                "change_pct": num(cell("change_pct")),
                "volume": num(cell("volume")),
                "turnover": num(cell("turnover")),
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
        raise RuntimeError("МХБ-ийн өдрийн тайлангаас үнэт цаасны мөр илрээгүй; өмнөх нийтлэгдсэн dashboard хэвээр үлдэнэ.")

    traded = [x for x in securities if (x.get("volume") or 0) > 0 or (x.get("turnover") or 0) > 0]
    if not traded:
        raise RuntimeError("Арилжаатай үнэт цаас илрээгүй; өмнөх нийтлэгдсэн dashboard хэвээр үлдэнэ.")

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
