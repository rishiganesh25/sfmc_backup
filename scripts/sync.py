#!/usr/bin/env python3
"""SFMCVault -- Fetch all SFMC assets and write them as individual files
for Git-based version tracking.

Credentials are read from environment variables:
    SFMC_CLIENT_ID
    SFMC_CLIENT_SECRET
    SFMC_SUBDOMAIN

Required SFMC Installed-Package permissions:
    Assets (Content Builder)  — Read
    Automations               — Read
    Journeys                  — Read
    Data Extensions           — Read  (SOAP API)
    List and Subscribers      — Read  (for DE field retrieval)

Output:
    email-content/emails/{id}_{name}.html
    email-content/emails/manifest.json
    email-content/templates/{id}_{name}.html
    email-content/templates/manifest.json
    email-content/content-blocks/{id}_{name}.*
    email-content/content-blocks/manifest.json
    email-content/images/{id}_{name}.{ext}
    email-content/images/manifest.json
    email-content/data-extensions/{key}_{name}.json
    email-content/data-extensions/manifest.json
    email-content/automations/{id}_{name}.json
    email-content/automations/manifest.json
    email-content/automation-activities/{type}/{id}_{name}.*
    email-content/automation-activities/manifest.json
    email-content/journeys/{id}_{name}.json
    email-content/journeys/manifest.json
    email-content/CHANGELOG.md
    email-content/.commit-summary
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "email-content"

EMAILS_DIR = OUTPUT_DIR / "emails"
TEMPLATES_DIR = OUTPUT_DIR / "templates"
CONTENT_BLOCKS_DIR = OUTPUT_DIR / "content-blocks"
IMAGES_DIR = OUTPUT_DIR / "images"
DATA_EXT_DIR = OUTPUT_DIR / "data-extensions"
AUTOMATIONS_DIR = OUTPUT_DIR / "automations"
ACTIVITIES_DIR = OUTPUT_DIR / "automation-activities"
JOURNEYS_DIR = OUTPUT_DIR / "journeys"

CHANGELOG_FILE = OUTPUT_DIR / "CHANGELOG.md"
COMMIT_SUMMARY_FILE = OUTPUT_DIR / ".commit-summary"
META_FILES = {"manifest.json"}

# Content Builder asset-type IDs
EMAIL_TYPE_IDS = [207, 208, 209]
TEMPLATE_TYPE_IDS = [210]
CONTENT_BLOCK_TYPE_IDS = [
    195, 196, 197, 198, 199, 202, 203, 205, 206,
    214, 220, 227, 230, 231, 232, 233, 236,
]
IMAGE_TYPE_IDS = [28, 29, 30, 31]

CB_PAGE_SIZE = 2500
REST_PAGE_SIZE = 50
TEXT_ONLY_TYPE_ID = 209
TEMPLATE_BASED_TYPE_ID = 207

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

# SOAP namespace
NS_PARTNER = "http://exacttarget.com/wsdl/partnerAPI"

DE_PROPERTIES = [
    "ObjectID", "CustomerKey", "Name", "Description", "CategoryID",
    "CreatedDate", "ModifiedDate", "IsSendable", "IsTestable",
]
DE_FIELD_PROPERTIES = [
    "ObjectID", "Name", "FieldType", "MaxLength", "IsRequired",
    "IsPrimaryKey", "DefaultValue", "Ordinal",
]

# Automation-activity REST endpoints.
# (endpoint_path, id_field, code_field | None, code_extension | None)
ACTIVITY_TYPES: dict[str, tuple[str, str, str | None, str | None]] = {
    "queries": ("/automation/v1/queries/", "queryDefinitionId", "queryText", ".sql"),
    "scripts": ("/automation/v1/scripts/", "ssjsActivityId", "script", ".ssjs"),
    "imports": ("/automation/v1/imports/", "importDefinitionId", None, None),
    "data-extracts": ("/automation/v1/dataextracts/", "dataExtractDefinitionId", None, None),
    "file-transfers": ("/automation/v1/filetransfers/", "id", None, None),
}

AUTOMATION_STATUS_MAP = {
    1: "Building", 2: "Ready", 3: "Running", 4: "Paused",
    5: "Stopped", 6: "Scheduled", 7: "Awaiting", 8: "InactiveTrigger",
}

JOURNEY_STATUS_MAP = {
    "Draft": "Draft", "Published": "Published",
    "ActivatedStarted": "Running", "Deactivated": "Stopped",
    "Deleted": "Deleted",
}


# ===================================================================
#  SHARED UTILITIES
# ===================================================================

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


def sanitize_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^\w\s\-]", "", name)
    name = re.sub(r"\s+", "_", name)
    return name or "unnamed"


def _normalize_json(obj: Any) -> str:
    """Deterministic JSON string for diffing."""
    return json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n"


def _asset_metadata(item: dict[str, Any]) -> dict[str, str]:
    modified_by = item.get("modifiedBy", {})
    return {
        "name": item.get("name", "unnamed"),
        "modifiedByName": modified_by.get("name", "unknown") if isinstance(modified_by, dict) else str(modified_by),
        "modifiedDate": item.get("modifiedDate", ""),
    }


# ---------------------------------------------------------------------------
# Generic change-detection helpers (used by JSON-based categories)
# ---------------------------------------------------------------------------

def _load_existing_text(directory: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not directory.exists():
        return result
    for f in directory.iterdir():
        if f.is_file() and f.name not in META_FILES:
            result[f.name] = f.read_text(encoding="utf-8", errors="replace")
    return result


def _load_previous_manifest(directory: Path) -> dict[str, dict[str, Any]]:
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {e["file"]: e for e in entries}
    except (json.JSONDecodeError, KeyError):
        return {}


def _change_entry(
    rel_path: str, name: str, mod_by: str, mod_date: str,
) -> dict[str, str]:
    return {"file": rel_path, "name": name, "modifiedBy": mod_by, "modifiedDate": mod_date}


def _detect_change(
    filename: str,
    rel_path: str,
    name: str,
    new_content: str,
    mod_date: str,
    mod_by: str,
    existing_files: dict[str, str],
    prev_manifest: dict[str, dict[str, Any]],
) -> str | dict[str, str]:
    """Return "added", "unchanged", or a modified-change dict with diff."""
    if filename not in existing_files:
        return "added"

    old_content = existing_files[filename]
    prev = prev_manifest.get(filename, {})
    content_changed = old_content != new_content
    meta_changed = prev.get("modifiedDate", "") != mod_date

    if not content_changed and not meta_changed:
        return "unchanged"

    diff_lines = list(difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
    ))
    entry = _change_entry(rel_path, name, mod_by, mod_date)
    entry["diff"] = "".join(diff_lines) if diff_lines else "(metadata changed, content identical)"
    return entry


def _remove_stale(
    directory: Path,
    existing_files: dict[str, str],
    written: set[str],
    label: str,
) -> list[dict[str, str]]:
    deleted: list[dict[str, str]] = []
    for stale in sorted(set(existing_files.keys()) - written):
        old_content = existing_files.get(stale, "")
        (directory / stale).unlink(missing_ok=True)
        deleted.append({"file": f"{label}/{stale}", "old_content": old_content})
        print(f"  Removed stale file: {label}/{stale}")
    return deleted


def _write_manifest(
    directory: Path,
    manifest: list[dict[str, Any]],
    key_fn: Any = None,
) -> None:
    if key_fn:
        manifest.sort(key=key_fn)
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")


def _changes_dict(added: list, modified: list, deleted: list, unchanged: int) -> dict[str, Any]:
    return {"added": added, "modified": modified, "deleted": deleted, "unchanged": unchanged}


def _merge_changes(*change_dicts: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {"added": [], "modified": [], "deleted": [], "unchanged": 0}
    for ch in change_dicts:
        merged["added"].extend(ch["added"])
        merged["modified"].extend(ch["modified"])
        merged["deleted"].extend(ch["deleted"])
        merged["unchanged"] += ch["unchanged"]
    return merged


def _print_changes(label: str, changes: dict[str, Any]) -> None:
    a, m, d, u = len(changes["added"]), len(changes["modified"]), len(changes["deleted"]), changes["unchanged"]
    print(f"  {label}: {a} added, {m} modified, {d} deleted, {u} unchanged")


# ===================================================================
#  CONTENT BUILDER API (emails, templates, content blocks)
# ===================================================================

def fetch_cb_assets(
    token: str, rest_url: str, type_ids: list[int], label: str,
) -> list[dict[str, Any]]:
    """Paginate through Content Builder Asset query."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    all_items: list[dict[str, Any]] = []
    page = 1

    while True:
        body: dict[str, Any] = {
            "page": {"page": page, "pageSize": CB_PAGE_SIZE},
            "query": {"property": "assetType.id", "simpleOperator": "in", "value": type_ids},
        }
        resp = httpx.post(
            f"{rest_url}/asset/v1/content/assets/query",
            headers=headers, json=body, timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        all_items.extend(items)
        count = data.get("count", 0)
        print(f"  [{label}] Page {page}: {len(items)} items (total {len(all_items)}/{count})")
        if not items or page * CB_PAGE_SIZE >= count:
            break
        page += 1

    return all_items


def fetch_cb_asset_detail(token: str, rest_url: str, asset_id: int) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = httpx.get(f"{rest_url}/asset/v1/content/assets/{asset_id}", headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()


def enrich_cb_assets(
    assets: list[dict[str, Any]], token: str, rest_url: str, label: str,
) -> list[dict[str, Any]]:
    """Re-fetch every asset individually for the full content payload."""
    enriched: list[dict[str, Any]] = []
    total = len(assets)
    for idx, item in enumerate(assets, 1):
        asset_id = item.get("id", 0)
        name = item.get("name", "unnamed")
        try:
            enriched.append(fetch_cb_asset_detail(token, rest_url, asset_id))
        except httpx.HTTPStatusError:
            print(f"  Warning: could not fetch detail for {label} asset {asset_id} ({name}), using bulk data")
            enriched.append(item)
        if idx % 25 == 0 or idx == total:
            print(f"  [{label}] Enriched {idx}/{total}")
    return enriched


# ---------------------------------------------------------------------------
# Content extraction (template merging, slot handling)
# ---------------------------------------------------------------------------

def _is_text_only(item: dict[str, Any]) -> bool:
    return item.get("assetType", {}).get("id", 0) == TEXT_ONLY_TYPE_ID


def _is_template_based(item: dict[str, Any]) -> bool:
    return item.get("assetType", {}).get("id", 0) == TEMPLATE_BASED_TYPE_ID


def _build_metadata_header(item: dict[str, Any]) -> str:
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
    slot = slots.get(slot_key, {})
    blocks = slot.get("blocks", {})
    parts: list[str] = []
    for block_key in sorted(blocks.keys()):
        block_content = blocks[block_key].get("content", "")
        if block_content:
            parts.append(block_content)
    return "".join(parts)


_SLOT_DIV_RE = re.compile(
    r'<div\s+data-type="slot"\s+data-key="([^"]+)"[^>]*>[\s]*</div>',
    re.IGNORECASE,
)


def _merge_slots_into_template(template_html: str, slots: dict[str, Any]) -> str:
    if not slots:
        return template_html

    def _replace_slot(match: re.Match) -> str:
        content = _get_slot_content(slots, match.group(1))
        return content if content else match.group(0)

    return _SLOT_DIV_RE.sub(_replace_slot, template_html)


def _compile_slots_only(html_view: dict[str, Any]) -> str:
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
    """Return (extension, content_string, extraction_source)."""
    views = item.get("views", {})
    meta_header = _build_metadata_header(item)
    html_view = views.get("html", {})
    html_content = html_view.get("content", "")
    slots = html_view.get("slots", {})

    if _is_template_based(item) and html_content and slots:
        compiled = _merge_slots_into_template(html_content, slots)
        if meta_header:
            compiled = f"<!--\n{meta_header}-->\n{compiled}"
        return ".html", compiled, "views.html.content+slots"

    if html_content:
        if meta_header:
            html_content = f"<!--\n{meta_header}-->\n{html_content}"
        return ".html", html_content, "views.html.content"

    if _is_template_based(item) and slots:
        slot_html = _compile_slots_only(html_view)
        if slot_html:
            if meta_header:
                slot_html = f"<!--\n{meta_header}-->\n{slot_html}"
            return ".html", slot_html, "views.html.slots"

    text_content = views.get("text", {}).get("content", "")
    if text_content:
        return ".txt", meta_header + text_content, "views.text.content"

    raw_content = item.get("content", "")
    if raw_content:
        ext = ".txt" if _is_text_only(item) else ".html"
        prefix = meta_header if ext == ".txt" else (f"<!--\n{meta_header}-->\n" if meta_header else "")
        return ext, prefix + raw_content, "item.content"

    design = item.get("design", "")
    if design:
        return ".json", design if isinstance(design, str) else json.dumps(design, indent=2), "item.design"

    name = item.get("name", "unnamed")
    asset_type = item.get("assetType", {}).get("displayName", "unknown")
    subject = views.get("subjectline", {}).get("content", "")
    fallback = f"(No extractable content)\nName: {name}\nType: {asset_type}\n"
    if subject:
        fallback += f"Subject: {subject}\n"
    return ".txt", fallback, "fallback"


# ---------------------------------------------------------------------------
# Content Builder asset writer
# ---------------------------------------------------------------------------

def write_cb_assets(
    assets: list[dict[str, Any]], directory: Path, label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Write Content Builder text assets to disk. Returns (manifest, changes)."""
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
        rel_path = f"{label}/{filename}"
        change_entry = {
            "file": rel_path, "name": name,
            "modifiedBy": meta["modifiedByName"], "modifiedDate": meta["modifiedDate"],
        }

        if filename not in existing_files:
            added.append(change_entry)
        else:
            old_content = existing_files[filename]
            prev_entry = prev_manifest.get(filename, {})
            if old_content != content or prev_entry.get("modifiedDate", "") != meta["modifiedDate"]:
                diff_lines = list(difflib.unified_diff(
                    old_content.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}",
                ))
                change_entry["diff"] = "".join(diff_lines) if diff_lines else "(metadata changed, content identical)"
                modified.append(change_entry)
            else:
                unchanged_count += 1

        (directory / filename).write_text(content, encoding="utf-8")
        written_files.add(filename)

        views = item.get("views", {})
        template_ref = item.get("template", {})
        manifest_entry: dict[str, Any] = {
            "file": filename, "id": asset_id, "name": name,
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
    for stale_file in sorted(set(existing_files.keys()) - written_files):
        old_content = existing_files.get(stale_file, "")
        (directory / stale_file).unlink()
        deleted.append({"file": f"{label}/{stale_file}", "old_content": old_content})
        print(f"  Removed stale file: {label}/{stale_file}")

    manifest.sort(key=lambda m: m["id"])
    _write_manifest(directory, manifest)

    src_summary = ", ".join(f"{k}: {v}" for k, v in sorted(source_counts.items()))
    print(f"  Content sources: {src_summary}")
    if fallback_assets:
        print(f"  WARNING: {len(fallback_assets)} asset(s) had no extractable content:")
        for fa in fallback_assets:
            print(f"    - {fa}")

    return manifest, _changes_dict(added, modified, deleted, unchanged_count)


# ---------------------------------------------------------------------------
# Image asset downloading & writing
# ---------------------------------------------------------------------------

def _guess_image_ext(item: dict[str, Any]) -> str:
    file_props = item.get("fileProperties", {})
    published_url = file_props.get("publishedURL", "") or file_props.get("fileURL", "")

    if published_url:
        path = urlparse(published_url).path
        if "." in path:
            ext = os.path.splitext(path)[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".tiff", ".pdf"):
                return ".jpg" if ext == ".jpeg" else ext

    mime = (file_props.get("mimeType", "") or "").lower()
    if mime in MIME_TO_EXT:
        return MIME_TO_EXT[mime]
    return ".bin"


def _download_image(url: str, token: str) -> bytes | None:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=120, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        print(f"  Warning: failed to download {url}: {exc}")
        return None


def write_image_assets(
    assets: list[dict[str, Any]], directory: Path, label: str, token: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Download image assets to disk with SHA-256 change detection."""
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
        rel_path = f"{label}/{filename}"

        image_bytes = _download_image(download_url, token)
        if image_bytes is None:
            skipped += 1
            continue

        new_hash = hashlib.sha256(image_bytes).hexdigest()
        change_entry = {
            "file": rel_path, "name": name,
            "modifiedBy": meta["modifiedByName"], "modifiedDate": meta["modifiedDate"],
        }

        if filename not in existing_files:
            added.append(change_entry)
        else:
            old_hash = hashlib.sha256(existing_files[filename]).hexdigest()
            prev_entry = prev_manifest.get(filename, {})
            if old_hash != new_hash or prev_entry.get("modifiedDate", "") != meta["modifiedDate"]:
                change_entry["diff"] = f"(binary changed: sha256 {old_hash[:12]}... → {new_hash[:12]}...)"
                modified.append(change_entry)
            else:
                unchanged_count += 1

        (directory / filename).write_bytes(image_bytes)
        written_files.add(filename)

        manifest.append({
            "file": filename, "id": asset_id, "name": name,
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
    for stale_file in sorted(set(existing_files.keys()) - written_files):
        (directory / stale_file).unlink()
        deleted.append({"file": f"{label}/{stale_file}", "old_content": ""})
        print(f"  Removed stale file: {label}/{stale_file}")

    manifest.sort(key=lambda m: m["id"])
    _write_manifest(directory, manifest)

    if skipped:
        print(f"  Skipped {skipped} image(s) (no URL or download failed)")

    return manifest, _changes_dict(added, modified, deleted, unchanged_count)


# ===================================================================
#  SOAP API (Data Extensions)
# ===================================================================

def _soap_envelope(token: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<s:Header>"
        f'<fueloauth xmlns="http://exacttarget.com">{token}</fueloauth>'
        "</s:Header>"
        f"<s:Body>{body}</s:Body>"
        "</s:Envelope>"
    )


def _retrieve_body(object_type: str, properties: list[str], filter_xml: str = "") -> str:
    props = "".join(f"<Properties>{p}</Properties>" for p in properties)
    return (
        f'<RetrieveRequestMsg xmlns="{NS_PARTNER}">'
        "<RetrieveRequest>"
        f"<ObjectType>{object_type}</ObjectType>"
        f"{props}{filter_xml}"
        "</RetrieveRequest>"
        "</RetrieveRequestMsg>"
    )


def _continue_body(request_id: str) -> str:
    return (
        f'<RetrieveRequestMsg xmlns="{NS_PARTNER}">'
        "<RetrieveRequest>"
        f"<ContinueRequest>{request_id}</ContinueRequest>"
        "</RetrieveRequest>"
        "</RetrieveRequestMsg>"
    )


def _simple_filter_xml(prop: str, operator: str, value: str) -> str:
    return (
        '<Filter xsi:type="SimpleFilterPart">'
        f"<Property>{prop}</Property>"
        f"<SimpleOperator>{operator}</SimpleOperator>"
        f"<Value>{value}</Value>"
        "</Filter>"
    )


def _elem_to_dict(elem: ET.Element) -> dict[str, Any]:
    """Recursively convert an XML element to a dict, stripping namespaces."""
    result: dict[str, Any] = {}
    for child in elem:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if len(child) > 0:
            value: Any = _elem_to_dict(child)
        else:
            value = (child.text or "").strip()
        if tag in result:
            existing = result[tag]
            if not isinstance(existing, list):
                result[tag] = [existing]
            result[tag].append(value)
        else:
            result[tag] = value
    return result


def soap_retrieve(
    token: str, subdomain: str,
    object_type: str, properties: list[str],
    filter_xml: str = "", label: str = "",
) -> list[dict[str, Any]]:
    """Execute a SOAP Retrieve with automatic continuation (pagination)."""
    soap_url = f"https://{subdomain}.soap.marketingcloudapis.com/Service.asmx"
    headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "Retrieve"}

    body = _retrieve_body(object_type, properties, filter_xml)
    envelope = _soap_envelope(token, body)
    all_items: list[dict[str, Any]] = []
    page = 1

    while True:
        resp = httpx.post(soap_url, content=envelope, headers=headers, timeout=120)
        resp.raise_for_status()

        root = ET.fromstring(resp.text)
        status_elem = root.find(f".//{{{NS_PARTNER}}}OverallStatus")
        rid_elem = root.find(f".//{{{NS_PARTNER}}}RequestID")
        overall_status = status_elem.text if status_elem is not None else ""
        request_id = rid_elem.text if rid_elem is not None else ""

        if overall_status and overall_status.startswith("Error"):
            print(f"  SOAP error: {overall_status}")
            break

        results = root.findall(f".//{{{NS_PARTNER}}}Results")
        for r in results:
            all_items.append(_elem_to_dict(r))

        tag = label or object_type
        print(f"  [{tag}] Page {page}: {len(results)} items (total {len(all_items)})")

        if overall_status != "MoreDataAvailable" or not request_id:
            break

        envelope = _soap_envelope(token, _continue_body(request_id))
        page += 1

    return all_items


# ===================================================================
#  REST API PAGINATOR (automations, activities, journeys)
# ===================================================================

def rest_paginate(
    token: str, rest_url: str, endpoint: str, label: str,
    page_size: int = REST_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Generic paginator for SFMC REST endpoints using $page / $pageSize."""
    headers = {"Authorization": f"Bearer {token}"}
    all_items: list[dict[str, Any]] = []
    page = 1

    while True:
        resp = httpx.get(
            f"{rest_url}{endpoint}", headers=headers,
            params={"$page": page, "$pageSize": page_size}, timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        all_items.extend(items)
        count = data.get("count", len(all_items))
        print(f"  [{label}] Page {page}: {len(items)} items (total {len(all_items)}/{count})")
        if not items or page * page_size >= count:
            break
        page += 1

    return all_items


# ===================================================================
#  DATA EXTENSIONS
# ===================================================================

def fetch_data_extensions(token: str, subdomain: str) -> list[dict[str, Any]]:
    return soap_retrieve(token, subdomain, "DataExtension", DE_PROPERTIES, label="data-extensions")


def fetch_de_fields(token: str, subdomain: str, customer_key: str) -> list[dict[str, Any]]:
    filt = _simple_filter_xml("DataExtension.CustomerKey", "equals", customer_key)
    return soap_retrieve(
        token, subdomain, "DataExtensionField", DE_FIELD_PROPERTIES,
        filter_xml=filt, label=f"fields({customer_key[:30]})",
    )


def enrich_data_extensions(
    data_extensions: list[dict[str, Any]], token: str, subdomain: str,
) -> list[dict[str, Any]]:
    total = len(data_extensions)
    for idx, de in enumerate(data_extensions, 1):
        key = de.get("CustomerKey", "")
        if not key:
            continue
        fields = fetch_de_fields(token, subdomain, key)
        de["Fields"] = sorted(fields, key=lambda f: int(f.get("Ordinal", 0) or 0))
        if idx % 25 == 0 or idx == total:
            print(f"  [data-extensions] Enriched {idx}/{total}")
    return data_extensions


def write_data_extensions(
    data_extensions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    directory = DATA_EXT_DIR
    directory.mkdir(parents=True, exist_ok=True)

    existing_files = _load_existing_text(directory)
    prev_manifest = _load_previous_manifest(directory)
    written: set[str] = set()
    manifest: list[dict[str, Any]] = []
    added: list[dict[str, str]] = []
    modified: list[dict[str, str]] = []
    unchanged = 0

    for de in data_extensions:
        key = de.get("CustomerKey", "unknown")
        name = de.get("Name", "unnamed")
        safe = sanitize_filename(name)
        filename = f"{sanitize_filename(key)}_{safe}.json"
        rel_path = f"data-extensions/{filename}"

        fields = de.get("Fields", [])
        schema: dict[str, Any] = {
            "customerKey": key, "name": name,
            "description": de.get("Description", ""),
            "isSendable": de.get("IsSendable", "false"),
            "isTestable": de.get("IsTestable", "false"),
            "categoryId": de.get("CategoryID", ""),
            "createdDate": de.get("CreatedDate", ""),
            "modifiedDate": de.get("ModifiedDate", ""),
            "fields": [
                {
                    "name": f.get("Name", ""), "fieldType": f.get("FieldType", ""),
                    "maxLength": f.get("MaxLength", ""),
                    "isRequired": f.get("IsRequired", "false"),
                    "isPrimaryKey": f.get("IsPrimaryKey", "false"),
                    "defaultValue": f.get("DefaultValue", ""),
                    "ordinal": f.get("Ordinal", ""),
                }
                for f in fields
            ],
        }

        content = _normalize_json(schema)
        change = _detect_change(
            filename, rel_path, name, content,
            de.get("ModifiedDate", ""), "", existing_files, prev_manifest,
        )
        if change == "added":
            added.append(_change_entry(rel_path, name, "", de.get("ModifiedDate", "")))
        elif isinstance(change, dict):
            modified.append(change)
        else:
            unchanged += 1

        (directory / filename).write_text(content, encoding="utf-8")
        written.add(filename)

        manifest.append({
            "file": filename, "customerKey": key, "name": name,
            "description": de.get("Description", ""),
            "isSendable": de.get("IsSendable", "false"),
            "fieldCount": len(fields),
            "createdDate": de.get("CreatedDate", ""),
            "modifiedDate": de.get("ModifiedDate", ""),
        })

    deleted = _remove_stale(directory, existing_files, written, "data-extensions")
    _write_manifest(directory, manifest, key_fn=lambda m: m.get("name", ""))

    return manifest, _changes_dict(added, modified, deleted, unchanged)


# ===================================================================
#  AUTOMATIONS
# ===================================================================

def fetch_automations(token: str, rest_url: str) -> list[dict[str, Any]]:
    return rest_paginate(token, rest_url, "/automation/v1/automations/", "automations")


def write_automations(
    automations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    directory = AUTOMATIONS_DIR
    directory.mkdir(parents=True, exist_ok=True)

    existing_files = _load_existing_text(directory)
    prev_manifest = _load_previous_manifest(directory)
    written: set[str] = set()
    manifest: list[dict[str, Any]] = []
    added: list[dict[str, str]] = []
    modified: list[dict[str, str]] = []
    unchanged = 0

    for item in automations:
        auto_id = item.get("id", "unknown")
        name = item.get("name", "unnamed")
        safe = sanitize_filename(name)
        filename = f"{auto_id}_{safe}.json"
        rel_path = f"automations/{filename}"

        status_code = item.get("status", 0)
        mod_date = item.get("lastRunTime", "") or item.get("modifiedDate", "")
        mod_by = item.get("lastModifiedBy", {}).get("name", "")

        record: dict[str, Any] = {
            "id": auto_id, "name": name,
            "description": item.get("description", ""),
            "status": AUTOMATION_STATUS_MAP.get(status_code, str(status_code)),
            "statusCode": status_code,
            "schedule": item.get("schedule", {}),
            "createdDate": item.get("createdDate", ""),
            "modifiedDate": item.get("modifiedDate", ""),
            "lastRunTime": item.get("lastRunTime", ""),
            "lastRunInstanceId": item.get("lastRunInstanceId", ""),
            "steps": item.get("steps", []),
        }

        content = _normalize_json(record)
        change = _detect_change(
            filename, rel_path, name, content,
            mod_date, mod_by, existing_files, prev_manifest,
        )
        if change == "added":
            added.append(_change_entry(rel_path, name, mod_by, mod_date))
        elif isinstance(change, dict):
            modified.append(change)
        else:
            unchanged += 1

        (directory / filename).write_text(content, encoding="utf-8")
        written.add(filename)

        manifest.append({
            "file": filename, "id": auto_id, "name": name,
            "status": AUTOMATION_STATUS_MAP.get(status_code, str(status_code)),
            "stepCount": len(item.get("steps", [])),
            "createdDate": item.get("createdDate", ""),
            "modifiedDate": item.get("modifiedDate", ""),
            "lastRunTime": item.get("lastRunTime", ""),
        })

    deleted = _remove_stale(directory, existing_files, written, "automations")
    _write_manifest(directory, manifest, key_fn=lambda m: m.get("name", ""))

    return manifest, _changes_dict(added, modified, deleted, unchanged)


# ===================================================================
#  AUTOMATION ACTIVITIES
# ===================================================================

def fetch_all_activities(
    token: str, rest_url: str,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for type_key, (endpoint, *_rest) in ACTIVITY_TYPES.items():
        print(f"Fetching automation activities ({type_key})...")
        try:
            items = rest_paginate(token, rest_url, endpoint, type_key)
            result[type_key] = items
            print(f"Retrieved {len(items)} {type_key} activity(ies).")
        except httpx.HTTPStatusError as exc:
            print(f"  Warning: failed to fetch {type_key}: {exc}")
            result[type_key] = []
    return result


def write_all_activities(
    activities_by_type: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ACTIVITIES_DIR.mkdir(parents=True, exist_ok=True)
    combined_manifest: list[dict[str, Any]] = []
    all_added: list[dict[str, str]] = []
    all_modified: list[dict[str, str]] = []
    all_deleted: list[dict[str, str]] = []
    total_unchanged = 0

    for type_key, items in activities_by_type.items():
        _endpoint, id_field, code_field, code_ext = ACTIVITY_TYPES[type_key]
        sub_dir = ACTIVITIES_DIR / type_key
        sub_dir.mkdir(parents=True, exist_ok=True)

        existing_files = _load_existing_text(sub_dir)
        prev_manifest = _load_previous_manifest(sub_dir)
        written: set[str] = set()
        manifest: list[dict[str, Any]] = []
        added: list[dict[str, str]] = []
        modified: list[dict[str, str]] = []
        unchanged = 0

        for item in items:
            act_id = item.get(id_field, item.get("id", "unknown"))
            name = item.get("name", "unnamed")
            safe = sanitize_filename(name)
            json_filename = f"{act_id}_{safe}.json"
            json_rel = f"automation-activities/{type_key}/{json_filename}"
            mod_date = item.get("modifiedDate", "")

            record = dict(item)
            if code_field and code_field in record:
                record.pop(code_field, None)

            content = _normalize_json(record)
            change = _detect_change(
                json_filename, json_rel, name, content,
                mod_date, "", existing_files, prev_manifest,
            )
            if change == "added":
                added.append(_change_entry(json_rel, name, "", mod_date))
            elif isinstance(change, dict):
                modified.append(change)
            else:
                unchanged += 1

            (sub_dir / json_filename).write_text(content, encoding="utf-8")
            written.add(json_filename)

            if code_field and item.get(code_field):
                code_filename = f"{act_id}_{safe}{code_ext}"
                code_content = item[code_field]
                code_rel = f"automation-activities/{type_key}/{code_filename}"

                code_change = _detect_change(
                    code_filename, code_rel, f"{name} ({code_ext})", code_content,
                    mod_date, "", existing_files, prev_manifest,
                )
                if code_change == "added":
                    added.append(_change_entry(code_rel, f"{name} ({code_ext})", "", mod_date))
                elif isinstance(code_change, dict):
                    modified.append(code_change)
                else:
                    unchanged += 1

                (sub_dir / code_filename).write_text(code_content, encoding="utf-8")
                written.add(code_filename)

            manifest.append({
                "file": json_filename, "activityType": type_key,
                "id": act_id, "name": name,
                "hasCode": bool(code_field and item.get(code_field)),
                "codeFile": f"{act_id}_{safe}{code_ext}" if code_field and item.get(code_field) else None,
                "createdDate": item.get("createdDate", ""),
                "modifiedDate": mod_date,
            })

        deleted = _remove_stale(sub_dir, existing_files, written, f"automation-activities/{type_key}")
        _write_manifest(sub_dir, manifest, key_fn=lambda m: m.get("name", ""))

        combined_manifest.extend(manifest)
        all_added.extend(added)
        all_modified.extend(modified)
        all_deleted.extend(deleted)
        total_unchanged += unchanged

        _print_changes(f"Activities/{type_key}", _changes_dict(added, modified, deleted, unchanged))

    _write_manifest(
        ACTIVITIES_DIR, combined_manifest,
        key_fn=lambda m: (m.get("activityType", ""), m.get("name", "")),
    )
    return combined_manifest, _changes_dict(all_added, all_modified, all_deleted, total_unchanged)


# ===================================================================
#  JOURNEY BUILDER
# ===================================================================

def fetch_journeys(token: str, rest_url: str) -> list[dict[str, Any]]:
    return rest_paginate(token, rest_url, "/interaction/v1/interactions", "journeys")


def fetch_journey_detail(token: str, rest_url: str, journey_id: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    resp = httpx.get(
        f"{rest_url}/interaction/v1/interactions/{journey_id}",
        headers=headers, timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def enrich_journeys(
    journeys: list[dict[str, Any]], token: str, rest_url: str,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    total = len(journeys)
    for idx, j in enumerate(journeys, 1):
        jid = j.get("id", "")
        name = j.get("name", "unnamed")
        try:
            enriched.append(fetch_journey_detail(token, rest_url, jid))
        except httpx.HTTPStatusError:
            print(f"  Warning: could not fetch detail for journey {jid} ({name}), using list data")
            enriched.append(j)
        if idx % 25 == 0 or idx == total:
            print(f"  [journeys] Enriched {idx}/{total}")
    return enriched


def write_journeys(
    journeys: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    directory = JOURNEYS_DIR
    directory.mkdir(parents=True, exist_ok=True)

    existing_files = _load_existing_text(directory)
    prev_manifest = _load_previous_manifest(directory)
    written: set[str] = set()
    manifest: list[dict[str, Any]] = []
    added: list[dict[str, str]] = []
    modified: list[dict[str, str]] = []
    unchanged = 0

    for item in journeys:
        jid = item.get("id", "unknown")
        name = item.get("name", "unnamed")
        safe = sanitize_filename(name)
        filename = f"{jid}_{safe}.json"
        rel_path = f"journeys/{filename}"

        status_raw = item.get("status", "")
        status = JOURNEY_STATUS_MAP.get(status_raw, str(status_raw))
        mod_date = item.get("lastPublishedDate", "") or item.get("modifiedDate", "")

        content = _normalize_json(item)
        change = _detect_change(
            filename, rel_path, name, content,
            mod_date, "", existing_files, prev_manifest,
        )
        if change == "added":
            added.append(_change_entry(rel_path, name, "", mod_date))
        elif isinstance(change, dict):
            modified.append(change)
        else:
            unchanged += 1

        (directory / filename).write_text(content, encoding="utf-8")
        written.add(filename)

        activities = item.get("activities", [])
        triggers = item.get("triggers", [])
        goals = item.get("goals", [])
        manifest.append({
            "file": filename, "id": jid, "key": item.get("key", ""),
            "name": name, "version": item.get("version", 1), "status": status,
            "activityCount": len(activities),
            "triggerCount": len(triggers),
            "goalCount": len(goals),
            "createdDate": item.get("createdDate", ""),
            "modifiedDate": item.get("modifiedDate", ""),
            "lastPublishedDate": item.get("lastPublishedDate", ""),
        })

    deleted = _remove_stale(directory, existing_files, written, "journeys")
    _write_manifest(directory, manifest, key_fn=lambda m: m.get("name", ""))

    return manifest, _changes_dict(added, modified, deleted, unchanged)


# ===================================================================
#  CHANGELOG & COMMIT SUMMARY
# ===================================================================

def _format_change_line(entry: dict[str, str]) -> str:
    date_part = entry.get("modifiedDate", "")
    if date_part:
        date_part = date_part.split("T")[0]
    by = entry.get("modifiedBy", "")
    if by and by != "unknown":
        suffix = f" (modified in SFMC by {by} on {date_part})"
    elif date_part:
        suffix = f" (modified {date_part})"
    else:
        suffix = ""
    return f"- `{entry['file']}` -- \"{entry.get('name', '')}\"{suffix}"


def append_changelog(changes: dict[str, Any], timestamp: str) -> bool:
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
            if diff_text and diff_text != "(metadata changed, content identical)":
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
                lines.append("```")
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


def write_commit_summary(changes: dict[str, Any], timestamp: str) -> None:
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
            lines.append(f"  - {e['file']} ({e.get('name', '')})")
    if modified:
        lines.append("Modified:")
        for e in modified:
            lines.append(f"  - {e['file']} ({e.get('name', '')})")
    if deleted:
        lines.append("Deleted:")
        for e in deleted:
            lines.append(f"  - {e['file']}")
    lines.append("")

    COMMIT_SUMMARY_FILE.write_text("\n".join(lines), encoding="utf-8")


# ===================================================================
#  MAIN — each category is isolated so one failure doesn't block others
# ===================================================================

def _sync_category(label: str, fn: Any) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """Run a category sync; return (manifest, changes) or None on failure."""
    try:
        return fn()
    except Exception as exc:
        print(f"ERROR: {label} failed: {exc}")
        return None


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

    failures: list[str] = []
    change_lists: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    # --- Emails ---
    def _sync_emails():
        print("Fetching emails...")
        emails = fetch_cb_assets(token, rest_url, EMAIL_TYPE_IDS, "emails")
        print(f"Retrieved {len(emails)} email(s).")
        if emails:
            print(f"Re-fetching {len(emails)} email(s) individually for full content...")
            enriched = enrich_cb_assets(emails, token, rest_url, "emails")
        else:
            enriched = emails
        print(f"Writing emails to {EMAILS_DIR}/...")
        manifest, changes = write_cb_assets(enriched, EMAILS_DIR, "emails")
        _print_changes("Emails", changes)
        return manifest, changes

    result = _sync_category("Emails", _sync_emails)
    if result:
        counts["emails"] = len(result[0])
        change_lists.append(result[1])
    else:
        failures.append("Emails")

    # --- Templates ---
    def _sync_templates():
        print("Fetching templates...")
        templates = fetch_cb_assets(token, rest_url, TEMPLATE_TYPE_IDS, "templates")
        print(f"Retrieved {len(templates)} template(s).")
        if templates:
            print(f"Re-fetching {len(templates)} template(s) individually for full content...")
            enriched = enrich_cb_assets(templates, token, rest_url, "templates")
        else:
            enriched = templates
        print(f"Writing templates to {TEMPLATES_DIR}/...")
        manifest, changes = write_cb_assets(enriched, TEMPLATES_DIR, "templates")
        _print_changes("Templates", changes)
        return manifest, changes

    result = _sync_category("Templates", _sync_templates)
    if result:
        counts["templates"] = len(result[0])
        change_lists.append(result[1])
    else:
        failures.append("Templates")

    # --- Content Blocks ---
    def _sync_blocks():
        print("Fetching content blocks...")
        blocks = fetch_cb_assets(token, rest_url, CONTENT_BLOCK_TYPE_IDS, "content-blocks")
        print(f"Retrieved {len(blocks)} content block(s).")
        if blocks:
            print(f"Re-fetching {len(blocks)} content block(s) individually for full content...")
            enriched = enrich_cb_assets(blocks, token, rest_url, "content-blocks")
        else:
            enriched = blocks
        print(f"Writing content blocks to {CONTENT_BLOCKS_DIR}/...")
        manifest, changes = write_cb_assets(enriched, CONTENT_BLOCKS_DIR, "content-blocks")
        _print_changes("Content blocks", changes)
        return manifest, changes

    result = _sync_category("Content blocks", _sync_blocks)
    if result:
        counts["content blocks"] = len(result[0])
        change_lists.append(result[1])
    else:
        failures.append("Content blocks")

    # --- Images ---
    def _sync_images():
        print("Fetching images...")
        images = fetch_cb_assets(token, rest_url, IMAGE_TYPE_IDS, "images")
        print(f"Retrieved {len(images)} image(s).")
        print(f"Downloading images to {IMAGES_DIR}/...")
        manifest, changes = write_image_assets(images, IMAGES_DIR, "images", token)
        _print_changes("Images", changes)
        return manifest, changes

    result = _sync_category("Images", _sync_images)
    if result:
        counts["images"] = len(result[0])
        change_lists.append(result[1])
    else:
        failures.append("Images")

    # --- Data Extensions ---
    def _sync_data_extensions():
        print("Fetching Data Extensions...")
        des = fetch_data_extensions(token, subdomain)
        print(f"Retrieved {len(des)} Data Extension(s).")
        if des:
            print(f"Fetching field definitions for {len(des)} Data Extension(s)...")
            des = enrich_data_extensions(des, token, subdomain)
        print(f"Writing Data Extensions to {DATA_EXT_DIR}/...")
        manifest, changes = write_data_extensions(des)
        _print_changes("Data Extensions", changes)
        return manifest, changes

    result = _sync_category("Data Extensions", _sync_data_extensions)
    if result:
        counts["data extensions"] = len(result[0])
        change_lists.append(result[1])
    else:
        failures.append("Data Extensions")

    # --- Automations ---
    def _sync_automations():
        print("Fetching Automations...")
        autos = fetch_automations(token, rest_url)
        print(f"Retrieved {len(autos)} Automation(s).")
        print(f"Writing Automations to {AUTOMATIONS_DIR}/...")
        manifest, changes = write_automations(autos)
        _print_changes("Automations", changes)
        return manifest, changes

    result = _sync_category("Automations", _sync_automations)
    if result:
        counts["automations"] = len(result[0])
        change_lists.append(result[1])
    else:
        failures.append("Automations")

    # --- Automation Activities ---
    def _sync_activities():
        activities_by_type = fetch_all_activities(token, rest_url)
        print(f"Writing Automation Activities to {ACTIVITIES_DIR}/...")
        manifest, changes = write_all_activities(activities_by_type)
        return manifest, changes

    result = _sync_category("Automation Activities", _sync_activities)
    if result:
        counts["activities"] = len(result[0])
        change_lists.append(result[1])
    else:
        failures.append("Automation Activities")

    # --- Journeys ---
    def _sync_journeys():
        print("Fetching Journeys...")
        journeys = fetch_journeys(token, rest_url)
        print(f"Retrieved {len(journeys)} Journey(s).")
        if journeys:
            print(f"Re-fetching {len(journeys)} Journey(s) for full canvas data...")
            journeys_full = enrich_journeys(journeys, token, rest_url)
        else:
            journeys_full = journeys
        print(f"Writing Journeys to {JOURNEYS_DIR}/...")
        manifest, changes = write_journeys(journeys_full)
        _print_changes("Journeys", changes)
        return manifest, changes

    result = _sync_category("Journeys", _sync_journeys)
    if result:
        counts["journeys"] = len(result[0])
        change_lists.append(result[1])
    else:
        failures.append("Journeys")

    # --- Changelog & commit summary ---
    if change_lists:
        all_changes = _merge_changes(*change_lists)
        has_changes = all_changes["added"] or all_changes["modified"] or all_changes["deleted"]
        if has_changes:
            append_changelog(all_changes, timestamp)
            print("Updated CHANGELOG.md")
        write_commit_summary(all_changes, timestamp)

    total = sum(counts.values())
    parts = ", ".join(f"{v} {k}" for k, v in counts.items())
    print(f"\nDone. {total} asset(s) synced ({parts}).")

    if failures:
        print(f"WARNING: {len(failures)} category(ies) failed: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
