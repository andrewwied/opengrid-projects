#!/usr/bin/env python3
"""Build OpenSCAD models with project-managed, checksum-verified tools."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
TOOL_ROOT = REPOSITORY_ROOT / ".tools"
LOCK_PATH = Path(__file__).with_name("tool-lock.json")
PRESET_ROOT = REPOSITORY_ROOT / "presets"
BUILD_ROOT = REPOSITORY_ROOT / "build"
MODEL_FILES = tuple(sorted(REPOSITORY_ROOT.glob("*.scad")))
DRAWER_PART_OVERRIDES = {
    "shell": {
        "generate_drawer_shell": "true",
        "generate_drawer_container": "false",
        "generate_drawer_stopper_clips": "false",
    },
    "container": {
        "generate_drawer_shell": "false",
        "generate_drawer_container": "true",
        "generate_drawer_stopper_clips": "false",
    },
    "stopper-clips": {
        "generate_drawer_shell": "false",
        "generate_drawer_container": "false",
        "generate_drawer_stopper_clips": "true",
    },
}


class BuildError(RuntimeError):
    """A configuration, toolchain, or build failure."""


@dataclass(frozen=True)
class ToolPaths:
    openscad: Path
    bosl2: Path
    fonts: Path


def load_lock() -> dict[str, Any]:
    with LOCK_PATH.open(encoding="utf-8") as file:
        lock = json.load(file)
    if lock.get("schema_version") != 1:
        raise BuildError(f"Unsupported tool lock schema in {LOCK_PATH}.")
    return lock


def platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    aliases = {"amd64": "x86_64", "x86-64": "x86_64", "arm64": "aarch64"}
    return f"{system}-{aliases.get(machine, machine)}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, expected: str) -> None:
    actual = sha256(path)
    if actual != expected.lower():
        raise BuildError(
            f"Checksum mismatch for {path}. Expected {expected.lower()}, received {actual}."
        )


def download(url: str, destination: Path, expected_hash: str, offline: bool) -> None:
    if destination.is_file():
        verify(destination, expected_hash)
        return
    if offline:
        raise BuildError(f"Offline mode cannot download {url}. Missing {destination}.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f"{destination.name}.", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            print(f"Downloading {url}")
            with urllib.request.urlopen(url) as response:
                shutil.copyfileobj(response, temporary)
        verify(temporary_path, expected_hash)
        temporary_path.replace(destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def extract_zip(archive: Path, destination: Path) -> None:
    temporary = destination.with_name(f"{destination.name}.partial")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)

    try:
        with zipfile.ZipFile(archive) as zip_file:
            root = temporary.resolve()
            for member in zip_file.infolist():
                target = (temporary / member.filename).resolve()
                if not target.is_relative_to(root):
                    raise BuildError(f"Unsafe archive path in {archive}: {member.filename}")
            zip_file.extractall(temporary)
        shutil.rmtree(destination, ignore_errors=True)
        temporary.replace(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def find_single(root: Path, filename: str) -> Path:
    matches = list(root.rglob(filename))
    if len(matches) != 1:
        raise BuildError(f"Expected one {filename} below {root}, found {len(matches)}.")
    return matches[0]


def setup_tools(lock: dict[str, Any], offline: bool) -> ToolPaths:
    key = platform_key()
    open_scad_spec = lock["openscad"].get(key)
    if open_scad_spec is None:
        raise BuildError(
            f"No managed OpenSCAD build is locked for {key}. "
            "Windows x86_64 is the currently verified managed platform."
        )

    download_root = TOOL_ROOT / "downloads"
    openscad_archive = download_root / f"openscad-{open_scad_spec['version']}-{key}.zip"
    download(open_scad_spec["url"], openscad_archive, open_scad_spec["sha256"], offline)
    openscad_root = TOOL_ROOT / "openscad" / open_scad_spec["version"] / key
    if not (openscad_root / ".complete").is_file():
        extract_zip(openscad_archive, openscad_root)
        find_single(openscad_root, "openscad.com")
        (openscad_root / ".complete").write_text(open_scad_spec["sha256"], encoding="utf-8")

    bosl_spec = lock["bosl2"]
    bosl_archive = download_root / f"bosl2-{bosl_spec['revision']}.zip"
    download(bosl_spec["url"], bosl_archive, bosl_spec["sha256"], offline)
    bosl_root = TOOL_ROOT / "bosl2" / bosl_spec["revision"] / "BOSL2"
    if not (bosl_root / "std.scad").is_file():
        extracted = bosl_root.parent.with_name(f"{bosl_root.parent.name}.archive")
        extract_zip(bosl_archive, extracted)
        source = find_single(extracted, "std.scad").parent
        shutil.rmtree(bosl_root.parent, ignore_errors=True)
        bosl_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(bosl_root))
        shutil.rmtree(extracted, ignore_errors=True)

    fonts_root = TOOL_ROOT / "fonts"
    for font_spec in lock["fonts"]:
        download(
            font_spec["url"],
            fonts_root / font_spec["filename"],
            font_spec["sha256"],
            offline,
        )

    return ToolPaths(
        openscad=find_single(openscad_root, "openscad.com"),
        bosl2=bosl_root,
        fonts=fonts_root,
    )


def existing_tools(lock: dict[str, Any]) -> ToolPaths:
    key = platform_key()
    spec = lock["openscad"].get(key)
    if spec is None:
        raise BuildError(f"No managed OpenSCAD build is locked for {key}.")
    root = TOOL_ROOT / "openscad" / spec["version"] / key
    bosl_root = TOOL_ROOT / "bosl2" / lock["bosl2"]["revision"] / "BOSL2"
    fonts_root = TOOL_ROOT / "fonts"
    try:
        openscad = find_single(root, "openscad.com")
        if not (bosl_root / "std.scad").is_file():
            raise BuildError(f"Missing managed BOSL2 library at {bosl_root}.")
        missing_fonts = [
            spec["filename"]
            for spec in lock["fonts"]
            if not (fonts_root / spec["filename"]).is_file()
        ]
        if missing_fonts:
            raise BuildError(f"Missing managed font files: {', '.join(missing_fonts)}.")
        return ToolPaths(openscad, bosl_root, fonts_root)
    except BuildError as error:
        raise BuildError("Managed tools are not ready. Run setup first.") from error


def normalize_model(model: str) -> Path:
    name = model if model.endswith(".scad") else f"{model}.scad"
    source = REPOSITORY_ROOT / name
    if source not in MODEL_FILES:
        choices = ", ".join(path.stem for path in MODEL_FILES)
        raise BuildError(f"Unknown model {model!r}. Available models: {choices}")
    return source


def parse_assignment(assignment: str) -> tuple[str, str]:
    if "=" not in assignment:
        raise BuildError(f"Invalid --set value {assignment!r}; expected NAME=OPENSCAD_EXPRESSION.")
    name, value = assignment.split("=", 1)
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise BuildError(f"Invalid OpenSCAD parameter name {name!r}.")
    if not value:
        raise BuildError(f"Missing OpenSCAD expression for {name!r}.")
    return name, value


def load_preset(model: Path, preset: str | None) -> dict[str, str]:
    if preset is None:
        return {}
    path = PRESET_ROOT / model.stem / f"{preset}.json"
    if not path.is_file():
        raise BuildError(f"Preset {preset!r} does not exist at {path}.")
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    parameters = data.get("parameters")
    if not isinstance(parameters, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in parameters.items()
    ):
        raise BuildError(f"Preset {path} must contain string parameters.")
    return parameters


def build_environment(tools: ToolPaths) -> dict[str, str]:
    environment = os.environ.copy()
    separator = os.pathsep
    environment["OPENSCADPATH"] = separator.join(
        item
        for item in (str(tools.bosl2.parent), environment.get("OPENSCADPATH", ""))
        if item
    )
    environment["OPENSCAD_FONT_PATH"] = separator.join(
        item
        for item in (str(tools.fonts), environment.get("OPENSCAD_FONT_PATH", ""))
        if item
    )
    fontconfig_root = TOOL_ROOT / "fontconfig"
    fontconfig_cache = fontconfig_root / "cache"
    fontconfig_config = fontconfig_root / "fonts.conf"
    fontconfig_cache.mkdir(parents=True, exist_ok=True)
    fontconfig_config.write_text(
        "\n".join(
            (
                '<?xml version="1.0"?>',
                '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">',
                "<fontconfig>",
                f"  <dir>{xml_escape(str(tools.fonts))}</dir>",
                f"  <cachedir>{xml_escape(str(fontconfig_cache))}</cachedir>",
                "</fontconfig>",
                "",
            )
        ),
        encoding="utf-8",
    )
    environment["FONTCONFIG_FILE"] = str(fontconfig_config)
    environment["FONTCONFIG_PATH"] = str(fontconfig_root)
    return environment


def git_revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_export(
    tools: ToolPaths,
    source: Path,
    output: Path,
    export_format: str,
    definitions: dict[str, str],
    strict: bool,
) -> str:
    command = [
        str(tools.openscad),
        "--backend",
        "Manifold",
        "--export-format",
        export_format,
        "-o",
        str(output),
    ]
    for name, value in sorted(definitions.items()):
        command.extend(["-D", f"{name}={value}"])
    command.append(str(source))
    result = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=build_environment(tools),
        text=True,
        capture_output=True,
    )
    diagnostics = f"{result.stdout}{result.stderr}"
    if result.returncode != 0:
        raise BuildError(f"OpenSCAD failed for {output.name}:\n{diagnostics}")
    if strict and "warning" in diagnostics.lower():
        raise BuildError(f"OpenSCAD emitted a warning for {output.name}:\n{diagnostics}")
    if not output.is_file() or output.stat().st_size == 0:
        raise BuildError(f"OpenSCAD did not create a non-empty {output}.")
    if export_format == "binstl" and output.stat().st_size < 84:
        raise BuildError(f"{output} is not a valid binary STL-sized output.")
    if export_format == "3mf":
        with output.open("rb") as file:
            if file.read(2) != b"PK":
                raise BuildError(f"{output} is not a valid ZIP-based 3MF package.")
    return diagnostics


def print_diagnostics(diagnostics: str) -> None:
    if diagnostics.strip():
        print(diagnostics, end="" if diagnostics.endswith("\n") else "\n")


def command_setup(args: argparse.Namespace) -> int:
    setup_tools(load_lock(), args.offline)
    print(f"Managed tools are ready in {TOOL_ROOT}.")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    lock = load_lock()
    print(f"Platform: {platform_key()}")
    print(f"Managed tool directory: {TOOL_ROOT}")
    try:
        tools = existing_tools(lock)
        version = subprocess.run(
            [str(tools.openscad), "--version"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        print(f"OpenSCAD: {version}")
        print(f"BOSL2: {lock['bosl2']['revision']}")
        print(f"Font directory: {tools.fonts}")
    except (BuildError, subprocess.CalledProcessError) as error:
        print(f"Status: {error}")
        return 1
    print("Status: ready")
    return 0


def command_list(_: argparse.Namespace) -> int:
    for model in MODEL_FILES:
        presets = sorted((PRESET_ROOT / model.stem).glob("*.json"))
        preset_names = ", ".join(item.stem for item in presets) or "none"
        print(f"{model.stem}: presets: {preset_names}")
    print("openconnect_drawer parts: shell, container, stopper-clips")
    return 0


def command_build(args: argparse.Namespace) -> int:
    lock = load_lock()
    tools = setup_tools(lock, args.offline)
    sources = MODEL_FILES if args.all else (normalize_model(args.model),)
    if args.all and args.part:
        raise BuildError("--part is only supported when building one model.")

    for source in sources:
        definitions = load_preset(source, args.preset)
        for assignment in args.set:
            name, value = parse_assignment(assignment)
            definitions[name] = value
        part = args.part or "default"
        if args.part:
            if source.stem != "openconnect_drawer":
                raise BuildError("--part currently applies only to openconnect_drawer.")
            try:
                definitions.update(DRAWER_PART_OVERRIDES[args.part])
            except KeyError as error:
                choices = ", ".join(DRAWER_PART_OVERRIDES)
                raise BuildError(f"Unknown drawer part {args.part!r}; choose {choices}.") from error

        variant = args.preset or "default"
        output_directory = BUILD_ROOT / source.stem / variant / part
        output_directory.mkdir(parents=True, exist_ok=True)
        diagnostics: dict[str, str] = {}
        for requested_format in args.format:
            export_format = "binstl" if requested_format == "stl" else "3mf"
            output = output_directory / f"{source.stem}.{requested_format}"
            diagnostics[requested_format] = run_export(
                tools, source, output, export_format, definitions, args.strict
            )
            print_diagnostics(diagnostics[requested_format])
            print(f"Created {output.relative_to(REPOSITORY_ROOT)}")

        provenance = {
            "source": source.name,
            "preset": args.preset,
            "part": part,
            "definitions": definitions,
            "formats": args.format,
            "openscad": lock["openscad"][platform_key()]["version"],
            "bosl2_revision": lock["bosl2"]["revision"],
            "git_revision": git_revision(),
            "diagnostics": diagnostics,
        }
        (output_directory / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline", action="store_true", help="Do not download missing managed tools."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("setup", help="Download and verify managed printing tools.")
    subparsers.add_parser("doctor", help="Report whether managed tools are ready.")
    subparsers.add_parser("list", help="List models and project presets.")
    build = subparsers.add_parser("build", help="Render models to STL and/or 3MF.")
    build.add_argument("model", nargs="?", help="Model filename or stem.")
    build.add_argument("--all", action="store_true", help="Build every model.")
    build.add_argument("--format", nargs="+", choices=("stl", "3mf"), required=True)
    build.add_argument("--preset", help="Versioned preset name for the selected model.")
    build.add_argument(
        "--part", help="Named subpart to build; currently supports openconnect_drawer."
    )
    build.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=EXPRESSION",
        help="Override an OpenSCAD parameter.",
    )
    build.add_argument(
        "--strict", action="store_true", help="Fail if OpenSCAD emits any warning."
    )
    return parser


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()
    try:
        if args.command == "build" and (bool(args.model) == bool(args.all)):
            raise BuildError("Specify exactly one model or --all.")
        commands = {
            "setup": command_setup,
            "doctor": command_doctor,
            "list": command_list,
            "build": command_build,
        }
        return commands[args.command](args)
    except BuildError as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
