# qidi-filament-admin

A small, self-contained local web tool for the QIDI Plus5 (and likely other
QIDI printers running the same `QIDI_Client` touchscreen stack) that lets you
add, edit, and delete **custom filament profiles** — something the printer's
own touchscreen UI and QIDI Studio can't do for you.

## Why this exists

QIDI Studio (the desktop slicer) lets you create a custom filament profile,
but "Send to Printer" fails for it, because the printer only recognizes
filaments listed in a closed catalog:

- `/home/qidi/printer_data/config/officiall_filas_list.cfg` — a flat,
  fixed-size INI-style allowlist (up to 50 `[filaN]` slots) that both
  Moonraker/Klipper and QIDI Studio's sync check against.
- `/home/qidi/QIDI_Client/resource/filament_config/filament-{0.2,0.4,0.6,0.8}.json`
  — a separate, richer per-filament catalog that the on-printer touchscreen
  app (`QIDI_Client`) actually reads from. A filament has to exist
  **consistently in both places** for the touchscreen UI to show it and for
  QIDI Studio's sync to stop complaining.

`QIDI_Client` itself is a closed, stripped ARM64 binary — patching it
directly to add a "new filament" dialog was ruled out as infeasible/risky.
This tool gets the same practical outcome (no SSH, no manual file editing,
no running a script by hand each time) by exposing a tiny local web form that
does the file surgery for you, safely.

## Supported printers

Built and confirmed working on a QIDI Plus5, against its real officiall_filas_list.cfg and filament-0.4.json.

QIDI's own factory firmware moved onto the Klipper/Moonraker/Fluidd stack starting with the Plus4 (released September 2024); every model since — Plus4, Plus5, Max4, Q2, Q2C — ships that same stack plus the closed QIDI_Client touchscreen app this tool works around. Those other models probably use the same officiall_filas_list.cfg format and the same filament-{0.2,0.4,0.6,0.8}.json layout under QIDI_Client/resource/filament_config/, since they're running the same app — but that hasn't been verified, and QIDI could ship a different schema or paths per model/firmware version. Before relying on this against a non-Plus5 printer:

Confirm officiall_filas_list.cfg and the filament-*.json files exist at the paths this tool expects (see Why this exists), and skim one filament-*.json file to make sure its field names match what Fields describes.
Test against a backup copy first, or at least expect to need to restore from the automatic backups (see Backups) if the schema turns out to differ.

Older, pre-Plus4 QIDI printers (X-Plus, X-Max, X-Plus3, X-Smart3, etc.) don't ship this stack at all — Klipper on those is a community add-on (FreeDi/FreeQIDI), and they don't run QIDI_Client, so this tool doesn't apply to them.

## What it does

- **Add** a custom filament: writes a consistent entry into
  `officiall_filas_list.cfg` *and* every `filament-*.json` file present on
  the printer (it auto-discovers which nozzle sizes exist), sharing one
  generated `filament_id` across all of them.
- **Edit** a filament this tool previously added, including renaming it.
- **Delete** a filament this tool previously added — removes it from every
  JSON file and resets its `.cfg` slot back to a genuinely blank, reusable
  slot (matching QIDI's own blank-slot format exactly).
- Exposes the JSON-only "advanced drying" fields (`filament_dev_*`) as an
  optional, pre-filled collapsible section instead of leaving them hidden.

Editing and deleting are intentionally scoped to **entries this tool
created** (identified by a `QFZ`-prefixed `filament_id`), never QIDI's own
stock catalog — see [Scope: why only tool-added entries](#scope-why-only-tool-added-entries) below.

## Safety model

Every write goes through the same pipeline:

1. **Validate everything first.** Name collisions (across the `.cfg` *and*
   every `.json` file), malformed advanced-drying input, duplicate names —
   all checked *before* anything is touched. If any check fails, nothing is
   written (`ValueError` — zero side effects).
2. **Back up every file that's about to change** (raw pre-edit text, saved
   with a timestamp) before writing it.
3. **Write all files.**
4. **Restart `qidi-client.service` once**, at the end, only if every write
   in the batch succeeded.

If something fails partway through an already-validated batch (disk full,
permissions, etc.), you get a `RuntimeError` stating exactly how far it got,
so a partial write is never silent. This has been exercised with real HTTP
round-trips against reconstructed fixture directory trees, and confirmed
end-to-end on the actual printer — see [What's tested](#whats-tested).

## Installation

[See Installation Instructions](./INSTALL.md)
   ```

## Usage

### Adding a filament

Fill in the form on the index page and submit. In order, the tool:

- Reads `officiall_filas_list.cfg` and every `filament-*.json` file under
  `filament_config/` (whichever nozzle sizes exist — 0.2/0.4/0.6/0.8 — it
  doesn't assume all four are present).
- Checks the display name doesn't already exist in *any* of those files. If
  it does, nothing is written and you get a clear error naming exactly where
  the collision is.
- If clear: backs up every file about to change, writes all of them with the
  same `filament_id` shared across every JSON file (so the ID stays
  consistent across nozzle sizes), and fills the first blank `[filaN]` slot
  in the `.cfg` (or adds a new numbered one if all 50 are used).
- Restarts `qidi-client.service` once, at the end, only on full success.

### Editing a filament

The index page shows an "Edit an existing custom filament" list of anything
you've added through this tool, each linking to a pre-filled edit form.

You can rename a filament while editing (just change the Display name field)
— it moves the entry to the new key in every file and keeps its original
`filament_id`. Renaming to a name that already exists anywhere is rejected
with nothing written, same guarantee as adding a duplicate.

### Deleting a filament

The edit page has a red "Delete this filament" button below the save
button, with a JS `confirm()` prompt before it submits.

What deleting actually does:

- Each `filament-*.json` file: the key is removed from the dict outright.
- `officiall_filas_list.cfg`: the `[filaN]` slot is reset to blank (every
  field empty) — matching the exact format QIDI's own file already uses for
  its unused slots — rather than removing the section. This makes the slot a
  normal, immediately reusable blank slot for the next filament you add
  (confirmed by testing that a subsequent add reuses the freed slot number).

Every file is backed up before the delete touches it, same as adding or
editing. There's no in-UI undo — restore from the relevant backup file if
you delete the wrong thing.

### Scope: why only tool-added entries

Editing and deleting only cover filaments **this tool added** (identified by
their `filament_id` starting with `QFZ`), never QIDI's own stock catalog.

QIDI's stock entries use *different* display names between
`officiall_filas_list.cfg` and the `filament-*.json` files — e.g. `.cfg` has
`"PLA Rapido"`, the JSON files have `"QIDI PLA Rapido"` for the same
material. They aren't the same key space for pre-existing data; only
entries this tool adds are forced to match across files. Editing stock
entries safely would mean resolving that mismatch per-entry, which wasn't
built.

A tool-added entry that's somehow become inconsistent (present in some files
but not others, or with mismatched IDs across files) also won't be offered
for editing or deleting — the listing and the edit/delete submission both
re-check consistency live, not just at page load.

### Fields

- **Display name**: must be unique across all files. This is the string
  you'll also use/match in QIDI Studio's custom filament dialog.
- **Vendor**: free text — not restricted to Generic/QIDI (the real
  `filament-0.4.json` already has Bambu, HATCHBOX, Overture, Polymaker,
  Sunlu as vendors).
- **Type**: free text, doesn't have to match an existing type string.
- **Min/max nozzle temp, box min/max temp**: as in QIDI Studio.
- **Chamber temperature**: printing chamber target (not drying-box temp).
  Defaults to 0 if left blank.
- **Support material checkbox**: sets `filament_is_support`.

The box-drying-profile fields in each JSON entry (`filament_dev_*`) are
exposed under an "Advanced drying settings" collapsible section, optional
and pre-filled with the boilerplate defaults (every existing entry in the
real `filament-0.4.json` had byte-identical values for them regardless of
material — leave them blank to match that). Accepts either one value
(replicated across all 4 box slots where the field has 4) or an explicit
comma-separated value per slot, e.g. `"55,60,65,70"`. A malformed count is
rejected before anything is written — same nothing-written-on-error
guarantee as a duplicate name.

`filament_dev_ams_drying_ams_limitations` is the one exception: every entry
has the identical `["1","0"]` pair including support materials, and its
meaning isn't established from the data, so it's left fixed rather than
exposing a guess as an editable field.

## Backups

Every file gets backed up before it's touched:

- `/home/qidi/printer_data/config/officiall_filas_list_backups/`
- `/home/qidi/QIDI_Client/resource/filament_config/filament_json_backups/`

## If something looks wrong

Stop the service, restore the relevant backup(s), restart:

```
sudo systemctl stop qidi-client.service
cp <backup file> <original path it came from>
sudo systemctl start qidi-client.service
```

## Security note

This has no login and no HTTPS — it's meant for a trusted home LAN only.
Anyone who can reach port 8899 on your network can add, edit, or delete
entries. Don't port-forward this or expose it outside your LAN.

## What's tested

Tested against a reconstructed copy of the real `officiall_filas_list.cfg`
and a subset of the real `filament-0.4.json` (verified round-trips through
`json.load`/`json.dump` with no type corruption — `nozzle_temperature`
stays integers, everything else stays strings, exactly like the original).
Also tested via real HTTP round-trips against the actual server code (not
just the underlying functions):

- Multi-file writes with a shared, non-colliding `filament_id`.
- Pre-write collision detection across the `.cfg` *and* every `.json` file
  (rejecting with nothing written if a duplicate name exists anywhere), and
  that a rejected duplicate leaves zero trace (no extra backups, no partial
  writes).
- Editing, including rename-across-all-files and rejection of edits to stock
  or inconsistent entries.
- Deleting, including rejection of deleting stock or nonexistent entries
  (zero file changes, zero new backups), correct JSON key removal, correct
  `.cfg` slot blanking matching QIDI's own format, and that a freed slot is
  correctly reused by a later add.

Confirmed on real hardware: `qidi-client.service` re-reads these files
cleanly on restart. After adding a filament, it shows up on the
touchscreen, and QIDI Studio's "Sync filaments with BOX" shows a one-time
"unknown filament" dialog on the first sync — a stale-cache notice, not a
blocker — after which printing works normally.

One cosmetic note: Python's JSON writer expands single-value arrays like
`["0"]` onto three lines instead of keeping them inline like the original
file does. This is valid JSON and doesn't affect parsing by anything — it
just means a diff against the old file will show reformatting on every
line, not just the new entry, if you ever compare them directly.

## Files in this repo

- `qidi_filament_admin.py` — the entire tool; stdlib-only, no pip installs
  needed. Deployed as-is to the printer.
- `qidi-filament-admin.service` — the systemd unit that runs it as root
  (needed for `systemctl restart` and to preserve file ownership on write).
