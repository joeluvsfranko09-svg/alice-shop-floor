# Copyright (c) 2026, Athlettia LLC
from frappe.model.document import Document

class FitModel(Document):
    def as_measurements_dict(self) -> dict:
        """Return all measurement fields as a plain dict for resolve_vit()."""
        fields = [
            "bust_cm","waist_cm","hip_cm","inseam_cm","rise_cm","thigh_cm",
            "shoulder_cm","sleeve_cm","neck_cm","chest_cm",
            "back_length_cm","front_length_cm",
        ]
        return {f: (getattr(self, f, None) or 0.0) for f in fields}
