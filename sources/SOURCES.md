# Primary sources (Espressif official only)

Fetched by `tools/fetch_sources.sh` (sha256-pinned) and deliberately **not committed**: these are
Espressif's documents, re-fetched rather than redistributed.

| file | document | version | pages | sha256 |
|---|---|---|---|---|
| `esp32-s3_technical_reference_manual_en.pdf` | ESP32-S3 Technical Reference Manual | v1.8 (2026-03-04) | 1531 | `4484bf8a69035ec42a731c58c64ada6fbd1f1618c5559409f134d9ea083f444f` |
| `esp32-s3_datasheet_en.pdf` | ESP32-S3 Series Datasheet | v2.2 (2026-03-05) | 87 | `2d5a7cb7fd559d8d972bd88db32669c0196d23f22d7afaafb0f63d099b589a3f` |

Both: printed page label == PDF page (checked at corpus build), no near-empty pages, so a citation can use
the page number as-is.

## Hosts and their failure modes

- `documentation.espressif.com/<name>.pdf` serves the current revision. **It answers HTTP 200 with a ~13 KB
  HTML shell for URLs that do not exist** (`xtensa_isa_en.pdf`, `esp32-s3_errata_en.pdf`, ... all did), so
  a status code proves nothing. Check the digest/size, as `tools/fetch_sources.sh` does.
- `www.espressif.com/sites/default/files/documentation/*.pdf` still serves the older mirror paths.
- The catalogue page (`/en/support/documents/technical-documents`) is JS-driven; the `keys=` query
  parameter is ignored, so filtering by chip needs the browser, not curl.

## Documents that are NOT available as PDF (as of M0)

- **Xtensa ISA reference manual** — Espressif does not distribute it as a PDF. The base Xtensa ISA is
  therefore not a usable primary source here; only what the TRM states (Ch.1 §1.7 pipeline stages and
  hazards) may be quoted.
- **Errata / chip revision / hardware design guidelines** — served as HTML on the documentation platform
  now. Not collected yet (would enter as tier-2 "official HTML", clearly distinguished from PDF tier-1).
