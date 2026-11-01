#!/bin/sh
# Keep a read-only mirror of the studio access secret on the host.
#
# The container file (/data/studio-access.secret) is the single writable source
# of truth: operators change it from the internal console settings page (or, in
# a pinch, with `docker exec`). This script only copies it *out*, so an operator
# can read the current values without entering the container.
#
# Usage:
#   studio-access-sync.sh sync   # container -> /root/.deploy/studio-access.secret
#   studio-access-sync.sh show   # print the host mirror (root only)
#
# Install (host, has docker):
#   install -m 0755 scripts/studio-access-sync.sh /usr/local/sbin/studio-access-sync.sh
#   systemctl enable --now studio-access-sync.timer     # 2-minute cadence
#
# Credentials are never logged; the mirror is chmod 600 inside a 0700 directory.

set -eu

CONTAINER="${STUDIO_ACCESS_CONTAINER:-dramaclaw-api-1}"
SRC="${STUDIO_ACCESS_SRC:-/data/studio-access.secret}"
DEST_DIR="${STUDIO_ACCESS_SYNC_DIR:-/root/.deploy}"
DEST="$DEST_DIR/studio-access.secret"

sync_once() {
  mkdir -p "$DEST_DIR"
  chmod 700 "$DEST_DIR"
  tmp="$DEST.tmp.$$"
  if ! docker cp "$CONTAINER:$SRC" "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    echo "sync skipped: cannot read $CONTAINER:$SRC" >&2
    return 1
  fi
  chmod 600 "$tmp"
  # Same-directory rename: a concurrent reader never sees a half-written file.
  mv -f "$tmp" "$DEST"
  echo "synced $CONTAINER:$SRC -> $DEST"
}

case "${1:-sync}" in
  sync) sync_once ;;
  show) cat "$DEST" ;;
  *)
    echo "usage: $0 [sync|show]" >&2
    exit 2
    ;;
esac
