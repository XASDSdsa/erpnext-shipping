ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG ERP_REV
ARG SF_REV
ARG SHIPPING_REV
ARG FLOW_REV
ARG RELEASE_NAME
USER root
COPY assets-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh
COPY --chown=frappe:frappe app-source/ /home/frappe/frappe-bench/apps/
COPY candidate-sources.json /tmp/candidate-sources.json
RUN python3 -c 'import json,pathlib; root=pathlib.Path("/home/frappe/frappe-bench/apps"); manifest=json.loads(pathlib.Path("/tmp/candidate-sources.json").read_text()); [(root/app/path).unlink(missing_ok=True) for app,data in manifest.items() for path in data["deleted"]]; pathlib.Path("/tmp/candidate-sources.json").unlink()'
USER frappe
WORKDIR /home/frappe/frappe-bench
# Reuse the complete r9 dependencies. --force also recompiles older Git-archive
# PO mtimes instead of accepting inherited MO files as newer.
RUN bench build --app erpnext --production --force
RUN bench build --app erpnext_shipping --production --force
RUN bench build --app flow --production --force
RUN env/bin/python -m compileall -q apps/erpnext/erpnext/stock/doctype/shipment apps/sf_international/sf_international apps/erpnext_shipping/erpnext_shipping apps/flow/flow/integrations
LABEL org.leya.erpnext-revision="${ERP_REV}" org.leya.sf-revision="${SF_REV}" org.leya.shipping-revision="${SHIPPING_REV}" org.leya.flow-revision="${FLOW_REV}" com.leya.release="${RELEASE_NAME}"
