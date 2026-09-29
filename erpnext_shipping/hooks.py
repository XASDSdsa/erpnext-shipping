app_name = "erpnext_shipping"
app_title = "ERPNext Shipping"
app_publisher = "Frappe"
app_description = "A Shipping Integration fir ERPNext"
app_icon = "octicon octicon-file-directory"
app_color = "grey"
app_email = "developers@frappe.io"
app_license = "MIT"

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/erpnext_shipping/css/erpnext_shipping.css"
# app_include_js = "/assets/erpnext_shipping/js/erpnext_shipping.js"

# include js, css files in header of web template
# web_include_css = "/assets/erpnext_shipping/css/erpnext_shipping.css"
# web_include_js = "/assets/erpnext_shipping/js/erpnext_shipping.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "erpnext_shipping/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# Both extensions use the native Shipment carrier choice. The SF extension
# owns only SF controls; the rate selector remains LetMeShip/SendCloud-specific.
doctype_js = {
    "Shipment": ["public/js/shipment.js", "public/js/sf_international_shipment.js"],
    "Journal Entry": "public/js/sf_international_journal_entry.js",
}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# "Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Installation
# ------------

# before_install = "erpnext_shipping.install.before_install"
after_install = "erpnext_shipping.install.after_install"
after_migrate = "erpnext_shipping.sf_international.install.sync_metadata"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "erpnext_shipping.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# }
# }

# Scheduled Tasks
# ---------------

scheduler_events = {"daily": ["erpnext_shipping.erpnext_shipping.utils.update_tracking_info_daily"]}

# Testing
# -------

# before_tests = "erpnext_shipping.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "erpnext_shipping.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "erpnext_shipping.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Validate protected SF facts before native Shipment status calculation. Saving
# a new local Shipment queues its first SF request only after insertion/commit.
# Neither unrelated carriers nor generic Delivery Note behavior are overridden.
doc_events = {
    "Shipment": {
        "before_validate": [
            "erpnext_shipping.sf_international.freight_accounting.validate_shipment",
            "erpnext_shipping.sf_international.interception.validate_state",
        ],
        "validate": [
            "erpnext_shipping.erpnext_shipping.utils.validate_phone",
            "erpnext_shipping.sf_international.shipping.validate_phone",
        ],
        "before_save": "erpnext_shipping.sf_international.shipping.place_sf_order_on_save",
        "after_insert": "erpnext_shipping.sf_international.shipping.book_sf_order_after_insert",
        "before_submit": "erpnext_shipping.sf_international.shipping.fill_sf_fields_before_submit",
        "on_submit": "erpnext_shipping.sf_international.shipping.mark_sf_waiting_label",
        "before_update_after_submit": [
            "erpnext_shipping.sf_international.freight_accounting.validate_shipment",
            "erpnext_shipping.sf_international.interception.validate_state",
        ],
        "onload": [
            "erpnext_shipping.sf_international.shipping.align_sf_status",
            "erpnext_shipping.sf_international.freight_accounting.onload",
            "erpnext_shipping.sf_international.interception.onload",
            "erpnext_shipping.sf_international.waybill.onload",
        ],
        "before_cancel": "erpnext_shipping.sf_international.shipping.cancel_sf_order_on_cancel",
        "before_discard": "erpnext_shipping.sf_international.shipping.cancel_sf_order_on_cancel",
        "on_trash": "erpnext_shipping.sf_international.shipping.cancel_sf_order_on_cancel",
    },
    "Journal Entry": {
        "before_validate": "erpnext_shipping.sf_international.freight_accounting.validate_journal",
        "before_update_after_submit": "erpnext_shipping.sf_international.freight_accounting.validate_journal",
        "before_cancel": "erpnext_shipping.sf_international.freight_accounting.before_cancel",
        "on_cancel": "erpnext_shipping.sf_international.freight_accounting.on_cancel",
        "on_trash": "erpnext_shipping.sf_international.freight_accounting.on_trash",
        "onload": "erpnext_shipping.sf_international.freight_accounting.journal_onload",
    },
}

shipment_carrier_adapters = ["erpnext_shipping.sf_international.carrier_adapter"]
