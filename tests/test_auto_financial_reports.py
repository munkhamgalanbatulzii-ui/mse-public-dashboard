"""Offline financial-auto-refresh guard tests; uses issuer XLSX values captured 2026-10-09."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from auto_financial_reports import (
    FinancialStatementError,
    build_xac_candidate,
    apply_candidate,
    safe_report_url,
)

BASE = Path(__file__).resolve().parents[1] / "financial" / "data"


class XacFinancialUpdatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        extraction = json.loads((BASE / "xac_2026q3_xlsx_extract.json").read_text(encoding="utf-8"))
        cls.file = extraction["files"][0]
        cls.candidate = build_xac_candidate(cls.file["sheets"], cls.file["url"], "test-sha")

    def test_source_and_nine_month_profit(self):
        row = self.candidate
        self.assertEqual("2026Q3", row["period"])
        self.assertEqual("2026-09-30", row["periodEnd"])
        self.assertEqual(8_847_742_000_000, row["facts"]["assets"])
        self.assertEqual(7_886_025_000_000, row["facts"]["liabilities"])
        self.assertEqual(961_717_000_000, row["facts"]["equity"])
        self.assertEqual(138_422_000_000, row["facts"]["netProfit"])
        self.assertEqual(50_187_000_000, row["facts"]["quarterlyNetProfit"])
        self.assertEqual(716_839_000_000, row["facts"]["interestIncome"])

    def test_rejects_untrusted_hosts(self):
        self.assertFalse(safe_report_url("https://evil.example.com/api/media/file/file.xlsx"))
        self.assertFalse(safe_report_url("http://xacbank.mn/api/media/file/file.xlsx"))
        self.assertTrue(safe_report_url(self.file["url"]))

    def test_rejects_unbalanced_statement(self):
        sheets = copy.deepcopy(self.file["sheets"])
        balance = sheets[0]
        for row in balance["rows"]:
            if row["row"] == 25:
                for c in row["cells"]:
                    if c["cell"] == "H25":
                        c["v"] += 500
        with self.assertRaises(FinancialStatementError):
            build_xac_candidate(sheets, self.file["url"], "test")

    def test_idempotence_and_insert(self):
        original = json.loads((BASE / "quarters.json").read_text(encoding="utf-8"))
        audit = json.loads((BASE / "bank_nbfi_audit.json").read_text(encoding="utf-8"))
        orig_issuer_count = len({key.split(":")[0] for key in original["quarters"]})
        del original["quarters"]["XAC:2026Q3"]
        with tempfile.TemporaryDirectory() as td:
            db = Path(td)/"quarters.json"
            ax = Path(td)/"audit.json"
            db.write_text(json.dumps(original,ensure_ascii=False),encoding="utf-8")
            ax.write_text(json.dumps(audit,ensure_ascii=False),encoding="utf-8")
            self.assertTrue(apply_candidate(self.candidate, db, ax))
            state = json.loads(db.read_text(encoding="utf-8"))
            self.assertEqual(2026, state["quarters"]["XAC:2026Q3"]["year"])
            self.assertEqual(orig_issuer_count,len({key.split(":")[0] for key in state["quarters"]}))
            self.assertFalse(apply_candidate(self.candidate, db, ax))

    def test_older_filing_never_downgrades(self):
        original = json.loads((BASE / "quarters.json").read_text(encoding="utf-8"))
        audit = json.loads((BASE / "bank_nbfi_audit.json").read_text(encoding="utf-8"))
        old = copy.deepcopy(self.candidate)
        old["year"], old["quarter"], old["period"] = 2026,2,"2026Q2"
        with tempfile.TemporaryDirectory() as td:
            db = Path(td)/"quarters.json"
            ax = Path(td)/"audit.json"
            db.write_text(json.dumps(original,ensure_ascii=False),encoding="utf-8")
            ax.write_text(json.dumps(audit,ensure_ascii=False),encoding="utf-8")
            self.assertFalse(apply_candidate(old, db, ax))


if __name__ == "__main__":
    unittest.main()
