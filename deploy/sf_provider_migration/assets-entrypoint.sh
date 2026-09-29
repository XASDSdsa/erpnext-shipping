#!/bin/sh
set -eu

BAKED_PATH=/home/frappe/frappe-bench/assets
ASSETS_PATH=/home/frappe/frappe-bench/sites/assets

test -d "$BAKED_PATH" || { echo "Baked assets directory is missing" >&2; exit 1; }
if test -e "$ASSETS_PATH" && ! test -L "$ASSETS_PATH"; then
    echo "Refusing to replace a real assets path" >&2
    exit 1
fi

link_dir=$(mktemp -d "${ASSETS_PATH%/*}/.assets-link.XXXXXX")
trap 'rm -rf "$link_dir"' EXIT
trap 'exit 1' HUP INT TERM
ln -s "$BAKED_PATH" "$link_dir/assets"
mv -Tf "$link_dir/assets" "$ASSETS_PATH"
rmdir "$link_dir"
trap - EXIT HUP INT TERM
exec "$@"
