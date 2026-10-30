# analyst-copilot: an MCP server for SEC filings

An [MCP](https://modelcontextprotocol.io) server that gives Claude three tools for
equity research, built on SEC EDGAR data via [EdgarTools](https://github.com/dgunning/edgartools).
You ask questions in plain English, and Claude pulls the numbers and filing text
straight from 10-Qs and 10-Ks.

## Why

During earnings season an analyst covering a group of companies does the same
three things again and again as each company reports:

1. **Check the numbers.** Is revenue growth speeding up or slowing down? Are margins expanding?
2. **Compare with peers.** How does this quarter stack up against competitors?
3. **Read the filing for what changed.** What is management saying now that it
   wasn't saying a year ago? New risks, new customers, softer language about demand?

Steps 1 and 2 mean copying figures out of filings into a spreadsheet. Step 3 means
reading two 40-page MD&A sections side by side. Each tool below automates one of
these steps so Claude can do the groundwork and you can spend your time on the analysis.

## The tools

### `quarterly_kpi_trend(ticker, quarters=8)`

Quarterly revenue, gross profit, and operating income, plus gross margin, operating
margin, and year-over-year revenue growth, with the fiscal period end date for each quarter.

> *"How have NVIDIA's margins trended over the last 8 quarters? Is growth decelerating?"*

```
Quarter     Period End  Revenue  Gross Profit  Op Income  Gross Mgn  Op Mgn  Rev YoY
----------  ----------  -------  ------------  ---------  ---------  ------  -------
FY2027 Q2   2026-07-26   96,221        72,142     63,734      75.0%   66.2%   105.9%
FY2027 Q1   2026-04-26   81,615        61,157     53,536      74.9%   65.6%    85.2%
FY2026 Q4*  2026-01-25   68,127        51,093     44,299      75.0%   65.0%    73.2%
...
* Q4 derived: full-year (10-K) minus nine-month YTD (Q3 10-Q).
```

Companies don't file a 10-Q for Q4, so Q4 is derived as **full year (10-K) minus
nine-month year-to-date (Q3 10-Q)** and marked with `*`. Metrics a company doesn't
report (banks have no gross profit) show `N/A`.

### `peer_snapshot(tickers)`

Side-by-side comparison of each company's most recent fiscal year: revenue, growth
vs. prior year, gross margin, operating margin, and fiscal year end date.

> *"Compare NVDA, AMD, and INTC on growth and profitability."*

```
                       NVDA                         AMD        INTC
--------------  -----------  --------------------------  ----------
Fiscal Year          FY2026                      FY2025      FY2025
FY End           2026-01-25                  2025-12-27  2025-12-27
Revenue             215,938                      34,639      52,853
Rev Growth YoY        65.5%                       34.3%       -0.5%
Gross Margin          71.1%                       49.5%       34.8%
Op Margin             60.4%                       10.7%       -4.2%
```

The FY End row matters: fiscal calendars differ (NVIDIA's year ends in late
January), so the columns don't always cover the same months. If one ticker fails,
the others are still returned and the error is listed underneath.

### `filing_language_changes(ticker, form="10-Q", max_changes=15)`

Compares the MD&A section of the latest 10-Q (or 10-K) with the comparable filing
from a year earlier. For a 10-Q that's the same quarter last year, which avoids
seasonal differences. Returns paragraphs that are **new**, **removed**, or
**changed** (old vs. new wording), with links to both filings.

> *"What's new in the MD&A of NVIDIA's latest 10-Q compared with a year ago?"*

```
(c) CHANGED paragraphs — same passage, reworded  [showing 5 of 34]

4. (50 words differ, 65% similar)
   OLD: Direct Customers – For the second quarter of fiscal year 2026, sales to one
        direct customer, Customer A, represented 23% of total revenue; and sales to
        a second direct customer, Customer B, represented 16% ...
   NEW: Direct Customers – For the second quarter of fiscal year 2027, one direct
        customer represented 16% of total revenue ... three direct customers
        represented 16%, 15%, and 13% of total revenue ...
```

How it works:
- **Ignoring trivial edits:** numbers, dates, and quarter/year references are
  ignored when comparing, so "revenue was $X, up Y%" with new figures doesn't count as a change.
- **Pairing paragraphs:** paragraphs are aligned in document order with Python's
  `difflib` before edited ones are paired, so look-alike sentences from different
  sections aren't matched to each other.
- **Ranking:** results are ranked by how many words changed and capped, so the
  output doesn't flood Claude's context.

## Setup

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/mmmelowang/MCP-for-SEC-filings.git
cd MCP-for-SEC-filings
uv sync
```

**Set your SEC identity.** The SEC requires every automated request to include a
contact email. The server reads it from the `EDGAR_IDENTITY` environment variable,
so it never has to be written into the code:

```bash
export EDGAR_IDENTITY="Your Name your.email@example.com"
```

**Check that the tools work** by calling them directly, without Claude:

```bash
uv run python test_server.py
```

### Add to Claude Code

```bash
claude mcp add --scope user analyst-copilot \
  -e EDGAR_IDENTITY="Your Name your.email@example.com" \
  -- "$(which uv)" --directory "$(pwd)" run server.py
```

Start a new Claude Code session and run `/mcp` to confirm `analyst-copilot` is connected.

### Add to Claude Desktop

Add an `mcpServers` entry to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows), using absolute paths:

```json
{
  "mcpServers": {
    "analyst-copilot": {
      "command": "/absolute/path/to/uv",
      "args": ["--directory", "/absolute/path/to/MCP-for-SEC-filings", "run", "server.py"],
      "env": { "EDGAR_IDENTITY": "Your Name your.email@example.com" }
    }
  }
}
```

Use a full path to `uv` (find it with `which uv`), because Claude Desktop doesn't
inherit your shell's PATH. Quit Claude Desktop fully (⌘Q) and reopen it. If the
tools don't appear, check `~/Library/Logs/Claude/mcp-server-analyst-copilot.log`.

## Known limitations

- **No price data, so no valuation multiples.** Everything comes from SEC filings.
  There is no stock price or market cap, so no P/E, EV/EBITDA, or EV/Sales.
  Pair this with a market-data source for valuation work.
- **SEC filing delays.** Numbers appear only once a company files, usually weeks
  after the earnings release (up to 40–45 days after quarter end for a 10-Q,
  60–90 days for a 10-K). On earnings day, the press release and call come
  before the filing, so the latest quarter may not be here yet.
- **Section extraction can fail on unusual filings.** MD&A extraction depends on
  how a filing's HTML is structured. Filings with non-standard layouts, MD&A
  included by reference to an exhibit (common for some banks and insurers), or
  10-Q/As and transition-period filings may not parse. The tool then returns an
  error instead of guessing.
- **Reported GAAP only.** XBRL data is as-filed GAAP, so there are no adjusted or
  non-GAAP figures, and companies that use unusual XBRL tags for revenue or gross
  profit can show `N/A`.
- **Heavily rewritten paragraphs** (less than 50% similar) appear as one removed and
  one new paragraph rather than as a single "changed" one.
- **This is a focused learning project, not a replacement for the official server.**
  EdgarTools ships its own, much more complete
  [MCP server](https://github.com/dgunning/edgartools). This project builds three
  opinionated tools from scratch to learn how MCP servers, XBRL data, and filing
  text work.

## What I learned

<!-- Fill these in yourself -->

- **About MCP:** _…_
- **About SEC data / XBRL:** _…_
- **About writing tools for an LLM (docstrings, output size, error handling):** _…_
- **What surprised me:** _…_
- **What I'd build next:** _…_

## Project files

| File | Purpose |
|---|---|
| `server.py` | The MCP server and all three tools |
| `test_server.py` | Calls each tool directly for NVDA, AAPL, AMD, INTC and prints the results |
| `test_edgar.py` | Early EdgarTools smoke test |
