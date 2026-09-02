"""
Unicommerce (Uniware) SOAP API pulls for the restock pipeline.

Run this from a machine with real network access.

SCOPE -- two pulls only:
  1. Inventory, per SKU per facility  (GetBulkItemTypeInventory)
  2. Sales, with line items           (CreateExportJob -> GetExportJobStatus)

Purchase orders are deliberately NOT integrated. POs are not created in
Unicommerce on this account's plan, so GetPurchaseOrders,
GetPurchaseOrderDetail and the OpenPurchase field all return unreliable
numbers. Open-PO quantities come from a manually maintained file outside
Unicommerce.

WHY GetBulkItemTypeInventory AND NOT GetInventorySnapshot
  Settled by the real WSDL signatures. GetInventorySnapshot returns flat
  per-SKU totals with NO facility dimension, and it is the operation that
  carries OpenPurchase. GetBulkItemTypeInventory nests
  Facilities/FacilityInventory {FacilityCode, FacilityName, Inventory}
  under each SKU and carries no PO-derived field. Only the latter can
  answer per-SKU-per-facility, so it is the one wired up.

SETUP:
  pip install -r requirements.txt
    export UNICOMMERCE_USERNAME="claude"
    export UNICOMMERCE_API_KEY="<the key from the admin panel>"
    export UNICOMMERCE_ENV="production"

USAGE -- narrow slices are the default; full runs need an explicit flag:
  python3 unicommerce_connect.py describe
  python3 unicommerce_connect.py inventory --sku ABC123
  python3 unicommerce_connect.py inventory --all
  python3 unicommerce_connect.py sale-orders --days 1
  python3 unicommerce_connect.py export --job-type "<name>" --days 1
  python3 unicommerce_connect.py export --job JOB-77          # resume

REQUEST SIGNATURES ARE CONFIRMED against the tenant's v1.9 WSDL for all
five operations. Two runtime VALUES are still unknown, because the WSDL
types them as plain strings and their vocabularies live in the Uniware
config rather than the schema:
  - ExportJobTypeName  -- must be passed with --job-type; take it from the
    export screen in the Uniware admin UI.
  - the ExportFilter `id` naming the date-range filter (--date-filter-id,
    default "created", almost certainly wrong for some tenants).
Both are called out at runtime rather than silently assumed. See
`validate_export_slice` for why a wrong filter id is the dangerous case.

Endpoints:
  Sandbox:    WSDL https://staging.unicommerce.com/services/soap/uniware15.wsdl
  Production: WSDL https://styxxinternational.unicommerce.com/services/soap/uniware19.wsdl
"""

import argparse
import datetime as dt
import difflib
import os
import sys
import time

from requests import Session
from zeep import Client
from zeep.exceptions import ValidationError
from zeep.helpers import serialize_object
from zeep.transports import Transport
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
    "inventory": "GetBulkItemTypeInventory",
    # Listed so `describe` still prints it, but deliberately unused: no
    # facility dimension, and it carries OpenPurchase.
    "inventory_snapshot": "GetInventorySnapshot",
    "sale_orders": "SearchSaleOrder",
    "export_create": "CreateExportJob",
    "export_status": "GetExportJobStatus",
}

# Confirmed present on GetInventorySnapshot responses; meaningless on this
# account. Never read these -- open-PO quantities come from the manual file.
IGNORED_RESPONSE_FIELDS = ("OpenPurchase", "OpenPurchaseQuantity", "PendingPO")

# The account's only facility, type WAREHOUSE, read from the Uniware admin UI
# (Settings -> Facilities). The WSDL exposes no way to list facilities, and
# GetBulkItemTypeInventory requires at least one, so this is the default.
# Override with --facility if a second facility is ever added.
DEFAULT_FACILITY = "styxxinternational"

NARROW_MAX_DAYS = 7
SKU_CHUNK_SIZE = 100
PAGE_SIZE = 100
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


def utcnow():
    # Microseconds are valid xsd:dateTime but some SOAP stacks reject them.
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def window(days, start=None, end=None):
    """Resolves a trailing-day window into (start, end) datetimes."""
    end = end or utcnow()
    return (start or end - dt.timedelta(days=days)), end


# --- Connection -------------------------------------------------------------

def get_client(facility=None):
    """
    Builds an authenticated SOAP client.

    PasswordText (use_digest=False) per Unicommerce's documented example
    header. Confirmed working against production -- `describe` connects.

    Uniware scopes facility-bound operations by a `Facility` HTTP header.
    Without it, GetBulkItemTypeInventory rejects even a facility code that
    exists and is enabled, with INVALID_FACILITY_CODE. The header is sent
    whenever a facility is known; it is ignored by operations that do not
    need it, so this is safe to send generally.
    """
    session = Session()
    if facility:
        session.headers["Facility"] = facility
    token = UsernameToken(USERNAME, API_KEY, use_digest=False)
    return Client(WSDL_URLS[ENV], wsse=token, transport=Transport(session=session))


def set_facility_header(client, facility):
    """Repoints an existing client at another facility, without refetching."""
    headers = client.transport.session.headers
    if facility:
        headers["Facility"] = facility
    else:
        headers.pop("Facility", None)
    return client


def _binding_operations(client):
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
    """Prints the exact request/response signature of each operation."""
    operations = _binding_operations(client)
    for name in names or list(OPS.values()):
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


def _occurs(element):
    maximum = element.max_occurs
    maximum = "n" if maximum in ("unbounded", None) or maximum == float("inf") else maximum
    return f"[{element.min_occurs}..{maximum}]"


def dump_element(name, element, depth=0, max_depth=4):
    """Prints one request element with its minOccurs/maxOccurs, recursively."""
    required = "REQUIRED" if (element.min_occurs or 0) >= 1 else "optional"
    type_name = getattr(element.type, "name", None) or type(element.type).__name__
    print(f"{'  ' * (depth + 1)}{name} {_occurs(element)} {required} : {type_name}")
    if depth < max_depth:
        for sub_name, sub_element in getattr(element.type, "elements", None) or []:
            dump_element(sub_name, sub_element, depth + 1, max_depth)


def show_schema(client, names=None):
    """
    Prints the REQUIRED/optional structure of each request.

    `describe` renders signature(), which does NOT show minOccurs -- so an
    element that looks optional there may be mandatory, and a wrapper that
    accepts an empty list may in fact require at least one entry. Both cost
    a failed round-trip to discover. This reads the same WSDL locally and
    prints the constraints outright.
    """
    operations = _binding_operations(client)
    for name in names or list(OPS.values()):
        print(f"\n{'=' * 70}\n{name} -- request structure\n{'=' * 70}")
        operation = operations.get(name)
        if operation is None:
            print("  !! NOT FOUND on this WSDL.")
            continue
        body = getattr(operation.input, "body", None)
        elements = getattr(getattr(body, "type", None), "elements", None)
        if not elements:
            print("  <no structured request body>")
            continue
        for element_name, element in elements:
            dump_element(element_name, element)
    print()


FACILITY_KEYWORDS = ("facilit", "warehouse", "location")
READ_PREFIXES = ("get", "search", "list", "fetch")


def find_facilities(client):
    """
    Looks for an operation that LISTS facility codes.

    On this tenant there is none: the facility operations are all writes
    (CreateFacility, EditFacility, CreateOrEditFacilityItemType) plus
    SwitchSaleOrderItemFacility. Codes therefore have to come from the
    Uniware admin UI rather than the API.
    """
    operations = _binding_operations(client)
    matches = sorted({op for keyword in FACILITY_KEYWORDS
                      for op in operations if keyword in op.lower()})
    readers = [op for op in matches
               if op.lower().startswith(READ_PREFIXES)]

    if readers:
        print("\nOperations that may list facilities:\n")
        describe_operations(client, readers)
        return readers

    print("\nNo facility-LISTING operation on this WSDL.")
    if matches:
        print("Facility-related operations are all writes or transfers:")
        for op in matches:
            print(f"  {op}")
    print(
        "\nGet the facility code from the Uniware admin UI instead -- the\n"
        "facility selector in the top bar, or Settings -> Facilities. It is\n"
        "the short code, not the display name. Then:\n"
        "  python3 unicommerce_connect.py inventory --sku SKU --facility CODE\n"
    )
    return []


def try_facilities(client, candidates, sku=None):
    """
    Probes candidate facility codes and reports which the service accepts.

    The UI's displayed code is not necessarily the one the API wants, and
    there is no listing operation, so the code has to be found by trial.
    Each probe is a single read-only inventory call.
    """
    seen, ordered = set(), []
    for candidate in candidates:
        for variant in (candidate, candidate.lower(), candidate.upper()):
            if variant not in seen:
                seen.add(variant)
                ordered.append(variant)

    print(f"\nProbing {len(ordered)} candidate facility code(s):\n")
    accepted = []
    for code in ordered:
        try:
            set_facility_header(client, code)
            get_inventory(client, skus=[sku] if sku else None, facilities=[code])
        except RuntimeError as exc:
            detail = str(exc)
            if "INVALID_FACILITY_CODE" in detail:
                print(f"  {code:<30} invalid")
            else:
                print(f"  {code:<30} {detail.splitlines()[0]}")
            continue
        print(f"  {code:<30} ACCEPTED")
        accepted.append(code)

    if accepted:
        print(f"\nUse: --facility {accepted[0]}")
        print(f"Set DEFAULT_FACILITY to it to make that the default.\n")
    else:
        print(
            "\nNone accepted. If the code is confirmed correct on the facility's\n"
            "detail page (Settings -> Facilities -> click through; the Code field\n"
            "is greyed out and immutable), then the code is not the problem and\n"
            "this is a permissions issue: the API user must be granted access to\n"
            "the facility. Check Settings -> Users -> the API user -> facility\n"
            "access in the Uniware admin UI.\n"
        )
    return accepted


def find_operations(client, keyword):
    """Lists operations whose name contains a keyword, with signatures."""
    matches = sorted(o for o in _binding_operations(client)
                     if keyword.lower() in o.lower())
    if not matches:
        print(f"No operation name contains '{keyword}'. Run `operations` for the "
              f"full list.")
        return matches
    print(f"\nOperations matching '{keyword}':\n")
    describe_operations(client, matches)
    return matches


def call(client, op_name, **fields):
    """
    Invokes a WSDL operation by name, dropping unset (None) fields.

    Turns zeep's schema ValidationError into one actionable line. These are
    almost always a REQUIRED element we did not send: signature() does not
    print minOccurs, so an element that looks optional in `describe` output
    may not be. The fix is normally to send the wrapper with an empty inner
    list rather than omitting it -- see get_inventory.
    """
    operation = getattr(client.service, op_name)
    try:
        return operation(**{k: v for k, v in fields.items() if v is not None})
    except ValidationError as exc:
        raise RuntimeError(
            f"{op_name} failed schema validation: {exc}\n"
            f"  Sent: {sorted(k for k, v in fields.items() if v is not None)}\n"
            f"  A 'Missing element X' here means X is required even though "
            f"`describe` shows no minOccurs. Send X as an empty wrapper "
            f"(e.g. {{'Item': []}}) rather than omitting it."
        ) from exc


# --- Shared response handling ----------------------------------------------

def _error_text(entry):
    """
    Renders one Error/Warning entry.

    Uniware returns these with LOWERCASE field names (code, message,
    description), so checking only the capitalised forms falls through to
    str(entry) and dumps the raw object.
    """
    code = getattr(entry, "code", None) or getattr(entry, "Code", None)
    message = (getattr(entry, "message", None) or getattr(entry, "Message", None))
    description = (getattr(entry, "description", None)
                   or getattr(entry, "Description", None))
    parts = [p for p in (message, description) if p]
    if not parts:
        return str(entry)
    text = ": ".join(str(p) for p in parts)
    return f"[{code}] {text}" if code else text


def collect_messages(response, container, item):
    """Flattens a Uniware {Errors: {Error: [...]}} wrapper into strings."""
    block = getattr(response, container, None)
    entries = getattr(block, item, None) if block is not None else None
    return [_error_text(entry) for entry in entries or []]


def check_response(response, what):
    """
    Raises unless the call reported Successful, and prints any warnings.

    Every one of the five operations wraps its payload in the same
    Successful/Errors/Warnings envelope, so this is applied uniformly.
    """
    for warning in collect_messages(response, "Warnings", "Warning"):
        print(f"  warning: {warning}", file=sys.stderr)
    if getattr(response, "Successful", None) is False:
        errors = collect_messages(response, "Errors", "Error")
        raise RuntimeError(
            f"{what} returned Successful=False: "
            + ("; ".join(errors) if errors else "no Errors given")
        )
    return response


def _listed(parent, container, item):
    """Reads a {Container: {Item: [...]}} wrapper, tolerating absence."""
    block = getattr(parent, container, None)
    return list(getattr(block, item, None) or []) if block is not None else []


# --- Pull 1: inventory ------------------------------------------------------

def get_inventory(client, skus=None, facilities=None, **overrides):
    """
    Inventory per SKU per facility, via GetBulkItemTypeInventory.

    Signature CONFIRMED:
      REQUEST : SkuCodes: {SkuCode: xsd:string[]},
                FacilityCodes: {FacilityCode: xsd:string[]}
      RESPONSE: ..., InventoryDetails: {ItemInventory: {ItemSKU, ItemTypeName,
                ImageUrl, ProductPageUrl, MaxRetailPrice,
                Facilities: {FacilityInventory: {FacilityCode, FacilityName,
                Inventory: xsd:int}[]}}[]}

    Both wrappers are REQUIRED, and FacilityCodes requires AT LEAST ONE
    FacilityCode -- an empty <FacilityCodes/> is rejected:

      Expected at least 1 items (minOccurs check) 0 items found.
      (GetBulkItemTypeInventoryRequest.FacilityCodes.FacilityCode)

    So there is no "all facilities" request; facilities must be enumerated.
    There is no operation to list facilities, so the code comes from the
    Uniware admin UI; DEFAULT_FACILITY holds this account's only one.
    """
    facilities = list(facilities) if facilities else [DEFAULT_FACILITY]
    fields = {
        "SkuCodes": {"SkuCode": list(skus) if skus else []},
        "FacilityCodes": {"FacilityCode": facilities},
    }
    fields.update(overrides)
    response = call(client, OPS["inventory"], **fields)
    return check_response(response, "GetBulkItemTypeInventory")


def flatten_inventory(response):
    """
    Turns the nested response into flat per-SKU-per-facility rows.

    This is the shape the restock pipeline wants. Any PO-derived field is
    dropped by construction -- only the fields named here are read.
    """
    rows = []
    for item in _listed(response, "InventoryDetails", "ItemInventory"):
        for facility in _listed(item, "Facilities", "FacilityInventory"):
            rows.append({
                "sku": getattr(item, "ItemSKU", None),
                "name": getattr(item, "ItemTypeName", None),
                "mrp": getattr(item, "MaxRetailPrice", None),
                "facility_code": getattr(facility, "FacilityCode", None),
                "facility_name": getattr(facility, "FacilityName", None),
                "inventory": getattr(facility, "Inventory", None),
            })
    return rows


def get_inventory_rows(client, skus=None, facilities=None, chunk=SKU_CHUNK_SIZE):
    """
    Fetches inventory, chunking large SKU lists into several calls.

    Defaults to DEFAULT_FACILITY when none is given -- see get_inventory.
    """
    if not skus:
        return flatten_inventory(get_inventory(client, facilities=facilities))

    skus = list(skus)
    rows = []
    for i in range(0, len(skus), chunk):
        batch = skus[i:i + chunk]
        print(f"  inventory: SKUs {i + 1}-{i + len(batch)} of {len(skus)}",
              file=sys.stderr)
        rows.extend(flatten_inventory(
            get_inventory(client, skus=batch, facilities=facilities)))
    return rows


# --- Pull 2a: sale order headers (validation probe) -------------------------

def search_sale_orders(client, days=1, start=None, end=None, status=None,
                       page_size=PAGE_SIZE, max_pages=None, **overrides):
    """
    Sale order HEADERS in a date range, via SearchSaleOrder.

    Signature CONFIRMED: the date fields are FromDate/ToDate (not the
    CreatedFrom/CreatedTo this script previously guessed), and results are
    paginated through SearchOptions {DisplayStart, DisplayLength} against a
    TotalRecords count.

    The response carries no line items -- Code, Status, Channel, CreatedOn
    and friends only. So this is the cheap probe for order VOLUME in a
    window, used to sanity-check the export, not the size-wise source.

    Returns (orders, total_records).
    """
    start, end = window(days, start, end)
    orders, total, page = [], None, 0

    while True:
        fields = {
            "FromDate": start,
            "ToDate": end,
            "Status": status,
            "SearchOptions": {"DisplayStart": len(orders),
                              "DisplayLength": page_size},
        }
        fields.update(overrides)
        response = check_response(
            call(client, OPS["sale_orders"], **fields), "SearchSaleOrder")

        if total is None:
            total = getattr(response, "TotalRecords", None)
        batch = _listed(response, "SaleOrders", "SaleOrder")
        orders.extend(batch)
        page += 1

        if not batch or (total is not None and len(orders) >= total):
            break
        if max_pages and page >= max_pages:
            print(f"  stopping after {max_pages} pages ({len(orders)} of {total})",
                  file=sys.stderr)
            break

    return orders, total


# --- Pull 2b: bulk export with line items -----------------------------------

def create_export_job(client, job_type, days=1, start=None, end=None,
                      date_filter_id="created", columns=None, email=None,
                      frequency="ONCE", **overrides):
    """
    Starts an async bulk export.

    Signature CONFIRMED:
      REQUEST : ExportJobTypeName: xsd:string,
                ExportColumns: {ExportColumn: xsd:string[]},
                ExportFilters: {ExportFilter: {SelectedValues, Text,
                  SelectedValue, DateTime, DateRange: {Start, End},
                  Checked, id}[]},
                ScheduleTime, NotificationEmail, Frequency
      RESPONSE: ..., JobCode: xsd:string

    `schema` reports Frequency as [1..1] REQUIRED, so it is always sent --
    leaving it unset fails validation before the request goes out.
    ScheduleTime stays unset so the job runs immediately.

    THREE values here are not derivable from the WSDL, all plain strings
    whose vocabularies live in the Uniware config:
      job_type       -- required, from the Uniware export screen
      date_filter_id -- which filter the date range applies to
      frequency      -- "ONCE" is a guess; override with --frequency if the
                        service rejects it
    """
    start, end = window(days, start, end)
    fields = {
        "ExportJobTypeName": job_type,
        "ExportColumns": {"ExportColumn": list(columns)} if columns else None,
        "ExportFilters": {"ExportFilter": [{
            "id": date_filter_id,
            "DateRange": {"Start": start, "End": end},
        }]},
        "NotificationEmail": email,
        "Frequency": frequency,
    }
    fields.update(overrides)
    response = call(client, OPS["export_create"], **fields)
    return check_response(response, "CreateExportJob")


def get_export_job_status(client, job_id, **overrides):
    """
    Polls one export job.

    Signature CONFIRMED:
      REQUEST : JobCode: xsd:string
      RESPONSE: ..., Status: xsd:string, FilePath: xsd:string
    """
    fields = {"JobCode": job_id}
    fields.update(overrides)
    return call(client, OPS["export_status"], **fields)


def wait_for_export(client, job_id, timeout=EXPORT_TIMEOUT_SECONDS,
                    interval=EXPORT_POLL_SECONDS):
    """
    Polls until the export job finishes, fails, or the timeout expires.

    The Status VALUES are still unverified -- the WSDL types Status as a
    plain xsd:string, so the vocabulary only shows up at runtime. Anything
    unrecognised keeps polling rather than being read as success, so a
    mismatch surfaces as a timeout instead of a silently empty result.
    """
    done = {"COMPLETE", "COMPLETED", "SUCCESS", "SUCCESSFUL"}   # UNVERIFIED values
    failed = {"FAILED", "ERROR", "CANCELLED"}                   # UNVERIFIED values
    deadline = time.monotonic() + timeout
    seen = set()

    while time.monotonic() < deadline:
        response = get_export_job_status(client, job_id)
        if getattr(response, "Successful", None) is False:
            errors = collect_messages(response, "Errors", "Error")
            raise RuntimeError(
                f"GetExportJobStatus({job_id}) returned Successful=False: "
                + ("; ".join(errors) if errors else "no Errors given"))

        for warning in collect_messages(response, "Warnings", "Warning"):
            if warning not in seen:
                seen.add(warning)
                print(f"  warning: {warning}", file=sys.stderr)

        status = str(getattr(response, "Status", "") or "").upper()
        print(f"  export {job_id}: {status or '<no Status field>'}", file=sys.stderr)
        if status in done:
            return response
        if status in failed:
            errors = collect_messages(response, "Errors", "Error")
            raise RuntimeError(
                f"Export job {job_id} ended in state {status}"
                + (f": {'; '.join(errors)}" if errors else ""))
        time.sleep(interval)

    raise TimeoutError(
        f"Export job {job_id} did not finish within {timeout}s. If it kept "
        f"reporting an unrecognised status, add that value to wait_for_export.")


def export_sale_orders(client, job_type=None, days=1, start=None, end=None,
                       job_id=None, date_filter_id="created", email=None,
                       frequency="ONCE", columns=None, **overrides):
    """
    Full export flow: create the job, poll it, report the file path.

    Pass job_id to resume polling an export already in flight rather than
    creating a second one.
    """
    if job_id is None:
        if not job_type:
            raise ValueError("job_type is required to create an export job")
        created = create_export_job(client, job_type, days=days, start=start,
                                    end=end, date_filter_id=date_filter_id,
                                    email=email, frequency=frequency,
                                    columns=columns, **overrides)
        job_id = getattr(created, "JobCode", None)
        if not job_id:
            raise RuntimeError(
                "CreateExportJob reported success but returned no JobCode.")
        print(f"  export job created: {job_id}", file=sys.stderr)

    finished = wait_for_export(client, job_id)
    file_path = getattr(finished, "FilePath", None)
    if file_path:
        print(f"  export file: {file_path}", file=sys.stderr)
    else:
        print("  !! job finished with no FilePath -- nothing to read",
              file=sys.stderr)
    return finished


def validate_export_slice(client, days, start=None, end=None):
    """
    Counts orders in the window via SearchSaleOrder before exporting.

    A wrong date_filter_id is the dangerous failure: the filter may be
    ignored rather than rejected, and the export then silently covers the
    entire order history instead of the requested slice. Comparing this
    count against the export's row count catches that.
    """
    start, end = window(days, start, end)
    _, total = search_sale_orders(client, start=start, end=end, max_pages=1)
    print(f"  SearchSaleOrder reports {total} orders between "
          f"{start:%Y-%m-%d %H:%M} and {end:%Y-%m-%d %H:%M} UTC", file=sys.stderr)
    print("  the export should contain line items for roughly that many orders; "
          "far more means the date filter was ignored", file=sys.stderr)
    return total


# --- Scope guards -----------------------------------------------------------

def guard_window(days, full, command):
    if days > NARROW_MAX_DAYS and not full:
        sys.exit(
            f"Refusing a {days}-day window without --full.\n"
            f"Test a narrow slice first, e.g.:\n"
            f"  python3 unicommerce_connect.py {command} --days 1\n"
            f"Then re-run with --full once the fields and output look right.")


# --- Entry point --------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Unicommerce inventory and sales pulls (no PO integration).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("operations", help="list every operation on the WSDL")
    sub.add_parser("describe", help="request signatures for the ops we call")
    sub.add_parser("schema", help="which request elements are REQUIRED "
                                  "(minOccurs, which describe cannot show)")
    p_find = sub.add_parser("find-operations",
                            help="search operation names for a keyword")
    p_find.add_argument("keyword")
    sub.add_parser("find-facilities",
                   help="locate the operation that lists facility codes")
    p_try = sub.add_parser("try-facilities",
                           help="probe candidate facility codes for a valid one")
    p_try.add_argument("codes", nargs="+")
    p_try.add_argument("--sku", help="SKU to probe with")

    p_inv = sub.add_parser("inventory", help="inventory per SKU per facility")
    p_inv.add_argument("--sku", action="append", dest="skus",
                       help="repeatable; narrow slice for a first test")
    p_inv.add_argument("--facility", action="append", dest="facilities")
    p_inv.add_argument("--all", action="store_true",
                       help="whole catalogue (required if no --sku given)")
    p_inv.add_argument("--no-facility-header", action="store_true",
                       help="omit the Facility HTTP header, to A/B its effect")

    p_so = sub.add_parser("sale-orders", help="order headers only (no line items)")
    p_so.add_argument("--days", type=int, default=1)
    p_so.add_argument("--full", action="store_true")

    p_ex = sub.add_parser("export", help="bulk sale-order export, with line items")
    p_ex.add_argument("--days", type=int, default=1)
    p_ex.add_argument("--full", action="store_true")
    p_ex.add_argument("--job-type", dest="job_type",
                      help="ExportJobTypeName, from the Uniware export screen")
    p_ex.add_argument("--date-filter-id", default="created",
                      help="ExportFilter id the date range applies to")
    p_ex.add_argument("--job", help="resume polling an existing job")
    p_ex.add_argument("--email", help="NotificationEmail for the finished job")
    p_ex.add_argument("--frequency", default="ONCE",
                      help="Frequency is a REQUIRED field; ONCE is a guess")
    p_ex.add_argument("--column", action="append", dest="columns",
                      help="repeatable ExportColumn; omit for the default set")
    p_ex.add_argument("--skip-validation", action="store_true",
                      help="skip the SearchSaleOrder order-count cross-check")

    args = parser.parse_args()

    if args.command == "inventory" and not args.skus and not args.all:
        sys.exit(
            "Refusing a full-catalogue pull by default.\n"
            "Test a narrow slice first, e.g.:\n"
            "  python3 unicommerce_connect.py inventory --sku ABC123\n"
            "Then re-run with --all once the output looks right.")

    if args.command in ("sale-orders", "export"):
        guard_window(args.days, args.full, args.command)
    if args.command == "export" and not args.job and not args.job_type:
        sys.exit(
            "--job-type is required to create an export.\n"
            "ExportJobTypeName is not in the WSDL -- take the exact name from\n"
            "the export screen in the Uniware admin UI, e.g.:\n"
            "  python3 unicommerce_connect.py export --job-type 'Sale Order Item' --days 1\n"
            "Or resume an existing job with --job JOBCODE.")

    require_credentials()
    facility = None
    if args.command in ("inventory", "try-facilities"):
        facility = (getattr(args, "facilities", None) or [DEFAULT_FACILITY])[0]
        if getattr(args, "no_facility_header", False):
            facility = None
    print(f"Connecting to Unicommerce ({ENV})...", file=sys.stderr)
    client = get_client(facility=facility)
    if facility:
        print(f"  Facility header: {facility}", file=sys.stderr)
    print("Connected.", file=sys.stderr)

    if args.command == "operations":
        list_operations(client)
    elif args.command == "describe":
        describe_operations(client)
    elif args.command == "schema":
        show_schema(client)
    elif args.command == "find-operations":
        find_operations(client, args.keyword)
    elif args.command == "find-facilities":
        find_facilities(client)
    elif args.command == "try-facilities":
        try_facilities(client, args.codes, sku=args.sku)
    elif args.command == "inventory":
        facilities = args.facilities or [DEFAULT_FACILITY]
        print(f"  facility: {', '.join(facilities)}", file=sys.stderr)
        rows = get_inventory_rows(client, skus=args.skus, facilities=facilities)
        print(f"{len(rows)} SKU/facility rows", file=sys.stderr)
        for row in rows:
            print(row)
    elif args.command == "sale-orders":
        orders, total = search_sale_orders(client, days=args.days)
        print(f"{len(orders)} of {total} orders", file=sys.stderr)
        for order in orders:
            print(serialize_object(order))
    elif args.command == "export":
        if not args.job and not args.skip_validation:
            validate_export_slice(client, args.days)
        print(export_sale_orders(client, job_type=args.job_type, days=args.days,
                                 job_id=args.job, email=args.email,
                                 date_filter_id=args.date_filter_id,
                                 frequency=args.frequency, columns=args.columns))


if __name__ == "__main__":
    main()
