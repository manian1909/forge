---
name: excel_budget
description: Excel expense or budget tracker with sample transactions, budget vs actual formulas, overspend highlighting and charts
replaces: excel,charts
triggers: budget|expenses?\b|spending|expense tracker|personal finance
---
Budget tracker with the helper `forge_office` (already installed). Write it in ONE short run_python call. Generate the sample transactions with numpy (seed 42), never type them by hand; totals are formulas.
```python
import numpy as np, pandas as pd
from forge_office import xl
rng = np.random.default_rng(42)
budget = {"Rent": 6000, "Mess/Food": 3500, "Transport": 1200, "Books": 1500, "Mobile & Data": 600, "Entertainment": 1000, "Shopping": 1500}
spend = {"Rent": (6000, 6000), "Mess/Food": (150, 450), "Transport": (30, 250), "Books": (150, 600), "Mobile & Data": (299, 299), "Entertainment": (150, 600), "Shopping": (300, 1100)}
cats = ["Rent", "Mobile & Data"] + list(rng.choice(["Mess/Food", "Transport", "Books", "Entertainment", "Shopping"], 28, p=[.42, .2, .1, .14, .14]))
df = pd.DataFrame({"Date": pd.to_datetime("2026-09-01") + pd.to_timedelta(rng.integers(0, 30, 30), unit="D"), "Category": cats})
df["Description"] = df.Category + " payment"
df["Method"] = rng.choice(["UPI", "Debit card", "Cash", "Net banking"], 30)
df["Amount"] = [int(rng.integers(spend[c][0], spend[c][1] + 1)) for c in df.Category]
df = df.sort_values("Date").reset_index(drop=True)[["Date", "Description", "Category", "Method", "Amount"]]
wb = xl.workbook()
data = xl.write_table(wb, "Transactions", df, formats={"Date": "dd-mmm-yyyy", "Amount": "#,##0"})
b = xl.summary(wb, "Budget", "Monthly budget vs actual", data, "Category", list(budget), ["Amount"])
xl.add_values(b, "Budget", list(budget.values()))
xl.add_formula(b, "Remaining", "={Budget}-{Amount}")
xl.add_formula(b, "Used", "=IF({Budget}=0,0,{Amount}/{Budget})", "0%")
xl.flag(b, "{Remaining}<0")
dash = xl.dashboard(wb, title="Expense dashboard")
xl.kpi(dash, 3, "Total spent", xl.total(data, "Amount"), "#,##0")
xl.kpi(dash, 4, "Total budget", xl.total(b, "Budget"), "#,##0")
xl.kpi(dash, 5, "Remaining", "=B4-B3", "#,##0")
xl.kpi(dash, 6, "Biggest spend", xl.best(b, "Amount"))
xl.chart(dash, "bar", "Spend by category", b, "Amount", "D2")
xl.chart(dash, "pie", "Share of spending", b, "Amount", "D18")
xl.chart(dash, "bar", "Budget", b, "Budget", "L2")
xl.save(wb, "expense_tracker.xlsx")
over = df.groupby("Category").Amount.sum().reindex(list(budget)).fillna(0) - pd.Series(budget)
print("over budget:", list(over[over > 0].index), "| total spent", int(df.Amount.sum()))
```
Overspent categories are highlighted red by `xl.flag`. If the printed "over budget" list is empty, lower two budgets in the dict and rerun. In your reply name the categories printed by the check.
Payment-method totals (optional): `xl.summary(wb, "Methods", "By payment method", data, "Method", ["UPI", "Debit card", "Cash", "Net banking"], ["Amount"])`.
