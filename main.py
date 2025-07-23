#!/usr/bin/env python3
# aiconpack.py
"""
AIconPack
~~~~~~~~~
单文件三大模块：
1. IconGenerator —— 调用 OpenAI 生成应用 icon
2. PyInstallerPacker —— 调用 PyInstaller 打包可执行文件
3. AIconPackGUI —— 现代化 GUI，串联生成 + 打包
"""
from __future__ import annotations

# ────────────────────  Stdlib  ────────────────────
import base64
import io
import json
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Literal, Mapping, Optional, Sequence

# ────────────────────  3rd-party  ────────────────────
import customtkinter as ctk
import requests
from PIL import Image
from tkinter import filedialog, messagebox
from openai import OpenAI, APIConnectionError, RateLimitError

# --------------------------------------------------------------------------- #
# 1) AI 生成模块
# --------------------------------------------------------------------------- #
class IconGenerator:
    """基于 OpenAI 的图标生成封装"""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        prompt_templates: Mapping[str, str] | None = None,
        request_timeout: int = 60,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        self.templates = dict(prompt_templates or {})
        self.client = None  # 先留空，等有了 key 再建
        if self.api_key:  # 程序启动时就有 key 才创建
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        self.timeout = request_timeout
        self.max_retries = max_retries

    # ----- 模板 -----
    def add_template(self, name: str, template: str, overwrite: bool = False) -> None:
        if name in self.templates and not overwrite:
            raise ValueError(f"模板 '{name}' 已存在")
        self.templates[name] = template

    def list_templates(self) -> list[str]:
        return list(self.templates)

    # ----- 核心 -----
    def generate(
        self,
        prompt: str,
        *,
        style: str | None = None,
        extra_keywords: Sequence[str] | None = None,
        size: Literal["256x256", "512x512", "1024x1024"] = "1024x1024",
        model: str = "dall-e-3",
        n: int = 1,
        output_dir: str | Path = "icons",
        filename_prefix: str | None = None,
        return_format: Literal["path", "pil", "bytes", "b64"] = "path",
        convert_to_ico: bool = False,
    ) -> list[Any]:
        if self.client is None:  # 还没建，或稍后才填了 key
            if not self.api_key:
                raise RuntimeError("请先在『设置』里填写 OpenAI API Key")
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        full_prompt = (
            self.templates.get(style, "{prompt}").format(prompt=prompt) if style else prompt
        )
        if extra_keywords:
            full_prompt += ", " + ", ".join(extra_keywords)

        # ---- 请求 (自动重试) ----
        attempts = 0
        while True:
            try:
                rsp = self.client.images.generate(
                    model=model,
                    prompt=full_prompt,
                    n=min(max(n, 1), 10),
                    size=size,
                    response_format="url",
                )
                break
            except (APIConnectionError, RateLimitError) as e:
                attempts += 1
                if attempts > self.max_retries:
                    raise RuntimeError(f"请求失败: {e}") from e
                time.sleep(2 ** attempts)

        # ---- 下载 & 保存 ----
        out_dir = Path(output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = filename_prefix or f"icon_{ts}"

        results: list[Any] = []
        for idx, item in enumerate(rsp.data, 1):
            img_bytes = requests.get(item.url, timeout=self.timeout).content
            img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

            match return_format:
                case "pil":
                    results.append(img)
                case "bytes":
                    results.append(img_bytes)
                case "b64":
                    results.append(base64.b64encode(img_bytes).decode())
                case _:
                    name = f"{prefix}_{idx}.png" if n > 1 else f"{prefix}.png"
                    png_path = out_dir / name
                    img.save(png_path, "PNG")
                    if convert_to_ico:
                        img.resize((256, 256)).save(png_path.with_suffix(".ico"), "ICO")
                    results.append(png_path)
        return results


# --------------------------------------------------------------------------- #
# 2) 打包模块
# --------------------------------------------------------------------------- #
class PyInstallerPacker:
    """封装 PyInstaller 调用"""

    def __init__(
        self,
        *,
        onefile: bool = True,
        windowed: bool = True,
        clean: bool = True,
        debug: bool = False,
        upx: bool = False,
        pyinstaller_exe: str | None = None,
    ) -> None:
        self.onefile = onefile
        self.windowed = windowed
        self.clean = clean
        self.debug = debug
        self.upx = upx
        self.pyinstaller_exe = pyinstaller_exe or sys.executable

    # ----- 生成命令 -----
    def build_cmd(
        self,
        script_path: str | Path,
        *,
        name: str | None = None,
        icon: str | Path | None = None,
        version_file: str | Path | None = None,
        add_data: Sequence[str] | None = None,
        add_binary: Sequence[str] | None = None,
        hidden_imports: Sequence[str] | None = None,
        runtime_hooks: Sequence[str] | None = None,
        exclude_modules: Sequence[str] | None = None,
        key: str | None = None,
        dist_dir: str | Path | None = None,
        build_dir: str | Path | None = None,
        workpath: str | Path | None = None,
        spec_path: str | Path | None = None,
        extra_args: Sequence[str] | None = None,
    ) -> List[str]:
        cmd: List[str] = [self.pyinstaller_exe, "-m", "PyInstaller", str(script_path)]

        if self.onefile:
            cmd.append("--onefile")
        if self.windowed:
            cmd.append("--noconsole")
        if self.clean:
            cmd.append("--clean")
        if self.debug:
            cmd.append("--debug")
        if self.upx:
            cmd.append("--upx-dir")

        if name:
            cmd += ["--name", name]
        if icon:
            cmd += ["--icon", str(icon)]
        if version_file and platform.system() == "Windows":
            cmd += ["--version-file", str(version_file)]

        _extend_arg(cmd, "--add-data", add_data)
        _extend_arg(cmd, "--add-binary", add_binary)
        _extend_arg(cmd, "--hidden-import", hidden_imports)
        _extend_arg(cmd, "--runtime-hook", runtime_hooks)
        _extend_arg(cmd, "--exclude-module", exclude_modules)

        if key:
            cmd += ["--key", key]

        if dist_dir:
            cmd += ["--distpath", str(dist_dir)]
        if build_dir:
            cmd += ["--workpath", str(build_dir)]
        if workpath:
            cmd += ["--workpath", str(workpath)]
        if spec_path:
            cmd += ["--specpath", str(spec_path)]
        if extra_args:
            cmd += list(extra_args)

        return cmd

    # ----- 执行 -----
    def pack(self, script_path: str | Path, *, dry_run: bool = False, **kwargs):
        cmd = self.build_cmd(script_path, **kwargs)
        if dry_run:
            return cmd
        return subprocess.run(cmd, capture_output=True, text=True)

    # ----- 工具 -----
    @staticmethod
    def create_version_file(
        *,
        company_name: str = "MyCompany",
        file_description: str = "MyApplication",
        file_version: str = "1.0.0.0",
        product_name: str = "MyApplication",
        product_version: str = "1.0.0.0",
        outfile: str | Path = "version_info.txt",
    ) -> Path:
        tpl = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({file_version.replace('.', ',')}),
    prodvers=({product_version.replace('.', ',')}),
    mask=0x3f,
    flags=0x0,
    OS=0x4,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', '{company_name}'),
         StringStruct('FileDescription', '{file_description}'),
         StringStruct('FileVersion', '{file_version}'),
         StringStruct('ProductName', '{product_name}'),
         StringStruct('ProductVersion', '{product_version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
        path = Path(outfile).resolve()
        path.write_text(tpl, encoding="utf-8")
        return path


def _extend_arg(cmd: list[str], flag: str, values: Iterable[str] | None):
    if values:
        for v in values:
            cmd += [flag, str(v)]


# --------------------------------------------------------------------------- #
# 3) GUI 模块
# --------------------------------------------------------------------------- #
_CONFIG_PATH = Path.home() / ".aiconpack_config.json"


def _load_cfg() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text("utf-8"))
        except Exception:
            pass
    return {"api_key": "", "base_url": "", "templates": {}}


def _save_cfg(cfg: dict) -> None:
    _CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


class SettingsDialog(ctk.CTkToplevel):
    """设置窗口"""

    def __init__(self, master: "AIconPackGUI", cfg: dict):
        super().__init__(master)
        self.title("设置")
        self.geometry("500x520")
        self.columnconfigure(0, weight=1)
        self.cfg = cfg
        # API Key
        ctk.CTkLabel(self, text="OpenAI API Key:", anchor="w", font=("", 14)).grid(
            row=0, column=0, sticky="w", padx=20, pady=(20, 6)
        )
        self.api_entry = ctk.CTkEntry(self, placeholder_text="sk-...", show="•")
        self.api_entry.insert(0, cfg.get("api_key", ""))
        self.api_entry.grid(row=1, column=0, sticky="ew", padx=20)
        # Base URL
        ctk.CTkLabel(self, text="API Base URL (可选):", anchor="w", font=("", 14)).grid(
            row=2, column=0, sticky="w", padx=20, pady=(20, 6)
        )
        self.base_entry = ctk.CTkEntry(self, placeholder_text="https://api.xxx.com/v1")
        self.base_entry.insert(0, cfg.get("base_url", ""))
        self.base_entry.grid(row=3, column=0, sticky="ew", padx=20)
        # Templates
        ctk.CTkLabel(self, text="提示词模板 (JSON):", anchor="w", font=("", 14)).grid(
            row=4, column=0, sticky="w", padx=20, pady=(20, 6)
        )
        self.tpl_box = ctk.CTkTextbox(self, height=220)
        self.tpl_box.insert("1.0", json.dumps(cfg.get("templates", {}), ensure_ascii=False, indent=2))
        self.tpl_box.grid(row=5, column=0, sticky="nsew", padx=20)
        self.rowconfigure(5, weight=1)

        # Buttons
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=6, column=0, pady=16)
        ctk.CTkButton(btns, text="取消", width=100, command=self.destroy).grid(row=0, column=0, padx=(0, 10))
        ctk.CTkButton(btns, text="保存", width=120, command=self._save).grid(row=0, column=1)

    def _save(self):
        try:
            tpls = json.loads(self.tpl_box.get("1.0", "end").strip() or "{}")
            if not isinstance(tpls, dict):
                raise ValueError
        except Exception:
            messagebox.showerror("错误", "模板 JSON 格式不正确")
            return
        cfg = {"api_key": self.api_entry.get().strip(), "base_url": self.base_entry.get().strip(), "templates": tpls}
        _save_cfg(cfg)
        self.master.apply_settings(cfg)
        self.destroy()


class AIconPackGUI(ctk.CTk):
    """主界面"""

    def __init__(self):
        super().__init__()
        self.cfg = _load_cfg()
        ctk.set_appearance_mode("dark")
        self.title("AIconPack · 图标生成 & 打包")
        self.geometry("900x680")
        self.minsize(820, 580)

        # Services
        self._init_services()

        # Layout
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self._header()
        self._script_frame()
        self._prompt_frame()
        self._bottom()

        # State
        self.generated_icon: Path | None = None
        self.preview_img = None

    # ----- Services -----
    def _init_services(self):
        self.icon_gen = IconGenerator(
            api_key=self.cfg.get("api_key"),
            base_url=self.cfg.get("base_url"),
            prompt_templates=self.cfg.get("templates"),
        )

    # ----- Header -----
    def _header(self):
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 6))
        hdr.columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text="🪄  AIconPack", font=("Segoe UI", 28, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(hdr, text="⚙️ 设置", width=90, command=self._open_settings).grid(row=0, column=1, sticky="e")

    # ----- Script frame -----
    def _script_frame(self):
        frm = ctk.CTkFrame(self, corner_radius=12)
        frm.grid(row=1, column=0, padx=20, pady=(4, 6), sticky="ew")
        frm.columnconfigure(1, weight=1)

        ctk.CTkLabel(frm, text="主脚本:", font=("", 14)).grid(row=0, column=0, padx=(16, 8), pady=16)
        self.entry_script = ctk.CTkEntry(frm, placeholder_text="请选择要打包的 *.py")
        self.entry_script.grid(row=0, column=1, sticky="ew", pady=16)
        ctk.CTkButton(frm, text="浏览", width=80, command=self._browse_script).grid(row=0, column=2, padx=(10, 16))

        sw = ctk.CTkFrame(frm, fg_color="transparent")
        sw.grid(row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 14))
        self.opt_onefile = ctk.CTkSwitch(sw, text="单文件 (--onefile)");      self.opt_onefile.select()
        self.opt_win     = ctk.CTkSwitch(sw, text="窗口模式 (--noconsole)");  self.opt_win.select()
        self.opt_clean   = ctk.CTkSwitch(sw, text="清理临时 (--clean)");      self.opt_clean.select()
        self.opt_debug   = ctk.CTkSwitch(sw, text="调试信息 (--debug)")
        for i, s in enumerate((self.opt_onefile, self.opt_win, self.opt_clean, self.opt_debug)):
            s.grid(row=0, column=i, padx=(0, 22))

    # ----- Prompt frame -----
    def _prompt_frame(self):
        frm = ctk.CTkFrame(self, corner_radius=12)
        frm.grid(row=2, column=0, padx=20, pady=(4, 0), sticky="nsew")
        frm.columnconfigure(1, weight=1); frm.rowconfigure(1, weight=1)

        ctk.CTkLabel(frm, text="Icon Prompt:", font=("", 14)).grid(row=0, column=0, padx=(16, 8), pady=(16, 10), sticky="w")
        self.entry_prompt = ctk.CTkEntry(frm, placeholder_text="如: 极简扁平风蓝色日历应用图标")
        self.entry_prompt.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(16, 10))

        self.menu_style = ctk.CTkOptionMenu(frm, values=["(无模板)"] + self.icon_gen.list_templates())
        self.menu_style.grid(row=0, column=2, padx=(0, 8))
        self.menu_style.set("(无模板)")
        self.menu_size = ctk.CTkOptionMenu(frm, values=["256x256", "512x512", "1024x1024"]); self.menu_size.set("512x512")
        self.menu_size.grid(row=0, column=3, padx=(0, 16))

        self.btn_gen = ctk.CTkButton(frm, text="🎨 生成 Icon", width=140, command=self._start_generate)
        self.btn_gen.grid(row=0, column=4, padx=(4, 16))

        self.lbl_preview = ctk.CTkLabel(frm, text="预览区", width=480, height=360, corner_radius=8, fg_color="#1a1a1a")
        self.lbl_preview.grid(row=1, column=0, columnspan=5, sticky="nsew", padx=16, pady=(0, 16))

    # ----- Bottom -----
    def _bottom(self):
        frm = ctk.CTkFrame(self, fg_color="transparent")
        frm.grid(row=3, column=0, padx=20, pady=12, sticky="ew")
        frm.columnconfigure(0, weight=1)

        self.btn_pack = ctk.CTkButton(frm, text="📦 打包应用", height=44, state="disabled", command=self._start_pack)
        self.btn_pack.grid(row=0, column=0, sticky="ew")
        self.bar = ctk.CTkProgressBar(frm, mode="indeterminate"); self.bar.stop()
        self.bar.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        self.lbl_status = ctk.CTkLabel(frm, text="状态: 就绪", anchor="w"); self.lbl_status.grid(row=2, column=0, sticky="w")

    # =================================================================== #
    #   SETTINGS
    # =================================================================== #
    def _open_settings(self):
        SettingsDialog(self, self.cfg)

    def apply_settings(self, cfg: dict):
        self.cfg = cfg
        self._init_services()
        self.menu_style.configure(values=["(无模板)"] + self.icon_gen.list_templates())
        self.menu_style.set("(无模板)")
        self._status("配置已更新")

    # =================================================================== #
    #   EVENTS
    # =================================================================== #
    def _browse_script(self):
        f = filedialog.askopenfilename(filetypes=[("Python files", "*.py")])
        if f:
            self.entry_script.delete(0, "end"); self.entry_script.insert(0, f)

    # ---- Generate icon
    def _start_generate(self):
        prompt = self.entry_prompt.get().strip()
        if not prompt:
            messagebox.showwarning("提示", "请输入 Icon Prompt")
            return
        style = None if self.menu_style.get() == "(无模板)" else self.menu_style.get()
        size = self.menu_size.get()

        self.btn_gen.configure(state="disabled"); self.btn_pack.configure(state="disabled")
        self.bar.start(); self._status("正在生成 Icon…")
        threading.Thread(target=self._gen_thread, args=(prompt, style, size), daemon=True).start()

    def _gen_thread(self, prompt, style, size):
        try:
            path = self.icon_gen.generate(
                prompt,
                style=style,
                size=size,
                convert_to_ico=True
            )[0]
            self.generated_icon = path
            img = Image.open(path)
            ctk_img = ctk.CTkImage(img, size=(min(420, img.width), min(420, img.height)))
            self.after(0, lambda: self._preview(ctk_img))
        except Exception as e:
            # ★ 捕获 e
            self.after(0, lambda err=e: self._status(f"生成失败: {err}"))
        finally:
            self.after(0, lambda: self.btn_gen.configure(state="normal"))
            self.after(0, self.bar.stop)

    def _preview(self, ctk_img):
        self.lbl_preview.configure(image=ctk_img, text=""); self.preview_img = ctk_img
        self.btn_pack.configure(state="normal")
        self._status("Icon 已生成，准备打包")

    # ---- Pack
    def _start_pack(self):
        script = self.entry_script.get().strip()
        if not script or not Path(script).exists():
            messagebox.showerror("错误", "请先选择有效脚本")
            return
        if not self.generated_icon:
            messagebox.showwarning("提示", "请先生成 Icon")
            return

        self.btn_pack.configure(state="disabled"); self.bar.start(); self._status("正在打包…")
        threading.Thread(target=self._pack_thread, args=(script,), daemon=True).start()

    def _pack_thread(self, script):
        packer = PyInstallerPacker(
            onefile=self.opt_onefile.get(),
            windowed=self.opt_win.get(),
            clean=self.opt_clean.get(),
            debug=self.opt_debug.get(),
        )
        try:
            result = packer.pack(
                script_path=script,
                name=Path(script).stem,
                icon=self.generated_icon
            )
            Path("pack_log.txt").write_text(result.stdout + "\n" + result.stderr, "utf-8")
            msg = "打包成功！dist/ 已生成" if result.returncode == 0 else "打包失败！查看 pack_log.txt"
            self.after(0, lambda: self._status(msg))
        except Exception as e:
            # ★ 捕获 e
            self.after(0, lambda err=e: self._status(f"打包异常: {err}"))
        finally:
            self.after(0, lambda: self.btn_pack.configure(state="normal"))
            self.after(0, self.bar.stop)
    # ---- Status
    def _status(self, txt: str):
        self.lbl_status.configure(text=f"状态: {txt}")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    try:
        AIconPackGUI().mainloop()
    except Exception as e:
        messagebox.showerror("错误", str(e))