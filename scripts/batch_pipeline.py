import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def run_cmd(cmd, cwd):
    print(">>", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=cwd)


def find_bank_pairs(root: Path, header_ext: str, bank_ext: str, recursive: bool = False):
    scan = root.rglob if recursive else root.glob
    hd_by_stem = {p.stem: p for p in scan(f"*.{header_ext}")}
    bd_by_stem = {p.stem: p for p in scan(f"*.{bank_ext}")}
    common = sorted(set(hd_by_stem) & set(bd_by_stem))
    return [(stem, hd_by_stem[stem], bd_by_stem[stem]) for stem in common]


def merge_unique_pairs(existing, incoming):
    seen = {(str(h.resolve()), str(b.resolve())) for _, h, b in existing}
    for stem, h, b in incoming:
        key = (str(h.resolve()), str(b.resolve()))
        if key in seen:
            continue
        existing.append((stem, h, b))
        seen.add(key)
    return existing


def main():
    parser = argparse.ArgumentParser(
        description="Batch Ape Escape pipeline: *.hd + *.bd -> metadata txt -> split wavs -> dls"
    )
    project_root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--input-root",
        default=str(project_root / "input"),
        help="Directory to scan for .hd/.bd pairs",
    )
    parser.add_argument(
        "--output-root",
        default=str(project_root / "output"),
        help="Base directory for generated output folders",
    )
    parser.add_argument("--sample-rate", type=int, default=22043)
    parser.add_argument("--master-volume-percent", type=int, default=75)
    parser.add_argument("--profile", default="awave", choices=["awave", "fl"])
    parser.add_argument("--cents-step", type=float, default=6.25)
    parser.add_argument("--root-shift", type=int, default=-12)
    parser.add_argument("--force-dls-rate", type=int, default=0)
    parser.add_argument("--adsr-mode", default="off", choices=["off", "basic"])
    parser.add_argument("--release-model", default="spu_sim", choices=["spu_sim", "spu_cal", "absolute"])
    parser.add_argument("--spu-release-start-level", default="full", choices=["full", "sustain"])
    parser.add_argument("--spu-release-scale", type=float, default=1.0)
    parser.add_argument("--spu-release-shape", type=float, default=1.0)
    parser.add_argument("--spu-cal-raw-a", type=int, default=206)
    parser.add_argument("--spu-cal-seconds-a", type=float, default=4.0)
    parser.add_argument("--spu-cal-raw-b", type=int, default=238)
    parser.add_argument("--spu-cal-seconds-b", type=float, default=5.0)
    parser.add_argument("--reverb-release-coupling", type=float, default=0.0)
    parser.add_argument("--reverb-release-power", type=float, default=1.0)
    parser.add_argument("--reverb-release-threshold", type=int, default=96)
    parser.add_argument("--use-adsr2", action="store_true")
    parser.add_argument("--log-header-txt", action="store_true")
    parser.add_argument("--log-wav-data", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--skip-existing", action="store_true", help="Skip banks that already have output dls")
    parser.add_argument("--header-ext", default="hd", help="Header file extension (default: hd)")
    parser.add_argument("--bank-ext", default="bd", help="Bank file extension (default: bd)")
    parser.add_argument(
        "--parser-script",
        default=str(project_root / "scripts" / "extract_hd_to_txt.py"),
        help="Header parser script path",
    )
    parser.add_argument(
        "--header-arg",
        default="--hd",
        help="Argument name for header path passed to parser script (default: --hd)",
    )
    parser.add_argument(
        "--bank-data-offset",
        type=int,
        default=0,
        help="Bytes to skip at start of bank file before slicing offsets",
    )
    parser.add_argument(
        "--auto-skip-zeros16",
        action="store_true",
        help="If first 16 bytes are 0x00, auto-skip them when slicing offsets",
    )
    parser.add_argument(
        "--container-ext",
        default="",
        help="Optional container extension to scan and extract first (example: fog)",
    )
    parser.add_argument(
        "--container-extract-script",
        default="",
        help="Optional script used to extract containers into header/bank files",
    )
    parser.add_argument(
        "--container-arg",
        default="--fog",
        help="Argument name for container path passed to extract script (default: --fog)",
    )
    parser.add_argument(
        "--pan-mode",
        default="psx127",
        choices=["psx127", "signed_byte", "989_table", "989_clef"],
        help="Pan mapping mode passed to make_dls.py",
    )
    args = parser.parse_args()

    input_root = Path(args.input_root).resolve()
    output_root = Path(args.output_root).resolve()
    header_root = output_root / "1-header"
    wav_root = output_root / "2-wav"
    dls_root = output_root / "3-dls"
    persist_header = bool(args.log_header_txt)
    persist_wav = bool(args.log_wav_data)
    if persist_header:
        header_root.mkdir(parents=True, exist_ok=True)
    if persist_wav:
        wav_root.mkdir(parents=True, exist_ok=True)
    dls_root.mkdir(parents=True, exist_ok=True)

    ok = []
    failed = []
    skipped = []

    with tempfile.TemporaryDirectory(prefix="vgm2dls_work_") as temp_work:
        temp_root = Path(temp_work)
        pairs = find_bank_pairs(input_root, args.header_ext, args.bank_ext, recursive=False)

        container_ext = str(args.container_ext).strip().lstrip(".").lower()
        container_extract_script = str(args.container_extract_script).strip()
        if container_ext and container_extract_script:
            extracted_root = temp_root / "0-container-extract"
            extracted_root.mkdir(parents=True, exist_ok=True)
            container_files = sorted(input_root.glob(f"*.{container_ext}"))
            if container_files:
                print(
                    f"Found {len(container_files)} .{container_ext} container(s); extracting with {container_extract_script}"
                )
            for container in container_files:
                per_container_out = extracted_root / container.stem
                per_container_out.mkdir(parents=True, exist_ok=True)
                run_cmd(
                    [
                        sys.executable,
                        container_extract_script,
                        args.container_arg,
                        str(container),
                        "--out-dir",
                        str(per_container_out),
                    ],
                    cwd=project_root,
                )
            extracted_pairs = find_bank_pairs(extracted_root, args.header_ext, args.bank_ext, recursive=True)
            if extracted_pairs:
                print(f"Discovered {len(extracted_pairs)} extracted .{args.header_ext}/.{args.bank_ext} pair(s) from containers.")
                pairs = merge_unique_pairs(pairs, extracted_pairs)

        if not pairs:
            print(f"No matching .{args.header_ext}/.{args.bank_ext} pairs found in {input_root}")
            return

        print(f"Found {len(pairs)} pair(s).")
        for stem, hd_path, bd_path in pairs:
            print(f"\n=== {stem} ===")
            if persist_header:
                per_header = header_root / stem
            else:
                per_header = temp_root / "1-header" / stem
            if persist_wav:
                per_wavs = wav_root / stem
            else:
                per_wavs = temp_root / "2-wav" / stem
            per_header.mkdir(parents=True, exist_ok=True)
            per_wavs.mkdir(parents=True, exist_ok=True)
            metadata_path = per_header / f"{stem}.txt"
            out_dls = dls_root / f"{stem}.dls"
            if args.skip_existing and out_dls.exists():
                skipped.append(stem)
                print(f"== Skipping existing: {stem}")
                continue

            try:
                run_cmd(
                    [
                        sys.executable,
                        str(Path(args.parser_script)),
                        args.header_arg,
                        str(hd_path),
                        "--out-dir",
                        str(per_header),
                    ],
                    cwd=project_root,
                )

                if args.auto_skip_zeros16:
                    run_cmd(
                        [
                            sys.executable,
                            str(project_root / "scripts" / "ps1_adpcm_to_wav.py"),
                            "--bank",
                            str(bd_path),
                            "--metadata",
                            str(metadata_path),
                            "--out",
                            str(per_wavs),
                            "--sample-rate",
                            str(args.sample_rate),
                            "--bank-data-offset",
                            str(args.bank_data_offset),
                            "--auto-skip-zeros16",
                        ],
                        cwd=project_root,
                    )
                else:
                    run_cmd(
                        [
                            sys.executable,
                            str(project_root / "scripts" / "ps1_adpcm_to_wav.py"),
                            "--bank",
                            str(bd_path),
                            "--metadata",
                            str(metadata_path),
                            "--out",
                            str(per_wavs),
                            "--sample-rate",
                            str(args.sample_rate),
                            "--bank-data-offset",
                            str(args.bank_data_offset),
                        ],
                        cwd=project_root,
                    )

                cmd = [
                    sys.executable,
                    str(project_root / "scripts" / "make_dls.py"),
                    "--metadata",
                    str(metadata_path),
                    "--wav-dir",
                    str(per_wavs),
                    "--out",
                    str(out_dls),
                    "--profile",
                    args.profile,
                    "--cents-step",
                    str(args.cents_step),
                    "--root-shift",
                    str(args.root_shift),
                    "--master-volume-percent",
                    str(args.master_volume_percent),
                    "--adsr-mode",
                    args.adsr_mode,
                    "--release-model",
                    args.release_model,
                    "--spu-release-start-level",
                    args.spu_release_start_level,
                    "--spu-release-scale",
                    str(args.spu_release_scale),
                    "--spu-release-shape",
                    str(args.spu_release_shape),
                    "--spu-cal-raw-a",
                    str(args.spu_cal_raw_a),
                    "--spu-cal-seconds-a",
                    str(args.spu_cal_seconds_a),
                    "--spu-cal-raw-b",
                    str(args.spu_cal_raw_b),
                    "--spu-cal-seconds-b",
                    str(args.spu_cal_seconds_b),
                    "--reverb-release-coupling",
                    str(args.reverb_release_coupling),
                    "--reverb-release-power",
                    str(args.reverb_release_power),
                    "--reverb-release-threshold",
                    str(args.reverb_release_threshold),
                    "--pan-mode",
                    args.pan_mode,
                ]
                if args.force_dls_rate:
                    cmd.extend(["--force-dls-rate", str(args.force_dls_rate)])
                if args.use_adsr2:
                    cmd.append("--use-adsr2")

                run_cmd(cmd, cwd=project_root)
                ok.append(stem)
            except Exception as exc:
                failed.append((stem, str(exc)))
                print(f"!! FAILED: {stem}: {exc}")
                if not args.continue_on_error:
                    break

    print("\n=== Summary ===")
    print(f"Success: {len(ok)}")
    for stem in ok:
        print(f"  OK  {stem}")
    print(f"Skipped: {len(skipped)}")
    for stem in skipped:
        print(f"  SKIP {stem}")
    print(f"Failed: {len(failed)}")
    for stem, err in failed:
        print(f"  ERR {stem}: {err}")

    log_path = project_root / "batch_failures.log"
    with log_path.open("w", encoding="utf-8") as log:
        for stem, err in failed:
            log.write(f"{stem}\t{err}\n")
    print(f"Failure log: {log_path}")


if __name__ == "__main__":
    main()



