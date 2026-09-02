"""
Unicommerce (Uniware) SOAP API connection script.

Run this in an environment with real network access (a server, your own
machine). It will NOT work from the Claude Code cloud sandbox -- both
unicommerce.com hosts sit outside that environment's egress allowlist and
the proxy answers 403 to CONNECT.

SETUP:
  pip install -r requirements.txt

  Store credentials as environment variables -- never hardcode them here:
    export UNICOMMERCE_USERNAME="claude"          # or "Sandbox" for sandbox
    export UNICOMMERCE_API_KEY="<the key from the admin panel>"
    export UNICOMMERCE_ENV="production"            # or "sandbox"

USAGE:
  python3 unicommerce_connect.py operations   # every operation on the WSDL
  python3 unicommerce_connect.py describe     # request signatures, ALL fields
  python3 unicommerce_connect.py inventory
  python3 unicommerce_connect.py sale-orders --days 90
  python3 unicommerce_connect.py purchase-orders
  python3 unicommerce_connect.py po UL-MUM/13-14/53

RUN `describe` FIRST.
  The operation NAMES below are confirmed against the real v1.9 WSDL. The
  request FIELD names inside each pull function are not -- they are marked
  `# UNVERIFIED` and are collected at the top of each function so there is
  exactly one place to correct per operation. `describe` prints the true
  signature for each operation; reconcile the two before trusting any
  numbers these functions return.

Endpoints (from the account's API panel):
  Sandbox:    https://staging.unicommerce.com/services/soap/?version=1.5
              WSDL: https://staging.unicommerce.com/services/soap/uniware15.wsdl
  Production: https://styxxinternational.unicommerce.com/services/soap/?version=1.9
              WSDL: https://styxxinternational.unicommerce.com/services/soap/uniware19.wsdl
"""

import argparse
import datetime as dt
import difflib
import os
import sys

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

# Operation names confirmed present on the tenant's v1.9 WSDL.
# GetSaleOrderDetails does NOT exist -- SearchSaleOrder is the date-range read.
OPS = {
    "inventory_bulk": "GetBulkItemTypeInventory",
    "inventory_snapshot": "GetInventorySnapshot",
    "sale_orders": "SearchSaleOrder",
    "purchase_orders": "GetPurchaseOrders",
    "purchase_order_detail": "GetPurchaseOrderDetail",
}


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
    expect the digest form despite the docs example. This has not been
    confirmed against the live service.
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
    """Prints every operation available on this WSDL."""
    operations = _binding_operations(client)
    print(f"\n=== Operations available on {WSDL_URLS[ENV]} ===\n")
    for name in sorted(operations):
        print(f"  {name}")
    print(f"\n=== {len(operations)} operations total ===\n")


def describe_operations(client, names=None):
    """
    Prints the exact request signature of each operation we call.

    This is the step that resolves the UNVERIFIED field names in the pull
    functions: SOAP requests must match the WSDL schema exactly, so the
    field names have to come from the WSDL rather than from the docs.
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
    print()


def call(client, op_name, **fields):
    """Invokes a WSDL operation by name, dropping unset (None) fields."""
    operation = getattr(client.service, op_name)
    return operation(**{k: v for k, v in fields.items() if v is not None})


# --- Pull functions ---------------------------------------------------------
#
# Each collects its request field names in one dict so that reconciling them
# against `describe` output is a single edit. Pass **overrides to substitute
# corrected names without editing the file.

def get_inventory_snapshot(client, skus=None, facility=None, bulk=True, **overrides):
    """
    Current inventory across all SKUs.

    Uses GetBulkItemTypeInventory by default (bulk=True) since the restock
    pipeline wants the whole catalogue in one call; GetInventorySnapshot is
    the per-SKU alternative. Both names are confirmed on the WSDL.
    """
    op = OPS["inventory_bulk"] if bulk else OPS["inventory_snapshot"]
    fields = {
        "ItemTypeSKU": skus,          # UNVERIFIED -- may be ItemSKU / SKUList
        "FacilityCode": facility,     # UNVERIFIED
    }
    fields.update(overrides)
    return call(client, op, **fields)


def get_sale_orders(client, days=90, start=None, end=None, status=None, **overrides):
    """
    Sale orders in a date range -- the 90-day size-wise sales figure.

    SearchSaleOrder is the correct operation; GetSaleOrderDetails does not
    exist on this WSDL. Defaults to the trailing `days` window.
    """
    end = end or dt.datetime.now(dt.timezone.utc)
    start = start or (end - dt.timedelta(days=days))
    fields = {
        # UNVERIFIED -- the range may be flat From/To fields or a nested
        # complex type; `describe` output settles which.
        "CreatedFrom": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "CreatedTo": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Status": status,
    }
    fields.update(overrides)
    return call(client, OPS["sale_orders"], **fields)


def get_purchase_orders(client, status="OPEN", facility=None, **overrides):
    """Lists purchase orders -- the current-open-PO figure."""
    fields = {
        "Status": status,             # UNVERIFIED -- enum casing unconfirmed
        "FacilityCode": facility,     # UNVERIFIED
    }
    fields.update(overrides)
    return call(client, OPS["purchase_orders"], **fields)


def get_purchase_order_detail(client, po_code):
    """
    Looks up a single PO by code.

    The one operation with a documented request shape:
        GetPurchaseOrderDetailRequest / GetPurchaseOrderDetailResponse
        <PurchaseOrderCode>UL-MUM/13-14/53</PurchaseOrderCode>
    """
    return call(client, OPS["purchase_order_detail"], PurchaseOrderCode=po_code)


# --- Entry point --------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("operations", help="list every operation on the WSDL")
    sub.add_parser("describe", help="print request signatures for the ops we call")
    sub.add_parser("inventory", help="current inventory across all SKUs")
    p_so = sub.add_parser("sale-orders", help="sale orders in a trailing window")
    p_so.add_argument("--days", type=int, default=90)
    sub.add_parser("purchase-orders", help="list open purchase orders")
    p_po = sub.add_parser("po", help="one purchase order by code")
    p_po.add_argument("code")
    args = parser.parse_args()

    require_credentials()
    print(f"Connecting to Unicommerce ({ENV})...", file=sys.stderr)
    client = get_client()
    print("Connected.", file=sys.stderr)

    if args.command == "operations":
        list_operations(client)
    elif args.command == "describe":
        describe_operations(client)
    elif args.command == "inventory":
        print(get_inventory_snapshot(client))
    elif args.command == "sale-orders":
        print(get_sale_orders(client, days=args.days))
    elif args.command == "purchase-orders":
        print(get_purchase_orders(client))
    elif args.command == "po":
        print(get_purchase_order_detail(client, args.code))


if __name__ == "__main__":
    main()
