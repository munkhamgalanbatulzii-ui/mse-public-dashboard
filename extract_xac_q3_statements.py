#!/usr/bin/env python3
"""Extract public XacBank Q3 2026 XLSX workbook rows without changing the financial dashboard."""
import json, zipfile, io, re
from urllib.request import Request,urlopen
from datetime import datetime,timezone,timedelta
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parent
OUT=ROOT/"financial"/"data"/"xac_2026q3_xlsx_extract.json"
URLS=[
 "https://xacbank.mn/api/media/file/XacBank%20JSC%20Condensed%20Financial%20statement%20Q3%202026.xlsx",
 "https://xacbank.mn/api/media/file/XacBank%20JSC%20Condensed%20Financial%20statement%20Q3%202026-1.xlsx"
]
S="{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
def collect(data):
 with zipfile.ZipFile(io.BytesIO(data)) as z:
  files=z.namelist()
  shared=[]
  if "xl/sharedStrings.xml" in files:
   root=ET.fromstring(z.read("xl/sharedStrings.xml"))
   shared=["".join(node.text or "" for node in si.iter(S+"t")) for si in root.findall(S+"si")]
  sheets=[]
  names=sorted([n for n in files if re.fullmatch(r"xl/worksheets/sheet\d+\.xml",n)])
  for n in names:
   root=ET.fromstring(z.read(n))
   rows=[]
   for row in root.iter(S+"row"):
    cells=[]
    for cell in row.findall(S+"c"):
     value=cell.find(S+"v")
     ctype=cell.get("t")
     if ctype=="inlineStr":
      s="".join(node.text or "" for node in cell.iter(S+"t"))
     elif value is None:
      continue
     elif ctype=="s":
      s=shared[int(value.text or 0)]
     elif ctype in ("str","e","b"):
      s=value.text
     else:
      try:s=float(value.text or "")
      except Exception:s=value.text
     if s is not None and s!="":
      cells.append({"cell":cell.get("r"),"v":s})
    if cells:rows.append({"row":int(row.get("r",0)),"cells":cells})
   sheets.append({"path":n,"rows":rows[:1400],"rowCount":len(rows)})
  return sheets
def main():
 out={"asOf":datetime.now(timezone(timedelta(hours=8))).isoformat(),"source":"official XacBank XLSX Q3 2026","files":[]}
 for url in URLS:
  try:
   req=Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*"})
   data=urlopen(req,timeout=35).read()
   out["files"].append({"url":url,"bytes":len(data),"sheets":collect(data)})
  except Exception as e:
   out["files"].append({"url":url,"error":str(e)})
 OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
 print(json.dumps({"files":[{"url":f["url"],"bytes":f.get("bytes"),"error":f.get("error"),"sheets":[{"rows":s["rowCount"],"head":s["rows"][:18]} for s in f.get("sheets",[])]} for f in out["files"]]},ensure_ascii=False))
 if not any("sheets" in f for f in out["files"]):raise SystemExit("No official spreadsheets could be downloaded")
if __name__=="__main__":main()
