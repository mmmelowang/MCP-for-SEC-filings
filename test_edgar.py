import os

from edgar import Company, set_identity

# SEC requires every requester to identify themselves with an email (set EDGAR_IDENTITY)
set_identity(os.environ["EDGAR_IDENTITY"])

company = Company("NVDA")
print(f"Company: {company.name} (CIK {company.cik})\n")

# 3 most recent quarterly reports
print("3 most recent 10-Q filings:")
for filing in company.get_filings(form="10-Q").latest(3):
    print(f"  {filing.filing_date}  period ending {filing.period_of_report}  {filing.accession_no}")

# Latest annual revenue, from the most recent 10-K
financials = company.get_financials()
revenue = financials.get_revenue()
print(f"\nLatest annual revenue: ${revenue:,.0f}")
