"""The knowledge registry: what this server can answer from, and which route reaches it.

Two things used to be implicit, and drifted apart. *Where* an artifact lives (a checkout's ``data/``, or the
copy inside the wheel that ``uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp``
installs) and *which* surface serves it (a tool, a resource, or nothing at all). Both are one table now:

  ARTIFACTS   artifact -> layer -> the tool(s) and resource(s) that serve it -> its producer -> its gate
  resolve()   role -> the directory that answered, and by which of the three ways (env / wheel / checkout)

so a route cannot quietly lose its artifact and an artifact cannot quietly lose its route. The server builds
its tools from this table and reports it through ``knowledge_routes`` and ``esp32s3://registry``;
``tools/check_wheel.py`` reads the same table to gate the packaged build. A wheel that drops a data file is
exactly the failure this prevents: every tool still starts, and every tool answers from nothing.
"""
from __future__ import annotations

import glob
import json
import os
import shutil

# The package directory is the wheel-installed one under uvx, and the checkout's copy when the server is run
# as `python server/esp32s3_mcp.py`. REPO_ROOT is therefore meaningful in the second case only.
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(PACKAGE_DIR)

ENV_DATA = "ESP32S3_DATA_DIR"
ENV_CORPUS = "ESP32S3_CORPUS_DIR"
ENV_SOURCES = "ESP32S3_SOURCES_DIR"
ENV_SIBLING_DOC = "ESP32S3_POCKETJS_DOC"
ENV_CACHE = "ESP32S3_CACHE_DIR"

# Where the artifacts that are *not* redistributed are put when there is no checkout: the corpus and the
# PDFs are built/fetched on demand by `esp32s3-hw-mcp --fetch-corpus`, and land here so that a uvx user gets
# page search without setting a single environment variable.
CACHE_DIR = os.environ.get(ENV_CACHE, "").strip() or os.path.join(
    os.path.expanduser("~"), ".cache", "esp32s3-hw-mcp")

LAYERS = {
    1: "Primary source (Espressif PDF: TRM / Datasheet). Citable by printed page; the PDFs themselves are "
       "not redistributed here.",
    2: "Toolchain (the Espressif binutils that builds firmware). Answers what a mnemonic encodes to.",
    3: "This repository's own silicon (ESP32-S3): timings measured by experiments/pie-timing and semantics "
       "measured by examples/firmware.",
    4: "The sibling project cardputer-adv-pocketjs's own board and write-up, quoted with its revision, line "
       "range and verbatim lines; never merged with layer 3.",
}


def _dir_candidates(role: str, env_var: str, packaged: str, *rest: tuple[str, str]) -> dict:
    """First existing wins, and every candidate is reported so the choice can be audited.

    env   -- an explicit request: it wins over everything, and when it points nowhere the search trace
             below shows exactly that (a wrong path must be visible, not silently replaced)
    repo  -- the checkout this package was installed from, i.e. `python server/esp32s3_mcp.py`
    cache -- what `--fetch-corpus` built under ~/.cache/esp32s3-hw-mcp: the uvx user's route to the
             artifacts that are not redistributed (the PDFs, the page corpus)
    wheel -- <package>/data: what `uvx --from git+...` installed; the extracted knowledge lives here
    """
    cands: list[tuple[str, str]] = []
    env = os.environ.get(env_var, "").strip()
    if env:
        cands.append(("env", env))
    cands.append(("wheel", packaged))
    cands.extend(rest)
    searched = [{"source": s, "path": p, "exists": os.path.isdir(p)} for s, p in cands]
    for source, path in cands:
        if os.path.isdir(path):
            return {"role": role, "exists": True, "source": source, "path": path, "searched": searched}
    return {"role": role, "exists": False, "source": "missing", "path": cands[-1][1],
            "searched": searched}


def resolve_data() -> dict:
    return _dir_candidates("data", ENV_DATA, os.path.join(PACKAGE_DIR, "data"),
                           ("repo", os.path.join(REPO_ROOT, "data")))


def resolve_corpus() -> dict:
    return _dir_candidates("corpus", ENV_CORPUS, os.path.join(PACKAGE_DIR, "corpus"),
                           ("repo", os.path.join(REPO_ROOT, "corpus")),
                           ("cache", os.path.join(CACHE_DIR, "corpus")))


def resolve_sources() -> dict:
    return _dir_candidates("sources", ENV_SOURCES, os.path.join(PACKAGE_DIR, "sources"),
                           ("repo", os.path.join(REPO_ROOT, "sources")),
                           ("cache", os.path.join(CACHE_DIR, "sources")))


def resolve_sibling_doc() -> dict:
    """The sibling project's write-up that layer 4 quotes. A file, not a directory, and never shipped."""
    cands: list[tuple[str, str]] = []
    env = os.environ.get(ENV_SIBLING_DOC, "").strip()
    if env:
        cands.append(("env", env))
    cands.append(("repo_sibling", os.path.join(os.path.dirname(REPO_ROOT), "cardputer-adv-pocketjs",
                                               "docs", "pie-simd.md")))
    searched = [{"source": s, "path": p, "exists": os.path.exists(p)} for s, p in cands]
    for source, path in cands:
        if os.path.exists(path):
            return {"role": "sibling_doc", "exists": True, "source": source, "path": path,
                    "searched": searched}
    return {"role": "sibling_doc", "exists": False, "source": "missing", "path": cands[-1][1],
            "searched": searched}


def resolve_all() -> dict:
    return {"data": resolve_data(), "corpus": resolve_corpus(), "sources": resolve_sources(),
            "sibling_doc": resolve_sibling_doc()}


# ---------------------------------------------------------------------------------------------------------
# Counting. A count is what makes "the route reaches real knowledge" checkable instead of asserted: the
# registry reports how many records answered, and tools/check_wheel.py + tools/test_mcp_server.py compare
# those numbers with the documents' printed totals.
# ---------------------------------------------------------------------------------------------------------

def _rows(path: str, key: str | None = None) -> int:
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    obj = obj[key] if key else obj
    return len(obj)


def _jsonl_lines(path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def _cost_items(path: str) -> int:
    """The curated items of the layer-4 file, summed over its section index (the file is sectioned)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return sum(len(data.get(s["name"], [])) for s in data["section_index"])


def _pdfs(path: str) -> int:
    return len(glob.glob(os.path.join(path, "*.pdf")))


def toolchain_present() -> bool:
    """Whether the Espressif assembler is reachable (PATH, XTENSA_TOOLCHAIN_DIR, or a managed install).

    A light check on purpose: the authoritative answer is `toolchain_status` from the server, which runs the
    binary and reports its version. This one only decides whether the registry says the route is live.
    """
    prefix = os.environ.get("XTENSA_TOOL_PREFIX", "xtensa-esp32s3-elf-")
    if shutil.which(prefix + "as"):
        return True
    patterns = [os.environ.get("XTENSA_TOOLCHAIN_DIR", ""),
                os.path.expanduser("~/.espressif/tools/xtensa-esp-elf/*/xtensa-esp-elf/bin"),
                "/opt/esp-idf/tools/xtensa-esp-elf/*/xtensa-esp-elf/bin"]
    return any(glob.glob(os.path.join(p, prefix + "as")) for p in patterns if p)


# ---------------------------------------------------------------------------------------------------------
# The registry. `presenters` is the route: the tools and resources a caller reaches this artifact through.
# A tool that reads two artifacts (analyze_sequence: the table's stages and this repository's measured
# stages) appears under both -- that is the point of keeping the route here rather than in the tool list.
# ---------------------------------------------------------------------------------------------------------

ARTIFACTS: list[dict] = [
    {
        "id": "registry",
        "route_kind": "index",
        "layer": None,
        "root": None,
        "file": None,
        "what": "This table: every artifact, its layer and the route that serves it, resolved at runtime.",
        "unit": None,
        "count": None,
        "presenters": {"tools": ["knowledge_routes"], "resources": ["esp32s3://registry"]},
        "producer": "esp32s3_hw_mcp/registry.py",
        "gate": "tools/test_mcp_server.py (every advertised tool and resource must appear here, and every "
                "entry here must exist)",
        "in_wheel": True,
        "required": True,
        "hint": "",
    },
    {
        "id": "registers",
        "route_kind": "extracted",
        "layer": 1,
        "root": "data",
        "file": "registers.json",
        "what": "TRM Register Summary tables (ch.2-39, 41): name, description, offset, access kind, group, "
                "section, printed page. 1581 registers.",
        "unit": "registers",
        "count": lambda p: _rows(p),
        "presenters": {"tools": ["get_register", "list_registers"], "resources": []},
        "producer": "tools/extract_registers.py",
        "gate": "tools/verify_registers.py; CI re-extracts and requires `git diff --exit-code data/`",
        "in_wheel": True,
        "required": True,
        "hint": "",
    },
    {
        "id": "pie_instructions",
        "route_kind": "extracted",
        "layer": 1,
        "root": "data",
        "file": "pie_instructions.json",
        "what": "TRM 1.8 per-instruction specs (p76-303): encoding diagram as bit fields, assembler syntax, "
                "description, operation pseudo-code. 220 instructions.",
        "unit": "instructions",
        "count": lambda p: _rows(p),
        "presenters": {"tools": ["get_instruction", "instruction_encoding"], "resources": []},
        "producer": "tools/extract_pie.py",
        "gate": "tools/verify_pie.py; tools/asm_toolchain.py --check-all (220 instructions vs the assembler)",
        "in_wheel": True,
        "required": True,
        "hint": "",
    },
    {
        "id": "pie_pipeline",
        "route_kind": "extracted",
        "layer": 1,
        "root": "data",
        "file": "pie_pipeline.json",
        "what": "TRM Table 1.7-2 (p66-74): the pipeline stage at which each instruction uses and defines its "
                "operands and special registers. 217 rows. The core data: stalls are computed from it.",
        "unit": "table rows",
        "count": lambda p: _rows(p),
        "presenters": {"tools": ["instruction_pipeline", "analyze_sequence"], "resources": []},
        "producer": "tools/extract_pie.py",
        "gate": "tools/verify_pie.py (stage numbers cross-read from Table 1.7-1)",
        "in_wheel": True,
        "required": True,
        "hint": "",
    },
    {
        "id": "pie_hazards",
        "route_kind": "extracted",
        "layer": 1,
        "root": "data",
        "file": "pie_hazards.md",
        "what": "TRM 1.7.1-1.7.3 (p65-75) verbatim with page markers: data hazards, hardware resource "
                "hazards, control hazards.",
        "unit": None,
        "count": None,
        "presenters": {"tools": [], "resources": ["esp32s3://trm/pie-hazards"]},
        "producer": "tools/extract_pie.py",
        "gate": "tools/verify_pie.py",
        "in_wheel": True,
        "required": True,
        "hint": "",
    },
    {
        "id": "pie_review",
        "route_kind": "extracted",
        "layer": 1,
        "root": "data",
        "file": "pie_review.json",
        "what": "Rows where the manual disagrees with itself (extracted value vs the manual's own cross "
                "reference). Kept as a review file: a document that contradicts itself is a finding about "
                "the document, not a licence to guess.",
        "unit": "rows",
        "count": lambda p: _rows(p),
        "presenters": {"tools": [], "resources": ["esp32s3://trm/review"]},
        "producer": "tools/verify_pie.py",
        "gate": "tools/verify_pie.py (regenerates it)",
        "in_wheel": True,
        "required": True,
        "hint": "",
    },
    {
        "id": "peripheral_map",
        "route_kind": "extracted",
        "layer": 1,
        "root": "data",
        "file": "peripheral_map.json",
        "what": "TRM Table 4.3-3 (p408-409): peripheral/module name, low and high boundary address, size. "
                "The basis for turning a register offset into an absolute address (as a heuristic, always "
                "labelled as one).",
        "unit": "rows",
        "count": lambda p: _rows(p),
        "presenters": {"tools": ["list_peripherals", "get_register"], "resources": []},
        "producer": "tools/extract_registers.py",
        "gate": "tools/verify_registers.py",
        "in_wheel": True,
        "required": True,
        "hint": "",
    },
    {
        "id": "pie_timing_measured",
        "route_kind": "measured",
        "layer": 3,
        "root": "data",
        "file": "pie_timing_measured.json",
        "what": "Stalls and pipeline stages measured on this repository's own ESP32-S3 (experiments/"
                "pie-timing): the anchors that gate the run, the individual timed cases, the staging "
                "Table 1.7-2 omits, and the predicted-versus-measured pairs. Derived values are only "
                "offered when the anchors all matched.",
        "unit": "timed cases",
        "count": lambda p: _rows(p, "measurements"),
        "presenters": {"tools": ["measured_timing", "instruction_pipeline", "analyze_sequence"],
                       "resources": []},
        "producer": "experiments/pie-timing (tools/device_experiment.sh, tools/parse_pie_timing.py)",
        "gate": "the file's own validity gate (anchors) + tools/selftest_pie_timing_parser.py",
        "in_wheel": True,
        "required": True,
        "hint": "",
    },
    {
        "id": "pie_examples_measured",
        "route_kind": "measured",
        "layer": 3,
        "root": "data",
        "file": "pie_examples_measured.json",
        "what": "What the instructions actually *mean*, measured with the examples/ kernels: lane order, "
                "saturation, address increments, the 128-bit access rounding -- each kernel checked three "
                "ways (device, C in firmware, Python on the host).",
        "unit": "findings",
        "count": lambda p: _rows(p, "findings"),
        "presenters": {"tools": ["example_measured_semantics"], "resources": []},
        "producer": "examples/firmware (tools/build_examples.sh, tools/host_flash_and_log.sh --examples, "
                    "tools/check_examples_log.py)",
        "gate": "tools/selftest_examples_checker.py (a reference log passes, every mutation is caught)",
        "in_wheel": True,
        "required": True,
        "hint": "",
    },
    {
        "id": "pie_encoding_errata",
        "route_kind": "measured",
        "layer": 2,
        "root": "data",
        "file": "pie_encoding_errata.json",
        "what": "The instructions where the manual's printed diagram and the Espressif assembler disagree, "
                "with the evidence for each. 6 of 220.",
        "unit": "instructions",
        "count": lambda p: _rows(p, "instructions"),
        "presenters": {"tools": ["manual_errata"], "resources": []},
        "producer": "tools/asm_toolchain.py --errata",
        "gate": "tools/selftest_asm_toolchain.py (skips itself where no binutils is installed)",
        "in_wheel": True,
        "required": True,
        "hint": "",
    },
    {
        "id": "pie_measured_costs",
        "route_kind": "measured",
        "layer": 4,
        "root": "data",
        "file": "pie_measured_costs.json",
        "what": "What a PIE kernel really costs and what breaks while writing one (cost model, scaffold, "
                "scalar routines, pitfalls, measurement discipline, disagreements), measured on the SIBLING "
                "project cardputer-adv-pocketjs's own board. Curated with every quote's line range.",
        "unit": "curated items",
        "count": lambda p: _cost_items(p),
        "presenters": {"tools": ["measured_costs", "pie_cost_estimate"],
                       "resources": ["esp32s3://pocketjs/pie-costs"]},
        "producer": "curated from cardputer-adv-pocketjs docs/pie-simd.md",
        "gate": "tools/verify_measured_costs.py (every quote, number, link and recorded search re-checked "
                "against the document when it is checked out)",
        "in_wheel": True,
        "required": True,
        "hint": "",
    },
    {
        "id": "corpus_trm",
        "route_kind": "local_optional",
        "layer": 1,
        "root": "corpus",
        "file": os.path.join("trm-s3", "pages.jsonl"),
        "what": "The TRM's page text, page by page, with the outline. Not shipped (the manual is Espressif's "
                "and is not redistributed here): without it, search_manual and get_page say so instead of "
                "answering.",
        "unit": "pages",
        "count": lambda p: _jsonl_lines(p),
        "presenters": {"tools": ["search_manual", "get_page"], "resources": []},
        "producer": "tools/build_corpus.py sources/esp32-s3_technical_reference_manual_en.pdf --out "
                    "corpus/trm-s3",
        "gate": "build_corpus.py checks the printed page label equals the PDF page, and that no page "
                "extracts empty",
        "in_wheel": False,
        "required": False,
        "hint": "`esp32s3-hw-mcp --fetch-corpus` fetches the PDFs (sha256-checked), builds the corpus under "
                "~/.cache/esp32s3-hw-mcp and the server finds it there -- add `--with pymupdf` to the uvx "
                "command for the one run that builds it. From a checkout instead: `bash "
                "tools/fetch_sources.sh` then `.venv/bin/python tools/build_corpus.py "
                "sources/esp32-s3_technical_reference_manual_en.pdf --out corpus/trm-s3`. "
                "ESP32S3_CORPUS_DIR overrides the search either way.",
    },
    {
        "id": "documents",
        "route_kind": "source_document",
        "layer": 1,
        "root": "sources",
        "file": None,
        "artifact_is_root": True,
        "what": "The PDFs every extracted fact cites: TRM v1.8 (1531 pp) and Datasheet v2.2 (87 pp), pinned "
                "by sha256. Fetched, never redistributed.",
        "unit": "PDFs",
        "count": lambda p: _pdfs(p),
        "presenters": {"tools": [], "resources": ["esp32s3://docs/sources"]},
        "producer": "tools/fetch_sources.sh",
        "gate": "sha256 per file, re-verified on every CI run; regeneration must be byte-identical",
        "in_wheel": False,
        "required": False,
        "hint": "`bash tools/fetch_sources.sh` (from a checkout; about 16 MB).",
    },
    {
        "id": "sibling_document",
        "route_kind": "source_document",
        "layer": 4,
        "root": "sibling_doc",
        "file": None,
        "artifact_is_root": True,
        "what": "cardputer-adv-pocketjs's docs/pie-simd.md -- the document layer 4's data file quotes. Not "
                "read at runtime (the quotes are already in pie_measured_costs.json); it is what the layer-4 "
                "gate re-checks those quotes against.",
        "unit": None,
        "count": None,
        "presenters": {"tools": [], "resources": []},
        "producer": "another repository (dj-oyu/cardputer-adv-pocketjs)",
        "gate": "tools/verify_measured_costs.py, given ESP32S3_POCKETJS_DOC",
        "in_wheel": False,
        "required": False,
        "hint": "Set ESP32S3_POCKETJS_DOC to the file (CI checks that repository out next to this one).",
    },
    {
        "id": "toolchain",
        "route_kind": "external_tool",
        "layer": 2,
        "root": None,
        "file": None,
        "what": "The Espressif binutils (xtensa-esp32s3-elf-as/objdump) that build the firmware, driven by "
                "the bridge in esp32s3_hw_mcp/asm_toolchain.py. Not a data file and not installed by this "
                "package: encoding tools report it as missing rather than guessing.",
        "unit": None,
        "count": None,
        "presenters": {"tools": ["check_asm", "decode_instruction", "instruction_encoding",
                                 "toolchain_status"], "resources": []},
        "producer": "ESP-IDF (`source /opt/esp-idf/export.sh`, or a managed install under ~/.espressif)",
        "gate": "tools/selftest_asm_toolchain.py, and check_asm compares a claimed word with the emitted one",
        "in_wheel": True,
        "required": False,
        "hint": "Optional: without it, 4 of the tools answer toolchain_missing and the rest keep working.",
    },
]


def artifact(id_: str) -> dict:
    for a in ARTIFACTS:
        if a["id"] == id_:
            return a
    raise KeyError(id_)


def _path_of(a: dict, roots: dict) -> str | None:
    if not a["root"] or not a["file"]:
        return None
    root = roots[a["root"]]
    return os.path.join(root["path"], a["file"]) if root["path"] else None


def describe(*, with_counts: bool = True) -> dict:
    """The registry as a payload: where each root resolved, and per artifact: present, size, count, route.

    A count is measured by opening the artifact, never by trusting a number in this file -- a registry that
    reports "1581 registers" while the file says something else is the bug this exists to catch.
    """
    roots = resolve_all()
    out, missing_required = [], []
    tools: set[str] = set()
    resources: set[str] = set()
    for a in ARTIFACTS:
        path = _path_of(a, roots)
        if a["route_kind"] == "index":
            # The registry is not a file: it is this table. It is always reachable, which is the point --
            # it is how a caller finds out what else is.
            present, path = True, None
        elif a.get("artifact_is_root") and a["root"]:
            # The artifact *is* the root: a directory of PDFs, or one file in another repository.
            path = roots[a["root"]]["path"]
            present = bool(path) and os.path.exists(path) and (
                not os.path.isdir(path) or bool(os.listdir(path)))
        elif a["route_kind"] == "external_tool":
            present = toolchain_present()
        else:
            present = bool(path) and os.path.exists(path)
        entry = {
            "id": a["id"],
            "route_kind": a["route_kind"],
            "layer": a["layer"],
            "layer_note": LAYERS.get(a["layer"]) if a["layer"] else None,
            "file": path or ("Espressif binutils on PATH or under ~/.espressif"
                             if a["route_kind"] == "external_tool" else None),
            "what": a["what"],
            "present": present,
            "shipped_in_wheel": a["in_wheel"],
            "required": a["required"],
            "served_by": {"tools": a["presenters"]["tools"], "resources": a["presenters"]["resources"]},
            "producer": a["producer"],
            "gate": a["gate"],
        }
        if present and path and os.path.isfile(path):
            entry["bytes"] = os.path.getsize(path)
            if with_counts and a["count"]:
                try:
                    entry["records"] = a["count"](path)
                    entry["records_unit"] = a["unit"]
                except Exception as exc:                      # a present file that will not parse
                    entry["records_error"] = f"{type(exc).__name__}: {exc}"
        if a["hint"]:
            entry["hint"] = a["hint"]
        if a["required"] and not present:
            missing_required.append(a["id"])
        tools.update(a["presenters"]["tools"])
        resources.update(a["presenters"]["resources"])
        out.append(entry)
    return {
        "roots": {role: {"path": r["path"], "source": r["source"], "exists": r["exists"],
                         "how": {"env": "set by the environment",
                                 "wheel": "inside the installed package (what uvx installs)",
                                 "repo": "the checkout this package was installed from",
                                 "cache": f"built on demand by `--fetch-corpus` ({CACHE_DIR})",
                                 "missing": "nothing found -- see `searched`"}.get(r["source"], r["source"]),
                         "searched": r["searched"]} for role, r in roots.items()},
        "layers": LAYERS,
        "artifacts": out,
        "summary": {"artifacts": len(out), "present": sum(1 for e in out if e["present"]),
                    "shipped_in_wheel": sum(1 for e in out if e["shipped_in_wheel"]),
                    "missing_required": missing_required,
                    "tools_reached": sorted(tools), "resources_reached": sorted(resources)},
        "how_to_read": "This is the route index, not the knowledge. `served_by` names the MCP surface that "
                       "reaches each artifact: call that tool (or read that resource) and you get the facts "
                       "with their citation. `present: false` on a shipped artifact means the install is "
                       "broken; on a local_optional/source_document artifact it is expected under uvx (the "
                       "PDFs and the corpus are not redistributed) -- `hint` says how to obtain it. Every "
                       "number in `records` was counted by opening the file just now.",
    }
