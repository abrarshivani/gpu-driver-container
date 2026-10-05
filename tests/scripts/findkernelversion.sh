#!/bin/bash

if [[ $# -lt 4 || $# -gt 5 ]]; then
	echo " KERNEL_FLAVOR DRIVER_BRANCH DIST LTS_KERNEL or KERNEL_FLAVOR DRIVER_BRANCH DIST LTS_KERNEL PLATFORM_SUFFIX are required"
	exit 1
fi

export KERNEL_FLAVOR="${1}"
export DRIVER_BRANCH="${2}"
export DIST="${3}"
export LTS_KERNEL="${4}"
export PLATFORM_SUFFIX="${5}"

export REGCTL_VERSION=v0.11.5
mkdir -p bin
curl -sSLo bin/regctl https://github.com/regclient/regclient/releases/download/${REGCTL_VERSION}/regctl-linux-amd64
chmod a+x bin/regctl
export PATH=$(pwd)/bin:${PATH}

# calculate kernel version of latest image
prefix="kernel-version-${DRIVER_BRANCH}-${LTS_KERNEL}"
suffix="${KERNEL_FLAVOR}-${DIST}"

artifact_dir="./kernel-version-artifacts"
PLATFORM="${PLATFORM_SUFFIX#-}"
[ -z "$PLATFORM" ] && PLATFORM=amd64
artifact_file=$(find "$artifact_dir" -maxdepth 1 -type f -name "${prefix}*-${suffix}-${PLATFORM}.tar" | head -1)
if [ -z "$artifact_file" ]; then
    echo "Missing native kernel metadata for $DRIVER_BRANCH/$DIST/$LTS_KERNEL/$KERNEL_FLAVOR/$PLATFORM" >&2
    return 1
fi
tar -xf "$artifact_file" -C ./
unset KERNEL_VERSION
# shellcheck disable=SC1091
source ./kernel_version.txt
: "${KERNEL_VERSION:?Native kernel metadata contains no kernel version}"
rm -f kernel_version.txt

# calculate driver tag
status_nvcr=0
status_ghcr=0
regctl manifest inspect nvcr.io/nvidia/driver:${DRIVER_BRANCH}-${KERNEL_VERSION}-${DIST} --platform=linux/${PLATFORM} > /dev/null 2>&1; status_nvcr=$?
regctl manifest inspect ghcr.io/nvidia/driver:${DRIVER_BRANCH}-${KERNEL_VERSION}-${DIST} --platform=linux/${PLATFORM} > /dev/null 2>&1; status_ghcr=$?

if [[ $status_nvcr -eq 0 || $status_ghcr -eq 0 ]]; then
    export should_continue=false
else
    export should_continue=true
fi
