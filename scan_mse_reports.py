#!/usr/bin/env python3
"""Read-only MSE I-category bank/NBFI report discovery.

Never overwrites financial statements from unverified OCR, narrative text or link titles.
Runs in GitHub Actions; resulting JSON is a provenance/discovery index, not audited numbers.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "financial" / "data" / "issuer_report_watch.json"
UB = timezone(timedelta(hours=8))
ISSUERS = {
    "KHAN": ("563", "Хаан банк ХК"),
    "GLMT": ("562", "Голомт банк ХК"),
    "XAC": ("568", "Хас Банк"),
    "SBM": ("564", "Төрийн Банк ХК"),
    "TDB": ("567", "Худалдаа хөгжлийн банк"),
    "INV": ("553", "Инвескор ББСБ ХК"),
    "LEND": ("545", "ЛэндМН ББСБ ХК"),
    "ADB": ("550", "Ард кредит ББСБ ХК"),
    "SEND": ("561", "Сэндли ББСБ ХК"),
}
SITES = ["https://mse.mn/company-reports", "https://new.mse.mn/issuers-hub"]
TABS = ["Хувьцаат компаниудын санхүүгийн тайлан", "Үйл ажиллагааны тайлан"]
LINK_PATHS = ("activityreport", "finance", "report", "pdf", "attachment", "financial", "upload")
TIMETABLE = re.compile(r"(2026|2027|2028|20[2-3]\d)")
OUT.parent.mkdir(parents=True,exist_ok=True)

def allowed_link(base: str, url: str) -> str | None:
    if not url or url.startswith(("javascript:", "data:", "mailto:")):
        return None
    joined = urljoin(base, url)
    p = urlparse(joined)
    if p.scheme != "https":
        return None
    return joined[:1200]

def discover_site(browser, url):
    data = {"url": url, "visited": False, "tabs": [], "errors": [], "sources": []}
    page = browser.new_page(locale="mn-MN", viewport={"width": 1440, "height": 900})
    try:
        response = page.goto(url,wait_until="domcontentloaded",timeout=45000)
        data["httpStatus"] = response.status if response else None
        page.wait_for_timeout(2300)
        data["visited"] = True

        def collect(tab):
            all_rows = page.locator("table tr").evaluate_all(
                """els => els.slice(0,2500).map(el=>({
                    text:(el.innerText||'').replace(/\\s+/g,' ').slice(0,1100),
                    html:(el.outerHTML||'').slice(0,5500),
                    links:[...el.querySelectorAll('a[href]')].map(a=>({url:a.href,text:a.innerText||''}))
                }))"""
            )
            all_links = page.locator("a[href]").evaluate_all(
                """els=>els.slice(0,3500).map(a=>({url:a.href,text:(a.innerText||'').slice(0,180)}))"""
            )
            for symbol, (code, name) in ISSUERS.items():
                matches=[]
                for r in all_rows:
                    t=r["text"]
                    if re.search(r"\b"+re.escape(symbol)+r"\b",t,re.I) or code in t.split():
                        links=[allowed_link(url,x["url"]) for x in r["links"]]
                        links=[x for x in links if x]
                        matches.append({"text":t[:650],"urls":links[:8],
                                        "hasAction":("onclick" in r["html"] or "download" in r["html"])})
                if matches:
                    data["sources"].append({"tab":tab,"symbol":symbol,"matches":matches[:30]})
            links=[]
            for x in all_links:
                link=allowed_link(url,x["url"])
                if link and any(term in link.lower() for term in LINK_PATHS):
                    links.append({"url":link,"text":x["text"][:100]})
            controls=page.locator("select, input[type=search], input[type=text]").evaluate_all(
                """els=>els.slice(0,22).map(el=>({
                    tag:el.tagName,id:el.id,name:el.name,placeholder:el.placeholder||'',
                    options:el.tagName==='SELECT'?[...el.options].slice(0,12).map(x=>({value:x.value,text:x.text})):[],
                    html:(el.outerHTML||'').slice(0,450)
                }))"""
            )
            data["tabs"].append({"name":tab,"rows":len(all_rows),
                                 "matchingIssuerRows":sum(1 for r in data["sources"] if r["tab"]==tab),
                                 "sampleRows":[r["text"][:260] for r in all_rows[:8]],
                                 "controls":controls,
                                 "reportLinks":links[:100],
                                 "navigation":page.locator("button, nav a, [role=button]").evaluate_all(
                                      """els=>els.slice(-80).map(el=>({
                                           text:(el.innerText||el.getAttribute('aria-label')||'').slice(0,60),
                                           disabled:el.disabled||false,html:el.outerHTML.slice(0,360)
                                      })).filter(x=>x.text)""")[:50]})
        collect("Анхны хуудас")
        if "issuers-hub" in url:
            try:
                page.get_by_role("button",name="Санхүү үйл ажиллагааны мэдээлэл",exact=True).first.click(timeout=7000)
                page.wait_for_timeout(3000)
                collect("Шинэ санхүү, үйл ажиллагааны мэдээ")
            except Exception as err:
                data["errors"].append(f"Financial disclosure filter: {type(err).__name__}: {str(err)[:120]}")
            data["pageTitle"]=page.title()
            data["finalURL"]=page.url
            return data
        if page.get_by_text("Үйл ажиллагааны тайлан",exact=True).count()==0:
            page.wait_for_timeout(4500)
        for tab in TABS:
            try:
                nodes=page.get_by_text(tab,exact=True).all()
                clicked=False
                for node in nodes[:6]:
                    try:
                        if node.is_visible():
                            node.click(timeout=8000)
                            page.wait_for_timeout(2800)
                            clicked=True
                            break
                    except Exception:
                        pass
                if not clicked:
                    data["errors"].append(f"Tab inaccessible: {tab}")
                collect(tab)
            except Exception as err:
                data["errors"].append(f"{tab}: {type(err).__name__}: {str(err)[:140]}")
        data["pageTitle"]=page.title()
        data["finalURL"]=page.url
        return data
    except Exception as err:
        data["errors"].append(f"{type(err).__name__}: {str(err)[:250]}")
        return data
    finally:
        page.close()

def main():
    out = {
        "scannedAt": datetime.now(UB).isoformat(timespec="seconds"),
        "mode": "discovery_only_no_automatic_financial_overwrite",
        "note": "Only newly published report locations are discovered. Figures require checked source/period/units before use.",
        "issuerCount":len(ISSUERS),
        "issuers": {s:{"code":v[0],"name":v[1],"reports":[]} for s,v in ISSUERS.items()},
        "sites":[]
    }
    with sync_playwright() as play:
        browser=play.chromium.launch(headless=True,args=["--no-sandbox"])
        try:
            for url in SITES:
                data=discover_site(browser,url)
                out["sites"].append(data)
                for found in data["sources"]:
                    symbol=found["symbol"]
                    for row in found["matches"]:
                        if len(out["issuers"][symbol]["reports"])>=50: break
                        out["issuers"][symbol]["reports"].append({
                            "tab": found["tab"], "sourcePage":data["url"],
                            "text":row["text"],"urls":row["urls"],
                            "hasAction":row["hasAction"],
                        })
        finally:
            browser.close()
    previous = {}
    if OUT.exists():
        try:
            previous=json.loads(OUT.read_text(encoding="utf-8"))
            for symbol,record in out["issuers"].items():
                historic=previous.get("issuers",{}).get(symbol,{}).get("reports",[])
                current={json.dumps(z,ensure_ascii=False,sort_keys=True) for z in record["reports"]}
                for row in historic:
                    key=json.dumps(row,ensure_ascii=False,sort_keys=True)
                    if key not in current and len(record["reports"])<50:
                        record["reports"].append(row)
                        current.add(key)
        except (OSError,ValueError,TypeError):
            pass
    if previous and previous.get("scannedAt","")[:10] == out["scannedAt"][:10]:
        old_sig={symbol:{json.dumps(row,ensure_ascii=False,sort_keys=True) for row in obj.get("reports",[])}
                 for symbol,obj in previous.get("issuers",{}).items()}
        new_sig={symbol:{json.dumps(row,ensure_ascii=False,sort_keys=True) for row in obj.get("reports",[])}
                 for symbol,obj in out["issuers"].items()}
        if old_sig == new_sig:
            print("NO NEW ISSUER REPORTS (latest scan succeeded); not committing duplicate daily data")
            return
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"scannedAt":out["scannedAt"],"sites":[{"url":s["url"],"http":s.get("httpStatus"),"tabs":s["tabs"] and [{"name":t["name"],"rows":t["rows"],"matches":t["matchingIssuerRows"]} for t in s["tabs"]],"errors":s["errors"]} for s in out["sites"]],
                      "perIssuer":{s:len(x["reports"]) for s,x in out["issuers"].items()},
                      "diagnostics":{site["url"]: [{"name":t["name"],"rows":t["sampleRows"],"controls":t["controls"],"navigation":t["navigation"][:15]} for t in site["tabs"]] for site in out["sites"]}},ensure_ascii=False))

if __name__ == "__main__":
    main()
