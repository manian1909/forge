---
name: excel_sales
description: Excel sales analysis with sample data, SUMIFS pivot summaries, charts and a dashboard
replaces: excel,charts
triggers: sales|revenue|profit|pivot|products?.*regions?|monthly trends?
---
For a workbook with data + summaries + charts use the helper `forge_office` (already installed). Keep the script SHORT: write it in one run_python call.
Never type sample rows by hand: generate them with numpy (seed 42) and pandas. Every total is a formula; `{Name}` in a formula means that column in the same row.
```python
import numpy as np, pandas as pd
from forge_office import xl
rng = np.random.default_rng(42)
products, regions, n = ["Laptop", "Phone", "Tablet", "Watch", "Earbuds"], ["North", "South", "East", "West"], 200
df = pd.DataFrame({"Date": pd.to_datetime("2026-01-01") + pd.to_timedelta(rng.integers(0, 181, n), unit="D"),
    "Product": rng.choice(products, n), "Region": rng.choice(regions, n),
    "Units": rng.integers(1, 20, n), "UnitPrice": rng.integers(50, 900, n)})
df["UnitCost"] = (df.UnitPrice * rng.uniform(0.55, 0.85, n)).round(2)
df = df.sort_values("Date").reset_index(drop=True)
df["Month"] = df.Date.dt.strftime("%Y-%m")
wb = xl.workbook()
data = xl.write_table(wb, "Data", df, formulas={"Revenue": "={Units}*{UnitPrice}", "Cost": "={Units}*{UnitCost}",
    "Profit": "={Revenue}-{Cost}", "Margin": "=IF({Revenue}=0,0,{Profit}/{Revenue})"},
    formats={"Date": "yyyy-mm-dd", "Revenue": "#,##0", "Cost": "#,##0", "Profit": "#,##0", "Margin": "0.0%"})
by_p = xl.summary(wb, "Summary", "By Product", data, "Product", products, ["Revenue", "Cost", "Profit"], ratio=("Margin", "Profit", "Revenue"))
by_r = xl.summary(wb, "Summary", "By Region", data, "Region", regions, ["Revenue", "Cost", "Profit"], ratio=("Margin", "Profit", "Revenue"))
by_m = xl.summary(wb, "Summary", "By Month", data, "Month", sorted(df.Month.unique()), ["Revenue", "Cost", "Profit"], ratio=("Margin", "Profit", "Revenue"))
xl.bars(by_p, "Profit")
dash = xl.dashboard(wb, title="Sales dashboard")
xl.kpi(dash, 3, "Total revenue", xl.total(data, "Revenue"), "#,##0")
xl.kpi(dash, 4, "Total profit", xl.total(data, "Profit"), "#,##0")
xl.kpi(dash, 5, "Overall margin", "=B4/B3", "0.0%")
xl.kpi(dash, 6, "Best product (profit)", xl.best(by_p, "Profit"))
xl.kpi(dash, 7, "Best region (profit)", xl.best(by_r, "Profit"))
xl.chart(dash, "bar", "Profit by product", by_p, "Profit", "D2")
xl.chart(dash, "pie", "Profit by region", by_r, "Profit", "D18")
xl.chart(dash, "line", "Profit by month", by_m, "Profit", "L2")
xl.save(wb, "sales_analysis.xlsx")
# check: the answer must name what the DATA says, so compute it again in pandas
rev = df.Units * df.UnitPrice; prof = rev - df.Units * df.UnitCost
print("best product:", prof.groupby(df.Product).sum().idxmax(), "| best region:", prof.groupby(df.Region).sum().idxmax(), "| margin", f"{prof.sum() / rev.sum():.1%}")
```
A real PivotTable cannot be made from Python: say the pivot is a SUMIFS summary. In your reply: name the best product/region printed by the check, never guess them.
Other helpers: `xl.count(data, "Product")`, `xl.bars(block, col)`, `xl.flag(block, cond, "green"|"yellow")`.
