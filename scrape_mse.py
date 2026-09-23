#!/usr/bin/env python3
import json, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

LIST_URL = "https://open.mse.mn/securities"
OUT = Path(__file__).parent / "data" / "latest.json"
UB = timezone(timedelta(hours=8))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Accept-Language": "mn,en;q=0.8",
}

def num(s):
    if s is None:
        return None
    s = str(s).replace("₮", "").replace("%", "").replace(",", "").strip()
    s = s.replace("−", "-").replace("–", "-")
    m = re.search(r"[+-]?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None

def get(url, timeout=20):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text

def parse_listing():
    html = get(LIST_URL)
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    categories = ["I ангилал", "II ангилал", "III ангилал"]
    items = []

    # open.mse.mn-ийн эхний 3 хүснэгт нь хувьцааны I/II/III ангилал.
    for ti, table in enumerate(tables[:3]):
        cat = categories[ti] if ti < len(categories) else f"Ангилал {ti+1}"
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            links = tr.find_all("a", href=re.compile(r"^/securities/\d+"))
            if not links:
                continue
            href = links[0].get("href")
            symbol = tds[1].get_text(" ", strip=True)
            name = tds[2].get_text(" ", strip=True)
            if not symbol or not re.fullmatch(r"[A-Za-z0-9._/-]{1,40}", symbol):
                continue
            items.append({
                "symbol": symbol,
                "name": name,
                "category": cat,
                "url": "https://open.mse.mn" + href,
            })

    # duplicate links-ийг арилгана
    dedup = {}
    for x in items:
        dedup[x["symbol"]] = x
    return list(dedup.values())

def parse_security(item):
    try:
        html = get(item["url"])
        soup = BeautifulSoup(html, "html.parser")
        lines = [x.strip() for x in soup.stripped_strings if x.strip()]

        # Симболын дараах эхний ₮ мөр нь тухайн үеийн ханш,
        # дараах (%) мөр нь абсолют ба хувийн өөрчлөлт.
        try:
            si = lines.index(item["symbol"])
        except ValueError:
            si = 0

        price = None
        change = None
        change_pct = None

        for line in lines[si:si+25]:
            if price is None and "₮" in line:
                price = num(line)
                continue
            if price is not None and "%" in line:
                m = re.search(r"([+-]?\d[\d,.]*)\s*\(([+-]?\d[\d,.]*)%\)", line)
                if m:
                    change = num(m.group(1))
                    change_pct = num(m.group(2))
                    break
                # Зарим хуудас зөвхөн %-ийг харуулж болно
                pm = re.search(r"([+-]?\d[\d,.]*)%", line)
                if pm:
                    change_pct = num(pm.group(1))
                    break

        if price is None:
            return None

        prev_close = price - change if change is not None else None
        return {
            "category": item["category"],
            "symbol": item["symbol"],
            "name": item["name"],
            "open": None,
            "high": None,
            "low": None,
            "last": price,
            "prev_close": prev_close,
            "close": price,
            "change": change,
            "change_pct": change_pct,
            "volume": None,
            "turnover": None,
            "detail_url": item["url"],
        }
    except Exception as e:
        print(f"WARN {item['symbol']}: {e}")
        return None

def main():
    items = parse_listing()
    print(f"Listed equities: {len(items)}")
    if not items:
        raise RuntimeError("open.mse.mn-ээс хувьцааны жагсаалт олдсонгүй")

    securities = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(parse_security, x): x for x in items}
        for fut in as_completed(futs):
            x = fut.result()
            if x:
                securities.append(x)

    if not securities:
        raise RuntimeError("open.mse.mn-ээс ханшийн мэдээлэл татагдсангүй")

    securities.sort(key=lambda x: x["symbol"])
    adv = sum(1 for x in securities if (x.get("change_pct") or 0) > 0)
    dec = sum(1 for x in securities if (x.get("change_pct") or 0) < 0)
    flat = sum(1 for x in securities if x.get("change_pct") == 0)

    def by_change(reverse):
        xs = [x for x in securities if x.get("change_pct") is not None]
        return sorted(xs, key=lambda z: z["change_pct"], reverse=reverse)[:10]

    # Turnover feed байхгүй үед баруун watchlist-ийг хамгийн их хөдөлгөөнтэй
    # үнэт цаасаар дүүргэнэ. turnover утгыг зохиож бөглөхгүй.
    active = sorted(
        [x for x in securities if x.get("change_pct") is not None],
        key=lambda z: abs(z.get("change_pct") or 0),
        reverse=True,
    )[:10]

    now = datetime.now(UB)
    payload = {
        "source": LIST_URL,
        "source_title": "MSE Open",
        "updated_at": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "data_mode": "official_open_mse_prices",
        "summary": {
            "securities": len(securities),
            "advancers": adv,
            "decliners": dec,
            "unchanged": flat,
            "volume": None,
            "turnover": None,
        },
        "top_gainers": by_change(True),
        "top_losers": by_change(False),
        "top_turnover": active,
        "securities": securities,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {len(securities)} equities | up={adv} down={dec} flat={flat}")

if __name__ == "__main__":
    main()
