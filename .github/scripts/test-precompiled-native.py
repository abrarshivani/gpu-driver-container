#!/usr/bin/env python3
"""Hermetic regression checks for native metadata and OCI artifact assembly.

Run with Python 3.9+ and Bash 4+ (the versions supplied by CI runners).
"""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github/scripts"
spec = importlib.util.spec_from_file_location(
    "merge_oci", SCRIPTS / "merge-oci-archives.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def archive(path: Path, arch: str, nested: bool = False) -> None:
    blobs = {}

    def blob(value: dict) -> dict:
        data = json.dumps(value).encode()
        digest = hashlib.sha256(data).hexdigest()
        blobs[f"blobs/sha256/{digest}"] = data
        return {"digest": f"sha256:{digest}", "size": len(data)}

    config = blob(
        {
            "architecture": arch,
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": []},
        }
    )
    config["mediaType"] = "application/vnd.oci.image.config.v1+json"
    image = blob(
        {
            "schemaVersion": 2,
            "mediaType": module.MANIFEST,
            "config": config,
            "layers": [],
        }
    )
    image["mediaType"] = module.MANIFEST
    if nested:
        image = blob(
            {"schemaVersion": 2, "mediaType": module.INDEX, "manifests": [image]}
        )
        image["mediaType"] = module.INDEX
    blobs["index.json"] = json.dumps(
        {"schemaVersion": 2, "manifests": [image]}
    ).encode()
    blobs["oci-layout"] = b'{"imageLayoutVersion":"1.0.0"}'
    with tarfile.open(path, "w") as output:
        for name, data in blobs.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            output.addfile(info, io.BytesIO(data))


def platforms(path: Path) -> list[str]:
    with tarfile.open(path) as source:
        root = json.load(source.extractfile("index.json"))
        descriptor = root["manifests"][0]
        index = json.load(
            source.extractfile("blobs/sha256/" + descriptor["digest"].split(":")[1])
        )
        # Verify the root selects the complete image and all blobs survive intact.
        for member in source:
            if member.name.startswith("blobs/sha256/"):
                assert (
                    hashlib.sha256(source.extractfile(member).read()).hexdigest()
                    == member.name.split("/")[-1]
                )
        return sorted(item["platform"]["architecture"] for item in index["manifests"])


class NativePrecompiled(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.env = dict(
            os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"]
        )
        self.bash = os.environ.get("BASH_BIN", "bash")

    def executable(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + text)
        path.chmod(0o755)

    def test_merge_nested_and_direct_archives(self) -> None:
        amd, arm, output = [
            self.work / name for name in ("amd.tar", "arm.tar", "combined.tar")
        ]
        archive(amd, "amd64")
        archive(arm, "arm64", nested=True)
        module.merge(output, [amd, arm])
        self.assertEqual(platforms(output), ["amd64", "arm64"])
        module.merge(output, [arm])
        self.assertEqual(platforms(output), ["arm64"])

    def test_merge_rejects_duplicates_and_missing_images(self) -> None:
        image, output = self.work / "image.tar", self.work / "combined.tar"
        archive(image, "amd64")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            module.merge(output, [image, image])
        with self.assertRaisesRegex(ValueError, "No native"):
            module.merge(output, [])
        archive(image, "unknown")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            module.merge(output, [image])

    def assemble(self, dist: str, flavor: str, kernels: dict[str, str]) -> Path:
        self.executable(
            "skopeo",
            'destination="${3#oci-archive:}"\n'
            'cp "${2#docker-archive:}" "${destination%:base}"\n',
        )
        for arch, kernel in kernels.items():
            bundle = (
                self.work / f"native/native-precompiled-580-6.8-{flavor}-{dist}-{arch}"
            )
            bundle.mkdir(parents=True)
            archive(bundle / f"base-{arch}.tar", arch)
            archive(bundle / f"driver-images-580-{kernel}-{dist}-{arch}.tar", arch)
        env = dict(
            self.env,
            DRIVER_BRANCH="580",
            LTS_KERNEL="6.8",
            KERNEL_FLAVOR=flavor,
            DIST=dist,
        )
        subprocess.run(
            [sys.executable, str(SCRIPTS / "assemble-precompiled-artifacts.py")],
            cwd=self.work,
            env=env,
            check=True,
            capture_output=True,
        )
        return self.work / "assembled"

    def test_assembly_with_shared_kernel(self) -> None:
        kernel = "6.8.0-100-generic"
        output = self.assemble(
            "ubuntu24.04", "generic", {"amd64": kernel, "arm64": kernel}
        )
        self.assertEqual(
            platforms(output / f"driver-images-580-{kernel}-ubuntu24.04.tar"),
            ["amd64", "arm64"],
        )
        self.assertEqual(
            platforms(output / "base-images-580-6.8-generic-ubuntu24.04.tar"),
            ["amd64", "arm64"],
        )

    def test_assembly_with_different_native_kernels(self) -> None:
        output = self.assemble(
            "ubuntu26.04",
            "generic",
            {"amd64": "6.8.0-100-generic", "arm64": "6.8.0-101-generic"},
        )
        self.assertEqual(
            platforms(output / "driver-images-580-6.8.0-100-generic-ubuntu26.04.tar"),
            ["amd64"],
        )
        self.assertEqual(
            platforms(output / "driver-images-580-6.8.0-101-generic-ubuntu26.04.tar"),
            ["arm64"],
        )

    def test_amd64_only_flavor(self) -> None:
        output = self.assemble(
            "ubuntu24.04", "azure-fde", {"amd64": "6.8.0-100-azure-fde"}
        )
        self.assertEqual(
            platforms(output / "base-images-580-6.8-azure-fde-ubuntu24.04.tar"),
            ["amd64"],
        )

    def test_missing_supported_architecture_fails_assembly(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            self.assemble("ubuntu24.04", "generic", {"amd64": "6.8.0-100-generic"})

    def test_base_metadata_comes_from_loaded_native_image(self) -> None:
        self.executable("make", 'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n')
        self.executable(
            "docker",
            """printf '%s\\n' "$*" >> "$COMMAND_LOG"
case "$1" in
 create) echo native-container ;;
 cp) echo 'export KERNEL_VERSION=6.8.0-101-generic' \
     'DRIVER_VERSION=580.1 DRIVER_VERSIONS=580.1' > "$3" ;;
esac
""",
        )
        log, github_env = self.work / "commands", self.work / "github-env"
        env = dict(
            self.env,
            IMAGE_NAME="ghcr.io/my-fork/driver",
            DIST="ubuntu24.04",
            ARCH="arm64",
            DRIVER_BRANCH="580",
            KERNEL_FLAVOR="generic",
            LTS_KERNEL="6.8",
            BASE_VERSION="abcd1234",
            COMMAND_LOG=str(log),
            GITHUB_ENV=str(github_env),
        )
        subprocess.run(
            [self.bash, str(SCRIPTS / "build-precompiled-base.sh")],
            cwd=self.work,
            env=env,
            check=True,
        )
        commands = log.read_text()
        image = "ghcr.io/my-fork/driver:base-noble-6.8-generic-580-abcd1234-arm64"
        self.assertIn("DOCKER_BUILD_OPTIONS=--load", commands)
        self.assertIn("DOCKER_BUILD_PLATFORM_OPTIONS=--platform=linux/arm64", commands)
        self.assertIn("create --platform=linux/arm64 " + image, commands)
        self.assertIn("cp native-container:/var/kernel_version.txt", commands)
        self.assertIn("rm -f native-container", commands)
        self.assertNotIn("ghcr.io/nvidia", commands)
        self.assertIn("KERNEL_VERSION=6.8.0-101-generic", github_env.read_text())

    def test_make_command_line_platform_overrides_amd64_policy(self) -> None:
        result = subprocess.run(
            [
                "make",
                "-n",
                "BUILD_MULTI_ARCH_IMAGES=true",
                "DRIVER_VERSIONS=580.1",
                "DRIVER_BRANCH=580",
                "KERNEL_FLAVOR=azure-fde",
                "KERNEL_VERSION=6.8.0-100-azure-fde",
                "DOCKER_BUILD_PLATFORM_OPTIONS=--platform=linux/amd64",
                "DOCKER_BUILD_OPTIONS=--output=type=oci,dest=driver.tar",
                "build-signed_ubuntu24.04-580.1",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("--platform=linux/amd64", result.stdout)
        self.assertNotIn("linux/arm64", result.stdout)
        self.assertIn('--build-arg KERNEL_VERSION="6.8.0-100-azure-fde"', result.stdout)

    def test_e2e_selects_architecture_specific_metadata(self) -> None:
        artifacts = self.work / "kernel-version-artifacts"
        artifacts.mkdir()
        for arch, version in [
            ("amd64", "6.8.0-100-generic"),
            ("arm64", "6.8.0-101-generic"),
        ]:
            metadata = self.work / "kernel_version.txt"
            metadata.write_text(
                f"export KERNEL_VERSION={version} "
                "DRIVER_VERSION=580.1 DRIVER_VERSIONS=580.1\n"
            )
            with tarfile.open(
                artifacts / f"kernel-version-580-{version}-ubuntu24.04-{arch}.tar", "w"
            ) as output:
                output.add(metadata, arcname="kernel_version.txt")
        self.executable("curl", 'printf "#!/bin/bash\\nexit 1\\n" > bin/regctl\n')
        script = ROOT / "tests/scripts/findkernelversion.sh"
        result = subprocess.run(
            [
                self.bash,
                "-c",
                (
                    f'source "{script}" generic 580 ubuntu24.04 6.8 -arm64; '
                    'echo "$KERNEL_VERSION"'
                ),
            ],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "6.8.0-101-generic")
        result = subprocess.run(
            [self.bash, "-c", f'source "{script}" generic 595 ubuntu24.04 6.8 -arm64'],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing native kernel metadata", result.stderr)

    def test_image_workflow_manifest_groups_actual_kernels(self) -> None:
        workflow = (ROOT / ".github/workflows/image.yaml").read_text()
        content = workflow.split(
            "      - name: Assemble kernel tags from native images\n", 1
        )[1]
        script = "\n".join(
            line[10:] for line in content.split("        run: |\n", 1)[1].splitlines()
        )
        self.executable("docker", 'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n')
        log = self.work / "manifest-commands"
        for arch, kernel in [
            ("amd64", "6.8.0-100-generic"),
            ("arm64", "6.8.0-101-generic"),
        ]:
            folder = (
                self.work
                / f"metadata/precompiled-metadata-580-6.8-generic-ubuntu24.04-{arch}"
            )
            folder.mkdir(parents=True)
            (folder / "kernel_version.txt").write_text(
                f"export KERNEL_VERSION={kernel} "
                "DRIVER_VERSION=580.1 DRIVER_VERSIONS=580.1\n"
            )
        env = dict(
            self.env,
            GITHUB_REPOSITORY_OWNER="My-Fork",
            GITHUB_SHA="abcd1234567890",
            COMMAND_LOG=str(log),
        )
        subprocess.run([self.bash, "-c", script], cwd=self.work, env=env, check=True)
        commands = log.read_text().splitlines()
        self.assertEqual(len(commands), 2)
        for arch, kernel in [
            ("amd64", "6.8.0-100-generic"),
            ("arm64", "6.8.0-101-generic"),
        ]:
            line = next(line for line in commands if kernel in line)
            self.assertIn(
                f"--tag ghcr.io/my-fork/driver:abcd1234-580-{kernel}-ubuntu24.04", line
            )
            self.assertIn(
                f"ghcr.io/my-fork/driver:abcd1234-{arch}-580-{kernel}-ubuntu24.04", line
            )

    def test_empty_kernel_fails_before_upload(self) -> None:
        self.executable("make", "exit 0\n")
        self.executable(
            "docker",
            """case "$1" in
 create) echo native-container ;;
 cp) echo 'export KERNEL_VERSION= DRIVER_VERSION=580.1 DRIVER_VERSIONS=580.1' > "$3" ;;
esac
""",
        )
        env = dict(
            self.env,
            IMAGE_NAME="ghcr.io/my-fork/driver",
            DIST="ubuntu24.04",
            ARCH="arm64",
            DRIVER_BRANCH="580",
            KERNEL_FLAVOR="generic",
            LTS_KERNEL="6.8",
        )
        result = subprocess.run(
            [self.bash, str(SCRIPTS / "build-precompiled-base.sh")],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No supported kernel found", result.stderr)


if __name__ == "__main__":
    unittest.main()
