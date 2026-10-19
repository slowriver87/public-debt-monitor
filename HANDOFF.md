# Public Debt Monitor: move to GitHub

Brief for Claude Code. Read this first, then `index.html`.

## Goal

Host this dashboard on GitHub Pages and refresh it with a GitHub Action running a plain Python script. No AI in the refresh: the script fetches public data, rebuilds the data blocks in `index.html`, runs the checks below, and commits only if every check passes.

## Rules

- The repo will be **public**. Do not put any personal information in any file, commit, comment or workflow: no names, no email addresses, no links to claude.ai. Any alert email address goes in a GitHub Actions secret, never in the code.
- Before the first commit, confirm the git author email is the GitHub no-reply address (`git config user.email`), not a personal email.
- Do not change the page's design, CSS, markup or explanatory text. The script changes only the three data objects.
- Keep the "For education only. Not financial advice." note at the top of the page.

## The page

`index.html` is a single self-contained page. All numbers come from three one-line JavaScript objects:

1. `const DATA = {...};` in the first `<script>`: the monthly debt statement (MSPD).
2. `const HOLD = {...};` at the top of the last `<script>`: who holds debt held by the public, quarterly.
3. `const TIC = {...};` right after HOLD: foreign holders by country, monthly.

Everything else on the page (tiles, charts, tables, notes) is drawn from these objects when the page loads. The script should replace each object's line with new compact JSON (`separators=(',',':')`).

## 1. DATA: Monthly Statement of the Public Debt

- Source: Treasury Fiscal Data API, `https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/debt/mspd/`
- Published around the 4th–7th business day of each month. New statement = latest `record_date` in `mspd_table_1` later than `DATA.asof`.
- Units: millions of dollars. Keep 1 decimal.
- Shape (keep every key):
  - `asof`, `prior`: statement dates (YYYY-MM-DD).
  - `totals`: `[["YYYY-MM", public, intragov, total], ...]` from `mspd_table_1`, security_type_desc "Total Public Debt Outstanding" (fields debt_held_public_mil_amt, intragov_hold_mil_amt, total_mil_amt). Append new months.
  - `mktcols`: `["Bills","Notes","Bonds","TIPS","FRN","FFB"]` (fixed).
  - `mkt`: `[["YYYY-MM", [[public, intragov] × 6 in mktcols order]], ...]` from the "Marketable" rows (classes Bills, Notes, Bonds, Treasury Inflation-Protected Securities, Floating Rate Notes, Federal Financing Bank). Append new months.
  - `table1`: full Table I for the latest month: `marketable` {Bills, Notes, Bonds, TIPS, Floating Rate Notes, Federal Financing Bank}, `total_marketable`, `nonmarketable` {Domestic Series, State and Local Government Series, U.S. Savings Securities, Government Account Series, Other}, `total_nonmarketable`, `total`. Each value is `[public, intragov, total]`.
  - `table2`: from `mspd_table_2`: `total`, `other_debt_not_subject`, `unamortized_discount`, `ffb`, `guaranteed`, `subject_to_limit` (each `[public, intragov, total]`), plus `limit` and `balance` (numbers).
  - `holders`: top 40 intragovernmental funds from `mspd_table_3_nonmarket` (security_class2_desc "Intragovernmental Holdings") as `{name, prior, issued, redeemed, cur}`; redeemed stays negative; strip trailing dots from names. `holders_other` and `holders_other_prior`: sums for all remaining intragov rows.
  - `gas_public`: top 6 rows with security_class2_desc "Held By the Public" as `{name, prior, cur}`.
- Checks (do not publish if any fails):
  - The new totals row equals `table1.total`, and `table1.total` equals `table2.total`.
  - The sum of all intragov fund rows equals `table1.nonmarketable["Government Account Series"][1]` within $1M.
  - The marketable classes sum to `total_marketable` within $1M.
  - Second read: compare the total with Debt to the Penny for the same date (`/v2/accounting/od/debt_to_penny`). Report a gap over $1M, but don't block on it.

## 2. HOLD: who holds the debt (quarterly)

- Source: Federal Reserve Financial Accounts (Z.1), table F3.2.s "Treasury securities": `https://www.federalreserve.gov/releases/z1/current/html/F3_2_s.htm`. Released in March, June, September and December; the next release is Dec 10, 2026. The Fed revises history each release, so take both quarters from the newest release. FRED carries the same lines (for example BOGZ1LM263061105Q for foreign holders) if a CSV is easier.
- Units: billions of dollars.
- Shape:
  - `asof`, `prior`: the newest quarter-end and the same quarter a year earlier.
  - `release`, `next`: release dates as text, e.g. "Sep 11, 2026".
  - `groups`: 10 groups, each `{name, color, cur, prior, parts: [[label, value], ...]}`. Keep the names, colors and component lines exactly as in the current file. The mapping from Z.1 lines:
    - Foreign holders: Rest of the world (parts: its bills and other).
    - Federal Reserve: Central bank (bills, other).
    - Money market funds (bills, other).
    - Households and nonprofits: Household sector.
    - Mutual funds and ETFs: mutual funds + ETFs + closed-end funds.
    - Banks: U.S.-chartered depository institutions + holding companies + foreign banking offices + credit unions + banks in U.S.-affiliated areas.
    - State and local governments.
    - Pensions: private + state and local govt. + federal government pension funds.
    - Insurers: property-casualty + life.
    - Other: brokers and dealers + GSEs + nonfinancial corporate + hedge funds + other financial business + nonfinancial noncorporate + ABS issuers.
  - `held`: "Total assets" line, `[prior, cur]`.
  - `disc`: "Discrepancy" line.
  - `z1mkt`: "Total liabilities" line.
  - `mspdMkt`: sum of the six marketable classes held by the public in `DATA.mkt` for each quarter-end month, in $B.
  - `public`: `DATA.totals` public for each quarter-end month, in $B.
- Checks:
  - The groups sum to "Total assets" within $1B.
  - `held + disc + (mspdMkt − z1mkt) + (public − mspdMkt) = public`.
  - If the Fed renames or renumbers lines, map them by description and flag it.

## 3. TIC: foreign holders by country (monthly)

- Current source: Treasury TIC Table 5, `https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/slt_table5.txt` (13 months, billions of dollars; an .html version is also available). Released around the 15th–18th of each month, for the month two months earlier. Do not use the legacy `mfh.txt`, which stops at January 2023.
- History source (already loaded back to January 2014): `mfhhis01.txt` / `.csv` in the same folder.
- Shape:
  - `asof`: newest month (YYYY-MM).
  - `release`: TIC press release date as text.
  - `dates`: list of months from 2014-01.
  - `countries`: the 20 country names (fixed order, as in the file).
  - `v`: `{name: [values aligned with dates]}` for the 20 countries plus "Grand Total" and "Foreign Official".
  - `allOther`: `[newest, month before, same month a year earlier]` for the "All Other" row.
- Update: append new month(s), then overwrite the last 13 months of every series with Table 5's values, since Treasury revises them.
- Checks:
  - Every series has the same length as `dates`.
  - The 20 countries plus All Other sum to the Grand Total within $1B for the newest month.
  - If Table 5's top 20 adds a country not in `countries`, do not reshape. Open an issue or send an alert instead.

## Schedule

- Run daily, or on the 4th–10th and 15th–20th of each month. The script exits quietly when no source has new data.
- Commit only when something changed and every check passed. The commit message should say what was updated.
- On a failed check: do not commit, and fail the workflow so GitHub emails the repo owner.

## Not automated (stays with Claude, occasionally)

- The written explanations on the page (field guide, notes). Plan a manual review about once a quarter.
- The Macro Cockpit card. Later, have the cockpit read a small `data/summary.json` that this script writes, with the latest statement date, total, public, intragov, bill share, debt-limit room and holder dates.

## Suggested repo layout

```
index.html              the page (served by GitHub Pages)
scripts/update.py       fetch, rebuild, check
data/summary.json       small summary for other pages
.github/workflows/update.yml
README.md               what it is, sources, how the update works, the education-only note
```
