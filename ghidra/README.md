# Ghidra annotations

Share the analysis without sharing Epson's firmware.

A Ghidra project (`.gpr` + `.rep/`) stores a compressed copy of every imported binary, so publishing one is effectively redistributing the firmware. This repo doesn't ship Ghidra projects. It ships `annotations.py` and JSON files of annotations (function names, labels, comments). The JSON holds names, comments and addresses only, no firmware bytes. Rebuild the project from your own copy of the firmware, then apply the annotations.

| File | Purpose |
|------|---------|
| [`annotations.py`](annotations.py) | `export` the open program's annotations to JSON, or `apply` a JSON file to it |
| [`../annotations/rtos_core.json`](../annotations/rtos_core.json) | Annotations for `rtos_core.bin` (43 functions, 16 comments) |

`annotations.py` talks to Ghidra through the [GhidraMCP](https://github.com/bethington/ghidra-mcp) plugin's HTTP API (default `http://localhost:8089`), so it needs that plugin running, but it needs no Ghidra scripting and nothing beyond Python 3's standard library. Override the address with `--url` or `GHIDRA_MCP_URL`. Script execution (`GHIDRA_MCP_ALLOW_SCRIPTS`) does not need to be enabled.

## Applying the annotations

1. **Get the firmware and extract it.** See [Extracting the Firmware](../README.md#extracting-the-firmware). You need `rcx_output/rtos_core.bin`.
2. **Import it into a new Ghidra project.**
   - File > Import File > `rtos_core.bin`
   - Format: **Raw Binary**
   - Language: **ARM:LE:32:v6** (ARM, little endian, 32-bit, v6)
   - Options: base address **`0x0`**
3. **Run auto-analysis** (accept the defaults), so function boundaries exist before names are applied.
4. **Start the GhidraMCP plugin** with that program open, and check `curl localhost:8089/check_connection` reports it.
5. **Preview, then apply:**

```bash
python3 ghidra/annotations.py apply annotations/rtos_core.json --dry-run
python3 ghidra/annotations.py apply annotations/rtos_core.json
```

The dry run lists every rename, new function, label and comment it would make, and changes nothing. Re-running is safe: names and comments that already match are skipped.

Before changing anything, `apply` compares the open program with what the annotations were made on: language, base address, size, and the SHA-256 of the imported file when the script can read it. On a mismatch it stops, since the addresses would be wrong. `--force` overrides that.

## Updating the annotations

After naming functions or adding comments in your own project, with GhidraMCP running:

```bash
python3 ghidra/annotations.py export          # writes annotations/<program>.json
git diff annotations/
```

Review the diff and commit the JSON. Never commit the `.gpr`/`.rep/` project.

## What is and isn't covered

| Covered | Not covered |
|---------|-------------|
| Functions with user-chosen names | Auto-generated names (`FUN_...`, `DAT_...`, `thunk_...`) |
| Named data labels | Function signatures, parameter and local variable names |
| Plate, pre, eol, post and repeatable comments at function entry points and at named data labels | Comments elsewhere inside function bodies, because the bridge cannot list which addresses have comments |
| | Data types and structures, bookmarks, references |

Only `rtos_core.bin` has been annotated so far. The decompressed CROM streams and the Linux `nwsoc` binary are analysed in the docs but have no annotation files.

## Status

Tested against a live Ghidra session (Ghidra 12.2, GhidraMCP 7.0.0): `export`, `apply --dry-run`, and real writes for function renames, comments and labels. Each was applied, checked in Ghidra, then reverted, and a repeat apply made no changes. Creating a function that doesn't exist yet (`create_function`) is the one write path not exercised, since `rtos_core.bin` already has all 43 functions. If you apply to a fresh project and that path misbehaves, run auto-analysis first and report it.
