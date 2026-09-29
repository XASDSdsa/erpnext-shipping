"""Immutable historical SF waybill record."""

from contextvars import ContextVar

import frappe
from frappe.model.document import Document


_mutation = ContextVar("sf_waybill_mutation", default=False)


def mutation_context():
	return _mutation.set(True)


def clear_mutation(token):
	_mutation.reset(token)


class SFWaybill(Document):
	def validate(self):
		if _mutation.get():
			return
		if self.is_new():
			frappe.throw("顺丰面单记录只能通过专用物流操作创建。")
		frappe.throw("顺丰面单历史记录不可直接修改，请通过专用换单或物流核对操作。")

	def on_trash(self):
		frappe.throw("顺丰面单历史记录不可删除。")
