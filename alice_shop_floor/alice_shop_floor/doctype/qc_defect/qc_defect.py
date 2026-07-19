from frappe.model.document import Document


SEVERITY_WEIGHT = {
    "Minor": 1,
    "Major": 2,
    "Critical": 3,
}


class QcDefect(Document):

    @property
    def weight(self):
        """Numeric weight for this defect's severity. Used for scoring."""
        return SEVERITY_WEIGHT.get(self.severity, 1)
