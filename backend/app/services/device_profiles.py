"""Built-in physical profiles of specific laptop models.

A profile describes the *physical* design of a model - chassis size, colour, port layout and
internal layout - taken from the manufacturer's published specifications. The frontend builds a
model-specific parametric 3D twin from it. It is reference data about the model, never telemetry:
every live value still comes from the agent.

Profiles are matched on the identity the agent reports (SMBIOS manufacturer + model, or the
machine-type prefix of the model number).
"""

from __future__ import annotations

import re
from typing import Any

# Port kinds understood by the frontend renderer.
#   usb_c, usb_c_tb, usb_a, hdmi, rj45, audio, lock, smartcard, power_led
_PROFILES: list[dict[str, Any]] = [
    {
        "id": "lenovo-thinkpad-l14-gen4",
        "name": "Lenovo ThinkPad L14 Gen 4",
        "match": {
            "manufacturer": "lenovo",
            "model": r"thinkpad\s+l14\s+gen\s*4",
            # Machine types: 21H1/21H2 (Intel), 21H5/21H6 (AMD).
            "model_number_prefixes": ["21H1", "21H2", "21H5", "21H6"],
        },
        "source": "Lenovo PSREF - ThinkPad L14 Gen 4 (published product specifications)",
        "chassis_mm": {"width": 324.5, "depth": 225.8, "height": 19.7},
        "lid_mm": 6.4,
        "colour": {"name": "Thunder Black", "hex": "#1c1d20"},
        "display": {
            "diagonal_in": 14.0,
            "aspect": "16:9",
            "bezel_mm": {"side": 7.5, "top": 10.5, "bottom": 22.0},
        },
        "keyboard": {
            "layout": "thinkpad-14",
            "trackpoint": True,
            "trackpad_buttons": 3,
            "numpad": False,
            "fingerprint": "power_button",
        },
        "webcam": {"privacy_shutter": True},
        "ports": {
            # Listed rear -> front along each side.
            "left": [
                {"kind": "usb_c_tb", "label": "USB-C (Thunderbolt 4 / USB4 40Gbps, power in)"},
                {"kind": "usb_c", "label": "USB-C (USB 3.2 Gen 2)"},
                {"kind": "usb_a", "label": "USB-A (USB 3.2 Gen 1)"},
                {"kind": "hdmi", "label": "HDMI"},
                {"kind": "audio", "label": "Headphone / mic combo jack"},
            ],
            "right": [
                {"kind": "lock", "label": "Kensington Nano Security Slot"},
                {"kind": "rj45", "label": "Ethernet RJ45"},
                {"kind": "usb_a", "label": "USB-A (USB 3.2 Gen 1, powered)"},
            ],
        },
        "internals": {
            "fans": 1,
            "heat_pipes": 1,
            "sodimm_slots": 2,
            "ssd": "M.2 2280 NVMe",
            "wlan": "M.2 2230",
            "battery_cells": 3,
            "speakers": 2,
        },
        "accuracy": "Chassis dimensions, colour, port layout and slot counts follow the published "
        "specifications. Surface detail and internal placement are a schematic approximation, "
        "not a scan of this unit.",
    },
]


def find_profile(
    manufacturer: str | None, model: str | None, model_number: str | None
) -> dict[str, Any] | None:
    man = (manufacturer or "").strip().lower()
    mod = (model or "").strip().lower()
    num = (model_number or "").strip().upper()
    for profile in _PROFILES:
        m = profile["match"]
        if man and m["manufacturer"] not in man:
            continue
        if mod and re.search(m["model"], mod):
            return profile
        if num and any(num.startswith(p) for p in m["model_number_prefixes"]):
            return profile
    return None


def public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Profile as exposed over the API (match rules are internal)."""
    return {k: v for k, v in profile.items() if k != "match"}
