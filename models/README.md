# Laptop 3D models

The twin picks its geometry in this order (see `backend/app/services/geometry.py`):

| Kind | Label in the UI | When |
|---|---|---|
| `exact` / `matched` | `EXACT MODEL` / `MATCHED MODEL` | A mesh file for the detected model is listed in `models/manifest.json` |
| `profile` | `MODEL PROFILE` | A built-in physical profile exists for the detected model (`backend/app/services/device_profiles.py`) |
| `generic` | `GENERIC MODEL` | Neither: generic parametric laptop scaled to the EDID panel size; the UI shows the illustrative renders |

## Built-in model profiles

A profile is published reference data about a model: chassis size (mm), colour, keyboard features,
ports per side (rear → front), webcam shutter, and internal layout (fans, SODIMM slots, SSD form
factor, battery cells). The frontend builds a model-specific parametric 3D twin from it; live
values (load, temperature, charge, memory, disk, network) still come only from telemetry.
Populated memory slots come from the hardware inventory.

Included: **Lenovo ThinkPad L14 Gen 4** (machine types 21H1/21H2 Intel, 21H5/21H6 AMD).
Surface detail and internal placement are schematic, not a scan of the unit. To add a model,
append an entry to `_PROFILES` with the same keys, using the manufacturer's spec sheet.

## Using a real mesh of your laptop

1. Put a glTF binary here, e.g. `models/lenovo/thinkpad-l14-gen4.glb`. Only use models you have
   the right to use.
2. Create `models/manifest.json`:

```json
[
  {"manufacturer": "LENOVO", "model": "ThinkPad L14 Gen 4",
   "file": "lenovo/thinkpad-l14-gen4.glb", "exact": false, "attribution": "author / license"}
]
```

`manufacturer` and `model` must match what `GET /api/v1/device` reports (case-insensitive). Set
`"exact": true` only if the mesh is a faithful replica. Files are served from `/models/...`, and only
from inside this directory. `*.glb` / `*.gltf` files are git-ignored by default. A mesh takes
priority over the built-in profile.
