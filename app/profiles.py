"""Region/plan tables and scan profile generation.

A LoRa receiver can only demodulate one (frequency, bandwidth, spreading
factor, coding rate, sync word) combination at a time.  "Scanning all LoRa
devices" therefore means cycling through a plan of the configurations that
real-world devices use, and using CAD to notice energy on channels whose
framing we cannot decode.  This module builds those plans.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable

# Meshtastic modem presets: name -> (spreading factor, bandwidth kHz, coding rate)
MESHTASTIC_PRESETS: dict[str, tuple[int, float, int]] = {
    "LongFast": (11, 250.0, 5),
    "LongSlow": (12, 125.0, 5),
    "VeryLongSlow": (12, 125.0, 8),
    "MediumSlow": (10, 250.0, 5),
    "MediumFast": (9, 250.0, 5),
    "ShortSlow": (8, 250.0, 5),
    "ShortFast": (7, 250.0, 5),
    "LongModerate": (11, 125.0, 5),
    "ShortTurbo": (7, 500.0, 5),
    "LongTurbo": (11, 500.0, 5),
    "MediumTurbo": (9, 500.0, 5),
}

# Default Meshtastic channel name (the primary channel is normally left at
# "LongFast" even when the modem preset is changed).
DEFAULT_CHANNEL_NAME = "LongFast"

REGIONS: dict[str, dict[str, Any]] = {
    "US": {
        "label": "United States (902–928)",
        "code": "US",
        "start": 902.0,
        "end": 928.0,
        "spacing": 0.0,
        "power": 30,
        "meshtastic": True,
    },
    "EU868": {
        "label": "Europe 868 (869.4–869.65)",
        "code": "EU_868",
        "start": 869.4,
        "end": 869.65,
        "spacing": 0.0,
        "power": 27,
        "meshtastic": True,
    },
    "EU433": {
        "label": "Europe 433 (433–434)",
        "code": "EU_433",
        "start": 433.0,
        "end": 434.0,
        "spacing": 0.0,
        "power": 10,
        "meshtastic": True,
    },
    "CN470": {
        "label": "China 470 (470–510)",
        "code": "CN",
        "start": 470.0,
        "end": 510.0,
        "spacing": 0.0,
        "power": 19,
        "meshtastic": True,
    },
    "JP920": {
        "label": "Japan 920 (920.5–923.5)",
        "code": "JP",
        "start": 920.5,
        "end": 923.5,
        "spacing": 0.0,
        "power": 13,
        "meshtastic": True,
    },
    "AU915": {
        "label": "Australia/NZ 915 (915–928)",
        "code": "ANZ",
        "start": 915.0,
        "end": 928.0,
        "spacing": 0.0,
        "power": 30,
        "meshtastic": True,
    },
    "IN865": {
        "label": "India 865 (865–867)",
        "code": "IN",
        "start": 865.0,
        "end": 867.0,
        "spacing": 0.0,
        "power": 30,
        "meshtastic": True,
    },
    "KR920": {
        "label": "Korea 920 (920–923)",
        "code": "KR",
        "start": 920.0,
        "end": 923.0,
        "spacing": 0.0,
        "power": 23,
        "meshtastic": True,
    },
    "RU864": {
        "label": "Russia 864 (868.7–869.2)",
        "code": "RU",
        "start": 868.7,
        "end": 869.2,
        "spacing": 0.0,
        "power": 20,
        "meshtastic": True,
    },
    "AS923": {
        "label": "Asia 923 (923–925)",
        "code": "AS923",
        "start": 923.0,
        "end": 925.0,
        "spacing": 0.0,
        "power": 16,
        "meshtastic": False,
    },
    "WORLD433": {
        "label": "Worldwide 433 ISM (433.05–434.79)",
        "code": "WORLD433",
        "start": 433.05,
        "end": 434.79,
        "spacing": 0.0,
        "power": 10,
        "meshtastic": False,
    },
}

REGION_ORDER = [
    "US",
    "EU868",
    "EU433",
    "CN470",
    "JP920",
    "AU915",
    "IN865",
    "KR920",
    "RU864",
    "AS923",
    "WORLD433",
]


def djb2(text: str) -> int:
    value = 5381
    for byte in text.encode("utf-8"):
        value = ((value << 5) + value + byte) & 0xFFFFFFFF
    return value


def meshtastic_frequency(
    region: str, bw_khz: float, channel_name: str = DEFAULT_CHANNEL_NAME
) -> float:
    info = REGIONS.get(region, REGIONS["US"])
    span = info["end"] - info["start"]
    num_channels = int(span // (info["spacing"] + bw_khz / 1000.0))
    if num_channels < 1:
        num_channels = 1
    slot = djb2(channel_name) % num_channels
    return round(info["start"] + bw_khz / 2000.0 + slot * (bw_khz / 1000.0), 4)


# LoRaWAN regional parameters (uplink / RX2).  These are the frequencies a
# passive scanner sees most often; the list is intentionally explicit.
LORAWAN_PLANS: dict[str, dict[str, Any]] = {
    "US": {
        "uplink125": [round(902.3 + 0.2 * n, 1) for n in range(8)]
        + [round(903.0 + 1.6 * n, 1) for n in range(8)],
        "uplink500": [round(903.0 + 1.6 * n, 1) for n in range(8)],
        "rx2": [923.3],
        "sf125": [7, 8, 9, 10],
        "sf500": [7, 8, 9, 10, 11, 12],
    },
    "EU868": {
        "uplink125": [868.1, 868.3, 868.5]
        + [round(867.1 + 0.2 * n, 1) for n in range(5)],
        "uplink500": [],
        "rx2": [869.525],
        "sf125": [7, 8, 9, 10, 11, 12],
        "sf500": [],
    },
    "AS923": {
        "uplink125": [923.2, 923.4],
        "uplink500": [],
        "rx2": [923.2],
        "sf125": [7, 8, 9, 10, 11, 12],
        "sf500": [],
    },
    "AU915": {
        "uplink125": [round(915.2 + 0.2 * n, 1) for n in range(8)],
        "uplink500": [round(915.9 + 1.6 * n, 1) for n in range(8)],
        "rx2": [923.3],
        "sf125": [7, 8, 9, 10, 11, 12],
        "sf500": [7, 8, 9, 10, 11, 12],
    },
    "CN470": {
        "uplink125": [round(470.3 + 0.2 * n, 1) for n in range(8)],
        "uplink500": [],
        "rx2": [505.3],
        "sf125": [7, 8, 9, 10, 11, 12],
        "sf500": [],
    },
    "IN865": {
        "uplink125": [865.0625, 865.4625, 865.985],
        "uplink500": [],
        "rx2": [866.55],
        "sf125": [7, 8, 9, 10, 11, 12],
        "sf500": [],
    },
    "KR920": {
        "uplink125": [920.9, 921.1, 921.3, 921.5, 921.7, 921.9],
        "uplink500": [],
        "rx2": [921.9],
        "sf125": [7, 8, 9, 10, 11, 12],
        "sf500": [],
    },
    "RU864": {
        "uplink125": [868.9, 869.1],
        "uplink500": [],
        "rx2": [869.1],
        "sf125": [7, 8, 9, 10, 11, 12],
        "sf500": [],
    },
    "WORLD433": {
        "uplink125": [433.175, 433.375, 433.575, 433.775, 433.975],
        "uplink500": [],
        "rx2": [434.665],
        "sf125": [7, 8, 9, 10, 11, 12],
        "sf500": [],
    },
}


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    protocol: str
    region: str
    frequency_mhz: float
    sf: int
    bw_khz: float
    cr: int
    sync_word: int
    preamble: int = 16
    preset: str = "custom"
    channel_name: str | None = None

    @property
    def frequency_hz(self) -> int:
        return int(round(self.frequency_mhz * 1_000_000))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["frequency_hz"] = self.frequency_hz
        return data


def _profile(
    protocol: str,
    region: str,
    frequency_mhz: float,
    sf: int,
    bw_khz: float,
    cr: int,
    sync_word: int,
    preamble: int,
    label: str,
    preset: str = "custom",
    channel_name: str | None = None,
) -> Profile:
    ident = (
        f"{protocol}:{region}:{frequency_mhz:.4f}:{sf}/{int(bw_khz)}/{cr}/"
        f"{sync_word:02x}/{preamble}"
    )
    return Profile(
        id=ident,
        label=label,
        protocol=protocol,
        region=region,
        frequency_mhz=round(frequency_mhz, 4),
        sf=int(sf),
        bw_khz=float(bw_khz),
        cr=int(cr),
        sync_word=int(sync_word),
        preamble=int(preamble),
        preset=preset,
        channel_name=channel_name,
    )


def _meshtastic_profiles(region: str, presets: Iterable[str]) -> list[Profile]:
    out = []
    for preset in presets:
        if preset not in MESHTASTIC_PRESETS:
            continue
        sf, bw, cr = MESHTASTIC_PRESETS[preset]
        frequency = meshtastic_frequency(region, bw, DEFAULT_CHANNEL_NAME)
        out.append(
            _profile(
                "meshtastic",
                region,
                frequency,
                sf,
                bw,
                cr,
                0x2B,
                16,
                f"Meshtastic {preset} · {frequency:.3f} MHz · SF{sf}/BW{int(bw)}",
                preset=preset,
                channel_name=DEFAULT_CHANNEL_NAME,
            )
        )
    return out


def _lorawan_profiles(region: str, sf125: list[int], sf500: list[int], sync: int = 0x34) -> list[Profile]:
    plan = LORAWAN_PLANS.get(region)
    if not plan:
        return []
    out = []
    for frequency in plan["uplink125"]:
        for sf in sf125:
            out.append(
                _profile(
                    "lorawan",
                    region,
                    frequency,
                    sf,
                    125.0,
                    5,
                    sync,
                    8,
                    f"LoRaWAN {frequency:.4f} MHz · SF{sf}/BW125",
                    preset="LoRaWAN",
                )
            )
    for frequency in plan["uplink500"]:
        for sf in sf500:
            out.append(
                _profile(
                    "lorawan",
                    region,
                    frequency,
                    sf,
                    500.0,
                    5,
                    sync,
                    8,
                    f"LoRaWAN {frequency:.4f} MHz · SF{sf}/BW500",
                    preset="LoRaWAN",
                )
            )
    for frequency in plan["rx2"]:
        out.append(
            _profile(
                "lorawan",
                region,
                frequency,
                12,
                125.0 if region not in ("US", "AU915") else 500.0,
                5,
                sync,
                8,
                f"LoRaWAN RX2 {frequency:.4f} MHz",
                preset="LoRaWAN-RX2",
            )
        )
    return out


def _raw_sweep(region: str, step_mhz: float, sfs: Iterable[int], sync: int = 0x12) -> list[Profile]:
    info = REGIONS.get(region, REGIONS["US"])
    out = []
    frequency = info["start"] + step_mhz / 2
    while frequency < info["end"]:
        for sf in sfs:
            out.append(
                _profile(
                    "raw",
                    region,
                    round(frequency, 4),
                    sf,
                    125.0,
                    5,
                    sync,
                    8,
                    f"Survey {frequency:.3f} MHz · SF{sf}/BW125",
                    preset="Survey",
                )
            )
        frequency += step_mhz
    return out


def _dedupe(profiles: Iterable[Profile]) -> list[Profile]:
    seen = set()
    out = []
    for profile in profiles:
        if profile.id in seen:
            continue
        seen.add(profile.id)
        out.append(profile)
    return out


def build_profiles(
    region: str = "US",
    mode: str = "quick",
    *,
    custom_frequency_mhz: float | None = None,
    custom_sf: int | None = None,
    custom_bw_khz: float | None = None,
    custom_cr: int | None = None,
    custom_sync_word: int | None = None,
    custom_preamble: int = 16,
    include_raw: bool = True,
) -> list[Profile]:
    region = region if region in REGIONS else "US"
    mode = mode if mode in ("quick", "standard", "deep", "survey", "custom") else "quick"

    if mode == "custom":
        frequency = custom_frequency_mhz or 906.875
        return [
            _profile(
                "custom",
                region,
                frequency,
                custom_sf or 11,
                custom_bw_khz or 250.0,
                custom_cr or 5,
                custom_sync_word if custom_sync_word is not None else 0x2B,
                custom_preamble,
                f"Custom {frequency:.4f} MHz · SF{custom_sf or 11}/BW{int(custom_bw_khz or 250)}",
            )
        ]

    if mode == "survey":
        info = REGIONS[region]
        step = 0.5 if region == "US" else 0.2
        out = []
        frequency = info["start"] + step / 2
        while frequency < info["end"]:
            for sf in (7, 9, 11, 12):
                for bw in (125.0, 250.0):
                    out.append(
                        _profile(
                            "raw",
                            region,
                            round(frequency, 4),
                            sf,
                            bw,
                            5,
                            0x12,
                            8,
                            f"CAD {frequency:.3f} MHz · SF{sf}/BW{int(bw)}",
                            preset="CAD survey",
                        )
                    )
            frequency += step
        return _dedupe(out)

    presets = ["LongFast", "LongSlow", "MediumFast", "ShortFast", "LongTurbo"]
    if mode in ("standard", "deep"):
        presets = list(MESHTASTIC_PRESETS)
    profiles: list[Profile] = []
    if REGIONS[region].get("meshtastic"):
        profiles.extend(_meshtastic_profiles(region, presets))

    plan = LORAWAN_PLANS.get(region, LORAWAN_PLANS["US"])
    if mode == "quick":
        # The eight 125 kHz uplinks at the three most common data rates, plus
        # the 500 kHz uplinks at SF8/SF10.  This is a ~30-45 second sweep.
        profiles.extend(_lorawan_profiles(region, [7, 9, 11], [8, 10]))
        if region == "US":
            profiles.extend(_lorawan_profiles(region, [], [8], sync=0x12))
    elif mode == "standard":
        profiles.extend(_lorawan_profiles(region, plan["sf125"], plan["sf500"]))
        profiles.extend(_lorawan_profiles(region, plan["sf125"], plan["sf500"], sync=0x12))
        profiles.extend(_raw_sweep(region, 0.8, [7, 9, 11]))
    else:  # deep
        profiles.extend(_lorawan_profiles(region, plan["sf125"], plan["sf500"]))
        profiles.extend(_lorawan_profiles(region, plan["sf125"], plan["sf500"], sync=0x12))
        profiles.extend(_raw_sweep(region, 0.2, [7, 8, 9, 10, 11, 12]))
        # Meshtastic channel-name variants (people do rename their channels).
        variants = ["LongFast", "LongSlow", "MediumFast", "ShortFast", "LongTurbo", "MediumSlow", "ShortSlow"]
        for preset in ("LongFast", "LongSlow", "MediumFast", "ShortFast"):
            sf, bw, cr = MESHTASTIC_PRESETS[preset]
            for name in variants:
                frequency = meshtastic_frequency(region, bw, name)
                profiles.append(
                    _profile(
                        "meshtastic",
                        region,
                        frequency,
                        sf,
                        bw,
                        cr,
                        0x2B,
                        16,
                        f"Meshtastic {preset} ({name}) · {frequency:.3f} MHz",
                        preset=preset,
                        channel_name=name,
                    )
                )

    if include_raw and mode == "quick":
        # A couple of common private-network frequencies are cheap to include.
        for frequency in (903.0, 906.875, 913.0):
            profiles.append(
                _profile(
                    "raw",
                    region,
                    frequency,
                    9,
                    125.0,
                    5,
                    0x12,
                    8,
                    f"Private LoRa {frequency:.3f} MHz · SF9/BW125",
                    preset="Private",
                )
            )
    return _dedupe(profiles)


def region_options() -> list[dict[str, Any]]:
    return [
        {"id": key, "label": REGIONS[key]["label"], "code": REGIONS[key]["code"]}
        for key in REGION_ORDER
    ]


def profile_to_dict(profile: Profile) -> dict[str, Any]:
    return profile.to_dict()
