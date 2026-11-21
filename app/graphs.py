"""Render a self-contained PNG for Telegram; no external chart service receives data."""
from io import BytesIO

from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.ticker import FuncFormatter

from .core import money


def render_summary(summary):
    fig = Figure(figsize=(12, 7), dpi=150, facecolor='#f5f6fb')
    canvas = FigureCanvasAgg(fig)
    fig.text(.07, .92, 'penny / monthly recap', color='#777e96', fontsize=12)
    fig.text(.07, .85, summary['month'], color='#1b2440', fontsize=21, weight='bold')
    fig.text(.07, .76, money(summary['total']), color='#1b2440', fontsize=32, weight='bold')
    budget = summary['budget']
    detail = f"{summary['count']} expenses  |  " + (f"Budget {money(budget)}  |  {'Remaining' if summary['remaining'] >= 0 else 'Over budget'} {money(abs(summary['remaining']))}" if budget else 'No budget set')
    fig.text(.07, .70, detail, color='#626c85', fontsize=11)
    categories = sorted([c for c in summary['categories'] if c['amount']], key=lambda c: c['amount'])
    ax = fig.add_axes([.21, .11, .70, .50], facecolor='#f5f6fb')
    if categories:
        amounts = [c['amount'] / 100 for c in categories]
        bars = ax.barh([c['label'] for c in categories], amounts, color=[c['color'] for c in categories], height=.65)
        ax.set_xlim(0, max(amounts) * 1.35)
        for bar, cat in zip(bars, categories):
            ax.text(bar.get_width() + max(amounts) * .02, bar.get_y() + bar.get_height() / 2,
                    f"{money(cat['amount'])} ({cat['amount'] / summary['total']:.0%})", va='center', fontsize=8, color='#36425e')
        ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f'${x:,.0f}'))
    else:
        ax.text(.5, .5, 'No expenses recorded this month', transform=ax.transAxes, ha='center', color='#626c85')
        ax.set_xticks([])
        ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(axis='both', length=0, colors='#626c85', labelsize=9)
    fig.text(.07, .035, f"SGD · {summary['timezone']} · recorded expenses only", color='#777e96', fontsize=9)
    output = BytesIO()
    canvas.print_png(output)
    return output.getvalue()
