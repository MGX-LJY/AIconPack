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
from tkinter import filedialog, messagebox, Toplevel, Label
from openai import OpenAI, APIConnectionError, RateLimitError
import shutil
from PIL import Image, ImageDraw

# --------------------------------------------------------------------------- #
# 1) AI 生成模块
# --------------------------------------------------------------------------- #
# DALL·E 3 限定分辨率
DALLE3_SIZES: set[str] = {"1024x1024", "1024x1792", "1792x1024"}


class IconGenerator:
    """
    调用 OpenAI 图像接口生成软件 icon。支持：
    • 自定义中转 Base URL
    • Prompt 模板系统
    • PNG 压缩 (0-9)
    • 可选同时输出 .ico
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        prompt_templates: Mapping[str, str] | None = None,
        request_timeout: int = 60,
        max_retries: int = 3,
    ) -> None:
        # ------ 基础配置 ------
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        self.templates = dict(prompt_templates or {})
        self.timeout = request_timeout
        self.max_retries = max_retries

        # 懒加载客户端（GUI 启动时无 KEY 也能运行）
        self._client: OpenAI | None = None
        if self.api_key:
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    # ---------------- 模板 ---------------- #
    def add_template(self, name: str, template: str, *, overwrite: bool = False) -> None:
        if name in self.templates and not overwrite:
            raise ValueError(f"模板 '{name}' 已存在")
        self.templates[name] = template

    def list_templates(self) -> list[str]:
        return list(self.templates)

    # ---------------- 核心生成 ---------------- #
    def generate(
        self,
        prompt: str,
        *,
        style: str | None = None,
        extra_keywords: Sequence[str] | None = None,
        size: str = "1024x1024",
        model: str = "dall-e-3",
        n: int = 1,
        output_dir: str | Path = "icons",
        filename_prefix: str | None = None,
        return_format: Literal["path", "pil", "bytes", "b64"] = "path",
        convert_to_ico: bool = False,
        compress_level: int | None = None,          # 0-9，None 表示不压缩
    ) -> List[Any]:
        """
        返回值依据 return_format：
        • "path"  →  [Path, ...]
        • "pil"   →  [PIL.Image.Image, ...]
        • "bytes" →  [bytes, ...]
        • "b64"   →  [str(base64), ...]
        """
        # -- 检查 / 构建客户端 --
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("请先提供 OpenAI API Key")
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        # -- 尺寸校正 --
        if model == "dall-e-3" and size not in DALLE3_SIZES:
            size = "1024x1024"

        # -- 拼 Prompt --
        full_prompt = (
            self.templates.get(style, "{prompt}").format(prompt=prompt) if style else prompt
        )
        if extra_keywords:
            full_prompt += ", " + ", ".join(extra_keywords)

        # -- 调用 OpenAI (带指数退避) --
        retries = 0
        while True:
            try:
                rsp = self._client.images.generate(
                    model=model,
                    prompt=full_prompt,
                    n=min(max(n, 1), 10),
                    size=size,
                    response_format="url",
                )
                break
            except (APIConnectionError, RateLimitError) as e:
                retries += 1
                if retries > self.max_retries:
                    raise RuntimeError(f"请求失败：{e}") from e
                time.sleep(2 ** retries)

        # -- 下载 / 保存 --
        out_dir = Path(output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = filename_prefix or f"icon_{ts}"

        results: List[Any] = []
        for idx, item in enumerate(rsp.data, 1):
            img_bytes = requests.get(item.url, timeout=self.timeout).content
            img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

            # ------ 根据格式返回 ------
            if return_format == "pil":
                results.append(img)
                continue
            if return_format == "bytes":
                results.append(img_bytes)
                continue
            if return_format == "b64":
                results.append(base64.b64encode(img_bytes).decode())
                continue

            # 默认保存文件
            name = f"{prefix}_{idx}.png" if n > 1 else f"{prefix}.png"
            png_path = out_dir / name

            save_kwargs = {}
            if isinstance(compress_level, int):
                save_kwargs.update(optimize=True, compress_level=max(0, min(compress_level, 9)))

            img.save(png_path, format="PNG", **save_kwargs)

            if convert_to_ico:
                img.resize((256, 256)).save(png_path.with_suffix(".ico"), format="ICO")

            results.append(png_path)

        return results

# --------------------------------------------------------------------------- #
# 2) 打包模块
# --------------------------------------------------------------------------- #
class PyInstallerPacker:
    """
    高级 PyInstaller 打包封装

    Parameters
    ----------
    onefile   : True → --onefile
    windowed  : True → --noconsole
    clean     : True → --clean
    debug     : True → --debug
    upx       : 启用 UPX 压缩，需要本地存在 upx 可执行或 --upx-dir 指向目录
    upx_dir   : upx 可执行所在目录；若设置则自动加 --upx-dir
    """

    def __init__(
        self,
        *,
        onefile: bool = True,
        windowed: bool = True,
        clean: bool = True,
        debug: bool = False,
        upx: bool = False,
        upx_dir: str | Path | None = None,
        pyinstaller_exe: str | Path | None = None,
    ):
        self.onefile = onefile
        self.windowed = windowed
        self.clean = clean
        self.debug = debug
        self.upx = upx
        self.upx_dir = Path(upx_dir).expanduser() if upx_dir else None
        self.pyinstaller_exe = str(pyinstaller_exe or sys.executable)

    # ---------------------------------------------------------- #
    #  CMD builder
    # ---------------------------------------------------------- #
    def build_cmd(
        self,
        script_path: str | Path,
        *,
        name: str | None = None,
        icon: str | Path | None = None,
        version_file: str | Path | None = None,
        add_data: Sequence[str] | None = None,     # "file;dest" (跨平台分号)
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
            if self.upx_dir:
                cmd.append(str(self.upx_dir))

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
            cmd += list(map(str, extra_args))

        return cmd

    # ---------------------------------------------------------- #
    #  Run pack
    # ---------------------------------------------------------- #
    def pack(
        self,
        script_path: str | Path,
        *,
        dry_run: bool = False,
        **kwargs,
    ) -> subprocess.CompletedProcess | list[str]:
        cmd = self.build_cmd(script_path, **kwargs)
        if dry_run:
            return cmd
        return subprocess.run(cmd, capture_output=True, text=True)

    # ---------------------------------------------------------- #
    #  Helper: create .version file (Windows)
    # ---------------------------------------------------------- #
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
        path = Path(outfile).expanduser().resolve()
        path.write_text(tpl, encoding="utf-8")
        return path

def _extend_arg(cmd: list[str], flag: str, values: Iterable[str] | None):
    if values:
        for v in values:
            cmd += [flag, str(v)]

# --------------------------------------------------------------------------- #
# 3) GUI 模块
# --------------------------------------------------------------------------- #
# =============== 简易悬浮注解组件 ==========================================
class _ToolTip:
    def __init__(self, widget, text: str):
        self.widget, self.text = widget, text
        self.tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _e):
        if self.tip or not self.text:
            return
        self.tip = Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{_e.x_root+10}+{_e.y_root+10}")
        lbl = Label(self.tip, text=self.text, justify="left",
                    bg="#111", fg="#fff", relief="solid", borderwidth=1,
                    font=("Segoe UI", 9))
        lbl.pack(ipadx=6, ipady=2)

    def _hide(self, _e):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def _set_tip(widget, text: str):
    _ToolTip(widget, text)


# =============== 配置文件操作 =============================================
_CFG = Path.home() / ".aiconpack_config.json"

# ☆ 额外导出一份到程序目录，文件名固定 config.json
CONFIG_EXPORT = Path(__file__).with_name("config.json")   # 也可改成 Path.cwd()/...

def _load_cfg():  # noqa
    if _CFG.exists():
        try:
            return json.loads(_CFG.read_text("utf-8"))
        except Exception: ...
    return {"api_key": "", "base_url": "", "templates": {}}
def _save_cfg(cfg):  # noqa
    _CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")

# ☆ 将 cfg 写到 CONFIG_EXPORT
def _export_cfg(cfg):  # noqa
    CONFIG_EXPORT.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


# =============== 设置窗口 ===================================================
class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, master: "AIconPackGUI", cfg: dict):
        super().__init__(master)
        self.title("设置")
        self.geometry("520x550")
        self.columnconfigure(0, weight=1)
        self.cfg = cfg

        # API Key
        ctk.CTkLabel(self, text="OpenAI API Key:", anchor="w", font=("", 14)).grid(
            row=0, column=0, sticky="w", padx=20, pady=(22, 6))
        self.key_ent = ctk.CTkEntry(self, placeholder_text="sk-...", show="•")
        self.key_ent.insert(0, cfg.get("api_key", ""))
        self.key_ent.grid(row=1, column=0, sticky="ew", padx=20)
        _set_tip(self.key_ent, "填写你的 OpenAI 密钥。留空则无法生成图标。")

        # Base URL
        ctk.CTkLabel(self, text="API Base URL (可选):", anchor="w", font=("", 14)).grid(
            row=2, column=0, sticky="w", padx=20, pady=(20, 6))
        self.base_ent = ctk.CTkEntry(self, placeholder_text="https://api.xxx.com/v1")
        self.base_ent.insert(0, cfg.get("base_url", ""))
        self.base_ent.grid(row=3, column=0, sticky="ew", padx=20)
        _set_tip(self.base_ent, "若你使用代理 / 中转服务，可在此配置 Base URL。")

        # 模板
        ctk.CTkLabel(self, text="Prompt 模板 (JSON):", anchor="w", font=("", 14)).grid(
            row=4, column=0, sticky="w", padx=20, pady=(20, 6))
        self.tpl_txt = ctk.CTkTextbox(self, height=240)
        self.tpl_txt.insert("1.0", json.dumps(cfg.get("templates", {}), ensure_ascii=False, indent=2))
        self.tpl_txt.grid(row=5, column=0, sticky="nsew", padx=20)
        self.rowconfigure(5, weight=1)
        _set_tip(self.tpl_txt, "键=模板名称，值=模板内容；使用 {prompt} 占位符。")

        # 按钮
        box = ctk.CTkFrame(self, fg_color="transparent"); box.grid(row=6, column=0, pady=18)
        ctk.CTkButton(box, text="取消", width=110, command=self.destroy).grid(row=0, column=0, padx=(0, 12))
        ctk.CTkButton(box, text="保存", width=130, command=self._save).grid(row=0, column=1)

    def _save(self):
        try:
            tpl_dict = json.loads(self.tpl_txt.get("1.0", "end").strip() or "{}")
            if not isinstance(tpl_dict, dict):
                raise ValueError
        except Exception:
            messagebox.showerror("错误", "模板 JSON 格式不正确")
            return
        conf = {
            "api_key": self.key_ent.get().strip(),
            "base_url": self.base_ent.get().strip(),
            "templates": tpl_dict,
        }
        _save_cfg(conf)
        _export_cfg(conf)     # ☆ 每次保存同步写出 config.json
        self.master.apply_settings(conf)
        self.destroy()


# =============== 主 GUI =====================================================
class AIconPackGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("AIconPack · AI 图标生成 & PyInstaller 打包")
        self.geometry("980x720")
        self.minsize(880, 640)

        # 服务
        self.cfg = _load_cfg()
        self._init_services()

        # 顶部
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))
        top.columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text="🪄 AIconPack", font=("Segoe UI", 28, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(top, text="⚙️ 设置", width=100,
                      command=lambda: SettingsDialog(self, self.cfg)).grid(row=0, column=1, sticky="e")

        # Tab
        self.columnconfigure(0, weight=1); self.rowconfigure(1, weight=1)
        self.tabs = ctk.CTkTabview(self); self.tabs.grid(row=1, column=0, sticky="nsew", padx=14, pady=12)
        self.ai_tab = self.tabs.add("AI 生成")
        self.pack_tab = self.tabs.add("PyInstaller 打包")

        # 状态栏
        self.status = ctk.CTkLabel(self, text="状态: 就绪", anchor="w")
        self.status.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 10))

        # 页面
        self._build_ai_page()
        self._build_pack_page()

        # 运行态
        self.generated_icon: Path | None = None
        self.preview_img = None

    # ---------- 服务 ----------
    def _init_services(self):
        self.icon_gen = IconGenerator(
            api_key=self.cfg.get("api_key"),
            base_url=self.cfg.get("base_url"),
            prompt_templates=self.cfg.get("templates"),
        )

    # ========== AI PAGE ==========
    def _build_ai_page(self):
        p = self.ai_tab
        p.columnconfigure(1, weight=1)
        p.rowconfigure(3, weight=1)

        # Prompt
        ctk.CTkLabel(p, text="Prompt:", font=("", 14)).grid(
            row=0, column=0, sticky="e", padx=18, pady=(16, 6))
        self.prompt_ent = ctk.CTkEntry(p, placeholder_text="极简扁平风蓝色日历图标")
        self.prompt_ent.grid(
            row=0, column=1, columnspan=5, sticky="ew", padx=18, pady=(16, 6))

        # 模板
        self.style_opt = ctk.CTkOptionMenu(
            p, values=["(无模板)"] + self.icon_gen.list_templates())
        self.style_opt.set("(无模板)")
        self.style_opt.grid(row=1, column=0, padx=18, pady=4)

        # 生成尺寸
        self.size_opt = ctk.CTkOptionMenu(
            p, values=["1024x1024", "1024x1792", "1792x1024"])
        self.size_opt.set("1024x1024")
        self.size_opt.grid(row=1, column=1, padx=10, pady=4)

        # 导出尺寸
        ctk.CTkLabel(p, text="导出尺寸:", font=("", 12)).grid(
            row=1, column=2, sticky="e", padx=6)
        self.outsize_opt = ctk.CTkOptionMenu(
            p, values=["原始", "256", "512", "768"])
        self.outsize_opt.set("原始")
        self.outsize_opt.grid(row=1, column=3, padx=10, pady=4)

        # PNG 压缩
        ctk.CTkLabel(p, text="PNG 压缩:", font=("", 12)).grid(
            row=1, column=4, sticky="e", padx=6)
        self.comp_slider = ctk.CTkSlider(
            p, from_=0, to=9, number_of_steps=9, width=140)
        self.comp_slider.set(6)
        self.comp_slider.grid(row=1, column=5, padx=18)

        # 生成按钮
        self.gen_btn = ctk.CTkButton(
            p, text="🎨 生成", width=120, command=self._start_generate)
        self.gen_btn.grid(row=1, column=6, padx=8)

        # ☆ 圆润按钮（初始禁用）
        self.smooth_btn = ctk.CTkButton(
            p, text="✨ 圆润处理", width=120,
            command=self._smooth_icon, state="disabled")
        self.smooth_btn.grid(row=1, column=7, padx=8)

        # 预览
        self.preview_lbl = ctk.CTkLabel(
            p, text="预览区域", fg_color="#151515",
            width=520, height=380, corner_radius=8)
        self.preview_lbl.grid(
            row=3, column=0, columnspan=8,
            sticky="nsew", padx=18, pady=(10, 16))

        # 进度条
        self.ai_bar = ctk.CTkProgressBar(p, mode="indeterminate")
        self.ai_bar.grid(row=4, column=0, columnspan=8,
                         sticky="ew", padx=18, pady=(0, 12))
        self.ai_bar.stop()

    # ========== PACK PAGE (centered) ==========
    def _build_pack_page(self):
        p = self.pack_tab
        p.columnconfigure(0, weight=1)
        p.columnconfigure(2, weight=1)

        outer = ctk.CTkFrame(p, fg_color="transparent")
        outer.grid(row=0, column=1, sticky="n", pady=12)
        outer.columnconfigure(1, weight=1)

        row = 0
        # 入口脚本
        ctk.CTkLabel(outer, text="入口脚本:", font=("", 14)).grid(
            row=row, column=0, sticky="e", pady=8, padx=10)
        self.script_ent = ctk.CTkEntry(outer, placeholder_text="app.py")
        self.script_ent.grid(row=row, column=1, sticky="ew", pady=8)
        ctk.CTkButton(outer, text="浏览", width=90,
                      command=self._browse_script).grid(row=row, column=2,
                                                        sticky="w", padx=10, pady=8)

        # 图标文件
        row += 1
        ctk.CTkLabel(outer, text="图标文件 (可选):", font=("", 12)).grid(
            row=row, column=0, sticky="e", pady=8, padx=10)
        self.icon_ent = ctk.CTkEntry(outer, placeholder_text="icon.ico / .png")
        self.icon_ent.grid(row=row, column=1, sticky="ew", pady=8)
        btn_frame = ctk.CTkFrame(outer, fg_color="transparent")
        btn_frame.grid(row=row, column=2, sticky="w")
        ctk.CTkButton(btn_frame, text="选择", width=50,
                      command=self._browse_icon).grid(row=0, column=0, padx=(0, 4))
        ctk.CTkButton(btn_frame, text="用生成",
                      width=64, command=self._use_generated_icon).grid(row=0, column=1)

        # 应用名称
        row += 1
        ctk.CTkLabel(outer, text="应用名称:", font=("", 14)).grid(
            row=row, column=0, sticky="e", pady=8, padx=10)
        self.name_ent = ctk.CTkEntry(outer, placeholder_text="MyApp")
        self.name_ent.grid(row=row, column=1, columnspan=2, sticky="ew", pady=8)

        # 开关
        row += 1
        swf = ctk.CTkFrame(outer, fg_color="transparent")
        swf.grid(row=row, column=0, columnspan=3, sticky="w", pady=10)
        self.sw_one = ctk.CTkSwitch(swf, text="--onefile");
        self.sw_one.select()
        self.sw_win = ctk.CTkSwitch(swf, text="--noconsole");
        self.sw_win.select()
        self.sw_clean = ctk.CTkSwitch(swf, text="--clean");
        self.sw_clean.select()
        self.sw_debug = ctk.CTkSwitch(swf, text="--debug (可选)")
        self.sw_upx = ctk.CTkSwitch(swf, text="UPX (可选)")
        self.sw_keep = ctk.CTkSwitch(swf, text="仅保留可执行 (可选)")
        for idx, sw in enumerate(
                (self.sw_one, self.sw_win, self.sw_clean,
                 self.sw_debug, self.sw_upx, self.sw_keep)
        ):
            sw.grid(row=idx // 3, column=idx % 3, padx=12, pady=4, sticky="w")

        # hidden-imports
        row += 1
        ctk.CTkLabel(outer, text="hidden-imports (可选):",
                     font=("", 12)).grid(row=row, column=0,
                                         sticky="e", pady=8, padx=10)
        self.hidden_ent = ctk.CTkEntry(outer, placeholder_text="pkg1,pkg2")
        self.hidden_ent.grid(row=row, column=1, columnspan=2, sticky="ew", pady=8)

        # add-data
        row += 1
        ctk.CTkLabel(outer, text="add-data (可选):",
                     font=("", 12)).grid(row=row, column=0,
                                         sticky="e", pady=8, padx=10)
        self.data_ent = ctk.CTkEntry(outer, placeholder_text="file.txt;data")
        self.data_ent.grid(row=row, column=1, columnspan=2, sticky="ew", pady=8)

        # 打包按钮
        row += 4
        self.pack_btn = ctk.CTkButton(outer, text="📦  开始打包",
                                      height=46, command=self._start_pack)
        self.pack_btn.grid(row=row, column=0, columnspan=3,
                           sticky="ew", pady=18)

        # 进度条
        row += 1
        self.pack_bar = ctk.CTkProgressBar(outer, mode="indeterminate")
        self.pack_bar.grid(row=row, column=0, columnspan=3,
                           sticky="ew", pady=(0, 12))
        self.pack_bar.stop()

    # ---------- 生成线程 ----------
    def _start_generate(self):
        prompt = self.prompt_ent.get().strip()
        if not prompt:
            messagebox.showwarning("提示", "请输入 Prompt")
            return
        style = None if self.style_opt.get() == "(无模板)" else self.style_opt.get()
        size = self.size_opt.get()
        comp = int(self.comp_slider.get())

        self.gen_btn.configure(state="disabled")
        self.ai_bar.start()  # ← 开启进度条
        self._status("生成中…")
        threading.Thread(target=self._gen_thread,
                         args=(prompt, style, size, comp),
                         daemon=True).start()

    def _gen_thread(self, prompt, style, size, comp):
        try:
            icon_path = self.icon_gen.generate(
                prompt, style=style, size=size,
                compress_level=comp, convert_to_ico=True
            )[0]
            self.generated_icon = icon_path
            img = Image.open(icon_path)
            cimg = ctk.CTkImage(img, size=(min(420, img.width),
                                           min(420, img.height)))
            self.after(0, lambda: self._show_preview(cimg))
        except Exception as e:
            self.after(0, lambda err=e: self._status(f"生成失败: {err}"))
        finally:
            self.after(0, lambda: self.gen_btn.configure(state="normal"))
            self.after(0, self.ai_bar.stop)

    def _show_preview(self, cimg):
        self.preview_lbl.configure(image=cimg, text=""); self.preview_img = cimg
        self._status("生成完成，可前往『打包』页")
        self.smooth_btn.configure(state="normal")  # 启用“圆润处理”
    # ---------- 打包线程 ----------
    def _browse_script(self):
        p = filedialog.askopenfilename(filetypes=[("Python files", "*.py")])
        if p:
            self.script_ent.delete(0, "end"); self.script_ent.insert(0, p)

    def _start_pack(self):
        script = self.script_ent.get().strip()
        if not script or not Path(script).exists():
            messagebox.showerror("错误", "请选择有效的入口脚本")
            return

        # 图标优先取输入框，没有则回退生成的
        icon_path = self.icon_ent.get().strip() or self.generated_icon

        self.pack_btn.configure(state="disabled")
        self.pack_bar.start()
        self._status("开始打包…")
        threading.Thread(target=self._pack_thread,
                         args=(script, icon_path),
                         daemon=True).start()

    def _pack_thread(self, script, icon_path):
        packer = PyInstallerPacker(
            onefile=self.sw_one.get(),
            windowed=self.sw_win.get(),
            clean=self.sw_clean.get(),
            debug=self.sw_debug.get(),
            upx=self.sw_upx.get()
        )
        try:
            result = packer.pack(
                script_path=script,
                name=self.name_ent.get().strip() or Path(script).stem,
                icon=icon_path if icon_path else None,
                dist_dir=self.dist_ent.get().strip() or None,
                hidden_imports=[x.strip() for x in
                                self.hidden_ent.get().split(",") if x.strip()] or None,
                add_data=[self.data_ent.get().strip()]
                if self.data_ent.get().strip() else None
            )
            ok = result.returncode == 0
            # 清理中间文件
            if ok and self.sw_keep.get():
                shutil.rmtree("build", ignore_errors=True)
                spec_file = Path(script).with_suffix(".spec")
                if spec_file.exists():
                    spec_file.unlink()
            Path("pack_log.txt").write_text(
                result.stdout + "\n" + result.stderr, "utf-8")
            txt = "打包成功！" if ok else "打包失败！查看 pack_log.txt"
            self.after(0, lambda: self._status(txt))
        except Exception as e:
            self.after(0, lambda err=e: self._status(f"打包异常: {err}"))
        finally:
            self.after(0, lambda: self.pack_btn.configure(state="normal"))
            self.after(0, self.pack_bar.stop)

    # ---------- 设置 & 状态 ----------
    def apply_settings(self, cfg: dict):
        self.cfg = cfg; self._init_services()
        self.style_opt.configure(values=["(无模板)"] + self.icon_gen.list_templates())
        self.style_opt.set("(无模板)")
        self._status("已加载新配置")

    def _status(self, text): self.status.configure(text=f"状态: {text}")

# ---------- 额外工具 ----------
def _smooth_icon(self):
    """对已生成 PNG 做 25% 圆角裁切并刷新预览"""
    if not self.generated_icon or not Path(self.generated_icon).exists():
        messagebox.showwarning("提示", "请先生成图标")
        return

    img = Image.open(self.generated_icon).convert("RGBA")
    w, h = img.size
    radius = int(min(w, h) * 0.25)

    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, w, h), radius=radius, fill=255)

    img.putalpha(mask)

    rounded_path = Path(self.generated_icon).with_stem(
        Path(self.generated_icon).stem + "_round")
    img.save(rounded_path, format="PNG")

    self.generated_icon = rounded_path
    cimg = ctk.CTkImage(img, size=(min(420, w), min(420, h)))
    self.preview_img = cimg
    self.preview_lbl.configure(image=cimg, text="")
    self._status("已生成圆润版本")

def _browse_icon(self):
    """手动选择 .ico / .png 作为打包图标"""
    p = filedialog.askopenfilename(
        filetypes=[("Icon files", "*.ico *.png")])
    if p:
        self.icon_ent.delete(0, "end")
        self.icon_ent.insert(0, p)

def _use_generated_icon(self):
    """把最新生成的图标填入图标输入框"""
    if not self.generated_icon:
        messagebox.showwarning("提示", "尚未生成图标")
        return
    self.icon_ent.delete(0, "end")
    self.icon_ent.insert(0, str(self.generated_icon))

# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    try:
        AIconPackGUI().mainloop()
    except Exception as e:
        messagebox.showerror("错误", str(e))