---
name: charts
description: draw charts and graphs to PNG with matplotlib
triggers: chart|\bgraphs?\b|\bplot|visuali[sz]|histogram|pie\b|diagram
---
Chart recipe (matplotlib is installed). Never call plt.show(): there is no screen. Save the image and say where it is:
```python
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

labels, values = ["Jan", "Feb", "Mar"], [120, 150, 90]
fig, ax = plt.subplots(figsize=(8, 4.5))
bars = ax.bar(labels, values, color="#1f77b4")
ax.bar_label(bars)
ax.set_title("Units sold"); ax.set_ylabel("Units")
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig("chart.png", dpi=150)
plt.close(fig)
print("saved chart.png")
```
- Line chart: `ax.plot(x, y, marker="o")`; several series: one `ax.plot` each with `label=` and then `ax.legend()`.
- Dates on the x axis: `fig.autofmt_xdate()`.
- To open the picture for the user on Windows: run_shell `Start-Process chart.png` (only if they asked to see it).
