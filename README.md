# vgm2dls

vgm2dls is an experimental toolkit for drafting and testing conversion of video game soundbanks into .dls soundfonts.

Status: Alpha
Current version: v0.5.0-alpha

##Features:
- Template-driven soundbank parsing
- Automatic sample extraction to WAV (via bundled tools like `vgmstream`)
- DLS generation with:
  - Regions (key/velocity ranges)
  - Loop points
  - Tuning (unity key / fine tuning)
  - Pan and attenuation
  - ADSR envelope conversion (including PS1 ADSR2 support)
- Batch processing support (CLI)
- GUI for interactive conversion

## Supported Formats

### Sony PlayStation Soundbanks

- **SShd/SSbd**
  - Used in: Ape Escape 3, Lifeline, Wild Arms 2, etc.

- **989 SBNK**
  - Used in: Jet Moto 3, Syphon Filter 2, and related Sony 989 titles.

> More formats may be added as templates are developed.

## Overview

The pipeline does as followed:
1. Parse template-defined bank metadata into txt.
2. Extract template-defined bank sample data to WAV (decode via `vgmstream`).
3. Build a `.dls` with regions, loops, tuning, pan, attenuation, and ADSR conversion.

## Requirements

- Python 3.10+
- Windows
- Bundled decoder tools in `tools/` including:
  -`vgmstream`: run vgmstream-get.bat to fetch necessary toolkit)

## GUI Usage

Open `vgm2dls.py`, then:
1. Set an input folder containing files supported by your selected template.
2. Select a template (example: `Sony - SShd/SSbd Soundbank` or `Sony - 989 SBNK Soundbank`).
3. Review the template description shown in the app and adjust optional features (`Use ADSR2`, etc.) as needed.
4. Click `Export`.

Exports are written to `output/`.

## CLI Usage

```powershell
python .\scripts\batch_pipeline.py `
  --input-root ".\input" `
  --output-root ".\output" `
  --header-ext hd `
  --bank-ext bd `
  --parser-script ".\scripts\extract_hd_to_txt.py" `
  --header-arg=--hd `
  --sample-rate 22043 `
  --master-volume-percent 50 `
  --adsr-mode basic `
  --release-model spu_sim `
  --use-adsr2 `
  --continue-on-error
```

Adjust `--header-ext`, `--bank-ext`, parser settings, and related options to match your template/format.

## ADSR Notes

- PS1 ADSR handling is currently shared across supported Sony templates in this project (including SShd/SSbd and 989 SBNK), based on observed parser behavior.
- Header `Release Rate` + `Sustain Rate` values come from ADSR2 bitfields.
- `Use ADSR2` preserves full ADSR2 low-byte release classification (mode + shift).
- Header dumps now include decoded ADSR2 fields for easier debugging.

## Scripts

- `scripts/batch_pipeline.py` - template-driven batch orchestration.
- `scripts/make_dls.py` - DLS builder.
- `scripts/ps1_adpcm_to_wav.py` - PS1 ADPCM sample extraction/decoder.
- `scripts/extract_hd_to_txt.py` - metadata parser for Sony SShd/SSbd templates.
- `scripts/extract_989_to_txt.py` - metadata parser for Sony 989 SBNK templates.
- `scripts/extract_989_fog.py` - optional container extractor for some 989 assets.

## Git Notes

`.gitignore` excludes generated output/temp/cache files so commits stay clean.


## LEGAL

All format notes and behavior in this project were gathered through research and reverse-engineering.


