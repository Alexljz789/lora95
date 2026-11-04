# Third-party notices

LoRa95 vendors the following UI assets so the application works offline on the
uConsole:

- **98.css** — https://github.com/jdan/98.css — MIT License.
  `app/static/vendor/98css/98.css`.
- **W95FA** — a free Windows 95 style bitmap font, distributed as
  `app/static/fonts/win95fa.woff2`.  It is included for the Windows 95 look;
  see the upstream font distribution for its terms.  If you redistribute this
  repository, keep this notice with the font.
- **Meshtastic protobuf definitions / protocol documentation** —
  https://github.com/meshtastic/protobufs and https://meshtastic.org — used for
  the enum tables and packet format.  Meshtastic is a registered trademark of
  Meshtastic LLC.  LoRa95 is not affiliated with Meshtastic.
- **LoRaWAN regional parameters** — the frequency plans follow the public
  LoRa Alliance regional parameters and common Meshtastic plans.  LoRaWAN is a
  trademark of the LoRa Alliance.

The SX126x command sequences were written from the Semtech SX1261/2 datasheet
and cross-checked against RadioLib (MIT) and the Meshtastic firmware (GPL-3.0).
No RadioLib or Meshtastic source code is copied into this repository; the
driver here is an independent Python implementation.
