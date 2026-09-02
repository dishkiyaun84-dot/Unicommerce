# Unicommerce connection script

`unicommerce_connect.py` authenticates against the Unicommerce (Uniware) SOAP
API with WS-Security and runs the two pulls the restock pipeline needs:

1. **Inventory** — per SKU per facility, via `GetBulkItemTypeInventory`.
2. **Sales** — with line items, via `CreateExportJob` → `GetExportJobStatus`.

All five request signatures are confirmed against the tenant's v1.9 WSDL.

## Purchase orders are out of scope

POs are not created in Unicommerce on this account's plan, so `GetPurchaseOrders`,
`GetPurchaseOrderDetail`, and the `OpenPurchase` field all return unreliable
numbers. None are integrated. Open-PO quantities come from a manually maintained
file outside Unicommerce.

`flatten_inventory` reads only the six fields it names, so no PO-derived value
can reach the pipeline even if the service returns one.

## Why `GetBulkItemTypeInventory`, not `GetInventorySnapshot`

The signatures settle it:

```
GetBulkItemTypeInventory
  → InventoryDetails: {ItemInventory: {ItemSKU, ItemTypeName, MaxRetailPrice,
      Facilities: {FacilityInventory: {FacilityCode, FacilityName, Inventory}[]}}[]}

GetInventorySnapshot
  → InventorySnapshots: {InventorySnapshot: {ItemSKU, Inventory, OpenSale,
      OpenPurchase, InventoryBlocked, PutawayPending, ...}[]}
```

`GetInventorySnapshot` has **no facility dimension** — flat per-SKU totals only —
and it is the operation carrying `OpenPurchase`. `GetBulkItemTypeInventory`
nests per-facility quantities under each SKU and carries no PO-derived field. Only
the latter can answer per-SKU-per-facility, so it is the one wired up.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export UNICOMMERCE_USERNAME="<your uniware username>"
export UNICOMMERCE_API_KEY="<key from the admin panel>"
export UNICOMMERCE_ENV="production"
```

## Narrow slices first

Full runs sit behind explicit flags. `inventory` refuses to go catalogue-wide
without `--all`; both date-ranged commands refuse a window over 7 days without
`--full`.

```bash
python3 unicommerce_connect.py describe
python3 unicommerce_connect.py schema
python3 unicommerce_connect.py inventory --sku ABC123
python3 unicommerce_connect.py sale-orders --days 1
python3 unicommerce_connect.py export --job-type "Sale Order Item" --days 1

python3 unicommerce_connect.py inventory --all
python3 unicommerce_connect.py export --job-type "Sale Order Item" --days 90 --full
```

Large SKU lists are chunked 100 per call; sale orders page through
`SearchOptions {DisplayStart, DisplayLength}` against `TotalRecords`.

### `describe` cannot show what is required — use `schema`

`signature()` does not print `minOccurs`, so an element that looks optional in
`describe` output may be mandatory, and a wrapper that looks like it accepts an
empty list may require at least one entry. Each of those costs a failed
round-trip to discover. The constraints are in the WSDL already:

```bash
python3 unicommerce_connect.py schema
```

```
GetBulkItemTypeInventory -- request structure
  SkuCodes [1..1] REQUIRED : SkuCodes
    SkuCode [0..n] optional : string
  FacilityCodes [1..1] REQUIRED : FacilityCodes
    FacilityCode [1..n] REQUIRED : string
```

Run this before adding any new operation. If a call still fails validation,
`call` reports the missing element and what was sent as one line rather than a
zeep traceback.

### Facilities must be enumerated

`FacilityCodes` requires at least one `FacilityCode`, so there is no "all
facilities" request. This account has exactly one facility:

| Code | Type |
| --- | --- |
| `styxxinternational` | WAREHOUSE |

That is the `DEFAULT_FACILITY` constant, used whenever `--facility` is not given:

```bash
python3 unicommerce_connect.py inventory --sku GryDT-PlnOS-XS
python3 unicommerce_connect.py inventory --sku GryDT-PlnOS-XS --facility OTHER
```

**The codes are not available through the API.** This tenant's WSDL has no
facility-listing operation — the facility operations are all writes
(`CreateFacility`, `EditFacility`, `CreateOrEditFacilityItemType`) plus
`SwitchSaleOrderItemFacility`, which `find-facilities` reports. The code above
was read from the Uniware admin UI (Settings → Facilities). If a second facility
is ever added, find it there and pass it with `--facility`.

Whether `--all` (catalogue-wide, empty `SkuCodes`) is possible depends on
`SkuCode`'s own `minOccurs` — `schema` output answers that. If it is also `[1..n]`,
SKUs have to be enumerated too and `--all` cannot work as written.

## The two values the WSDL cannot give you

Everything else is reconciled. These two are plain `xsd:string` in the schema and
their vocabularies live in the Uniware config, so they only resolve at runtime:

**`ExportJobTypeName`** — required, passed as `--job-type`. Take the exact name
from the export screen in the Uniware admin UI; `"Sale Order Item"` above is an
illustrative example, not a confirmed value. Keep it quoted, since these names
contain spaces. The script refuses to invent one.

**The `ExportFilter` `id`** for the date range — `--date-filter-id`, defaulting to
`created`, which is a guess.

**`Frequency`** — `schema` reports it `[1..1] REQUIRED`, so it is always sent.
`ONCE` is a guess; override with `--frequency` if the service rejects it.

A wrong filter id is the dangerous case: the filter may be **ignored rather than
rejected**, and the export then silently covers the entire order history instead
of the day you asked for. So `export` first calls `SearchSaleOrder` for the same
window and prints the order count:

```
  SearchSaleOrder reports 43 orders between 2026-09-01 00:00 and 2026-09-02 00:00 UTC
  the export should contain line items for roughly that many orders; far more
  means the date filter was ignored
```

Check the export against that number before scaling to 90 days. `--skip-validation`
turns the cross-check off.

## Operations

| Purpose | Operation | Key request fields |
| --- | --- | --- |
| Inventory per SKU per facility | `GetBulkItemTypeInventory` | `SkuCodes{SkuCode[]}`, `FacilityCodes{FacilityCode[]}` |
| Order headers (probe only) | `SearchSaleOrder` | `FromDate`, `ToDate`, `SearchOptions{DisplayStart,DisplayLength}` |
| Start bulk export | `CreateExportJob` | `ExportJobTypeName`, `ExportFilters{ExportFilter[{id,DateRange{Start,End}}]}` |
| Poll bulk export | `GetExportJobStatus` | `JobCode` → `Status`, `FilePath` |
| *(unused)* | `GetInventorySnapshot` | no facility dimension; carries `OpenPurchase` |

`SearchSaleOrder` returns headers only — `Code`, `Status`, `Channel`, `CreatedOn`
and friends, no line items — which is why the size-wise figure needs the export.
Its real value here is as the order-count cross-check above.

The export's `Status` **values** remain unknown (`xsd:string` in the schema).
Unrecognised values keep polling and end in a timeout rather than being read as
success. Long exports resume with `--job JOB-77` instead of restarting.

## Testing status

Not yet run against the live service from this repo's development environment —
both unicommerce.com hosts are outside the Claude Code sandbox's egress allowlist
(the proxy answers `403` to `CONNECT`). The `describe` output that confirmed every
signature above was produced by running the script on a networked machine.

Verified offline:

- Request serialization for `GetBulkItemTypeInventory`, `SearchSaleOrder` and
  `CreateExportJob` against XSDs rebuilt from the confirmed signatures — the
  generated SOAP bodies are schema-valid, including the nested `SkuCodes`,
  `SearchOptions` and `ExportFilters/DateRange` wrappers, and with `SkuCodes`
  and `FacilityCodes` marked required as the live service reported them.
- `flatten_inventory` producing per-SKU-per-facility rows, and dropping
  `OpenPurchase` even when injected at either nesting level.
- SKU chunking (250 SKUs → 3 calls of 100/100/50) and order pagination
  (250 records → 3 pages at `DisplayStart` 0/100/200).
- The `Successful`/`Errors`/`Warnings` envelope raising with error text.
- The export state machine: create → poll → complete with `FilePath`,
  `Successful=False` and `FAILED` both raising, warnings deduplicated across
  polls, completion without a `FilePath` called out, `--job` resuming without
  creating a second export, and unrecognised or absent statuses timing out
  rather than reporting success.
- Every CLI guard.

Unexercised: authentication against production, and all live response content.
