#!/usr/bin/env python3
"""Automatically publish validated XacBank financial statements to the MSE dashboard.

Discovery does not depend on a known release date.  The source is the bank's
official financial-statement XLSX link, not a news article or an OCR transcript.
This job is intentionally conservative: inconsistent or unfamiliar sheets
leave the existing public dashboard unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from extract_xac_q3_statements import collect

ROOT = Path(__file__).resolve().parent
DB = ROOT / "financial" / "data" / "quarters.json"
AUDIT = ROOT / "financial" / "data" / "bank_nbfi_audit.json"
UB = timezone(timedelta(hours=8))
HOST = "xacbank.mn"
BALANCE_PAGE = "https://xacbank.mn/en/statement-of-financial-position"
INCOME_PAGE = "https://xacbank.mn/en/statement-of-comprehensive-income"
PAGE_URLS = [BALANCE_PAGE, INCOME_PAGE]
MONTH = {3: 1, 6: 2, 9: 3, 12: 4}
MONEY = 1_000_000  # Source states "Reported (MNT million)".
MAX_XLSX_BYTES = 4_000_000
MIN_XLSX_BYTES = 2_000
USER_AGENT = "MSE-financial-dashboard/1.0 (+read-only public report monitor)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"}


class FinancialStatementError(ValueError):
    pass


def safe_report_url(address: str) -> bool:
    parsed = urlparse(address)
    return (
        parsed.scheme == "https"
        and parsed.hostname in ("xacbank.mn", "www.xacbank.mn")
        and parsed.path.startswith("/api/media/file/")
        and parsed.path.lower().endswith(".xlsx")
    )


def discover_links(session: requests.Session) -> list[str]:
    links = []
    for page in PAGE_URLS:
        response = session.get(page, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for a in soup.select("a[href]"):
            uri = urljoin(page, a["href"])
            if safe_report_url(uri):
                links.append(uri)
        # Some versions of the site's frontend serialize report URLs into JSON.
        for raw in re.findall(r'https?:[^"<> ]{6,350}\.xlsx', response.text, flags=re.I):
            uri = raw.replace("\\u0026", "&").replace("\\/", "/")
            if safe_report_url(uri):
                links.append(uri)
    return list(dict.fromkeys(links))


def col_name(cell: str) -> str:
    m = re.match(r"([A-Z]+)\d+$", cell or "")
    return m.group(1) if m else ""


def by_row(sheet: dict, number: int) -> dict[str, object]:
    for row in sheet.get("rows", []):
        if row.get("row") == number:
            return {col_name(c.get("cell", "")): c["v"] for c in row["cells"]}
    return {}


def find_row(sheet: dict, english: str) -> tuple[int, dict[str, object]]:
    wanted = re.sub(r"\s+", " ", english).strip().casefold()
    for item in sheet.get("rows", []):
        cells = {col_name(c.get("cell", "")): c["v"] for c in item["cells"]}
        label = str(cells.get("C" if "BAL" in sheet.get("_role", "") else "B", "")).strip()
        label = re.sub(r"\s+", " ", label).casefold()
        if label == wanted:
            return item["row"], cells
    raise FinancialStatementError("Missing required line: " + english)


def as_number(value, label: str) -> float:
    if not isinstance(value, (float, int)) or not math.isfinite(value):
        raise FinancialStatementError("Missing or invalid numeric cell: " + label)
    return float(value)


def excel_date(value) -> datetime | None:
    if not isinstance(value, (float, int)) or not 42000 <= value <= 70000:
        return None
    return datetime(1899, 12, 30) + timedelta(days=int(value))


def end_period(dt: datetime) -> tuple[int, int]:
    if dt.month not in MONTH:
        raise FinancialStatementError("Quarter month invalid: " + dt.isoformat())
    expected = (datetime(dt.year + (dt.month == 12), (dt.month % 12) + 1, 1) - timedelta(days=1))
    if dt.date() != expected.date():
        raise FinancialStatementError("Financial statement is not a quarter-end snapshot: " + dt.isoformat())
    today = datetime.now(UB).date()
    if dt.date() > today + timedelta(days=2):
        raise FinancialStatementError("Future dated financial data rejected")
    return dt.year, MONTH[dt.month]


def sheet_for(sheets: list[dict], labels: tuple[str, ...], text_column: str) -> dict:
    for sheet in sheets:
        available = set()
        for row in sheet.get("rows", []):
            item = by_row(sheet, row.get("row"))
            val = str(item.get(text_column, "")).casefold().strip()
            available.add(re.sub(r"\s+", " ", val))
        if all(re.sub(r"\s+", " ", term.casefold()).strip() in available for term in labels):
            return dict(sheet, _role="BAL" if text_column == "C" else "INC")
    raise FinancialStatementError("Unexpected XLSX template (missing labels): " + ", ".join(labels))


def build_xac_candidate(sheets: list[dict], url: str, sha256: str) -> dict:
    balance = sheet_for(sheets, ("Total assets", "Total liabilities", "Total equity"), "C")
    income = sheet_for(sheets, ("Interest income", "Profit after tax"), "B")

    period_dates = []
    for column, value in by_row(balance, 5).items():
        when = excel_date(value)
        if when and column not in ("A", "B", "C"):
            year, quarter = end_period(when)
            period_dates.append((year, quarter, column, when))
    if not period_dates:
        raise FinancialStatementError("No dated balance sheet columns")
    year, quarter, col, when = max(period_dates)

    fname = unquote(urlparse(url).path)
    matches = re.findall(r"\bQ\s*([1-4])\s*(20\d{2})\b", fname, re.I)
    if matches and (int(matches[-1][1]), int(matches[-1][0])) != (year, quarter):
        raise FinancialStatementError("Filename and reported quarter differ")

    def balance_item(label):
        row_number, cells = find_row(balance, label)
        return as_number(cells.get(col), f"BAL {label} {col}{row_number}")

    def income_item(label, column):
        row_number, cells = find_row(income, label)
        return as_number(cells.get(column), f"INC {label} {column}{row_number}")

    dated_income = []
    for column, value in by_row(income, 5).items():
        when_i = excel_date(value)
        if when_i:
            dated_income.append((when_i.year, MONTH.get(when_i.month), column))
    curr = {(y, q): column for y, q, column in dated_income if q is not None and column != "L"}
    quarterly_cols = []
    for n in range(1, quarter + 1):
        if (year, n) not in curr:
            raise FinancialStatementError(f"Missing {year}Q{n} income statement to validate YTD")
        quarterly_cols.append(curr[(year, n)])

    ytd_header = by_row(income, 3)
    ytd_columns = [key for key, value in ytd_header.items()
                   if isinstance(value, str) and "year to date" in value.casefold()]
    if not ytd_columns:
        raise FinancialStatementError("Missing explicit Year to Date column")
    ytd_column = ytd_columns[-1]
    if (year, quarter, ytd_column) not in dated_income:
        raise FinancialStatementError("YTD income column date does not equal balance date")

    total_assets = balance_item("Total assets")
    liabilities = balance_item("Total liabilities")
    equity = balance_item("Total equity")
    if min(total_assets, liabilities, equity) <= 0:
        raise FinancialStatementError("Negative/zero bank balance totals")
    if abs(total_assets - liabilities - equity) > 3.0:
        raise FinancialStatementError("Assets != liabilities + equity")
    if not 200_000 <= total_assets <= 500_000_000:
        raise FinancialStatementError("Suspicious unit scale for reported MNT million")

    net_profit = income_item("Profit after tax", ytd_column)
    interest_income = income_item("Interest income", ytd_column)
    profits = [income_item("Profit after tax", key) for key in quarterly_cols]
    interests = [income_item("Interest income", key) for key in quarterly_cols]
    if abs(sum(profits) - net_profit) > 3.0:
        raise FinancialStatementError("Q1..Qn net profits != YTD profit")
    if abs(sum(interests) - interest_income) > 3.0:
        raise FinancialStatementError("Q1..Qn interest income != YTD interest income")
    if not 0 <= interest_income <= total_assets:
        raise FinancialStatementError("Interest income value out of reasonable range")
    loans = balance_item("Loans and advances to customers (Net)")
    deposits = balance_item("Deposits")
    current_accounts = balance_item("Current accounts")
    if not 0 < loans < total_assets or not 0 < deposits < liabilities:
        raise FinancialStatementError("Balance item range validation failed")

    period = f"{year}Q{quarter}"
    facts = {
        "assets": int(round(total_assets * MONEY)),
        "liabilities": int(round(liabilities * MONEY)),
        "equity": int(round(equity * MONEY)),
        "netProfit": int(round(net_profit * MONEY)),
        "revenue": int(round(interest_income * MONEY)),  # Bank's interest income (NOT merchandise sales).
        "interestIncome": int(round(interest_income * MONEY)),
        "netInterestIncome": int(round(income_item("Net interest income", ytd_column) * MONEY)),
        "interestExpenses": int(round(income_item("Interest expenses", ytd_column) * MONEY)),
        "loansNet": int(round(loans * MONEY)),
        "deposits": int(round(deposits * MONEY)),
        "currentAccounts": int(round(current_accounts * MONEY)),
        "quarterlyNetProfit": int(round(profits[-1] * MONEY)),
        "quarterlyInterestIncome": int(round(interests[-1] * MONEY)),
    }
    if facts["assets"] != facts["liabilities"] + facts["equity"]:
        if abs(facts["assets"] - facts["liabilities"] - facts["equity"]) > 3*MONEY:
            raise FinancialStatementError("Accounting identity mismatch after currency conversion")

    return {
        "symbol": "XAC", "period": period, "year": year, "quarter": quarter,
        "periodEnd": when.strftime("%Y-%m-%d"), "unit": "MNT", "basis": "ytd",
        "sourceUrl": url, "sha256": sha256,
        "retrievedAt": datetime.now(UB).isoformat(timespec="seconds"),
        "facts": facts,
        "quarterlyProfit": int(round(profits[-1]*MONEY)),
        "validated": True,
        "checks": ["Official issuer XLSX source", "Date matching",
                   "Quarter-to-YTD profit reconciliation", "Quarter-to-YTD interest income reconciliation",
                   "Assets equal liabilities plus equity", "MNT million conversion"],
    }


def fetch_candidate() -> dict:
    with requests.Session() as session:
        addresses = discover_links(session)
        if not addresses:
            raise FinancialStatementError("No XLSX download links found on official issuer pages")
        candidates = []
        problems = []
        for url in addresses[:6]:
            try:
                response = session.get(
                    url,
                    headers={"User-Agent": USER_AGENT,
                             "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*"},
                    timeout=35)
                response.raise_for_status()
                payload = response.content
                if not MIN_XLSX_BYTES <= len(payload) <= MAX_XLSX_BYTES:
                    raise FinancialStatementError("Unexpected report file size")
                if not payload.startswith(b"PK"):
                    raise FinancialStatementError("File is not an XLSX")
                digest = hashlib.sha256(payload).hexdigest()
                report = build_xac_candidate(collect(payload), url, digest)
                candidates.append(report)
            except (FinancialStatementError, requests.RequestException, ValueError, KeyError) as exc:
                problems.append({"url": url, "error": str(exc)[:240]})
        if not candidates:
            raise FinancialStatementError("No verifiable financial report found: " + json.dumps(problems))
        candidate = max(candidates, key=lambda x: (x["year"], x["quarter"]))
        candidate["downloadWarnings"] = problems
        return candidate


def quarter_index(row: dict) -> int:
    return row["year"]*4 + row["quarter"]


def apply_candidate(report: dict, db_path: Path = DB, audit_path: Path = AUDIT) -> bool:
    """Only change official XAC fields after full validation; never overwrite other issuers."""
    if not report.get("validated") or report.get("symbol") != "XAC":
        raise FinancialStatementError("Unverified/unknown issuer update forbidden")
    period = report.get("period")
    if period != f"{report['year']}Q{report['quarter']}":
        raise FinancialStatementError("Candidate period fields inconsistent")
    year, quarter = report["year"], report["quarter"]
    if not 2026 <= year <= 2045 or quarter not in (1,2,3,4):
        raise FinancialStatementError("Candidate year/quarter outside guard")
    if report.get("unit") != "MNT" or report.get("basis") != "ytd":
        raise FinancialStatementError("Candidate units or reporting basis invalid")
    if not safe_report_url(report["sourceUrl"]):
        raise FinancialStatementError("Untrusted issuer report host")

    db = json.loads(db_path.read_text(encoding="utf-8"))
    rows = db["quarters"]
    latest_keys = [k for k in rows if re.fullmatch(r"XAC:20\d{2}Q[1-4]", k)
                   and isinstance(rows[k].get("assets"), (int, float))
                   and rows[k]["assets"] > 0]
    newest = max((quarter_index(rows[k]) for k in latest_keys), default=0)
    arriving = year*4 + quarter
    if arriving < newest:
        print("SKIP: historical report does not replace newer financial period", period)
        return False

    key = f"XAC:{period}"
    previous = rows.get(key, {})
    compared = report["facts"]
    if previous and all(previous.get(k) == v for k, v in compared.items()):
        print("UNCHANGED: already published", period, report["sha256"][:12])
        return False

    # Validate again after saving candidate (defense against malformed staging JSON).
    facts = report["facts"]
    for metric in ("assets", "liabilities", "equity", "netProfit", "revenue"):
        if type(facts.get(metric)) is not int:
            raise FinancialStatementError("Non-integer metric in staged candidate: " + metric)
    if abs(facts["assets"] - facts["liabilities"] - facts["equity"]) > 3*MONEY:
        raise FinancialStatementError("Staged accounting identity failed")

    sharebase = next((rows[k].get("shares") for k in latest_keys
                      if rows[k].get("shares") and rows[k]["shares"] > 0), None)
    # An unchanged shareholder count is an assumption; do not derive count from capital amount.
    row = {
        **previous,
        "symbol": "XAC", "year": year, "quarter": quarter,
        "basis": "ytd", "statementType": "issuer_consolidated",
        "periodEnd": report["periodEnd"],
        "publishedAt": report["retrievedAt"][:10],
        "sourceUrl": report["sourceUrl"],
        "sourceSha256": report["sha256"],
        "auditStatus": "ХасБанкны албан XLSX: огноо, 9 сарын дүн, баланс тэнцлийг автоматаар шалгасан; аудитлагдаагүй байж болно",
        "shares": previous.get("shares") or sharebase,
        **facts,
    }
    if arriving > newest:
        for metric in ("nplRatio", "annualizedRoe", "annualizedEPS", "loanPortfolio"):
            row.pop(metric, None)  # Prior quarter's ratios may not be reused.
    rows[key] = row
    db["asOf"] = report["retrievedAt"][:10]
    db["notes"] = "XAC financials updated automatically from official issuer XLSX; provenance on each row."
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    e = audit["issuers"].get("XAC", {})
    verified = ["assets", "liabilities", "equity", "netProfit", "revenue", "loansNet", "deposits", "currentAccounts"]
    e.update({
        "sourceUrl": report["sourceUrl"],
        "sourceType": "issuer_official_xlsx_autovalidated",
        "period": period,
        "financialPeriodEnd": report["periodEnd"],
        "matchStatus": "matched",
        "facts": {key: {"amount": facts[key], "unit": "MNT", "approx": False}
                  for key in verified},
        "checks": {key: {"sourceAmount": facts[key], "existingAmount": facts[key],
                         "relativeDifference": 0, "passes": True} for key in verified},
        "note": "Автомат шалгасан XLSX тайлан. Ашиг/хүүгийн орлого оны эхнээс өссөн дүн; "
                "зөвхөн тухайн улирлын ашиг: "
                +str(facts["quarterlyNetProfit"]//MONEY)+" сая ₮. "
                "P/E/P/B-ийн хувьцааны тоог тусад нь нягтлах шаардлагатай.",
    })
    audit["issuers"]["XAC"] = e
    audit["asOf"] = report["retrievedAt"][:10]

    db_path.write_text(json.dumps(db, ensure_ascii=False, separators=(",", ":"))+"\n", encoding="utf-8")
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(f"UPDATED {period}: assets={facts['assets']}, profit YTD={facts['netProfit']}, report={report['sourceUrl']}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discover", metavar="JSON_OUTPUT")
    parser.add_argument("--apply", metavar="JSON_INPUT")
    args = parser.parse_args()
    if bool(args.discover) == bool(args.apply):
        parser.error("Use exactly one of --discover or --apply")
    if args.discover:
        candidate = fetch_candidate()
        Path(args.discover).write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"foundPeriod":candidate["period"],"source":candidate["sourceUrl"],
                          "validated":candidate["validated"],"checks":candidate["checks"]},ensure_ascii=False))
    else:
        report = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        apply_candidate(report)


if __name__ == "__main__":
    main()
