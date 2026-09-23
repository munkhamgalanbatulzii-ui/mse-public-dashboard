#!/usr/bin/env python3
import json,re
from datetime import datetime,timezone,timedelta
from pathlib import Path
import requests
from bs4 import BeautifulSoup

URL="https://stock.bbe.mn/Home/TopIndex"
OUT=Path(__file__).parent/"data"/"latest.json"
UB=timezone(timedelta(hours=8))
HEAD={"User-Agent":"Mozilla/5.0","Accept-Language":"mn,en;q=0.8"}

def n(s):
    if s is None:return None
    s=str(s).replace("₮","").replace("%","").replace(",","").replace("−","-").replace("–","-").strip()
    m=re.search(r"[+-]?\d+(?:\.\d+)?",s)
    return float(m.group()) if m else None

def rows(table):
    out=[]
    for tr in table.select("tbody tr"):
        td=[x.get_text(" ",strip=True) for x in tr.select("td")]
        if len(td)<6: continue
        sym=td[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,40}",sym): continue
        out.append({
            "category":"МХБ","symbol":sym,"name":sym,
            "open":None,"high":None,"low":None,
            "last":n(td[1]),"close":n(td[1]),"change":n(td[2]),
            "change_pct":n(td[3]),"volume":n(td[4]),"turnover":n(td[5]),
        })
    return out

def main():
    r=requests.get(URL,headers=HEAD,timeout=30)
    r.raise_for_status()
    soup=BeautifulSoup(r.text,"html.parser")
    tables=soup.find_all("table")
    if len(tables)<4: raise RuntimeError(f"Ханшийн хүснэгт олдсонгүй: {len(tables)}")

    top_turn=rows(tables[0])
    top_vol=rows(tables[1])
    gainers=rows(tables[2])
    losers=rows(tables[3])

    d={}
    for x in top_turn+top_vol+gainers+losers:
        old=d.get(x["symbol"])
        if old is None or (x.get("turnover") or 0)>(old.get("turnover") or 0):
            d[x["symbol"]]=x
    sec=list(d.values())

    text=soup.get_text(" ",strip=True)
    dm=re.search(r"(20\d{2})[-./](\d{2})[-./](\d{2})",text)
    day=f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else datetime.now(UB).date().isoformat()

    total_turn=total_vol=None
    adv=dec=flat=None
    m=re.search(r"([\d,]+)\s*төгрөгийн\s+үнийн\s+дүн\s+бүхий\s+([\d,]+)\s*ширхэг.*?([\d,]+)\s+өсч,\s*([\d,]+)\s+буурсан.*?([\d,]+)\s+ханш\s+тогтвортой",text,re.I)
    if m:
        total_turn=n(m.group(1)); total_vol=n(m.group(2))
        adv=int(m.group(3).replace(",","")); dec=int(m.group(4).replace(",","")); flat=int(m.group(5).replace(",",""))
    else:
        adv=sum((x.get("change_pct") or 0)>0 for x in sec)
        dec=sum((x.get("change_pct") or 0)<0 for x in sec)
        flat=sum(x.get("change_pct")==0 for x in sec)

    for x in sec:
        if x["close"] is not None and x["change"] is not None:
            x["prev_close"]=x["close"]-x["change"]
        else:x["prev_close"]=None

    now=datetime.now(UB)
    data={
      "source":URL,"source_title":"МХБ ханшийн мэдээ","updated_at":now.isoformat(timespec="seconds"),
      "date":day,"data_mode":"mse_daily_mirror",
      "summary":{"securities":len(sec),"advancers":adv,"decliners":dec,"unchanged":flat,
                 "volume":total_vol,"turnover":total_turn},
      "top_gainers":gainers[:10],"top_losers":losers[:10],"top_turnover":top_turn[:10],
      "securities":sorted(sec,key=lambda x:-(x.get("turnover") or 0))
    }
    if not sec: raise RuntimeError("Үнэт цаасны мөр олдсонгүй")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"OK {day}: {len(sec)} symbols, turnover={total_turn}, volume={total_vol}")

if __name__=="__main__": main()
