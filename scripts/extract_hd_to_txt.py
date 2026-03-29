import argparse
import os
import struct

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_header_file(file_path, output_dir=os.path.join(PROJECT_ROOT, "data", "header_temp")):
    # Create the output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Generate the output file path
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    output_file_path = os.path.join(output_dir, base_name + ".txt")
    
    with open(file_path, 'rb') as file, open(output_file_path, 'w') as output_file:
        data = file.read()

        if data[0x0C:0x10].decode('ascii') != 'SShd':
            raise ValueError("Not a valid SShd header file")

        totalHDSize = struct.unpack('<I', data[0x00:0x04])[0]
        totalBDSize = struct.unpack('<I', data[0x04:0x08])[0]
        offsetHDTable = struct.unpack('<I', data[0x10:0x14])[0]

        output_file.write(f"Total HD Size: {hex(totalHDSize)}\n")
        output_file.write(f"Total BD Size: {hex(totalBDSize)}\n")
        output_file.write(f"HD Table Offset Location: {hex(offsetHDTable)}\n")

        # Table stores "count-1", so +1 gives the number of table entries to scan.
        patch_table_entries = struct.unpack('<H', data[offsetHDTable:offsetHDTable + 2])[0] + 1

        patch_entries = []
        skipped_ffff = 0
        skipped_oob = 0
        for i in range(patch_table_entries):
            patch_number = i + 1
            patch_offset = struct.unpack('<H', data[offsetHDTable + 2 + (i * 2):offsetHDTable + 4 + (i * 2)])[0]
            # 0xFFFF means "no patch in this slot"; keep slot numbering intact.
            if patch_offset == 0xFFFF:
                skipped_ffff += 1
                patch_entries.append((patch_number, None, patch_offset, "ffff"))
                continue
            abs_patch_offset = offsetHDTable + patch_offset
            if abs_patch_offset + 8 > len(data):
                skipped_oob += 1
                patch_entries.append((patch_number, None, patch_offset, "oob"))
                continue
            patch_entries.append((patch_number, abs_patch_offset, patch_offset, "ok"))

        # Patch numbering in txt is based on valid patch entries only.
        output_file.write(f"Patch table entries (raw): {patch_table_entries}\n")
        output_file.write(f"Patch table skipped (0xFFFF): {skipped_ffff}\n")
        output_file.write(f"Patch table skipped (out-of-range): {skipped_oob}\n")
        output_file.write(f"Number of patches: {len([e for e in patch_entries if e[1] is not None])}\n")
        
        for patch_number, patch_offset, rel_patch_offset, state in patch_entries:
            if patch_offset is None:
                output_file.write(f"\nPatch {patch_number}:\n")
                if state == "ffff":
                    output_file.write("  Skipped: True (0xFFFF patch slot)\n")
                else:
                    output_file.write("  Skipped: True (offset out-of-range)\n")
                output_file.write(f"  Relative Offset Location: {hex(rel_patch_offset)}\n")
                continue

            patch_volume = data[patch_offset + 1]
            num_layers = (data[patch_offset] & 0x7F) + 1
            output_file.write(f"\nPatch {patch_number}:\n")
            output_file.write(f"  Patch Volume: {hex(patch_volume)}\n")
            output_file.write(f"  Number of layers: {num_layers}\n")
            output_file.write(f"  Relative Offset Location: {hex(patch_offset - offsetHDTable)}\n")
            output_file.write(f"  Offset Location: {hex(patch_offset)}\n")
            output_file.write(f"  First 8 bytes after offset:\n")

            layer_offset = patch_offset + 8
            for layer_index in range(num_layers):
                if layer_offset + 16 > len(data):
                    break
                layer_data = data[layer_offset:layer_offset + 16]
                output_file.write(f"    Layer {layer_index + 1}:\n")
                output_file.write(f"      Data: {''.join([f'{byte:02X}' for byte in layer_data])}\n")
                output_file.write(f"      Min Key Range: {layer_data[0]}\n")
                output_file.write(f"      Max Key Range: {layer_data[1]}\n")
                output_file.write(f"      Root Key: {layer_data[2]}\n")
                output_file.write(f"      Cents: {struct.unpack('b', bytes([layer_data[3]]))[0]}\n")
                vb_offset = struct.unpack('<H', layer_data[4:6])[0] * 8
                output_file.write(f"      VB Offset (filename): {vb_offset}\n")
                adsr1_lo = layer_data[6]
                adsr1_hi = layer_data[7]
                sustain_level = adsr1_lo & 0x0F
                decay_shift = ((adsr1_lo >> 4) + 24) & 0x1F
                attack_rate = adsr1_hi & 0x7F
                attack_exp = 1 if (adsr1_hi & 0x80) else 0
                output_file.write(f"      Sustain Level: {sustain_level}\n")
                output_file.write(f"      Attack Rate: {attack_rate}\n")
                output_file.write(f"      Release Rate: {layer_data[8]}\n")
                output_file.write(f"      Sustain Rate: {layer_data[9]}\n")
                output_file.write(f"      ADSR1 Raw (LE): 0x{(adsr1_lo | (adsr1_hi << 8)):04X}\n")
                output_file.write(f"      ADSR1 Decay Shift (0..31): {decay_shift}\n")
                output_file.write(f"      ADSR1 Attack Exponential: {attack_exp}\n")
                adsr2_lo = layer_data[8]
                adsr2_hi = layer_data[9]
                adsr2_raw = adsr2_lo | (adsr2_hi << 8)
                output_file.write(f"      ADSR2 Raw (LE): 0x{adsr2_raw:04X}\n")
                output_file.write(f"      ADSR2 Release Shift (0..31): {adsr2_lo & 0x1F}\n")
                output_file.write(f"      ADSR2 Release Exponential: {1 if (adsr2_lo & 0x20) else 0}\n")
                output_file.write(f"      ADSR2 Sustain Step Code (0..3): {(adsr2_lo >> 6) & 0x03}\n")
                output_file.write(f"      ADSR2 Sustain Shift (0..31): {adsr2_hi & 0x1F}\n")
                output_file.write(f"      ADSR2 Sustain Decrease Flag (Dec=1/Inc=0): {1 if (adsr2_hi & 0x40) else 0}\n")
                output_file.write(f"      ADSR2 Sustain Exponential: {1 if (adsr2_hi & 0x80) else 0}\n")
                output_file.write(f"      Volume: {layer_data[11]}\n")
                output_file.write(f"      Pan: {layer_data[12]}\n")
                output_file.write(f"      Reverb: {layer_data[15]}\n")

                layer_offset += 16

    return output_file_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dump Ape Escape SShd metadata to TXT.")
    parser.add_argument("--hd", default="s_02_horn_melody.hd", help="Input .hd file")
    parser.add_argument("--out-dir", default=os.path.join(PROJECT_ROOT, "data", "header_temp"), help="Output directory for txt dump")
    args = parser.parse_args()

    out_path = read_header_file(args.hd, args.out_dir)
    print(f"Wrote metadata: {out_path}")
