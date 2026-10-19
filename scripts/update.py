#!/usr/bin/env python3
"""Refresh the data objects in index.html from public sources.

The page draws everything from three one-line JavaScript objects:

  const DATA = {...};   Monthly Statement of the Public Debt (Treasury Fiscal Data API)
  const HOLD = {...};   Who holds the debt, quarterly (Federal Reserve Z.1 table F3.2.s, via FRED,
                        or the Fed's own table if FRED cannot be read)
  const TIC = {...};    Foreign holders by country, monthly (Treasury TIC Table 5)

This script fetches each source, rebuilds only those three lines, runs the checks,
and writes index.html and data/summary.json only when every check passes.

Usage:
  python scripts/update.py              fetch, check, write if anything changed
  python scripts/update.py --dry-run    fetch and check, report what would change, write nothing
  python scripts/update.py --check      no network: validate the objects already in index.html
  --hold always                         read Z.1 even if no new release is due (default: only
                                        on or after HOLD.next, the next scheduled release)

Exit codes: 0 = success (updated or nothing new), 1 = a check failed or a source
could not be read. Nothing is written unless the exit code is 0. One exception: if
Z.1 cannot be read from FRED or the Fed, HOLD keeps its current values, a warning
is reported (and written to --alert-file), and DATA and TIC can still update.

Standard library only.
"""

import argparse
import calendar
import datetime as dt
import decimal
import html.parser
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
SUMMARY = os.path.join(ROOT, "data", "summary.json")

FISCAL_API = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
MSPD = FISCAL_API + "/v1/debt/mspd/"
PENNY = FISCAL_API + "/v2/accounting/od/debt_to_penny"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
Z1_TABLE = "https://www.federalreserve.gov/releases/z1/current/html/F3_2_s.htm"
Z1_HOME = "https://www.federalreserve.gov/releases/z1/"
TIC_TABLE5 = "https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/slt_table5.txt"
TIC_PRESS = "https://home.treasury.gov/data/treasury-international-capital-tic-system-home-page/tic-press-releases"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "text/html,application/json,text/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT, TRIES, PAUSE = 60, 3, 5
DISCLAIMER = "For education only. Not financial advice."
OBJECTS = ("DATA", "HOLD", "TIC")

MKTCOLS = ["Bills", "Notes", "Bonds", "TIPS", "FRN", "FFB"]
# MSPD Table I class description -> (mkt column, table1 key)
MARKETABLE = {
    "bills": ("Bills", "Bills"),
    "notes": ("Notes", "Notes"),
    "bonds": ("Bonds", "Bonds"),
    "treasury inflation-protected securities": ("TIPS", "TIPS"),
    "floating rate notes": ("FRN", "Floating Rate Notes"),
    "federal financing bank": ("FFB", "Federal Financing Bank"),
}
NONMARKETABLE = {
    "domestic series": "Domestic Series",
    "state and local government series": "State and Local Government Series",
    "united states savings securities": "U.S. Savings Securities",
    "us savings securities": "U.S. Savings Securities",
    "government account series": "Government Account Series",
    "other": "Other",
}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


class CheckFailed(Exception):
    pass


class SourceUnavailable(CheckFailed):
    """A source could not be read (network error, timeout, HTTP error, or a missing line)."""


class Report:
    """Collects check results, warnings and a human-readable comparison."""

    def __init__(self):
        self.errors = []
        self.warnings = []
        self.passed = []
        self.lines = []
        self.alerts = []      # non-blocking problems that still need a person (opened as an issue)

    def check(self, ok, label, detail=""):
        text = label + (f" ({detail})" if detail else "")
        (self.passed if ok else self.errors).append(text)
        return ok

    def warn(self, text):
        self.warnings.append(text)

    def say(self, text=""):
        self.lines.append(text)

    def render(self):
        out = list(self.lines)
        if self.passed:
            out += ["", "Checks passed:"] + [f"  ok   {p}" for p in self.passed]
        if self.warnings:
            out += ["", "Warnings (not blocking):"] + [f"  warn {w}" for w in self.warnings]
        if self.errors:
            out += ["", "Checks FAILED:"] + [f"  FAIL {e}" for e in self.errors]
        return "\n".join(out)


# ---------------------------------------------------------------- helpers

def fetch(url, params=None, tries=TRIES):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, safe=":,[]-")
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt == tries - 1:
                raise SourceUnavailable(f"could not read {url} after {tries} tries: {e}")
            time.sleep(PAUSE)


def api(endpoint, params, page_size=10000):
    """All rows of a Fiscal Data API endpoint, following pagination and checking the row count."""
    rows, page = [], 1
    while True:
        p = dict(params, **{"page[size]": page_size, "page[number]": page})
        body = json.loads(fetch(endpoint, p))
        meta = body.get("meta", {})
        rows += body.get("data", [])
        if page >= meta.get("total-pages", 1):
            break
        page += 1
    expected = meta.get("total-count")
    if expected is not None and len(rows) != expected:
        raise CheckFailed(f"{endpoint}: read {len(rows)} rows but meta.total-count is {expected}")
    return rows


def api_newest(endpoint, params=None):
    """The newest row of a Fiscal Data API endpoint."""
    p = dict(params or {}, **{"sort": "-record_date", "page[size]": 1})
    data = json.loads(fetch(endpoint, p)).get("data", [])
    return data[0] if data else None


def num(v):
    if v in (None, "", "null"):
        return 0.0
    return float(str(v).replace(",", ""))


def r1(x):
    x = round(x + 0.0, 1)
    return 0.0 if x == 0 else x


def norm(s):
    s = re.sub(r"\(\d+\)|\[\d+\]", " ", s or "")      # footnote markers
    s = re.sub(r"^\s*\d+\s+", " ", s)                  # leading line numbers
    s = re.sub(r"[.\s]+$", "", s.replace("\xa0", " "))  # dot leaders
    return re.sub(r"\s+", " ", s).strip().lower().replace("u.s.", "us").replace("’", "'")


def month_end(year, month):
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def nice_date(d):
    return f"{calendar.month_abbr[d.month]} {d.day}, {d.year}"


def parse_text_date(s):
    m = re.search(r"\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),\s*(\d{4})", s)
    if not m or m.group(1)[:3].lower() not in MONTHS:
        return None
    return dt.date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))


def need_fields(rows, fields, what):
    if rows and not set(fields) <= set(rows[0]):
        raise CheckFailed(f"{what}: expected fields {sorted(set(fields) - set(rows[0]))} "
                          f"not in {sorted(rows[0])}")


def close(a, b, tol):
    return abs(a - b) <= tol + 1e-9


# ---------------------------------------------------------- page handling

def read_page():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def find_objects(page):
    lines = page.split("\n")
    found = {}
    for i, line in enumerate(lines):
        m = re.match(r"^const (DATA|HOLD|TIC) = (\{.*\});$", line)
        if m:
            if m.group(1) in found:
                raise CheckFailed(f"const {m.group(1)} appears more than once in index.html")
            found[m.group(1)] = (i, json.loads(m.group(2)))
    missing = [k for k in OBJECTS if k not in found]
    if missing:
        raise CheckFailed(f"could not find one-line objects in index.html: {missing}")
    return lines, found


def dump(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def rebuild_page(page, objs):
    lines, found = find_objects(page)
    for name, (i, _) in found.items():
        lines[i] = f"const {name} = {dump(objs[name])};"
    return "\n".join(lines)


def check_page(old, new, report):
    """Everything outside the three object lines must be unchanged."""
    ol, of = find_objects(old)
    nl, nf = find_objects(new)
    skip_o = {i for i, _ in of.values()}
    skip_n = {i for i, _ in nf.values()}
    same = ([l for i, l in enumerate(ol) if i not in skip_o]
            == [l for i, l in enumerate(nl) if i not in skip_n])
    report.check(same, "page: only the three data lines differ")
    report.check(DISCLAIMER in new, "page: education-only note present")
    for k in OBJECTS:
        report.check(set(of[k][1]) == set(nf[k][1]), f"page: {k} keeps the same keys")
    node = shutil_which("node")
    if node:
        scripts = re.findall(r"<script>(.*?)</script>", new, re.S)
        with tempfile.TemporaryDirectory() as d:
            ok = True
            for n, s in enumerate(scripts):
                p = os.path.join(d, f"s{n}.js")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(s)
                ok &= subprocess.run([node, "--check", p], capture_output=True).returncode == 0
        report.check(ok, "page: every <script> parses (node --check)")


def shutil_which(cmd):
    import shutil
    return shutil.which(cmd)


# ------------------------------------------------------------ 1. DATA (MSPD)

def mspd_rows(table, **filt):
    return api(MSPD + table, {"filter": ",".join(f"{k}:{v}" for k, v in filt.items())})


def triple(row):
    return [r1(num(row["debt_held_public_mil_amt"])), r1(num(row["intragov_hold_mil_amt"])),
            r1(num(row["total_mil_amt"]))]


def build_table1(rows, report):
    t1 = {"marketable": {}, "total_marketable": None, "nonmarketable": {},
          "total_nonmarketable": None, "total": None}
    mkt = [None] * 6
    for row in rows:
        typ, cls = norm(row.get("security_type_desc")), norm(row.get("security_class_desc"))
        vals = triple(row)
        if typ == "total public debt outstanding":
            t1["total"] = vals
        elif typ == "marketable" and cls in MARKETABLE:
            col, key = MARKETABLE[cls]
            t1["marketable"][key] = vals
            mkt[MKTCOLS.index(col)] = vals[:2]
        elif typ == "marketable" and cls.startswith("total"):
            t1["total_marketable"] = vals
        elif typ == "nonmarketable" and cls in NONMARKETABLE:
            t1["nonmarketable"][NONMARKETABLE[cls]] = vals
        elif typ == "nonmarketable" and cls.startswith("total"):
            t1["total_nonmarketable"] = vals
        elif typ in ("total marketable",):
            t1["total_marketable"] = vals
        elif typ in ("total nonmarketable",):
            t1["total_nonmarketable"] = vals
        elif any(vals):
            report.check(False, "MSPD Table I: every non-zero line is mapped",
                         f"unmapped: {row.get('security_type_desc')} / {row.get('security_class_desc')}")
    return t1, mkt


def order_like(new, template):
    """Keep the key order of the current file so diffs stay small."""
    if isinstance(new, dict) and isinstance(template, dict):
        keys = [k for k in template if k in new] + [k for k in new if k not in template]
        return {k: order_like(new[k], template.get(k)) for k in keys}
    return new


T2_LINES = [  # key, pattern on "class1 class2", deduction (stored as positive)
    ("total", r"^total public debt outstanding", False),
    ("other_debt_not_subject", r"not subject to (the )?limit .*other debt|^other debt not subject", True),
    ("unamortized_discount", r"not subject to (the )?limit .*unamortized discount|^unamortized discount", True),
    ("ffb", r"not subject to (the )?limit .*federal financing bank|^federal financing bank", True),
    ("guaranteed", r"guaranteed debt", False),
    ("subject_to_limit", r"total public debt subject to", False),
    ("limit", r"^statutory debt limit", False),
    ("balance", r"^balance of (the )?statutory debt limit", False),
]
T2_FIELDS = ("debt_limit_class1_desc", "debt_limit_class2_desc",
             "debt_held_public_mil_amt", "intragov_hold_mil_amt", "total_mil_amt")


def t2_desc(row):
    parts = [row.get("debt_limit_class1_desc"), row.get("debt_limit_class2_desc")]
    return norm(" ".join(p for p in parts if p and p not in ("_", "null")))


def build_table2(rows):
    if not rows:
        raise CheckFailed("MSPD Table II returned no rows")
    need_fields(rows, T2_FIELDS, "MSPD Table II")
    t2 = {}
    for key, pat, deduction in T2_LINES:
        hits = [r for r in rows if re.search(pat, t2_desc(r))]
        if len(hits) != 1:
            raise CheckFailed(f"MSPD Table II: expected one line matching '{pat}', found "
                              f"{[t2_desc(r) for r in hits]}")
        vals = triple(hits[0])
        if deduction:
            vals = [abs(v) for v in vals]
        t2[key] = vals[2] if key in ("limit", "balance") else vals
    return t2


def build_holders(rows):
    if not rows:
        raise CheckFailed("MSPD Table III (nonmarketable) returned no rows")
    name_f, prior_f, issued_f, redeemed_f, cur_f = (
        "security_class3_desc", "prior_month_outstanding_amt", "current_month_issued_amt",
        "current_month_redeemed_amt", "current_month_outstanding_amt")
    need_fields(rows, ("security_class2_desc", name_f, prior_f, issued_f, redeemed_f, cur_f),
                "MSPD Table III")

    def pick(cls2):
        agg = {}
        for r in rows:
            if norm(r.get("security_class2_desc")) != cls2:
                continue
            name = re.sub(r"[.\s]+$", "", (r.get(name_f) or "").strip())
            if not name or name.lower() in ("null", "_") or name.lower().startswith("total"):
                continue
            a = agg.setdefault(name, {"name": name, "prior": 0.0, "issued": 0.0,
                                      "redeemed": 0.0, "cur": 0.0})
            a["prior"] += num(r[prior_f])
            a["issued"] += num(r[issued_f])
            a["redeemed"] -= abs(num(r[redeemed_f]))
            a["cur"] += num(r[cur_f])
        out = [{k: (r1(v) if k != "name" else v) for k, v in a.items()} for a in agg.values()]
        return sorted(out, key=lambda a: -a["cur"])

    intra = pick("intragovernmental holdings")
    public = pick("held by the public")
    if not intra:
        raise CheckFailed("MSPD Table III: no 'Intragovernmental Holdings' rows")
    rest = intra[40:]
    return {
        "holders": intra[:40],
        "holders_other": r1(sum(a["cur"] for a in rest)),
        "holders_other_prior": r1(sum(a["prior"] for a in rest)),
        "gas_public": [{"name": a["name"], "prior": a["prior"], "cur": a["cur"]} for a in public[:6]],
        "_intra_sum": r1(sum(a["cur"] for a in intra)),
    }


def update_data(old, report):
    latest = api_newest(MSPD + "mspd_table_1")
    newest = latest["record_date"] if latest else None
    if not newest or newest <= old["asof"]:
        report.say(f"DATA: no new statement (page {old['asof']}, source {newest}).")
        return None

    rows = mspd_rows("mspd_table_1", record_date=f"gt:{old['asof']}")
    need_fields(rows, ("record_date", "security_type_desc", "security_class_desc",
                       "debt_held_public_mil_amt", "intragov_hold_mil_amt", "total_mil_amt"), "MSPD Table I")
    by_date = {}
    for r in rows:
        by_date.setdefault(r["record_date"], []).append(r)
    dates = sorted(by_date)
    new = json.loads(json.dumps(old))
    known_months = {t[0] for t in new["totals"]}
    for d in dates:
        t1, mkt = build_table1(by_date[d], report)
        if t1["total"] is None or None in mkt:
            raise CheckFailed(f"MSPD Table I for {d} is missing the total or a marketable class")
        if d[:7] not in known_months:
            new["totals"].append([d[:7]] + t1["total"])
        if d[:7] not in {m[0] for m in new["mkt"]}:
            new["mkt"].append([d[:7], mkt])
    asof = dates[-1]
    all_dates = sorted(set([old["asof"]] + dates))
    new["asof"], new["prior"] = asof, all_dates[-2]

    t1, _ = build_table1(by_date[asof], report)
    new["table1"] = order_like(t1, old["table1"])
    new["table2"] = order_like(build_table2(mspd_rows("mspd_table_2", record_date=f"eq:{asof}")),
                               old["table2"])
    h = build_holders(mspd_rows("mspd_table_3_nonmarket", record_date=f"eq:{asof}"))
    intra_sum = h.pop("_intra_sum")
    new.update(h)

    # ---- checks
    t1, t2 = new["table1"], new["table2"]
    last = new["totals"][-1]
    report.check(last[0] == asof[:7] and all(close(a, b, 0.05) for a, b in zip(last[1:], t1["total"])),
                 "DATA: new totals row equals table1.total", f"{last[1:]} vs {t1['total']}")
    report.check(all(close(a, b, 0.05) for a, b in zip(t1["total"], t2["total"])),
                 "DATA: table1.total equals table2.total", f"{t1['total']} vs {t2['total']}")
    gas = t1["nonmarketable"].get("Government Account Series", [0, 0, 0])[1]
    report.check(close(intra_sum, gas, 1.0),
                 "DATA: intragov fund rows sum to Government Account Series intragov within $1M",
                 f"{intra_sum:,.1f} vs {gas:,.1f}")
    for i, part in enumerate(("public", "intragov", "total")):
        s = sum(v[i] for v in t1["marketable"].values())
        report.check(close(s, t1["total_marketable"][i], 1.0),
                     f"DATA: marketable classes sum to total_marketable ({part}) within $1M",
                     f"{s:,.1f} vs {t1['total_marketable'][i]:,.1f}")
    s = sum(v[2] for v in t1["nonmarketable"].values())
    report.check(close(s, t1["total_nonmarketable"][2], 1.0),
                 "DATA: nonmarketable classes sum to total_nonmarketable within $1M")
    report.check(len(new["mkt"][-1][1]) == 6 and set(t1["marketable"]) == set(old["table1"]["marketable"]),
                 "DATA: six marketable classes present")
    table2_identities(t2, report)
    penny_check(new, report)

    report.say(f"DATA: new statement {asof} (page had {old['asof']}).")
    for label, i in (("total", 3), ("held by the public", 1), ("intragovernmental", 2)):
        report.say(f"  {label:<20} {old['totals'][-1][i]:>14,.1f} -> {last[i]:>14,.1f} $M "
                   f"({last[i] - old['totals'][-1][i]:+,.1f})")
    report.say(f"  debt-limit room      {old['table2']['balance']:>14,.1f} -> {t2['balance']:>14,.1f} $M")
    report.say(f"  months appended: {', '.join(d[:7] for d in dates)}")
    return new


def table2_identities(t2, report):
    sub = (t2["total"][2] - t2["other_debt_not_subject"][2] - t2["unamortized_discount"][2]
           - t2["ffb"][2] + t2["guaranteed"][2])
    report.check(close(sub, t2["subject_to_limit"][2], 1.0),
                 "DATA: Table II total - deductions + guaranteed = subject to limit within $1M",
                 f"{sub:,.1f} vs {t2['subject_to_limit'][2]:,.1f}")
    report.check(close(t2["limit"] - t2["subject_to_limit"][2], t2["balance"], 1.0),
                 "DATA: Table II limit - subject to limit = balance within $1M")


def penny_check(new, report):
    try:
        row = api_newest(PENNY, {"filter": f"record_date:lte:{new['asof']}"})
    except (CheckFailed, ValueError) as e:
        report.warn(f"Debt to the Penny not read: {e}")
        return
    if not row:
        report.warn("Debt to the Penny returned no rows")
        return
    penny = num(row["tot_pub_debt_out_amt"]) / 1e6
    gap = penny - new["table1"]["total"][2]
    msg = f"Debt to the Penny {row['record_date']}: {penny:,.1f} $M, gap {gap:+,.1f} $M"
    if abs(gap) > 1.0:
        report.warn(msg)
    else:
        report.check(True, "DATA: second read against Debt to the Penny", msg)


# ------------------------------------------------------- 2. HOLD (Z.1 via FRED)

# Z.1 table F3.2.s lines, read from FRED as BOGZ1<code>Q (millions of dollars, quarterly levels).
# group -> list of (part label or None, code); a group whose first entry has label None is a
# single sector line, optionally followed by its bills / longer-term sub-lines.
Z1_GROUPS = {
    "Foreign holders": [(None, "LM263061105"), ("Bills", "LM263061110"), ("longer-term", "LM263061120")],
    "Federal Reserve": [(None, "LM713061103"), ("Bills", "LM713061113"), ("longer-term", "LM713061125")],
    "Money market funds": [(None, "FL633061105"), ("Bills", "FL633061110"), ("longer-term", "FL633061120")],
    "Households and nonprofits": [(None, "LM153061105")],
    "Mutual funds and ETFs": [("Mutual funds", "LM653061105"), ("ETFs", "LM563061103"),
                              ("closed-end funds", "LM553061103")],
    "Banks": [("U.S. banks", "LM763061100"), ("holding companies", "LM733061103"),
              ("foreign bank offices", "LM753061103"), ("credit unions", "LM473061105"),
              ("banks in U.S. territories", "LM743061103")],
    "State and local governments": [(None, "LM213061103")],
    "Pensions": [("Private", "LM573061105"), ("state and local", "LM223061143"), ("federal", "LM343061105")],
    "Insurers": [("Property-casualty", "LM513061105"), ("life", "LM543061105")],
    "Other": [("Broker-dealers", "LM663061105"), ("Fannie, Freddie and other GSEs", "LM403061105"),
              ("nonfinancial companies", "LM103061103"), ("hedge funds", "LM623061103"),
              ("other financial firms", "FL503061123"), ("small businesses", "LM113061003"),
              ("ABS issuers", "FL673061103")],
}
Z1_TOTALS = {"held": "FL893061105", "disc": "LM903061103", "z1mkt": "FL313161105"}


def fred_series(code):
    """{(year, quarter): value in $B} for one Z.1 line from FRED."""
    sid = f"BOGZ1{code}Q"
    text = fetch(FRED_CSV, {"id": sid})
    out = {}
    for line in text.strip().splitlines()[1:]:
        cells = line.split(",")
        if len(cells) != 2 or not re.match(r"^\d{4}-\d{2}-\d{2}$", cells[0]):
            continue
        try:
            v = float(cells[1])
        except ValueError:          # "." marks a missing observation
            continue
        y, m = int(cells[0][:4]), int(cells[0][5:7])
        out[(y, (m - 1) // 3 + 1)] = v / 1000
    if not out:
        raise SourceUnavailable(f"FRED {sid}: no observations (series renamed or discontinued?)")
    return out


def z1_from_fred(codes):
    series = {}
    for code in sorted(codes):     # stops at the first series FRED cannot serve
        series[code] = fred_series(code)
        time.sleep(0.2)
    return series


class TableParser(html.parser.HTMLParser):
    """Collects the text of every table row as a list of cell strings."""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


Z1_CODE = re.compile(r"\b([A-Z]{2})(\d{9})\b")
Z1_QUARTER = re.compile(r"(\d{4})\s*:?\s*Q\s*([1-4])\b|\bQ\s*([1-4])\s*:?\s*(\d{4})", re.I)
NUMBER = re.compile(r"^-?\d*\.?\d+$")


def z1_quarter(cell):
    m = Z1_QUARTER.search(cell)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(4)), int(m.group(3)))


def z1_from_fed_table(codes, report):
    """The same lines from the Fed's own HTML table, matched by series code (e.g. FL313161105)."""
    page_html = fetch(Z1_TABLE)
    parser = TableParser()
    parser.feed(page_html)
    scale = 1 / 1000 if re.search(r"millions of dollars", page_html, re.I) else 1.0
    header, cols, found = None, None, {}
    for row in parser.rows:
        qs = {i: q for i, c in enumerate(row) if (q := z1_quarter(c))}
        if len(qs) >= 2:
            header, cols = row, qs
            continue
        code = next((m.group(1) + m.group(2) for c in row if (m := Z1_CODE.search(c))), None)
        if header is None or code is None:
            continue
        offset = len(row) - len(header)     # align value columns from the right
        for i, q in cols.items():
            j = i + offset
            text = row[j].replace(",", "").replace(" ", "") if 0 <= j < len(row) else ""
            if NUMBER.match(text):
                found.setdefault(code, {})[q] = float(text) * scale
    if not found:
        raise SourceUnavailable(f"{Z1_TABLE}: no series codes with quarterly values found")
    series, missing = {}, []
    for code in sorted(codes):
        other = {"FL": "LM", "LM": "FL"}.get(code[:2], "") + code[2:]
        if code in found:
            series[code] = found[code]
        elif other in found:
            report.warn(f"Fed table: {code} not found, used {other} (same line, other prefix)")
            series[code] = found[other]
        else:
            missing.append(code)
    if missing:
        raise SourceUnavailable(f"Fed table F3.2.s has no lines {missing} (renamed or renumbered?)")
    return series


def read_z1(codes, report):
    """Z.1 lines from FRED, or from the Fed's own table if FRED cannot be read."""
    try:
        return z1_from_fred(codes), "FRED"
    except SourceUnavailable as e:
        report.warn(f"HOLD: FRED not read ({e}); used the Fed's table F3.2.s instead")
    return z1_from_fed_table(codes, report), "the Fed's table F3.2.s"


def hold_due(old, today=None):
    """True when a new Z.1 release could be out: on or after HOLD.next, which is later than HOLD.release."""
    today = today or dt.date.today()
    nxt, rel = parse_text_date(old.get("next", "")), parse_text_date(old.get("release", ""))
    if nxt is None or (rel is not None and nxt <= rel):
        return True
    return today >= nxt


def z1_release_dates(report, old):
    """Latest and next Z.1 release dates from the Fed's Z.1 page; keeps the page's values if not found."""
    try:
        text = re.sub(r"<[^>]+>", " ", fetch(Z1_HOME))
    except CheckFailed as e:
        report.warn(f"Z.1 release dates not read: {e}")
        return old["release"], old["next"]
    dates = sorted({d for m in re.finditer(r"\b[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s*\d{4}", text)
                    if (d := parse_text_date(m.group(0)))})
    today = dt.date.today()
    past = [d for d in dates if d <= today]
    release = max(past) if past else None
    nxt = min((d for d in dates if d > today), default=None)
    rel_s = nice_date(release) if release else old["release"]
    nxt_s = nice_date(nxt) if nxt else old["next"]
    if release is None:
        report.warn("Z.1 release date not found on the Fed's Z.1 page; kept the current value")
    if nxt is None:
        report.warn("Z.1 next release date not found on the Fed's Z.1 page; kept the current value")
    return rel_s, nxt_s


def update_hold(old, data, report):
    codes = {code for parts in Z1_GROUPS.values() for _, code in parts} | set(Z1_TOTALS.values())
    series, source = read_z1(codes, report)
    newest = {code: max(s) for code, s in series.items()}
    cur_q = max(newest.values())
    lagging = sorted(c for c, q in newest.items() if q != cur_q)
    if lagging:
        raise CheckFailed(f"Z.1 lines without {cur_q[0]}:Q{cur_q[1]} on {source}: {lagging} "
                          f"(renamed or renumbered? map them again from table F3.2.s)")
    prior_q = (cur_q[0] - 1, cur_q[1])
    missing = sorted(c for c, s in series.items() if prior_q not in s)
    if missing:
        raise CheckFailed(f"Z.1 lines without {prior_q[0]}:Q{prior_q[1]} on {source}: {missing}")
    asof = month_end(cur_q[0], cur_q[1] * 3)
    prior = month_end(prior_q[0], prior_q[1] * 3)
    if asof < old["asof"]:
        raise CheckFailed(f"Z.1 newest quarter {asof} is older than the page's {old['asof']}")

    groups = []
    for g in old["groups"]:
        lines = Z1_GROUPS.get(g["name"])
        if not lines:
            raise CheckFailed(f"HOLD: no Z.1 lines mapped to group '{g['name']}'")
        if lines[0][0] is None:      # single sector line, with optional bills / longer-term split
            total, subs = series[lines[0][1]], lines[1:]
            cur, pri = r1(total[cur_q]), r1(total[prior_q])
            parts = [[lab, r1(series[c][cur_q])] for lab, c in subs]
            if parts:
                report.check(close(parts[0][1] + parts[1][1], cur, 0.15),
                             f"HOLD: {g['name']} bills + longer-term equal the sector line",
                             f"{parts[0][1] + parts[1][1]:,.1f} vs {cur:,.1f}")
        else:
            cur = r1(sum(series[c][cur_q] for _, c in lines))
            pri = r1(sum(series[c][prior_q] for _, c in lines))
            parts = [[lab, r1(series[c][cur_q])] for lab, c in lines]
        report.check([p[0] for p in parts] == [p[0] for p in g["parts"]],
                     f"HOLD: {g['name']} keeps its component lines")
        groups.append({"name": g["name"], "color": g["color"], "cur": cur, "prior": pri, "parts": parts})

    def pair(key):
        s = series[Z1_TOTALS[key]]
        return [r1(s[prior_q]), r1(s[cur_q])]

    def month_row(rows, month):
        hit = [r for r in rows if r[0] == month]
        if not hit:
            raise CheckFailed(f"HOLD: DATA has no month {month} to compare with Z.1")
        return hit[0]

    months = (prior[:7], asof[:7])
    new = {
        "asof": asof, "prior": prior, "release": old["release"], "next": old["next"], "groups": groups,
        "held": pair("held"), "disc": pair("disc"), "z1mkt": pair("z1mkt"),
        "mspdMkt": [r1(sum(c[0] for c in month_row(data["mkt"], m)[1]) / 1000) for m in months],
        "public": [r1(month_row(data["totals"], m)[1] / 1000) for m in months],
    }
    new = order_like(new, old)

    # ---- checks
    for i, label in ((1, "current"), (0, "prior")):
        s = sum(g["cur" if i else "prior"] for g in groups)
        report.check(close(s, new["held"][i], 1.0), f"HOLD: groups sum to Total assets within $1B ({label})",
                     f"{s:,.1f} vs {new['held'][i]:,.1f}")
        lhs = (new["held"][i] + new["disc"][i] + (new["mspdMkt"][i] - new["z1mkt"][i])
               + (new["public"][i] - new["mspdMkt"][i]))
        report.check(close(lhs, new["public"][i], 0.25),
                     f"HOLD: held + disc + (mspdMkt - z1mkt) + (public - mspdMkt) = public ({label})",
                     f"{lhs:,.1f} vs {new['public'][i]:,.1f}")

    # ---- compare with the page
    changed = {k: v for k, v in new.items() if k not in ("release", "next") and v != old[k]}
    if not changed:
        report.say(f"HOLD: Z.1 values from {source} match the page for {prior} and {asof} (rounded to 0.1).")
        return None
    if new["asof"] != old["asof"] or new["held"] != old["held"]:
        new["release"], new["next"] = z1_release_dates(report, old)
    report.say(f"HOLD: quarter {asof} from {source} (page had {old['asof']}); "
               f"release {new['release']}, next {new['next']}.")
    report.say(f"  {'group':<28} {'page prior':>10} {'new prior':>10} {'page cur':>10} {'new cur':>10}  $B")
    for g_old, g_new in zip(old["groups"], groups):
        flag = "" if (g_old["cur"], g_old["prior"], g_old["parts"]) == \
            (g_new["cur"], g_new["prior"], g_new["parts"]) else "  *"
        report.say(f"  {g_new['name']:<28} {g_old['prior']:>10,.1f} {g_new['prior']:>10,.1f} "
                   f"{g_old['cur']:>10,.1f} {g_new['cur']:>10,.1f}{flag}")
    for key in ("held", "disc", "z1mkt", "mspdMkt", "public"):
        flag = "" if old[key] == new[key] else "  *"
        report.say(f"  {key:<28} {old[key][0]:>10,.1f} {new[key][0]:>10,.1f} "
                   f"{old[key][1]:>10,.1f} {new[key][1]:>10,.1f}{flag}")
    report.say("  (* = differs from the page; a revision if the quarter is unchanged)")
    return new


# ------------------------------------------------------------- 3. TIC

def parse_tic(text):
    """Table 5 is tab-delimited: country, then 13 month columns headed YYYY-MM, newest first."""
    lines = text.replace("\r", "").split("\n")
    head = None
    for i, line in enumerate(lines):
        cells = [c.strip() for c in line.split("\t")]
        months = [c for c in cells if re.fullmatch(r"\d{4}-\d{2}", c)]
        if len(months) >= 13:
            head, cols = i, [(j, c) for j, c in enumerate(cells) if re.fullmatch(r"\d{4}-\d{2}", c)]
            break
    if head is None:
        raise CheckFailed("TIC Table 5: no header row with 13 YYYY-MM columns")
    dates = [c for _, c in cols]
    rows, order, official_seen = {}, [], False
    for line in lines[head + 1:]:
        cells = [c.strip() for c in line.split("\t")]
        name = re.sub(r"\s+", " ", cells[0]).strip().rstrip(":")
        if not name:
            continue
        try:
            vals = [float(cells[j].replace(",", "")) for j, _ in cols]
        except (IndexError, ValueError):
            continue
        key = name.lower()
        if key == "grand total":
            rows["Grand Total"] = vals
        elif key.startswith("all other"):
            rows["All Other"] = vals
        elif "foreign official" in key:
            rows["Foreign Official"] = vals
            official_seen = True
        elif "Grand Total" in rows or official_seen:
            continue                        # bills / bonds lines under the official row
        elif name not in rows:
            rows[name] = vals
            order.append(name)
    release = parse_text_date("\n".join(lines[:head]))
    return dates, rows, order, release


def tic_release_date(asof, report, old):
    try:
        text = re.sub(r"<[^>]+>", " ", fetch(TIC_PRESS))
    except CheckFailed as e:
        report.warn(f"TIC press release page not read: {e}")
        return None
    y, m = int(asof[:4]), int(asof[5:])
    month_name = calendar.month_name[m]
    for hit in re.finditer(r"([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s*\d{4})[^.]{0,200}?" + month_name + r"\s+" + str(y),
                           text):
        d = parse_text_date(hit.group(1))
        if d:
            return nice_date(d)
    return None


def update_tic(old, report):
    text = fetch(TIC_TABLE5)
    dates, rows, order, release = parse_tic(text)
    if dates[0] > dates[-1]:
        dates = dates[::-1]
        rows = {k: v[::-1] for k, v in rows.items()}
    for need in ("Grand Total", "All Other", "Foreign Official"):
        if need not in rows:
            raise CheckFailed(f"TIC Table 5: no '{need}' row")

    named = {n: rows[n] for n in order}
    top20 = order[:20]
    newcomers = [n for n in top20 if n not in old["countries"]]
    if newcomers:
        raise CheckFailed(f"TIC Table 5: new country in the top 20, not reshaping: {newcomers}. "
                          f"Update the country list by hand.")
    missing = [c for c in old["countries"] if c not in named]
    if missing:
        raise CheckFailed(f"TIC Table 5: countries not found: {missing}")

    asof = dates[-1]
    if asof < old["asof"]:
        raise CheckFailed(f"TIC Table 5 newest month {asof} is older than the page's {old['asof']}")
    new = json.loads(json.dumps(old))
    y, m = map(int, new["dates"][-1].split("-"))
    while new["dates"][-1] < asof:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        new["dates"].append(f"{y:04d}-{m:02d}")
        for s in new["v"].values():
            s.append(None)
    pos = {d: i for i, d in enumerate(new["dates"])}
    series = dict(named, **{"Grand Total": rows["Grand Total"], "Foreign Official": rows["Foreign Official"]})
    for name in new["v"]:
        for d, v in zip(dates, series[name]):
            if d in pos:
                new["v"][name][pos[d]] = r1(v)
    gaps = [n for n, s in new["v"].items() if None in s]
    if gaps:
        raise CheckFailed(f"TIC: months between {old['asof']} and {asof} not covered by Table 5 for {gaps}")
    ao = dict(zip(dates, rows["All Other"]))
    yago = f"{int(asof[:4]) - 1}{asof[4:]}"
    prev = dates[-2]
    if yago not in ao:
        raise CheckFailed(f"TIC Table 5 has no column for {yago}")
    new["allOther"] = [r1(ao[asof]), r1(ao[prev]), r1(ao[yago])]
    new["asof"] = asof
    if asof != old["asof"]:
        rel = (nice_date(release) if release else None) or tic_release_date(asof, report, old)
        if rel is None:
            report.warn("TIC release date not found; set to today's date")
            rel = nice_date(dt.date.today())
        new["release"] = rel

    # ---- checks
    n = len(new["dates"])
    report.check(all(len(s) == n for s in new["v"].values()), "TIC: every series has the same length as dates")
    s = sum(new["v"][c][-1] for c in new["countries"]) + new["allOther"][0]
    report.check(close(s, new["v"]["Grand Total"][-1], 1.0),
                 "TIC: 20 countries + All Other equal Grand Total within $1B (newest month)",
                 f"{s:,.1f} vs {new['v']['Grand Total'][-1]:,.1f}")

    if new == old:
        report.say(f"TIC: no new month and no revisions (page {old['asof']}).")
        return None
    revised = sum(1 for name in old["v"] for a, b in zip(old["v"][name], new["v"][name]) if a != b)
    report.say(f"TIC: newest month {asof} (page had {old['asof']}); {revised} earlier values revised.")
    for name in ("Grand Total", "Japan", "United Kingdom", "China, Mainland", "Foreign Official"):
        report.say(f"  {name:<20} {old['v'][name][-1]:>9,.1f} ({old['asof']}) -> "
                   f"{new['v'][name][-1]:>9,.1f} ({asof}) $B")
    return new


# ------------------------------------------------------- offline checks

def check_current(objs, report):
    """Validate the objects already in index.html (no network)."""
    D, H, T = objs["DATA"], objs["HOLD"], objs["TIC"]
    t1, t2 = D["table1"], D["table2"]
    last = D["totals"][-1]
    report.check(last[0] == D["asof"][:7] and all(close(a, b, 0.05) for a, b in zip(last[1:], t1["total"])),
                 "DATA: latest totals row equals table1.total")
    report.check(all(close(a, b, 0.05) for a, b in zip(t1["total"], t2["total"])),
                 "DATA: table1.total equals table2.total")
    intra = sum(h["cur"] for h in D["holders"]) + D["holders_other"]
    gas = t1["nonmarketable"]["Government Account Series"][1]
    report.check(close(intra, gas, 1.0), "DATA: intragov fund rows sum to Government Account Series within $1M",
                 f"{intra:,.1f} vs {gas:,.1f}")
    for i in range(3):
        s = sum(v[i] for v in t1["marketable"].values())
        report.check(close(s, t1["total_marketable"][i], 1.0),
                     f"DATA: marketable classes sum to total_marketable (column {i + 1}) within $1M")
    report.check(D["mktcols"] == MKTCOLS and D["mkt"][-1][0] == D["asof"][:7], "DATA: mkt columns and month")
    table2_identities(t2, report)

    for i, label in ((1, "current"), (0, "prior")):
        s = sum(g["cur" if i else "prior"] for g in H["groups"])
        report.check(close(s, H["held"][i], 1.0), f"HOLD: groups sum to Total assets within $1B ({label})",
                     f"{s:,.1f} vs {H['held'][i]:,.1f}")
        lhs = H["held"][i] + H["disc"][i] + (H["mspdMkt"][i] - H["z1mkt"][i]) + (H["public"][i] - H["mspdMkt"][i])
        report.check(close(lhs, H["public"][i], 0.25), f"HOLD: reconciliation identity ({label})")
    for i, m in ((0, H["prior"][:7]), (1, H["asof"][:7])):
        row = [x for x in D["mkt"] if x[0] == m]
        if row:
            v = r1(sum(c[0] for c in row[0][1]) / 1000)
            report.check(close(v, H["mspdMkt"][i], 0.1), f"HOLD: mspdMkt matches DATA.mkt for {m}")
        tot = [x for x in D["totals"] if x[0] == m]
        report.check(bool(tot) and close(r1(tot[0][1] / 1000), H["public"][i], 0.1),
                     f"HOLD: public matches DATA.totals for {m}")

    n = len(T["dates"])
    report.check(all(len(s) == n for s in T["v"].values()), "TIC: every series has the same length as dates")
    report.check(T["dates"][-1] == T["asof"] and len(T["countries"]) == 20, "TIC: asof and 20 countries")
    s = sum(T["v"][c][-1] for c in T["countries"]) + T["allOther"][0]
    report.check(close(s, T["v"]["Grand Total"][-1], 1.0),
                 "TIC: 20 countries + All Other equal Grand Total within $1B",
                 f"{s:,.1f} vs {T['v']['Grand Total'][-1]:,.1f}")


def bill_share(D):
    """Same formula as the page: (Bills public + intragov) / (all marketable public + intragov),
    from the latest DATA.mkt row."""
    cols = D["mkt"][-1][1]
    bills = sum(cols[D["mktcols"].index("Bills")])
    share = 100 * bills / sum(sum(c) for c in cols)
    # round like JavaScript's toFixed(1): half up on the exact binary value
    return float(decimal.Decimal(share).quantize(decimal.Decimal("0.1"), rounding=decimal.ROUND_HALF_UP))


def summary(objs):
    D, H, T = objs["DATA"], objs["HOLD"], objs["TIC"]
    last = D["totals"][-1]
    return {
        "statement_date": D["asof"],
        "units": "millions of dollars",
        "total_public_debt": last[3],
        "held_by_public": last[1],
        "intragovernmental": last[2],
        "bill_share_pct": bill_share(D),
        "debt_limit": D["table2"]["limit"],
        "debt_limit_room": D["table2"]["balance"],
        "holders_quarter": H["asof"],
        "holders_release": H["release"],
        "foreign_holders_month": T["asof"],
        "foreign_holders_release": T["release"],
    }


# -------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and check, write nothing")
    ap.add_argument("--check", action="store_true", help="validate index.html without network access")
    ap.add_argument("--message-file", help="write the commit message here when something changed")
    ap.add_argument("--report-file", help="also write the report here (e.g. the GitHub job summary)")
    ap.add_argument("--alert-file", help="write non-blocking problems that need a person here")
    ap.add_argument("--hold", choices=("auto", "always"), default="auto",
                    help="auto: read Z.1 only when a new release is due (default); always: read it anyway")
    args = ap.parse_args()

    report = Report()
    page = read_page()
    code = 0
    try:
        _, found = find_objects(page)
        objs = {k: v for k, (_, v) in found.items()}
        roundtrip = rebuild_page(page, objs)
        report.check(roundtrip == page, "page: re-serializing the current objects reproduces index.html exactly")
        if args.check:
            report.say("Offline check of the data in index.html.")
            check_current(objs, report)
            check_page(page, page, report)
            changed = []
        else:
            new = dict(objs)
            changed = []
            hold = objs["HOLD"]
            for name, fn in (("DATA", lambda: update_data(objs["DATA"], report)),
                             ("HOLD", lambda: update_hold(hold, new["DATA"], report)),
                             ("TIC", lambda: update_tic(objs["TIC"], report))):
                if name == "HOLD" and args.hold == "auto" and not hold_due(hold):
                    report.say(f"HOLD: skipped; no new Z.1 release due until {hold['next']} "
                               f"(page has the {hold['release']} release).")
                    report.say()
                    continue
                try:
                    result = fn()
                except SourceUnavailable as e:
                    if name != "HOLD":
                        report.check(False, f"{name}: {e}")
                    else:           # keep the current HOLD so DATA and TIC can still update
                        msg = f"HOLD not updated, kept the current values: {e}"
                        report.warn(msg)
                        report.alerts.append(msg)
                    result = None
                except CheckFailed as e:
                    report.check(False, f"{name}: {e}")
                    result = None
                if result is not None:
                    new[name] = result
                    changed.append(name)
                report.say()
            if changed:
                new_page = rebuild_page(page, new)
                check_page(page, new_page, report)
            sm = summary(new)
            try:
                with open(SUMMARY, encoding="utf-8") as f:
                    sm_changed = json.load(f) != sm
            except (OSError, ValueError):
                sm_changed = True
            if not report.errors and not args.dry_run and (changed or sm_changed):
                if changed:
                    with open(INDEX, "w", encoding="utf-8") as f:
                        f.write(new_page)
                os.makedirs(os.path.dirname(SUMMARY), exist_ok=True)
                with open(SUMMARY, "w", encoding="utf-8") as f:
                    json.dump(sm, f, indent=2)
                    f.write("\n")
                if args.message_file:
                    with open(args.message_file, "w", encoding="utf-8") as f:
                        f.write(commit_message(objs, new, changed) + "\n")
            if args.dry_run:
                report.say("Dry run: nothing written." if changed else "Dry run: nothing new to write.")
    except CheckFailed as e:
        report.check(False, str(e))
    if report.errors:
        code = 1
        report.say("Nothing was written because a check failed.")
    out = report.render()
    print(out)
    if args.alert_file and report.alerts:
        with open(args.alert_file, "w", encoding="utf-8") as f:
            f.write("\n".join(report.alerts) + "\n")
    if args.report_file:
        with open(args.report_file, "a", encoding="utf-8") as f:
            f.write("```\n" + out + "\n```\n")
    return code


def commit_message(old, new, changed):
    parts = []
    if "DATA" in changed:
        parts.append(f"MSPD to {new['DATA']['asof']}")
    if "HOLD" in changed:
        parts.append(f"Z.1 holders to {new['HOLD']['asof']} ({new['HOLD']['release']} release)")
    if "TIC" in changed:
        if new["TIC"]["asof"] != old["TIC"]["asof"]:
            parts.append(f"TIC to {new['TIC']['asof']}")
        else:
            parts.append("TIC revisions")
    return "Update " + "; ".join(parts) if parts else "Update data/summary.json"


if __name__ == "__main__":
    sys.exit(main())
