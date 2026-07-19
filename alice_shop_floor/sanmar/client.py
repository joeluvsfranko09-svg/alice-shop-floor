"""
SanMar API Client
=================
Supports both SanMar PromoStandards REST (primary) and the legacy SOAP
WebService (fallback).  All public methods return plain Python dicts so
callers don't need to know which transport was used.

Credentials are read from the SanMar Config singleton — never hard-coded.

PromoStandards endpoints used
──────────────────────────────
  Product Data Service v2.0  →  /promostandards/pds/v2/
  Inventory Service v2.0     →  /promostandards/inventory/v2/
  Pricing Service v1.0       →  /promostandards/ppc/v1/
  Purchase Order Service v1.0 → /promostandards/pos/v1/

SOAP WebService (legacy fallback)
──────────────────────────────────
  WSDL: http://ws.sanmar.com:8080/SanMarWebService/SanMarWebServicePort?wsdl
  Key methods: getInventoryQty, getProductInfoByStyle, getPriceInfo
"""

import json
import frappe
import requests


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class SanMarAuthError(Exception):
    """Raised when SanMar returns a 401/403 or SOAP auth fault."""

class SanMarAPIError(Exception):
    """Raised on 4xx/5xx responses or malformed payloads."""

class SanMarConfigMissing(Exception):
    """Raised when SanMar Config is not yet filled in."""


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────

class SanMarClient:
    """
    Thin wrapper around SanMar's API.  Instantiate via `from_config()`.

    Usage::

        client = SanMarClient.from_config()
        inv    = client.get_inventory("PC61", color_name="Red")
        price  = client.get_pricing("PC61")
        product= client.get_product("PC61")
    """

    REST_BASE = "https://api.sanmar.com"

    def __init__(self, username: str, password: str, mode: str = "REST",
                 rest_base: str | None = None, soap_wsdl: str | None = None,
                 timeout: int = 30):
        self.username = username
        self.password = password
        self.mode     = mode            # "REST (PromoStandards)" or "SOAP (Legacy WebService)"
        self.rest_base = (rest_base or self.REST_BASE).rstrip("/")
        self.soap_wsdl = soap_wsdl or "http://ws.sanmar.com:8080/SanMarWebService/SanMarWebServicePort?wsdl"
        self.timeout   = timeout
        self._soap_client = None        # lazy-loaded zeep client

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config=None):
        """Build a client from the SanMar Config singleton."""
        if config is None:
            try:
                config = frappe.get_single("SanMar Config")
            except Exception:
                raise SanMarConfigMissing(
                    "SanMar Config is not set up. Go to SanMar Config and enter your API credentials."
                )
        username = config.sanmar_username
        password = config.get_password("sanmar_password")
        if not username or not password:
            raise SanMarConfigMissing(
                "SanMar Config is missing username or password. "
                "Go to SanMar Config and fill in your credentials."
            )
        return cls(
            username  = username,
            password  = password,
            mode      = config.api_mode or "REST (PromoStandards)",
            rest_base = config.rest_base_url,
            soap_wsdl = config.soap_wsdl_url,
            timeout   = int(config.connection_timeout or 30),
        )

    # ── Connection smoke-test ─────────────────────────────────────────────────

    def ping(self) -> tuple[bool, str]:
        """
        Return (ok, message).  Called from SanMar Config 'Test Connection' button.
        Tries to fetch a known lightweight endpoint.
        """
        try:
            if "SOAP" in self.mode:
                self._get_soap_client()
                return True, "SOAP client initialised — WSDL loaded OK"
            else:
                # Fetch product data for PC61 (a universally available style)
                result = self.get_product("PC61")
                if result:
                    return True, "Connected to SanMar REST API — PC61 data fetched OK"
                return False, "Connected but response was empty"
        except SanMarAuthError as e:
            return False, "Authentication failed: " + str(e)
        except Exception as e:
            return False, "Connection error: " + str(e)

    # ── Product Data ──────────────────────────────────────────────────────────

    def get_product(self, style_number: str) -> dict:
        """
        Return product information for a SanMar style number.

        Returns dict with keys:
          style, brand, product_name, description,
          colors: [{color_name, color_code, fits: [{fit_code, fit_label}]}]
        """
        if "SOAP" in self.mode:
            return self._soap_get_product(style_number)
        return self._rest_get_product(style_number)

    def _rest_get_product(self, style_number: str) -> dict:
        url = f"{self.rest_base}/promostandards/pds/v2/product"
        payload = {
            "wsVersion": "2.0.0",
            "id": self.username,
            "password": self.password,
            "localizationCountry": "US",
            "localizationLanguage": "en",
            "productId": style_number,
            "isSellable": True,
        }
        resp = self._post(url, payload)
        return self._parse_product_response(resp)

    def _parse_product_response(self, raw: dict) -> dict:
        """Normalise PromoStandards PDS response into our internal format."""
        prod = raw.get("Product", {})
        if not prod:
            return {}

        colors = []
        for part in prod.get("ProductPartArray", {}).get("ProductPart", []):
            color = part.get("ColorArray", {}).get("Color", [{}])[0]
            size  = part.get("ApparelSize", {})
            colors.append({
                "color_name":  color.get("colorName", ""),
                "color_code":  color.get("hex", ""),
                "fit_code":    size.get("apparelStyle", ""),
                "fit_label":   size.get("labelSize", ""),
                "sanmar_sku":  part.get("partId", ""),
            })

        return {
            "style":        prod.get("productId", ""),
            "brand":        prod.get("brand", ""),
            "product_name": prod.get("productName", ""),
            "description":  prod.get("description", ""),
            "colors":       colors,
        }

    # ── Inventory ─────────────────────────────────────────────────────────────

    def get_inventory(self, style_number: str, color_name: str = None,
                      fit_code: str = None) -> list[dict]:
        """
        Return inventory for a style (optionally filtered to a color and/or fit).

        Each entry: {sanmar_sku, style, color_name, fit_code,
                     total_qty, status, warehouses: [{name, qty}]}
        """
        if "SOAP" in self.mode:
            return self._soap_get_inventory(style_number, color_name, fit_code)
        return self._rest_get_inventory(style_number, color_name, fit_code)

    def _rest_get_inventory(self, style: str, color: str = None, fit: str = None) -> list[dict]:
        url = f"{self.rest_base}/promostandards/inventory/v2/inventory"
        payload = {
            "wsVersion": "2.0.0",
            "id": self.username,
            "password": self.password,
            "productId": style,
            "Filter": {"partIdArray": {"partId": []}},
        }
        resp = self._post(url, payload)
        return self._parse_inventory_response(resp, color_filter=color, fit_filter=fit)

    def _parse_inventory_response(self, raw: dict, color_filter=None, fit_filter=None) -> list[dict]:
        results = []
        inv_obj = raw.get("Inventory", {})
        parts   = inv_obj.get("PartInventoryArray", {}).get("PartInventory", [])
        if isinstance(parts, dict):
            parts = [parts]  # single item — PromoStandards wraps inconsistently

        for part in parts:
            part_id = part.get("partId", "")
            # Split partId into style-color-fit (SanMar convention)
            segments = part_id.rsplit("-", 2)
            color_name = segments[1] if len(segments) >= 2 else ""
            fit_code   = segments[2] if len(segments) >= 3 else ""

            if color_filter and color_name.lower() != color_filter.lower():
                continue
            if fit_filter and fit_code.lower() != fit_filter.lower():
                continue

            # Warehouse breakdown
            warehouses = []
            for loc in part.get("InventoryLocationArray", {}).get("InventoryLocation", []):
                warehouses.append({
                    "name": loc.get("inventoryLocationId", ""),
                    "qty":  int(loc.get("inventoryLocationQuantity", {}).get("Quantity", {}).get("value", 0)),
                })

            total_qty = sum(w["qty"] for w in warehouses)
            status    = _qty_to_status(total_qty)

            results.append({
                "sanmar_sku":  part_id,
                "style":       part_id.split("-")[0] if "-" in part_id else part_id,
                "color_name":  color_name,
                "fit_code":    fit_code,
                "total_qty":   total_qty,
                "status":      status,
                "warehouses":  warehouses,
            })

        return results

    # ── Pricing ───────────────────────────────────────────────────────────────

    def get_pricing(self, style_number: str) -> list[dict]:
        """
        Return pricing for a style.

        Each entry: {sanmar_sku, net_price, case_price, currency}
        """
        if "SOAP" in self.mode:
            return self._soap_get_pricing(style_number)
        return self._rest_get_pricing(style_number)

    def _rest_get_pricing(self, style: str) -> list[dict]:
        url = f"{self.rest_base}/promostandards/ppc/v1/pricing"
        payload = {
            "wsVersion": "1.0.0",
            "id": self.username,
            "password": self.password,
            "productId": style,
            "currency": "USD",
            "fobId": "1",
            "priceType": "Net",
            "configurationType": "Blank",
        }
        resp = self._post(url, payload)
        return self._parse_pricing_response(resp)

    def _parse_pricing_response(self, raw: dict) -> list[dict]:
        results = []
        config = raw.get("PriceServiceResponse", {}).get("Configuration", {})
        if not config:
            return []
        for part in config.get("PartArray", {}).get("Part", []):
            part_id = part.get("partId", "")
            prices  = part.get("PartPriceArray", {}).get("PartPrice", [])
            if isinstance(prices, dict):
                prices = [prices]
            net = case = 0.0
            for p in prices:
                qty_max = int(p.get("maxQuantity", 0) or 0)
                price   = float(p.get("price", 0) or 0)
                if qty_max == 0 or qty_max >= 12:  # treat 0 max as "any qty"
                    net = price
                if qty_max >= 72:                   # case-break pricing
                    case = price
            results.append({
                "sanmar_sku": part_id,
                "net_price":  net,
                "case_price": case,
                "currency":   "USD",
            })
        return results

    # ── Purchase Order submission ─────────────────────────────────────────────

    def submit_purchase_order(self, po_data: dict) -> dict:
        """
        Submit a purchase order to SanMar.

        po_data format::

            {
              "po_number": "PO-0001",
              "ship_to": {
                "company": "ZAZFIT",
                "address1": "...",
                "city": "...", "state": "TX", "zip": "78701",
                "country": "US"
              },
              "lines": [
                {"sanmar_sku": "PC61-Red-M", "qty": 12}
              ]
            }

        Returns {ok, sanmar_po_id, message}
        """
        if "SOAP" in self.mode:
            return self._soap_submit_po(po_data)
        return self._rest_submit_po(po_data)

    def _rest_submit_po(self, po_data: dict) -> dict:
        url = f"{self.rest_base}/promostandards/pos/v1/purchaseOrder"
        lines = []
        for i, line in enumerate(po_data.get("lines", []), start=1):
            lines.append({
                "lineNumber": i,
                "partId": line["sanmar_sku"],
                "quantity": {"uom": "EA", "value": line["qty"]},
            })
        ship = po_data.get("ship_to", {})
        payload = {
            "wsVersion": "1.0.0",
            "id": self.username,
            "password": self.password,
            "PurchaseOrder": {
                "purchaseOrderNumber": po_data.get("po_number", ""),
                "orderType": "Blank",
                "ShipmentArray": {
                    "Shipment": [{
                        "shipReferences": "",
                        "ShipTo": {
                            "companyName": ship.get("company", ""),
                            "address1": ship.get("address1", ""),
                            "city": ship.get("city", ""),
                            "region": ship.get("state", ""),
                            "postalCode": ship.get("zip", ""),
                            "country": ship.get("country", "US"),
                        },
                        "LineItemArray": {"LineItem": lines},
                    }]
                },
            }
        }
        resp = self._post(url, payload)
        if resp.get("PoConfirmationArray"):
            conf = resp["PoConfirmationArray"].get("PoConfirmation", [{}])
            if isinstance(conf, dict):
                conf = [conf]
            return {
                "ok":           True,
                "sanmar_po_id": conf[0].get("confirmationNum", ""),
                "message":      "PO submitted to SanMar",
            }
        return {"ok": False, "sanmar_po_id": "", "message": str(resp)}

    # ── HTTP transport ────────────────────────────────────────────────────────

    def _post(self, url: str, payload: dict) -> dict:
        """POST JSON to a PromoStandards REST endpoint, return parsed JSON."""
        try:
            resp = requests.post(
                url,
                json=payload,
                auth=(self.username, self.password),
                timeout=self.timeout,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except requests.exceptions.Timeout:
            raise SanMarAPIError(f"Timeout connecting to SanMar API at {url}")
        except requests.exceptions.ConnectionError as e:
            raise SanMarAPIError(f"Cannot reach SanMar API: {e}")

        if resp.status_code in (401, 403):
            raise SanMarAuthError("SanMar rejected credentials — check SanMar Config username/password")
        if resp.status_code >= 400:
            raise SanMarAPIError(f"SanMar API error {resp.status_code}: {resp.text[:500]}")

        try:
            return resp.json()
        except Exception:
            raise SanMarAPIError(f"SanMar returned non-JSON response: {resp.text[:300]}")

    # ── SOAP transport (legacy) ───────────────────────────────────────────────

    def _get_soap_client(self):
        if self._soap_client:
            return self._soap_client
        try:
            from zeep import Client as ZeepClient
            from zeep.wsse.username import UsernameToken
        except ImportError:
            raise SanMarAPIError(
                "zeep is required for SOAP mode. "
                "Run: bench pip install zeep"
            )
        self._soap_client = ZeepClient(
            self.soap_wsdl,
            wsse=UsernameToken(self.username, self.password),
        )
        return self._soap_client

    def _soap_get_product(self, style: str) -> dict:
        client = self._get_soap_client()
        try:
            resp = client.service.getProductInfoByStyle(
                sanMarUserName=self.username,
                sanMarUserPassword=self.password,
                style=style,
            )
        except Exception as e:
            raise SanMarAPIError(f"SOAP getProductInfoByStyle failed: {e}")
        # Normalise SOAP response to our dict format
        if not resp:
            return {}
        colors = []
        for item in (resp.listOfProductsAndSizes or []):
            colors.append({
                "color_name": getattr(item, "colorName", ""),
                "color_code": getattr(item, "colorSwatchImage", ""),
                "fit_code":   getattr(item, "caseSize", ""),
                "fit_label":  getattr(item, "caseSize", ""),
                "sanmar_sku": f"{style}-{getattr(item, 'colorName','')}-{getattr(item,'caseSize','')}",
            })
        return {
            "style":        style,
            "brand":        getattr(resp, "brandName", ""),
            "product_name": getattr(resp, "title", ""),
            "description":  getattr(resp, "description", ""),
            "colors":       colors,
        }

    def _soap_get_inventory(self, style: str, color: str = None, fit: str = None) -> list[dict]:
        client = self._get_soap_client()
        try:
            resp = client.service.getInventoryQty(
                sanMarUserName=self.username,
                sanMarUserPassword=self.password,
                style=style,
                color=color or "",
                size=fit or "",
            )
        except Exception as e:
            raise SanMarAPIError(f"SOAP getInventoryQty failed: {e}")
        results = []
        for item in (resp.listOfInventoryAndSizes or []):
            qty    = int(getattr(item, "qty", 0) or 0)
            status = _qty_to_status(qty)
            results.append({
                "sanmar_sku": f"{style}-{getattr(item,'colorName','')}-{getattr(item,'size','')}",
                "style":      style,
                "color_name": getattr(item, "colorName", ""),
                "fit_code":   getattr(item, "size", ""),
                "total_qty":  qty,
                "status":     status,
                "warehouses": [{"name": "SanMar", "qty": qty}],
            })
        return results

    def _soap_get_pricing(self, style: str) -> list[dict]:
        client = self._get_soap_client()
        try:
            resp = client.service.getPriceInfo(
                sanMarUserName=self.username,
                sanMarUserPassword=self.password,
                style=style,
            )
        except Exception as e:
            raise SanMarAPIError(f"SOAP getPriceInfo failed: {e}")
        results = []
        for item in (resp.listOfPrices or []):
            results.append({
                "sanmar_sku": f"{style}-{getattr(item,'colorName','')}-{getattr(item,'size','')}",
                "net_price":  float(getattr(item, "ourPrice", 0) or 0),
                "case_price": float(getattr(item, "casePrice", 0) or 0),
                "currency":   "USD",
            })
        return results

    def _soap_submit_po(self, po_data: dict) -> dict:
        # SanMar legacy SOAP does not expose a PO submission endpoint the same
        # way — fall back to REST for PO submission even in SOAP mode.
        frappe.log_error(
            "SanMar SOAP mode does not support PO submission. Attempting REST PO fallback.",
            "SanMar PO Fallback"
        )
        return self._rest_submit_po(po_data)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _qty_to_status(qty: int) -> str:
    if qty <= 0:
        return "Out of Stock"
    if qty < 12:
        return "Low Stock"
    return "In Stock"
