#!/usr/bin/env python3
"""Exercise merge-oci-archives.sh against regctl ($REGCTL or PATH) with local archives only."""

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple

REGCTL = os.environ.get("REGCTL", "regctl")
SCRIPT = Path(__file__).with_name("merge-oci-archives.sh")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"


class BuiltImage(NamedTuple):
    manifest_digest: str
    layer: bytes


def write_oci_archive(
    path: Path, arch: str, *, wrap_in_index: bool = False
) -> BuiltImage:
    """Write a one-image OCI layout tar.

    With wrap_in_index, index.json points at a nested image index, as Buildx emits;
    otherwise it points at the image manifest directly.
    """
    layout_files: dict[str, bytes] = {}

    def add_blob(data: bytes, media_type: str) -> dict[str, object]:
        digest = hashlib.sha256(data).hexdigest()
        layout_files[f"blobs/sha256/{digest}"] = data
        return {
            "mediaType": media_type,
            "digest": f"sha256:{digest}",
            "size": len(data),
        }

    layer_buffer = io.BytesIO()
    with tarfile.open(fileobj=layer_buffer, mode="w") as layer:
        data = f"native {arch}\n".encode()
        entry = tarfile.TarInfo("architecture.txt")
        entry.size = len(data)
        layer.addfile(entry, io.BytesIO(data))
    layer_data = layer_buffer.getvalue()
    config_descriptor = add_blob(
        json.dumps(
            {
                "architecture": arch,
                "os": "linux",
                "config": {},
                "rootfs": {
                    "type": "layers",
                    "diff_ids": [f"sha256:{hashlib.sha256(layer_data).hexdigest()}"],
                },
            }
        ).encode(),
        "application/vnd.oci.image.config.v1+json",
    )
    manifest_descriptor = add_blob(
        json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "config": config_descriptor,
                "layers": [
                    add_blob(layer_data, "application/vnd.oci.image.layer.v1.tar")
                ],
            }
        ).encode(),
        OCI_MANIFEST,
    )
    top_level_descriptor = manifest_descriptor
    if wrap_in_index:
        platform_descriptor = {
            **manifest_descriptor,
            "platform": {"os": "linux", "architecture": arch},
        }
        top_level_descriptor = add_blob(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": OCI_INDEX,
                    "manifests": [platform_descriptor],
                }
            ).encode(),
            OCI_INDEX,
        )
    layout_files["index.json"] = json.dumps(
        {"schemaVersion": 2, "manifests": [top_level_descriptor]}
    ).encode()
    layout_files["oci-layout"] = b'{"imageLayoutVersion":"1.0.0"}'
    with tarfile.open(path, "w") as archive:
        for name, data in layout_files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return BuiltImage(str(manifest_descriptor["digest"]), layer_data)


class MergeArchivesTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.output = self.root / "combined.tar"
        self.env = {**os.environ, "REGCTL": REGCTL}

    def merge(self, *sources: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), str(self.output), *sources],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_combined_archive(self, expected_images: dict[str, BuiltImage]) -> None:
        imported_ref = f"ocidir://{self.root}/imported:combined"
        subprocess.run(
            [REGCTL, "image", "import", imported_ref, str(self.output)],
            env=self.env,
            check=True,
        )
        merged_index = json.loads(
            subprocess.check_output(
                [REGCTL, "manifest", "get", imported_ref, "--format", "raw-body"],
                env=self.env,
            )
        )
        self.assertEqual(
            sorted(
                (
                    descriptor["platform"]["os"],
                    descriptor["platform"]["architecture"],
                    descriptor["digest"],
                )
                for descriptor in merged_index["manifests"]
            ),
            sorted(
                ("linux", arch, image.manifest_digest)
                for arch, image in expected_images.items()
            ),
        )
        with tarfile.open(self.output) as archive:
            for image in expected_images.values():
                layer_name = f"blobs/sha256/{hashlib.sha256(image.layer).hexdigest()}"
                layer_file = archive.extractfile(layer_name)
                assert layer_file is not None
                self.assertEqual(layer_file.read(), image.layer)

    def test_merge_preserves_native_platforms_digests_and_layers(self) -> None:
        amd64_image = write_oci_archive(self.root / "amd64.tar", "amd64")
        arm64_image = write_oci_archive(
            self.root / "arm64.tar", "arm64", wrap_in_index=True
        )
        result = self.merge(
            f"amd64={self.root}/amd64.tar", f"arm64={self.root}/arm64.tar"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_combined_archive({"amd64": amd64_image, "arm64": arm64_image})

    def test_amd64_only_archive(self) -> None:
        amd64_image = write_oci_archive(
            self.root / "amd64.tar", "amd64", wrap_in_index=True
        )
        result = self.merge(f"amd64={self.root}/amd64.tar")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_combined_archive({"amd64": amd64_image})

    def test_missing_archive_fails_without_output(self) -> None:
        write_oci_archive(self.root / "amd64.tar", "amd64")
        result = self.merge(
            f"amd64={self.root}/amd64.tar", f"arm64={self.root}/missing.tar"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing archive: arm64=", result.stderr)
        self.assertFalse(self.output.exists())

    def test_mislabeled_platform_fails_without_output(self) -> None:
        for wrap_in_index in (False, True):
            with self.subTest(wrap_in_index=wrap_in_index):
                write_oci_archive(
                    self.root / "arm64.tar", "arm64", wrap_in_index=wrap_in_index
                )
                result = self.merge(f"amd64={self.root}/arm64.tar")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("do not match", result.stderr)
                self.assertFalse(self.output.exists())

    def test_duplicate_platform_fails_without_output(self) -> None:
        write_oci_archive(self.root / "amd64.tar", "amd64")
        result = self.merge(
            f"amd64={self.root}/amd64.tar", f"amd64={self.root}/amd64.tar"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("do not match", result.stderr)
        self.assertFalse(self.output.exists())

    def test_source_without_architecture_is_a_usage_error(self) -> None:
        write_oci_archive(self.root / "amd64.tar", "amd64")
        result = self.merge(f"{self.root}/amd64.tar")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Expected <arch>=<archive>", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
