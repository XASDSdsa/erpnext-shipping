#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
# The checked-in file supplies defaults. Preserve explicit process overrides,
# including empty values which the Python required() checks must reject instead
# of silently replacing them with a historical deployment value.
release_config_keys=(
  PROJECT_PATH PROJECT SITE BASE_IMAGE NEW_IMAGE RELEASE_NAME
  ERP_REMOTE ERP_BRANCH ERP_REV BASE_ERPNEXT_REV
  SF_REMOTE SF_BRANCH SF_REV BASE_SF_REV
  SHIPPING_REMOTE SHIPPING_BRANCH SHIPPING_REV BASE_SHIPPING_REV
  FLOW_REMOTE FLOW_BRANCH FLOW_REV BASE_FLOW_REV
  DB_IMAGE REDIS_IMAGE BACKUP_DIR PRODUCTION_NETWORK METADATA_SCRIPT_RELATIVE
)
release_override_names=()
release_override_values=()
for release_key in "${release_config_keys[@]}"; do
  if [[ ${!release_key+x} ]]; then
    release_override_names+=("$release_key")
    release_override_values+=("${!release_key}")
  fi
done
set -a
source release.env
for release_index in "${!release_override_names[@]}"; do
  printf -v "${release_override_names[$release_index]}" '%s' "${release_override_values[$release_index]}"
done
set +a
case "${1:-}" in prepare|rehearse|deploy) ;; *) echo "usage: release.sh prepare|rehearse|deploy" >&2; exit 2 ;; esac
if [[ ${RELEASE_NO_TEE:-0} == 1 ]]; then
  # Restricted CI shells may not provide /dev/fd for process substitution.
  # Production keeps the tee path so progress remains visible and persisted.
  exec > "$1.log" 2>&1
else
  exec > >(tee -a "$1.log") 2>&1
fi
exec python3 release.py "$@"
