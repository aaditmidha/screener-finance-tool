import re, json
from screener.scraper import client

html = client.fetch("https://www.screener.in/company/CGPOWER/consolidated/")
ids = set(re.findall(r"/api/company/(\d+)/", html)) | set(re.findall(r'data-company-id="(\d+)"', html))
print("company ids found:", ids)
cid = sorted(ids)[0]

for parent, section in [("Expenses", "profit-loss"), ("Other Assets", "balance-sheet"),
                        ("Other Liabilities", "balance-sheet"), ("Cash from Operating Activity", "cash-flow")]:
    url = f"https://www.screener.in/api/company/{cid}/schedules/?parent={parent}&section={section}&consolidated=true"
    try:
        raw = client.fetch(url.replace(" ", "%20"))
        data = json.loads(raw)
        print(f"\n--- {parent} ({section}): {len(data)} child rows")
        for label, values in list(data.items())[:12]:
            sample = list(values.items())[-2:] if isinstance(values, dict) else values
            print(f"  {label}: {sample}")
    except Exception as e:
        print(f"\n--- {parent}: FAILED {type(e).__name__}: {e}")
