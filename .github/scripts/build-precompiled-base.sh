#!/bin/bash
# Build and read metadata from the native base image, without consulting a registry tag.
set -euo pipefail
case "$DIST" in
  ubuntu22.04) BASE_TARGET=jammy ;;
  ubuntu24.04) BASE_TARGET=noble ;;
  ubuntu26.04) BASE_TARGET=resolute ;;
  *) echo "Unsupported distribution: $DIST" >&2; exit 1 ;;
esac
BASE_IMAGE="${IMAGE_NAME}:base-${BASE_TARGET}-${LTS_KERNEL}-${KERNEL_FLAVOR}-${DRIVER_BRANCH}${BASE_VERSION:+-${BASE_VERSION}}-${ARCH}"
make DRIVER_BRANCH="$DRIVER_BRANCH" KERNEL_FLAVOR="$KERNEL_FLAVOR" LTS_KERNEL="$LTS_KERNEL" \
  IMAGE_TAG="${BASE_IMAGE##*:}" DOCKER_BUILD_PLATFORM_OPTIONS="--platform=linux/${ARCH}" \
  DOCKER_BUILD_OPTIONS="--load" "build-base-${BASE_TARGET}"
# The metadata is created during the build; a stopped container is sufficient to copy it.
container=$(docker create --platform="linux/${ARCH}" "$BASE_IMAGE")
trap 'docker rm -f "$container"' EXIT
docker cp "${container}:/var/kernel_version.txt" kernel_version.txt
# shellcheck disable=SC1091
source kernel_version.txt
: "${KERNEL_VERSION:?No supported kernel found for $DIST/$ARCH/$KERNEL_FLAVOR}"
: "${DRIVER_VERSION:?No driver version found for $DIST/$ARCH}"
: "${DRIVER_VERSIONS:?No driver versions found for $DIST/$ARCH}"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "BASE_IMAGE=$BASE_IMAGE" >> "$GITHUB_ENV"
  echo "KERNEL_VERSION=$KERNEL_VERSION" >> "$GITHUB_ENV"
fi
