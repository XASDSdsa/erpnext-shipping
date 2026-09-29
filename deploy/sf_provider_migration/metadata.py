#!/usr/bin/env python3
"""Move existing module registrations, preserving business rows and schema.

This is a release operation, not an application install hook. Both the previous
and candidate images execute this same Git-pinned script for exact rollback.
No carrier API, business installer, catalog repair or full migration is run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import frappe
from frappe.utils.response import json_handler

SITES = Path("/home/frappe/frappe-bench/sites")
DOCTYPES = ["SF International Settings", "SF International Product", "SF Waybill", "PayPal Receipt Record"]
OLD_SCRIPTS = ["Sales Order Freight List", "Shipment Label Open Guard", "Delivery Note Shipment List", "Delivery Note Shipment Status"]
SINGLE_SCOPE = "doctype=%s OR (doctype=%s AND field IN (%s,%s,%s))"
SINGLE_VALUES = ("Installed Applications", "System Settings", "setup_complete", "modified", "modified_by")
TOOL_NAMES = [
    "activate_sf_waybill_replacement", "record_sf_waybill_replacement_feedback",
    "create_sf_waybill_replacement", "preview_sf_waybill_replacement", "sync_sf_tracking",
    "save_sales_invoice", "preview_sales_invoice", "get_sales_invoice_options",
    "save_purchase_receipt", "preview_purchase_receipt", "get_purchase_receipt_options",
    "get_sf_label_result", "create_sf_label", "preview_sf_label", "list_sf_label_addresses",
    "prepare_sf_label", "create_delivery_note_draft", "preview_delivery_note",
    "get_delivery_note_options", "create_sales_order_draft", "preview_sales_order",
    "preview_customer_profile", "paypal_receipt_procedure", "create_customer_sticker_variant",
    "create_or_reuse_customer_profile", "lookup_sf_postcode", "recreate_sf_waybill",
    "cancel_sf_waybill", "dispatch_sf_shipment", "print_sf_label", "book_sf_waybill",
    "query_sf_tracking", "query_sf_freight", "get_sf_shipment_status",
]
FLOW_SALES_ORDER_QUERY_SLUG = "query_sales_order_details"
FLOW_SALES_ORDER_QUERY_PATH = "flow.integrations.erpnext.sales_order_flow.query_sales_order_details"
BUSINESS = [
    "Shipment", "Shipment Parcel", "Shipment Delivery Note", "Delivery Note", "Delivery Note Item",
    "Sales Order", "Sales Order Item", "Packed Item", "SF Waybill", "SF International Product",
    "PayPal Receipt Record", "GL Entry", "Stock Ledger Entry", "Payment Entry", "Payment Entry Reference",
    "Journal Entry", "Journal Entry Account", "Sales Invoice", "Sales Invoice Item", "Purchase Receipt",
    "Purchase Receipt Item", "Customer", "Address", "Contact", "Item", "Item Attribute", "Item Attribute Value",
    "Product Bundle", "Product Bundle Item", "Integration Request", "Flow Agent", "Flow Agent Tool",
]


def encoded(value):
    return json.dumps(value, default=json_handler, sort_keys=True, ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def clear_runtime_cache():
    # Resolve hooks and module ownership from this image before loading any
    # transferred controller. An old Redis app_hooks map is not a compatibility API.
    frappe.client_cache.delete_value("app_hooks")
    frappe.client_cache.delete_value("installed_app_modules")
    frappe.cache.delete_value("app_modules")
    frappe.local.doc_events_hooks = None
    frappe.local.module_app = None
    frappe.controllers.pop(frappe.local.site, None)
    frappe.lazy_controllers.pop(frappe.local.site, None)
    frappe.clear_cache()
    frappe.setup_module_map()


def business():
    result = {}
    for doctype in BUSINESS:
        if frappe.db.table_exists(doctype):
            rows = frappe.get_all(doctype, fields=["*"], order_by="name")
            schema = frappe.db.sql("SHOW CREATE TABLE `tab" + doctype + "`")[0][1]
            result[doctype] = {"count": len(rows), "rows_sha256": digest(rows), "schema_sha256": digest(schema)}
    result["SF International Settings"] = digest(frappe.db.sql(
        "SELECT * FROM tabSingles WHERE doctype=%s ORDER BY field", ("SF International Settings",), as_dict=True))
    # Only digests leave this function. Never print or copy decrypted credentials.
    result["credentials"] = digest(frappe.db.sql("SELECT * FROM __Auth ORDER BY doctype,name,fieldname", as_dict=True))
    result["encryption_key"] = digest(frappe.conf.get("encryption_key"))
    return result


def scopes():
    result = [{"doctype": "DocType", "filters": {"name": ["in", DOCTYPES]}}]
    for field in frappe.get_meta("DocType").get_table_fields():
        result.append({"doctype": field.options, "filters": {"parent": ["in", DOCTYPES], "parenttype": "DocType"}})
    flow_agents = frappe.get_all("Flow Agent", filters={"title": ["in", ["Flow", "销售助理"]]}, pluck="name")
    result.extend([
        {"doctype": "Module Def", "filters": {"name": "SF International"}},
        {"doctype": "Custom Field", "filters": {}},
        {"doctype": "Property Setter", "filters": {}},
        {"doctype": "Custom DocPerm", "filters": {"parent": ["in", DOCTYPES]}},
        {"doctype": "Flow Tool", "filters": {"name": ["in", TOOL_NAMES]}},
        {"doctype": "Flow Tool", "filters": {"slug": FLOW_SALES_ORDER_QUERY_SLUG}},
        {"doctype": "Flow Agent", "filters": {"name": ["in", flow_agents]}},
        {"doctype": "Flow Agent Tool", "filters": {"parent": ["in", flow_agents], "parenttype": "Flow Agent"}},
        {"doctype": "Client Script", "filters": {"name": ["in", OLD_SCRIPTS]}},
        {"doctype": "Workspace Sidebar", "filters": {"name": ["in", ["Shipping", "SF International"]]}},
        {"doctype": "Desktop Icon", "filters": {"name": ["in", ["Shipping", "SF International"]]}},
        {"doctype": "DefaultValue", "filters": {"defkey": "installed_apps"}},
        {"doctype": "Installed Application", "filters": {"parent": "Installed Applications"}},
    ])
    for doctype in ("Workspace Sidebar", "Desktop Icon"):
        for field in frappe.get_meta(doctype).get_table_fields():
            result.append({"doctype": field.options, "filters": {"parent": ["in", ["Shipping", "SF International"]], "parenttype": doctype}})
    return result


def config_apps():
    config = json.loads((SITES / frappe.local.site / "site_config.json").read_text())
    return {"exists": "installed_apps" in config, "value": config.get("installed_apps")}


def singles():
    return frappe.db.sql("SELECT * FROM tabSingles WHERE " + SINGLE_SCOPE + " ORDER BY doctype,field",
        SINGLE_VALUES, as_dict=True)


def capture():
    groups = [{**scope, "rows": frappe.get_all(scope["doctype"], filters=scope["filters"], fields=["*"], order_by="name")} for scope in scopes()]
    return {"site": frappe.local.site, "metadata": groups, "business": business(), "config_apps": config_apps(), "singles": singles()}


def semantic(snapshot):
    # Exclude only native import audit timestamps and generated child row names.
    groups = []
    for group in snapshot["metadata"]:
        rows = [{k: v for k, v in row.items() if k not in {"creation", "modified", "modified_by"}} for row in group["rows"]]
        groups.append({**group, "rows": rows})
    return digest({"metadata": groups, "config_apps": snapshot["config_apps"], "singles": snapshot["singles"]})


def field_contracts():
    """Changing an app owner must never silently change the existing schema."""
    sources = {}
    for doctype in DOCTYPES:
        folder = frappe.scrub(doctype)
        app, module = ("erpnext", "accounts") if doctype == "PayPal Receipt Record" else ("erpnext_shipping", "sf_international")
        path = Path(frappe.get_app_path(app, module, "doctype", folder, folder + ".json"))
        source = json.loads(path.read_text())
        actual = {row.fieldname: row for row in frappe.get_all("DocField", filters={"parent": doctype, "parenttype": "DocType"}, fields=["*"])}
        assert set(actual) == {row["fieldname"] for row in source["fields"]}, "doctype_fields_changed:" + doctype
        for row in source["fields"]:
            for key in ("fieldtype", "options", "unique", "reqd", "precision"):
                assert str(actual[row["fieldname"]].get(key) or "") == str(row.get(key) or ""), "doctype_field_contract_changed:" + doctype + ":" + row["fieldname"] + ":" + key
        sources[doctype] = (path, source)
    return sources


def freight_options():
    path = Path(frappe.get_app_path("erpnext_shipping", "sf_international", "custom_fields.json"))
    fields = json.loads(path.read_text())["Shipment"]
    return next(row["options"] for row in fields if row["fieldname"] == "sf_freight_status")


def tool_contract(snapshot):
    from flow.integrations.erpnext.install import LEGACY_TOOL_PATHS

    original = next(group["rows"] for group in snapshot["metadata"] if group["doctype"] == "Flow Tool")
    assert {row["name"] for row in original} == set(TOOL_NAMES), "unexpected_legacy_tool_inventory"
    known_paths = set(LEGACY_TOOL_PATHS) | set(LEGACY_TOOL_PATHS.values())
    for row in original:
        expected = dict(row)
        # A release can be applied to a site that already completed the ownership
        # migration. Preserve the current Flow path in that case; only rewrite the
        # retired SF path. Unknown paths still fail closed below.
        assert row["import_path"] in known_paths, "unknown_flow_tool_path:" + row["name"]
        expected["import_path"] = LEGACY_TOOL_PATHS.get(row["import_path"], row["import_path"])
        actual = frappe.get_all("Flow Tool", filters={"name": row["name"]}, fields=["*"])
        assert len(actual) == 1 and encoded(actual[0]) == encoded(expected), "tool_settings_changed:" + row["name"]
        assert callable(frappe.get_attr(expected["import_path"])), "unresolved_tool:" + row["name"]
    assert not frappe.get_all("Flow Tool", filters={"import_path": ["like", "sf_international.%"]}, pluck="name"), "legacy_tool_path_remaining"


def validate_flow_sales_order_query():
    """Verify the source-controlled Flow query tool after the release migration."""
    tool = frappe.get_all(
        "Flow Tool",
        filters={"slug": FLOW_SALES_ORDER_QUERY_SLUG},
        fields=["name", "type", "code", "import_path", "enabled", "requires_confirmation"],
    )
    assert len(tool) == 1, "sales_order_query_tool_missing"
    row = tool[0]
    assert row.type == "Imported", "sales_order_query_tool_not_imported"
    assert not row.code and row.import_path == FLOW_SALES_ORDER_QUERY_PATH, "sales_order_query_tool_path_invalid"
    assert int(row.enabled or 0) == 1 and int(row.requires_confirmation or 0) == 0, "sales_order_query_tool_disabled"
    for title in ("Flow", "销售助理"):
        name = frappe.db.get_value("Flow Agent", {"title": title}, "name") or (
            title if frappe.db.exists("Flow Agent", title) else None
        )
        assert name, "sales_order_query_agent_missing:" + title
        agent = frappe.get_doc("Flow Agent", name)
        assert any(item.tool == row.name for item in agent.get("tools") or []), "sales_order_query_agent_unbound:" + title
    assert callable(frappe.get_attr(FLOW_SALES_ORDER_QUERY_PATH)), "sales_order_query_tool_unresolved"


def sync_flow_sales_order_query():
    """Run the Flow-owned metadata migration through its explicit public interface."""
    from flow.integrations.erpnext.sales_order_install import install_sales_order_query_tool

    result = install_sales_order_query_tool(enable=True)
    assert result.get("installed") and result.get("enabled"), "sales_order_query_tool_install_failed"
    frappe.db.commit()
    validate_flow_sales_order_query()
    return result


def business_before_flow_query(snapshot):
    """Compare business rows while allowing only an already-installed query binding."""
    current = business()
    expected = dict(snapshot["business"])
    tool = frappe.db.get_value("Flow Tool", {"slug": FLOW_SALES_ORDER_QUERY_SLUG}, ["type", "import_path"], as_dict=True)
    if tool and tool.type == "Imported" and tool.import_path == FLOW_SALES_ORDER_QUERY_PATH:
        for doctype in ("Flow Agent", "Flow Agent Tool"):
            current.pop(doctype, None)
            expected.pop(doctype, None)
    assert current == expected, "business_changed_before_migration"


def validate(snapshot, *, flow_query=False):
    from frappe.model.base_document import get_controller
    from frappe.modules.utils import get_module_app

    current_business = business()
    expected_business = dict(snapshot["business"])
    if flow_query:
        # The release explicitly adds one Flow tool and its two Agent bindings;
        # all ERP, stock, accounting and customer rows remain strict.
        for doctype in ("Flow Agent", "Flow Agent Tool"):
            current_business.pop(doctype, None)
            expected_business.pop(doctype, None)
    assert current_business == expected_business, "business_rows_schema_or_credentials_changed"
    assert "sf_international" not in frappe.get_installed_apps(), "legacy_app_still_installed"
    assert get_module_app("SF International") == "erpnext_shipping", "wrong_sf_module_owner"
    assert frappe.db.get_value("Module Def", "SF International", "app_name") == "erpnext_shipping"
    for doctype in DOCTYPES:
        expected = "erpnext.accounts.doctype." if doctype == "PayPal Receipt Record" else "erpnext_shipping.sf_international.doctype."
        assert get_controller(doctype).__module__.startswith(expected), "wrong_controller:" + doctype
    assert not frappe.db.exists("Workspace Sidebar", "SF International") and not frappe.db.exists("Desktop Icon", "SF International"), "old_navigation_remaining"
    sidebar = frappe.get_doc("Workspace Sidebar", "Shipping")
    assert sidebar.app == "erpnext_shipping"
    assert sum(row.link_to == "SF International Settings" for row in sidebar.items) == 1, "sf_settings_navigation_count"
    adapters = frappe.get_hooks("shipment_carrier_adapters")
    assert adapters.count("erpnext_shipping.sf_international.carrier_adapter") == 1
    assert not any(path.startswith("sf_international.") for path in adapters)
    assert frappe.get_meta("Shipment", cached=False).get_field("sf_freight_status").options == freight_options(), "freight_status_options_not_synchronized"
    assert "erpnext_shipping.sf_international.shipping.book_sf_order_after_insert" in frappe.get_hooks("doc_events").get("Shipment", {}).get("after_insert", []), "initial_booking_hook_missing"
    tool_contract(snapshot)
    if flow_query:
        validate_flow_sales_order_query()
    for doctype, key in (("Shipment", "shipment_contents"), ("Delivery Note", "shipping_state")):
        names = frappe.get_all(doctype, filters={"docstatus": ["!=", 2]}, pluck="name", limit=1)
        if names:
            doc = frappe.get_doc(doctype, names[0]); doc.run_method("onload")
            assert key in (doc.get("__onload") or {}), "missing_native_onload:" + doctype
    after_onload = business()
    expected_after_onload = dict(snapshot["business"])
    if flow_query:
        for doctype in ("Flow Agent", "Flow Agent Tool"):
            after_onload.pop(doctype, None)
            expected_after_onload.pop(doctype, None)
    assert after_onload == expected_after_onload, "onload_changed_business_rows"
    assert config_apps()["value"] == frappe.get_installed_apps(), "site_config_apps_not_synchronized"
    print("OWNERSHIP_TOOLS_NAVIGATION_AND_BUSINESS_VALIDATED")


def migrate(snapshot, inject_failure=False):
    from frappe.installer import remove_from_installed_apps
    from frappe.modules.import_file import import_file_by_path
    from flow.integrations.erpnext.install import LEGACY_TOOL_PATHS, migrate_legacy_tool_paths

    business_before_flow_query(snapshot)
    if "sf_international" not in frappe.get_installed_apps():
        validate(snapshot)
        result = sync_flow_sales_order_query()
        validate(snapshot, flow_query=True)
        return {"already_migrated": True, "flow_sales_order_query": result}
    assert {"erpnext", "flow", "erpnext_shipping"} <= set(frappe.get_installed_apps()), "target_apps_missing"
    assert frappe.db.get_value("Module Def", "SF International", "app_name") == "sf_international", "unexpected_initial_module_owner"
    field_contracts()
    for path in LEGACY_TOOL_PATHS.values():
        assert callable(frappe.get_attr(path)), "unresolved_target_tool:" + path
    existing = frappe.get_all("Flow Tool", filters={"import_path": ["like", "sf_international.%"]}, fields=["name", "import_path"])
    assert {row.name for row in existing} == set(TOOL_NAMES)
    assert all(row.import_path in LEGACY_TOOL_PATHS for row in existing)
    for doctype in ("Workspace Sidebar", "Desktop Icon"):
        assert frappe.db.get_value(doctype, "SF International", "app") == "sf_international", "unexpected_old_navigation_owner"
        assert frappe.get_all(doctype, filters={"app": "sf_international"}, pluck="name") == ["SF International"], "unexpected_legacy_navigation:" + doctype
    for row in frappe.get_all("Client Script", filters={"name": ["in", OLD_SCRIPTS]}, fields=["name", "enabled"]):
        assert not row.enabled, "legacy_script_reenabled:" + row.name
    freight = frappe.get_all("Custom Field", filters={"dt": "Shipment", "fieldname": "sf_freight_status"}, fields=["name", "options"])
    assert len(freight) == 1 and freight[0].options in {"\n未结算\n已结算", freight_options()}, "unexpected_freight_options"
    assert not frappe.db.exists("Property Setter", {"doc_type": "Shipment", "field_name": "sf_freight_status", "property": "options"}), "freight_options_overridden"

    # The document definitions are structurally identical. Transfer ownership
    # without table sync, business hooks or permission reset.
    frappe.db.set_value("Module Def", "SF International", "app_name", "erpnext_shipping", update_modified=False)
    frappe.db.set_value("DocType", "PayPal Receipt Record", "module", "Accounts", update_modified=False)
    frappe.db.commit()
    if inject_failure:
        raise RuntimeError("ISOLATED_INJECTED_FAILURE_AFTER_OWNER")
    clear_runtime_cache()
    # sync_freight_status writes these current states. Keep legacy stored values
    # valid too; only extend the Select definition, never rewrite shipment rows.
    frappe.db.set_value("Custom Field", freight[0].name, "options", freight_options(), update_modified=False)
    changed_tools = migrate_legacy_tool_paths()
    path = frappe.get_app_path("erpnext_shipping", "workspace_sidebar", "shipping.json")
    assert import_file_by_path(path, force=True, ignore_version=True), "shipping_sidebar_not_imported"
    for doctype in ("Workspace Sidebar", "Desktop Icon"):
        for field in frappe.get_meta(doctype).get_table_fields():
            frappe.db.delete(field.options, {"parent": "SF International", "parenttype": doctype})
        frappe.db.delete(doctype, {"name": "SF International"})
    frappe.db.delete("Client Script", {"name": ["in", OLD_SCRIPTS]})
    # Native app-list removal retains documents. Native uninstall would delete
    # them, and therefore must never be used for this ownership migration.
    remove_from_installed_apps("sf_international")
    frappe.db.commit()
    clear_runtime_cache()
    validate(snapshot)
    result = sync_flow_sales_order_query()
    validate(snapshot, flow_query=True)
    return {"tools": changed_tools, "module_owner": "erpnext_shipping", "retired_app": "sf_international", "flow_sales_order_query": result}


def restore(snapshot):
    assert snapshot["site"] == frappe.local.site
    assert business() == snapshot["business"], "rollback_refused_business_or_schema_changed"
    for group in reversed(snapshot["metadata"]):
        frappe.db.delete(group["doctype"], group["filters"])
    for group in snapshot["metadata"]:
        if group["rows"]:
            fields = list(group["rows"][0])
            frappe.db.bulk_insert(group["doctype"], fields, [tuple(row[field] for field in fields) for row in group["rows"]])
    frappe.db.sql("DELETE FROM tabSingles WHERE " + SINGLE_SCOPE, SINGLE_VALUES)
    if snapshot["singles"]:
        fields = list(snapshot["singles"][0])
        frappe.db.bulk_insert("Singles", fields, [tuple(row[field] for field in fields) for row in snapshot["singles"]])
    frappe.db.commit()
    from frappe.installer import update_site_config
    previous = snapshot["config_apps"]
    update_site_config("installed_apps", previous["value"] if previous["exists"] else "None")
    clear_runtime_cache()
    assert encoded(capture()) == encoded(snapshot), "metadata_restore_not_exact"
    print("EXACT_METADATA_ROLLBACK_OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["snapshot", "migrate", "validate", "restore", "compare", "fail-after-owner"])
    parser.add_argument("--site", required=True)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--db-host", required=True)
    args = parser.parse_args()
    if args.mode == "fail-after-owner":
        assert args.db_host.startswith("shipment-check-"), "failure_injection_requires_isolated_database"
    args.snapshot = args.snapshot.resolve()
    os.chdir(SITES)
    for directory in (SITES.parent / "logs", SITES / args.site / "logs"):
        assert directory.is_dir() and os.access(directory, os.W_OK), "missing_or_unwritable_log_directory"
    frappe.init(args.site, sites_path=str(SITES))
    assert frappe.conf.db_host == args.db_host, "unexpected_database_host"
    if args.db_host.startswith("shipment-check-"):
        assert all((urlparse(frappe.conf.get(key) or "").hostname or "").startswith("shipment-check-") for key in ("redis_cache", "redis_queue", "redis_socketio")), "unexpected_isolation_redis_host"
    frappe.connect(); frappe.set_user("Administrator")
    try:
        clear_runtime_cache()
        if args.mode == "snapshot":
            assert not args.snapshot.exists(), "snapshot_exists"
            temporary = args.snapshot.with_suffix(".pending")
            temporary.write_bytes(encoded(capture())); temporary.chmod(0o600)
            os.replace(temporary, args.snapshot)
            print("METADATA_SNAPSHOT", hashlib.sha256(args.snapshot.read_bytes()).hexdigest())
            return
        snapshot = json.loads(args.snapshot.read_text())
        assert snapshot["site"] == args.site
        if args.mode == "restore":
            restore(snapshot)
        elif args.mode == "validate":
            validate(snapshot, flow_query=True)
        elif args.mode == "compare":
            assert encoded(capture()) == encoded(snapshot), "snapshot_differs"
            print("SNAPSHOT_EXACT_MATCH")
        else:
            before = semantic(capture())
            result = migrate(snapshot, args.mode == "fail-after-owner")
            after = semantic(capture())
            print(json.dumps({"result": "MIGRATION_OK", "before": before, "after": after, "actions": result}, default=json_handler))
    finally:
        frappe.destroy()


if __name__ == "__main__":
    main()
