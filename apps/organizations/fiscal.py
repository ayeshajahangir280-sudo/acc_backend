"""Fiscal-year arithmetic. A fiscal year starts on the 1st of `start_month` and ends the day before the next one."""
import calendar
from datetime import date, timedelta


def fiscal_year_bounds(on, start_month):
    """(first_day, last_day) of the fiscal year that contains `on`."""
    year = on.year if on.month >= start_month else on.year - 1
    first = date(year, start_month, 1)
    nxt = date(year + 1, start_month, 1)
    return first, nxt - timedelta(days=1)


def is_fiscal_year_end(d, start_month):
    return fiscal_year_bounds(d, start_month)[1] == d


def previous_year_end(d, start_month):
    return fiscal_year_bounds(d, start_month)[0] - timedelta(days=1)
