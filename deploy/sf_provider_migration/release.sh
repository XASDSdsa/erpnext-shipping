#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
set -a
source release.env
set +a
case "${1:-}" in prepare|rehearse|deploy) ;; *) echo "usage: release.sh prepare|rehearse|deploy" >&2; exit 2 ;; esac
exec > >(tee -a "$1.log") 2>&1
exec python3 release.py "$@"
