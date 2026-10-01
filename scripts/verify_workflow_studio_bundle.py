"""Verify the offline Workflow Studio runtime bundle without third-party packages."""

from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import os
import posixpath
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


MANIFEST_NAME = "bundle-manifest.json"
LICENSES_NAME = "THIRD_PARTY_LICENSES.json"
BUNDLE_SCHEMA = "workflow-studio-bundle-v1"
LICENSES_SCHEMA = "workflow-studio-third-party-licenses-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
CSS_URL_RE = re.compile(r"url\(\s*(?:(['\"])(.*?)\1|([^)]*?))\s*\)", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\(\s*)?(['\"])(.*?)\1\s*\)?", re.IGNORECASE)
JS_STATIC_CALL_RE = re.compile(
    r"\b(fetch|WebSocket|EventSource)\s*\(\s*(['\"`])([^'\"`]*)\2",
    re.IGNORECASE,
)
JS_DYNAMIC_IMPORT_RE = re.compile(r"\bimport\s*\(\s*(['\"`])([^'\"`]*)\1")
JS_ATTRIBUTE_ASSIGNMENT_RE = re.compile(
    r"(?:\.\s*(src|href|poster|srcset)|\[\s*['\"](src|href|poster|srcset)['\"]\s*\])\s*=\s*(['\"`])([^'\"`]*)\3",
    re.IGNORECASE,
)
JS_SET_ATTRIBUTE_RE = re.compile(
    r"\bsetAttribute\s*\(\s*(['\"])(src|href|poster|srcset)\1\s*,\s*(['\"`])([^'\"`]*)\3",
    re.IGNORECASE,
)
JS_TELEMETRY_RE = re.compile(r"\bnavigator\s*\.\s*sendBeacon\s*\(", re.IGNORECASE)
SVG_RESOURCE_TAGS = {"image", "use", "feimage", "script", "link"}
HTML_RESOURCE_TAGS = {
    "script", "link", "img", "source", "video", "audio", "iframe", "object",
    "embed", "image", "use", "feimage",
}
RESOURCE_ATTRIBUTES = {"src", "href", "poster", "srcset", "xlink:href"}
JSON_RESOURCE_KEYS = {
    "src", "href", "poster", "srcset", "url", "uri", "font", "image", "asset",
    "stylesheet", "script", "endpoint", "api_url", "font_url", "image_url",
}
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_LICENSE_BYTES = 4 * 1024 * 1024
MAX_SCANNED_TEXT_BYTES = 16 * 1024 * 1024


class _ResourceParser(html.parser.HTMLParser):
    def __init__(self, *, source: str, errors: list[str], root: Path) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.errors = errors
        self.root = root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag, attrs)

    def _check(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        allowed_tag = normalized_tag in HTML_RESOURCE_TAGS or normalized_tag in SVG_RESOURCE_TAGS
        if not allowed_tag:
            return
        for name, value in attrs:
            normalized_name = name.lower()
            if value is None or normalized_name not in RESOURCE_ATTRIBUTES:
                continue
            if normalized_name == "srcset":
                candidates = [part.strip().split()[0] for part in value.split(",") if part.strip()]
            else:
                candidates = [value]
            for candidate in candidates:
                _check_resource_reference(candidate, self.source, self.root, self.errors)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_relative_path(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        return None
    reserved = {"con", "prn", "aux", "nul"}
    reserved.update(f"com{index}" for index in range(1, 10))
    reserved.update(f"lpt{index}" for index in range(1, 10))
    for part in raw.split("/"):
        if part in {"", ".", ".."} or ":" in part or part.endswith((" ", ".")):
            return None
        if part.split(".", 1)[0].casefold() in reserved:
            return None
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    normalized = path.as_posix()
    if normalized != raw or normalized in {".", ".."}:
        return None
    return normalized


def _check_resource_reference(raw: str, source: str, root: Path, errors: list[str]) -> None:
    value = raw.strip().strip("\"'")
    if not value or value.startswith("#"):
        return
    if value.lower().startswith("data:"):
        return
    if "\\" in value or "\x00" in value or value.startswith("/"):
        errors.append(f"{source}: resource must be relative to the bundle: {value[:160]}")
        return
    try:
        parsed = urlsplit(value)
    except ValueError:
        errors.append(f"{source}: malformed resource reference: {value[:160]}")
        return
    if parsed.scheme or parsed.netloc or value.startswith("//"):
        errors.append(f"{source}: external resource is not allowed: {value[:160]}")
        return
    decoded_path = unquote(parsed.path)
    if decoded_path.startswith("/") or "\\" in decoded_path or "\x00" in decoded_path:
        errors.append(f"{source}: resource path is not bundle-relative: {value[:160]}")
        return
    if not decoded_path:
        return
    normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source), decoded_path))
    if normalized == ".." or normalized.startswith("../"):
        errors.append(f"{source}: resource escapes the bundle: {value[:160]}")
        return
    target = root.joinpath(*PurePosixPath(normalized).parts)
    try:
        if _has_symlink_component(root, target) or not target.is_file():
            errors.append(f"{source}: referenced bundle resource is missing or not a regular file: {value[:160]}")
    except OSError:
        errors.append(f"{source}: referenced bundle resource cannot be inspected: {value[:160]}")


def _scan_text_resource_file(relative: str, content: str, root: Path, errors: list[str]) -> None:
    suffix = PurePosixPath(relative).suffix.lower()
    if "sourceMappingURL=" in content:
        errors.append(f"{relative}: source maps are not part of the runtime bundle")
    if suffix in {".html", ".svg", ".xml"}:
        try:
            parser = _ResourceParser(source=relative, errors=errors, root=root)
            parser.feed(content)
            parser.close()
        except (ValueError, AssertionError) as exc:
            errors.append(f"{relative}: resource markup could not be parsed: {exc}")
    if suffix in {".css", ".svg", ".xml"}:
        for match in CSS_URL_RE.finditer(content):
            candidate = match.group(2) if match.group(1) else match.group(3)
            if candidate is not None:
                _check_resource_reference(candidate.strip(), relative, root, errors)
        for match in CSS_IMPORT_RE.finditer(content):
            _check_resource_reference(match.group(2).strip(), relative, root, errors)
    if suffix in {".js", ".mjs", ".cjs"}:
        if JS_TELEMETRY_RE.search(content):
            errors.append(f"{relative}: beacon telemetry is not allowed in the runtime bundle")
        for match in JS_STATIC_CALL_RE.finditer(content):
            argument = match.group(3).strip()
            if "${" not in argument:
                if match.group(1).lower() == "fetch" and argument.startswith("./api/"):
                    if ".." in PurePosixPath(argument).parts or "\\" in argument:
                        errors.append(f"{relative}: unsafe local API route: {argument[:160]}")
                else:
                    _check_resource_reference(argument, relative, root, errors)
        for match in JS_DYNAMIC_IMPORT_RE.finditer(content):
            argument = match.group(2).strip()
            if "${" not in argument:
                _check_resource_reference(argument, relative, root, errors)
        for match in JS_ATTRIBUTE_ASSIGNMENT_RE.finditer(content):
            _check_resource_reference(match.group(4).strip(), relative, root, errors)
        for match in JS_SET_ATTRIBUTE_RE.finditer(content):
            _check_resource_reference(match.group(4).strip(), relative, root, errors)


def _scan_runtime_json(relative: str, value: object, root: Path, errors: list[str], depth: int = 0) -> None:
    if depth > 64:
        errors.append(f"{relative}: runtime JSON nesting exceeds the scan limit")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in JSON_RESOURCE_KEYS and isinstance(child, str):
                _check_resource_reference(child, relative, root, errors)
            else:
                _scan_runtime_json(relative, child, root, errors, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _scan_runtime_json(relative, child, root, errors, depth + 1)


def _walk_regular_files(root: Path, errors: list[str]) -> set[str]:
    files: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as stream:
                entries = sorted(stream, key=lambda item: item.name)
        except OSError as exc:
            errors.append(f"cannot inspect bundle directory: {exc}")
            continue
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                errors.append(f"{relative}: cannot inspect filesystem entry: {exc}")
                continue
            if stat.S_ISLNK(mode):
                errors.append(f"{relative}: symbolic links are not allowed in the bundle")
            elif stat.S_ISDIR(mode):
                pending.append(path)
            elif stat.S_ISREG(mode):
                files.add(relative)
            else:
                errors.append(f"{relative}: only regular files and directories are allowed")
    return files


def _has_symlink_component(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_license_inventory(path: Path, errors: list[str]) -> None:
    try:
        if path.stat().st_size > MAX_LICENSE_BYTES:
            raise ValueError("license inventory exceeds the scan size limit")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        errors.append(f"{LICENSES_NAME}: invalid third-party license inventory: {exc}")
        return
    if not isinstance(value, dict) or set(value) != {"schema_version", "packages"}:
        errors.append(f"{LICENSES_NAME}: inventory fields are invalid")
        return
    if value.get("schema_version") != LICENSES_SCHEMA or not isinstance(value.get("packages"), list):
        errors.append(f"{LICENSES_NAME}: inventory schema is invalid")
        return
    previous: tuple[str, str] | None = None
    for item in value["packages"]:
        if not isinstance(item, dict) or set(item) != {"name", "version", "license", "homepage", "repository"}:
            errors.append(f"{LICENSES_NAME}: package entry has invalid fields")
            continue
        if not all(isinstance(item[field], str) for field in ("name", "version", "license", "homepage", "repository")) or not item["license"]:
            errors.append(f"{LICENSES_NAME}: package entry is missing declared metadata")
        identity = (item["name"], item["version"]) if isinstance(item.get("name"), str) and isinstance(item.get("version"), str) else None
        if identity is not None and previous is not None and identity < previous:
            errors.append(f"{LICENSES_NAME}: package entries are not sorted")
        if identity is not None:
            previous = identity


def verify_bundle(path: Path | str) -> dict[str, object]:
    """Return a deterministic verification report for one prebuilt Studio bundle."""
    root_input = Path(path).expanduser()
    errors: list[str] = []
    try:
        if root_input.is_symlink() or not root_input.exists() or not root_input.is_dir():
            return {"status": "fail", "file_count": 0, "aggregate_sha256": "", "errors": ["bundle root must be a real directory"]}
        root = root_input.resolve(strict=True)
    except OSError as exc:
        return {"status": "fail", "file_count": 0, "aggregate_sha256": "", "errors": [f"bundle root cannot be inspected: {exc}"]}

    actual_files = _walk_regular_files(root, errors)
    manifest_path = root / MANIFEST_NAME
    try:
        if MANIFEST_NAME not in actual_files:
            raise OSError("manifest is not a regular file in the bundle")
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("manifest exceeds the scan size limit")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        manifest = None
        errors.append(f"{MANIFEST_NAME}: missing or invalid JSON manifest: {exc}")

    listed: dict[str, str] = {}
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "release_version", "files"}:
        errors.append(f"{MANIFEST_NAME}: manifest fields are invalid")
    else:
        if manifest.get("schema_version") != BUNDLE_SCHEMA:
            errors.append(f"{MANIFEST_NAME}: unsupported schema version")
        version = manifest.get("release_version")
        if not isinstance(version, str) or not RELEASE_VERSION_RE.fullmatch(version):
            errors.append(f"{MANIFEST_NAME}: release_version is invalid")
        files_value = manifest.get("files")
        if not isinstance(files_value, dict):
            errors.append(f"{MANIFEST_NAME}: files must be a path-to-SHA-256 object")
        else:
            for raw_name, raw_hash in files_value.items():
                relative = _safe_relative_path(raw_name)
                if relative is None or relative == MANIFEST_NAME:
                    errors.append(f"{MANIFEST_NAME}: unsafe or reserved file path: {raw_name!r}")
                    continue
                if not isinstance(raw_hash, str) or not SHA256_RE.fullmatch(raw_hash):
                    errors.append(f"{MANIFEST_NAME}: invalid SHA-256 for {relative}")
                    continue
                listed[relative] = raw_hash

    actual_runtime_files = actual_files - {MANIFEST_NAME}
    for relative in sorted(actual_runtime_files - listed.keys()):
        errors.append(f"unlisted runtime file: {relative}")
    for relative in sorted(listed.keys() - actual_runtime_files):
        errors.append(f"manifest file is missing: {relative}")

    verified_hashes: dict[str, str] = {}
    for relative, expected_hash in sorted(listed.items()):
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        try:
            if _has_symlink_component(root, candidate) or not candidate.is_file():
                raise OSError("path contains a symbolic link or is not a regular file")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            digest = _sha256_file(candidate)
        except (OSError, ValueError) as exc:
            errors.append(f"{relative}: file is not safely contained in the bundle: {exc}")
            continue
        verified_hashes[relative] = digest
        if digest != expected_hash:
            errors.append(f"{relative}: SHA-256 does not match {MANIFEST_NAME}")
        if PurePosixPath(relative).suffix.lower() == ".map":
            errors.append(f"{relative}: source-map files are not allowed in the runtime bundle")
        if relative != LICENSES_NAME and PurePosixPath(relative).suffix.lower() == ".json":
            try:
                if candidate.stat().st_size > MAX_SCANNED_TEXT_BYTES:
                    raise ValueError("runtime JSON exceeds the scan size limit")
                config = json.loads(candidate.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
            except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
                errors.append(f"{relative}: runtime JSON is invalid: {exc}")
            else:
                _scan_runtime_json(relative, config, root, errors)
        if PurePosixPath(relative).suffix.lower() in {".html", ".css", ".js", ".mjs", ".cjs", ".svg", ".xml"}:
            try:
                if candidate.stat().st_size > MAX_SCANNED_TEXT_BYTES:
                    errors.append(f"{relative}: text resource exceeds the scan size limit")
                    continue
                _scan_text_resource_file(relative, candidate.read_text(encoding="utf-8"), root, errors)
            except (OSError, UnicodeError) as exc:
                errors.append(f"{relative}: text resource could not be read as UTF-8: {exc}")

    licenses_path = root / LICENSES_NAME
    if LICENSES_NAME not in actual_runtime_files:
        errors.append(f"required file is missing: {LICENSES_NAME}")
    else:
        _validate_license_inventory(licenses_path, errors)

    aggregate_sha256 = hashlib.sha256(_canonical_json(verified_hashes)).hexdigest() if verified_hashes else ""
    return {
        "status": "pass" if not errors else "fail",
        "file_count": len(actual_runtime_files),
        "aggregate_sha256": aggregate_sha256,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="prebuilt assets/workflow-studio directory")
    args = parser.parse_args(argv)
    result = verify_bundle(args.bundle)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
