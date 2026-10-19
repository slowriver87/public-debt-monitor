# Public Debt Monitor

> **For education only. Not financial advice.** Figures come from public government sources and are checked automatically, but may contain errors. Check the original source before relying on them.

A single-page dashboard of U.S. federal debt: how much there is, what kind of securities it is made of, how close it is to the statutory limit, which government funds hold it, who holds the part owned by the public, and which countries hold the most.

The page is `index.html`, a self-contained file served by GitHub Pages. Every number on it comes from three one-line JavaScript objects in that file; the tiles, charts, tables and notes are drawn from them when the page loads.

## Sources

| Object | What | Source | Frequency | Units |
|---|---|---|---|---|
| `DATA` | Monthly Statement of the Public Debt (MSPD): totals, marketable and nonmarketable securities, debt limit, intragovernmental fund holdings | U.S. Treasury, [Fiscal Data API](https://fiscaldata.treasury.gov/datasets/monthly-statement-public-debt/) (`mspd_table_1`, `mspd_table_2`, `mspd_table_3_nonmarket`); cross-checked with [Debt to the Penny](https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/) | Monthly, around the 4th–7th business day | $ millions |
| `HOLD` | Who holds debt held by the public (foreign holders, the Fed, money market funds, households, banks, pensions, …) | Federal Reserve, [Financial Accounts of the United States (Z.1)](https://www.federalreserve.gov/releases/z1/), table F3.2.s "Treasury securities", read line by line from [FRED](https://fred.stlouisfed.org/) (series `BOGZ1<code>Q`), or from the [Fed's own table](https://www.federalreserve.gov/releases/z1/current/html/F3_2_s.htm) by series code if FRED cannot be read | Quarterly (March, June, September, December) | $ billions |
| `TIC` | Foreign holders by country | U.S. Treasury, [TIC Table 5: Major Foreign Holders of Treasury Securities](https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/slt_table5.html); history back to 2014 from `mfhhis01` | Monthly, around the 15th–18th, for the month two months earlier | $ billions |

`data/summary.json` is a small summary for other pages: latest statement date, total, held by the public, intragovernmental, bill share (the same formula as the page: bills, public plus intragovernmental, as a percentage of all marketable debt, public plus intragovernmental, from the latest month in `DATA.mkt`), debt limit and room under it, and the dates of the holder data.

## How the update works

`scripts/update.py` is a plain Python script (standard library only, no AI). A GitHub Actions workflow, `.github/workflows/update.yml`, runs it every day.

1. **Fetch.** For each source it asks whether there is anything newer than what the page shows:
   - `DATA`: a newer `record_date` in MSPD Table I than `DATA.asof`. New months are appended; the detailed tables are rebuilt for the latest month.
   - `HOLD`: only once a new Z.1 release could be out, i.e. on or after the next scheduled release date stored in `HOLD.next`; on other days it is skipped. When due, it reads FRED, and if FRED cannot be read it falls back to the Fed's own table F3.2.s, matching lines by series code (e.g. `FL313161105`). The Fed revises history with every release, so both quarters shown are taken from the newest release. If neither source can be read, the page keeps its current holder figures, the run summary shows a warning, and an issue is opened, but the run does not fail, so the monthly data can still update.
   - `TIC`: a newer month in Table 5. New months are appended, then the last 13 months of every series are overwritten with Table 5's values, because Treasury revises them. Rows are matched by their exact label (after trimming spaces), never by partial text: countries by the names on the page, and "Grand Total", "All Other" and "Of Which: Foreign Official" exactly.
2. **Check.** Nothing is written unless every check passes:
   - MSPD: the new totals row equals Table I's total, which equals Table II's total; the intragovernmental fund rows add up to the Government Account Series (within $1M); the marketable classes add up to total marketable (within $1M); the debt-limit arithmetic in Table II adds up. The total is also compared with Debt to the Penny; a gap over $1M is reported but does not block.
   - Z.1: the ten holder groups add up to "Total assets" (within $1B); held + discrepancy + (MSPD marketable − Z.1 marketable) + (public − MSPD marketable) = public; every line is present for both quarters (a missing line usually means the Fed renamed or renumbered it).
   - TIC: every series has as many values as there are months; the 20 countries plus "All Other" add up to the Grand Total (within $1B) for the newest month; Foreign Official equals its Treasury Bills plus its T-Bonds & Notes rows (within $1B) for every month read; no existing value is revised by more than 5% (if any would be, the run stops and lists them instead of overwriting); if a country not on the page enters the top 20, the script stops instead of reshaping the page.
   - The page: only the three data lines change, the education-only note is still there, each object keeps its keys, and the page's scripts still parse.
3. **Write and commit.** If something changed and every check passed, the script rewrites only those three lines of `index.html` (compact JSON) plus `data/summary.json`, and the workflow commits with a message saying what was updated, e.g. `Update MSPD to 2026-10-31; TIC to 2026-09`. GitHub Pages republishes the page. The run summary lists every value that changes, old → new.
4. **On a failed check** nothing is committed, the workflow run fails (GitHub emails the repository owner), and the workflow opens or updates an issue titled "Data update needs attention" with the check report.

Every request sends a normal browser User-Agent, waits up to 60 seconds, and is tried 3 times with a short pause before a source counts as unreadable.

When no source has anything new, the run ends quietly.

### Running it by hand

```sh
python scripts/update.py --check     # no network: validate the data already in index.html
python scripts/update.py --dry-run   # fetch and check, report what would change, write nothing
python scripts/update.py             # fetch, check, and write if anything changed
```

From GitHub: **Actions → Update data → Run workflow**. "Dry run" is ticked by default: the run fetches and checks everything and shows the comparison with the current page in the run's summary, without committing. Untick it to run a real update. Tick "Check holders" to read the Z.1 data even when no new release is due, for example to compare the current page with FRED.

## Not automated

- The written explanations on the page (field guide, notes, source list). These are reviewed by hand about once a quarter.
- If Table 5's top 20 gains a new country, the country list on the page is updated by hand.
