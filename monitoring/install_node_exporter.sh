#!/usr/bin/env bash
# Install the pinned node_exporter binary the host telemetry unit expects.
#
#   monitoring/install_node_exporter.sh              # install to ~/.local/bin
#   monitoring/install_node_exporter.sh --check      # is what is installed the pin?
#   monitoring/install_node_exporter.sh --dest DIR   # e.g. /usr/local/bin (needs root)
#
# Why a script and not a line in the runbook: "pinned version" only means
# something if the thing downloaded is checked against a digest that is in git.
# A curl-| -sh install from a moving URL is the same class of problem as a
# `:latest` image — an unreviewed remote push becomes a change on this box. So:
# one version, one digest per architecture, both committed here, both verified
# before anything is executed, and the binary installed where
# monitoring/node-exporter.service's ExecStart looks for it.
#
# Nothing here needs root: the exporter is a *user* service reading world-readable
# sysfs files (spikes/amd-gpu-metrics/FINDINGS.md §1 — every attribute it needs is
# S_IRUGO). Installing into ~/.local/bin is what keeps that true.
#
# Bumping the pin (deliberately, like the compose images):
#   1. NODE_EXPORTER_VERSION=<new> below;
#   2. fetch both digests and paste them in:
#        for a in amd64 arm64; do
#          curl -sSL -o /tmp/ne.tar.gz \
#            "https://github.com/prometheus/node_exporter/releases/download/v${V}/node_exporter-${V}.linux-${a}.tar.gz"
#          sha256sum /tmp/ne.tar.gz   # the release page's own checksum, from the asset you ran
#        done
#   3. re-run this script, re-run monitoring/measure_footprint.sh --proc … and
#      update the footprint row in monitoring/README.md (the budget row is a
#      measurement, not a formality).
set -uo pipefail

NODE_EXPORTER_VERSION="1.12.1"
# sha256 of the release tarball, per architecture. Measured with this exact
# download (workspace-szw.13), not copied from a README.
SHA_AMD64="b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63"
SHA_ARM64="ad35b605f9954b9f1ffddf5ba054bdc5a98d790b9eae5291e1eeb83f1ecbd0e7"

DEST="${XDG_BIN_HOME:-$HOME/.local/bin}"
CHECK_ONLY=0
FORCE=0

usage() {
  sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --force) FORCE=1; shift ;;
    --dest) DEST="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) ARCH=amd64; EXPECTED="$SHA_AMD64" ;;
  aarch64 | arm64) ARCH=arm64; EXPECTED="$SHA_ARM64" ;;
  *) echo "no pin for architecture '$ARCH' — add one above rather than trusting a download" >&2; exit 1 ;;
esac

TARBALL="node_exporter-${NODE_EXPORTER_VERSION}.linux-${ARCH}.tar.gz"
URL="https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/${TARBALL}"
TARGET="${DEST}/node_exporter"

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
  fi
}

if [ "$CHECK_ONLY" = 1 ]; then
  if [ ! -x "$TARGET" ]; then
    echo "$TARGET: not installed (run: monitoring/install_node_exporter.sh)"
    exit 1
  fi
  INSTALLED="$("$TARGET" --version 2>/dev/null | awk '/^node_exporter, version/ {print $3; exit}')"
  if [ "$INSTALLED" = "$NODE_EXPORTER_VERSION" ]; then
    echo "$TARGET: node_exporter ${INSTALLED} — matches the pin in monitoring/install_node_exporter.sh"
    exit 0
  fi
  echo "$TARGET: node_exporter ${INSTALLED:-?} does not match the pin (${NODE_EXPORTER_VERSION})"
  exit 1
fi

command -v curl >/dev/null 2>&1 || { echo "need curl (or fetch the tarball yourself and use --dest)" >&2; exit 1; }
mkdir -p "$DEST" || exit 1

if [ -x "$TARGET" ] && [ "$FORCE" = 0 ]; then
  CURRENT="$("$TARGET" --version 2>/dev/null | awk '/^node_exporter, version/ {print $3; exit}')"
  if [ "$CURRENT" = "$NODE_EXPORTER_VERSION" ]; then
    echo "$TARGET is already node_exporter ${NODE_EXPORTER_VERSION}; nothing to do"
    exit 0
  fi
  echo "$TARGET is node_exporter ${CURRENT:-unknown}, not ${NODE_EXPORTER_VERSION}." >&2
  echo "Refusing to overwrite a binary this script did not fetch. Re-run with --force" >&2
  echo "once you know where the current one came from (a distro package would own it)." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
echo "downloading ${URL}"
curl -fsSL --proto '=https' --tlsv1.2 -o "${WORK}/${TARBALL}" "$URL" || exit 1

GOT="$(sha256 "${WORK}/${TARBALL}")"
if [ "$GOT" != "$EXPECTED" ]; then
  cat >&2 <<EOF
checksum MISMATCH — nothing was installed.
  expected ($ARCH, pinned in this script): $EXPECTED
  got:                                    $GOT
If upstream really re-published the release, bump the pin above in the same commit
as the version, so the change is reviewed. Never install a tarball you cannot name.
EOF
  exit 1
fi
echo "checksum OK ($ARCH, ${NODE_EXPORTER_VERSION})"

tar -xzf "${WORK}/${TARBALL}" -C "$WORK" "node_exporter-${NODE_EXPORTER_VERSION}.linux-${ARCH}/node_exporter" || exit 1
# Install via a temp name + mv: an interrupted install cannot leave a half-written
# binary where a running service's ExecStart points.
install -m 0755 "${WORK}/node_exporter-${NODE_EXPORTER_VERSION}.linux-${ARCH}/node_exporter" "${TARGET}.new"
mv -f "${TARGET}.new" "$TARGET"

echo "installed ${TARGET}"
"$TARGET" --version | head -1
cat <<EOF

Next:
  cp monitoring/node-exporter.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now node-exporter
  systemctl --user status node-exporter

  curl -s http://127.0.0.1:9100/metrics | grep -E '^node_(scrape_collector_success\{collector="drm"|drm_memory_gtt)'

Then restart Prometheus so the `host` job picks up the target
(monitoring/prometheus/targets/host.json), and read the runbook entry
"Host + GPU telemetry (node_exporter)" in monitoring/README.md.
EOF
