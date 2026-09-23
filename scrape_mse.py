#!/usr/bin/env python3
import asyncio,json,re
from datetime import datetime,timezone,timedelta
from pathlib import Path
from playwright.async_api import async_playwright

URL="https://open.mse.mn/securities"
OUT=Path(__file__).parent/"data"/"latest.json"
UB=timezone(timedelta(hours=8))

def n(s):
    if s is None:return None
    s=str(s).replace("₮","").replace("%","").replace(",","").replace("−","-").replace("–","-")
    m=re.search(r"[+-]?\d+(?:\.\d+)?",s)
    return float(m.group()) if m else None

def parse(text,it):
    a=[x.strip() for x in text.splitlines() if x.strip()]
    try:i=a.index(it["symbol"])
    except ValueError:i=0
    price=chg=pct=None
    for x in a[i:i+30]:
        if price is None and "₮" in x:
            price=n(x);continue
        if price is not None and "%" in x:
            m=re.search(r"([+-]?\d[\d,.]*)\s*\(([+-]?\d[\d,.]*)%\)",x)
            if m: chg=n(m.group(1));pct=n(m.group(2))
            else:
                m=re.search(r"([+-]?\d[\d,.]*)%",x)
                if m:pct=n(m.group(1))
            break
    if price is None:return None
    return {"category":it["category"],"symbol":it["symbol"],"name":it["name"],
            "open":None,"high":None,"low":None,"last":price,
            "prev_close":price-chg if chg is not None else None,"close":price,
            "change":chg,"change_pct":pct,"volume":None,"turnover":None,
            "detail_url":it["url"]}

async def worker(browser,q,out):
    p=await browser.new_page()
    while True:
        it=await q.get()
        if it is None:q.task_done();break
        try:
            await p.goto(it["url"],wait_until="domcontentloaded",timeout=45000)
            await p.wait_for_timeout(700)
            r=parse(await p.locator("body").inner_text(),it)
            if r:out.append(r)
        except Exception as e: print("WARN",it["symbol"],e)
        q.task_done()
    await p.close()

async def main():
    async with async_playwright() as pw:
        b=await pw.chromium.launch(headless=True)
        p=await b.new_page()
        await p.goto(URL,wait_until="domcontentloaded",timeout=90000)
        await p.wait_for_timeout(3000)
        items=await p.evaluate("""() => {
          const cats=['I ангилал','II ангилал','III ангилал'],out=[];
          [...document.querySelectorAll('table')].slice(0,3).forEach((t,ti)=>{
            [...t.querySelectorAll('tbody tr')].forEach(tr=>{
              const td=[...tr.querySelectorAll('td')].map(x=>x.innerText.trim());
              const a=tr.querySelector('a[href^="/securities/"]');
              if(a&&td.length>=3&&/^[A-Za-z0-9._/-]{1,40}$/.test(td[1]))
                out.push({symbol:td[1],name:td[2],category:cats[ti],url:new URL(a.getAttribute('href'),location.origin).href});
            });
          }); return out;
        }""")
        await p.close()
        items=list({x["symbol"]:x for x in items}.values())
        print("Listed equities:",len(items))
        if not items: raise RuntimeError("MSE Open жагсаалт олдсонгүй")
        q=asyncio.Queue();out=[]
        for x in items:await q.put(x)
        ws=[asyncio.create_task(worker(b,q,out)) for _ in range(8)]
        for _ in ws:await q.put(None)
        await q.join();await asyncio.gather(*ws);await b.close()
    if len(out)<10:raise RuntimeError(f"Ханштай үнэт цаас хэт цөөн: {len(out)}")
    out.sort(key=lambda x:x["symbol"])
    changed=[x for x in out if x["change_pct"] is not None]
    adv=sum((x["change_pct"] or 0)>0 for x in out)
    dec=sum((x["change_pct"] or 0)<0 for x in out)
    flat=sum(x["change_pct"]==0 for x in out)
    now=datetime.now(UB)
    data={"source":URL,"source_title":"MSE Open","updated_at":now.isoformat(timespec="seconds"),
          "date":now.date().isoformat(),"data_mode":"official_open_mse_prices",
          "summary":{"securities":len(out),"advancers":adv,"decliners":dec,"unchanged":flat,"volume":None,"turnover":None},
          "top_gainers":sorted(changed,key=lambda x:x["change_pct"],reverse=True)[:10],
          "top_losers":sorted(changed,key=lambda x:x["change_pct"])[:10],
          "top_turnover":sorted(changed,key=lambda x:abs(x["change_pct"]),reverse=True)[:10],
          "securities":out}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    print("OK:",len(out),"up",adv,"down",dec,"flat",flat)

if __name__=="__main__":asyncio.run(main())
