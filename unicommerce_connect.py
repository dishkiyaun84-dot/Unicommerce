"""
Unicommerce (Uniware) SOAP API connection script.

Run this in an environment with real network access (Claude Code, a server,
your own machine) -- it will NOT work from within a claude.ai chat sandbox.

SETUP (do this before running):
  pip install zeep

  Store credentials as environment variables -- never hardcode them here:
    export UNICOMMERCE_USERNAME="claude"          # or "Sandbox" for sandbox
    export UNICOMMERCE_API_KEY="<the key from the admin panel>"
    export UNICOMMERCE_ENV="sandbox"               # or "production"

WHAT THIS SCRIPT DOES, IN ORDER:
  1. Connects and authenticates via WS-Security (UsernameToken).
  2. Introspects the WSDL and prints every available operation. Do this
     FIRST and read the output -- the generic Unicommerce docs don't give
     exact operation/field names for your tenant's v1.9 WSDL, so the
     pull functions below use best-guess operation names based on the
     public support-portal docs and MUST be corrected against the real
     printed list before they'll work.
  3. Provides three scaffolded pull functions (inventory, sale orders,
     purchase orders) matching what our restock pipeline needs. Fix the
     operation name and request fields in each based on step 2's output,
     then they're ready to feed real data into the same calculation logic
     we've been running by hand.

Endpoints (from your account's API panel):
  Sandbox:    https://staging.unicommerce.com/services/soap/?version=1.5
              WSDL: https://staging.unicommerce.com/services/soap/uniware15.wsdl
  Production: https://styxxinternational.unicommerce.com/services/soap/?version=1.9
              WSDL: https://styxxinternational.unicommerce.com/services/soap/uniware19.wsdl
"""

import os
import sys
from zeep import Client
from zeep.wsse.username import UsernameToken

# --- Configuration --------------------------------------------------------

ENV = os.environ.get("UNICOMMERCE_ENV", "sandbox").lower()
USERNAME = os.environ.get("UNICOMMERCE_USERNAME")
API_KEY = os.environ.get("UNICOMMERCE_API_KEY")

if not USERNAME or not API_KEY:
    sys.exit(
        "Missing credentials. Set UNICOMMERCE_USERNAME and UNICOMMERCE_API_KEY "
        "as environment variables before running this script."
    )

WSDL_URLS = {
    "sandbox": "https://staging.unicommerce.com/services/soap/uniware15.wsdl",
    "production": "https://styxxinternational.unicommerce.com/services/soap/uniware19.wsdl",
}

if ENV not in WSDL_URLS:
    sys.exit(f"UNICOMMERCE_ENV must be 'sandbox' or 'production', got '{ENV}'")

WSDL_URL = WSDL_URLS[ENV]


# --- Connection -------------------------------------------------------------

def get_client():
    """
    Builds an authenticated SOAP client.

    Unicommerce's docs show PasswordText (not PasswordDigest) in their
    example WS-Security header, so use_digest=False. If auth fails with
    a security/nonce error, try use_digest=True instead -- some Uniware
    versions expect the digest form despite the docs example.
    """
    token = UsernameToken(USERNAME, API_KEY, use_digest=False)
    client = Client(WSDL_URL, wsse=token)
    return client


def list_operations(client):
    """
    Step 1: run this and read the output before touching anything else.
    Prints every operation available on this WSDL so we can confirm exact
    names instead of guessing from the generic docs.
    """
    print(f"\n=== Operations available on {WSDL_URL} ===\n")
    for service in client.wsdl.services.values():
        for port in service.ports.values():
            operations = sorted(port.binding._operations.keys())
            for op in operations:
                print(f"  {op}")
    print(f"\n=== {sum(len(p.binding._operations) for s in client.wsdl.services.values() for p in s.ports.values())} operations total ===\n")


# --- Pull functions (fix operation names against list_operations() output) --

def get_inventory_snapshot(client, item_type_sku=None):
    """
    Pulls current inventory. Likely operation name based on the support
    docs' mention of an "inventory snapshot" API -- CONFIRM the exact name
    via list_operations() first, this is a best guess.
    """
    # e.g. result = client.service.GetInventorySnapshot(ItemTypeSKU=item_type_sku)
    raise NotImplementedError(
        "Confirm the exact operation name from list_operations() output, "
        "then replace this with the real call."
    )


def get_sale_order_details(client, order_code=None, created_date_range=None):
    """
    Pulls sale order data (for our 90-day size-wise sales figure).
    Docs reference "GetSaleOrderDetails" -- confirm exact name/casing.
    created_date_range should be a dict like:
        {"Start": "2026-06-01T00:00:00Z", "End": "2026-08-30T00:00:00Z"}
    """
    # e.g. result = client.service.GetSaleOrderDetails(
    #     SaleOrderCode=order_code, CreatedDateRange=created_date_range
    # )
    raise NotImplementedError(
        "Confirm the exact operation name from list_operations() output, "
        "then replace this with the real call."
    )


def get_purchase_order_detail(client, po_code):
    """
    Pulls a specific open PO (for our current-open-PO figure).
    Docs confirm the exact request/response shape for this one:
        GetPurchaseOrderDetailRequest / GetPurchaseOrderDetailResponse
        <PurchaseOrderCode>UL-MUM/13-14/53</PurchaseOrderCode>
    This is the one operation we have real documented confidence in.
    """
    result = client.service.GetPurchaseOrderDetail(PurchaseOrderCode=po_code)
    return result


# --- Entry point --------------------------------------------------------

if __name__ == "__main__":
    print(f"Connecting to Unicommerce ({ENV})...")
    client = get_client()
    print("Connected. Listing available operations:")
    list_operations(client)
    print(
        "\nNext step: match the pull functions above against this operation "
        "list, fix any names that don't match, then wire them into the "
        "restock pipeline (75-day demand, size-wise sales, inventory, open POs)."
    )
