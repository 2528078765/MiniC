"""Update MiniC version files using decimal carry: 1.0.9 -> 1.1.0."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def parse_version(version: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.[0-9]\.[0-9]", version):
        raise ValueError("Use major.minor.patch; minor and patch must be digits 0-9.")
    return tuple(int(part) for part in version.split("."))


def next_version(version: str) -> str:
    major, minor, patch = parse_version(version)
    patch += 1
    if patch == 10:
        minor, patch = minor + 1, 0
    if minor == 10:
        major, minor = major + 1, 0
    return f"{major}.{minor}.{patch}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="version", help="Set an explicit version, e.g. 1.0.0")
    parser.add_argument("--dry-run", action="store_true", help="Show the next version without changes")
    args = parser.parse_args()
    config = ROOT / "pyproject.toml"
    current = tomllib.loads(config.read_text(encoding="utf-8"))["project"]["version"]
    target = args.version if args.version is not None else next_version(current)
    major, minor, patch = parse_version(target)
    changes = {}
    for relative in ["pyproject.toml", "src/minic/__init__.py", "packaging/version_info.txt",
                     "README.md", "README.en.md"]:
        path = ROOT / relative
        content = path.read_text(encoding="utf-8")
        updated = (content.replace(f"MiniC-Setup-{current}.exe", f"MiniC-Setup-{target}.exe")
                   if relative.startswith("README") else content.replace(current, target))
        if relative == "packaging/version_info.txt":
            updated = re.sub(r"((?:filevers|prodvers)=)\([^)]*\)",
                             rf"\g<1>({major}, {minor}, {patch}, 0)", updated)
        if updated != content:
            changes[path] = updated
    print(f"{current} -> {target}")
    if not args.dry_run:
        for path, content in changes.items():
            path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
