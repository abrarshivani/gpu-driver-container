#!/usr/bin/env bash
# Copyright 2026 NVIDIA CORPORATION
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <output.tar> <arch=input.tar> [<arch=input.tar> ...]" >&2
  exit 2
fi
OUTPUT=$1
shift
REGCTL=${REGCTL:-regctl}
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

REFS=()
PLATFORMS=()
EXPECTED=()
for SOURCE in "$@"; do
  ARCH=${SOURCE%%=*}
  ARCHIVE=${SOURCE#*=}
  case "$ARCH" in
    amd64|arm64) ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
  esac
  [[ "$SOURCE" == *=* && -f "$ARCHIVE" ]] || { echo "Missing archive: $SOURCE" >&2; exit 1; }
  REF="ocidir://${WORK_DIR}/${ARCH}:native"
  "$REGCTL" image import "$REF" "$ARCHIVE"
  REFS+=(--ref "$REF")
  PLATFORMS+=(--platform "linux/$ARCH")
  EXPECTED+=("$ARCH")
done

# Local OCI references keep untested scheduled images out of the registry.
MERGED="ocidir://${WORK_DIR}/merged:combined"
"$REGCTL" index create "$MERGED" "${REFS[@]}" "${PLATFORMS[@]}"
EXPECTED_JSON=$(printf '%s\n' "${EXPECTED[@]}" | jq -Rs 'split("\n")[:-1] | sort')
"$REGCTL" manifest get "$MERGED" --format raw-body | jq -e --argjson expected "$EXPECTED_JSON" \
  '([.manifests[] | select(.platform.os == "linux") | .platform.architecture] | sort) == $expected' >/dev/null
"$REGCTL" image export "$MERGED" "$OUTPUT"
