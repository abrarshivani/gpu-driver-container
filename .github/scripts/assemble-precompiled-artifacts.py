#!/usr/bin/env python3
"""Group driver archives by discovered kernel and combine native base images."""

import importlib.util
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.dont_write_bytecode = True

spec = importlib.util.spec_from_file_location(
    "merge_oci", Path(__file__).with_name("merge-oci-archives.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
merge = module.merge
branch, lts, flavor, dist = (
    os.environ[key] for key in ("DRIVER_BRANCH", "LTS_KERNEL", "KERNEL_FLAVOR", "DIST")
)
arches = (
    ["amd64"] if dist == "ubuntu22.04" or flavor == "azure-fde" else ["amd64", "arm64"]
)
output = Path("assembled")
output.mkdir(exist_ok=True)
drivers = defaultdict(list)
with tempfile.TemporaryDirectory() as temporary:
    bases = []
    for arch in arches:
        bundle = (
            Path("native") / f"native-precompiled-{branch}-{lts}-{flavor}-{dist}-{arch}"
        )
        images = list(bundle.glob(f"driver-images-*-{arch}.tar"))
        if len(images) != 1:
            raise ValueError(f"Expected one driver image for {arch}, found {images}")
        drivers[images[0].name.removesuffix(f"-{arch}.tar")].append(images[0])
        base = Path(temporary) / f"base-{arch}.tar"
        subprocess.run(
            [
                "skopeo",
                "copy",
                f"docker-archive:{bundle}/base-{arch}.tar",
                f"oci-archive:{base}:base",
            ],
            check=True,
        )
        bases.append(base)
    merge(output / f"base-images-{branch}-{lts}-{flavor}-{dist}.tar", bases)
    for name, images in drivers.items():
        merge(output / f"{name}.tar", images)
