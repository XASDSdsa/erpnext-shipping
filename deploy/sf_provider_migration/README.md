# SF International provider migration

This release moves the existing SF International integration into ERPNext
Shipping. It preserves document names, carrier order numbers, credentials,
accounting links and Flow tool identities.

## Ownership

| Responsibility | Repository and package |
| --- | --- |
| Shipment and Delivery Note lifecycle, manual carriers, accounting and stock validation | `XASDSdsa/erp_next`, `erpnext` |
| SF API, address matching, labels, interception, freight and waybill history | `XASDSdsa/erpnext-shipping`, `erpnext_shipping.sf_international` |
| Agent tools, user review and business workflow orchestration | `XASDSdsa/frappe-flow`, `flow.integrations.erpnext` |
| Retired application's release marker | `XASDSdsa/frappe-sf`, inactive `sf_international` package |

The retired package must register no hooks, modules, scripts or provider. Its
Git history preserves the former implementation. It is not a compatibility
implementation or an alternative API. Do not run `uninstall-app` on it: native
uninstall would delete the records which this release transfers to their owners.

## Controlled release

`release.env` pins the verified r9 baseline and SSH repositories. Export exact
`ERP_REV`, `SHIPPING_REV`, `FLOW_REV` and `SF_REV` commit hashes, together with a
new `RELEASE_NAME` and `NEW_IMAGE`. Obtain this directory from the exact pushed
Shipping commit. Never edit a release directory or application code on the server.

1. `./release.sh prepare`: verify remote branch heads, baseline source and
   resource hashes, construct a candidate from Git archives, build each changed
   app explicitly, and verify all four repositories in the immutable image.
2. Set `BACKUP_DIR` to a verified backup and run `./release.sh rehearse`.
   A private network, independent MariaDB/Redis instances and separate volumes
   are used for migration, repeated migration and exact metadata rollback.
3. Set `PRODUCTION_NETWORK` to the verified current network and run
   `./release.sh deploy`. The script validates the same candidate again, checks
   old queue references, drains requests and jobs, takes a fresh backup, applies
   only the ownership migration and switches the six application services.

The database and production Redis services are never stopped. Unrelated
applications, tools and data are not synchronized by a full migration. Old
queued SF calls must finish before removing the old application's registration;
the release never retries or rewrites their serialized payloads.

Before traffic resumes, the running source, metadata, resources and application
logs must pass verification. Public and internal health probes then require
the exact native ping response. A pre-traffic failure restores the captured
metadata and previous image. After traffic resumes, metadata rollback is not
automatic because new business writes may already exist.

Successful release evidence is `rehearsal.ok.json` and `deploy.ok.json`, linked
to exact image IDs, Git revisions, script hashes and backup hashes. Record the
actual commands and outcome in `DEPLOYMENT_NOTES.md`; a candidate build alone
does not count as a completed deployment.
