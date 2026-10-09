#!/usr/bin/env python3
"""Capture official XacBank Q3 2026 financial statement tables via browser.
No financial data is changed until content is independently inspected.
"""
import json, time, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parent
out=root/"financial"/"data"/"xac_2026q3_raw.json"
URLs=[
 "https://xacbank.mn/en/news/2026q3-financialresult",
 "https://xacbank.mn/en/statement-of-financial-position",
 "https://xacbank.mn/en/statement-of-comprehensive-income",
 "https://xacbank.mn/en/financial-operational-results",
 "https://xacbank.mn/investors",
 "https://mse.mn/company-reports"
]
def capture(page,label,records):
    text=page.locator('body').inner_text(timeout=12000)
    trs=page.locator('tr').evaluate_all("""els=>els.slice(0,300).map(e=>(e.innerText||'').replace(/\\s+/g,' ').slice(0,350))""")
    sels=page.locator('select').evaluate_all("""els=>els.map(e=>({html:e.outerHTML.slice(0,600),value:e.value,options:[...e.options].map(o=>({value:o.value,text:o.text}))}))""")
    btns=page.locator('button').evaluate_all("""els=>els.slice(0,100).map(e=>({text:e.innerText?.slice(0,70)||'',html:e.outerHTML.slice(0,350)}))""")
    links=page.locator('a[href]').evaluate_all("""els=>els.slice(0,350).map(a=>({text:(a.innerText||'').slice(0,80),href:a.href})).filter(a=>/\\.pdf|\\.(xlsx?|csv)|financial|report|api\\/media/i.test(a.href))""")
    records.append(dict(label=label,url=page.url,title=page.title(),body=text[:28000],rows=trs[:200],selects=sels,buttons=btns,links=links[:170]))
def main():
 result={"fetchedAt":datetime.now(timezone(timedelta(hours=8))).isoformat(),"pages":[],"errors":[],"source":"XacBank and MSE public pages"}
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
  for url in URLs:
   page=browser.new_page(locale='en-US',viewport={"width":1440,"height":960})
   try:
    resp=page.goto(url,wait_until="domcontentloaded",timeout=45000)
    page.wait_for_timeout(2200)
    capture(page,"initial",result["pages"])
    if "statement-of" in url:
     # Select Q3 2026 in native select controls, when available.
     for idx in range(min(page.locator("select").count(),4)):
      node=page.locator("select").nth(idx)
      opts=node.locator("option").all()
      for option in opts:
       label=option.inner_text().strip()
       if "2026" in label:
        try:
         node.select_option(label=label,timeout=2500)
         page.wait_for_timeout(1500)
         capture(page,"selected "+label,result["pages"])
         break
        except Exception as e:
         result["errors"].append(str(e)[:120])
    if "company-reports" in url:
     for label in ["Хувьцаат компаниудын санхүүгийн тайлан","Үйл ажиллагааны тайлан"]:
      try:
       page.get_by_text(label,exact=True).first.click(timeout=4500)
       page.wait_for_timeout(1500)
       capture(page,label,result["pages"])
      except Exception as e:
       result["errors"].append(f"{label} {e}"[:250])
    if "investors" in url:
     try:
      capture(page,"investor loaded",result["pages"])
     except Exception:pass
   except Exception as e:result["errors"].append(f"{url}: {str(e)[:200]}")
   finally:page.close()
  browser.close()
 out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({"pages":[{"label":x["label"],"url":x["url"],"head":x["body"][:500],"rows":len(x["rows"]),"links":x["links"][:8],"selects":x["selects"][:2]} for x in result["pages"]],"errors":result["errors"]},ensure_ascii=False))
if __name__=="__main__":main()
