import argparse
import os
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TXTH_TEMPLATE = "\n".join(
    [
        "codec=PSX",
        "sample_rate={sample_rate}",
        "channels=1",
        "loop_flag=auto",
        "num_samples=data_size",
        "",
    ]
)


def parse_metadata(metadata_path: Path):
    text = metadata_path.read_text(encoding="utf-8")

    total_bd_size_match = re.search(r"^Total BD Size:\s*(0x[0-9a-fA-F]+)", text, re.MULTILINE)
    if not total_bd_size_match:
        total_bd_size_match = re.search(r"^Total VB Size:\s*(0x[0-9a-fA-F]+)", text, re.MULTILINE)

    offsets = sorted(
        {
            int(match.group(1))
            for match in re.finditer(r"VB Offset \(filename\):\s*(\d+)", text)
        }
    )
    if not offsets:
        raise ValueError(f"No VB offsets found in {metadata_path}")

    total_size_hint = int(total_bd_size_match.group(1), 16) if total_bd_size_match else None
    return offsets, total_size_hint


def write_txth_for_bd(chunk_bd_path: Path, sample_rate: int):
    txth_path = chunk_bd_path.with_suffix(chunk_bd_path.suffix + ".txth")
    txth_path.write_text(TXTH_TEMPLATE.format(sample_rate=sample_rate), encoding="ascii")
    return txth_path


def decode_chunk_to_wav(vgmstream_exe: Path, chunk_bd_path: Path, wav_path: Path):
    command = [str(vgmstream_exe), str(chunk_bd_path), "-L", "-o", str(wav_path)]
    subprocess.run(command, check=True)


def trim_psx_chunk_at_end_flag(chunk: bytes):
    # PSX ADPCM frames are 16 bytes, flag byte at frame[1].
    # If a chunk contains spillover into the next sample, the first end-flag frame
    # marks the true stream end for the current sample.
    if len(chunk) < 16:
        return chunk, None

    frame_count = len(chunk) // 16
    for fi in range(frame_count):
        off = fi * 16
        flag = chunk[off + 1]
        if (flag & 0x01) == 0:
            continue

        # Keep any immediate consecutive end-flag frames.
        fj = fi
        while fj + 1 < frame_count:
            noff = (fj + 1) * 16
            nflag = chunk[noff + 1]
            if (nflag & 0x01) == 0:
                break
            fj += 1

        trim_end = (fj + 1) * 16
        if 0 < trim_end < len(chunk):
            return chunk[:trim_end], trim_end
        break

    return chunk, None


def extract_and_decode_bank(
    bank_bd_path: Path,
    metadata_path: Path,
    output_folder: Path,
    vgmstream_exe: Path,
    sample_rate: int,
    bank_data_offset: int,
    auto_skip_zeros16: bool,
    trim_at_end_flag: bool,
):
    offsets, total_size_hint = parse_metadata(metadata_path)
    output_folder.mkdir(parents=True, exist_ok=True)

    with bank_bd_path.open("rb") as f:
        bd_data = f.read()

    data_offset = max(0, int(bank_data_offset))
    if auto_skip_zeros16 and len(bd_data) >= 16 and all(b == 0 for b in bd_data[:16]):
        data_offset = max(data_offset, 16)

    payload_total_size = max(0, len(bd_data) - data_offset)
    if total_size_hint is not None and total_size_hint != payload_total_size:
        print(
            f"Warning: metadata total size ({total_size_hint}) != payload size ({payload_total_size}); "
            f"using payload size for slicing."
        )

    valid_offsets = [off for off in offsets if 0 <= off < payload_total_size]
    invalid_offsets = [off for off in offsets if off < 0 or off >= payload_total_size]
    if invalid_offsets:
        print(
            f"Warning: {metadata_path} has {len(invalid_offsets)} out-of-range offsets; "
            f"max invalid={max(invalid_offsets)}; payload size={payload_total_size}. Skipping invalid entries."
        )
    if not valid_offsets:
        raise ValueError(f"No valid VB offsets found in {metadata_path}")

    payload = bd_data[data_offset : data_offset + payload_total_size]
    boundaries = valid_offsets + [payload_total_size]
    for idx, start in enumerate(valid_offsets):
        end = boundaries[idx + 1]
        if end <= start:
            continue

        chunk = payload[start:end]
        if trim_at_end_flag:
            trimmed, trim_end = trim_psx_chunk_at_end_flag(chunk)
            if trim_end is not None:
                # Guard against pathological tiny chunks after trim.
                if len(trimmed) >= 16:
                    chunk = trimmed
                    print(f"Trimmed {start}.bd at first end-flag: {end - start} -> {len(chunk)} bytes")
        chunk_bd_path = output_folder / f"{start}.bd"
        chunk_bd_path.write_bytes(chunk)
        write_txth_for_bd(chunk_bd_path, sample_rate)

        wav_path = output_folder / f"{start}.wav"
        print(f"Decoding {chunk_bd_path.name} -> {wav_path.name} ({len(chunk)} bytes)")
        decode_chunk_to_wav(vgmstream_exe, chunk_bd_path, wav_path)


def main():
    parser = argparse.ArgumentParser(
        description="Split PSX ADPCM bank data by metadata offsets and decode to WAV using vgmstream + .txth sidecars."
    )
    parser.add_argument("--bank", default="s_02_horn_melody.bd", help="Input bank file (.bd/.vb)")
    parser.add_argument(
        "--metadata",
        default=os.path.join(PROJECT_ROOT, "data", "header_temp", "s_02_horn_melody.txt"),
        help="Metadata text generated by 1-HD_to_TXTBIN.py",
    )
    parser.add_argument(
        "--out",
        default=os.path.join(PROJECT_ROOT, "data", "wav_temp", "s_02_horn_melody"),
        help="Output folder for split chunk/.txth/.wav files",
    )
    parser.add_argument(
        "--vgmstream",
        default=os.path.join(PROJECT_ROOT, "tools", "vgmstream", "vgmstream-cli.exe"),
        help="Path to vgmstream-cli executable",
    )
    parser.add_argument("--sample-rate", type=int, default=22043, help="Sample rate to place in .txth")
    parser.add_argument(
        "--bank-data-offset",
        type=int,
        default=0,
        help="Bytes to skip at the start of the bank file before slicing offsets (e.g., 16 for some .vb).",
    )
    parser.add_argument(
        "--auto-skip-zeros16",
        action="store_true",
        help="If the first 16 bytes are 0x00, auto-skip them for slicing.",
    )
    parser.add_argument(
        "--no-trim-end-flag",
        action="store_true",
        help="Disable PSX ADPCM end-flag trimming (enabled by default).",
    )
    args = parser.parse_args()

    extract_and_decode_bank(
        bank_bd_path=Path(args.bank),
        metadata_path=Path(args.metadata),
        output_folder=Path(args.out),
        vgmstream_exe=Path(args.vgmstream),
        sample_rate=args.sample_rate,
        bank_data_offset=args.bank_data_offset,
        auto_skip_zeros16=args.auto_skip_zeros16,
        trim_at_end_flag=not args.no_trim_end_flag,
    )


if __name__ == "__main__":
    main()
