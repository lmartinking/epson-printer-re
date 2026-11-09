#!/usr/bin/env python3
"""Extract every documented component from an Epson XP-442 firmware update.

Reproduces the layout described in FIRMWARE_LAYOUT.md end-to-end. Given a downloaded
`.efu` (Epson updater package) or the `.rcx` inside it, this drops:

    <out>/<name>.rcx                       (only if input was .efu)
    <out>/rcx_output/rcx_header
    <out>/rcx_output/ipl_0_{hdr,seg0,seg1}
    <out>/rcx_output/ipl_1_{hdr,seg0,seg1}
    <out>/rcx_output/rtos_core.bin                 (ARM1136 RTOS, isolated from Linux)
    <out>/rcx_output/block1_streamA_web.bin        (WebConfig UI, decompressed)
    <out>/rcx_output/block1_streamB_panel.bin      (front-panel UI)
    <out>/rcx_output/block1_streamC_gfx.bin        (icons/fonts)
    <out>/squashfs_rootfs1/                        (rootfs, unpacked)
    <out>/squashfs_rootfs2/                        (network module, unpacked)

Dependencies:
    - python3 (stdlib only for this script; uncrom.py wants tqdm — auto-shimmed)
    - unsquashfs (SquashFS 4.x) on PATH
    - The abrasive/epson-reversing toolkit reachable as ./epson-reversing/
      (rcx_unpack + uncrom.py). If missing, pass --toolkit /path.

See extract_firmware.sh for a wrapper that assumes the repo layout.
"""
from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# Block 0 (ipl_0_seg1) internal layout — confirmed in FIRMWARE_LAYOUT.md.
RTOS_CORE_END = 0x110400  # ARM1136 RTOS ends here; Linux uImage starts

SQUASHFS_OFFSETS = {
    "squashfs_rootfs1": 0x230000,  # rootfs (426 inodes, 1.1 MB xz)
    "squashfs_rootfs2": 0x370000,  # network module / nwsoc (35 inodes, 2.85 MB xz)
}

# Block 1 (ipl_1_seg1) CROM stream offsets — from FIRMWARE_LAYOUT.md's Block 1 table.
CROM_STREAMS = [
    ("block1_streamA_web.bin",   0x10),      # WebConfig admin UI + ARM code
    ("block1_streamB_panel.bin", 0x330000),  # front-panel UI app
    ("block1_streamC_gfx.bin",   0x530000),  # graphics/fonts
]

SQUASHFS_MAGIC = b"hsqs"


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def need_tool(name: str, hint: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"error: {name} not found on PATH.\n  {hint}")
    return path


def resolve_toolkit(explicit: str | None) -> Path:
    """Locate abrasive/epson-reversing toolkit (rcx_unpack + uncrom.py)."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path(__file__).resolve().parent / "epson-reversing")
    candidates.append(Path.cwd() / "epson-reversing")
    for c in candidates:
        if (c / "rcx_unpack").is_file() and (c / "uncrom.py").is_file():
            return c
    sys.exit(
        "error: epson-reversing toolkit not found.\n"
        "  Run: git submodule update --init epson-reversing\n"
        "  or pass --toolkit /path/to/epson-reversing"
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def unzip_efu(efu: Path, out_dir: Path) -> Path:
    """.efu is a ZIP containing one .rcx. Extract it and return its path."""
    with zipfile.ZipFile(efu) as z:
        rcx_names = [n for n in z.namelist() if n.lower().endswith(".rcx")]
        if not rcx_names:
            sys.exit(f"error: no .rcx inside {efu}")
        if len(rcx_names) > 1:
            sys.exit(f"error: {efu} contains multiple .rcx files: {rcx_names}")
        (rcx_name,) = rcx_names
        z.extract(rcx_name, out_dir)
    return out_dir / rcx_name


def run_rcx_unpack(toolkit: Path, rcx: Path, out_dir: Path) -> Path:
    """Invoke the toolkit's rcx_unpack to split the .rcx into ipl blocks."""
    rcx_out = out_dir / "rcx_output"
    rcx_out.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["python3", str(toolkit / "rcx_unpack"), str(rcx), str(rcx_out)],
        check=True,
    )
    return rcx_out


def slice_file(src: Path, offset: int, dst: Path, size: int | None = None) -> int:
    """Byte-carve `size` bytes from `src` starting at `offset` into `dst`.

    If size is None, reads to EOF. Returns bytes written.
    """
    with open(src, "rb") as f, open(dst, "wb") as w:
        f.seek(offset)
        if size is None:
            data = f.read()
        else:
            data = f.read(size)
        w.write(data)
        return len(data)


def carve_crom_streams(toolkit: Path, ipl_1_seg1: Path, out_dir: Path) -> None:
    """Slice each CROM stream out of ipl_1_seg1 and run uncrom.py on it.

    uncrom.py wants tqdm; if it's not installed we drop a shim on PYTHONPATH.
    """
    env = os.environ.copy()
    tqdm_shim = None
    try:
        import tqdm  # noqa: F401
    except ImportError:
        tqdm_shim = tempfile.mkdtemp(prefix="uncrom_shim_")
        (Path(tqdm_shim) / "tqdm.py").write_text(
            "def tqdm(iterable=None, *a, **k):\n"
            "    return iterable if iterable is not None else iter([])\n"
        )
        env["PYTHONPATH"] = (
            tqdm_shim + os.pathsep + env.get("PYTHONPATH", "")
        )

    try:
        for name, offset in CROM_STREAMS:
            carved = out_dir / (name + ".raw")
            n = slice_file(ipl_1_seg1, offset, carved)
            if n == 0:
                sys.exit(f"error: no data at ipl_1_seg1 offset 0x{offset:x}")
            # uncrom writes <input>.0.bin next to its input
            subprocess.run(
                ["python3", str(toolkit / "uncrom.py"), str(carved)],
                check=True,
                env=env,
            )
            produced = carved.with_suffix(carved.suffix + ".0.bin")
            if not produced.is_file():
                sys.exit(
                    f"error: uncrom.py did not produce {produced}\n"
                    f"  (expected from carved input {carved})"
                )
            final = out_dir / name
            produced.replace(final)
            carved.unlink()
            print(f"  {name}: {final.stat().st_size:>9d} bytes")
    finally:
        if tqdm_shim:
            shutil.rmtree(tqdm_shim, ignore_errors=True)


def extract_squashfs(unsquashfs: str, ipl_0_seg1: Path, out_dir: Path) -> None:
    """Carve each SquashFS image out of ipl_0_seg1 and unpack it."""
    for name, offset in SQUASHFS_OFFSETS.items():
        target_root = out_dir / name
        if target_root.exists():
            shutil.rmtree(target_root)
        with tempfile.NamedTemporaryFile(prefix=name + "_", suffix=".sqfs",
                                         delete=False) as tf:
            tmp_sqfs = Path(tf.name)
        try:
            slice_file(ipl_0_seg1, offset, tmp_sqfs)
            if tmp_sqfs.read_bytes()[:4] != SQUASHFS_MAGIC:
                sys.exit(
                    f"error: no SquashFS magic at ipl_0_seg1 offset 0x{offset:x}\n"
                    f"  (expected 'hsqs' — is this the right firmware?)"
                )
            # unsquashfs writes to -d target. -ignore-errors keeps going past the
            # unavoidable "can't create device node as non-root" failures on macOS
            # — the firmware rootfs ships ~150 /dev/* nodes we don't need for
            # static analysis. Requires unsquashfs >= 4.5.
            proc = subprocess.run(
                [unsquashfs, "-quiet", "-no-progress", "-ignore-errors",
                 "-d", str(target_root),
                 str(tmp_sqfs)],
                stderr=subprocess.PIPE, text=True,
            )
            if proc.returncode != 0 and not target_root.is_dir():
                sys.stderr.write(proc.stderr)
                sys.exit(f"error: unsquashfs failed for {name} (exit {proc.returncode})")
            n_files = sum(1 for _ in target_root.rglob("*"))
            skipped = proc.stderr.count("could not create")
            note = f", {skipped} /dev/* nodes skipped" if skipped else ""
            print(f"  {name}: unpacked ({n_files} entries{note})")
        finally:
            tmp_sqfs.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Extract Epson XP-442 firmware (.efu or .rcx) into rcx_output/, "
                    "squashfs_rootfs1/, squashfs_rootfs2/.")
    p.add_argument("firmware", help="Path to .efu or .rcx firmware file")
    p.add_argument("--out", default=".",
                   help="Output directory (default: current dir)")
    p.add_argument("--toolkit", default=None,
                   help="Path to epson-reversing toolkit "
                        "(default: ./epson-reversing/)")
    p.add_argument("--skip-crom", action="store_true",
                   help="Skip Block 1 CROM decompression (WebConfig / panel / gfx)")
    p.add_argument("--skip-squashfs", action="store_true",
                   help="Skip SquashFS extraction (rootfs / network module)")
    args = p.parse_args(argv)

    firmware = Path(args.firmware).resolve()
    if not firmware.is_file():
        sys.exit(f"error: firmware not found: {firmware}")

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    toolkit = resolve_toolkit(args.toolkit)
    unsquashfs = None
    if not args.skip_squashfs:
        unsquashfs = need_tool(
            "unsquashfs",
            "Install with: brew install squashfs   (macOS)\n"
            "              apt install squashfs-tools   (Debian/Ubuntu)"
        )

    # Step 1: .efu -> .rcx (if needed)
    ext = firmware.suffix.lower()
    if ext == ".efu":
        print(f"[1/5] Unzipping {firmware.name} …")
        rcx = unzip_efu(firmware, out_dir)
        print(f"      -> {rcx.relative_to(out_dir)}")
    elif ext == ".rcx":
        rcx = firmware
        print(f"[1/5] Skipping unzip (input is already .rcx)")
    else:
        sys.exit(f"error: unsupported input extension {ext!r}; expected .efu or .rcx")

    # Step 2: rcx_unpack
    print(f"[2/5] Splitting {rcx.name} into IPL blocks …")
    rcx_out = run_rcx_unpack(toolkit, rcx, out_dir)
    ipl_0_seg1 = rcx_out / "ipl_0_seg1"
    ipl_1_seg1 = rcx_out / "ipl_1_seg1"
    for p in (ipl_0_seg1, ipl_1_seg1):
        if not p.is_file():
            sys.exit(f"error: rcx_unpack did not produce {p}")
    print(f"      -> {rcx_out.relative_to(out_dir)}/")

    # Step 3: Isolate ARM1136 RTOS core from ipl_0_seg1
    rtos_bin = rcx_out / "rtos_core.bin"
    print("[3/5] Isolating ARM1136 RTOS core …")
    n = slice_file(ipl_0_seg1, 0, rtos_bin, RTOS_CORE_END)
    print(f"      -> rtos_core.bin: {n:>9d} bytes")

    # Step 4: Block 1 CROM streams
    if not args.skip_crom:
        print("[4/5] Decompressing Block 1 CROM streams …")
        carve_crom_streams(toolkit, ipl_1_seg1, rcx_out)
    else:
        print("[4/5] Skipping CROM decompression (--skip-crom)")

    # Step 5: Block 0 SquashFS images
    if not args.skip_squashfs:
        print("[5/5] Extracting Block 0 SquashFS filesystems …")
        extract_squashfs(unsquashfs, ipl_0_seg1, out_dir)
    else:
        print("[5/5] Skipping SquashFS extraction (--skip-squashfs)")

    print("\nDone. Layout:")
    print(f"  {out_dir}/rcx_output/")
    print(f"    rtos_core.bin              (ARM1136 RTOS, base 0x0, ARM:LE:32:v6)")
    if not args.skip_squashfs:
        print(f"  {out_dir}/squashfs_rootfs1/   (Linux rootfs)")
        print(f"  {out_dir}/squashfs_rootfs2/   (network module)")
    print("\nSee FIRMWARE_LAYOUT.md for the annotated layout, ENPC.md / PNRSC_CONFIG_REGISTRY.md for analysis.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
