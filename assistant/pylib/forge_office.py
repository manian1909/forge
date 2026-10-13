"""Short helpers so a small model can build a real Excel dashboard in about 30 lines instead of 150.

    from forge_office import xl
    wb = xl.workbook()
    data = xl.write_table(wb, "Data", df, formulas={"Revenue": "={Units}*{UnitPrice}"}, formats={"Revenue": "#,##0"})
    by_product = xl.summary(wb, "Summary", "By Product", data, key="Product", keys=products, sums=["Revenue", "Cost"])
    xl.add_formula(by_product, "Profit", "={Revenue}-{Cost}")
    dash = xl.dashboard(wb)
    xl.kpi(dash, 3, "Total revenue", xl.total(data, "Revenue"), "#,##0")
    xl.chart(dash, "bar", "Profit by product", by_product, "Profit", "D2")
    xl.save(wb, "sales.xlsx")

Every number stays a live Excel formula. ``{Name}`` inside a formula is replaced by that column's cell in the same row.
"""

import re
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.marker import DataPoint
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.drawing.line import LineProperties
from openpyxl.formatting.rule import DataBarRule, FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

HEAD_FILL = PatternFill("solid", fgColor="1F4E79")
COLORS = {"red": ("FFC7CE", "9C0006"), "green": ("C6EFCE", "006100"), "yellow": ("FFEB9C", "9C5700")}


class Sheet:
    """A written table: where it is and which column letter each header has."""

    def __init__(self, ws, header_row, first, last, cols):
        self.ws, self.header_row, self.first, self.last, self.cols = ws, header_row, first, last, cols

    def col(self, name):
        if name not in self.cols:
            raise KeyError(f"no column {name!r} in {self.ws.title}; columns are {list(self.cols)}")
        return self.cols[name]

    def rng(self, name, absolute=True):
        c = self.col(name)
        return f"'{self.ws.title}'!${c}${self.first}:${c}${self.last}" if absolute else f"{c}{self.first}:{c}{self.last}"


def workbook():
    wb = Workbook()
    wb.remove(wb.active)
    return wb


def _fill(template, cols, row):
    def sub(m):
        name = m.group(1)
        return str(row) if name == "r" else f"{cols[name]}{row}"
    return re.sub(r"\{(\w+)\}", sub, template)


def _style_header(ws, row, ncols, start=1):
    for c in range(start, start + ncols):
        cell = ws.cell(row, c)
        cell.font, cell.fill, cell.alignment = Font(bold=True, color="FFFFFF"), HEAD_FILL, Alignment(horizontal="center")


def autosize(ws, minimum=8, maximum=45):
    for i, col in enumerate(ws.columns, start=1):
        width = max((len(str(c.value)) for c in col if c.value is not None and not str(c.value).startswith("=")), default=minimum)
        ws.column_dimensions[get_column_letter(i)].width = max(minimum, min(maximum, width + 3))


def write_table(wb, name, df, formulas=None, formats=None, as_table=True):
    """Write a DataFrame to a new sheet. ``formulas`` adds computed columns: {"Profit": "={Revenue}-{Cost}"}.
    ``formats`` sets number formats by column name: {"Margin": "0.0%", "Date": "yyyy-mm-dd"}."""
    ws = wb.create_sheet(name)
    headers = list(df.columns) + list(formulas or {})
    cols = {h: get_column_letter(i) for i, h in enumerate(headers, start=1)}
    ws.append(headers)
    for r, row in enumerate(df.itertuples(index=False), start=2):
        values = [v.to_pydatetime() if hasattr(v, "to_pydatetime") else (v.item() if hasattr(v, "item") else v) for v in row]
        values += [_fill(f, cols, r) for f in (formulas or {}).values()]
        ws.append(values)
    last = len(df) + 1
    _style_header(ws, 1, len(headers))
    for h, fmt in (formats or {}).items():
        for c in ws[cols[h]][1:]:
            c.number_format = fmt
    if as_table and len(df):
        t = Table(displayName=re.sub(r"\W", "_", name), ref=f"A1:{get_column_letter(len(headers))}{last}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(t)
    ws.freeze_panes = "A2"
    autosize(ws)
    return Sheet(ws, 1, 2, last, cols)


def summary(wb, sheet, title, data, key, keys, sums, ratio=None, top=None, fmt="#,##0"):
    """A pivot-style block: one row per key with SUMIFS over ``data`` for each column in ``sums``.
    ``ratio=("Margin", "Profit", "Revenue")`` adds a percentage column (needs those columns in ``sums``/added earlier)."""
    ws = wb[sheet] if sheet in wb.sheetnames else wb.create_sheet(sheet)
    top = top or (ws.max_row + 2 if ws.max_row > 1 else 1)
    ws.cell(top, 1, title).font = Font(bold=True, size=12)
    headers = [key] + list(sums)
    for j, h in enumerate(headers, start=1):
        ws.cell(top + 1, j, h)
    _style_header(ws, top + 1, len(headers))
    cols = {h: get_column_letter(j) for j, h in enumerate(headers, start=1)}
    first = top + 2
    for i, k in enumerate(keys, start=first):
        ws.cell(i, 1, k)
        for j, s in enumerate(sums, start=2):
            ws.cell(i, j, f"=SUMIFS({data.rng(s)},{data.rng(key)},$A{i})").number_format = fmt
    block = Sheet(ws, top + 1, first, first + len(keys) - 1, cols)
    if ratio:
        add_formula(block, ratio[0], f"=IF({{{ratio[2]}}}=0,0,{{{ratio[1]}}}/{{{ratio[2]}}})", "0.0%")
    autosize(ws)
    return block


def add_formula(block, name, template, fmt="#,##0"):
    """Add a formula column to a summary block: add_formula(b, "Remaining", "={Budget}-{Actual}")."""
    ws = block.ws
    col = len(block.cols) + 1
    block.cols[name] = get_column_letter(col)
    ws.cell(block.header_row, col, name)
    _style_header(ws, block.header_row, 1, start=col)
    for r in range(block.first, block.last + 1):
        ws.cell(r, col, _fill(template, block.cols, r)).number_format = fmt
    autosize(ws)


def add_values(block, name, values, fmt="#,##0"):
    """Add a column of typed-in inputs (for example the monthly budget of each category)."""
    ws = block.ws
    col = len(block.cols) + 1
    block.cols[name] = get_column_letter(col)
    ws.cell(block.header_row, col, name)
    _style_header(ws, block.header_row, 1, start=col)
    for r, v in zip(range(block.first, block.last + 1), values):
        ws.cell(r, col, v).number_format = fmt
    autosize(ws)


def flag(block, condition, color="red", whole_row=True):
    """Highlight rows of a block where a condition holds: flag(b, "{Remaining}<0")."""
    first = block.first
    cond = re.sub(r"\{(\w+)\}", lambda m: f"${block.cols[m.group(1)]}{first}", condition)
    fill, font = COLORS.get(color, COLORS["red"])
    last_col = get_column_letter(len(block.cols)) if whole_row else block.cols[re.search(r"\{(\w+)\}", condition).group(1)]
    first_col = "A" if whole_row else last_col
    block.ws.conditional_formatting.add(f"{first_col}{first}:{last_col}{block.last}", FormulaRule(
        formula=[cond.lstrip("=")], fill=PatternFill("solid", bgColor=fill), font=Font(color=font)))


def bars(block, column):
    """In-cell data bars on a column."""
    block.ws.conditional_formatting.add(block.rng(column, absolute=False), DataBarRule(start_type="min", end_type="max", color="63BE7B"))


def dashboard(wb, name="Dashboard", title=None):
    ws = wb.create_sheet(name, 0)
    ws.sheet_view.showGridLines = False
    for c in range(1, 26):
        ws.cell(1, c).fill = HEAD_FILL
    ws["A1"] = title or name
    ws["A1"].font = Font(bold=True, size=18, color="FFFFFF")
    ws.row_dimensions[1].height = 34
    ws["A1"].alignment = Alignment(vertical="center")
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 20
    return ws


def total(sheet, column):
    return f"=SUM({sheet.rng(column)})"


def count(sheet, column):
    return f"=COUNTA({sheet.rng(column)})"


def best(block, column, worst=False):
    """Formula giving the label of the row with the highest (or lowest) value in a column of a summary block."""
    fn = "MIN" if worst else "MAX"
    return (f"=INDEX({block.rng(block_key(block), True)},MATCH({fn}({block.rng(column)}),{block.rng(column)},0))")


def block_key(block):
    return next(iter(block.cols))


def kpi(ws, row, label, formula, fmt="General"):
    """A label and its value as a small card: grey caption, big bold number on a light panel."""
    lab, val = ws.cell(row, 1, label), ws.cell(row, 2, formula)
    lab.font, lab.fill = Font(color="595959", size=11), PatternFill("solid", fgColor="EAF1F8")
    val.font, val.fill = Font(bold=True, size=14, color="1F4E79"), PatternFill("solid", fgColor="EAF1F8")
    val.number_format, val.alignment = fmt, Alignment(horizontal="right")
    ws.row_dimensions[row].height = 22


PALETTE = ["1F77B4", "FF7F0E", "2CA02C", "D62728", "9467BD", "8C564B", "E377C2", "7F7F7F"]


def chart(ws, kind, title, block, column, anchor, width=16, height=8.5):
    """kind: bar, line, pie. Plots a column of a summary block against its labels, with visible axes, colours and labels.
    ``column`` can be a list, e.g. ["Budget", "Amount"], to draw the columns side by side (a bar chart of budget vs actual)."""
    ch = {"bar": BarChart, "line": LineChart, "pie": PieChart}[kind]()
    ch.title, ch.width, ch.height = title, width, height
    names = list(column) if isinstance(column, (list, tuple)) else [column]
    for name in names:
        block.col(name)
        idx = list(block.cols).index(name) + 1
        ch.add_data(Reference(block.ws, min_col=idx, min_row=block.header_row, max_row=block.last), titles_from_data=True)
    idx = list(block.cols).index(names[0]) + 1
    column = " / ".join(names)
    ch.set_categories(Reference(block.ws, min_col=1, min_row=block.first, max_row=block.last))
    ser = ch.series[0]
    shown = block.ws.cell(block.first, idx).number_format
    labels = DataLabelList()
    labels.showSerName = labels.showCatName = labels.showLegendKey = False
    if kind == "pie":
        labels.showVal, labels.showPercent = False, True
        for i in range(block.last - block.first + 1):
            pt = DataPoint(idx=i)
            pt.graphicalProperties.solidFill = PALETTE[i % len(PALETTE)]
            ser.dPt.append(pt)
        ch.legend.position = "r"
    else:
        labels.showVal, labels.showPercent = True, False
        labels.numFmt = shown if shown != "General" else "#,##0"
        ch.x_axis.delete = ch.y_axis.delete = False        # openpyxl leaves the axes hidden otherwise (newer Excel then shows none)
        ch.x_axis.title, ch.y_axis.title = block_key(block), column
        ch.y_axis.number_format = shown
        if len(names) == 1:
            ch.legend = None                                # one series: the title already says what it is
        else:
            ch.legend.position = "b"
        ch.y_axis.majorGridlines.spPr = GraphicalProperties(ln=LineProperties(solidFill="D9D9D9"))
        for i, one in enumerate(ch.series):
            color = PALETTE[i % len(PALETTE)]
            if kind == "bar":
                one.graphicalProperties.solidFill = color
            else:
                one.graphicalProperties.line.solidFill, one.graphicalProperties.line.width = color, 28000
                one.marker.symbol, one.marker.size, one.smooth = "circle", 7, False
                one.marker.graphicalProperties = GraphicalProperties(solidFill=color)
        if kind == "bar":
            ch.gapWidth = 60
        else:
            labels.position = "t"
    ch.dataLabels = labels
    ws.add_chart(ch, anchor)
    return column


def _bake(path):
    """Store each formula's result in the file, and the plotted numbers inside each chart.

    openpyxl writes formulas with no result. Excel recalculates on open, but previews, phones, Google Drive and
    LibreOffice show empty cells and empty charts until then. The ``formulas`` package computes the results here.
    Returns how many results were stored (0 when the package is missing or anything fails: the file is still fine).
    """
    try:
        import formulas
        solved = formulas.ExcelModel().loads(str(path)).finish().calculate()
    except Exception:  # noqa: BLE001 - baking is a bonus; never lose the workbook over it
        return 0
    import os
    import zipfile
    from xml.sax.saxutils import escape

    book = load_workbook(path)
    names = book.sheetnames
    values = {ws.title: {c.coordinate: c.value for row in ws.iter_rows() for c in row
                         if c.value is not None and not (isinstance(c.value, str) and c.value.startswith("="))}
              for ws in book.worksheets}
    for key, cell in solved.items():
        m = re.match(r"'\[.*?\](.+?)'!([A-Z]+\d+)$", key)
        if not m:
            continue
        sheet = next((n for n in names if n.upper() == m.group(1).upper()), None)
        try:
            v = cell.value[0][0]
        except (AttributeError, IndexError, TypeError):
            continue
        v = v.item() if hasattr(v, "item") else v
        if sheet and isinstance(v, (int, float, str, bool)):
            values[sheet][m.group(2)] = v

    def column_values(ref):
        m = re.match(r"'?(.+?)'?!\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$", ref.replace("&apos;", "'"))
        if not m or (m.group(4) and m.group(4) != m.group(2)):
            return []
        sheet, col, r1, _, r2 = m.groups()
        return [values.get(sheet, {}).get(f"{col}{r}") for r in range(int(r1), int(r2 or r1) + 1)]

    def cache(m):
        ref = m.group(1)
        vals = column_values(ref)
        if not vals or any(v is None for v in vals):
            return m.group(0)
        pts = "".join(f'<pt idx="{i}"><v>{escape(str(v))}</v></pt>' for i, v in enumerate(vals))
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            return f'<numRef><f>{ref}</f><numCache><formatCode>General</formatCode><ptCount val="{len(vals)}"/>{pts}</numCache></numRef>'
        return f'<strRef><f>{ref}</f><strCache><ptCount val="{len(vals)}"/>{pts}</strCache></strRef>'

    stored = 0
    tmp = Path(str(path) + ".tmp")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            sheet_file = re.match(r"xl/worksheets/sheet(\d+)\.xml$", item.filename)
            if sheet_file and int(sheet_file.group(1)) <= len(names):
                current = names[int(sheet_file.group(1)) - 1]

                def put(m, current=current):
                    ref, attrs, formula = m.group(1), m.group(2), m.group(3)
                    v = values[current].get(ref)
                    if v is None:
                        return m.group(0)
                    attrs = re.sub(r' t="[^"]*"', "", attrs)
                    if isinstance(v, bool):
                        return f'<c r="{ref}"{attrs} t="b"><f>{formula}</f><v>{int(v)}</v></c>'
                    if isinstance(v, str):
                        return f'<c r="{ref}"{attrs} t="str"><f>{formula}</f><v>{escape(v)}</v></c>'
                    return f'<c r="{ref}"{attrs}><f>{formula}</f><v>{repr(float(v)) if isinstance(v, float) else v}</v></c>'

                text, n = re.subn(r'<c r="([A-Z]+\d+)"([^>]*)><f>(.*?)</f><v\s*/?>(?:</v>)?</c>', put, data.decode("utf-8"))
                stored += n
                data = text.encode("utf-8")
            elif item.filename.startswith("xl/charts/chart"):
                data = re.sub(r"<(?:numRef|strRef)><f>(.*?)</f></(?:numRef|strRef)>", cache, data.decode("utf-8")).encode("utf-8")
            zout.writestr(item, data)
    os.replace(tmp, path)
    return stored


def save(wb, path):
    """Save, then open the file again and print what is in it (formulas are computed when Excel opens it)."""
    path = Path(path)
    wb.save(path)
    baked = _bake(path)
    back = load_workbook(path)
    for ws in back.worksheets:
        n = sum(1 for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("="))
        print(f"saved {path.name} | sheet {ws.title}: {ws.dimensions}, {n} formulas, {len(ws._charts)} charts")
    print(f"results stored for {baked} formulas, so charts show in previews too" if baked else "(formula results are computed when Excel opens the file)")
    return path


class _Xl:
    workbook = staticmethod(workbook)
    write_table = staticmethod(write_table)
    summary = staticmethod(summary)
    add_formula = staticmethod(add_formula)
    add_values = staticmethod(add_values)
    flag = staticmethod(flag)
    bars = staticmethod(bars)
    dashboard = staticmethod(dashboard)
    total = staticmethod(total)
    count = staticmethod(count)
    best = staticmethod(best)
    kpi = staticmethod(kpi)
    chart = staticmethod(chart)
    save = staticmethod(save)
    autosize = staticmethod(autosize)


xl = _Xl()
