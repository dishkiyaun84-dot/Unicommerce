# Unicommerce connection script

`unicommerce_connect.py` authenticates against the Unicommerce (Uniware) SOAP
API with WS-Security and prints every operation exposed by the tenant's WSDL.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run

Credentials are read from the environment; never hardcode them in the script.

```bash
export UNICOMMERCE_USERNAME="<your uniware username>"
export UNICOMMERCE_API_KEY="<key from the admin panel>"
export UNICOMMERCE_ENV="production"     # or "sandbox"

python3 unicommerce_connect.py describe          # run this first
python3 unicommerce_connect.py operations
python3 unicommerce_connect.py inventory
python3 unicommerce_connect.py sale-orders --days 90
python3 unicommerce_connect.py purchase-orders
python3 unicommerce_connect.py po 'UL-MUM/13-14/53'
```

Endpoints used:

| env | WSDL |
| --- | --- |
| `sandbox` | `https://staging.unicommerce.com/services/soap/uniware15.wsdl` |
| `production` | `https://styxxinternational.unicommerce.com/services/soap/uniware19.wsdl` |

## Operations

| Purpose | Operation | Status |
| --- | --- | --- |
| Inventory, all SKUs | `GetBulkItemTypeInventory` | name confirmed |
| Inventory, per SKU | `GetInventorySnapshot` | name confirmed |
| Sale orders by date range | `SearchSaleOrder` | name confirmed |
| List purchase orders | `GetPurchaseOrders` | name confirmed |
| One PO by code | `GetPurchaseOrderDetail` | name + request shape documented |

`GetSaleOrderDetails` does not exist on this WSDL; `SearchSaleOrder` replaces it.

## Next step: confirm the request fields

The operation **names** above are confirmed. The request **field** names inside
each pull function are not -- they are marked `# UNVERIFIED` in the source and
grouped into one dict per function so each is a single edit.

Run `describe` and reconcile its output against those dicts:

```bash
python3 unicommerce_connect.py describe
```

Field names can also be corrected at the call site without editing the file,
which is useful while iterating:

```python
get_sale_orders(client, days=90, CreatedFrom=None, DateRange={"From": ..., "To": ...})
```

Until that reconciliation is done, treat any numbers these functions return as
unverified -- a SOAP request whose fields do not match the schema will fail, or
silently ignore the fields it does not recognise.

## Note on sandboxed environments

Both `staging.unicommerce.com` and `styxxinternational.unicommerce.com` are
outside the egress allowlist of the Claude Code cloud sandbox, so the script
cannot reach them from there (the proxy answers `403` to `CONNECT`). Run it
from a machine or server with direct network access to those hosts.

This means the WSDL-dependent behaviour -- authentication, the real request
signatures, and every live response -- has not been exercised against the
service. What has been tested offline: CLI dispatch, the credential and env
guards, request construction for all five operations, the trailing-window date
maths, and `describe` itself against a locally built WSDL.
