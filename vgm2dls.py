import json
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


ROOT = Path(__file__).resolve().parent
TEMPLATES_ROOT = ROOT / "templates"
OUTPUT_ROOT = ROOT / "output"
VGMSTREAM_CLI = ROOT / "tools" / "vgmstream" / "vgmstream-cli.exe"


def load_templates():
    templates = []
    for manifest in TEMPLATES_ROOT.rglob("template.json"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["_manifest_path"] = manifest
            templates.append(data)
        except Exception:
            continue
    templates.sort(key=lambda x: x.get("display_name", x.get("id", "")))
    return templates


class Vgm2DlsApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("vgm2dls")
        self.geometry("900x560")
        self.templates = load_templates()
        self.input_dir = tk.StringVar(value=str(ROOT / "input"))
        self.template_name = tk.StringVar()
        self.template_description = tk.StringVar(value="Select a template to see details.")
        self.sample_rate = tk.StringVar(value="")
        self.master_volume_percent = tk.StringVar(value="100")
        self.use_adsr2 = tk.BooleanVar(value=False)
        self.log_header_txt = tk.BooleanVar(value=False)
        self.log_wav_data = tk.BooleanVar(value=False)
        self.sample_rate_default_text = tk.StringVar(value="Template default: (select template)")
        self._vgmstream_warned_template_ids = set()
        self._build_ui()
        self._set_initial_state()
        self._ensure_default_input_dir()

    def _build_ui(self):
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        row1 = ttk.Frame(frame)
        row1.pack(fill="x", pady=(0, 8))
        ttk.Label(row1, text="Input Folder:").pack(side="left")
        ttk.Entry(row1, textvariable=self.input_dir).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row1, text="Browse...", command=self._browse_input).pack(side="left")

        row2 = ttk.Frame(frame)
        row2.pack(fill="x", pady=(0, 8))
        ttk.Label(row2, text="Template:").pack(side="left")
        self.template_combo = ttk.Combobox(
            row2,
            textvariable=self.template_name,
            values=[t.get("display_name", t.get("id", "")) for t in self.templates],
            state="readonly",
            width=50,
        )
        self.template_combo.pack(side="left", padx=8)
        self.template_combo.bind("<<ComboboxSelected>>", self._on_template_change)
        ttk.Checkbutton(row2, text="Log Header TXT", variable=self.log_header_txt).pack(side="left", padx=(12, 4))
        ttk.Checkbutton(row2, text="Log WAV Data", variable=self.log_wav_data).pack(side="left", padx=(4, 0))

        row2_desc = ttk.Frame(frame)
        row2_desc.pack(fill="x", pady=(0, 8))
        ttk.Label(row2_desc, text="Description:").pack(anchor="w")
        ttk.Label(
            row2_desc,
            textvariable=self.template_description,
            justify="left",
            wraplength=860,
        ).pack(anchor="w", pady=(2, 0))

        row3 = ttk.Frame(frame)
        row3.pack(fill="x", pady=(0, 8))
        ttk.Label(row3, text="Sample Rate Override:").pack(side="left")
        validate_cmd = (self.register(self._validate_sample_rate), "%P")
        ttk.Entry(
            row3,
            textvariable=self.sample_rate,
            width=10,
            validate="key",
            validatecommand=validate_cmd,
        ).pack(side="left", padx=8)
        ttk.Label(row3, textvariable=self.sample_rate_default_text).pack(side="left")

        row4 = ttk.Frame(frame)
        row4.pack(fill="x", pady=(0, 8))
        ttk.Label(row4, text="Master Volume (%):").pack(side="left")
        vol_validate_cmd = (self.register(self._validate_master_volume), "%P")
        ttk.Entry(
            row4,
            textvariable=self.master_volume_percent,
            width=6,
            validate="key",
            validatecommand=vol_validate_cmd,
        ).pack(side="left", padx=8)
        ttk.Label(row4, text="(0..100)").pack(side="left")

        row5 = ttk.Frame(frame)
        row5.pack(fill="x", pady=(0, 8))
        self.export_button = ttk.Button(row5, text="Export", command=self._run_export, state="disabled")
        self.export_button.pack(side="left")
        ttk.Label(
            row5,
            text="Output: output/",
        ).pack(side="left", padx=12)

        psx_group = ttk.LabelFrame(frame, text="PSX")
        psx_group.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(psx_group, text="Use ADSR2", variable=self.use_adsr2).pack(
            side="left", padx=8, pady=4
        )

        ttk.Label(frame, text="Log:").pack(anchor="w")
        self.log = tk.Text(frame, wrap="word", height=24)
        self.log.pack(fill="both", expand=True)

    def _ensure_default_input_dir(self):
        try:
            Path(self.input_dir.get()).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _set_initial_state(self):
        self.template_name.set("")
        self.sample_rate.set("")
        self.master_volume_percent.set("100")
        self.use_adsr2.set(False)
        self.log_header_txt.set(False)
        self.log_wav_data.set(False)
        self.template_description.set("Select a template to see details.")
        self.sample_rate_default_text.set("Template default: (select template)")
        self.export_button.configure(state="disabled")

    def _on_template_change(self, _event=None):
        tpl = self._selected_template()
        if not tpl:
            self.sample_rate.set("")
            self.sample_rate_default_text.set("Template default: (select template)")
            self.template_description.set("Select a template to see details.")
            self.export_button.configure(state="disabled")
            return
        default_sr = tpl.get("sample_rate_default", 22043)
        try:
            default_master = int(tpl.get("master_volume_default", 100))
        except (TypeError, ValueError):
            default_master = 100
        self.sample_rate.set(str(default_sr))
        self.master_volume_percent.set(str(max(0, min(100, int(default_master)))))
        dls_defaults = tpl.get("dls_defaults", {})
        self.use_adsr2.set(bool(dls_defaults.get("use_adsr2", False)))
        self.template_description.set(tpl.get("description", "No description available."))
        self.sample_rate_default_text.set(f"Template default: {default_sr} Hz")
        self.export_button.configure(state="normal")
        self._warn_missing_vgmstream_for_template(tpl)

    def _selected_template(self):
        name = self.template_name.get()
        for t in self.templates:
            if t.get("display_name", t.get("id", "")) == name:
                return t
        return None

    def _browse_input(self):
        picked = filedialog.askdirectory(initialdir=self.input_dir.get() or str(ROOT))
        if picked:
            self.input_dir.set(picked)

    def _validate_sample_rate(self, proposed):
        if proposed == "":
            return True
        return proposed.isdigit() and len(proposed) <= 5

    def _validate_master_volume(self, proposed):
        if proposed == "":
            return True
        if not proposed.isdigit():
            return False
        if len(proposed) > 3:
            return False
        value = int(proposed)
        return 0 <= value <= 100

    def _append_log(self, text):
        self.log.insert("end", text)
        self.log.see("end")
        self.update_idletasks()

    def _has_vgmstream_cli(self):
        return VGMSTREAM_CLI.exists()

    def _template_uses_bd_wav_vgmstream(self, tpl):
        if not tpl:
            return False
        bank_ext = str(tpl.get("bank_ext", "bd")).strip().lower()
        return bank_ext in {"bd", "vb"}

    def _warn_missing_vgmstream_for_template(self, tpl):
        if not self._template_uses_bd_wav_vgmstream(tpl) or self._has_vgmstream_cli():
            return
        template_id = str(tpl.get("id", "")).strip().lower()
        if template_id in self._vgmstream_warned_template_ids:
            return
        self._vgmstream_warned_template_ids.add(template_id)
        messagebox.showwarning(
            "Missing vgmstream-cli",
            "vgmstream-cli component is missing.\n\n"
            "Please run vgmstream-get.bat to get the necessary latest components.",
        )
        self._append_log(f"[warning] Missing vgmstream-cli: {VGMSTREAM_CLI}\n")
        self._append_log("[warning] Run vgmstream-get.bat to install/update vgmstream components.\n\n")

    def _run_export(self):
        tpl = self._selected_template()
        if not tpl:
            messagebox.showerror("Template Required", "Select a template first.")
            return
        if self._template_uses_bd_wav_vgmstream(tpl) and not self._has_vgmstream_cli():
            messagebox.showerror(
                "Missing vgmstream-cli",
                "vgmstream-cli component is missing.\n\n"
                "Please run vgmstream-get.bat to get the necessary latest components.",
            )
            return

        input_root = Path(self.input_dir.get()).resolve()
        if not input_root.exists():
            messagebox.showerror("Input Missing", f"Input folder does not exist:\n{input_root}")
            return

        try:
            sr = int(self.sample_rate.get())
        except ValueError:
            messagebox.showerror("Invalid Sample Rate", "Sample rate must be an integer.")
            return
        try:
            master_vol = int(self.master_volume_percent.get())
        except ValueError:
            messagebox.showerror("Invalid Master Volume", "Master Volume (%) must be a whole number from 0 to 100.")
            return
        if master_vol < 0 or master_vol > 100:
            messagebox.showerror("Invalid Master Volume", "Master Volume (%) must be from 0 to 100.")
            return
        if master_vol == 0:
            proceed = messagebox.askyesno(
                "Master Volume is 0%",
                "0% means no audible playback. Are you sure you want to export?",
            )
            if not proceed:
                return

        self.log.delete("1.0", "end")
        self._append_log(f"Template: {tpl.get('display_name', tpl.get('id'))}\n")
        self._append_log(f"Input: {input_root}\n")
        self._append_log(f"Master Volume: {master_vol}%\n")
        self._append_log(f"PSX Use ADSR2: {self.use_adsr2.get()}\n")
        self._append_log(f"Log Header TXT: {self.log_header_txt.get()}\n")
        self._append_log(f"Log WAV Data: {self.log_wav_data.get()}\n")
        self._append_log(f"Output: {OUTPUT_ROOT}\n\n")

        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "batch_pipeline.py"),
            "--input-root",
            str(input_root),
            "--output-root",
            str(OUTPUT_ROOT),
            "--sample-rate",
            str(sr),
            "--master-volume-percent",
            str(master_vol),
            "--continue-on-error",
        ]
        header_ext = str(tpl.get("header_ext", "hd"))
        bank_ext = str(tpl.get("bank_ext", "bd"))
        parser_script = str(tpl.get("parser_script", str(ROOT / "scripts" / "extract_hd_to_txt.py")))
        header_arg = str(tpl.get("header_arg", "--hd"))
        bank_data_offset = tpl.get("bank_data_offset", 0)
        auto_skip_zeros16 = bool(tpl.get("auto_skip_zeros16", False))
        container_ext = str(tpl.get("container_ext", "")).strip()
        container_extract_script = str(tpl.get("container_extract_script", "")).strip()
        container_arg = str(tpl.get("container_arg", "--fog")).strip()
        template_id = str(tpl.get("id", "")).strip().lower()
        if template_id == "sony_989_sbnk":
            if not container_ext:
                container_ext = "fog"
            if not container_extract_script:
                container_extract_script = "scripts/extract_989_fog.py"
            if not container_arg:
                container_arg = "--fog"
        try:
            bank_data_offset = int(bank_data_offset)
        except (TypeError, ValueError):
            bank_data_offset = 0
        header_arg_flag = f"--header-arg={header_arg}"
        cmd.extend(
            [
                "--header-ext",
                header_ext,
                "--bank-ext",
                bank_ext,
                "--parser-script",
                parser_script,
                header_arg_flag,
                "--bank-data-offset",
                str(bank_data_offset),
            ]
        )
        if container_ext and container_extract_script:
            container_script_path = Path(container_extract_script)
            if not container_script_path.is_absolute():
                container_script_path = ROOT / container_script_path
            cmd.extend(
                [
                    "--container-ext",
                    container_ext,
                    "--container-extract-script",
                    str(container_script_path),
                    f"--container-arg={container_arg}",
                ]
            )
        cents_step = tpl.get("cents_step", None)
        root_shift = tpl.get("root_shift", None)
        force_dls_rate = tpl.get("force_dls_rate", None)
        try:
            if cents_step is not None:
                cmd.extend(["--cents-step", str(float(cents_step))])
        except (TypeError, ValueError):
            pass
        try:
            if root_shift is not None:
                cmd.extend(["--root-shift", str(int(root_shift))])
        except (TypeError, ValueError):
            pass
        try:
            if force_dls_rate is not None:
                cmd.extend(["--force-dls-rate", str(int(force_dls_rate))])
        except (TypeError, ValueError):
            pass
        dls_defaults = tpl.get("dls_defaults", {})
        adsr_mode = str(dls_defaults.get("adsr_mode", "off"))
        release_model = str(dls_defaults.get("release_model", "spu_sim"))
        spu_release_start = str(dls_defaults.get("spu_release_start_level", "full"))
        spu_release_scale = dls_defaults.get("spu_release_scale", 1.0)
        spu_release_shape = dls_defaults.get("spu_release_shape", 1.0)
        spu_cal_raw_a = dls_defaults.get("spu_cal_raw_a", 206)
        spu_cal_seconds_a = dls_defaults.get("spu_cal_seconds_a", 4.0)
        spu_cal_raw_b = dls_defaults.get("spu_cal_raw_b", 238)
        spu_cal_seconds_b = dls_defaults.get("spu_cal_seconds_b", 5.0)
        reverb_release_coupling = dls_defaults.get("reverb_release_coupling", 0.0)
        reverb_release_power = dls_defaults.get("reverb_release_power", 1.0)
        reverb_release_threshold = dls_defaults.get("reverb_release_threshold", 96)
        pan_mode = dls_defaults.get("pan_mode", None)
        try:
            spu_release_scale = float(spu_release_scale)
        except (TypeError, ValueError):
            spu_release_scale = 1.0
        try:
            spu_release_shape = float(spu_release_shape)
        except (TypeError, ValueError):
            spu_release_shape = 1.0
        try:
            spu_cal_raw_a = int(spu_cal_raw_a)
        except (TypeError, ValueError):
            spu_cal_raw_a = 206
        try:
            spu_cal_seconds_a = float(spu_cal_seconds_a)
        except (TypeError, ValueError):
            spu_cal_seconds_a = 4.0
        try:
            spu_cal_raw_b = int(spu_cal_raw_b)
        except (TypeError, ValueError):
            spu_cal_raw_b = 238
        try:
            spu_cal_seconds_b = float(spu_cal_seconds_b)
        except (TypeError, ValueError):
            spu_cal_seconds_b = 5.0
        try:
            reverb_release_coupling = float(reverb_release_coupling)
        except (TypeError, ValueError):
            reverb_release_coupling = 0.0
        try:
            reverb_release_power = float(reverb_release_power)
        except (TypeError, ValueError):
            reverb_release_power = 1.0
        try:
            reverb_release_threshold = int(reverb_release_threshold)
        except (TypeError, ValueError):
            reverb_release_threshold = 96
        cmd.extend(
            [
                "--adsr-mode",
                adsr_mode,
                "--release-model",
                release_model,
                "--spu-release-start-level",
                spu_release_start,
                "--spu-release-scale",
                str(spu_release_scale),
                "--spu-release-shape",
                str(spu_release_shape),
                "--spu-cal-raw-a",
                str(spu_cal_raw_a),
                "--spu-cal-seconds-a",
                str(spu_cal_seconds_a),
                "--spu-cal-raw-b",
                str(spu_cal_raw_b),
                "--spu-cal-seconds-b",
                str(spu_cal_seconds_b),
                "--reverb-release-coupling",
                str(reverb_release_coupling),
                "--reverb-release-power",
                str(reverb_release_power),
                "--reverb-release-threshold",
                str(reverb_release_threshold),
            ]
        )
        if pan_mode:
            cmd.extend(["--pan-mode", str(pan_mode)])
        if self.use_adsr2.get():
            cmd.append("--use-adsr2")
        if self.log_header_txt.get():
            cmd.append("--log-header-txt")
        if self.log_wav_data.get():
            cmd.append("--log-wav-data")
        if auto_skip_zeros16:
            cmd.append("--auto-skip-zeros16")

        def worker():
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    self.after(0, self._append_log, line)
                code = proc.wait()
                if code == 0:
                    self.after(0, lambda: messagebox.showinfo("Done", "Export completed."))
                else:
                    self.after(0, lambda: messagebox.showerror("Failed", f"Export failed (exit {code})."))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    app = Vgm2DlsApp()
    app.mainloop()




