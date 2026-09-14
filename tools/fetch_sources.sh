#!/usr/bin/env bash
# Fetch the primary sources and verify them byte-for-byte against the pinned digests.
# The PDFs are Espressif's own documents, so they are fetched, not committed.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p sources

fetch() {  # name url sha256
  local name="$1" url="$2" want="$3" out="sources/$1"
  if [ -f "$out" ] && echo "$want  $out" | sha256sum -c --quiet 2>/dev/null; then
    echo "ok (cached)  $name"; return 0
  fi
  curl -fsSL -A "Mozilla/5.0" -o "$out" "$url"
  got=$(sha256sum "$out" | cut -d" " -f1)
  if [ "$got" != "$want" ]; then
    echo "FAIL  $name: sha256 $got != pinned $want (Espressif re-released the document?)" >&2
    return 1
  fi
  echo "ok (fetched) $name"
}

# documentation.espressif.com serves the current revision of each document; a 200 with a 13 KB body is its
# SPA shell, not a PDF -- the digest check catches that case here.
fetch esp32-s3_technical_reference_manual_en.pdf \
  "https://documentation.espressif.com/esp32-s3_technical_reference_manual_en.pdf" \
  4484bf8a69035ec42a731c58c64ada6fbd1f1618c5559409f134d9ea083f444f
fetch esp32-s3_datasheet_en.pdf \
  "https://documentation.espressif.com/esp32-s3_datasheet_en.pdf" \
  2d5a7cb7fd559d8d972bd88db32669c0196d23f22d7afaafb0f63d099b589a3f

echo "-> sources/ ready"
