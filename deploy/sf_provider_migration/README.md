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

`release.env` contains historical baseline defaults and SSH repositories.
Explicitly exported known release variables take precedence over these defaults.
Before each invocation, export the verified current `BASE_IMAGE` and all four
`BASE_*_REV` values, exact target `ERP_REV`, `SHIPPING_REV`, `FLOW_REV` and `SF_REV`
commit hashes, together with a new `RELEASE_NAME` and `NEW_IMAGE`. The same values
must remain in effect for prepare, rehearsal and deployment. An explicitly empty
value is preserved and fails the required-value check; it never falls back to an
older deployment. Do not source `release.env` after exporting overrides.

Obtain this directory from the exact pushed `SHIPPING_REV` commit. The candidate
Shipping revision includes these release-script changes even if its runtime
application code is unchanged. `prepare` verifies the target branch head, checks
out that commit and compares every release tool with its file in that checkout.
Never edit a release directory or application code on the server.

If the current production image has accumulated too many OverlayFS layers for
Docker to mount during a new build, export `BUILD_BASE_IMAGE` with a separate,
immutable Git-built image whose source and Frappe revision have already been
verified. `BASE_IMAGE` remains the running production image for source checks,
backup, rollback and the production switch; `BUILD_BASE_IMAGE` is used only by
the Docker build and is pinned in `release-state.json`. Without this explicit
override the build uses `BASE_IMAGE` as before.

By default the release runs `metadata.py` for SF ownership migration. For a
Flow-only workflow release, export
`METADATA_SCRIPT_RELATIVE=app-source/flow/deploy/customer_service_workflows/metadata.py`.
The selected script remains owned by the Flow repository. `prepare` accepts only
a regular Python file from the exact `FLOW_REV` Git tree and its matching source
archive; absolute paths, other apps, path traversal and substituted source files
are rejected. The relative path and SHA-256 are fixed in `release-state.json`
and checked again for rehearsal and deployment. The same script must support the
baseline image's `snapshot`, `restore` and `compare` commands without requiring
new Flow application modules, and the candidate's `migrate`, `validate` and
`fail-after-owner` commands. The injected failure must occur after the Flow
metadata write and report `ISOLATED_INJECTED_FAILURE_AFTER_OWNER:FLOW_METADATA`.

Only the selected script receives the read permission and its ancestor
directories receive the traversal permission needed by the container's `frappe`
user; secret directories and files retain their existing restrictions. Before
creating the isolated network, volumes or database, `rehearse` reads that script
as `frappe` in both baseline and candidate images and checks the exact SHA-256.
Custom Flow migrations always run the injected-failure and exact-rollback checks,
including when the SF module had already been migrated.

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
