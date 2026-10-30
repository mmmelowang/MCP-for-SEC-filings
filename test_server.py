"""Quick manual test: call the tools directly (no MCP client needed)."""

from server import filing_language_changes, peer_snapshot, quarterly_kpi_trend

SEPARATOR = "\n" + "=" * 90 + "\n"

for ticker in ["NVDA", "AAPL"]:
    print(quarterly_kpi_trend(ticker))
    print(SEPARATOR)

print(peer_snapshot(["NVDA", "AMD", "INTC"]))
print(SEPARATOR)

print(filing_language_changes("NVDA"))  # latest 10-Q vs. same quarter last year
print(SEPARATOR)
print(filing_language_changes("AAPL", form="10-K"))  # latest 10-K vs. prior 10-K
