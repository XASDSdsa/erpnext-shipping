# SF International provider

`erpnext_shipping.sf_international` owns SF API calls, address lookup, labels,
tracking, cancellation/interception, replacement history and freight accounting.
ERPNext owns the native Shipment/Delivery Note lifecycle and manual carriers.
LetMeShip and SendCloud continue to use their existing Shipping modules.

The native Shipment chooses a carrier. Shipping registers the SF adapter and
form extension once. A first Shipment save validates locally, inserts its parent
record and then queues SF booking with `enqueue_after_commit`; no network POST
runs before the parent transaction commits. An attempted request is never treated
as a confirmed booking/cancellation/settlement. Historical waybills and freight
journals remain linked to their original carrier numbers.

Flow tools live in `flow.integrations.erpnext`. They use the provider's
`reviewed_booking` contract to validate the approved ledger, current actor,
Delivery Note, recipient, package dimensions and exact carrier request immediately
before booking. The provider has no Flow imports and does not modify Agents.

## Schema and migration

The three DocTypes keep their original names and `SF International` module:
SF International Settings, SF International Product, and SF Waybill. Shipping
becomes their sole app owner. Existing settings/password keys and history are
preserved. PayPal and generic business tools are not part of this provider.

`custom_fields.json` captures the verified 42-field SF contract, including hidden
historical fields. Freight status retains old values `未结算`/`已结算` and accepts
`待核实`/`账单已取得`, which the current freight synchronizer actually writes.
Schema changes do not rewrite freight facts or create accounting documents.

Fresh installation creates provider metadata and seeds an empty product list.
Migration of an existing deployment uses the separate reviewed ownership
migration; it must not run old SF business repairs or blind app uninstall.

The shipping settings sidebar links to SF International Settings. Future
carriers should register their own module, adapter and fields through the same
native hooks, without importing or modifying this SF implementation.
