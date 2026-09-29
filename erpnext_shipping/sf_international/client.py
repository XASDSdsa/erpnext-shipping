# SF International IUOP merchant / IECS web APIs.
# Spec: IUOP 商家后台 createOrder, queryOrderInfoWeb, printLabel, queryRouteDetailsbyId, queryCaseOrders.
import json
import time

import frappe
import requests
from frappe import _
from frappe.utils.password import get_decrypted_password

IECS_HOST = "https://http-iecs.sf-international.com"
IPCS_HOST = "https://iuop-ipcs.trackmeeasy.com"


def get_settings():
	return frappe.get_single("SF International Settings")


def is_enabled():
	return bool(frappe.db.get_single_value("SF International Settings", "enabled"))


def _password(field: str) -> str:
	return (
		get_decrypted_password(
			"SF International Settings",
			"SF International Settings",
			field,
			raise_exception=False,
		)
		or ""
	).strip()


def _token(settings=None) -> str:
	token = _password("iuop_token") or _password("pms_token")
	if not token:
		frappe.throw(_("SF International token is not configured."))
	return token


def _pms_token(settings=None) -> str:
	return _password("pms_token") or _password("iuop_token")


def headers(extra: bool = False, settings=None) -> dict:
	settings = settings or get_settings()
	token = _token(settings)
	out = {
		"Accept": "application/json",
		"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
		"Origin": "https://iuop.sf-express.com",
		"Referer": "https://iuop.sf-express.com/",
		"sf_partner": "iuop",
		"sflang": "zh-CN",
		"token": token,
		"pms_token": _pms_token(settings),
	}
	if extra:
		out["systemcode"] = "IBU-IUOP-CORE"
		out["intl_ipcs_gray_version"] = "green"
		if settings.customer_code:
			out["customercode"] = str(settings.customer_code).strip()
		if settings.username:
			out["username"] = str(settings.username).strip()
		if settings.userid_mod_10:
			out["x-iuop-userid-mod-10"] = str(settings.userid_mod_10).strip()
	return out


def _ok_http(response: requests.Response):
	if response.status_code < 200 or response.status_code >= 300:
		if response.status_code == 504:
			frappe.throw(_("SF International gateway timed out (HTTP {0}). Please try again later.").format(response.status_code))
		frappe.throw(_("SF International request failed (HTTP {0}).").format(response.status_code))


def _json(response: requests.Response) -> dict:
	_ok_http(response)
	try:
		payload = response.json()
	except Exception:
		frappe.throw(_("SF International returned a non-JSON response."))
	if not isinstance(payload, dict):
		frappe.throw(_("SF International returned an invalid response."))
	return payload


def _business(payload: dict, need_data: bool = True) -> dict:
	code = payload.get("code")
	if str(code) != "200":
		frappe.throw(_("SF International business error [{0}]: {1}").format(code, _(payload.get("msg") or "Unknown error")))
	data = payload.get("data")
	if need_data and data is None:
		frappe.throw(_("SF International returned empty data."))
	return payload


def create_order(body: dict) -> dict:
	settings = get_settings()
	response = requests.post(
		f"{IECS_HOST}/sf-express/order/createOrder",
		json=body,
		headers={**headers(extra=True, settings=settings), "Content-Type": "application/json;charset=UTF-8"},
		timeout=60,
	)
	payload = _business(_json(response))
	data = payload.get("data") or {}
	tracking = data.get("trackingNo")
	if not tracking:
		frappe.throw(_("SF International did not return trackingNo."))
	return payload


def cancel_order(waybill_nos) -> dict:
	if isinstance(waybill_nos, str):
		waybill_nos = [waybill_nos]
	nos = [str(no).strip() for no in (waybill_nos or []) if no is not None and str(no).strip()]
	if not nos:
		frappe.throw(_("SF International waybill number is required to cancel."))
	settings = get_settings()
	head = {**headers(extra=True, settings=settings), "Content-Type": "application/json;charset=UTF-8"}
	last_status = 0
	for host in (IPCS_HOST, IECS_HOST):
		try:
			response = requests.post(
				f"{host}/sf-express/order/cancel",
				json={"waybillNos": nos},
				headers=head,
				timeout=25,
			)
		except requests.RequestException:
			last_status = 0
			continue
		body = response.text or ""
		if response.status_code in (502, 503, 504) or "Gateway Time-out" in body or "stgw" in body:
			last_status = response.status_code
			continue
		if response.status_code != 200:
			frappe.throw(_("SF International HTTP {0}: {1}").format(response.status_code, body[:300]))
		try:
			payload = response.json()
		except Exception:
			frappe.throw(_("SF International returned a non-JSON response."))
		if not isinstance(payload, dict):
			frappe.throw(_("SF International returned a non-JSON response."))
		msg = str(payload.get("msg") or "")
		if str(payload.get("code")) == "200" or _already_cancelled_msg(msg):
			return payload if str(payload.get("code")) == "200" else {"code": "200", "msg": msg, "data": payload.get("data")}
		frappe.throw(
			_("SF International business error [{0}]: {1}").format(payload.get("code"), _(msg or "Unknown error"))
		)
	frappe.throw(_("顺丰取消接口超时（HTTP {0}）。请稍等几秒再点一次「取消面单」，不要整单作废。").format(last_status or 504))


def _already_cancelled_msg(msg: str) -> bool:
	text = " ".join((msg or "").lower().split()).rstrip(".!。！")
	# Error messages can mention cancellation without confirming it.
	return text in {
		"已取消", "已经取消", "订单已取消", "订单已经取消", "运单已取消", "运单已经取消",
		"该订单已取消", "该订单已经取消", "该运单已取消", "该运单已经取消",
		"already cancelled", "already canceled", "order already cancelled", "order already canceled",
		"order has already been cancelled", "order has already been canceled",
		"the order has already been cancelled", "the order has already been canceled",
	}


def query_order(waybill: str) -> dict:
	waybill = str(waybill or "").strip()
	if not waybill:
		frappe.throw(_("SF International order {0} was not found.").format(waybill))
	settings = get_settings()
	body = {
		"currentPage": 1,
		"pageSize": 10,
		"orderStatus": "",
		"historyStatus": "0",
		"createdTime": None,
		"sfOrderId": [waybill],
		"isSpecial": True,
		"isIntercepted": "",
		"notPrinted": 0,
		"cancelOrder": 0,
		"field": "",
		"order": "",
	}
	response = requests.post(
		f"{IPCS_HOST}/sf-express/order/queryOrderInfoWeb",
		json=body,
		headers={**headers(extra=True, settings=settings), "Content-Type": "application/json;charset=UTF-8"},
		timeout=45,
	)
	payload = _business(_json(response), need_data=False)
	data = payload.get("data")
	rows = (data.get("items") or data.get("list") or []) if isinstance(data, dict) else data
	for row in rows if isinstance(rows, list) else []:
		if isinstance(row, dict) and str(row.get("trackingNo") or "").strip() == waybill:
			return row
	frappe.throw(_("SF International order {0} was not found.").format(waybill))


CANCELLED_ORDER_STATUSES = {
	"已取消", "订单已取消", "运单已取消", "已作废", "订单已作废",
	"cancelled", "canceled", "order cancelled", "order canceled",
}


def cancellation_evidence(row, waybill):
	if not isinstance(row, dict):
		return None
	identifiers = {str(row[k]).strip() for k in ("trackingNo", "waybillNo", "sfWaybillNo") if row.get(k)}
	if not waybill or identifiers != {waybill}:
		return None
	for field in ("orderStatus", "status", "cancelStatus"):
		value = " ".join(str(row.get(field) or "").lower().split()).rstrip(".!。！")
		if value in CANCELLED_ORDER_STATUSES:
			return {"waybill": waybill, "trackingNo": waybill, "status": value, "status_field": field}
	return None


def query_order_cancellation(waybill: str) -> dict:
	"""Read carrier history and confirm cancellation only from an explicit status."""
	waybill = str(waybill or "").strip()
	if not waybill:
		return {"confirmed": False, "rows": []}
	settings = get_settings()
	rows = []
	for history_status, cancel_order in (("0", 1), ("1", 1), ("1", 0)):
		body = {
			"currentPage": 1,
			"pageSize": 20,
			"orderStatus": "",
			"historyStatus": history_status,
			"createdTime": None,
			"sfOrderId": [waybill],
			"isSpecial": True,
			"isIntercepted": "",
			"notPrinted": 0,
			"cancelOrder": cancel_order,
			"field": "",
			"order": "",
		}
		response = requests.post(
			f"{IPCS_HOST}/sf-express/order/queryOrderInfoWeb",
			json=body,
			headers={**headers(extra=True, settings=settings), "Content-Type": "application/json;charset=UTF-8"},
			timeout=20,
		)
		payload = _business(_json(response), need_data=False)
		data = payload.get("data")
		batch = (data.get("items") or data.get("list") or []) if isinstance(data, dict) else data
		if isinstance(batch, list):
			rows.extend(row for row in batch if isinstance(row, dict))

	seen = set()
	unique = []
	confirmed = None
	for row in rows:
		key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
		if key in seen:
			continue
		seen.add(key)
		identifiers = {str(row[k]).strip() for k in ("trackingNo", "waybillNo", "sfWaybillNo") if row.get(k)}
		if identifiers != {waybill}:
			continue
		evidence = {
			k: str(row.get(k)).strip()
			for k in ("trackingNo", "waybillNo", "sfWaybillNo", "orderStatus", "status", "cancelStatus")
			if row.get(k) not in (None, "")
		}
		evidence["waybill"] = waybill
		unique.append(evidence)
		confirmed = cancellation_evidence(row, waybill) or confirmed
	if confirmed:
		return {"confirmed": True, "evidence": confirmed, "rows": unique}
	return {"confirmed": False, "evidence": None, "rows": unique}


def start_print(order_id) -> dict:
	try:
		oid = int(order_id)
	except (TypeError, ValueError):
		oid = 0
	if oid <= 0:
		frappe.throw(_("SF International printLabel requires a numeric orderId."))
	settings = get_settings()
	response = requests.post(
		f"{IPCS_HOST}/sf-express/download-center/printLabel",
		json={"ids": [oid], "pickup": 0},
		headers={**headers(extra=True, settings=settings), "Content-Type": "application/json;charset=UTF-8"},
		timeout=45,
	)
	return _business(_json(response))


def get_download_url(task_id) -> dict:
	settings = get_settings()
	response = requests.get(
		f"{IPCS_HOST}/sf-express/download-center/getDownloadUrl",
		params={"id": str(task_id)},
		headers=headers(extra=True, settings=settings),
		timeout=45,
	)
	return _json(response)


def download_pdf(url: str, v2token: str) -> bytes:
	settings = get_settings()
	head = headers(extra=True, settings=settings)
	head["x-auth-token"] = v2token
	response = requests.get(url, headers=head, timeout=60)
	_ok_http(response)
	content = response.content or b""
	if not content.startswith(b"%PDF"):
		frappe.throw(_("SF International label is not a PDF."))
	return content


def query_route(order_id) -> dict:
	settings = get_settings()
	response = requests.get(
		f"{IPCS_HOST}/sf-express/order/queryRouteDetailsbyId",
		params={"orderId": order_id},
		headers=headers(extra=True, settings=settings),
		timeout=45,
	)
	return _business(_json(response), need_data=False)


def query_case_orders(waybill: str, begin: str | None = None, end: str | None = None) -> dict:
	settings = get_settings()
	search_time = None
	if begin and end:
		begin_s = begin if " " in str(begin) else f"{begin} 00:00:00"
		end_s = end if " " in str(end) else f"{end} 23:59:59"
		search_time = [begin_s, end_s]
	body = {
		"searchTime": search_time,
		"pageSize": 10,
		"orderId": "",
		"waybillNo": waybill,
		"currentPage": 1,
	}
	response = requests.post(
		f"{IPCS_HOST}/sf-express/financialManagement/queryCaseOrders",
		json=body,
		headers={**headers(extra=True, settings=settings), "Content-Type": "application/json;charset=UTF-8"},
		timeout=45,
	)
	return _business(_json(response), need_data=False)


def _form_post(path: str, data: dict) -> dict:
	settings = get_settings()
	response = requests.post(
		f"{IECS_HOST}{path}",
		data=data,
		headers={
			**headers(extra=True, settings=settings),
			"Content-Type": "application/x-www-form-urlencoded",
		},
		timeout=45,
	)
	return _json(response)


def _region_names(payload: dict) -> list[str]:
	data = payload.get("data")
	rows = []
	if isinstance(data, list):
		rows = data
	elif isinstance(data, dict):
		for key in ("items", "list", "regionList", "regions"):
			if isinstance(data.get(key), list):
				rows = data.get(key)
				break
	names = []
	seen = set()
	for row in rows or []:
		if isinstance(row, str):
			name = row.strip()
		elif isinstance(row, dict):
			name = str(
				row.get("regionName")
				or row.get("regionOneName")
				or row.get("regionTwoName")
				or row.get("name")
				or ""
			).strip()
		else:
			name = ""
		if name and name not in seen:
			seen.add(name)
			names.append(name)
	return names


def region_cascade(country_code: str, region_one_name: str = "") -> list[str]:
	data = {"pageSize": "1000", "countryCode": (country_code or "").strip().upper()}
	if region_one_name:
		data["regionOneName"] = region_one_name.strip()
	payload = _form_post("/sf-express/bycdao/ZoneDivision/RegionCascadeQuery", data)
	if str(payload.get("code")) not in ("200", "0", ""):
		return []
	return _region_names(payload)


def query_postcode(country_code: str, post_code: str) -> list[dict]:
	payload = _form_post(
		"/sf-express/bycdao/ZoneDivision/QueryPostCodeInfoVague",
		{
			"countryCode": (country_code or "").strip().upper(),
			"postCode": (post_code or "").strip(),
			"pageSize": "1000",
		},
	)
	if str(payload.get("code")) not in ("200", "0", ""):
		frappe.throw(
			_("SF International business error [{0}]: {1}").format(
				payload.get("code"), _(payload.get("msg") or "Unknown error")
			)
		)
	data = payload.get("data")
	rows = data if isinstance(data, list) else (data or {}).get("items") or (data or {}).get("list") or []
	out = []
	seen = set()
	for row in rows or []:
		if not isinstance(row, dict):
			continue
		item = {
			"country": str(row.get("countryCode") or country_code or "").upper(),
			"province": str(
				row.get("region1")
				or row.get("regionOneName")
				or row.get("province")
				or ""
			).strip(),
			"city": str(
				row.get("region2")
				or row.get("regionTwoName")
				or row.get("city")
				or ""
			).strip(),
			"county": str(
				row.get("region3")
				or row.get("regionThreeName")
				or row.get("county")
				or ""
			).strip(),
			"post_code": str(row.get("postCode") or post_code or "").strip(),
		}
		key = (item["country"], item["province"], item["city"], item["county"], item["post_code"])
		if key in seen:
			continue
		seen.add(key)
		out.append(item)
	return out


def poll_label(task_id, attempts: int = 5, wait: float = 2.0) -> tuple[str, str]:
	last = {}
	for attempt in range(attempts):
		if attempt:
			time.sleep(wait)
		last = get_download_url(task_id) or {}
		data = last.get("data") if str(last.get("code")) == "200" else None
		if data is None:
			data = {}
		url = data.get("downloadUrl") if isinstance(data, dict) else None
		token = data.get("v2token") if isinstance(data, dict) else None
		if url and token:
			return url, token
	if str(last.get("code")) != "200":
		frappe.throw(
			_("SF International business error [{0}]: {1}").format(
				last.get("code"), _(last.get("msg") or "Unknown error")
			)
		)
	frappe.throw(_("SF International label download timed out."))
