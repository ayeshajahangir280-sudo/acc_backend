"""Financial reports computed straight from ledger lines (and invoice documents for sales summary).

Nothing here is stored or cached: every figure is derived from posted accounting entries, so a report can
never disagree with the ledger. Voided documents contribute both the original and the reversing entry,
which net to zero.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDay, TruncMonth

from apps.ledger.models import Account, AccountType, JournalLine
from apps.ledger.selectors import party_balances
from apps.sales.models import SalesInvoice
from apps.treasury.selectors import outstanding_invoices

Z = Decimal("0")


def _lines(org, date_from=None, date_to=None, branch=None):
    qs = JournalLine.objects.filter(organization=org)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if branch:
        qs = qs.filter(branch_id=branch)
    return qs


def _net_by_account(org, date_from=None, date_to=None, branch=None):
    rows = _lines(org, date_from, date_to, branch).values("account_id").annotate(dr=Sum("debit"), cr=Sum("credit"))
    return {r["account_id"]: (r["dr"] or Z, r["cr"] or Z) for r in rows}


def trial_balance(org, as_of=None, branch=None):
    net = _net_by_account(org, None, as_of, branch)
    rows, tot_dr, tot_cr = [], Z, Z
    for a in Account.objects.filter(organization=org).order_by("code"):
        dr, cr = net.get(a.id, (Z, Z))
        bal = dr - cr
        if dr == 0 and cr == 0:
            continue
        row = {"account": a.id, "code": a.code, "name": a.name, "type": a.type,
               "debit": bal if bal > 0 else Z, "credit": -bal if bal < 0 else Z}
        tot_dr += row["debit"]
        tot_cr += row["credit"]
        rows.append(row)
    return {"as_of": as_of, "rows": rows, "total_debit": tot_dr, "total_credit": tot_cr, "balanced": tot_dr == tot_cr}


def _section(org, net, types, sign):
    """sign=+1: debit-normal (assets, expenses); -1: credit-normal (liabilities, equity, income)."""
    out, total = [], Z
    for a in Account.objects.filter(organization=org, type__in=types).order_by("code"):
        dr, cr = net.get(a.id, (Z, Z))
        amount = (dr - cr) * sign
        if dr == 0 and cr == 0:
            continue
        out.append({"account": a.id, "code": a.code, "name": a.name, "amount": amount})
        total += amount
    return out, total


def profit_and_loss(org, date_from=None, date_to=None, branch=None):
    net = _net_by_account(org, date_from, date_to, branch)
    income, total_income = _section(org, net, [AccountType.INCOME], -1)
    expenses, total_expenses = _section(org, net, [AccountType.EXPENSE], +1)
    return {"date_from": date_from, "date_to": date_to, "income": income, "total_income": total_income,
            "expenses": expenses, "total_expenses": total_expenses, "net_profit": total_income - total_expenses}


def balance_sheet(org, as_of=None, branch=None):
    net = _net_by_account(org, None, as_of, branch)
    assets, total_assets = _section(org, net, [AccountType.ASSET], +1)
    liabilities, total_liabilities = _section(org, net, [AccountType.LIABILITY], -1)
    equity, total_equity = _section(org, net, [AccountType.EQUITY], -1)
    _, income = _section(org, net, [AccountType.INCOME], -1)
    _, expense = _section(org, net, [AccountType.EXPENSE], +1)
    earnings = income - expense  # cumulative profit not yet closed into equity (there is no period close yet)
    total_le = total_liabilities + total_equity + earnings
    return {"as_of": as_of, "assets": assets, "total_assets": total_assets, "liabilities": liabilities,
            "total_liabilities": total_liabilities, "equity": equity, "total_equity": total_equity,
            "current_period_earnings": earnings, "total_liabilities_and_equity": total_le,
            "balanced": total_assets == total_le}


def bucket_labels(bounds):
    labels = ["current", f"1-{bounds[0]}"]
    labels += [f"{bounds[i] + 1}-{bounds[i + 1]}" for i in range(len(bounds) - 1)]
    labels.append(f"{bounds[-1]}+")
    return labels


def aging(org, direction, as_of, party=None, bounds=(30, 60, 90)):
    """Invoice-level aging by days past due as of `as_of`, from allocations (not party-level balances).

    `unapplied` = ledger balance - invoice outstanding: unapplied receipts/payments and journal
    adjustments, negative meaning a credit. This keeps every total reconcilable to the ledger.
    """
    labels = bucket_labels(bounds)
    edges = [as_of - timedelta(days=b) for b in bounds]  # due on/after edges[i] => within bucket i+1
    inv = outstanding_invoices(org, direction, party=party, as_of=as_of).annotate(eff_due=Coalesce("due_date", "date"))
    inv = inv.exclude(outstanding=0)
    aggs = {"b0": Sum("outstanding", filter=Q(eff_due__gte=as_of))}
    previous = as_of
    for i, edge in enumerate(edges):
        aggs[f"b{i + 1}"] = Sum("outstanding", filter=Q(eff_due__lt=previous, eff_due__gte=edge))
        previous = edge
    aggs[f"b{len(edges) + 1}"] = Sum("outstanding", filter=Q(eff_due__lt=previous))
    rows = {r["party_id"]: r for r in inv.values("party_id", "party__name").annotate(**aggs, n=Count("id"))}

    ledger = party_balances(org, party_id=party, as_of=as_of)
    key = "receivable" if direction == "receipt" else "payable"
    from apps.parties.models import Party

    names = dict(Party.objects.filter(organization=org, id__in=set(rows) | set(ledger)).values_list("id", "name"))
    out, totals = [], {l: Z for l in labels}
    t_out = t_unapplied = t_ledger = Z
    for pid in sorted(set(rows) | set(ledger), key=lambda i: names.get(i, "")):
        r = rows.get(pid)
        buckets = {labels[i]: (r[f"b{i}"] or Z) if r else Z for i in range(len(labels))}
        outstanding = sum(buckets.values(), Z)
        led = ledger.get(pid, {}).get(key, Z)
        unapplied = led - outstanding
        if outstanding == 0 and unapplied == 0:
            continue
        out.append({"party": pid, "party_name": names.get(pid, ""), "buckets": buckets, "outstanding": outstanding,
                    "unapplied": unapplied, "ledger_balance": led})
        for l in labels:
            totals[l] += buckets[l]
        t_out += outstanding
        t_unapplied += unapplied
        t_ledger += led
    return {"as_of": as_of, "type": "receivable" if direction == "receipt" else "payable", "labels": labels, "rows": out,
            "totals": {"buckets": totals, "outstanding": t_out, "unapplied": t_unapplied, "ledger_balance": t_ledger}}


def vendor_report(org, date_from=None, date_to=None, party=None):
    """Per supplier: opening payable, purchases, returns, payments, other adjustments, closing payable.
    Derived from Accounts Payable ledger lines so it always equals the supplier's ledger balance."""
    lines = JournalLine.objects.filter(organization=org, party__isnull=False, account__system_code="ACCOUNTS_PAYABLE")
    if date_to:
        lines = lines.filter(date__lte=date_to)
    if party:
        lines = lines.filter(party_id=party)
    before = Q(date__lt=date_from) if date_from else Q(pk__in=[])
    during = Q(date__gte=date_from) if date_from else Q()
    rows = lines.values("party_id", "party__name", "transaction__source_type").annotate(
        o_dr=Sum("debit", filter=before), o_cr=Sum("credit", filter=before),
        p_dr=Sum("debit", filter=during), p_cr=Sum("credit", filter=during))
    acc = {}
    for r in rows:
        a = acc.setdefault(r["party_id"], {"party": r["party_id"], "party_name": r["party__name"], "opening": Z,
                                          "purchases": Z, "returns": Z, "payments": Z, "other": Z})
        a["opening"] += (r["o_cr"] or Z) - (r["o_dr"] or Z)
        net_cr = (r["p_cr"] or Z) - (r["p_dr"] or Z)
        src = r["transaction__source_type"]
        if src == "purchases.invoice":
            a["purchases"] += net_cr
        elif src == "purchases.return":
            a["returns"] += -net_cr
        elif src == "treasury.payment":
            a["payments"] += -net_cr
        else:
            a["other"] += net_cr
    out = []
    for a in sorted(acc.values(), key=lambda x: x["party_name"]):
        a["closing"] = a["opening"] + a["purchases"] - a["returns"] - a["payments"] + a["other"]
        if any(a[k] != 0 for k in ("opening", "purchases", "returns", "payments", "other", "closing")):
            out.append(a)
    keys = ("opening", "purchases", "returns", "payments", "other", "closing")
    return {"date_from": date_from, "date_to": date_to, "rows": out, "totals": {k: sum((r[k] for r in out), Z) for k in keys}}


def sales_summary(org, date_from=None, date_to=None, party=None, group_by="month"):
    qs = SalesInvoice.objects.filter(organization=org, status="posted")
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if party:
        qs = qs.filter(party_id=party)
    inv, ret = Q(kind="invoice"), Q(kind="return")
    if group_by == "customer":
        grouped = qs.values(key=F("party_id"), label=F("party__name"))
    else:
        trunc = TruncDay if group_by == "day" else TruncMonth
        grouped = qs.annotate(period=trunc("date")).values(key=F("period"), label=F("period"))
    rows = list(grouped.annotate(
        invoices=Count("id", filter=inv), returns=Count("id", filter=ret),
        sales=Sum("subtotal", filter=inv), sales_tax=Sum("tax_total", filter=inv), sales_total=Sum("total", filter=inv),
        returns_subtotal=Sum("subtotal", filter=ret), returns_tax=Sum("tax_total", filter=ret), returns_total=Sum("total", filter=ret),
    ).order_by("label" if group_by == "customer" else "key"))
    keys = ("sales", "sales_tax", "sales_total", "returns_subtotal", "returns_tax", "returns_total")
    for r in rows:
        for k in keys:
            r[k] = r[k] or Z
        r["net_sales"] = r["sales"] - r["returns_subtotal"]
        r["net_total"] = r["sales_total"] - r["returns_total"]
        if isinstance(r["label"], date):
            r["label"] = r["label"].isoformat() if group_by == "day" else r["label"].strftime("%Y-%m")
        r["key"] = r["label"] if group_by != "customer" else r["key"]
    totals = {k: sum((r[k] for r in rows), Z) for k in (*keys, "net_sales", "net_total", "invoices", "returns")}
    return {"date_from": date_from, "date_to": date_to, "group_by": group_by, "rows": rows, "totals": totals}


def vat_summary(org, date_from=None, date_to=None, branch=None):
    """Tax collected (output) and tax paid (input) from the tax ledger accounts, including returns.

    This is a ledger summary to support preparing a return. It is NOT a filing and has no official return layout.
    """
    net = _net_by_account(org, date_from, date_to, branch)
    ids = dict(Account.objects.filter(organization=org, system_code__in=["TAX_OUTPUT", "TAX_INPUT"]).values_list("system_code", "id"))
    out_dr, out_cr = net.get(ids.get("TAX_OUTPUT"), (Z, Z))
    in_dr, in_cr = net.get(ids.get("TAX_INPUT"), (Z, Z))
    output_tax, input_tax = out_cr - out_dr, in_dr - in_cr
    return {"date_from": date_from, "date_to": date_to,
            "output_tax": {"charged": out_cr, "returned": out_dr, "net": output_tax},
            "input_tax": {"paid": in_dr, "returned": in_cr, "net": input_tax},
            "net_payable": output_tax - input_tax}
