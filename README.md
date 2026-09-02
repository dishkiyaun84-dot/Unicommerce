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
python3 unicommerce_connect.py inventory --sku ABC123
python3 unicommerce_connect.py sale-orders --days 1
python3 unicommerce_connect.py export --job-type "<name>" --days 1

python3 unicommerce_connect.py inventory --all
python3 unicommerce_connect.py export --job-type "<name>" --days 90 --full
```

Large SKU lists are chunked 100 per call; sale orders page through
`SearchOptions {DisplayStart, DisplayLength}` against `TotalRecords`.

## The two values the WSDL cannot give you

Everything else is reconciled. These two are plain `xsd:string` in the schema and
their vocabularies live in the Uniware config, so they only resolve at runtime:

**`ExportJobTypeName`** — required, passed as `--job-type`. Take the exact name
from the export screen in the Uniware admin UI. The script refuses to invent one.

**The `ExportFilter` `id`** for the date range — `--date-filter-id`, defaulting to
`created`, which is a guess.

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
  `SearchOptions` and `ExportFilters/DateRange` wrappers.
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
