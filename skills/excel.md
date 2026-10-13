---
name: excel
description: make or edit Excel workbooks (.xlsx) and CSVs with openpyxl and pandas
triggers: excel|xlsx|xls\b|spreadsheet|workbook|\bsheet\b|\bcsv\b|pivot
---
Excel recipe (openpyxl and pandas are installed). One run_python call that writes the workbook and then reads it back:
```python
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import BarChart, LineChart, Reference

out = "sales.xlsx"
rows = [("Month", "Units", "Price", "Revenue"), ("Jan", 120, 9.5), ("Feb", 150, 9.5)]
wb = Workbook(); ws = wb.active; ws.title = "Sales"
ws.append(rows[0])
for i, (m, u, p) in enumerate(rows[1:], start=2):
    ws.append([m, u, p, f"=B{i}*C{i}"])                      # real formulas so the sheet stays live
last = ws.max_row
ws.append(["Total", f"=SUM(B2:B{last})", None, f"=SUM(D2:D{last})"])
head = PatternFill("solid", fgColor="1F4E79")
for c in ws[1]:
    c.font, c.fill, c.alignment = Font(bold=True, color="FFFFFF"), head, Alignment(horizontal="center")
for c in ws[ws.max_row]:
    c.font = Font(bold=True)
for col in ("C", "D"):
    for c in ws[col][1:]:
        c.number_format = "#,##0.00"
for i, col in enumerate(ws.columns, start=1):
    ws.column_dimensions[get_column_letter(i)].width = max(len(str(c.value or "")) for c in col) + 4
ws.freeze_panes = "A2"; ws.auto_filter.ref = f"A1:D{last}"
chart = BarChart(); chart.title = "Revenue by month"
chart.add_data(Reference(ws, min_col=4, min_row=1, max_row=last), titles_from_data=True)
chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=last))
ws.add_chart(chart, "F2")
wb.save(out)

wb2 = load_workbook(out)
for s in wb2.worksheets:
    print("saved", out, "| sheet", s.title, s.dimensions, [c.value for c in s[1]])
```
- Formulas are only calculated when Excel opens the file. If the user needs the numbers now, compute them in Python too and print them.
- A table of data: `df.to_excel(out, index=False, sheet_name="Data")` with pandas, then style it with openpyxl as above.
- Several sheets: `wb.create_sheet("Name")`; with pandas use `pd.ExcelWriter(out, engine="openpyxl")`.
- Read: `pd.read_excel(path, sheet_name=None)` gives every sheet; print `df.head()` and `df.shape`, never the whole file.
- Editing an existing file: `load_workbook(path)`, change cells, save under the same name only if the user asked.
- CSV: `df.to_csv(out, index=False)`; read with `pd.read_csv`.
