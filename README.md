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
export UNICOMMERCE_ENV="sandbox"        # or "production"

python3 unicommerce_connect.py
```

Endpoints used:

| env | WSDL |
| --- | --- |
| `sandbox` | `https://staging.unicommerce.com/services/soap/uniware15.wsdl` |
| `production` | `https://styxxinternational.unicommerce.com/services/soap/uniware19.wsdl` |

## Next step

Read the printed operation list, then correct the operation names and request
fields in `get_inventory_snapshot`, `get_sale_order_details`, and
`get_purchase_order_detail` to match it before wiring them into the restock
pipeline.

## Note on sandboxed environments

Both `staging.unicommerce.com` and `styxxinternational.unicommerce.com` are
outside the egress allowlist of the Claude Code cloud sandbox, so the script
cannot reach them from there (the proxy answers `403` to `CONNECT`). Run it
from a machine or server with direct network access to those hosts.
