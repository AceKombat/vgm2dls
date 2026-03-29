import argparse
import struct
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def u16_le(data, off):
    return struct.unpack_from("<H", data, off)[0] if off + 2 <= len(data) else 0


def u32_le(data, off):
    return struct.unpack_from("<I", data, off)[0] if off + 4 <= len(data) else 0


def pan_raw_to_7bit(pan_raw):
    raw = int(pan_raw) & 0xFFFF
    if raw <= 0x7F:
        return raw
    if raw <= 0x1FF:
        return int(round((raw / 0x1FF) * 127))
    return raw & 0x7F


def find_vb_file(vh_path: Path):
    parent = vh_path.parent
    stem_lower = vh_path.stem.lower()
    direct = parent / f"{vh_path.stem}.vb"
    if direct.is_file():
        return direct
    direct_upper = parent / f"{vh_path.stem}.VB"
    if direct_upper.is_file():
        return direct_upper
    for cand in parent.glob("*"):
        if cand.is_file() and cand.suffix.lower() == ".vb" and cand.stem.lower() == stem_lower:
            return cand
    return None


def parse_989_header(vh_path: Path, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{vh_path.stem}.txt"

    data = vh_path.read_bytes()

    vh_size = len(data)
    vb_size_header = u32_le(data, 0x30)
    vb_file = find_vb_file(vh_path)
    if vb_file:
        vb_size = vb_file.stat().st_size
    else:
        vb_size = vb_size_header
    patch_meta_off = u32_le(data, 0x24)
    layer_table_off = u32_le(data, 0x28)
    max_patches = data[0x1A] if 0x1A < len(data) else 0

    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"Total VH Size: 0x{vh_size:X}\n")
        f.write(f"Total VB Size: 0x{vb_size:X}\n")
        f.write(f"VH Table Offset Location: 0x{layer_table_off:X}\n")
        f.write(f"Number of patches: {max_patches}\n\n")

        current_layer_idx = 0
        processed = 0
        patch_idx = 1
        meta_off = patch_meta_off

        while meta_off + 8 <= len(data) and processed < max_patches:
            entry = data[meta_off : meta_off + 8]
            layer_count = entry[0]

            # Null entry: skip printing, consume slot
            if all(b == 0 for b in entry):
                meta_off += 8
                patch_idx += 1
                processed += 1
                continue

            # Invalid count: warn, consume slot
            if layer_count == 0 or layer_count > 32:
                f.write(f"Patch {patch_idx}: unusual (layer count {layer_count})\n\n")
                meta_off += 8
                patch_idx += 1
                processed += 1
                continue

            patch_vol = entry[1]
            pan_patch = u16_le(entry, 2)
            layer_base_off = u32_le(entry, 4)

            f.write(f"Patch {patch_idx}:\n")
            f.write(f"  Patch Volume: 0x{patch_vol:02X}\n")
            f.write(f"  Number of layers: {layer_count}\n")
            f.write(f"  Relative Offset Location: 0x{meta_off - patch_meta_off:X}\n")
            f.write(f"  Offset Location: 0x{meta_off:X}\n")
            f.write(f"  First 8 bytes after offset: {entry.hex().upper()}\n")

            for layer_num in range(1, layer_count + 1):
                if layer_base_off:
                    layer_off = layer_base_off + ((layer_num - 1) * 24)
                else:
                    layer_off = layer_table_off + (current_layer_idx * 24)
                if layer_off + 24 > len(data):
                    f.write(f"    Layer {layer_num}: truncated\n")
                    break

                ld = data[layer_off : layer_off + 24]

                vol = ld[1]
                root = ld[2]
                cents = ld[3]
                pan_raw = u16_le(ld, 4)
                min_key = ld[6]
                max_key = ld[7]
                decay = ld[0x0A]
                attack_raw = ld[0x0B]
                attack_eff = attack_raw & 0x7F
                adsr2_raw = u16_le(ld, 0x0C)
                vb_offset = u32_le(ld, 0x14)

                f.write(f"    Layer {layer_num}:\n")
                f.write(f"      Data: {ld.hex().upper()}\n")
                f.write(f"      Min Key Range: {min_key}\n")
                f.write(f"      Max Key Range: {max_key}\n")
                f.write(f"      Root Key: {root}\n")
                f.write(f"      Cents: {cents}\n")
                f.write(f"      VB Offset (filename): {vb_offset}\n")
                f.write(f"      Sustain Level: -1\n")
                f.write(f"      Attack Rate: {attack_eff}\n")
                f.write(f"      Release Rate: {adsr2_raw & 0xFF}\n")
                f.write(f"      Sustain Rate: {(adsr2_raw >> 8) & 0xFF}\n")
                f.write(f"      ADSR2 Raw (LE): 0x{adsr2_raw:04X}\n")
                f.write(f"      Decay: {decay}\n")

                lo = adsr2_raw & 0xFF
                hi = (adsr2_raw >> 8) & 0xFF
                f.write(f"      ADSR2 Release Shift (0..31): {lo & 0x1F}\n")
                f.write(f"      ADSR2 Release Exponential: {1 if (lo & 0x20) else 0}\n")
                f.write(f"      ADSR2 Sustain Step Code (0..3): {(lo >> 6) & 0x03}\n")
                f.write(f"      ADSR2 Sustain Shift (0..31): {hi & 0x1F}\n")
                f.write(f"      ADSR2 Sustain Direction (Inc=1/Dec=0): {1 if (hi & 0x40) else 0}\n")
                f.write(f"      ADSR2 Sustain Exponential: {1 if (hi & 0x80) else 0}\n")
                f.write(f"      Volume: {vol}\n")
                f.write(f"      Pan: {pan_raw_to_7bit(pan_raw)}\n")
                f.write(f"      Pan raw (16-bit LE): 0x{pan_raw:04X}\n")
                f.write(f"      Reverb: 0\n")

                current_layer_idx += 1

            f.write("\n")

            meta_off += 8
            patch_idx += 1
            processed += 1

    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="989 Studios SBNK (.vh) header parser to vgm2dls TXT format."
    )
    parser.add_argument("--vh", default="COLO.vh", help="Input .vh file")
    parser.add_argument(
        "--out-dir",
        default=str(PROJECT_ROOT / "data" / "header_temp"),
        help="Output directory for txt dump",
    )
    args = parser.parse_args()

    out_path = parse_989_header(Path(args.vh), Path(args.out_dir))
    print(f"Wrote metadata: {out_path}")


if __name__ == "__main__":
    main()
