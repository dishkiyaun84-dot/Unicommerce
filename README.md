# Unicommerce connection script

`unicommerce_connect.py` authenticates against the Unicommerce (Uniware) SOAP
API with WS-Security and runs the two pulls the restock pipeline needs:

1. **Inventory** — per SKU per facility.
2. **Sales** — with line items, via the async export job.

## Purchase orders are out of scope

POs are not created in Unicommerce on this account's plan, so `GetPurchaseOrders`,
`GetPurchaseOrderDetail`, and the `OpenPurchase` field on inventory responses all
return unreliable numbers. None of them are integrated, and nothing should be
built against them.

Open-PO quantities come from a manually maintained file outside Unicommerce.

If the service returns a PO-derived field anyway, the script names it on stderr
and ignores it — see `IGNORED_RESPONSE_FIELDS`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export UNICOMMERCE_USERNAME="<your uniware username>"
export UNICOMMERCE_API_KEY="<key from the admin panel>"
export UNICOMMERCE_ENV="production"     # or "sandbox"
```

## Narrow slices first

Full-range pulls are behind explicit flags. `inventory` refuses to run
catalogue-wide without `--all`, and both date-ranged commands refuse a window
over 7 days without `--full`. Work outward from a small slice:

```bash
python3 unicommerce_connect.py describe                 # 1. what the WSDL says
python3 unicommerce_connect.py inventory --sku ABC123   # 2. one SKU
python3 unicommerce_connect.py sale-orders --days 1     # 3. one day of headers
python3 unicommerce_connect.py export --days 1          # 4. one day with line items

python3 unicommerce_connect.py inventory --all          # 5. only once 2 looks right
python3 unicommerce_connect.py export --days 90 --full  # 6. only once 4 looks right
```

## Operations

| Purpose | Operation | Status |
| --- | --- | --- |
| Inventory, catalogue-wide | `GetBulkItemTypeInventory` | name confirmed |
| Inventory, per SKU | `GetInventorySnapshot` | name confirmed |
| Sale order headers | `SearchSaleOrder` | name confirmed; **no line items** |
| Start bulk export | `CreateExportJob` | name confirmed; request fields unconfirmed |
| Poll bulk export | `GetExportJobStatus` | **fully confirmed and wired up** |

Which inventory operation gives cleaner per-SKU-per-facility numbers is still
open — both stay reachable so they can be compared on the same narrow slice
(`--snapshot` forces the per-SKU one). The bulk op is the default when no SKUs
are given.

`SearchSaleOrder` returns order headers only, which is why the size-wise sales
figure needs the export path. That path exists — `GetExportJobStatus` is
confirmed against the WSDL as:

```
REQUEST : JobCode: xsd:string
RESPONSE: Successful: xsd:boolean, Errors: {Error: ns0:Error[]},
          Warnings: {Warning: ns0:Warning[]}, Status: xsd:string,
          FilePath: xsd:string
```

`FilePath` on the finished job is where the line items land. `Successful` is the
API-call wrapper rather than the job outcome, so a `False` there raises with the
`Errors` text instead of polling on blindly.

The `Status` **values** are still unknown — the WSDL types it as a plain
`xsd:string`, so the real vocabulary only appears at runtime. Unrecognised
values keep polling and end in a timeout rather than being read as success.

Long exports can be resumed instead of restarted:

```bash
python3 unicommerce_connect.py export --job JOB-77
```

## Next step: confirm the request fields

`GetExportJobStatus` is fully reconciled. Still outstanding: the request fields
for `GetBulkItemTypeInventory`, `GetInventorySnapshot`, `SearchSaleOrder` and
`CreateExportJob`, plus the job-id field on the `CreateExportJob` response and
the real `Status` values. These are marked `# UNVERIFIED` in the source and
grouped one dict per function, so each is a single edit.

```bash
python3 unicommerce_connect.py describe
```

Fields can also be corrected at the call site without editing the file:

```python
get_sale_orders(client, days=1, CreatedFrom=None, DateRange={"From": ..., "To": ...})
```

Until that reconciliation is done, treat any numbers these functions return as
unverified — a SOAP request whose fields do not match the schema will fail, or
silently ignore the fields it does not recognise.

## Note on sandboxed environments

Both `staging.unicommerce.com` and `styxxinternational.unicommerce.com` are
outside the egress allowlist of the Claude Code cloud sandbox, so the script
cannot reach them from there (the proxy answers `403` to `CONNECT`). Run it from
a machine or server with direct network access.

This means everything WSDL-dependent — authentication, the real request
signatures, whether the export operations exist, and every live response — is
**unexercised against the service**.

Tested offline: CLI dispatch, credential and env guards, the narrow-slice
guards, operation routing for both inventory ops, request construction, the
trailing-window date maths, `describe` against a locally built WSDL, and the
export state machine against the confirmed response shape (create → poll →
complete with FilePath, `Successful=False` and FAILED both raising with their
Errors text, warnings deduplicated across polls, a completed job with no
FilePath called out, `--job` resuming without creating a second export, missing
job id, and unrecognised or absent statuses timing out rather than reporting
success).
