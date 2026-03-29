import argparse
import math
import re
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def chunk(fourcc: bytes, data: bytes) -> bytes:
    out = fourcc + struct.pack("<I", len(data)) + data
    if len(data) % 2:
        out += b"\x00"
    return out


def list_chunk(list_type: bytes, data: bytes) -> bytes:
    payload = list_type + data
    return chunk(b"LIST", payload)


def parse_metadata_txt(path: Path):
    text = path.read_text(encoding="utf-8")
    patches = []
    current_patch = None
    current_layer = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        m_patch = re.match(r"^Patch\s+(\d+):$", line)
        if m_patch:
            patch_number = int(m_patch.group(1))
            current_patch = {
                "patch_number": patch_number,
                "program": max(0, patch_number - 1),
                "layers": [],
                "volume": 127,
            }
            patches.append(current_patch)
            current_layer = None
            continue

        if current_patch is None:
            continue

        if line.startswith("Patch Volume:"):
            vol_text = line.split(":", 1)[1].strip()
            try:
                current_patch["volume"] = int(vol_text, 16)
            except ValueError:
                current_patch["volume"] = 127
            continue

        if re.match(r"^Layer\s+\d+:$", line):
            current_layer = {}
            current_patch["layers"].append(current_layer)
            continue

        if current_layer is None:
            continue

        if line.startswith("Data:"):
            hex_bytes = line.split(":", 1)[1].strip()
            if len(hex_bytes) >= 20:
                try:
                    raw_data = bytes.fromhex(hex_bytes)
                    current_layer["raw_data_bytes"] = raw_data
                    b6 = int(hex_bytes[12:14], 16)
                    b7 = int(hex_bytes[14:16], 16)
                    b8 = int(hex_bytes[16:18], 16)
                    b9 = int(hex_bytes[18:20], 16)
                    current_layer["adsr1_raw"] = b6 | (b7 << 8)
                    current_layer["adsr2_raw"] = b8 | (b9 << 8)
                except ValueError:
                    pass
            continue

        def parse_int_auto(s):
            return int(s, 0)

        field_map = {
            "Min Key Range": ("min_key", int),
            "Max Key Range": ("max_key", int),
            "Root Key": ("root_key", int),
            "Cents": ("cents", int),
            "VB Offset (filename)": ("sample_offset", int),
            "Sustain Level": ("sustain_level", int),
            "Attack Rate": ("attack_rate", int),
            # Raw ADSR2 release byte from metadata (bitwise field, not linear seconds).
            "Release Rate": ("release_adsr_byte", int),
            "Sustain Rate": ("sustain_rate", int),
            "Volume": ("volume", int),
            "Pan": ("pan", int),
            "Pan raw (16-bit LE)": ("pan_raw_16", parse_int_auto),
            "Reverb": ("reverb", int),
        }
        for field_name, (target, caster) in field_map.items():
            if line.startswith(field_name + ":"):
                current_layer[target] = caster(line.split(":", 1)[1].strip())
                break

    # Keep only patches with at least one valid layer.
    valid_patches = []
    for p in patches:
        good_layers = []
        for layer in p["layers"]:
            required = ["min_key", "max_key", "root_key", "cents", "sample_offset"]
            if all(k in layer for k in required):
                # 989 SBNK uses ADSR1/ADSR2 in bytes 0x0A..0x0D of layer data.
                # Generic parser above reads SShd positions (0x06..0x09), so fix it here.
                if "pan_raw_16" in layer and "raw_data_bytes" in layer:
                    raw = layer["raw_data_bytes"]
                    if len(raw) >= 0x0E:
                        layer["adsr1_raw"] = raw[0x0A] | (raw[0x0B] << 8)
                        layer["adsr2_raw"] = raw[0x0C] | (raw[0x0D] << 8)
                good_layers.append(layer)
        if good_layers:
            p["layers"] = good_layers
            valid_patches.append(p)

    if not valid_patches:
        raise ValueError(f"No usable patches/layers parsed from {path}")
    return valid_patches


def parse_wav_chunks(wav_data: bytes):
    if wav_data[:4] != b"RIFF" or wav_data[8:12] != b"WAVE":
        raise ValueError("Not a WAVE RIFF file")
    chunks = []
    off = 12
    while off + 8 <= len(wav_data):
        cid = wav_data[off : off + 4]
        csize = struct.unpack_from("<I", wav_data, off + 4)[0]
        start = off + 8
        end = start + csize
        if end > len(wav_data):
            raise ValueError("Invalid chunk size in WAV")
        chunks.append((cid, wav_data[start:end]))
        off = end + (csize % 2)
    return chunks


def read_wav_for_dls(path: Path):
    wav_data = path.read_bytes()
    chunks = parse_wav_chunks(wav_data)
    fmt_chunk = None
    data_chunk = None
    smpl_chunk = None
    for cid, cdata in chunks:
        if cid == b"fmt " and fmt_chunk is None:
            fmt_chunk = cdata
        elif cid == b"data" and data_chunk is None:
            data_chunk = cdata
        elif cid == b"smpl" and smpl_chunk is None:
            smpl_chunk = cdata
    if fmt_chunk is None or data_chunk is None:
        raise ValueError(f"{path} missing fmt/data")
    return fmt_chunk, data_chunk, smpl_chunk


def override_fmt_sample_rate(fmt_chunk: bytes, sample_rate: int):
    # PCM fmt chunk layout puts nSamplesPerSec at byte offset 4
    # and nAvgBytesPerSec at byte offset 8.
    if len(fmt_chunk) < 16:
        return fmt_chunk
    channels = struct.unpack_from("<H", fmt_chunk, 2)[0]
    bits_per_sample = struct.unpack_from("<H", fmt_chunk, 14)[0]
    block_align = max(1, (channels * bits_per_sample) // 8)
    avg_bytes_per_sec = sample_rate * block_align
    out = bytearray(fmt_chunk)
    struct.pack_into("<I", out, 4, sample_rate)
    struct.pack_into("<I", out, 8, avg_bytes_per_sec)
    return bytes(out)


def extract_loop_from_smpl(smpl_chunk: bytes):
    # WAV smpl: cSampleLoops at offset 28, first loop starts at 36.
    if smpl_chunk is None or len(smpl_chunk) < 60:
        return None
    c_loops = struct.unpack_from("<I", smpl_chunk, 28)[0]
    if c_loops < 1:
        return None
    loop_start = struct.unpack_from("<I", smpl_chunk, 44)[0]
    loop_end = struct.unpack_from("<I", smpl_chunk, 48)[0]
    if loop_end <= loop_start:
        return None
    # WAV smpl uses an explicit end sample. DLS stores loop length, so keep it exclusive.
    return loop_start, (loop_end - loop_start)


def _decode_pcm16_mono(fmt_chunk: bytes, data_chunk: bytes):
    if len(fmt_chunk) < 16:
        return None
    w_format = struct.unpack_from("<H", fmt_chunk, 0)[0]
    channels = struct.unpack_from("<H", fmt_chunk, 2)[0]
    bits = struct.unpack_from("<H", fmt_chunk, 14)[0]
    if w_format != 1 or channels != 1 or bits != 16:
        return None
    if len(data_chunk) % 2:
        return None
    return struct.unpack("<" + "h" * (len(data_chunk) // 2), data_chunk)


def _loop_boundary_score(samples, start_idx: int, end_idx: int):
    if samples is None:
        return None
    if start_idx <= 0 or end_idx <= 0:
        return None
    if start_idx >= len(samples) or end_idx >= len(samples):
        return None
    jump = abs(int(samples[start_idx]) - int(samples[end_idx]))
    slope_jump = abs(
        int(samples[start_idx]) - int(samples[start_idx - 1])
        - (int(samples[end_idx]) - int(samples[end_idx - 1]))
    )
    return (jump * 4) + slope_jump


def refine_loop_for_dls(fmt_chunk: bytes, data_chunk: bytes, loop_info, search_radius: int = 8):
    # DLS loops raw PCM, so ADPCM-authored loops can click even with nominally correct points.
    # Nudge loop boundaries in a tiny window only when it clearly reduces boundary discontinuity.
    if not loop_info:
        return loop_info
    samples = _decode_pcm16_mono(fmt_chunk, data_chunk)
    if samples is None:
        return loop_info

    start, length = loop_info
    if length <= 2:
        return loop_info
    end = start + length

    base_score = _loop_boundary_score(samples, start, end)
    if base_score is None:
        return loop_info

    best = (base_score, 0, 0, start, end)
    for ds in range(-search_radius, search_radius + 1):
        for de in range(-search_radius, search_radius + 1):
            s = start + ds
            e = end + de
            if s <= 1 or e <= 1 or s >= len(samples) or e >= len(samples):
                continue
            if e <= s:
                continue
            score = _loop_boundary_score(samples, s, e)
            if score is None:
                continue
            cand = (score, abs(ds) + abs(de), abs(ds) + abs(de), s, e)
            if cand < best:
                best = cand

    best_score, _, _, best_start, best_end = best
    # Conservative adoption: only if clearly better and movement is tiny.
    if best_score <= (base_score * 0.35):
        return best_start, (best_end - best_start)
    return loop_info


def build_wsmp(root_key: int, cents: int, attenuation_db: float, loop_info):
    # DLS WSMP uses signed 16-bit fine tune in cents.
    fine_tune = max(-32768, min(32767, cents))
    # Awave displays lAttenuation using a 10x-scaled dB convention in this field.
    # Empirical mapping: displayed dB ~= (lAttenuation / 65536) / 10.
    attenuation_units = -int(round(attenuation_db * 10.0 * 65536.0))
    base = struct.pack(
        "<I H h i I I",
        20,  # cbSize
        root_key & 0xFFFF,  # usUnityNote
        fine_tune,  # sFineTune
        attenuation_units,  # lAttenuation (16.16 dB attenuation)
        0,  # fulOptions
        1 if loop_info else 0,  # cSampleLoops
    )
    if not loop_info:
        return base
    loop_start, loop_len = loop_info
    loop = struct.pack(
        "<I I I I",
        16,  # cbSize
        0,  # ulLoopType (forward)
        loop_start,
        loop_len,
    )
    return base + loop


def rate_to_timecents(raw_rate: int, min_seconds: float, max_seconds: float):
    raw = max(0, min(255, raw_rate))
    if min_seconds <= 0 or max_seconds <= 0 or min_seconds >= max_seconds:
        return 0
    # Higher raw rate = faster envelope (shorter time).
    t = max_seconds * ((min_seconds / max_seconds) ** (raw / 255.0))
    return int(round(1200.0 * math.log2(t)))


def signed_attack_to_raw(attack_signed: int):
    # Header attack is signed (-128..127) where -128 is the fastest edge.
    # Convert to 0..255 where larger means faster for rate_to_timecents().
    s = max(-128, min(127, int(attack_signed)))
    return max(0, min(255, 127 - s))

def simulate_spu_attack_seconds(
    adsr1_raw: int,
    sample_rate: int = 44100,
    max_seconds: float = 40.000,
):
    # Matches 989snd-clef envelope.cpp Attack path:
    # m_Exp=AttackExp, m_Shift=AttackShift, m_Step=(7-AttackStep), target=0x7FFF.
    attack_exp = ((adsr1_raw >> 15) & 0x1) != 0
    attack_shift = (adsr1_raw >> 10) & 0x1F
    attack_step_code = (adsr1_raw >> 8) & 0x03
    step = (7 - attack_step_code) << max(0, 11 - attack_shift)
    if step <= 0:
        return max_seconds

    c_step = 0x800000
    shift = attack_shift - 11
    if shift > 0:
        c_step >>= shift

    level = 0
    counter = 0
    ticks = 0
    max_ticks = int(sample_rate * max_seconds)
    while level < 0x7FFF and ticks < max_ticks:
        cur_c_step = c_step
        # Exponential attack slows once level passes 0x6000.
        if attack_exp and level > 0x6000:
            cur_c_step >>= 2
        counter += cur_c_step
        if counter >= 0x800000:
            counter = 0
            level = min(0x7FFF, level + step)
        ticks += 1
    return min(max_seconds, ticks / float(sample_rate))


def attack_to_timecents_for_layer(layer):
    # 989 layers carry raw ADSR1; prefer exact SPU simulation over fitted curves.
    if "pan_raw_16" in layer and "adsr1_raw" in layer:
        # 989 convention in these banks: effective attack 0 should be instant.
        if int(layer.get("attack_rate", 0)) <= 0:
            return None
        seconds = simulate_spu_attack_seconds(int(layer["adsr1_raw"]))
        # Treat sub-ms attack as effectively instant to avoid artificial fades.
        if seconds <= 0.0005:
            return None
        return int(round(1200.0 * math.log2(seconds)))

    # Generic SShd path: attack byte behaves as 7-bit bucket with 0x80 mode bit.
    # In observed banks, lower 7-bit values are faster attacks.
    if "adsr1_raw" in layer:
        adsr1_raw = int(layer["adsr1_raw"])
        attack_7bit = (adsr1_raw >> 8) & 0x7F
        atk_raw = max(0, min(255, (127 - attack_7bit) * 2))
        return rate_to_timecents(atk_raw, min_seconds=0.00001, max_seconds=8.0)

    attack = int(layer.get("attack_rate", 0))
    atk_raw = signed_attack_to_raw(attack)
    return rate_to_timecents(atk_raw, min_seconds=0.00001, max_seconds=8.0)
def pan_byte_to_norm(raw_pan: int, pan_mode: str):
    raw = int(raw_pan) & 0xFF
    if pan_mode == "psx127":
        # Game-style behavior: domain is effectively 0..127.
        # Values >=127 are treated as hard-right (same as 127).
        clamped = min(raw, 127)
        return (clamped / 127.0) * 2.0 - 1.0
    # Legacy/signed-byte interpretation for testing.
    if raw <= 127:
        return (raw / 127.0) * 2.0 - 1.0
    return (raw - 256.0) / 128.0


SBNK_PAN_TABLE = [
    (0x3FFF, 0x0000), (0x3FFE, 0x008E), (0x3FFC, 0x011D), (0x3FF9, 0x01AC),
    (0x3FF5, 0x023B), (0x3FEF, 0x02CA), (0x3FE8, 0x0359), (0x3FE0, 0x03E8),
    (0x3FD7, 0x0476), (0x3FCC, 0x0505), (0x3FC0, 0x0593), (0x3FB3, 0x0622),
    (0x3FA5, 0x06B0), (0x3F95, 0x073E), (0x3F84, 0x07CC), (0x3F72, 0x085A),
    (0x3F5F, 0x08E8), (0x3F4B, 0x0975), (0x3F35, 0x0A02), (0x3F1E, 0x0A8F),
    (0x3F06, 0x0B1C), (0x3EEC, 0x0BA9), (0x3ED1, 0x0C36), (0x3EB6, 0x0CC2),
    (0x3E98, 0x0D4E), (0x3E7A, 0x0DD9), (0x3E5B, 0x0E65), (0x3E3A, 0x0EF0),
    (0x3E18, 0x0F7B), (0x3DF5, 0x1005), (0x3DD0, 0x1090), (0x3DAB, 0x111A),
    (0x3D84, 0x11A3), (0x3D5C, 0x122D), (0x3D33, 0x12B5), (0x3D08, 0x133E),
    (0x3CDD, 0x13C6), (0x3CB0, 0x144E), (0x3C82, 0x14D5), (0x3C53, 0x155C),
    (0x3C22, 0x15E3), (0x3BF1, 0x1669), (0x3BBE, 0x16EF), (0x3B8B, 0x1774),
    (0x3B56, 0x17F9), (0x3B1F, 0x187D), (0x3AE8, 0x1901), (0x3AB0, 0x1984),
    (0x3A76, 0x1A07), (0x3A3B, 0x1A89), (0x3A00, 0x1B0B), (0x39C3, 0x1B8D),
    (0x3984, 0x1C0D), (0x3945, 0x1C8E), (0x3905, 0x1D0D), (0x38C3, 0x1D8C),
    (0x3881, 0x1E0B), (0x383D, 0x1E89), (0x37F8, 0x1F06), (0x37B3, 0x1F83),
    (0x376C, 0x1FFF), (0x3724, 0x207B), (0x36DA, 0x20F5), (0x3690, 0x2170),
    (0x3645, 0x21E9), (0x35F9, 0x2262), (0x35AB, 0x22DA), (0x355D, 0x2352),
    (0x350E, 0x23C9), (0x34BD, 0x243F), (0x346C, 0x24B4), (0x3419, 0x2529),
    (0x33C6, 0x259D), (0x3371, 0x2610), (0x331C, 0x2683), (0x32C5, 0x26F5),
    (0x326D, 0x2766), (0x3215, 0x27D6), (0x31BB, 0x2846), (0x3161, 0x28B4),
    (0x3106, 0x2922), (0x30A9, 0x298F), (0x304C, 0x29FC), (0x2FEE, 0x2A67),
    (0x2F8E, 0x2AD2), (0x2F2E, 0x2B3C), (0x2ECD, 0x2BA5), (0x2E6B, 0x2C0D),
    (0x2E08, 0x2C74), (0x2DA5, 0x2CDA), (0x2D40, 0x2D40), (0x2CDA, 0x2DA5),
    (0x2C74, 0x2E08), (0x2C0D, 0x2E6B), (0x2BA5, 0x2ECD), (0x2B3C, 0x2F2E),
    (0x2AD2, 0x2F8E), (0x2A67, 0x2FEE), (0x29FC, 0x304C), (0x298F, 0x30A9),
    (0x2922, 0x3106), (0x28B4, 0x3161), (0x2846, 0x31BB), (0x27D6, 0x3215),
    (0x2766, 0x326D), (0x26F5, 0x32C5), (0x2683, 0x331C), (0x2610, 0x3371),
    (0x259D, 0x33C6), (0x2529, 0x3419), (0x24B4, 0x346C), (0x243F, 0x34BD),
    (0x23C9, 0x350E), (0x2352, 0x355D), (0x22DA, 0x35AB), (0x2262, 0x35F9),
    (0x21E9, 0x3645), (0x2170, 0x3690), (0x20F5, 0x36DA), (0x207B, 0x3724),
    (0x1FFF, 0x376C), (0x1F83, 0x37B3), (0x1F06, 0x37F8), (0x1E89, 0x383D),
    (0x1E0B, 0x3881), (0x1D8C, 0x38C3), (0x1D0D, 0x3905), (0x1C8E, 0x3945),
    (0x1C0D, 0x3984), (0x1B8D, 0x39C3), (0x1B0B, 0x3A00), (0x1A89, 0x3A3B),
    (0x1A07, 0x3A76), (0x1984, 0x3AB0), (0x1901, 0x3AE8), (0x187D, 0x3B1F),
    (0x17F9, 0x3B56), (0x1774, 0x3B8B), (0x16EF, 0x3BBE), (0x1669, 0x3BF1),
    (0x15E3, 0x3C22), (0x155C, 0x3C53), (0x14D5, 0x3C82), (0x144E, 0x3CB0),
    (0x13C6, 0x3CDD), (0x133E, 0x3D08), (0x12B5, 0x3D33), (0x122D, 0x3D5C),
    (0x11A3, 0x3D84), (0x111A, 0x3DAB), (0x1090, 0x3DD0), (0x1005, 0x3DF5),
    (0x0F7B, 0x3E18), (0x0EF0, 0x3E3A), (0x0E65, 0x3E5B), (0x0DD9, 0x3E7A),
    (0x0D4E, 0x3E98), (0x0CC2, 0x3EB6), (0x0C36, 0x3ED1), (0x0BA9, 0x3EEC),
    (0x0B1C, 0x3F06), (0x0A8F, 0x3F1E), (0x0A02, 0x3F35), (0x0975, 0x3F4B),
    (0x08E8, 0x3F5F), (0x085A, 0x3F72), (0x07CC, 0x3F84), (0x073E, 0x3F95),
    (0x06B0, 0x3FA5), (0x0622, 0x3FB3), (0x0593, 0x3FC0), (0x0505, 0x3FCC),
    (0x0476, 0x3FD7), (0x03E8, 0x3FE0), (0x0359, 0x3FE8), (0x02CA, 0x3FEF),
    (0x023B, 0x3FF5), (0x01AC, 0x3FF9), (0x011D, 0x3FFC), (0x008E, 0x3FFE),
]


def pan_raw_to_norm_989_clef(pan_raw: int) -> float:
    pan = int(pan_raw) & 0xFFFF
    if pan & 0x8000:
        pan -= 0x10000
    pan %= 360
    total_pan = pan
    if total_pan >= 270:
        total_pan -= 270
    else:
        total_pan += 90
    if total_pan < 180:
        l = SBNK_PAN_TABLE[total_pan][0] / 0x3FFF
        r = SBNK_PAN_TABLE[total_pan][1] / 0x3FFF
    else:
        l = SBNK_PAN_TABLE[total_pan - 180][1] / 0x3FFF
        r = SBNK_PAN_TABLE[total_pan - 180][0] / 0x3FFF
    theta = math.atan2(r, l)
    x = (theta / (math.pi / 2.0)) * 2.0 - 1.0
    return max(-1.0, min(1.0, x))


SBNK_PAN_SAMPLES = [
    ("3D01", 2500, 1086),
    ("2A00", 1108, 2491),
    ("5901", 2162, 1659),
    ("0000", 1927, 1927),
    ("0E00", 1678, 2148),
    ("1C00", 1404, 2337),
    ("2F01", 2614, 774),
    ("7200", 565, 2667),
    ("0500", 1841, 2010),
    ("1600", 1524, 2260),
    ("6201", 2026, 1824),
    ("0F00", 1659, 2162),
    ("4D01", 2324, 1424),
    ("0800", 1788, 2057),
    ("2101", 2689, 449),
]


def parse_pan_array_hex(text: str) -> int:
    token = text.strip().lower().replace("0x", "")
    if len(token) != 4:
        return int(token, 16)
    lo = int(token[0:2], 16) & 0xFF
    hi = int(token[2:4], 16) & 0xFF
    return lo | (hi << 8)


def build_sbnk_pan_map():
    sample_map = {}
    for pan_hex, l, r in SBNK_PAN_SAMPLES:
        pan_raw = parse_pan_array_hex(pan_hex)
        lo = pan_raw & 0xFF
        hi = (pan_raw >> 8) & 0xFF
        theta = math.atan2(r, l)
        x = (theta / (math.pi / 2.0)) * 2.0 - 1.0
        sample_map.setdefault(hi, []).append((lo, x))
    for hi in sample_map:
        sample_map[hi].sort(key=lambda t: t[0])
    return sample_map


SBNK_PAN_MAP = build_sbnk_pan_map()


def interpolate_pan_x(sample_map, hi, lo):
    if hi not in sample_map or not sample_map[hi]:
        return (lo / 127.0)
    points = sample_map[hi]
    if lo <= points[0][0]:
        return points[0][1]
    if lo >= points[-1][0]:
        return points[-1][1]
    for i in range(1, len(points)):
        lo0, x0 = points[i - 1]
        lo1, x1 = points[i]
        if lo0 <= lo <= lo1:
            if lo1 == lo0:
                return x0
            t = (lo - lo0) / (lo1 - lo0)
            return x0 + t * (x1 - x0)
    return points[-1][1]


def pan_raw_to_norm_989(pan_raw: int) -> float:
    lo = pan_raw & 0xFF
    hi = (pan_raw >> 8) & 0xFF
    return max(-1.0, min(1.0, interpolate_pan_x(SBNK_PAN_MAP, hi, lo)))


def absolute_rate_to_timecents(
    raw_rate: int,
    min_seconds: float,
    max_seconds: float,
    higher_is_slower: bool,
    shape: float,
):
    raw = max(0, min(255, int(raw_rate)))
    if min_seconds <= 0 or max_seconds <= 0 or min_seconds >= max_seconds:
        return 0
    norm = raw / 255.0
    if not higher_is_slower:
        norm = 1.0 - norm
    # Shape > 1.0 reduces aggressive long tails; < 1.0 extends them.
    sh = max(0.1, float(shape))
    norm = norm**sh
    t = min_seconds * ((max_seconds / min_seconds) ** norm)
    return int(round(1200.0 * math.log2(t)))


def simulate_spu_release_seconds(
    adsr1_raw: int,
    adsr2_raw: int,
    sample_rate: int = 44100,
    max_seconds: float = 40.000,
    start_level_mode: str = "full",
):
    # Matches 989snd-clef envelope.cpp Release path:
    # m_Exp=ReleaseExp, m_Shift=ReleaseShift, m_Step=-8, decrease=true.
    # Uses sustain level from ADSR1 when requested.
    release_shift = adsr2_raw & 0x1F
    release_exp = ((adsr2_raw >> 5) & 0x1) != 0
    sustain_level = ((adsr1_raw & 0x0F) + 1) * 0x800
    sustain_increase = ((adsr2_raw >> 14) & 0x1) != 0
    if start_level_mode == "sustain":
        level = max(0, min(0x7FFF, sustain_level))
    elif start_level_mode == "auto":
        # If sustain is increasing, key-off usually occurs near full level.
        # If sustain is decreasing, start from sustain level target.
        level = 0x7FFF if sustain_increase else max(0, min(0x7FFF, sustain_level))
    else:
        level = 0x7FFF
    if level <= 0:
        return 0.0

    step_base = -8 << max(0, 11 - release_shift)
    c_step = 0x800000
    shift = release_shift - 11
    if shift > 0:
        c_step >>= shift

    max_ticks = int(sample_rate * max_seconds)
    counter = 0
    ticks = 0
    while level > 0 and ticks < max_ticks:
        counter += c_step
        if counter >= 0x800000:
            if release_exp:
                step = (step_base * level) // 0x8000
            else:
                step = step_base
            level += step
            if level < 0:
                level = 0
            counter = 0
        ticks += 1

    return min(max_seconds, ticks / float(sample_rate))


def simulate_spu_sustain_decay_seconds(
    adsr1_raw: int,
    adsr2_raw: int,
    sample_rate: int = 44100,
    max_seconds: float = 40.000,
):
    # Approximate PSX SPU sustain phase while key is held.
    # This path is used to mimic in-game "loop keeps playing but fades out" behavior.
    sustain_level = ((adsr1_raw & 0x0F) + 1) * 0x800
    level = max(0, min(0x7FFF, sustain_level))
    if level <= 0:
        return 0.0

    sustain_shift = (adsr2_raw >> 8) & 0x1F
    sustain_step_code = (adsr2_raw >> 6) & 0x03
    sustain_exp = ((adsr2_raw >> 15) & 0x1) != 0
    # In these SShd banks, bit14=1 aligns with sustain-down behavior observed in-game.
    sustain_decrease = ((adsr2_raw >> 14) & 0x1) != 0
    if not sustain_decrease:
        return None

    step_base = (-8 + sustain_step_code) << max(0, 11 - sustain_shift)
    if step_base >= 0:
        return None

    c_step = 0x800000
    shift = sustain_shift - 11
    if shift > 0:
        c_step >>= shift

    max_ticks = int(sample_rate * max_seconds)
    counter = 0
    ticks = 0
    while level > 0 and ticks < max_ticks:
        counter += c_step
        if counter >= 0x800000:
            if sustain_exp:
                step = (step_base * level) // 0x8000
            else:
                step = step_base
            level += step
            if level < 0:
                level = 0
            counter = 0
        ticks += 1

    return min(max_seconds, ticks / float(sample_rate))
def build_art1_connections(
    layer,
    adsr_mode: str,
    release_min_seconds: float,
    release_max_seconds: float,
    release_higher_is_slower: bool,
    release_shape: float,
    release_model: str,
    spu_release_start_level: str,
    spu_release_scale: float,
    spu_release_shape: float,
    spu_cal_raw_a: int,
    spu_cal_seconds_a: float,
    spu_cal_raw_b: int,
    spu_cal_seconds_b: float,
    reverb_release_coupling: float,
    reverb_release_power: float,
    reverb_release_threshold: int,
    pan_conn_range: float,
    pan_invert: bool,
    use_adsr2: bool,
):
    # DLS destination constants.
    CONN_DST_PAN = 0x0004
    CONN_DST_EG1_ATTACKTIME = 0x0206
    CONN_DST_EG1_DECAYTIME = 0x0207
    CONN_DST_EG1_SUSTAINLEVEL = 0x020A
    CONN_DST_EG1_RELEASETIME = 0x0209

    connections = []
    pan_scale = int(round(layer["pan_norm"] * 100.0 * 65536.0))
    if pan_invert:
        pan_scale = -pan_scale
    # DLS pan connection uses a much wider scale than +/-100 in many players.
    pan_scale = int(round((pan_scale / (100.0 * 65536.0)) * pan_conn_range * 65536.0))
    connections.append((CONN_DST_PAN, pan_scale))

    if adsr_mode != "off":
        atk_tc = attack_to_timecents_for_layer(layer)
        rel_raw = int(layer.get("release_adsr_byte", layer.get("release_rate", 0))) & 0xFF
        dec_raw = int(layer.get("sustain_rate", 0)) & 0xFF
        sus_lvl = int(layer.get("sustain_level", -1))
        sustain_percent_override = None
        if "adsr1_raw" in layer:
            adsr1_lo = int(layer["adsr1_raw"]) & 0xFF
            # SShd/PSX-style ADSR1 low byte packs sustain-level nibble + decay shift bucket.
            sus_lvl = adsr1_lo & 0x0F
            decay_shift = ((adsr1_lo >> 4) + 24) & 0x1F
            dec_raw = int(round((decay_shift / 31.0) * 255.0))
        dec_tc = rate_to_timecents(dec_raw, min_seconds=0.005, max_seconds=12.0)
        if (
            release_model in ("spu_sim", "spu_cal")
            and "adsr1_raw" in layer
            and "adsr2_raw" in layer
        ):
            adsr1_raw = int(layer["adsr1_raw"])
            adsr2_raw = int(layer["adsr2_raw"])
            # 989 banks need full ADSR2 low byte interpretation for release.
            use_adsr2_effective = use_adsr2 or ("pan_raw_16" in layer)
            if use_adsr2_effective:
                # Preserve full ADSR2 low byte so release shift + release mode are retained.
                adsr2_raw = (adsr2_raw & ~0xFF) | rel_raw
            else:
                # Legacy behavior: only 5-bit release bucket.
                adsr2_raw = (adsr2_raw & ~0x3F) | (rel_raw & 0x1F)

            # If sustain is a "down" slope, emulate held-note fade by decaying to zero.
            # This is the in-game behavior seen in some LoD looping drum layers.
            if use_adsr2_effective:
                sustain_step_code = (adsr2_raw >> 6) & 0x03
                # Restrict auto sustain-fade to the steeper PSX sustain cases.
                # Step codes 2/3 in these banks are often long-held content and
                # should not be forced to fade to zero.
                if sustain_step_code <= 1:
                    sustain_fade_seconds = simulate_spu_sustain_decay_seconds(
                        adsr1_raw,
                        adsr2_raw,
                        sample_rate=44100,
                        max_seconds=release_max_seconds,
                    )
                    if sustain_fade_seconds is not None:
                        # DLS players tend to realize EG1 decay much faster than SPU
                        # for this sustain-down case. Stretch target time so audible
                        # fade duration better matches in-game tails.
                        sustain_fade_seconds *= 8.0
                        sustain_fade_seconds = max(0.005, min(release_max_seconds, sustain_fade_seconds))
                        dec_tc = int(round(1200.0 * math.log2(sustain_fade_seconds)))
                        sustain_percent_override = 0.0

            release_start_mode = spu_release_start_level
            if "pan_raw_16" in layer and release_start_mode == "full":
                release_start_mode = "auto"
            rel_seconds = simulate_spu_release_seconds(
                adsr1_raw,
                adsr2_raw,
                sample_rate=44100,
                max_seconds=release_max_seconds,
                start_level_mode=release_start_mode,
            )
            if release_model == "spu_cal":
                # Per-layer calibration using two anchor release byte targets.
                # This reduces exaggerated jumps (e.g. nearby high raw values diverging too much).
                low = max(0, min(255, int(spu_cal_raw_a)))
                high = max(0, min(255, int(spu_cal_raw_b)))
                ta = max(release_min_seconds, float(spu_cal_seconds_a))
                tb = max(release_min_seconds, float(spu_cal_seconds_b))
                if low != high and ta > 0 and tb > 0:
                    adsr2_hi = adsr2_raw & 0xFF00
                    base_a = simulate_spu_release_seconds(
                        adsr1_raw,
                        adsr2_hi | low,
                        sample_rate=44100,
                        max_seconds=release_max_seconds,
                        start_level_mode=release_start_mode,
                    )
                    base_b = simulate_spu_release_seconds(
                        adsr1_raw,
                        adsr2_hi | high,
                        sample_rate=44100,
                        max_seconds=release_max_seconds,
                        start_level_mode=release_start_mode,
                    )
                    if base_a > 0 and base_b > 0 and abs(base_b - base_a) > 1e-12:
                        ratio_base = base_b / base_a
                        ratio_target = tb / ta
                        if ratio_base > 0 and ratio_target > 0:
                            power = math.log(ratio_target) / math.log(ratio_base)
                            gain = ta / (base_a ** power)
                            rel_seconds = gain * (rel_seconds ** power)

            rel_seconds *= max(0.1, float(spu_release_scale))
            rel_seconds = max(release_min_seconds, min(release_max_seconds, rel_seconds))
            # Optional log-domain shaping to reduce large jumps at the long-release end.
            rel_shape = max(0.1, float(spu_release_shape))
            if rel_shape != 1.0:
                min_s = max(1e-6, float(release_min_seconds))
                max_s = max(min_s * 1.0001, float(release_max_seconds))
                log_min = math.log2(min_s)
                log_max = math.log2(max_s)
                denom = log_max - log_min
                if denom > 0:
                    n = (math.log2(rel_seconds) - log_min) / denom
                    n = max(0.0, min(1.0, n))
                    n = n**rel_shape
                    rel_seconds = 2.0 ** (log_min + n * denom)
            rel_tc = int(round(1200.0 * math.log2(rel_seconds)))
        else:
            # Fallback mode uses 5-bit release bucket behavior (0x00..0x1F).
            # Convert to 0..255 domain for existing absolute mapping utility.
            rel_spu = rel_raw & 0x1F
            rel_255 = int(round((rel_spu / 31.0) * 255.0))
            rel_tc = absolute_rate_to_timecents(
                rel_255,
                min_seconds=release_min_seconds,
                max_seconds=release_max_seconds,
                higher_is_slower=release_higher_is_slower,
                shape=release_shape,
            )

        # Sustain level heuristic:
        # negative values are commonly "full sustain" in these banks.
        if sustain_percent_override is not None:
            sustain_percent = sustain_percent_override
        elif sus_lvl < 0:
            sustain_percent = 100.0
        elif 0 <= sus_lvl <= 15:
            sustain_percent = (sus_lvl / 15.0) * 100.0
        else:
            sustain_percent = max(0.0, min(100.0, (sus_lvl / 127.0) * 100.0))
        # DLS uses 0.1% units for sustain level connection.
        sus_scale = int(round((sustain_percent * 10.0) * 65536.0))

        if atk_tc is not None:
            connections.append((CONN_DST_EG1_ATTACKTIME, atk_tc * 65536))
        connections.append((CONN_DST_EG1_DECAYTIME, dec_tc * 65536))
        connections.append((CONN_DST_EG1_SUSTAINLEVEL, sus_scale))
        connections.append((CONN_DST_EG1_RELEASETIME, rel_tc * 65536))

    art1_data = struct.pack("<II", 8, len(connections))
    for dst, scale in connections:
        art1_data += struct.pack(
            "<HHHHi",
            0,  # usSource = none
            0,  # usControl = none
            dst,
            0,  # usTransform = none
            int(scale),
        )
    return list_chunk(b"lart", chunk(b"art1", art1_data))


def build_region_layer(
    layer,
    sample_index,
    loop_info,
    region_list_type: bytes,
    adsr_mode: str,
    release_min_seconds: float,
    release_max_seconds: float,
    release_higher_is_slower: bool,
    release_shape: float,
    release_model: str,
    spu_release_start_level: str,
    spu_release_scale: float,
    spu_release_shape: float,
    spu_cal_raw_a: int,
    spu_cal_seconds_a: float,
    spu_cal_raw_b: int,
    spu_cal_seconds_b: float,
    reverb_release_coupling: float,
    reverb_release_power: float,
    reverb_release_threshold: int,
    pan_conn_range: float,
    pan_invert: bool,
    wlnk_channel: int,
    use_adsr2: bool,
):
    rgnh_data = struct.pack(
        "<HHHHHH",
        layer["min_key"],
        layer["max_key"],
        0,  # vel low
        127,  # vel high
        0x0001,  # fusOptions (SELFNONEXCLUSIVE: allow same-note overlap)
        0,  # usKeyGroup
    )
    rgnh = chunk(b"rgnh", rgnh_data)

    wsmp = chunk(
        b"wsmp",
        build_wsmp(
            layer["root_key"],
            layer["fine_cents"],
            layer["attenuation_db"],
            loop_info,
        ),
    )

    wlnk_data = struct.pack(
        "<HHII",
        0,  # fusOptions
        0,  # usPhaseGroup
        wlnk_channel,  # ulChannel
        sample_index,  # ulTableIndex
    )
    wlnk = chunk(b"wlnk", wlnk_data)

    lart = build_art1_connections(
        layer,
        adsr_mode,
        release_min_seconds,
        release_max_seconds,
        release_higher_is_slower,
        release_shape,
        release_model,
        spu_release_start_level,
        spu_release_scale,
        spu_release_shape,
        spu_cal_raw_a,
        spu_cal_seconds_a,
        spu_cal_raw_b,
        spu_cal_seconds_b,
        reverb_release_coupling,
        reverb_release_power,
        reverb_release_threshold,
        pan_conn_range,
        pan_invert,
        use_adsr2,
    )

    return list_chunk(region_list_type, rgnh + wsmp + wlnk + lart)


def build_ins(
    patch,
    sample_index_by_offset,
    loop_by_offset,
    region_list_type: bytes,
    adsr_mode: str,
    release_min_seconds: float,
    release_max_seconds: float,
    release_higher_is_slower: bool,
    release_shape: float,
    release_model: str,
    spu_release_start_level: str,
    spu_release_scale: float,
    spu_release_shape: float,
    spu_cal_raw_a: int,
    spu_cal_seconds_a: float,
    spu_cal_raw_b: int,
    spu_cal_seconds_b: float,
    reverb_release_coupling: float,
    reverb_release_power: float,
    reverb_release_threshold: int,
    pan_conn_range: float,
    pan_invert: bool,
    wlnk_channel: int,
    use_adsr2: bool,
):
    region_lists = b""
    for layer in patch["layers"]:
        sample_offset = layer["sample_offset"]
        sample_index = sample_index_by_offset[sample_offset]
        loop_info = loop_by_offset.get(sample_offset)
        region_lists += build_region_layer(
            layer,
            sample_index,
            loop_info,
            region_list_type,
            adsr_mode,
            release_min_seconds,
            release_max_seconds,
            release_higher_is_slower,
            release_shape,
            release_model,
            spu_release_start_level,
            spu_release_scale,
            spu_release_shape,
            spu_cal_raw_a,
            spu_cal_seconds_a,
            spu_cal_raw_b,
            spu_cal_seconds_b,
            reverb_release_coupling,
            reverb_release_power,
            reverb_release_threshold,
            pan_conn_range,
            pan_invert,
            wlnk_channel,
            use_adsr2,
        )

    insh_data = struct.pack(
        "<III",
        len(patch["layers"]),
        0,  # bank 0 (melodic)
        patch["program"],
    )
    insh = chunk(b"insh", insh_data)
    lrgn = list_chunk(b"lrgn", region_lists)

    inam = chunk(b"INAM", f"Patch {patch['program']:03d}\x00".encode("ascii"))
    info = list_chunk(b"INFO", inam)
    return list_chunk(b"ins ", insh + lrgn + info)


def build_wave_list(
    fmt_chunk: bytes,
    data_chunk: bytes,
    sample_name: str,
    root_key: int,
    fine_cents: int,
    loop_info,
):
    wave_body = b""
    wave_body += chunk(b"fmt ", fmt_chunk)
    wave_body += chunk(b"data", data_chunk)
    # Always include sample-level wsmp so waveform root/fine tune is preserved.
    # Keep sample-level attenuation neutral; per-layer attenuation is applied in region wsmp.
    wave_body += chunk(b"wsmp", build_wsmp(root_key, fine_cents, 0.0, loop_info))
    inam = chunk(b"INAM", (sample_name + "\x00").encode("ascii"))
    wave_body += list_chunk(b"INFO", inam)
    return list_chunk(b"wave", wave_body)


def build_dls(
    metadata_path: Path,
    wav_dir: Path,
    out_path: Path,
    ptbl_offset_base: int,
    region_list_type: bytes,
    cents_step: float,
    root_shift: int,
    force_dls_rate: int,
    adsr_mode: str,
    release_min_seconds: float,
    release_max_seconds: float,
    release_higher_is_slower: bool,
    release_shape: float,
    release_model: str,
    spu_release_start_level: str,
    spu_release_scale: float,
    spu_release_shape: float,
    spu_cal_raw_a: int,
    spu_cal_seconds_a: float,
    spu_cal_raw_b: int,
    spu_cal_seconds_b: float,
    reverb_release_coupling: float,
    reverb_release_power: float,
    reverb_release_threshold: int,
    pan_conn_range: float,
    pan_invert: bool,
    pan_mode: str,
    wlnk_channel: int,
    master_volume_percent: int,
    use_adsr2: bool,
):
    patches = parse_metadata_txt(metadata_path)
    available_wav_offsets = {
        int(p.stem) for p in wav_dir.glob("*.wav") if p.stem.isdigit()
    }
    if not available_wav_offsets:
        raise ValueError(f"No WAV files found in {wav_dir}")

    master_pct = max(0, min(100, int(master_volume_percent)))
    if master_pct <= 0:
        master_attenuation_db = 96.0
    else:
        master_gain = master_pct / 100.0
        master_attenuation_db = min(96.0, max(0.0, -20.0 * math.log10(master_gain)))

    dropped_layers = 0
    # Apply patch volume as an additional gain stage (0..127 -> 0..-96 dB).
    def volume_to_atten_db(vol_raw):
        vol = max(0, min(127, int(vol_raw)))
        if vol == 0:
            return 96.0
        gain = vol / 127.0
        return min(96.0, max(0.0, -20.0 * math.log10(gain)))
    for patch in patches:
        patch_vol = patch.get("volume", 127)
        patch_attenuation_db = volume_to_atten_db(patch_vol)
        kept_layers = []
        for layer in patch["layers"]:
            if layer["sample_offset"] not in available_wav_offsets:
                dropped_layers += 1
                continue
            layer["root_key"] = max(0, min(127, layer["root_key"] + root_shift))
            layer["fine_cents"] = int(round(layer["cents"] * cents_step))
            # Volume mapping rule requested:
            # header volume 0 -> -96 dB, 127 -> 0 dB, linear in-between.
            layer_vol = max(0, min(127, int(layer.get("volume", 127))))
            # Map header volume as gain ratio, then convert to dB attenuation.
            # 127 => 0 dB, 70 => ~5.17 dB attenuation, 10 => ~22.08 dB attenuation.
            if layer_vol == 0:
                layer_attenuation_db = 96.0
            else:
                gain = layer_vol / 127.0
                layer_attenuation_db = min(96.0, max(0.0, -20.0 * math.log10(gain)))
            layer["attenuation_db"] = min(96.0, layer_attenuation_db + patch_attenuation_db + master_attenuation_db)
            # Pan mapping from header byte or raw 16-bit (989 SBNK).
            if pan_mode == "989_clef" and "pan_raw_16" in layer:
                pan_norm = pan_raw_to_norm_989_clef(int(layer["pan_raw_16"]))
            elif pan_mode == "989_table" and "pan_raw_16" in layer:
                pan_norm = pan_raw_to_norm_989(int(layer["pan_raw_16"]))
            else:
                raw_pan = int(layer.get("pan", 64))
                pan_norm = pan_byte_to_norm(raw_pan, pan_mode)
            layer["pan_norm"] = max(-1.0, min(1.0, pan_norm))
            kept_layers.append(layer)
        patch["layers"] = kept_layers

    patches = [p for p in patches if p["layers"]]
    if dropped_layers:
        print(f"Warning: dropped {dropped_layers} layer(s) with missing sample WAVs.")
    if not patches:
        raise ValueError("No valid patches remain after filtering missing sample WAVs.")

    all_offsets = sorted({layer["sample_offset"] for p in patches for layer in p["layers"]})
    wav_by_offset = {}
    for offset in all_offsets:
        wav_path = wav_dir / f"{offset}.wav"
        if not wav_path.exists():
            raise FileNotFoundError(f"Missing WAV for offset {offset}: {wav_path}")
        wav_by_offset[offset] = wav_path

    sample_index_by_offset = {off: idx for idx, off in enumerate(all_offsets)}
    tuning_by_offset = {}
    for patch in patches:
        for layer in patch["layers"]:
            off = layer["sample_offset"]
            fine_cents = int(round(layer["cents"] * cents_step))
            tuning = (layer["root_key"], fine_cents)
            existing = tuning_by_offset.get(off)
            if existing is None:
                tuning_by_offset[off] = tuning
            elif existing != tuning:
                # Keep parsing deterministic if a future bank has collisions.
                # Choose the first encountered tuning.
                pass

    wave_offsets = []
    loop_by_offset = {}
    wave_pool_data = b""

    for off in all_offsets:
        fmt_chunk, data_chunk, smpl_chunk = read_wav_for_dls(wav_by_offset[off])
        if force_dls_rate:
            fmt_chunk = override_fmt_sample_rate(fmt_chunk, force_dls_rate)
        loop_info = extract_loop_from_smpl(smpl_chunk)
        loop_info = refine_loop_for_dls(fmt_chunk, data_chunk, loop_info)
        loop_by_offset[off] = loop_info

        root_key, fine_cents = tuning_by_offset[off]
        wave_chunk = build_wave_list(
            fmt_chunk,
            data_chunk,
            str(off),
            root_key,
            fine_cents,
            loop_info,
        )
        wave_offsets.append(len(wave_pool_data) + ptbl_offset_base)
        wave_pool_data += wave_chunk

    colh = chunk(b"colh", struct.pack("<I", len(patches)))

    lins_body = b""
    for patch in patches:
        lins_body += build_ins(
            patch,
            sample_index_by_offset,
            loop_by_offset,
            region_list_type,
            adsr_mode,
            release_min_seconds,
            release_max_seconds,
            release_higher_is_slower,
            release_shape,
            release_model,
            spu_release_start_level,
            spu_release_scale,
            spu_release_shape,
            spu_cal_raw_a,
            spu_cal_seconds_a,
            spu_cal_raw_b,
            spu_cal_seconds_b,
            reverb_release_coupling,
            reverb_release_power,
            reverb_release_threshold,
            pan_conn_range,
            pan_invert,
            wlnk_channel,
            use_adsr2,
        )
    lins = list_chunk(b"lins", lins_body)

    ptbl_data = struct.pack("<II", 8, len(all_offsets))
    for off in wave_offsets:
        ptbl_data += struct.pack("<I", off)
    ptbl = chunk(b"ptbl", ptbl_data)

    wvpl = list_chunk(b"wvpl", wave_pool_data)

    info = list_chunk(
        b"INFO",
        chunk(b"INAM", (out_path.stem + "\x00").encode("ascii"))
        + chunk(b"ISFT", b"3-makedls.py\x00"),
    )

    dls_payload = b"DLS " + colh + lins + ptbl + wvpl + info
    riff = b"RIFF" + struct.pack("<I", len(dls_payload)) + dls_payload
    out_path.write_bytes(riff)

    print(f"Wrote {out_path}")
    print(f"Instruments: {len(patches)}")
    print(f"Samples: {len(all_offsets)}")
    print(f"ptbl offset base: {ptbl_offset_base}")
    print(f"region list type: {region_list_type.decode('ascii', errors='replace')}")
    print(f"cents step: {cents_step}")
    print(f"root shift: {root_shift}")
    print(f"forced dls rate: {force_dls_rate if force_dls_rate else 'none'}")
    print(f"adsr mode: {adsr_mode}")
    print(f"pan conn range: {pan_conn_range}")
    print(f"pan invert: {pan_invert}")
    print(f"pan mode: {pan_mode}")
    print(f"wlnk channel: {wlnk_channel}")
    print(f"master volume percent: {master_pct}")
    print(f"master attenuation db: {master_attenuation_db:.3f}")
    print(f"use adsr2: {use_adsr2}")
    if adsr_mode != "off":
        print(f"release seconds range: {release_min_seconds}..{release_max_seconds}")
        print(f"release higher is slower: {release_higher_is_slower}")
        print(f"release shape: {release_shape}")
        print(f"release model: {release_model}")
        print(f"spu release start: {spu_release_start_level}")
        print(f"spu release scale: {spu_release_scale}")
        print(f"spu release shape: {spu_release_shape}")
        print("reverb->release coupling: disabled")
        if release_model == "spu_cal":
            print(f"spu cal anchors: {spu_cal_raw_a}->{spu_cal_seconds_a}s, {spu_cal_raw_b}->{spu_cal_seconds_b}s")


def main():
    parser = argparse.ArgumentParser(description="Build a DLS bank from Ape Escape metadata + extracted WAVs.")
    parser.add_argument("--metadata", default=str(PROJECT_ROOT / "data" / "header_temp" / "s_02_horn_melody.txt"))
    parser.add_argument("--wav-dir", default=str(PROJECT_ROOT / "data" / "wav_temp" / "s_02_horn_melody"))
    parser.add_argument("--out", default=str(PROJECT_ROOT / "data" / "dls_output" / "s_02_horn_melody.dls"))
    parser.add_argument(
        "--ptbl-offset-base",
        type=int,
        default=0,
        choices=[0, 4, 8],
        help="Offset base added to each ptbl entry.",
    )
    parser.add_argument(
        "--region-list",
        default="rgn2",
        choices=["rgn", "rgn2"],
        help="Use DLS1-style rgn or rgn2.",
    )
    parser.add_argument(
        "--profile",
        default="awave",
        choices=["awave", "fl"],
        help="Preset for known loader behavior. awave=rgn2/base0, fl=rgn/base4.",
    )
    parser.add_argument(
        "--cents-step",
        type=float,
        default=6.25,
        help="Multiplier for TXT fine byte to DLS cents. Ape SShd appears to use 6.25 cents per step.",
    )
    parser.add_argument(
        "--root-shift",
        type=int,
        default=0,
        help="Semitone shift applied to all root keys (use -12 if bank sounds one octave low).",
    )
    parser.add_argument(
        "--force-dls-rate",
        type=int,
        default=0,
        help="Override nSamplesPerSec in DLS wave fmt chunks (0 keeps source WAV rate).",
    )
    parser.add_argument(
        "--adsr-mode",
        default="off",
        choices=["off", "basic"],
        help="Experimental envelope conversion from header fields.",
    )
    parser.add_argument(
        "--release-min-seconds",
        type=float,
        default=0.01,
        help="Fastest release time when ADSR mode is enabled.",
    )
    parser.add_argument(
        "--release-max-seconds",
        type=float,
        default=40.000,
        help="Slowest release time when ADSR mode is enabled.",
    )
    parser.add_argument(
        "--release-higher-is-slower",
        action="store_true",
        help="Interpret higher release byte values as slower/longer releases.",
    )
    parser.add_argument(
        "--release-shape",
        type=float,
        default=1.6,
        help="Curve shaping for release mapping (>1 shortens long tails; <1 lengthens).",
    )
    parser.add_argument(
        "--release-model",
        default="spu_sim",
        choices=["spu_sim", "spu_cal", "absolute"],
        help="Release conversion model: SPU ADSR simulation or absolute byte mapping.",
    )
    parser.add_argument(
        "--spu-release-start-level",
        default="full",
        choices=["full", "sustain"],
        help="SPU sim release start level.",
    )
    parser.add_argument(
        "--spu-release-scale",
        type=float,
        default=1.0,
        help="Global multiplier for SPU-sim release duration.",
    )
    parser.add_argument(
        "--spu-release-shape",
        type=float,
        default=1.0,
        help="Log-domain shaping for SPU release times (>1 reduces long-tail jumps).",
    )
    parser.add_argument("--spu-cal-raw-a", type=int, default=206)
    parser.add_argument("--spu-cal-seconds-a", type=float, default=4.0)
    parser.add_argument("--spu-cal-raw-b", type=int, default=238)
    parser.add_argument("--spu-cal-seconds-b", type=float, default=5.0)
    parser.add_argument("--reverb-release-coupling", type=float, default=0.0)
    parser.add_argument("--reverb-release-power", type=float, default=1.0)
    parser.add_argument("--reverb-release-threshold", type=int, default=96)
    parser.add_argument(
        "--pan-conn-range",
        type=float,
        default=500.0,
        help="Pan connection range in DLS units (higher gives stronger L/R separation).",
    )
    parser.add_argument(
        "--pan-invert",
        action="store_true",
        help="Invert pan direction if left/right are swapped in the target player.",
    )
    parser.add_argument(
        "--pan-mode",
        default="psx127",
        choices=["psx127", "signed_byte", "989_table", "989_clef"],
        help="Pan byte interpretation mode.",
    )
    parser.add_argument(
        "--wlnk-channel",
        type=int,
        default=1,
        help="wlnk ulChannel value. Awave FL-converted files commonly use 1.",
    )
    parser.add_argument(
        "--master-volume-percent",
        type=int,
        default=75,
        help="Global volume scaler in percent (0..100). 100 keeps layer volumes unchanged; 0 is silent.",
    )
    parser.add_argument(
        "--use-adsr2",
        action="store_true",
        help="Use full ADSR2 low byte (release mode + shift) instead of only 5-bit release bucket.",
    )
    args = parser.parse_args()

    if args.profile == "awave":
        effective_region = "rgn2"
        effective_base = 0
    else:
        effective_region = "rgn"
        effective_base = 4

    # Explicit switches override profile defaults.
    if "--region-list" in sys.argv:
        effective_region = args.region_list
    if "--ptbl-offset-base" in sys.argv:
        effective_base = args.ptbl_offset_base

    build_dls(
        metadata_path=Path(args.metadata),
        wav_dir=Path(args.wav_dir),
        out_path=Path(args.out),
        ptbl_offset_base=effective_base,
        region_list_type=(b"rgn " if effective_region == "rgn" else b"rgn2"),
        cents_step=args.cents_step,
        root_shift=args.root_shift,
        force_dls_rate=args.force_dls_rate,
        adsr_mode=args.adsr_mode,
        release_min_seconds=args.release_min_seconds,
        release_max_seconds=args.release_max_seconds,
        release_higher_is_slower=args.release_higher_is_slower,
        release_shape=args.release_shape,
        release_model=args.release_model,
        spu_release_start_level=args.spu_release_start_level,
        spu_release_scale=args.spu_release_scale,
        spu_release_shape=args.spu_release_shape,
        spu_cal_raw_a=args.spu_cal_raw_a,
        spu_cal_seconds_a=args.spu_cal_seconds_a,
        spu_cal_raw_b=args.spu_cal_raw_b,
        spu_cal_seconds_b=args.spu_cal_seconds_b,
        reverb_release_coupling=args.reverb_release_coupling,
        reverb_release_power=args.reverb_release_power,
        reverb_release_threshold=args.reverb_release_threshold,
        pan_conn_range=args.pan_conn_range,
        pan_invert=args.pan_invert,
        pan_mode=args.pan_mode,
        wlnk_channel=args.wlnk_channel,
        master_volume_percent=args.master_volume_percent,
        use_adsr2=args.use_adsr2,
    )


if __name__ == "__main__":
    main()
















