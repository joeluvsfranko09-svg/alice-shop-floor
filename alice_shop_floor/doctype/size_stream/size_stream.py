# Copyright (c) 2026, Athlettia LLC
from frappe.model.document import Document

class SizeStream(Document):
    def get_size(self, size_code: str):
        """Return the SizeStreamRow matching size_code, or None."""
        for row in self.sizes:
            if row.size_code == size_code:
                return row
        return None
