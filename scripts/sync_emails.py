#!/usr/bin/env python3
"""Fetch all SFMC Content Builder assets (emails, templates, content blocks,
and images) and write them as individual files for version tracking.

Credentials are read from environment variables:
    SFMC_CLIENT_ID
    SFMC_CLIENT_SECRET
    SFMC_SUBDOMAIN

Output:
    email-content/emails/{id}_{name}.html           -- one file per email
    email-content/emails/manifest.json              -- metadata index of emails
    email-content/templates/{id}_{name}.html        -- one file per template
    email-content/templates/manifest.json           -- metadata index of templates
    email-content/content-blocks/{id}_{name}.*      -- one file per content block
    email-content/content-blocks/manifest.json      -- metadata index of blocks
    email-content/images/{id}_{name}.{ext}          -- one file per image
    email-content/images/manifest.json              -- metadata index of images
    email-content/CHANGELOG.md                      -- running history of changes
    email-content/.commit-summary                   -- consumed by the workflow
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "email-content"
EMAILS_DIR = OUTPUT_DIR / "emails"
TEMPLATES_DIR = OUTPUT_DIR / "templates"
CONTENT_BLOCKS_DIR = OUTPUT_DIR / "content-blocks"
IMAGES_DIR = OUTPUT_DIR / "images"

EMAIL_TYPE_IDS = [
    207,  # templatebasedemail
    208,  # htmlemail
    209,  # textonlyemail
]

TEMPLATE_TYPE_IDS = [
    4,    # template (legacy / pre-Content Builder; some "Save as Template" flows)
    210,  # template (Content Builder)
]

# Name-based catch-all for future / unknown template asset-type IDs.
# The canonical assetType.name for every template flavor in SFMC is "template",
# so filtering by name covers any tenant that surfaces an ID we haven't listed.
TEMPLATE_TYPE_NAMES = ["template"]

CONTENT_BLOCK_TYPE_IDS = [
    195,  # webpage
    196,  # textblock
    197,  # htmlblock
    198,  # imageblock
    199,  # buttonblock
    202,  # smartcaptureblock
    203,  # smartcaptureformfieldblock
    205,  # socialshareblock
    206,  # socialfollowblock
    214,  # freeformblock
    220,  # codesnippetblock
    227,  # dynamiccontentblock
    230,  # livecontent
    231,  # referenceblock
    232,  # imagecarouselblock
    233,  # customblock
    236,  # abtestblock
]

IMAGE_TYPE_IDS = [
    28,   # image (gif)
    29,   # image (jpeg)
    30,   # image (png)
    31,   # image (unknown/other)
]

PAGE_SIZE = 2500

CHANGELOG_FILE = OUTPUT_DIR / "CHANGELOG.md"
COMMIT_SUMMARY_FILE = OUTPUT_DIR / ".commit-summary"
META_FILES = {"manifest.json"}

MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "application/pdf": ".pdf",
}


# ---------------------------------------------------------------------------
# SFMC Authentication
# ---------------------------------------------------------------------------

def authenticate(client_id: str, client_secret: str, subdomain: str) -> tuple[str, str]:
    """Return (access_token, rest_base_url) via client_credentials grant."""
    auth_url = f"https://{subdomain}.auth.marketingcloudapis.com/v2/token"
    resp = httpx.post(
        auth_url,
        json={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    rest_url = data.get("rest_instance_url", f"https://{subdomain}.rest.marketingcloudapis.com")
    return data["access_token"], rest_url.rstrip("/")


# ---------------------------------------------------------------------------
# Content Builder API
# ---------------------------------------------------------------------------

def _build_asset_query(
    type_ids: list[int], type_names: list[str] | None = None,
) -> dict[str, Any]:
    """Build a Content Builder Asset query clause.

    If type_names is provided, the resulting query matches assets whose
    assetType.id is in type_ids OR whose assetType.name equals any of
    type_names. This guards against SFMC introducing new asset-type IDs
    for the same conceptual category (e.g. new "template" subtypes).
    """
    id_clause: dict[str, Any] = {
        "property": "assetType.id",
        "simpleOperator": "in",
        "value": type_ids,
    }
    if not type_names:
        return id_clause

    name_clauses: list[dict[str, Any]] = [
        {"property": "assetType.name", "simpleOperator": "equal", "value": n}
        for n in type_names
    ]
    combined: dict[str, Any] = id_clause
    for clause in name_clauses:
        combined = {
            "leftOperand": combined,
            "logicalOperator": "OR",
            "rightOperand": clause,
        }
    return combined


def fetch_assets(
    token: str,
    rest_url: str,
    type_ids: list[int],
    label: str,
    type_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Paginate through the Content Builder Asset query and return all items
    matching the given asset type IDs (and optional type names)."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    all_items: list[dict[str, Any]] = []
    page = 1
    query = _build_asset_query(type_ids, type_names)

    while True:
        body: dict[str, Any] = {
            "page": {"page": page, "pageSize": PAGE_SIZE},
            "query": query,
        }
        resp = httpx.post(
            f"{rest_url}/asset/v1/content/assets/query",
            headers=headers,
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        all_items.extend(items)

        count = data.get("count", 0)
        type_breakdown: dict[str, int] = {}
        for it in items:
            at = it.get("assetType", {})
            key = f"{at.get('id', '?')}:{at.get('name', '?')}"
            type_breakdown[key] = type_breakdown.get(key, 0) + 1
        breakdown_str = ", ".join(f"{k}={v}" for k, v in sorted(type_breakdown.items()))
        print(
            f"  [{label}] Page {page}: {len(items)} items "
            f"(total {len(all_items)}/{count}) | types: {breakdown_str or 'none'}"
        )

        if not items or page * PAGE_SIZE >= count:
            break
        page += 1

    return all_items


TEXT_ONLY_TYPE_ID = 209


def fetch_asset_detail(token: str, rest_url: str, asset_id: int) -> dict[str, Any]:
    """Fetch a single asset by ID to get the full views/content payload."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = httpx.get(
        f"{rest_url}/asset/v1/content/assets/{asset_id}",
        headers=headers,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def enrich_assets(
    assets: list[dict[str, Any]], token: str, rest_url: str, label: str,
) -> list[dict[str, Any]]:
    """Re-fetch every asset individually so we get the full content payload.

    The bulk query endpoint (/asset/v1/content/assets/query) often returns
    truncated or missing views.html.content / views.text.content, especially
    for template-based emails and text-only emails. Fetching each asset by
    ID via /asset/v1/content/assets/{id} guarantees the complete payload
    including all AMPscript, full HTML, subject lines, and preheaders.
    """
    enriched: list[dict[str, Any]] = []
    total = len(assets)
    for idx, item in enumerate(assets, 1):
        asset_id = item.get("id", 0)
        name = item.get("name", "unnamed")
        try:
            full_item = fetch_asset_detail(token, rest_url, asset_id)
            enriched.append(full_item)
        except httpx.HTTPStatusError:
            print(f"  Warning: could not fetch detail for {label} asset {asset_id} ({name}), using bulk data")
            enriched.append(item)
        if idx % 25 == 0 or idx == total:
            print(f"  [{label}] Enriched {idx}/{total}")
    return enriched


# ---------------------------------------------------------------------------
# File writing with change detection
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^\w\s\-]", "", name)
    name = re.sub(r"\s+", "_", name)
    return name or "unnamed"


def _asset_metadata(item: dict[str, Any]) -> dict[str, str]:
    """Extract human-readable metadata from an SFMC asset item."""
    modified_by = item.get("modifiedBy", {})
    return {
        "name": item.get("name", "unnamed"),
        "modifiedByName": modified_by.get("name", "unknown"),
        "modifiedDate": item.get("modifiedDate", ""),
    }


def _load_previous_manifest(directory: Path) -> dict[str, dict[str, Any]]:
    """Load the previous manifest.json from a directory, keyed by filename."""
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {e["file"]: e for e in entries}
    except (json.JSONDecodeError, KeyError):
        return {}


TEMPLATE_BASED_TYPE_ID = 207

# Asset IDs whose content is authored as a skeleton + slot blocks and should
# therefore be compiled (slot placeholders replaced with slot content) before
# writing to disk. Covers template-based emails AND the template assets
# themselves so templates with pre-filled default blocks render correctly.
SLOT_MERGE_TYPE_IDS: set[int] = {TEMPLATE_BASED_TYPE_ID, *TEMPLATE_TYPE_IDS}


def _is_text_only(item: dict[str, Any]) -> bool:
    return item.get("assetType", {}).get("id", 0) == TEXT_ONLY_TYPE_ID


def _is_template_based(item: dict[str, Any]) -> bool:
    at = item.get("assetType", {})
    if at.get("id", 0) in SLOT_MERGE_TYPE_IDS:
        return True
    return at.get("name", "") == "template"


def _build_metadata_header(item: dict[str, Any]) -> str:
    """Build a header block with subject/preheader so changes to those
    fields appear in git diffs, not just in manifest.json."""
    views = item.get("views", {})
    subject = views.get("subjectline", {}).get("content", "")
    preheader = views.get("preheader", {}).get("content", "")

    parts: list[str] = []
    if subject:
        parts.append(f"Subject: {subject}")
    if preheader:
        parts.append(f"Preheader: {preheader}")

    if not parts:
        return ""
    return "\n".join(parts) + "\n---\n"


def _get_slot_content(slots: dict[str, Any], slot_key: str) -> str:
    """Extract the combined HTML content for a single slot key."""
    slot = slots.get(slot_key, {})
    blocks = slot.get("blocks", {})
    parts: list[str] = []
    for block_key in sorted(blocks.keys()):
        block_content = blocks[block_key].get("content", "")
        if block_content:
            parts.append(block_content)
    return "".join(parts)


_SLOT_DIV_RE = re.compile(
    r'<div\s+data-type="slot"\s+data-key="([^"]+)"[^>]*>'
    r'[\s]*'
    r'</div>',
    re.IGNORECASE,
)


def _merge_slots_into_template(template_html: str, slots: dict[str, Any]) -> str:
    """Replace empty <div data-type="slot" data-key="..."></div> placeholders
    in the template HTML with the actual slot content from views.html.slots.

    This produces the same compiled HTML that the SFMC UI displays.
    """
    if not slots:
        return template_html

    def _replace_slot(match: re.Match) -> str:
        slot_key = match.group(1)
        content = _get_slot_content(slots, slot_key)
        return content if content else match.group(0)

    return _SLOT_DIV_RE.sub(_replace_slot, template_html)


def _compile_slots_only(html_view: dict[str, Any]) -> str:
    """Last-resort extraction: concatenate all slot block content when
    views.html.content is completely empty."""
    slots = html_view.get("slots", {})
    if not slots:
        return ""

    parts: list[str] = []
    for slot_key in sorted(slots.keys()):
        content = _get_slot_content(slots, slot_key)
        if content:
            parts.append(content)
    return "\n".join(parts)


def _extract_content(item: dict[str, Any]) -> tuple[str, str, str]:
    """Determine filename extension and text content for any asset type.

    Returns (extension, content_string, extraction_source).
    extraction_source describes where the content came from for diagnostics.
    """
    views = item.get("views", {})
    meta_header = _build_metadata_header(item)
    html_view = views.get("html", {})
    html_content = html_view.get("content", "")
    slots = html_view.get("slots", {})

    # --- Source 1: template-based emails - merge slot content into template skeleton ---
    if _is_template_based(item) and html_content and slots:
        compiled = _merge_slots_into_template(html_content, slots)
        if meta_header:
            compiled = f"<!--\n{meta_header}-->\n{compiled}"
        return ".html", compiled, "views.html.content+slots"

    # --- Source 2: views.html.content (HTML paste emails, non-template) ---
    if html_content:
        if meta_header:
            html_content = f"<!--\n{meta_header}-->\n{html_content}"
        return ".html", html_content, "views.html.content"

    # --- Source 3: views.html.slots only (template-based but content is empty) ---
    if _is_template_based(item) and slots:
        slot_html = _compile_slots_only(html_view)
        if slot_html:
            if meta_header:
                slot_html = f"<!--\n{meta_header}-->\n{slot_html}"
            return ".html", slot_html, "views.html.slots"

    # --- Source 4: views.text.content (text-only emails) ---
    text_content = views.get("text", {}).get("content", "")
    if text_content:
        return ".txt", meta_header + text_content, "views.text.content"

    # --- Source 5: top-level content field ---
    raw_content = item.get("content", "")
    if raw_content:
        ext = ".txt" if _is_text_only(item) else ".html"
        prefix = meta_header if ext == ".txt" else (f"<!--\n{meta_header}-->\n" if meta_header else "")
        return ext, prefix + raw_content, "item.content"

    # --- Source 6: design field (some blocks store JSON design data) ---
    design = item.get("design", "")
    if design:
        return ".json", design if isinstance(design, str) else json.dumps(design, indent=2), "item.design"

    # --- Fallback ---
    name = item.get("name", "unnamed")
    asset_type = item.get("assetType", {}).get("displayName", "unknown")
    subject = views.get("subjectline", {}).get("content", "")
    fallback = f"(No extractable content)\nName: {name}\nType: {asset_type}\n"
    if subject:
        fallback += f"Subject: {subject}\n"
    return ".txt", fallback, "fallback"


def write_assets(
    assets: list[dict[str, Any]],
    directory: Path,
    label: str,
) -> tuple[list[dict[str, Any]], dict[str, list]]:
    """Write each asset to disk. Returns (manifest, changes).

    changes = {"added": [...], "modified": [...], "deleted": [...], "unchanged": int}
    """
    directory.mkdir(parents=True, exist_ok=True)

    existing_files: dict[str, str] = {}
    for f in directory.iterdir():
        if f.is_file() and f.name not in META_FILES:
            existing_files[f.name] = f.read_text(encoding="utf-8", errors="replace")

    prev_manifest = _load_previous_manifest(directory)

    written_files: set[str] = set()
    manifest: list[dict[str, Any]] = []
    added: list[dict[str, str]] = []
    modified: list[dict[str, str]] = []
    unchanged_count = 0
    source_counts: dict[str, int] = {}
    fallback_assets: list[str] = []

    for item in assets:
        asset_id = item.get("id", 0)
        name = item.get("name", "unnamed")
        safe_name = sanitize_filename(name)
        meta = _asset_metadata(item)

        ext, content, source = _extract_content(item)
        source_counts[source] = source_counts.get(source, 0) + 1
        if source == "fallback":
            fallback_assets.append(f"{asset_id} ({name})")

        filename = f"{asset_id}_{safe_name}{ext}"

        filepath = directory / filename
        rel_path = f"{label}/{filename}"
        change_entry = {
            "file": rel_path,
            "name": name,
            "modifiedBy": meta["modifiedByName"],
            "modifiedDate": meta["modifiedDate"],
        }

        if filename not in existing_files:
            added.append(change_entry)
        else:
            old_content = existing_files[filename]
            content_changed = old_content != content
            prev_entry = prev_manifest.get(filename, {})
            metadata_changed = prev_entry.get("modifiedDate", "") != meta["modifiedDate"]
            if content_changed or metadata_changed:
                diff_lines = list(difflib.unified_diff(
                    old_content.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{rel_path}",
                    tofile=f"b/{rel_path}",
                ))
                change_entry["diff"] = "".join(diff_lines) if diff_lines else "(metadata changed, content identical)"
                modified.append(change_entry)
            else:
                unchanged_count += 1

        filepath.write_text(content, encoding="utf-8")
        written_files.add(filename)

        views = item.get("views", {})
        template_ref = item.get("template", {})
        manifest_entry: dict[str, Any] = {
            "file": filename,
            "id": asset_id,
            "name": name,
            "customerKey": item.get("customerKey", ""),
            "assetType": item.get("assetType", {}).get("displayName", ""),
            "status": item.get("status", {}).get("name", ""),
            "category": item.get("category", {}).get("name", ""),
            "subject": views.get("subjectline", {}).get("content", ""),
            "preheader": views.get("preheader", {}).get("content", ""),
            "createdDate": item.get("createdDate", ""),
            "modifiedDate": item.get("modifiedDate", ""),
            "modifiedBy": meta["modifiedByName"],
            "contentSource": source,
        }
        if template_ref and template_ref.get("id"):
            manifest_entry["templateId"] = template_ref["id"]
            manifest_entry["templateName"] = template_ref.get("name", "")
        manifest.append(manifest_entry)

    deleted: list[dict[str, str]] = []
    stale = set(existing_files.keys()) - written_files
    for stale_file in sorted(stale):
        old_content = existing_files.get(stale_file, "")
        (directory / stale_file).unlink()
        deleted.append({
            "file": f"{label}/{stale_file}",
            "old_content": old_content,
        })
        print(f"  Removed stale file: {label}/{stale_file}")

    manifest.sort(key=lambda m: m["id"])
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")

    src_summary = ", ".join(f"{k}: {v}" for k, v in sorted(source_counts.items()))
    print(f"  Content sources: {src_summary}")
    if fallback_assets:
        print(f"  WARNING: {len(fallback_assets)} asset(s) had no extractable content:")
        for fa in fallback_assets:
            print(f"    - {fa}")

    changes = {
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "unchanged": unchanged_count,
    }
    return manifest, changes


# ---------------------------------------------------------------------------
# Image / binary asset downloading
# ---------------------------------------------------------------------------

def _guess_image_ext(item: dict[str, Any], resp_headers: dict[str, str] | None = None) -> str:
    """Determine file extension for an image asset from metadata or HTTP headers."""
    file_props = item.get("fileProperties", {})
    published_url = file_props.get("publishedURL", "") or item.get("fileProperties", {}).get("fileURL", "")

    if published_url:
        path = urlparse(published_url).path
        if "." in path:
            ext = os.path.splitext(path)[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".tiff", ".pdf"):
                return ".jpg" if ext == ".jpeg" else ext

    mime = (file_props.get("mimeType", "") or "").lower()
    if not mime and resp_headers:
        mime = (resp_headers.get("content-type", "").split(";")[0]).strip().lower()
    if mime in MIME_TO_EXT:
        return MIME_TO_EXT[mime]

    return ".bin"


def _download_image(url: str, token: str) -> bytes | None:
    """Download binary content from a URL. Returns bytes or None on failure."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=120, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        print(f"  Warning: failed to download {url}: {exc}")
        return None


def write_image_assets(
    assets: list[dict[str, Any]],
    directory: Path,
    label: str,
    token: str,
) -> tuple[list[dict[str, Any]], dict[str, list]]:
    """Download image assets to disk. Returns (manifest, changes).

    Uses file hash comparison for change detection since binary diffs
    are not human-readable.
    """
    directory.mkdir(parents=True, exist_ok=True)

    existing_files: dict[str, bytes] = {}
    for f in directory.iterdir():
        if f.is_file() and f.name not in META_FILES:
            existing_files[f.name] = f.read_bytes()

    prev_manifest = _load_previous_manifest(directory)

    written_files: set[str] = set()
    manifest: list[dict[str, Any]] = []
    added: list[dict[str, str]] = []
    modified: list[dict[str, str]] = []
    unchanged_count = 0
    skipped = 0

    for item in assets:
        asset_id = item.get("id", 0)
        name = item.get("name", "unnamed")
        safe_name = sanitize_filename(name)
        meta = _asset_metadata(item)
        file_props = item.get("fileProperties", {})

        download_url = file_props.get("publishedURL", "") or file_props.get("fileURL", "")
        if not download_url:
            print(f"  Skipping image {asset_id} ({name}): no download URL")
            skipped += 1
            continue

        ext = _guess_image_ext(item)
        filename = f"{asset_id}_{safe_name}{ext}"
        filepath = directory / filename
        rel_path = f"{label}/{filename}"

        image_bytes = _download_image(download_url, token)
        if image_bytes is None:
            skipped += 1
            continue

        new_hash = hashlib.sha256(image_bytes).hexdigest()

        change_entry = {
            "file": rel_path,
            "name": name,
            "modifiedBy": meta["modifiedByName"],
            "modifiedDate": meta["modifiedDate"],
        }

        if filename not in existing_files:
            added.append(change_entry)
        else:
            old_hash = hashlib.sha256(existing_files[filename]).hexdigest()
            prev_entry = prev_manifest.get(filename, {})
            metadata_changed = prev_entry.get("modifiedDate", "") != meta["modifiedDate"]
            if old_hash != new_hash or metadata_changed:
                change_entry["diff"] = f"(binary changed: sha256 {old_hash[:12]}... → {new_hash[:12]}...)"
                modified.append(change_entry)
            else:
                unchanged_count += 1

        filepath.write_bytes(image_bytes)
        written_files.add(filename)

        manifest.append({
            "file": filename,
            "id": asset_id,
            "name": name,
            "customerKey": item.get("customerKey", ""),
            "assetType": item.get("assetType", {}).get("displayName", ""),
            "category": item.get("category", {}).get("name", ""),
            "fileSize": file_props.get("fileSize", ""),
            "mimeType": file_props.get("mimeType", ""),
            "publishedURL": file_props.get("publishedURL", ""),
            "sha256": new_hash,
            "createdDate": item.get("createdDate", ""),
            "modifiedDate": item.get("modifiedDate", ""),
            "modifiedBy": meta["modifiedByName"],
        })

    deleted: list[dict[str, str]] = []
    stale = set(existing_files.keys()) - written_files
    for stale_file in sorted(stale):
        (directory / stale_file).unlink()
        deleted.append({"file": f"{label}/{stale_file}", "old_content": ""})
        print(f"  Removed stale file: {label}/{stale_file}")

    manifest.sort(key=lambda m: m["id"])
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")

    if skipped:
        print(f"  Skipped {skipped} image(s) (no URL or download failed)")

    changes = {
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "unchanged": unchanged_count,
    }
    return manifest, changes


# ---------------------------------------------------------------------------
# Merge change dicts from multiple asset categories
# ---------------------------------------------------------------------------

def _merge_changes(*change_dicts: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {"added": [], "modified": [], "deleted": [], "unchanged": 0}
    for ch in change_dicts:
        merged["added"].extend(ch["added"])
        merged["modified"].extend(ch["modified"])
        merged["deleted"].extend(ch["deleted"])
        merged["unchanged"] += ch["unchanged"]
    return merged


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------

def _format_change_line(entry: dict[str, str]) -> str:
    date_part = entry.get("modifiedDate", "")
    if date_part:
        date_part = date_part.split("T")[0]
    by = entry.get("modifiedBy", "unknown")
    return f"- `{entry['file']}` -- \"{entry['name']}\" (modified in SFMC by {by} on {date_part})"


def append_changelog(changes: dict[str, Any], timestamp: str) -> bool:
    """Append a changelog entry. Returns True if anything changed."""
    added = changes["added"]
    modified = changes["modified"]
    deleted = changes["deleted"]
    unchanged = changes["unchanged"]

    if not added and not modified and not deleted:
        return False

    lines: list[str] = [f"## {timestamp}\n"]

    if added:
        lines.append(f"### Added ({len(added)})")
        for entry in added:
            lines.append(_format_change_line(entry))
        lines.append("")

    if modified:
        lines.append(f"### Modified ({len(modified)})")
        for entry in modified:
            lines.append(_format_change_line(entry))
            diff_text = entry.get("diff", "")
            if diff_text:
                lines.append("")
                lines.append("<details>")
                lines.append(f"<summary>Diff for {entry['file']}</summary>")
                lines.append("")
                lines.append("```diff")
                lines.append(diff_text.rstrip())
                lines.append("```")
                lines.append("")
                lines.append("</details>")
            lines.append("")

    if deleted:
        lines.append(f"### Deleted ({len(deleted)})")
        for entry in deleted:
            lines.append(f"- `{entry['file']}`")
            old_content = entry.get("old_content", "")
            if old_content:
                lines.append("")
                lines.append("<details>")
                lines.append(f"<summary>Last known content of {entry['file']}</summary>")
                lines.append("")
                lines.append("```html")
                lines.append(old_content.rstrip())
                lines.append("```")
                lines.append("")
                lines.append("</details>")
            lines.append("")

    lines.append(f"### Unchanged: {unchanged} asset(s)\n")
    lines.append("---\n")

    new_entry = "\n".join(lines)

    existing = ""
    if CHANGELOG_FILE.exists():
        existing = CHANGELOG_FILE.read_text(encoding="utf-8")

    header = "# SFMC Content Builder Changelog\n\n"
    if existing.startswith("# SFMC"):
        body = existing[existing.index("\n") + 1:].lstrip("\n")
        CHANGELOG_FILE.write_text(header + new_entry + "\n" + body, encoding="utf-8")
    else:
        CHANGELOG_FILE.write_text(header + new_entry + "\n" + existing, encoding="utf-8")

    return True


# ---------------------------------------------------------------------------
# Commit summary
# ---------------------------------------------------------------------------

def write_commit_summary(changes: dict[str, Any], timestamp: str) -> None:
    """Write a file the workflow can use as the git commit message."""
    added = changes["added"]
    modified = changes["modified"]
    deleted = changes["deleted"]
    unchanged = changes["unchanged"]

    subject = f"chore: sync SFMC content {timestamp}"
    stats = f"Added: {len(added)} | Modified: {len(modified)} | Deleted: {len(deleted)} | Unchanged: {unchanged}"

    lines = [subject, "", stats, ""]

    if added:
        lines.append("Added:")
        for e in added:
            lines.append(f"  - {e['file']} ({e['name']})")
    if modified:
        lines.append("Modified:")
        for e in modified:
            lines.append(f"  - {e['file']} ({e['name']})")
    if deleted:
        lines.append("Deleted:")
        for e in deleted:
            lines.append(f"  - {e['file']}")

    lines.append("")
    COMMIT_SUMMARY_FILE.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    client_id = os.environ.get("SFMC_CLIENT_ID", "")
    client_secret = os.environ.get("SFMC_CLIENT_SECRET", "")
    subdomain = os.environ.get("SFMC_SUBDOMAIN", "")

    if not all([client_id, client_secret, subdomain]):
        print("ERROR: Set SFMC_CLIENT_ID, SFMC_CLIENT_SECRET, and SFMC_SUBDOMAIN environment variables.")
        return 1

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("Authenticating with SFMC...")
    try:
        token, rest_url = authenticate(client_id, client_secret, subdomain)
    except httpx.HTTPStatusError as exc:
        print(f"ERROR: Authentication failed: {exc}")
        return 1
    print(f"Authenticated. REST endpoint: {rest_url}")

    # --- Fetch emails ---
    print("Fetching emails...")
    try:
        emails = fetch_assets(token, rest_url, EMAIL_TYPE_IDS, "emails")
    except httpx.HTTPStatusError as exc:
        print(f"ERROR: Failed to fetch emails: {exc}")
        return 1
    print(f"Retrieved {len(emails)} email(s).")

    if emails:
        print(f"Re-fetching {len(emails)} email(s) individually for full content...")
        emails = enrich_assets(emails, token, rest_url, "emails")

    # --- Fetch templates ---
    # Uses both ID-based and name-based matching so every flavor of template
    # is captured: legacy (id=4), Content Builder (id=210), and any future
    # template subtypes SFMC introduces whose assetType.name is still "template".
    print("Fetching templates...")
    try:
        templates = fetch_assets(
            token, rest_url, TEMPLATE_TYPE_IDS, "templates",
            type_names=TEMPLATE_TYPE_NAMES,
        )
    except httpx.HTTPStatusError as exc:
        print(f"ERROR: Failed to fetch templates: {exc}")
        return 1

    # De-duplicate in case both clauses matched the same asset.
    seen_ids: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for t in templates:
        tid = t.get("id", 0)
        if tid and tid not in seen_ids:
            seen_ids.add(tid)
            deduped.append(t)
    templates = deduped
    print(f"Retrieved {len(templates)} template(s).")

    if templates:
        print(f"Re-fetching {len(templates)} template(s) individually for full content...")
        templates = enrich_assets(templates, token, rest_url, "templates")

    # --- Fetch content blocks ---
    print("Fetching content blocks...")
    try:
        blocks = fetch_assets(token, rest_url, CONTENT_BLOCK_TYPE_IDS, "content-blocks")
    except httpx.HTTPStatusError as exc:
        print(f"ERROR: Failed to fetch content blocks: {exc}")
        return 1
    print(f"Retrieved {len(blocks)} content block(s).")

    if blocks:
        print(f"Re-fetching {len(blocks)} content block(s) individually for full content...")
        blocks = enrich_assets(blocks, token, rest_url, "content-blocks")

    # --- Fetch images ---
    print("Fetching images...")
    try:
        images = fetch_assets(token, rest_url, IMAGE_TYPE_IDS, "images")
    except httpx.HTTPStatusError as exc:
        print(f"ERROR: Failed to fetch images: {exc}")
        return 1
    print(f"Retrieved {len(images)} image(s).")

    # --- Write emails ---
    print(f"Writing emails to {EMAILS_DIR}/...")
    email_manifest, email_changes = write_assets(emails, EMAILS_DIR, "emails")
    _print_changes("Emails", email_changes)

    # --- Write templates ---
    print(f"Writing templates to {TEMPLATES_DIR}/...")
    template_manifest, template_changes = write_assets(templates, TEMPLATES_DIR, "templates")
    _print_changes("Templates", template_changes)

    # --- Write content blocks ---
    print(f"Writing content blocks to {CONTENT_BLOCKS_DIR}/...")
    block_manifest, block_changes = write_assets(blocks, CONTENT_BLOCKS_DIR, "content-blocks")
    _print_changes("Content blocks", block_changes)

    # --- Write images ---
    print(f"Downloading images to {IMAGES_DIR}/...")
    image_manifest, image_changes = write_image_assets(images, IMAGES_DIR, "images", token)
    _print_changes("Images", image_changes)

    # --- Changelog & commit summary ---
    all_changes = _merge_changes(email_changes, template_changes, block_changes, image_changes)

    has_changes = (
        all_changes["added"] or all_changes["modified"] or all_changes["deleted"]
    )
    if has_changes:
        append_changelog(all_changes, timestamp)
        print("Updated CHANGELOG.md")

    write_commit_summary(all_changes, timestamp)
    total = len(email_manifest) + len(template_manifest) + len(block_manifest) + len(image_manifest)
    print(
        f"Done. {total} asset(s) synced "
        f"({len(email_manifest)} emails, {len(template_manifest)} templates, "
        f"{len(block_manifest)} content blocks, {len(image_manifest)} images)."
    )

    return 0


def _print_changes(label: str, changes: dict[str, Any]) -> None:
    a = len(changes["added"])
    m = len(changes["modified"])
    d = len(changes["deleted"])
    u = changes["unchanged"]
    print(f"  {label}: {a} added, {m} modified, {d} deleted, {u} unchanged")


if __name__ == "__main__":
    sys.exit(main())
