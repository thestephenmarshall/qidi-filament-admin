# Installing qidi-filament-admin

1. Copy the script to the printer:

   `scp qidi_filament_admin.py qidi@$printer_ip:/home/qidi/qidi_filament_admin.py`

2. Copy the systemd unit and install it (as root/sudo):
   <code>
    scp qidi-filament-admin.service qidi@$printer_ip:/tmp/
    ssh qidi@$printer_ip
    sudo mv /tmp/qidi-filament-admin.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now qidi-filament-admin.service
   </code>

3. Check it started cleanly:

   `sudo systemctl status qidi-filament-admin.service`
   # should show "active (running)"

4. Open in a browser on the same network:

   `http://$printer_ip:8899`

5. Fill in the form and submit. It will, in this order:
   - Read `officiall_filas_list.cfg` and every filament-*.json file under
     `/home/qidi/QIDI_Client/resource/filament_config/` (covers whichever
     nozzle sizes you have: 0.2/0.4/0.6/0.8 - it auto-discovers them,
     doesn't assume all four exist)
   - Check the display name doesn't already exist in ANY of those
     files. If it does, NOTHING is written and you get a clear error
     saying exactly where the collision is.
   - If clear: back up every file about to change (timestamped), then
     write all of them with the same filament_id shared across every
     JSON file (so the box/catalog ID stays consistent across nozzle
     sizes), then fill the first blank [filaN] slot in the cfg (or add
     a new numbered one if all 50 are used).
   - Restart qidi-client.service once, at the end, only if every
     write succeeded.

## Editing an existing filament

The index page (`http://$printer_ip:8899/`) now shows an "Edit an
existing custom filament" expander listing anything you've added
through this tool, each linking to a pre-filled edit form.

Scope, deliberately: editing only covers filaments THIS TOOL added
(identified by their filament_id starting with QFZ), not QIDI's own
stock catalog. QIDI's stock entries use different display names
between officiall_filas_list.cfg and the filament-*.json files (e.g.
cfg has "PLA Rapido", json has "QIDI PLA Rapido" for the same
material) - they aren't the same key space for pre-existing data, only
for entries this tool adds (which it forces to match). Editing stock
entries safely would mean resolving that mismatch per-entry; this
wasn't built. A tool-added entry that's somehow become inconsistent
(present in some files but not others, or with mismatched IDs across
files) also won't be offered for editing - the listing and the edit
submission both re-check consistency live.

You can rename a filament while editing it (just change the Display
name field) - it moves the entry under the new key in every file and
keeps its original filament_id. Renaming to a name that already
exists anywhere is rejected with nothing written, same guarantee as
adding a new duplicate.

## Deleting a filament

The edit page (reached via the index page's "Edit an existing custom
filament" list) has a red "Delete this filament" button below the
save button, with a JS confirm() prompt before it submits.

Same scope as editing: only tool-added (QFZ*) entries, consistency
re-checked live at submit time, not trusted from the page load.

What deleting actually does:
- Each filament-*.json file: the key is removed from the dict outright.
- officiall_filas_list.cfg: the [filaN] slot is reset to blank (every
  field empty) - matching the exact format QIDI's own file already
  uses for its unused slots (fila9, fila16, etc. in your original
  file) - rather than removing the section. This makes the slot a
  normal, immediately reusable blank slot for the next filament you
  add, confirmed by testing that a subsequent add reuses the freed
  slot number.

Every file is backed up before the delete touches it, same as adding
or editing. There's no in-UI undo - restore from the relevant backup
file if you delete the wrong thing.

## Fields

- Display name: must be unique across all files. This is the string
  you'll also use/match in QIDI Studio's custom filament dialog.
- Vendor: free text - NOT restricted to Generic/QIDI (verified against
  your real filament-0.4.json, which already has Bambu, HATCHBOX,
  Overture, Polymaker, Sunlu as vendors).
- Type: free text, doesn't have to match an existing type string.
- Min/max nozzle temp, box min/max temp: as in QIDI Studio.
- Chamber temperature: printing chamber target (not drying-box temp).
  Defaults to 0 if left blank.
- Support material checkbox: sets filament_is_support.

The box-drying-profile fields in each JSON entry (filament_dev_*) are
now exposed under an "Advanced drying settings" collapsible section,
optional and pre-filled with the boilerplate defaults (every existing
entry in your real filament-0.4.json had byte-identical values for
them regardless of material - leave them blank to match that).
Accepts either one value (replicated across all 4 box slots where the
field has 4) or an explicit comma-separated value per slot, e.g.
"55,60,65,70". A malformed count is rejected before anything is
written - same nothing-written-on-error guarantee as a duplicate name.

`filament_dev_ams_drying_ams_limitations` is the one exception: every
entry has the identical ["1","0"] pair including support materials,
and its meaning isn't established from the data, so it's left fixed
rather than exposing a guess as an editable field.

