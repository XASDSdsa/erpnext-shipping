# Shipping provider source checks

This directory is a Git-pinned helper used by the clean image build. It does
not migrate database records, install a retired application, or switch
production. The image is built from official Frappe build/runtime images and
exact Git archives.

## Ownership

- Generic Shipment, Delivery Note, stock, and accounting behavior belongs to
  ERPNext.
- Carrier integrations, including the `erpnext_shipping.sf_international`
  package, belong to ERPNext Shipping.
- Flow tools and orchestration belong to frappe-flow.
- Existing business records are left untouched by this build.

## Build contract

The caller supplies repository revisions, official build/runtime image digests,
and a new candidate tag. Production paths, credentials, registry namespaces,
and site configuration are not stored in this repository. The only supported
entry point is:

```sh
bash release.sh prepare
```

`prepare` verifies the exact Git heads, builds the candidate, and verifies its
source and assets. `rehearse` and `deploy` are intentionally disabled. No
backup, metadata snapshot, migration, cache repair, or production switch is
performed.

Use `erp_next/deploy/clean_git_release` as the top-level clean build procedure.
It creates a new bench from the official build/runtime images and selected Git
repositories; it does not use an existing application image or production
volume as build input.
