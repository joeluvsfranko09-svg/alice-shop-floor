import frappe
from frappe.model.document import Document


class SanMarConfig(Document):

    def validate(self):
        if self.stock_cache_ttl_minutes and self.stock_cache_ttl_minutes < 5:
            frappe.throw("Stock Cache TTL must be at least 5 minutes to avoid hammering SanMar's API.")

    @staticmethod
    def get_config():
        """Return the singleton SanMar Config doc, or None if unconfigured."""
        try:
            return frappe.get_single("SanMar Config")
        except Exception:
            return None

    def test_connection(self):
        """Smoke-test the SanMar API credentials. Called from UI button."""
        from alice_shop_floor.alice_shop_floor.sanmar.client import SanMarClient
        client = SanMarClient.from_config(self)
        ok, msg = client.ping()
        return {"ok": ok, "message": msg}
