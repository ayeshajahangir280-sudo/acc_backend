"""Granular, per-organization permissions: module x action.

Every membership has a role (owner / admin / accountant / viewer). The role is a *preset*; an owner or admin can give a
member custom permissions instead (Membership.permissions, a {module: [actions]} dict; NULL = use the preset).

Enforcement is central: core.permissions.IsOrgMember asks `resolve(request, view)` which (module, action) a request
needs and checks it against the caller's effective permissions. Writes to views/actions that are not mapped here are
refused (fail closed); tests walk every registered route to make sure each one is mapped.

Actions are independent: granting one never implies another. In particular "edit" (change existing records) does not
include delete, void, post, approve, export or manage; each must be granted explicitly. The only coupling is in the UI,
which ticks "view" when another action of the same module is ticked (an action on records you cannot see is useless).
"update" is accepted as a synonym of "edit". "approve" (approve / reject documents waiting for approval) used to be
stored as "post" for purchase invoices and expenses; that legacy spelling is still read (see LEGACY_ALIASES).

Presets reproduce the behaviour that existed before custom permissions, endpoint by endpoint:
- owner, admin: everything (owner always, whatever is stored).
- accountant: everything except: approving bills/expenses, banking, period lock / fiscal close / branches /
  approval settings / user management (settings.manage, users.manage), opening balances (ledger.manage) and enabling
  inventory costing (inventory.manage).
- viewer: read everything except banking (including viewing PDFs), export reports; no changes and no emailing.
Banking additionally requires an unrestricted role (owner/admin): bank accounts hold every branch's money.
"""
VIEW, CREATE, EDIT, DELETE, POST, APPROVE, VOID, EXPORT, MANAGE = (
    "view", "create", "edit", "delete", "post", "approve", "void", "export", "manage")
ACTIONS = [VIEW, CREATE, EDIT, DELETE, POST, APPROVE, VOID, EXPORT, MANAGE]

# (key, label, group, actions that mean something for this module)
MODULES = [
    ("dashboard", "Dashboard", "General", [VIEW]),
    ("quotes", "Quotes", "Sales", [VIEW, CREATE, EDIT, POST, EXPORT]),
    ("sales_orders", "Sales orders", "Sales", [VIEW, CREATE, EDIT, POST, EXPORT]),
    ("sales_invoices", "Sales invoices", "Sales", [VIEW, CREATE, EDIT, VOID, EXPORT]),
    ("sales_returns", "Sales returns", "Sales", [VIEW, CREATE, VOID, EXPORT]),
    ("receipts", "Receipts (money in)", "Sales", [VIEW, CREATE, EDIT, VOID, EXPORT]),
    ("customer_credits", "Customer credits", "Sales", [VIEW, CREATE, EDIT, VOID]),
    ("purchase_orders", "Purchase orders", "Purchases", [VIEW, CREATE, EDIT, POST, EXPORT]),
    ("purchase_invoices", "Purchase invoices", "Purchases", [VIEW, CREATE, APPROVE, VOID, EXPORT]),
    ("purchase_returns", "Purchase returns", "Purchases", [VIEW, CREATE, VOID, EXPORT]),
    ("payments", "Payments (money out)", "Purchases", [VIEW, CREATE, EDIT, VOID, EXPORT]),
    ("supplier_credits", "Supplier credits", "Purchases", [VIEW, CREATE, EDIT, VOID]),
    ("expenses", "Expenses", "Purchases", [VIEW, CREATE, EDIT, APPROVE, VOID, EXPORT]),
    ("banking", "Banking", "Finance", [VIEW, CREATE, EDIT, DELETE, EXPORT]),
    ("inventory", "Inventory & van loading", "Finance", [VIEW, CREATE, EDIT, VOID, MANAGE]),
    ("ledger", "Ledger & vouchers", "Finance", [VIEW, CREATE, VOID, MANAGE]),
    ("reports", "Reports", "Finance", [VIEW, EXPORT]),
    ("chart_of_accounts", "Chart of accounts", "Setup", [VIEW, CREATE, EDIT]),
    ("contacts", "Customers & suppliers", "Setup", [VIEW, CREATE, EDIT, EXPORT]),
    ("settings", "Organization settings", "Setup", [VIEW, CREATE, EDIT, MANAGE]),
    ("users", "User management", "Setup", [VIEW, MANAGE]),
]
MODULE_KEYS = [m[0] for m in MODULES]
MODULE_ACTIONS = {m[0]: set(m[3]) for m in MODULES}


def _all():
    return {m: set(a) for m, a in MODULE_ACTIONS.items()}


def _accountant():
    p = _all()
    p["purchase_invoices"].discard(APPROVE)  # approving bills
    p["expenses"].discard(APPROVE)  # approving expenses
    p["banking"] = set()  # bank data spans every branch: branch-restricted roles never see it (see BankStatementViewSet)
    p["inventory"].discard(MANAGE)
    p["ledger"].discard(MANAGE)
    p["settings"].discard(MANAGE)
    p["users"] = {VIEW}
    return p


def _viewer():
    p = {m: {VIEW} for m in MODULE_KEYS}
    p["reports"] = {VIEW, EXPORT}
    p["banking"] = set()
    return p


PRESETS = {"owner": _all(), "admin": _all(), "accountant": _accountant(), "viewer": _viewer()}


# Spellings accepted on input and in stored data. "post" -> "approve" applies only to modules where approving is the
# meaning of the old "post" (they support "approve" but not "post"); elsewhere "post" keeps its own meaning.
LEGACY_ALIASES = {"update": EDIT}


def _canonical(mod, action):
    action = LEGACY_ALIASES.get(action, action)
    if action == POST and POST not in MODULE_ACTIONS[mod] and APPROVE in MODULE_ACTIONS[mod]:
        return APPROVE
    return action


def normalize(raw):
    """Clean a {module: [actions]} payload: unknown modules/actions dropped, actions limited to what the module supports.

    Nothing is ever added: an action is granted only if it is listed (after legacy aliases)."""
    out = {}
    if not isinstance(raw, dict):
        raise ValueError("Permissions must be an object of module: [actions].")
    for mod, acts in raw.items():
        if mod not in MODULE_ACTIONS:
            continue
        if not isinstance(acts, (list, tuple, set)):
            raise ValueError(f"Permissions for {mod} must be a list of actions.")
        out[mod] = {c for c in (_canonical(mod, a) for a in acts if isinstance(a, str)) if c in MODULE_ACTIONS[mod]}
    return {m: out.get(m, set()) for m in MODULE_KEYS}


def effective(membership):
    """{module: set(actions)} for an active membership. Owners always have everything."""
    if membership is None or not membership.is_active:
        return {m: set() for m in MODULE_KEYS}
    if membership.role == "owner":
        return _all()
    if membership.permissions is not None:
        try:
            return normalize(membership.permissions)
        except ValueError:
            return {m: set() for m in MODULE_KEYS}
    return {m: set(a) for m, a in PRESETS.get(membership.role, {}).items()}


def has(membership, module, action):
    """module may be a tuple of alternatives (any one is enough)."""
    if isinstance(module, tuple):
        return any(has(membership, m, action) for m in module)
    cache = getattr(membership, "_perm_cache", None)
    if cache is None:
        cache = effective(membership)
        if membership is not None:
            membership._perm_cache = cache
    return action in cache.get(module, set())


def as_lists(perms):
    return {m: sorted(perms.get(m, set()), key=ACTIONS.index) for m in MODULE_KEYS}


def flat(perms):
    """{module: actions} -> set of "module.action" strings (for diffs and audit records)."""
    return {f"{m}.{a}" for m, acts in perms.items() for a in acts}


def exceeds(perms, limit):
    """Sorted "module.action" strings present in `perms` but not in `limit`."""
    return sorted(flat(perms) - flat(limit), key=lambda s: (MODULE_KEYS.index(s.split(".")[0]), ACTIONS.index(s.split(".")[1])))


# ---------------------------------------------------------------------------------------------------------------------
# Route table: which (module, action) each request needs.
# ---------------------------------------------------------------------------------------------------------------------
STD = {"list": VIEW, "retrieve": VIEW, "create": CREATE, "update": EDIT, "partial_update": EDIT, "destroy": DELETE}
REFERENCE = object()  # GETs any active member may make (lookups used by forms); writes still need the module permission


def _kind_from(request, view, model, field="kind", query_param=None):
    """Document kind for a request: from the URL object (detail), the body (create) or a query parameter (list)."""
    pk = view.kwargs.get("pk")
    if pk is not None:
        val = model.objects.filter(pk=pk, organization=request.organization).values_list(field, flat=True).first()
        return val
    if request.method == "POST":
        return request.data.get(field) if hasattr(request.data, "get") else None
    return request.query_params.get(query_param or field)


def _sales_doc(request, view):
    from apps.sales.models import SalesInvoice

    return "sales_returns" if _kind_from(request, view, SalesInvoice) == "return" else "sales_invoices"


def _purchase_doc(request, view):
    from apps.purchases.models import PurchaseInvoice

    return "purchase_returns" if _kind_from(request, view, PurchaseInvoice) == "return" else "purchase_invoices"


def _sales_order(request, view):
    from apps.orders.models import SalesOrder

    return "quotes" if _kind_from(request, view, SalesOrder) == "quote" else "sales_orders"


DOC = {"void": VOID, "pdf": VIEW, "email": EXPORT}
CREDITS = {"credit_list": VIEW, "credit": VIEW, "apply_credit": CREATE, "unapply_credit": EDIT, "void_refund": VOID,
           "refunds": {"GET": VIEW, "POST": CREATE}}

# view class name -> (module or callable(request, view) -> module, {drf action: permission action | {METHOD: action}},
#                     reference_gets, {drf action: module override})
ROUTES = {
    "SalesInvoiceViewSet": (_sales_doc, {**DOC}, False, {k: "customer_credits" for k in CREDITS}),
    # approve / reject: gated at "create" here; the approval service then requires "approve" and answers 403
    # approval_forbidden (the long-standing error code clients rely on).
    "PurchaseInvoiceViewSet": (_purchase_doc, {**DOC, "approve": CREATE, "reject": CREATE}, False, {k: "supplier_credits" for k in CREDITS}),
    "SalesOrderViewSet": (_sales_order, {"accept": EDIT, "decline": EDIT, "cancel": EDIT, "convert": POST}, False, {}),
    "PurchaseOrderViewSet": ("purchase_orders", {"accept": EDIT, "decline": EDIT, "cancel": EDIT, "convert": POST}, False, {}),
    # open_invoices is invoice-balance data: readable with either the cash module or the invoice module
    "ReceiptViewSet": ("receipts", {"void": VOID, "allocate": EDIT, "open_invoices": VIEW}, False, {"open_invoices": ("receipts", "sales_invoices")}),
    "PaymentViewSet": ("payments", {"void": VOID, "allocate": EDIT, "open_invoices": VIEW}, False, {"open_invoices": ("payments", "purchase_invoices")}),
    "ExpenseViewSet": ("expenses", {"void": VOID, "approve": CREATE, "reject": CREATE, "receipts": {"GET": VIEW, "POST": EDIT},
                                    "receipt_file": {"GET": VIEW, "DELETE": EDIT}, "receipt": {"GET": VIEW, "POST": EDIT}}, False, {}),
    "RecurringViewSet": ("sales_invoices", {"pause": EDIT, "resume": EDIT, "run_now": CREATE}, False, {}),
    "RecurringExpenseViewSet": ("expenses", {"pause": EDIT, "resume": EDIT, "run_now": CREATE}, False, {}),
    "BankStatementViewSet": ("banking", {"import_": CREATE, "lines": VIEW, "candidates": VIEW, "suggestions": VIEW, "report": EXPORT,
                                         "match": EDIT, "unmatch": EDIT, "auto_match": EDIT, "complete": EDIT, "reopen": EDIT,
                                         "discard": DELETE, "create_from_line": CREATE}, False, {}),
    "BankRuleViewSet": ("banking", {}, False, {}),
    "ItemViewSet": ("inventory", {}, True, {}),
    "LocationViewSet": ("inventory", {}, True, {}),
    "StockViewSet": ("inventory", {}, False, {}),
    "MovementViewSet": ("inventory", {}, False, {}),
    "AdjustmentViewSet": ("inventory", {}, False, {}),
    "ProductionViewSet": ("inventory", {"void": VOID}, False, {}),
    "CostingViewSet": ("inventory", {"enable": MANAGE, "valuation": VIEW}, False, {}),
    "VehicleViewSet": ("inventory", {}, False, {}),
    "VanLoadViewSet": ("inventory", {"send_out": EDIT, "close": EDIT, "cancel": VOID}, False, {}),
    "AccountViewSet": ("chart_of_accounts", {"balances": VIEW}, True, {}),
    "JournalViewSet": ("ledger", {}, False, {}),
    "PartyBalanceViewSet": ("reports", {}, False, {}),
    "VoucherViewSet": ("ledger", {"void": VOID}, False, {}),
    "PartyViewSet": ("contacts", {"statement": VIEW, "statement_pdf": VIEW, "statement_email": EXPORT}, True, {}),
    "TaxRateViewSet": ("settings", {}, True, {}),
    "BranchViewSet": ("settings", {"create": MANAGE, "update": MANAGE, "partial_update": MANAGE}, True, {}),
    "BranchAccessViewSet": ("settings", {"create": MANAGE, "destroy": MANAGE}, True, {}),
    "MembershipViewSet": ("users", {"partial_update": MANAGE, "update": MANAGE, "destroy": MANAGE, "audit": MANAGE}, False, {}),
    "InvitationViewSet": ("users", {"list": MANAGE, "retrieve": MANAGE, "create": MANAGE, "resend": MANAGE, "cancel": MANAGE}, False, {}),
    # APIViews (no DRF action): reports
    "TrialBalanceView": ("reports", {}, False, {}), "ProfitLossView": ("reports", {}, False, {}),
    "BalanceSheetView": ("reports", {}, False, {}), "AgingView": ("reports", {}, False, {}),
    "VendorReportView": ("reports", {}, False, {}), "SalesSummaryView": (("reports", "sales_invoices"), {}, False, {}),
    "VatSummaryView": ("reports", {}, False, {}), "VatReturnView": ("reports", {}, False, {}),
}
SAFE = {"GET", "HEAD", "OPTIONS"}


class Unmapped(Exception):
    pass


def resolve(request, view):
    """(module, action) the request needs; None when any active member may do it (reference-data reads)."""
    entry = ROUTES.get(type(view).__name__)
    if entry is None:
        raise Unmapped(type(view).__name__)
    module, extra, reference, overrides = entry
    if request.method.lower() not in getattr(view, "http_method_names", ()):
        return None  # method not allowed on this view: no handler runs, DRF answers 405
    act_name = getattr(view, "action", None)
    if act_name is None and hasattr(view, "action_map"):
        return None  # viewset with no handler for this method: nothing can run, DRF answers 405
    if act_name is None:  # plain APIView
        if request.method in SAFE:
            return (module(request, view) if callable(module) else module), VIEW
        raise Unmapped(type(view).__name__)
    spec = extra.get(act_name, STD.get(act_name))
    if isinstance(spec, dict):
        spec = spec.get(request.method)
    if spec is None:
        if act_name in overrides and act_name in CREDITS:
            spec = CREDITS[act_name]
            spec = spec.get(request.method) if isinstance(spec, dict) else spec
        if spec is None:
            if request.method in SAFE and act_name not in STD:
                spec = VIEW
            else:
                raise Unmapped(f"{type(view).__name__}.{act_name}")
    if reference and request.method in SAFE and spec == VIEW and act_name in ("list", "retrieve"):
        return None
    mod = overrides.get(act_name) or (module(request, view) if callable(module) else module)
    return mod, spec
