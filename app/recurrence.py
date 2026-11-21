"""Calendar-anchored subscription schedules, shared by posting and reminders."""
import calendar
from datetime import date, timedelta
from fractions import Fraction


def start_date(sub):
    if sub['start_date']:
        return date.fromisoformat(sub['start_date'])
    first = date.fromisoformat(sub['start_month'] + '-01')
    return first.replace(day=min(sub['billing_day'], calendar.monthrange(first.year, first.month)[1]))


def due_dates(sub, first, last):
    """Yield dates without drifting after short months or leap years."""
    anchor = start_date(sub)
    first = max(first, anchor, date.fromisoformat(sub['schedule_effective_from'] or anchor.isoformat()))
    count, unit = sub['interval_count'], sub['interval_unit']
    if unit in ('days', 'weeks'):
        step = count * (7 if unit == 'weeks' else 1)
        index = max(0, (first - anchor).days // step)
        while True:
            ordinal = anchor.toordinal() + index * step
            if ordinal > last.toordinal():
                return
            due = date.fromordinal(ordinal)
            if due >= first:
                yield due
            index += 1
    else:
        step = count * (12 if unit == 'years' else 1)
        offset = (first.year - anchor.year) * 12 + first.month - anchor.month
        index = max(0, offset // step)
        while True:
            month_index = (anchor.year - 1) * 12 + anchor.month - 1 + index * step
            year, month = month_index // 12 + 1, month_index % 12 + 1
            if year > last.year or (year == last.year and month > last.month):
                return
            due = date(year, month, min(sub['billing_day'], calendar.monthrange(year, month)[1]))
            if due > last:
                return
            if due >= first:
                yield due
            index += 1


def next_due(sub, as_of):
    return next(due_dates(sub, as_of, date.max), None)


def annual_cost(sub):
    # Day/week plans use a 365-day year; these are estimates, not posted expenses.
    periods = {'days': Fraction(365), 'weeks': Fraction(365, 7),
               'months': Fraction(12), 'years': Fraction(1)}
    return sub['amount'] * periods[sub['interval_unit']] / sub['interval_count']
