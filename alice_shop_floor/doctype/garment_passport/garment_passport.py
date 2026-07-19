"""
Garment Passport - Module 8: QR-sealed proof of craftsmanship.

Sealed automatically when a Production Stage Tracker reaches Pack.
Immutable after sealing.

The QR code on the hangtag encodes a URL:
  https://passport.zazfit.com/PASSPORT-WO-2026-001

Customer scans it and sees:
  - What garment it is
  - The fabric lot it was cut from
  - The pattern file (.val) made for their measurements
  - The PrintFactory job that cut it
  - Every operator who touched it, by stage
  - Every QC checkpoint it passed
  - "Made in the USA" as scannable fact, not marketing copy

This is ZAZFIT's strategic differentiator. No competitor can build this
because they do not own the pattern generation layer. ZAZFIT does.
"""

import json
import os
import frappe
from frappe import _
from frappe.utils import now_datetime

PASSPORT_BASE_URL = os.environ.get("PASSPORT_BASE_URL", "https://passport.zazfit.com")


class GarmentPassport(frappe.model.document.Document):

    def validate(self):
        if self.is_sealed:
            frappe.throw(_("A sealed Garment Passport cannot be modified."))

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def seal(self, sealed_by=None):
        """
        Seal this passport. Called when the tracker reaches Pack.
        1. Gathers all QC results for this work order
        2. Builds the full passport payload JSON
        3. Generates QR code image (requires qrcode[pil])
        4. Uploads QR to S3 (if boto3 + env vars available)
        5. Marks is_sealed = 1 and saves
        """
        if self.is_sealed:
            frappe.throw(_("Passport {} is already sealed.").format(self.name))

        self._gather_qc_summary()
        payload = self._build_payload()
        self.qr_payload = json.dumps(payload, indent=2, default=str)
        self.passport_url = "{}/{}".format(PASSPORT_BASE_URL, self.name)

        qr_s3_key = self._generate_and_upload_qr(self.passport_url)
        if qr_s3_key:
            self.qr_image_ref = qr_s3_key

        self.is_sealed = 1
        self.sealed_at = now_datetime()
        self.sealed_by = sealed_by or frappe.session.user
        self.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.logger().info(
            "ALICE: Garment Passport {} sealed for WO {}. URL: {}".format(
                self.name, self.work_order, self.passport_url
            )
        )

        frappe.publish_realtime(
            event="passport_sealed",
            message={
                "passport": self.name,
                "work_order": self.work_order,
                "passport_url": self.passport_url,
            },
            room="shop_floor",
        )

        return self.passport_url

    def get_public_data(self):
        """
        Returns the clean passport dict for the customer-facing page.
        Called by the passport URL endpoint.
        """
        if self.qr_payload:
            return json.loads(self.qr_payload)
        return {}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _gather_qc_summary(self):
        """Pull all QC check results for this work order and summarise."""
        checks = frappe.get_list(
            "Garment QC Check",
            filters={"work_order": self.work_order},
            fields=["name", "qc_stage", "result", "checked_by", "checked_at"],
            order_by="checked_at ASC",
        )

        passed = [c["qc_stage"] for c in checks if c["result"] == "Pass"]
        failed = [c["qc_stage"] for c in checks if c["result"] in ("Fail", "Rework Required")]

        self.qc_passed_stages = ", ".join(passed) if passed else ""
        self.qc_failed_stages = ", ".join(failed) if failed else ""
        self.has_defects = 1 if failed else 0

    def _build_payload(self):
        """Build the full passport JSON payload."""
        operators = [
            {
                "stage": op.stage,
                "operator": op.operator,
                "at": str(op.touched_at),
            }
            for op in (self.operators or [])
        ]

        qc_checks = frappe.get_list(
            "Garment QC Check",
            filters={"work_order": self.work_order},
            fields=["qc_stage", "result", "checked_by", "checked_at", "trigger_source"],
            order_by="checked_at ASC",
        )

        return {
            "passport_id": self.name,
            "passport_url": self.passport_url,
            "garment": {
                "work_order": self.work_order,
                "item": self.production_item,
            },
            "provenance": {
                "fabric_lot": self.fabric_lot or "",
                "pattern_file_ref": self.pattern_file_ref or "",
                "printfactory_job_id": self.printfactory_job_id or "",
                "made_in": "USA",
            },
            "made_by": operators,
            "quality_record": {
                "checkpoints": [
                    {
                        "stage": c["qc_stage"],
                        "result": c["result"],
                        "inspector": c["checked_by"],
                        "at": str(c["checked_at"]),
                        "source": c["trigger_source"],
                    }
                    for c in qc_checks
                ],
                "all_stages_passed": not bool(self.qc_failed_stages),
            },
            "sealed_at": str(self.sealed_at),
            "sealed_by": self.sealed_by,
        }

    def _generate_and_upload_qr(self, url):
        """
        Generate a QR PNG for the passport URL and upload to S3.
        Returns the S3 key, or None if qrcode / boto3 not available.
        """
        try:
            import qrcode
            from io import BytesIO

            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_H,
                box_size=10,
                border=4,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")

            buf = BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)

            return self._upload_to_s3(buf.read(), self.name + ".png")

        except ImportError:
            frappe.logger().warning(
                "qrcode or PIL not installed — QR image not generated for {}.".format(self.name)
            )
            return None
        except Exception as e:
            frappe.logger().error(
                "QR generation failed for {}: {}".format(self.name, e)
            )
            return None

    def _upload_to_s3(self, image_bytes, filename):
        """Upload QR PNG to S3 and return the S3 key. Returns None if not configured."""
        try:
            import boto3

            bucket = os.environ.get("AWS_S3_PASSPORT_BUCKET")
            if not bucket:
                frappe.logger().warning("AWS_S3_PASSPORT_BUCKET not set — QR not uploaded.")
                return None

            s3 = boto3.client("s3")
            key = "passports/qr/{}".format(filename)
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=image_bytes,
                ContentType="image/png",
                ACL="public-read",
            )
            frappe.logger().info("QR uploaded: s3://{}/{}".format(bucket, key))
            return key

        except ImportError:
            frappe.logger().warning("boto3 not available — QR not uploaded to S3.")
            return None
        except Exception as e:
            frappe.logger().error("S3 upload failed for {}: {}".format(filename, e))
            return None
