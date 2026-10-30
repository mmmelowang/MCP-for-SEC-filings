"""analyst-copilot: an MCP server that gives Claude tools for analyzing SEC filings."""

import difflib
import os
import re
import sys
from datetime import date, timedelta

import pandas as pd
from edgar import Company, set_identity
from mcp.server.mcpserver import MCPServer  # mcp 2.x name for FastMCP

# SEC requires every requester to identify themselves with a contact email.
# Read it from the environment so no personal email lives in the code.
_identity = os.environ.get("EDGAR_IDENTITY")
if not _identity:
    sys.exit(
        "EDGAR_IDENTITY is not set. The SEC requires a contact email for API access, e.g.\n"
        '  export EDGAR_IDENTITY="Your Name your.email@example.com"'
    )
set_identity(_identity)

mcp = MCPServer("analyst-copilot")

# XBRL tags to try for each metric, in priority order. Companies switch tags over
# time (e.g. Apple used SalesRevenueNet before ASC 606), so we merge across them.
REVENUE_TAGS = [
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:SalesRevenueNet",
    "us-gaap:RevenuesNetOfInterestExpense",  # banks / broker-dealers
]
GROSS_PROFIT_TAGS = ["us-gaap:GrossProfit"]
OPERATING_INCOME_TAGS = ["us-gaap:OperatingIncomeLoss"]

# Duration windows (days) that identify a period's length. 52/53-week fiscal
# calendars (NVDA, AAPL) make these drift by about a week.
QUARTER_DAYS = range(80, 101)
NINE_MONTH_DAYS = range(260, 285)
ANNUAL_DAYS = range(350, 380)


def _duration_facts(facts: pd.DataFrame, tags: list[str]) -> dict[tuple[date, date], float]:
    """Return {(period_start, period_end): value} for USD duration facts, merged across tags.

    The tag the company uses in its most recent filings wins when two tags report
    the same period; older tags only fill in history (ties go to list order).
    Within a tag, the same period appears once per filing that reports it (current
    + comparatives); the last occurrence is kept, which reflects the most recent
    (possibly restated) value.
    """
    usable = facts[
        facts["concept"].isin(tags)
        & (facts["unit"] == "USD")
        & (facts["period_type"] == "duration")
        & facts["period_start"].notna()
        & facts["numeric_value"].notna()
    ]
    latest = usable.groupby("concept")["period_end"].max()
    ordered = sorted(latest.index, key=lambda t: (-pd.Timestamp(latest[t]).value, tags.index(t)))

    merged: dict[tuple[date, date], float] = {}
    for tag in ordered:
        rows = usable[usable["concept"] == tag]
        per_tag = {
            (pd.Timestamp(s).date(), pd.Timestamp(e).date()): float(v)
            for s, e, v in zip(rows["period_start"], rows["period_end"], rows["numeric_value"])
        }
        for key, value in per_tag.items():
            merged.setdefault(key, value)
    return merged


def _quarterly_series(periods: dict[tuple[date, date], float]) -> dict[date, tuple[date, float, bool]]:
    """Build {quarter_end: (quarter_start, value, derived)} from mixed-length periods.

    Discrete quarters (Q1-Q3, from 10-Qs) are used as reported. Q4 is almost never
    filed on its own, so it is derived from the 10-K:
        Q4 = full fiscal year - nine-month year-to-date (from the Q3 10-Q)
    If the nine-month YTD fact is unavailable, falls back to
        Q4 = full fiscal year - (Q1 + Q2 + Q3)
    """
    quarters: dict[date, tuple[date, float, bool]] = {}
    for (start, end), value in periods.items():
        if (end - start).days in QUARTER_DAYS:
            quarters[end] = (start, value, False)

    for (fy_start, fy_end), fy_value in periods.items():
        if (fy_end - fy_start).days not in ANNUAL_DAYS or fy_end in quarters:
            continue
        # Preferred: FY minus the nine-month YTD that starts on the same day
        nine_month = [
            (end, value)
            for (start, end), value in periods.items()
            if start == fy_start and (end - start).days in NINE_MONTH_DAYS
        ]
        if nine_month:
            q3_end, ytd = max(nine_month)
            quarters[fy_end] = (q3_end + timedelta(days=1), fy_value - ytd, True)
            continue
        # Fallback: FY minus the three discrete quarters inside it
        inside = sorted(
            (end, q) for end, q in quarters.items() if fy_start < end < fy_end and not q[2]
        )
        if len(inside) == 3:
            q3_end = inside[-1][0]
            quarters[fy_end] = (q3_end + timedelta(days=1), fy_value - sum(q[1] for _, q in inside), True)
    return quarters


def _fiscal_label(quarter_end: date, annual_periods: list[tuple[date, date]]) -> str:
    """Label a quarter like 'FY2026 Q3' using the company's own fiscal year.

    The fiscal year is named for the calendar year it ends in (NVDA's FY2026 ends
    Jan 2026; AAPL's FY2025 ends Sep 2025). For recent quarters whose 10-K isn't
    filed yet, the latest fiscal year is projected forward one year at a time.
    """
    if not annual_periods:
        return "?"
    for fy_start, fy_end in annual_periods:
        if fy_start <= quarter_end <= fy_end + timedelta(days=7):
            break
    else:
        fy_start, fy_end = max(annual_periods, key=lambda p: p[1])
        while quarter_end > fy_end + timedelta(days=7):
            fy_start, fy_end = fy_end + timedelta(days=1), fy_end + timedelta(days=364)
    q_num = min(4, max(1, round((quarter_end - fy_start).days / 91.3)))
    return f"FY{fy_end.year} Q{q_num}"


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value / 1e6:,.0f}"


def _fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1%}"


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    return numerator / denominator if numerator is not None and denominator else None


def _load_facts(ticker: str) -> tuple[Company, pd.DataFrame]:
    """Fetch a company's XBRL facts as a DataFrame. Raises ValueError with a readable message."""
    try:
        company = Company(ticker)
        facts = company.get_facts()
    except Exception as e:
        raise ValueError(f"Could not load SEC data for '{ticker}': {e}") from e
    if facts is None:
        raise ValueError(f"No XBRL financial data found for '{ticker}'.")
    return company, facts.to_dataframe()


def _render_table(headers: list[str], rows: list[list[str]], left_cols: int = 1) -> list[str]:
    """Render a fixed-width text table; the first `left_cols` columns are left-aligned."""
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "  ".join(
            c.ljust(w) if i < left_cols else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths))
        ).rstrip()

    return [line(headers), "  ".join("-" * w for w in widths), *(line(r) for r in rows)]


@mcp.tool()
def quarterly_kpi_trend(ticker: str, quarters: int = 8) -> str:
    """Show a company's quarterly revenue, profitability, and growth trend from SEC XBRL filings.

    Returns a text table, one row per fiscal quarter (most recent first), with:
    fiscal quarter label, period end date, revenue, gross profit, operating income
    (all in USD millions), gross margin, operating margin, and year-over-year
    revenue growth versus the same quarter one year earlier.

    Use this when an analyst asks how a company's top line or margins are trending,
    whether growth is accelerating or decelerating, how the latest quarter compares
    with a year ago, or wants a quick quarterly snapshot before an earnings call.
    Data comes straight from 10-Q and 10-K filings, so it reflects reported GAAP
    figures (not adjusted / non-GAAP numbers).

    Notes on the data:
    - Q4 is usually not filed separately; it is derived as full-year (10-K) minus
      nine-month year-to-date (Q3 10-Q). Derived rows are marked with '*'.
    - Metrics a company doesn't report (e.g. banks have no gross profit) show "N/A".
    - Fiscal quarters follow the company's own fiscal calendar, which may not
      match calendar quarters (e.g. NVDA's fiscal year ends in late January).

    Args:
        ticker: Stock ticker symbol, e.g. "NVDA" or "AAPL".
        quarters: Number of most recent quarters to return (default 8, max 40).
    """
    ticker = ticker.strip().upper()
    quarters = max(1, min(int(quarters), 40))

    try:
        company, df = _load_facts(ticker)
    except ValueError as e:
        return str(e)

    revenue_periods = _duration_facts(df, REVENUE_TAGS)
    revenue = _quarterly_series(revenue_periods)
    gross_profit = _quarterly_series(_duration_facts(df, GROSS_PROFIT_TAGS))
    operating_income = _quarterly_series(_duration_facts(df, OPERATING_INCOME_TAGS))
    if not revenue:
        return f"No quarterly revenue data found in XBRL filings for '{ticker}'."

    annual_periods = [(s, e) for s, e in revenue_periods if (e - s).days in ANNUAL_DAYS]

    def lookup(series: dict, end: date) -> float | None:
        entry = series.get(end)
        return None if entry is None else entry[1]

    def year_ago_revenue(end: date) -> float | None:
        # 52/53-week calendars put the prior-year quarter 357-371 days earlier
        for prior_end, (_, value, _) in revenue.items():
            if 357 <= (end - prior_end).days <= 371:
                return value
        return None

    rows = []
    any_derived = False
    for end in sorted(revenue, reverse=True)[:quarters]:
        _, rev, derived = revenue[end]
        gp = lookup(gross_profit, end)
        oi = lookup(operating_income, end)
        prior = year_ago_revenue(end)
        any_derived |= derived
        rows.append([
            _fiscal_label(end, annual_periods) + ("*" if derived else ""),
            end.isoformat(),
            _fmt_money(rev),
            _fmt_money(gp),
            _fmt_money(oi),
            _fmt_pct(_ratio(gp, rev)),
            _fmt_pct(_ratio(oi, rev)),
            _fmt_pct(rev / prior - 1 if prior else None),
        ])

    headers = ["Quarter", "Period End", "Revenue", "Gross Profit", "Op Income",
               "Gross Mgn", "Op Mgn", "Rev YoY"]
    out = [
        f"{company.name} ({ticker}) — quarterly KPI trend (USD millions)",
        "",
        *_render_table(headers, rows, left_cols=2),
    ]
    if any_derived:
        out += ["", "* Q4 derived: full-year (10-K) minus nine-month YTD (Q3 10-Q)."]
    return "\n".join(out)


def _latest_fiscal_year(ticker: str) -> dict:
    """Most recent full fiscal year (from 10-K data) for one company, as raw values."""
    company, df = _load_facts(ticker)
    annual = lambda tags: {  # noqa: E731
        k: v for k, v in _duration_facts(df, tags).items() if (k[1] - k[0]).days in ANNUAL_DAYS
    }
    revenue = annual(REVENUE_TAGS)
    if not revenue:
        raise ValueError(f"No annual revenue found in XBRL filings for '{ticker}'.")
    gross_profit = annual(GROSS_PROFIT_TAGS)
    operating_income = annual(OPERATING_INCOME_TAGS)

    (fy_start, fy_end), rev = max(revenue.items(), key=lambda kv: kv[0][1])
    prior_rev = next(
        (v for (s, e), v in revenue.items() if 357 <= (fy_end - e).days <= 371), None
    )
    gp = gross_profit.get((fy_start, fy_end))
    oi = operating_income.get((fy_start, fy_end))
    return {
        "name": company.name,
        "fiscal_year": f"FY{fy_end.year}",
        "fy_end": fy_end,
        "revenue": rev,
        "growth": rev / prior_rev - 1 if prior_rev else None,
        "gross_margin": _ratio(gp, rev),
        "operating_margin": _ratio(oi, rev),
    }


@mcp.tool()
def peer_snapshot(tickers: list[str]) -> str:
    """Compare several companies side by side on their most recent full fiscal year.

    Returns a text table with one column per company and these rows: company name,
    fiscal year, fiscal year end date, revenue (USD millions), revenue growth vs. the
    prior fiscal year, gross margin, and operating margin. Figures are reported GAAP
    numbers from each company's latest 10-K (XBRL data).

    Use this when an analyst wants to benchmark a company against its competitors,
    asks "how does X compare to Y and Z", is building a comps table, or wants to
    see who is growing fastest or has the best margins in a peer group. For a
    single company's quarter-by-quarter trend, use quarterly_kpi_trend instead.

    Notes on the data:
    - Each company is shown on its OWN latest fiscal year, and fiscal calendars
      differ (e.g. NVIDIA's year ends in late January, Apple's in late September),
      so the periods may not line up exactly. Check the "FY End" row before
      drawing conclusions about the same time window.
    - Metrics a company doesn't report (e.g. banks have no gross profit) show "N/A".
    - If a ticker can't be found or has no data, the others are still returned and
      the problem is listed under "Errors".

    Args:
        tickers: List of stock ticker symbols, e.g. ["NVDA", "AMD", "INTC"]. Max 10.
    """
    tickers = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))[:10]
    if not tickers:
        return "No tickers provided."

    results: dict[str, dict] = {}
    errors: list[str] = []
    for ticker in tickers:
        try:
            results[ticker] = _latest_fiscal_year(ticker)
        except Exception as e:
            errors.append(f"- {ticker}: {e}")

    out = ["Peer snapshot — most recent fiscal year from 10-K filings (USD millions)", ""]
    if results:
        cols = list(results.values())
        metrics = [
            ("Company", lambda r: r["name"]),
            ("Fiscal Year", lambda r: r["fiscal_year"]),
            ("FY End", lambda r: r["fy_end"].isoformat()),
            ("Revenue", lambda r: _fmt_money(r["revenue"])),
            ("Rev Growth YoY", lambda r: _fmt_pct(r["growth"])),
            ("Gross Margin", lambda r: _fmt_pct(r["gross_margin"])),
            ("Op Margin", lambda r: _fmt_pct(r["operating_margin"])),
        ]
        rows = [[label, *(fmt(r) for r in cols)] for label, fmt in metrics]
        out += _render_table(["", *results], rows)
    else:
        out.append("No results.")
    if errors:
        out += ["", "Errors:", *errors]
    return "\n".join(out)


# --- filing_language_changes helpers -------------------------------------------

# Section keys tried in order; edgartools labels 10-Q items by part
MDA_SECTIONS = {"10-Q": ["Part I, Item 2", "Item 2"], "10-K": ["Item 7"]}
MIN_PARAGRAPH_WORDS = 8  # shorter blocks are headings, captions, page furniture
MIN_CHANGED_WORDS = 3  # edits smaller than this (after ignoring numbers) are noise
MATCH_RATIO = 0.5  # word-level similarity above which two paragraphs are "the same, edited"
MOVED_MATCH_RATIO = 0.75  # stricter bar for pairing paragraphs that also moved position
MAX_ITEM_CHARS = 550

_MONTHS = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
_ORDINALS = r"first|second|third|fourth"
_NUMBER = r"[$€£]?\(?\d[\d,]*(?:\.\d+)?\)?%?"


def _normalize_word(word: str) -> str:
    """Canonical form of a word for comparison: numbers, months, and quarter ordinals
    collapse to placeholders so 'Q2 FY2027 revenue of $96.2 billion' matches last year's
    'Q2 FY2026 revenue of $46.7 billion'."""
    w = word.lower().strip(".,;:()\"'“”‘’")
    w = w.replace("’", "'")
    if re.fullmatch(_NUMBER, w) or re.fullmatch(r"(?:fy|q)\d+", w):
        return "#"
    if re.fullmatch(_MONTHS, w):
        return "<month>"
    if re.fullmatch(_ORDINALS, w):
        return "<ord>"
    return w


def _is_table(block: str) -> bool:
    lines = [l for l in block.splitlines() if l.strip()]
    columnar = sum(1 for l in lines if "  " in l.strip())
    return len(lines) >= 2 and columnar / len(lines) >= 0.5


def _mda_paragraphs(text: str) -> list[str]:
    """Split MD&A text into prose paragraphs, dropping tables and page furniture and
    re-joining paragraphs that a page break split mid-sentence."""
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block or _is_table(block):
            continue
        if re.fullmatch(r"\d{1,3}|table of contents", block, re.IGNORECASE):
            continue  # page numbers / running headers
        block = re.sub(r"\s+", " ", block)
        if paragraphs and block[0].islower() and not paragraphs[-1].endswith((".", ":", ";", "?", "!")):
            paragraphs[-1] += " " + block
        else:
            paragraphs.append(block)
    return [p for p in paragraphs if len(p.split()) >= MIN_PARAGRAPH_WORDS]


def _comparable_filings(company: Company, form: str):
    """Latest original filing of `form` and the one covering the period one year earlier."""
    filings = company.get_filings(form=form, amendments=False)
    if filings is None or len(filings) == 0:
        raise ValueError(f"No {form} filings found for {company.name}.")
    latest = filings[0]
    latest_period = date.fromisoformat(str(latest.period_of_report))
    for filing in filings[1:12]:
        period = date.fromisoformat(str(filing.period_of_report))
        if 350 <= (latest_period - period).days <= 380:
            return latest, filing
    raise ValueError(f"Could not find the {form} covering the period one year before {latest_period}.")


def _extract_mda(filing, form: str) -> str:
    doc = filing.obj()
    for key in MDA_SECTIONS[form]:
        try:
            text = doc[key]
        except Exception:
            continue
        if text and len(text) > 500:
            return text
    raise ValueError(f"Could not extract the MD&A section from the {form} filed {filing.filing_date}.")


def _excerpt(words: list[str], lo: int, hi: int, context: int = 15) -> str:
    """Words[lo:hi] with some surrounding context, ellipsized and length-capped."""
    start, end = max(0, lo - context), min(len(words), hi + context)
    text = " ".join(words[start:end])
    if len(text) > MAX_ITEM_CHARS:
        text = text[:MAX_ITEM_CHARS].rsplit(" ", 1)[0] + " …"
    return ("… " if start > 0 else "") + text + (" …" if end < len(words) and not text.endswith("…") else "")


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_ITEM_CHARS else text[:MAX_ITEM_CHARS].rsplit(" ", 1)[0] + " …"


def _diff_paragraphs(old: list[str], new: list[str]) -> list[dict]:
    """Classify paragraph-level differences as added / removed / changed, scored by
    how many non-numeric words differ. Paragraphs that are identical once numbers
    and dates are ignored are dropped as trivial."""
    old_tok = [[_normalize_word(w) for w in p.split()] for p in old]
    new_tok = [[_normalize_word(w) for w in p.split()] for p in new]

    # 1. Drop paragraphs whose normalized text appears on both sides (boilerplate,
    #    or the same sentence with only the numbers updated).
    old_keys = [" ".join(t) for t in old_tok]
    new_keys = [" ".join(t) for t in new_tok]
    old_set, new_set = set(old_keys), set(new_keys)

    def similarity(i: int, j: int, threshold: float) -> float | None:
        sm = difflib.SequenceMatcher(None, old_tok[i], new_tok[j], autojunk=False)
        if sm.real_quick_ratio() >= threshold and sm.quick_ratio() >= threshold:
            ratio = sm.ratio()
            if ratio >= threshold:
                return ratio
        return None

    # 2. Align the two paragraph sequences in document order. Edited paragraphs
    #    are paired only within the same unmatched block, so a templated sentence
    #    in one section ("iPad net sales decreased ...") can't be mistaken for its
    #    look-alike in another section ("Greater China net sales decreased ...").
    candidates = []
    aligner = difflib.SequenceMatcher(None, old_keys, new_keys, autojunk=False)
    for op, i1, i2, j1, j2 in aligner.get_opcodes():
        if op != "equal":
            olds = [i for i in range(i1, i2) if old_keys[i] not in new_set]
            news = [j for j in range(j1, j2) if new_keys[j] not in old_set]
            candidates += [(r, i, j) for i in olds for j in news if (r := similarity(i, j, MATCH_RATIO))]
    paired_old, paired_new = set(), set()
    pairs = []

    def take_pairs(cands):
        for ratio, i, j in sorted(cands, reverse=True):
            if i not in paired_old and j not in paired_new:
                paired_old.add(i)
                paired_new.add(j)
                pairs.append((ratio, i, j))

    take_pairs(candidates)

    # 3. Paragraphs that moved to a different part of the MD&A *and* were edited:
    #    pair across blocks, but only when they're clearly the same passage.
    old_left = [i for i in range(len(old)) if old_keys[i] not in new_set and i not in paired_old]
    new_left = [j for j in range(len(new)) if new_keys[j] not in old_set and j not in paired_new]
    take_pairs([(r, i, j) for i in old_left for j in new_left if (r := similarity(i, j, MOVED_MATCH_RATIO))])

    items = []
    for ratio, i, j in pairs:
        sm = difflib.SequenceMatcher(None, old_tok[i], new_tok[j], autojunk=False)
        edits = [op for op in sm.get_opcodes() if op[0] != "equal"]
        changed_words = sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in edits)
        if changed_words < MIN_CHANGED_WORDS:
            continue
        old_words, new_words = old[i].split(), new[j].split()
        # Show the span from the first to the last edit, plus context
        i_lo, i_hi = edits[0][1], edits[-1][2]
        j_lo, j_hi = edits[0][3], edits[-1][4]
        items.append({
            "kind": "CHANGED",
            "score": changed_words,
            "similarity": ratio,
            "old": _excerpt(old_words, i_lo, i_hi),
            "new": _excerpt(new_words, j_lo, j_hi),
        })

    # 4. Whatever is left unpaired is genuinely new or removed.
    for j in new_left:
        if j not in paired_new:
            items.append({"kind": "NEW", "score": len(new_tok[j]), "text": _truncate(new[j])})
    for i in old_left:
        if i not in paired_old:
            items.append({"kind": "REMOVED", "score": len(old_tok[i]), "text": _truncate(old[i])})
    return items


@mcp.tool()
def filing_language_changes(ticker: str, form: str = "10-Q", max_changes: int = 15) -> str:
    """Compare the MD&A wording of a company's latest 10-Q or 10-K against the same
    filing one year earlier, to surface shifts in management's tone, emphasis, or
    disclosures.

    Takes the most recent filing of the given form and the comparable filing from a
    year before (for a 10-Q, the same fiscal quarter last year, which avoids
    seasonal differences; for a 10-K, last year's 10-K). Extracts Management's
    Discussion & Analysis from both (Part I Item 2 of a 10-Q, Item 7 of a 10-K),
    compares them paragraph by paragraph, and returns the most substantive:
      - NEW paragraphs: topics management now discusses that it didn't before
        (new risks, products, customers, regulations, accounting changes)
      - REMOVED paragraphs: topics management stopped mentioning
      - CHANGED paragraphs: the same passage reworded, showing old vs. new text
        (e.g. "strong demand" -> "moderating demand", added caveats or qualifiers)

    Changes that only update numbers, dates, or quarter/year references are ignored,
    so "revenue was $X, up Y%" with new figures doesn't count as a change. Tables are
    skipped; use quarterly_kpi_trend for the numbers. Results are ranked by how many
    words changed and capped to keep the output short.

    Use this when an analyst asks what's new or different in a company's latest
    filing, wants to detect changes in management tone or guidance language,
    is preparing for an earnings call, or is looking for newly disclosed risks,
    supply constraints, customer concentration, regulatory or legal developments.

    Args:
        ticker: Stock ticker symbol, e.g. "NVDA".
        form: "10-Q" (quarterly, default) or "10-K" (annual).
        max_changes: Maximum number of changes to return (default 15, max 40).
    """
    ticker = ticker.strip().upper()
    form = form.strip().upper().replace("10Q", "10-Q").replace("10K", "10-K")
    if form not in MDA_SECTIONS:
        return f"Unsupported form '{form}'. Use '10-Q' or '10-K'."
    max_changes = max(1, min(int(max_changes), 40))

    try:
        company = Company(ticker)
        latest, prior = _comparable_filings(company, form)
        new_paras = _mda_paragraphs(_extract_mda(latest, form))
        old_paras = _mda_paragraphs(_extract_mda(prior, form))
    except Exception as e:
        return f"Could not compare {form} filings for '{ticker}': {e}"

    items = _diff_paragraphs(old_paras, new_paras)
    kinds = ("NEW", "REMOVED", "CHANGED")
    by_kind = {k: sorted((it for it in items if it["kind"] == k), key=lambda it: -it["score"]) for k in kinds}
    counts = {k: len(v) for k, v in by_kind.items()}

    # Share the output budget across categories (round-robin by score) so long new
    # paragraphs can't crowd out subtle rewording, which is often the real signal.
    selected = {k: [] for k in kinds}
    queues = {k: list(v) for k, v in by_kind.items()}
    while sum(map(len, selected.values())) < max_changes and any(queues.values()):
        for k in kinds:
            if queues[k] and sum(map(len, selected.values())) < max_changes:
                selected[k].append(queues[k].pop(0))

    out = [
        f"{company.name} ({ticker}) — MD&A language changes, {form}",
        "",
        f"Current:  {form} for period ended {latest.period_of_report}, filed {latest.filing_date}",
        f"          {latest.homepage_url}",
        f"Prior:    {form} for period ended {prior.period_of_report}, filed {prior.filing_date}",
        f"          {prior.homepage_url}",
        "",
        f"MD&A paragraphs: {len(old_paras)} prior -> {len(new_paras)} current. "
        f"Substantive differences: {counts['NEW']} new, {counts['REMOVED']} removed, "
        f"{counts['CHANGED']} changed (number/date-only edits ignored).",
        f"Showing the {sum(map(len, selected.values()))} most substantive (ranked by words changed).",
    ]
    titles = {
        "NEW": "(a) NEW paragraphs — in the current filing, not in the prior one",
        "REMOVED": "(b) REMOVED paragraphs — in the prior filing, dropped from the current one",
        "CHANGED": "(c) CHANGED paragraphs — same passage, reworded (excerpt around the edits)",
    }
    for kind in kinds:
        if not selected[kind]:
            continue
        out += ["", "=" * 80, f"{titles[kind]}  [showing {len(selected[kind])} of {counts[kind]}]", "=" * 80]
        for n, it in enumerate(selected[kind], 1):
            out.append("")
            if kind == "CHANGED":
                out += [
                    f"{n}. ({it['score']} words differ, {it['similarity']:.0%} similar)",
                    f"   OLD: {it['old']}",
                    f"   NEW: {it['new']}",
                ]
            else:
                out += [f"{n}. ({it['score']} words) {it['text']}"]
    if not items:
        out += ["", "No substantive language changes found."]
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
