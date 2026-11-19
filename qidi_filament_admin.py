#!/usr/bin/env python3
"""
qidi-filament-admin: a tiny local web form for adding custom filament
entries to officiall_filas_list.cfg AND every filament-*.json file in
QIDI_Client's filament_config directory, without hand-editing files or
SSHing in every time.

Reachable at http://<printer-ip>:8899 on your LAN. There is NO
authentication - anyone on your network can use it. Fine for a home
LAN, not fine if your network isn't trusted.
"""

import glob
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

CFG_PATH = "/home/qidi/printer_data/config/officiall_filas_list.cfg"
FILAMENT_JSON_DIR = "/home/qidi/QIDI_Client/resource/filament_config"
FILAMENT_JSON_GLOB = os.path.join(FILAMENT_JSON_DIR, "filament-*.json")
SERVICE_NAME = "qidi-client.service"
PORT = 8899
CFG_BACKUP_DIR = "/home/qidi/printer_data/config/officiall_filas_list_backups"
JSON_BACKUP_DIR = os.path.join(FILAMENT_JSON_DIR, "filament_json_backups")

CFG_FIELD_NAMES = ["filament", "min_temp", "max_temp", "box_min_temp", "box_max_temp", "type"]
ALL_REQUIRED_FIELDS = CFG_FIELD_NAMES + ["vendor"]
# chamber_temperature and is_support have defaults, not required

CFG_FIELD_LINE_RE = {
    name: re.compile(r"^([ \t]*" + re.escape(name) + r"[ \t]*=[ \t]*)(.*)$", re.MULTILINE)
    for name in CFG_FIELD_NAMES
}
SECTION_SPLIT_RE = re.compile(r"(?=^\[)", re.MULTILINE)
SECTION_HEADER_RE = re.compile(r"^\[([A-Za-z_]+?)(\d*)\]")

# In every real entry seen so far these have byte-identical values
# regardless of material - looks like boilerplate tied to the physical
# drying box rather than per-filament tuning, so they're offered as
# pre-filled, overridable "advanced" fields rather than silently fixed.
# The one exception is ams_drying_ams_limitations: every entry has the
# exact same ["1","0"] pair, including support materials, and its
# meaning isn't established from the data - left genuinely fixed so a
# guess doesn't get written as fact.
JSON_DRYING_FIXED = {
    "filament_dev_ams_drying_ams_limitations": ["1", "0"],
}
JSON_DRYING_ADVANCED_DEFAULTS = {
    "filament_dev_ams_drying_temperature": ["45", "45", "45", "45"],
    "filament_dev_ams_drying_time": ["12", "12", "12", "12"],
    "filament_dev_drying_softening_temperature": ["50"],
    "filament_dev_ams_drying_heat_distortion_temperature": ["45"],
    "filament_dev_drying_cooling_temperature": ["45"],
    "filament_dev_chamber_drying_bed_temperature": ["70"],
    "filament_dev_chamber_drying_time": ["12.0"],
}
JSON_ID_PREFIX = "QFZ"  # reserved prefix for entries this tool adds


# ---------------------------------------------------------------- cfg ----

def read_cfg():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return f.read()


def backup_text(backup_dir, filename, content):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = os.path.join(backup_dir, f"{filename}.{stamp}.bak")
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(content)
    return backup_path


def write_text_preserving_stat(path, content):
    st = os.stat(path)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(tmp_path, st.st_mode)
    try:
        os.chown(tmp_path, st.st_uid, st.st_gid)
    except PermissionError:
        pass
    os.replace(tmp_path, path)


def cfg_name_exists(content, filament_name):
    for block in SECTION_SPLIT_RE.split(content):
        header = SECTION_HEADER_RE.match(block.strip())
        if not header or header.group(1) != "fila":
            continue
        m = CFG_FIELD_LINE_RE["filament"].search(block)
        if m and m.group(2).strip() == filament_name:
            return True
    return False


def cfg_block_is_blank_fila(block):
    header = SECTION_HEADER_RE.match(block.strip())
    if not header or header.group(1) != "fila":
        return False, None
    m = CFG_FIELD_LINE_RE["filament"].search(block)
    if not m:
        return False, None
    return (m.group(2).strip() == ""), int(header.group(2))


def cfg_fill_block(block, values):
    for name in CFG_FIELD_NAMES:
        pattern = CFG_FIELD_LINE_RE[name]
        new_value = str(values[name])

        def _sub(m, new_value=new_value):
            prefix = m.group(1)
            if not prefix.endswith(" "):
                prefix += " "
            return prefix + new_value

        block, n = pattern.subn(_sub, block, count=1)
        if n == 0:
            raise ValueError(f"template block is missing a '{name}' line")
    return block


def cfg_make_new_block(number, values):
    return (
        f"[fila{number}]\n"
        f"filament                       = {values['filament']}\n"
        f"min_temp                       = {values['min_temp']}\n"
        f"max_temp                       = {values['max_temp']}\n"
        f"box_min_temp                   = {values['box_min_temp']}\n"
        f"box_max_temp                   = {values['box_max_temp']}\n"
        f"type                           = {values['type']}\n"
        "\n"
    )


def cfg_find_block_index_by_name(blocks, filament_name):
    for i, block in enumerate(blocks):
        header = SECTION_HEADER_RE.match(block.strip())
        if not header or header.group(1) != "fila":
            continue
        m = CFG_FIELD_LINE_RE["filament"].search(block)
        if m and m.group(2).strip() == filament_name:
            return i
    return None


def cfg_edit_filament(content, original_name, values):
    """Rewrite the existing [filaN] block for original_name in place,
    including renaming it if values['filament'] differs. Raises
    ValueError if original_name isn't found."""
    blocks = SECTION_SPLIT_RE.split(content)
    idx = cfg_find_block_index_by_name(blocks, original_name)
    if idx is None:
        raise ValueError(f'"{original_name}" not found in {os.path.basename(CFG_PATH)}')
    header = SECTION_HEADER_RE.match(blocks[idx].strip())
    slot_num = header.group(2)
    blocks[idx] = cfg_fill_block(blocks[idx], values)
    return "".join(blocks), f"updated [fila{slot_num}]"


def cfg_add_filament(content, values):
    blocks = SECTION_SPLIT_RE.split(content)

    max_fila_num = 0
    colordict_index = None
    for i, block in enumerate(blocks):
        header = SECTION_HEADER_RE.match(block.strip())
        if not header:
            continue
        name, num = header.group(1), header.group(2)
        if name == "fila" and num:
            max_fila_num = max(max_fila_num, int(num))
        elif name == "colordict" and colordict_index is None:
            colordict_index = i

    for i, block in enumerate(blocks):
        is_blank, num = cfg_block_is_blank_fila(block)
        if is_blank:
            blocks[i] = cfg_fill_block(block, values)
            return "".join(blocks), f"filled existing blank slot [fila{num}]"

    new_num = max_fila_num + 1
    new_block = cfg_make_new_block(new_num, values)
    insert_at = colordict_index if colordict_index is not None else len(blocks)
    blocks.insert(insert_at, new_block)
    return "".join(blocks), f"appended new slot [fila{new_num}]"


def restart_service():
    subprocess.run(["systemctl", "restart", SERVICE_NAME], check=True)


# --------------------------------------------------------------- json ----

def json_generate_id(all_existing_ids):
    for i in range(1, 100):
        candidate = f"{JSON_ID_PREFIX}{i:02d}"
        if candidate not in all_existing_ids:
            return candidate
    raise RuntimeError("Exhausted QFZ01-QFZ99 ID space")


def resolve_drying_value(key, raw, default_list):
    """Parse one advanced-drying field's raw form input against its
    default list. Returns a list of the right length, or raises
    ValueError with a message naming the field - used both for
    pre-flight validation (nothing written yet) and actual entry
    building, so the two can never disagree."""
    if raw is None or str(raw).strip() == "":
        return list(default_list)
    parts = [p.strip() for p in str(raw).split(",") if p.strip() != ""]
    if len(parts) == 1 and len(default_list) > 1:
        parts = parts * len(default_list)
    if len(parts) != len(default_list):
        raise ValueError(
            f"{key} needs {len(default_list)} value(s) "
            f"(comma-separated), got {len(parts)}"
        )
    return parts


def validate_advanced_drying_fields(values):
    """Raise ValueError naming the first bad field, before anything is
    written. Returns nothing - call resolve_drying_value again per
    entry to get the actual values."""
    for key, default_list in JSON_DRYING_ADVANCED_DEFAULTS.items():
        resolve_drying_value(key, values.get(key, ""), default_list)


def json_build_entry(values, filament_id):
    entry = {
        "type": values["type"],
        "filament_id": filament_id,
        "nozzle_temperature": [int(values["min_temp"]), int(values["max_temp"])],
        "box_temperature_range_high": [str(values["box_max_temp"])],
        "box_temperature_range_low": [str(values["box_min_temp"])],
        "filament_vendor": values["vendor"],
        "filament_is_support": "1" if values.get("is_support") else "0",
        "chamber_temperatures": str(values.get("chamber_temperature") or "0"),
    }
    entry.update(JSON_DRYING_FIXED)
    for key, default_list in JSON_DRYING_ADVANCED_DEFAULTS.items():
        entry[key] = resolve_drying_value(key, values.get(key, ""), default_list)
    return entry


def entry_to_form_values(entry):
    """Reverse an existing JSON entry back into the form-field dict
    shape, for pre-filling the edit form."""
    values = {
        "type": entry.get("type", ""),
        "vendor": entry.get("filament_vendor", ""),
        "min_temp": entry.get("nozzle_temperature", ["", ""])[0],
        "max_temp": entry.get("nozzle_temperature", ["", ""])[1],
        "box_min_temp": (entry.get("box_temperature_range_low") or [""])[0],
        "box_max_temp": (entry.get("box_temperature_range_high") or [""])[0],
        "chamber_temperature": entry.get("chamber_temperatures", "0"),
        "is_support": entry.get("filament_is_support") == "1",
    }
    for key in JSON_DRYING_ADVANCED_DEFAULTS:
        lst = entry.get(key) or []
        values[key] = ",".join(str(v) for v in lst)
    return values


def json_write(path, data):
    st = os.stat(path)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp_path, st.st_mode)
    try:
        os.chown(tmp_path, st.st_uid, st.st_gid)
    except PermissionError:
        pass
    os.replace(tmp_path, path)


# ------------------------------------------------------------ pipeline ----

def discover_json_files():
    return sorted(glob.glob(FILAMENT_JSON_GLOB))


def validate_no_collisions(filament_name, cfg_content, json_paths_and_data):
    """Raise ValueError naming exactly where a collision was found,
    before anything is written."""
    if cfg_name_exists(cfg_content, filament_name):
        raise ValueError(
            f'"{filament_name}" already exists in {os.path.basename(CFG_PATH)}'
        )
    for path, data in json_paths_and_data:
        if filament_name in data:
            raise ValueError(f'"{filament_name}" already exists in {os.path.basename(path)}')


def list_editable_filaments():
    """Names this tool can safely edit: present with a QFZ* filament_id
    in EVERY discovered json file (same id in each), AND present in
    the cfg. Anything inconsistent (partially added, hand-edited, or
    one of QIDI's own stock entries) is left out rather than guessed
    about - editing only covers what this tool itself can fully
    account for."""
    json_paths = discover_json_files()
    if not json_paths:
        return []

    cfg_content = read_cfg()
    per_file_qfz = []
    for path in json_paths:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        per_file_qfz.append(
            {name: e.get("filament_id") for name, e in data.items()
             if str(e.get("filament_id", "")).startswith(JSON_ID_PREFIX)}
        )

    common_names = set(per_file_qfz[0].keys())
    for d in per_file_qfz[1:]:
        common_names &= set(d.keys())

    result = []
    for name in sorted(common_names):
        ids = {d[name] for d in per_file_qfz}
        if len(ids) != 1:
            continue  # inconsistent id across files - don't offer it
        if not cfg_name_exists(cfg_content, name):
            continue  # not in cfg - don't offer it
        result.append(name)
    return result


def load_entry_for_edit(name):
    """Returns (entry, json_paths) for a name that list_editable_filaments
    would also return, re-validated at call time (not trusting a stale
    listing from an earlier page load)."""
    if name not in list_editable_filaments():
        raise ValueError(
            f'"{name}" is not an editable (tool-added) filament - either it '
            "does not exist, is one of QIDI's stock entries, or is in an "
            "inconsistent state across files (check the backups)."
        )
    json_paths = discover_json_files()
    with open(json_paths[0], "r", encoding="utf-8") as f:
        data = json.load(f)
    return data[name], json_paths


def apply_filament_edit(original_name, values):
    """Same nothing-written-until-validated, one-restart-at-the-end
    guarantees as apply_filament_addition."""
    new_name = values["filament"]

    if original_name not in list_editable_filaments():
        raise ValueError(
            f'"{original_name}" is not an editable (tool-added) filament - '
            "either it no longer exists, is one of QIDI's stock entries, or "
            "is in an inconsistent state across files."
        )

    cfg_content = read_cfg()
    json_paths = discover_json_files()
    json_data_by_path = {}
    existing_filament_id = None
    for path in json_paths:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
        json_data_by_path[path] = (raw, data)
        existing_filament_id = data[original_name]["filament_id"]

    if new_name != original_name:
        validate_no_collisions(
            new_name, cfg_content,
            [(p, d) for p, (raw, d) in json_data_by_path.items()],
        )
    validate_advanced_drying_fields(values)

    results = []

    new_cfg_content, slot_desc = cfg_edit_filament(cfg_content, original_name, values)
    cfg_backup = backup_text(CFG_BACKUP_DIR, os.path.basename(CFG_PATH), cfg_content)
    try:
        write_text_preserving_stat(CFG_PATH, new_cfg_content)
    except Exception as e:
        raise RuntimeError(
            f"Failed writing {CFG_PATH} ({e}). Nothing else was touched. "
            f"Backup (unneeded, nothing changed yet) at {cfg_backup}"
        )
    results.append(f"{os.path.basename(CFG_PATH)}: {slot_desc} (backup: {cfg_backup})")

    written_json = []
    try:
        for path, (raw, data) in json_data_by_path.items():
            entry = json_build_entry(values, existing_filament_id)
            if new_name != original_name:
                del data[original_name]
            data[new_name] = entry
            backup_path = backup_text(JSON_BACKUP_DIR, os.path.basename(path), raw)
            json_write(path, data)
            written_json.append(os.path.basename(path))
            results.append(
                f"{os.path.basename(path)}: updated \"{original_name}\""
                + (f' -> "{new_name}"' if new_name != original_name else "")
                + f" (id {existing_filament_id}, backup: {backup_path})"
            )
    except Exception as e:
        raise RuntimeError(
            "Partial failure while writing filament-*.json files. "
            f"Successfully updated: {written_json or '(none)'}. "
            f"{os.path.basename(CFG_PATH)} WAS already updated. Error: {e}. "
            "Check backups to restore consistency."
        )

    try:
        restart_service()
    except Exception as e:
        raise RuntimeError(
            f"Files were written successfully, but restarting {SERVICE_NAME} "
            f"failed: {e}. Restart it manually."
        )
    results.append(f"{SERVICE_NAME} restarted.")

    return results


def apply_filament_delete(name):
    """Same scope restriction as editing: only a tool-added (QFZ*),
    cross-file-consistent entry can be deleted. The cfg slot is reset
    to blank (matching the format QIDI's own file already uses for
    unused slots, e.g. fila9 in your original file) rather than the
    section being removed, so it becomes a normal reusable blank slot
    again. Each json file has the key removed outright."""
    if name not in list_editable_filaments():
        raise ValueError(
            f'"{name}" is not a deletable (tool-added) filament - either it '
            "does not exist, is one of QIDI's stock entries, or is in an "
            "inconsistent state across files."
        )

    cfg_content = read_cfg()
    json_paths = discover_json_files()
    json_data_by_path = {}
    for path in json_paths:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        json_data_by_path[path] = (raw, json.loads(raw))

    blocks = SECTION_SPLIT_RE.split(cfg_content)
    idx = cfg_find_block_index_by_name(blocks, name)
    if idx is None:
        raise ValueError(f'"{name}" not found in {os.path.basename(CFG_PATH)}')
    header = SECTION_HEADER_RE.match(blocks[idx].strip())
    slot_num = header.group(2)
    blank_values = {k: "" for k in CFG_FIELD_NAMES}
    blocks[idx] = cfg_fill_block(blocks[idx], blank_values)
    new_cfg_content = "".join(blocks)

    results = []

    cfg_backup = backup_text(CFG_BACKUP_DIR, os.path.basename(CFG_PATH), cfg_content)
    try:
        write_text_preserving_stat(CFG_PATH, new_cfg_content)
    except Exception as e:
        raise RuntimeError(
            f"Failed writing {CFG_PATH} ({e}). Nothing else was touched. "
            f"Backup (unneeded, nothing changed yet) at {cfg_backup}"
        )
    results.append(
        f"{os.path.basename(CFG_PATH)}: cleared [fila{slot_num}] "
        f"(backup: {cfg_backup})"
    )

    written_json = []
    try:
        for path, (raw, data) in json_data_by_path.items():
            if name not in data:
                continue
            backup_path = backup_text(JSON_BACKUP_DIR, os.path.basename(path), raw)
            del data[name]
            json_write(path, data)
            written_json.append(os.path.basename(path))
            results.append(
                f'{os.path.basename(path)}: removed "{name}" (backup: {backup_path})'
            )
    except Exception as e:
        raise RuntimeError(
            "Partial failure while writing filament-*.json files. "
            f"Successfully updated: {written_json or '(none)'}. "
            f"{os.path.basename(CFG_PATH)} WAS already cleared. Error: {e}. "
            "Check backups to restore consistency."
        )

    try:
        restart_service()
    except Exception as e:
        raise RuntimeError(
            f"Files were written successfully, but restarting {SERVICE_NAME} "
            f"failed: {e}. Restart it manually."
        )
    results.append(f"{SERVICE_NAME} restarted.")

    return results


def apply_filament_addition(values):
    """Does all reads/validation first, then all writes, then one
    restart. Returns a list of human-readable result lines.
    Raises ValueError (safe, nothing written) or RuntimeError (partial
    write occurred - message says exactly how far it got)."""
    filament_name = values["filament"]

    cfg_content = read_cfg()

    json_paths = discover_json_files()
    json_data_by_path = {}
    for path in json_paths:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        json_data_by_path[path] = (raw, json.loads(raw))

    validate_no_collisions(
        filament_name, cfg_content,
        [(p, d) for p, (raw, d) in json_data_by_path.items()],
    )
    validate_advanced_drying_fields(values)

    results = []

    # --- write cfg ---
    new_cfg_content, slot_desc = cfg_add_filament(cfg_content, values)
    cfg_backup = backup_text(CFG_BACKUP_DIR, os.path.basename(CFG_PATH), cfg_content)
    try:
        write_text_preserving_stat(CFG_PATH, new_cfg_content)
    except Exception as e:
        raise RuntimeError(
            f"Failed writing {CFG_PATH} ({e}). Nothing else was touched. "
            f"Backup (unneeded, nothing changed yet) at {cfg_backup}"
        )
    results.append(f"{os.path.basename(CFG_PATH)}: {slot_desc} (backup: {cfg_backup})")

    # --- write every json file, using one shared filament_id across all of them ---
    all_existing_ids = set()
    for _, data in json_data_by_path.values():
        for entry in data.values():
            fid = entry.get("filament_id")
            if fid:
                all_existing_ids.add(fid)
    shared_filament_id = json_generate_id(all_existing_ids)

    written_json = []
    try:
        for path, (raw, data) in json_data_by_path.items():
            entry = json_build_entry(values, shared_filament_id)
            data[filament_name] = entry
            backup_path = backup_text(JSON_BACKUP_DIR, os.path.basename(path), raw)
            json_write(path, data)
            written_json.append(os.path.basename(path))
            results.append(
                f"{os.path.basename(path)}: added as \"{filament_name}\" "
                f"(id {shared_filament_id}, backup: {backup_path})"
            )
    except Exception as e:
        raise RuntimeError(
            "Partial failure while writing filament-*.json files. "
            f"Successfully updated: {written_json or '(none)'}. "
            f"{os.path.basename(CFG_PATH)} WAS already updated. Error: {e}. "
            "Check backups to restore consistency."
        )

    if not json_paths:
        results.append(
            f"WARNING: no filament-*.json files found under {FILAMENT_JSON_DIR} "
            "- only the cfg allowlist was updated."
        )

    try:
        restart_service()
    except Exception as e:
        raise RuntimeError(
            f"Files were written successfully, but restarting {SERVICE_NAME} "
            f"failed: {e}. Restart it manually."
        )
    results.append(f"{SERVICE_NAME} restarted.")

    return results


# ------------------------------------------------------------- web app ----

PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QIDI custom filament</title>
<style>
  body {{ font-family: sans-serif; max-width: 480px; margin: 2em auto; padding: 0 1em; }}
  label {{ display: block; margin-top: 0.8em; font-weight: bold; }}
  input, select {{ width: 100%; padding: 0.4em; font-size: 1em; box-sizing: border-box; }}
  button {{ margin-top: 1.2em; padding: 0.6em 1.2em; font-size: 1em; }}
  .msg {{ padding: 0.8em; margin-bottom: 1em; border-radius: 4px; white-space: pre-wrap; }}
  .ok {{ background: #d7f5d7; }}
  .err {{ background: #f5d7d7; }}
  .row {{ display: flex; gap: 0.5em; }}
  .row > div {{ flex: 1; }}
  .checkbox-row {{ display: flex; align-items: center; gap: 0.5em; margin-top: 0.8em; }}
  .checkbox-row input {{ width: auto; }}
</style>
</head>
<body>
<h2>{heading}</h2>
{editable_list_html}
{message}
<form method="POST">
  <input type="hidden" name="mode" value="{mode}">
  <input type="hidden" name="original_name" value="{original_name}">
  <label>Display name</label>
  <input name="filament" required value="{filament}" placeholder="e.g. PolyLite PETG-HF">

  <label>Vendor</label>
  <input name="vendor" required value="{vendor}" placeholder="e.g. PolyLite">

  <label>Type</label>
  <input name="type" required value="{type}" placeholder="e.g. PETG, PLA-CF, My-Custom-Type">

  <div class="row">
    <div>
      <label>Min nozzle temp</label>
      <input name="min_temp" type="number" required value="{min_temp}">
    </div>
    <div>
      <label>Max nozzle temp</label>
      <input name="max_temp" type="number" required value="{max_temp}">
    </div>
  </div>

  <div class="row">
    <div>
      <label>Box min temp</label>
      <input name="box_min_temp" type="number" required value="{box_min_temp}">
    </div>
    <div>
      <label>Box max temp</label>
      <input name="box_max_temp" type="number" required value="{box_max_temp}">
    </div>
  </div>

  <label>Chamber temperature</label>
  <input name="chamber_temperature" type="number" value="{chamber_temperature}" placeholder="0">

  <div class="checkbox-row">
    <input type="checkbox" name="is_support" id="is_support" {is_support_checked}>
    <label for="is_support" style="margin:0">This is a support material</label>
  </div>

  <details style="margin-top:1.2em">
    <summary style="cursor:pointer;font-weight:bold">Advanced drying settings (optional)</summary>
    <p style="color:#888;font-size:0.85em">
      Every existing entry QIDI shipped uses the same values here
      regardless of material - leave these blank to match that.
      filament_dev_ams_drying_ams_limitations is not editable here;
      every entry has the identical ["1","0"] pair and its meaning
      isn't established, so a guessed override isn't offered.
    </p>

    <label>AMS drying temperature (1 value, or 4 comma-separated, per slot)</label>
    <input name="ams_drying_temperature" value="{ams_drying_temperature}" placeholder="45,45,45,45">

    <label>AMS drying time, hours (1 value, or 4 comma-separated, per slot)</label>
    <input name="ams_drying_time" value="{ams_drying_time}" placeholder="12,12,12,12">

    <label>Drying softening temperature</label>
    <input name="drying_softening_temperature" value="{drying_softening_temperature}" placeholder="50">

    <label>AMS drying heat distortion temperature</label>
    <input name="ams_drying_heat_distortion_temperature" value="{ams_drying_heat_distortion_temperature}" placeholder="45">

    <label>Drying cooling temperature</label>
    <input name="drying_cooling_temperature" value="{drying_cooling_temperature}" placeholder="45">

    <label>Chamber drying bed temperature</label>
    <input name="chamber_drying_bed_temperature" value="{chamber_drying_bed_temperature}" placeholder="70">

    <label>Chamber drying time, hours</label>
    <input name="chamber_drying_time" value="{chamber_drying_time}" placeholder="12.0">
  </details>

  <button type="submit">{submit_label}</button>
</form>
{delete_form}
<p style="color:#888;font-size:0.85em;margin-top:2em">
Updates {cfg_path} and every filament-*.json under {json_dir},
backing each up first, then restarts {service}.
</p>
</body>
</html>
"""

ADVANCED_DRYING_FORM_NAMES = [
    "ams_drying_temperature",
    "ams_drying_time",
    "drying_softening_temperature",
    "ams_drying_heat_distortion_temperature",
    "drying_cooling_temperature",
    "chamber_drying_bed_temperature",
    "chamber_drying_time",
]
# Map the short form field names above to the real JSON keys they override.
ADVANCED_DRYING_JSON_KEY = {
    "ams_drying_temperature": "filament_dev_ams_drying_temperature",
    "ams_drying_time": "filament_dev_ams_drying_time",
    "drying_softening_temperature": "filament_dev_drying_softening_temperature",
    "ams_drying_heat_distortion_temperature": "filament_dev_ams_drying_heat_distortion_temperature",
    "drying_cooling_temperature": "filament_dev_drying_cooling_temperature",
    "chamber_drying_bed_temperature": "filament_dev_chamber_drying_bed_temperature",
    "chamber_drying_time": "filament_dev_chamber_drying_time",
}

FORM_FIELDS = [
    "filament", "vendor", "type", "min_temp", "max_temp",
    "box_min_temp", "box_max_temp", "chamber_temperature",
] + ADVANCED_DRYING_FORM_NAMES


def _render_editable_list():
    try:
        names = list_editable_filaments()
    except Exception as e:  # noqa: BLE001
        return f'<p style="color:#b00">Could not list existing filaments: {html.escape(str(e))}</p>'
    if not names:
        return ""
    items = "".join(
        f'<li><a href="/edit?name={quote(n)}">{html.escape(n)}</a></li>' for n in names
    )
    return (
        '<details style="margin-bottom:1em">'
        "<summary style=\"cursor:pointer\">Edit an existing custom filament "
        f"({len(names)})</summary><ul>{items}</ul></details>"
    )


class Handler(BaseHTTPRequestHandler):
    def _render(self, message="", fields=None, is_support_checked=False,
                mode="add", original_name="", heading=None, submit_label=None,
                show_list=True):
        fields = fields or {}
        defaults = {name: "" for name in FORM_FIELDS}
        defaults.update(fields)
        escaped = {k: html.escape(str(v)) for k, v in defaults.items()}

        if heading is None:
            heading = "Edit custom filament" if mode == "edit" else "Add custom filament"
        if submit_label is None:
            submit_label = (
                "Save changes & restart QIDI Client" if mode == "edit"
                else "Add filament & restart QIDI Client"
            )

        delete_form = ""
        if mode == "edit" and original_name:
            delete_form = (
                '<form method="POST" style="margin-top:0.5em" '
                "onsubmit=\"return confirm('Delete " +
                html.escape(original_name).replace("'", "&#39;") +
                " from every file? This cannot be undone from the UI "
                "(only from the backup files).');\">"
                '<input type="hidden" name="mode" value="delete">'
                f'<input type="hidden" name="original_name" value="{html.escape(original_name)}">'
                '<button type="submit" style="background:#c0392b;color:white">'
                "Delete this filament</button>"
                "</form>"
            )

        page = PAGE_TEMPLATE.format(
            message=message,
            heading=html.escape(heading),
            submit_label=html.escape(submit_label),
            mode=html.escape(mode),
            original_name=html.escape(original_name),
            delete_form=delete_form,
            editable_list_html=_render_editable_list() if show_list else "",
            cfg_path=html.escape(CFG_PATH),
            json_dir=html.escape(FILAMENT_JSON_DIR),
            service=html.escape(SERVICE_NAME),
            is_support_checked="checked" if is_support_checked else "",
            **escaped,
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _render_404(self, message):
        body = message.encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed_url = urlparse(self.path)
        if parsed_url.path in ("/", "/index.html"):
            self._render()
            return

        if parsed_url.path == "/edit":
            qs = parse_qs(parsed_url.query)
            name = qs.get("name", [""])[0]
            if not name:
                self._render_404("Missing ?name= for /edit")
                return
            try:
                entry, _ = load_entry_for_edit(name)
            except ValueError as e:
                self._render(
                    f'<div class="msg err">{html.escape(str(e))}</div>',
                    mode="add",
                )
                return
            form_values = entry_to_form_values(entry)
            form_values["filament"] = name
            self._render(
                fields=form_values,
                is_support_checked=bool(form_values.get("is_support")),
                mode="edit",
                original_name=name,
                show_list=False,
            )
            return

        self._render_404("Not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(raw)
        values = {}
        for name in FORM_FIELDS:
            values[name] = parsed.get(name, [""])[0].strip()
        is_support = "is_support" in parsed
        values["is_support"] = is_support

        mode = parsed.get("mode", ["add"])[0]
        original_name = parsed.get("original_name", [""])[0]

        # json_build_entry looks up advanced-drying overrides by their
        # real JSON key names; translate the short form field names.
        for short_name, json_key in ADVANCED_DRYING_JSON_KEY.items():
            values[json_key] = values.get(short_name, "")

        if mode == "delete":
            if not original_name:
                self._render(
                    '<div class="msg err">Missing: original_name</div>',
                    mode="add",
                )
                return
            try:
                results = apply_filament_delete(original_name)
            except (ValueError, RuntimeError) as e:
                self._render(
                    f'<div class="msg err">Failed: {html.escape(str(e))}</div>',
                    mode="add",
                )
                return
            except Exception as e:  # noqa: BLE001
                self._render(
                    f'<div class="msg err">Unexpected error: {html.escape(str(e))}</div>',
                    mode="add",
                )
                return
            message_lines = "\n".join(results)
            self._render(
                f'<div class="msg ok">Deleted "{html.escape(original_name)}":\n'
                f'{html.escape(message_lines)}</div>',
                mode="add",
            )
            return

        missing = [n for n in ALL_REQUIRED_FIELDS if values.get(n, "") == ""]
        if mode == "edit" and not original_name:
            missing.append("original_name")
        if missing:
            self._render(
                f'<div class="msg err">Missing: {html.escape(", ".join(missing))}</div>',
                fields=values, is_support_checked=is_support,
                mode=mode, original_name=original_name,
                show_list=(mode != "edit"),
            )
            return

        try:
            if mode == "edit":
                results = apply_filament_edit(original_name, values)
            else:
                results = apply_filament_addition(values)
        except (ValueError, RuntimeError) as e:
            self._render(
                f'<div class="msg err">Failed: {html.escape(str(e))}</div>',
                fields=values, is_support_checked=is_support,
                mode=mode, original_name=original_name,
                show_list=(mode != "edit"),
            )
            return
        except Exception as e:  # noqa: BLE001
            self._render(
                f'<div class="msg err">Unexpected error: {html.escape(str(e))}</div>',
                fields=values, is_support_checked=is_support,
                mode=mode, original_name=original_name,
                show_list=(mode != "edit"),
            )
            return

        message_lines = "\n".join(results)
        verb = "Updated" if mode == "edit" else "Added"
        self._render(
            f'<div class="msg ok">{verb} "{html.escape(values["filament"])}":\n'
            f'{html.escape(message_lines)}</div>',
            mode="add",
        )

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving on http://0.0.0.0:{PORT} ...")
    server.serve_forever()


if __name__ == "__main__":
    main()
