"""Resolve which 3D geometry represents the detected laptop - honestly.

If ``MODELS_DIR/manifest.json`` lists a model for the detected manufacturer/model, that file is
served and labelled ``EXACT`` (only if the manifest marks it exact) or ``MATCHED``. Otherwise, if a
built-in physical profile exists for the model (see ``device_profiles``), the frontend builds a
model-specific parametric twin from it, labelled ``MODEL PROFILE``. Otherwise it renders its
parametric generic laptop, labelled ``GENERIC MODEL``.

manifest.json::

    [{"manufacturer": "LENOVO", "model": "ThinkPad L14 Gen 4",
      "file": "lenovo/thinkpad-l14-gen4.glb", "exact": true, "attribution": "..."}]
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.services.device_profiles import find_profile, public_profile


class GeometryResolver:
    def __init__(self, models_dir: Path) -> None:
        self._dir = models_dir

    def _manifest(self) -> list[dict[str, Any]]:
        path = self._dir / "manifest.json"
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def _entry(self, manufacturer: str | None, model: str | None) -> dict[str, Any] | None:
        for entry in self._manifest():
            if (
                str(entry.get("manufacturer", "")).lower() == (manufacturer or "").lower()
                and str(entry.get("model", "")).lower() == (model or "").lower()
            ):
                return entry
        return None

    def _inside(self, rel: str | None) -> Path | None:
        if not rel:
            return None
        target = (self._dir / rel).resolve()
        return target if self._dir.resolve() in target.parents and target.is_file() else None

    def resolve(
        self, manufacturer: str | None, model: str | None, inventory: dict[str, Any]
    ) -> dict[str, Any]:
        profile = find_profile(manufacturer, model, inventory.get("model_number"))
        exposed = public_profile(profile) if profile else None
        detected = " ".join(x for x in (manufacturer, model) if x) or "Unknown laptop"
        panel = (inventory.get("display") or {}).get("panel_size")
        gpus = inventory.get("gpu") or []
        resolution = gpus[0].get("resolution") if gpus else None
        entry = self._entry(manufacturer, model) or {}
        photo = self._inside(entry.get("photo"))
        common = {
            "detected": detected,
            "panel": panel,
            "resolution": resolution,
            "profile": exposed,
            "photo_url": f"/models/{entry['photo']}?v={int(photo.stat().st_mtime)}" if photo else None,
            "photo_attribution": entry.get("photo_attribution") if photo else None,
        }
        mesh = self._inside(entry.get("file"))
        if mesh is not None:
            exact = bool(entry.get("exact"))
            return {
                "kind": "exact" if exact else "matched",
                "label": "EXACT MODEL" if exact else "MATCHED MODEL",
                "url": f"/models/{entry['file']}?v={int(mesh.stat().st_mtime)}",
                "attribution": entry.get("attribution"),
                **common,
            }
        if exposed is not None:
            return {
                "kind": "profile",
                "label": "MODEL PROFILE",
                "url": None,
                "note": f"Parametric twin built from the published specifications of the {exposed['name']}. "
                "Telemetry is real; surface and internal detail are schematic.",
                "attribution": exposed["source"],
                **common,
            }
        return {
            "kind": "generic",
            "label": "GENERIC MODEL",
            "url": None,
            "note": "No 3D model for this laptop exists in the local model directory. "
            "The geometry is a parametric "
            "generic laptop scaled to the detected display size; telemetry is real.",
            **common,
        }

    # ------------------------------------------------------------------ uploads (admin only)
    def save_asset(
        self,
        manufacturer: str,
        model: str,
        kind: str,
        data: bytes,
        ext: str,
        *,
        exact: bool,
        attribution: str | None,
    ) -> str:
        """Store an uploaded mesh (``kind='mesh'``) or photo for a model and update ``manifest.json``."""
        slug = re.sub(r"[^a-z0-9]+", "-", f"{manufacturer}-{model}".lower()).strip("-")[:80] or "device"
        rel = f"uploads/{slug}-{'model' if kind == 'mesh' else 'photo'}.{ext}"
        target = self._dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        manifest = self._manifest()
        entry = next(
            (
                e
                for e in manifest
                if str(e.get("manufacturer", "")).lower() == manufacturer.lower()
                and str(e.get("model", "")).lower() == model.lower()
            ),
            None,
        )
        if entry is None:
            entry = {"manufacturer": manufacturer, "model": model}
            manifest.append(entry)
        for old_key in ("file", "photo"):
            if (
                old_key == ("file" if kind == "mesh" else "photo")
                and entry.get(old_key)
                and entry[old_key] != rel
            ):
                old = self._inside(str(entry[old_key]))
                if old is not None and old.parent.name == "uploads":
                    old.unlink(missing_ok=True)
        if kind == "mesh":
            entry.update({"file": rel, "exact": exact, "attribution": attribution})
        else:
            entry.update({"photo": rel, "photo_attribution": attribution})
        self._write_manifest(manifest)
        return rel

    def remove_asset(self, manufacturer: str, model: str, kind: str) -> bool:
        manifest = self._manifest()
        key = "file" if kind == "mesh" else "photo"
        for entry in manifest:
            if (
                str(entry.get("manufacturer", "")).lower() == manufacturer.lower()
                and str(entry.get("model", "")).lower() == model.lower()
                and entry.get(key)
            ):
                path = self._inside(str(entry[key]))
                if path is not None and path.parent.name == "uploads":
                    path.unlink(missing_ok=True)
                entry.pop(key, None)
                entry.pop("exact" if kind == "mesh" else "photo_attribution", None)
                if kind == "mesh":
                    entry.pop("attribution", None)
                self._write_manifest([e for e in manifest if e.get("file") or e.get("photo")])
                return True
        return False

    def _write_manifest(self, manifest: list[dict[str, Any]]) -> None:
        path = self._dir / "manifest.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(path)
