"""Pure input checks for the reviewed SF label workflow.

The carrier, database and current user are intentionally outside this module.
"""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import math
import re

SF_ADDRESS_MAX_LENGTH = 60


class LabelInputError(ValueError):
	def __init__(self, message, fields=None):
		super().__init__(message)
		self.fields = list(fields or [])


def normalize_postcode(value):
	return re.sub(r"[\s-]+", "", str(value or "")).upper()


def _text(value):
	return str(value or "").strip()


def validate_sf_address(value, label="收件详细地址", field="address"):
	"""Validate the trimmed address actually sent to SF; never truncate it."""
	address = _text(value)
	length = len(address)
	if length > SF_ADDRESS_MAX_LENGTH:
		raise LabelInputError(
			f"{label}最多 {SF_ADDRESS_MAX_LENGTH} 个字符，当前 {length} 个，请修改后再提交（空格和标点也计入）。",
			[field],
		)
	return address


def postcode_candidates(rows, receiver):
	"""Keep only actual, destination-compatible carrier rows, never invent regions."""
	country = _text(receiver.get("country")).upper()
	postcode = normalize_postcode(receiver.get("post_code"))
	if not country or not postcode:
		return []
	result, seen = [], set()
	for row in rows or []:
		if not isinstance(row, Mapping):
			continue
		if _text(row.get("country")).upper() != country:
			continue
		api_postcode = normalize_postcode(row.get("post_code"))
		zip5_match = (
			country == "US"
			and bool(re.fullmatch(r"[0-9]{9}", postcode))
			and bool(re.fullmatch(r"[0-9]{5}", api_postcode))
			and postcode[:5] == api_postcode
		)
		if api_postcode != postcode and not zip5_match:
			continue
		province, city = _text(row.get("province")), _text(row.get("city"))
		if not province or not city:
			continue
		county = _text(row.get("county"))
		key = (country, province.casefold(), city.casefold(), county.casefold(), postcode)
		candidate = {
			"country": country,
			"province": province,
			"city": city,
			"county": county,
			"post_code": _text(receiver.get("post_code")),
		}
		if zip5_match:
			candidate["warning"] = "顺丰返回该地址对应的 5 位邮编地区；下单仍保留原完整邮编，请核对省市。"
			candidate["source_post_code"] = _text(row.get("post_code"))
		if key in seen:
			# Prefer an exact ZIP+4 response if a less specific duplicate came first.
			if not zip5_match:
				for index, old in enumerate(result):
					old_key = (old["country"], old["province"].casefold(), old["city"].casefold(), old["county"].casefold(), postcode)
					if old_key == key and old.get("warning"):
						result[index] = candidate
						break
			continue
		seen.add(key)
		result.append(candidate)
	return result


def _positive_number(value):
	if isinstance(value, bool) or value is None:
		raise ValueError("not a positive number")
	try:
		number = float(value)
	except (ValueError, TypeError, OverflowError):
		raise ValueError("not a positive number") from None
	if not math.isfinite(number) or number <= 0:
		raise ValueError("not a positive number")
	return number


def _positive_integer(value):
	if isinstance(value, bool) or value is None:
		raise ValueError("not a positive integer")
	try:
		number = Decimal(str(value))
	except (InvalidOperation, ValueError, TypeError):
		raise ValueError("not a positive integer") from None
	if not number.is_finite() or number <= 0 or number != number.to_integral_value():
		raise ValueError("not a positive integer")
	# Frappe's parcel count is an Int field; reject values the database cannot save.
	if number > 2147483647:
		raise ValueError("not a supported positive integer")
	return int(number)


def validate_parcels(rows):
	if not isinstance(rows, (list, tuple)) or not rows:
		raise LabelInputError("请补齐包裹：每种包裹的长、宽、高（厘米）、单件重量（千克）及件数。", ["parcels"])
	result, errors, fields = [], [], []
	labels = {"length": "长", "width": "宽", "height": "高", "weight": "单件重量", "count": "件数"}
	for index, row in enumerate(rows):
		if not isinstance(row, Mapping):
			errors.append(f"第 {index + 1} 种包裹的格式无效")
			fields.append(f"parcels[{index}]")
			continue
		parcel = {}
		for field, label in labels.items():
			try:
				parcel[field] = _positive_integer(row.get(field)) if field == "count" else _positive_number(row.get(field))
			except ValueError:
				rule = "须为正整数" if field == "count" else "须为大于 0 的有限数值"
				errors.append(f"第 {index + 1} 种包裹的{label}{rule}")
				fields.append(f"parcels[{index}].{field}")
		result.append(parcel)
	if errors:
		raise LabelInputError("；".join(errors) + "。", fields)
	return result


def parcel_totals(rows):
	rows = validate_parcels(rows)
	try:
		weight = math.fsum(row["weight"] * row["count"] for row in rows)
	except OverflowError:
		weight = float("inf")
	if not math.isfinite(weight):
		raise LabelInputError("包裹总重量超出有效范围，请核对单件重量和件数。", ["parcels"])
	quantity = sum(row["count"] for row in rows)
	if quantity > 2147483647:
		raise LabelInputError("包裹总件数超出系统支持范围，请核对件数。", ["parcels"])
	return {
		"total_weight": weight,
		"parcel_quantity": quantity,
		"length": max(row["length"] for row in rows),
		"width": max(row["width"] for row in rows),
		"height": max(row["height"] for row in rows),
	}


def apply_details(receiver, details=None):
	allowed = {"company", "contact", "phone", "mobile", "email", "doorplate"}
	if details is not None and not isinstance(details, Mapping):
		raise LabelInputError("收件人补充信息须为字段对象。", ["receiver_details"])
	unknown = set(details or {}) - allowed
	if unknown:
		raise LabelInputError("收件国家、省市、邮编及街道不能由补充信息覆盖；请重新选择地址候选。", sorted(unknown))
	result = dict(receiver)
	for key, value in (details or {}).items():
		if not isinstance(value, str):
			raise LabelInputError(f"收件人字段 {key} 须为文本。", [key])
		result[key] = value.strip()
	labels = {"contact": "收件人", "phone": "收件电话", "country": "国家", "province": "省/州", "city": "城市", "address": "详细街道地址"}
	missing = [key for key in labels if not _text(result.get(key))]
	if missing:
		raise LabelInputError("请补齐：" + "、".join(labels[key] for key in missing) + "。", missing)
	result["address"] = validate_sf_address(result["address"])
	return result


def validate_customs(values):
	allowed = {"declared_value", "declared_currency", "purchase_currency", "hs_code", "ename", "cname"}
	if not isinstance(values, Mapping):
		raise LabelInputError("请补齐报关参数。", ["customs"])
	unknown = set(values) - allowed
	if unknown:
		raise LabelInputError("包含不支持的报关参数，请仅填写审核表中的字段。", sorted(unknown))
	result, errors, fields = {}, [], []
	try:
		result["declared_value"] = _positive_number(values.get("declared_value"))
	except ValueError:
		errors.append("申报金额须为大于 0 的有限数值")
		fields.append("declared_value")
	for key, label in (("declared_currency", "申报币种"), ("purchase_currency", "采购币种")):
		value = values.get(key)
		if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value.strip()):
			errors.append(f"{label}须为 3 位大写英文字母代码")
			fields.append(key)
		else:
			result[key] = value.strip()
	for key, label in (("hs_code", "HS 编码"), ("ename", "英文品名"), ("cname", "中文品名")):
		value = values.get(key)
		if not isinstance(value, str) or not value.strip():
			errors.append(f"请补齐{label}")
			fields.append(key)
		elif key in {"ename", "cname"} and len(value.strip()) > 100:
			errors.append(f"{label}超过顺丰接口的 100 字符限制")
			fields.append(key)
		else:
			result[key] = value.strip()
	if errors:
		raise LabelInputError("；".join(errors) + "。", fields)
	return result
