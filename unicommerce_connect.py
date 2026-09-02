"""
Unicommerce (Uniware) SOAP API pulls for the restock pipeline.

Run this from a machine with real network access. It will NOT work from the
Claude Code cloud sandbox -- both unicommerce.com hosts sit outside that
environment's egress allowlist and the proxy answers 403 to CONNECT.

SCOPE -- two pulls only:
  1. Inventory, per SKU per facility.
  2. Sales, with line items, via the async export job.

Purchase orders are deliberately NOT integrated. POs are not created in
Unicommerce on this account's plan, so GetPurchaseOrders,
GetPurchaseOrderDetail and the OpenPurchase field on inventory responses all
return unreliable numbers. Open-PO quantities come from a manually
maintained file outside Unicommerce. Do not build against those operations
or that field -- see IGNORED_RESPONSE_FIELDS below.

SETUP:
  pip install -r requirements.txt
    export UNICOMMERCE_USERNAME="claude"
    export UNICOMMERCE_API_KEY="<the key from the admin panel>"
    export UNICOMMERCE_ENV="production"            # or "sandbox"

USAGE -- narrow slices are the default; full runs need an explicit flag:
  python3 unicommerce_connect.py describe                    # run this first
  python3 unicommerce_connect.py operations
  python3 unicommerce_connect.py inventory --sku ABC123      # narrow
  python3 unicommerce_connect.py inventory --all             # full catalogue
  python3 unicommerce_connect.py sale-orders --days 1        # narrow, headers
  python3 unicommerce_connect.py export --days 1             # narrow, line items
  python3 unicommerce_connect.py export --days 90 --full     # full window

RUN `describe` FIRST.
  Operation NAMES for inventory and SearchSaleOrder are confirmed against the
  real v1.9 WSDL. CreateExportJob/GetExportJobStatus are NOT yet confirmed to
  exist on this tenant -- `describe` reports whether they do. Request FIELD
  names throughout are marked `# UNVERIFIED` and grouped one dict per
  function, so reconciling each against `describe` output is a single edit.

Endpoints (from the account's API panel):
  Sandbox:    WSDL https://staging.unicommerce.com/services/soap/uniware15.wsdl
  Production: WSDL https://styxxinternational.unicommerce.com/services/soap/uniware19.wsdl
"""

import argparse
import datetime as dt
import difflib
import os
import sys
import time

from zeep import Client
from zeep.wsse.username import UsernameToken

# --- Configuration --------------------------------------------------------

ENV = os.environ.get("UNICOMMERCE_ENV", "sandbox").lower()
USERNAME = os.environ.get("UNICOMMERCE_USERNAME")
API_KEY = os.environ.get("UNICOMMERCE_API_KEY")

WSDL_URLS = {
    "sandbox": "https://staging.unicommerce.com/services/soap/uniware15.wsdl",
    "production": "https://styxxinternational.unicommerce.com/services/soap/uniware19.wsdl",
}

OPS = {
    # Confirmed present on the tenant's v1.9 WSDL.
    "inventory_bulk": "GetBulkItemTypeInventory",
    "inventory_snapshot": "GetInventorySnapshot",
    "sale_orders": "SearchSaleOrder",
    # NOT yet confirmed on this tenant -- `describe` reports whether they exist.
    # SearchSaleOrder returns order headers without line items, so the
    # size-wise figure needs this export path instead.
    "export_create": "CreateExportJob",
    "export_status": "GetExportJobStatus",
}

# Present in some responses but meaningless on this account: POs are not
# created in Unicommerce here. Never read these -- open-PO quantities come
# from the manually maintained file instead.
IGNORED_RESPONSE_FIELDS = ("OpenPurchase", "OpenPurchaseQuantity", "PendingPO")

# Narrow-slice ceilings. Exceeding either needs an explicit --full / --all.
NARROW_MAX_DAYS = 7
EXPORT_POLL_SECONDS = 10
EXPORT_TIMEOUT_SECONDS = 900


def require_credentials():
    if not USERNAME or not API_KEY:
        sys.exit(
            "Missing credentials. Set UNICOMMERCE_USERNAME and UNICOMMERCE_API_KEY "
            "as environment variables before running this script."
        )
    if ENV not in WSDL_URLS:
        sys.exit(f"UNICOMMERCE_ENV must be 'sandbox' or 'production', got '{ENV}'")


# --- Connection -------------------------------------------------------------

def get_client():
    """
    Builds an authenticated SOAP client.

    Unicommerce's docs show PasswordText (not PasswordDigest) in their
    example WS-Security header, so use_digest=False. If auth fails with a
    security/nonce error, try use_digest=True -- some Uniware versions
    expect the digest form despite the docs example. Unconfirmed against
    the live service.
    """
    token = UsernameToken(USERNAME, API_KEY, use_digest=False)
    return Client(WSDL_URLS[ENV], wsse=token)


def _binding_operations(client):
    """Maps operation name -> zeep operation object, across all ports."""
    found = {}
    for service in client.wsdl.services.values():
        for port in service.ports.values():
            for name, operation in port.binding._operations.items():
                found.setdefault(name, operation)
    return found


def list_operations(client):
    operations = _binding_operations(client)
    print(f"\n=== Operations available on {WSDL_URLS[ENV]} ===\n")
    for name in sorted(operations):
        print(f"  {name}")
    print(f"\n=== {len(operations)} operations total ===\n")


def describe_operations(client, names=None):
    """
    Prints the exact request signature of each operation we call.

    Resolves two open questions: whether CreateExportJob/GetExportJobStatus
    exist on this tenant at all, and the real field names behind every
    UNVERIFIED marker below. SOAP requests must match the schema exactly.
    """
    operations = _binding_operations(client)
    names = names or list(OPS.values())

    for name in names:
        print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
        operation = operations.get(name)
        if operation is None:
            print("  !! NOT FOUND on this WSDL.")
            close = difflib.get_close_matches(name, operations, n=5, cutoff=0.5)
            close += [o for o in operations
                      if name.lower() in o.lower() and o not in close]
            if close:
                print(f"  Closest names present: {', '.join(close)}")
            continue
        for label, message in (("REQUEST ", operation.input),
                               ("RESPONSE", operation.output)):
            try:
                print(f"  {label}:", message.signature())
            except Exception as exc:                   # noqa: BLE001
                print(f"  {label}: <could not render signature: {exc}>")

    print("\nExport-job operations decide the sales path: if both are absent,")
    print("bulk line-item export is unavailable and the size-wise figure needs")
    print("another route. Reconcile UNVERIFIED field names against the above.\n")


def call(client, op_name, **fields):
    """Invokes a WSDL operation by name, dropping unset (None) fields."""
    operation = getattr(client.service, op_name)
    return operation(**{k: v for k, v in fields.items() if v is not None})


def warn_on_ignored_fields(response):
    """
    Flags PO-derived fields if the service returns them.

    They are unreliable on this account and must not reach the pipeline.
    """
    present = [f for f in IGNORED_RESPONSE_FIELDS if hasattr(response, f)]
    if present:
        print(
            f"  note: ignoring PO-derived field(s) {', '.join(present)} -- "
            "unreliable on this account, use the manual open-PO file",
            file=sys.stderr,
        )
    return response


# --- Pull 1: inventory ------------------------------------------------------

def get_inventory(client, skus=None, facility=None, bulk=None, **overrides):
    """
    Inventory per SKU per facility.

    Two candidate operations, both confirmed to exist. Which returns cleaner
    per-SKU-per-facility numbers is an open question that needs real output
    to settle, so both stay reachable:
      - GetBulkItemTypeInventory (bulk=True)  -- catalogue-wide, default when
        no SKUs are given.
      - GetInventorySnapshot (bulk=False)     -- default when SKUs are given.
    Compare the two on the same narrow SKU slice before picking one.

    Ignores the OpenPurchase family of response fields -- see
    IGNORED_RESPONSE_FIELDS.
    """
    if bulk is None:
        bulk = not skus
    op = OPS["inventory_bulk"] if bulk else OPS["inventory_snapshot"]
    fields = {
        "ItemTypeSKU": skus,          # UNVERIFIED -- may be ItemSKU / SKUList
        "FacilityCode": facility,     # UNVERIFIED
    }
    fields.update(overrides)
    return warn_on_ignored_fields(call(client, op, **fields))


# --- Pull 2: sales ----------------------------------------------------------

def get_sale_orders(client, days=1, start=None, end=None, status=None, **overrides):
    """
    Sale order HEADERS in a date range, via SearchSaleOrder.

    Confirmed to exist, but returns no line items -- so this is the cheap
    narrow probe for checking connectivity, date-field names and order
    volume, not the source of the size-wise sales figure. Use the export
    path below for line items.
    """
    end = end or dt.datetime.now(dt.timezone.utc)
    start = start or (end - dt.timedelta(days=days))
    fields = {
        # UNVERIFIED -- range may be flat From/To or a nested complex type.
        "CreatedFrom": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "CreatedTo": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Status": status,
    }
    fields.update(overrides)
    return call(client, OPS["sale_orders"], **fields)


def create_export_job(client, days=1, start=None, end=None, **overrides):
    """
    Starts an async bulk sale-order export -- the line-item path.

    Returns the raw response; the job identifier is somewhere in it (field
    name UNVERIFIED, see extract_job_id).
    """
    end = end or dt.datetime.now(dt.timezone.utc)
    start = start or (end - dt.timedelta(days=days))
    fields = {
        "ExportType": "SALE_ORDER",   # UNVERIFIED -- enum value and casing
        "DateFrom": start.strftime("%Y-%m-%dT%H:%M:%SZ"),   # UNVERIFIED
        "DateTo": end.strftime("%Y-%m-%dT%H:%M:%SZ"),       # UNVERIFIED
    }
    fields.update(overrides)
    return call(client, OPS["export_create"], **fields)


def extract_job_id(response):
    """Pulls the job id out of a CreateExportJob response."""
    for field in ("JobCode", "JobId", "ExportJobCode", "Code"):  # UNVERIFIED
        value = getattr(response, field, None)
        if value:
            return value
    raise RuntimeError(
        f"Could not find a job id on the CreateExportJob response. "
        f"Fields present: {dir(response)}. Add the right name to extract_job_id."
    )


def get_export_job_status(client, job_id, **overrides):
    """Polls one export job."""
    fields = {"JobCode": job_id}      # UNVERIFIED -- must match create's id field
    fields.update(overrides)
    return call(client, OPS["export_status"], **fields)


def wait_for_export(client, job_id, timeout=EXPORT_TIMEOUT_SECONDS,
                    interval=EXPORT_POLL_SECONDS):
    """
    Polls until the export job finishes, fails, or the timeout expires.

    Terminal-state names are UNVERIFIED; anything unrecognised keeps polling
    rather than being treated as success, so a schema mismatch surfaces as a
    timeout instead of a silently empty result.
    """
    done = {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCESSFUL"}   # UNVERIFIED
    failed = {"FAILED", "ERROR", "CANCELLED"}                   # UNVERIFIED
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        response = get_export_job_status(client, job_id)
        status = str(getattr(response, "Status", "") or "").upper()
        print(f"  export {job_id}: {status or '<no Status field>'}", file=sys.stderr)
        if status in done:
            return response
        if status in failed:
            raise RuntimeError(f"Export job {job_id} ended in state {status}")
        time.sleep(interval)

    raise TimeoutError(
        f"Export job {job_id} did not finish within {timeout}s. If it kept "
        f"reporting an unrecognised status, add that value to wait_for_export."
    )


def export_sale_orders(client, days=1, start=None, end=None, **overrides):
    """Full export flow: create the job, poll it, return the finished status."""
    created = create_export_job(client, days=days, start=start, end=end, **overrides)
    job_id = extract_job_id(created)
    print(f"  export job created: {job_id}", file=sys.stderr)
    return wait_for_export(client, job_id)


# --- Scope guards -----------------------------------------------------------

def guard_window(days, full, command):
    """Keeps accidental full-range pulls behind an explicit flag."""
    if days > NARROW_MAX_DAYS and not full:
        sys.exit(
            f"Refusing a {days}-day window without --full.\n"
            f"Test a narrow slice first, e.g.:\n"
            f"  python3 unicommerce_connect.py {command} --days 1\n"
            f"Then re-run with --full once the fields and output look right."
        )


# --- Entry point --------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Unicommerce inventory and sales pulls (no PO integration)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("operations", help="list every operation on the WSDL")
    sub.add_parser("describe", help="request signatures for the ops we call")

    p_inv = sub.add_parser("inventory", help="inventory per SKU per facility")
    p_inv.add_argument("--sku", action="append", dest="skus",
                       help="repeatable; narrow slice for a first test")
    p_inv.add_argument("--facility")
    p_inv.add_argument("--all", action="store_true",
                       help="whole catalogue (required if no --sku given)")
    p_inv.add_argument("--snapshot", action="store_true",
                       help="force GetInventorySnapshot instead of the bulk op")

    p_so = sub.add_parser("sale-orders", help="order headers only (no line items)")
    p_so.add_argument("--days", type=int, default=1)
    p_so.add_argument("--full", action="store_true")

    p_ex = sub.add_parser("export", help="bulk sale-order export, with line items")
    p_ex.add_argument("--days", type=int, default=1)
    p_ex.add_argument("--full", action="store_true")

    args = parser.parse_args()

    if args.command == "inventory" and not args.skus and not args.all:
        sys.exit(
            "Refusing a full-catalogue pull by default.\n"
            "Test a narrow slice first, e.g.:\n"
            "  python3 unicommerce_connect.py inventory --sku ABC123\n"
            "Then re-run with --all once the output looks right."
        )
    if args.command in ("sale-orders", "export"):
        guard_window(args.days, args.full, args.command)

    require_credentials()
    print(f"Connecting to Unicommerce ({ENV})...", file=sys.stderr)
    client = get_client()
    print("Connected.", file=sys.stderr)

    if args.command == "operations":
        list_operations(client)
    elif args.command == "describe":
        describe_operations(client)
    elif args.command == "inventory":
        bulk = False if args.snapshot else (None if args.skus else True)
        print(get_inventory(client, skus=args.skus, facility=args.facility,
                            bulk=bulk))
    elif args.command == "sale-orders":
        print(get_sale_orders(client, days=args.days))
    elif args.command == "export":
        print(export_sale_orders(client, days=args.days))


if __name__ == "__main__":
    main()
