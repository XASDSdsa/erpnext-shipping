"""Revalidate an approved SF booking immediately before its carrier request.

The provider owns the immutable booking input contract. Flow records the review
in an Integration Request and authorizes its worker via the active-request flag;
this module does not import Flow or change conversation state.
"""

from copy import deepcopy
import hashlib
import json

import frappe

from . import shipping
from .sf_label_rules import LabelInputError

SERVICE = "Flow SF Label"


def canonical_hash(value):
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def parcels(doc):
    return [{key: row.get(key) for key in ("length", "width", "height", "weight", "count")}
            for row in doc.get("shipment_parcel") or []]


def payload_hash(doc, form):
    body, _, _ = shipping._create_order_body_from_form(doc, form)
    body = deepcopy(body)
    body["pieceorderBaseInfo"].pop("userOrderid", None)
    return canonical_hash(body)


def validate_reviewed_booking(doc, form, attempt):
    """Called immediately before a Flow-owned attempt may issue its carrier POST."""
    name = frappe.db.get_value("Integration Request", {"integration_request_service": SERVICE,
        "request_id": attempt.name, "reference_docname": doc.name}, "name")
    if not name or frappe.flags.get("flow_sf_active_request") != name:
        raise LabelInputError("此面单须由已批准的 Flow 后台任务执行，请查看原任务结果，不能重复请求。")
    doc.check_permission("read")
    doc.check_permission("write")
    ledger = frappe.get_doc("Integration Request", name)
    plan = json.loads(ledger.data)["plan"]
    if not shipping.is_enabled() or doc.docstatus != 1:
        raise LabelInputError("接口已停用或运单状态已变化，未发送顺丰下单请求，请重新核对。")
    dn = frappe.get_doc("Delivery Note", plan["state"]["delivery_note"], for_update=True)
    dn.check_permission("read")
    if dn.docstatus != 1 or dn.get("is_return") or str(dn.modified) != plan["state"]["dn_modified"]:
        raise LabelInputError("出库单在批准后已变化，未发送顺丰下单请求，请重新核对。")
    if {r.delivery_note for r in doc.get("shipment_delivery_note") or []} != {dn.name}:
        raise LabelInputError("运单关联的出库单已变化，未发送顺丰下单请求。")
    if doc.get("delivery_customer") != plan["state"]["customer"] or doc.get("pickup_company") != plan["state"]["company"]:
        raise LabelInputError("运单客户或公司在批准后已变化，未发送顺丰下单请求。")
    if doc.get("delivery_address_name") != plan["state"]["address_name"]:
        raise LabelInputError("运单收货地址已变化，未发送顺丰下单请求。")
    current = shipping._party_from_address(doc.delivery_address_name, doc.delivery_contact_name)
    current = shipping._fill_receiver_gaps(current, delivery_to=doc.get("delivery_to"),
        delivery_contact_name=doc.get("delivery_contact_name"), delivery_contact=doc.get("delivery_contact"))
    if canonical_hash(current) != canonical_hash(plan["state"]["receiver"]):
        raise LabelInputError("收件人档案在批准后已变化，未发送顺丰下单请求。")
    if (canonical_hash(parcels(doc)) != canonical_hash(plan["parcels"])
        or payload_hash(doc, form) != plan["payload_hash"]
        or payload_hash(doc, shipping._booking_form_for_doc(doc)) != plan["payload_hash"]):
        raise LabelInputError("包裹、仓库发件信息或下单内容在批准后已变化，未发送顺丰下单请求。")
