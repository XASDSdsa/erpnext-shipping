import frappe
from frappe import _
from frappe.model.document import Document


class SFInternationalSettings(Document):
	def validate(self):
		if not self.enabled:
			return
		token = ""
		try:
			from frappe.utils.password import get_decrypted_password

			token = (
				get_decrypted_password(
					self.doctype, self.name, "iuop_token", raise_exception=False
				)
				or get_decrypted_password(
					self.doctype, self.name, "pms_token", raise_exception=False
				)
				or ""
			).strip()
		except Exception:
			token = ""
		if not token and not (self.iuop_token or "").strip() and not (self.pms_token or "").strip():
			frappe.throw(_("Please fill {0}").format("IUOP Token"))
		if self.customer_code:
			self.customer_code = self.customer_code.strip()
		if self.username:
			self.username = self.username.strip()
		if self.userid_mod_10:
			self.userid_mod_10 = str(self.userid_mod_10).strip()
		if not self.products:
			frappe.throw(_("Please add at least one IUOP product (numeric code such as 10)."))
		for row in self.products:
			code = str(row.product_code or "").strip()
			if code.upper().startswith("INT"):
				frappe.throw(
					_("Product {0} uses an Open API INT code. Use IUOP numeric codes such as 10 / 29 / 72.").format(
						code
					)
				)

	def on_update(self):
		frappe.cache().delete_value("sf_international_access_token")
		frappe.cache().delete_value("sf_international_access_token:sit")
		frappe.cache().delete_value("sf_international_access_token:prod")
