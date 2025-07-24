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
import re
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
        self.geometry("520x620")          # 高度增加 70
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

        # Prompt 模板
        ctk.CTkLabel(self, text="Prompt 模板 (JSON):", anchor="w", font=("", 14)).grid(
            row=4, column=0, sticky="w", padx=20, pady=(20, 6))
        self.tpl_txt = ctk.CTkTextbox(self, height=200)
        self.tpl_txt.insert("1.0", json.dumps(cfg.get("templates", {}), ensure_ascii=False, indent=2))
        self.tpl_txt.grid(row=5, column=0, sticky="nsew", padx=20)
        self.rowconfigure(5, weight=1)
        _set_tip(self.tpl_txt, "键=模板名称，值=模板内容；使用 {prompt} 占位符。")

        # 依赖分析模型
        ctk.CTkLabel(self, text="依赖分析模型:", anchor="w", font=("", 14)).grid(
            row=6, column=0, sticky="w", padx=20, pady=(20, 6))
        self.model_opt = ctk.CTkOptionMenu(
            self, values=["gpt-4o-mini", "gpt-4o", "gpt-4o-128k"])
        self.model_opt.set(cfg.get("chat_model", "gpt-4o-mini"))
        self.model_opt.grid(row=7, column=0, sticky="ew", padx=20)
        _set_tip(self.model_opt, "用于扫描入口脚本并生成 requirements.txt")

        # 按钮
        box = ctk.CTkFrame(self, fg_color="transparent")
        box.grid(row=8, column=0, pady=18)
        ctk.CTkButton(box, text="取消", width=110, command=self.destroy).grid(
            row=0, column=0, padx=(0, 12))
        ctk.CTkButton(box, text="保存", width=130, command=self._save).grid(
            row=0, column=1)

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
            "chat_model": self.model_opt.get(),        # 新增
        }
        _save_cfg(conf)
        _export_cfg(conf)
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
        # 图像生成
        self.icon_gen = IconGenerator(
            api_key=self.cfg.get("api_key"),
            base_url=self.cfg.get("base_url"),
            prompt_templates=self.cfg.get("templates"),
        )
        # 聊天模型（依赖分析用）
        self.chat_model = self.cfg.get("chat_model", "gpt-4o-mini")
        self._chat_client: Optional[OpenAI] = None      # 懒加载

    # ---------- 图标后处理 ----------
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
        p = filedialog.askopenfilename(filetypes=[("Icon files", "*.ico *.png")])
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

    # ========== AI PAGE ==========
    def _build_ai_page(self):
        """构建“AI 生成”标签页（布局已优化，窄窗口也能看到所有控件）"""
        p = self.ai_tab
        p.columnconfigure(1, weight=1)      # 输入框列自适应
        p.rowconfigure(5, weight=1)         # 预览区域可伸缩

        # ── Row-0: Prompt ──────────────────────────────────────────────
        ctk.CTkLabel(p, text="Prompt:", font=("", 14)).grid(
            row=0, column=0, sticky="e", padx=18, pady=(16, 6))
        self.prompt_ent = ctk.CTkEntry(p, placeholder_text="极简扁平风蓝色日历图标")
        self.prompt_ent.grid(
            row=0, column=1, columnspan=10, sticky="ew", padx=18, pady=(16, 6))

        # ── Row-1: 模板 + 尺寸 + 压缩滑块 ───────────────────────────────
        ctk.CTkLabel(p, text="模板:", font=("", 12)).grid(
            row=1, column=0, sticky="e", padx=6)
        self.style_opt = ctk.CTkOptionMenu(
            p, values=["(无模板)"] + self.icon_gen.list_templates())
        self.style_opt.set("(无模板)")
        self.style_opt.grid(row=1, column=1, padx=6, pady=4)

        ctk.CTkLabel(p, text="分辨率:", font=("", 12)).grid(
            row=1, column=2, sticky="e", padx=6)
        self.size_opt = ctk.CTkOptionMenu(
            p, values=["1024x1024", "1024x1792", "1792x1024"])
        self.size_opt.set("1024x1024")
        self.size_opt.grid(row=1, column=3, padx=6, pady=4)

        ctk.CTkLabel(p, text="PNG 压缩:", font=("", 12)).grid(
            row=1, column=4, sticky="e", padx=6)
        self.comp_slider = ctk.CTkSlider(
            p, from_=0, to=9, number_of_steps=9, width=150)
        self.comp_slider.set(6)
        self.comp_slider.grid(row=1, column=5, padx=6)

        # ── Row-Btn: 数量 + 三个操作按钮（独立一行，窄窗也能显示） ───────
        row_btn = 2
        ctk.CTkLabel(p, text="数量:", font=("", 12)).grid(
            row=row_btn, column=0, sticky="e", padx=6)
        self.count_opt = ctk.CTkOptionMenu(
            p, values=[str(i) for i in range(1, 11)])
        self.count_opt.set("1")
        self.count_opt.grid(row=row_btn, column=1, padx=6, pady=4)

        self.gen_btn = ctk.CTkButton(
            p, text="🎨 生成", width=110, command=self._start_generate)
        self.gen_btn.grid(row=row_btn, column=2, padx=6, pady=2)

        self.smooth_btn = ctk.CTkButton(
            p, text="✨ 圆润处理", width=110,
            command=self._smooth_icon, state="disabled")
        self.smooth_btn.grid(row=row_btn, column=3, padx=6, pady=2)

        self.import_btn = ctk.CTkButton(
            p, text="📂 导入图片", width=110, fg_color="#455A9C",
            command=self._import_image)
        self.import_btn.grid(row=row_btn, column=4, padx=6, pady=2)

        # ── 预览区域 ───────────────────────────────────────────────────
        self.preview_lbl = ctk.CTkLabel(
            p, text="预览区域", fg_color="#151515",
            width=520, height=380, corner_radius=8)
        self.preview_lbl.grid(
            row=row_btn + 2, column=0, columnspan=11,
            sticky="nsew", padx=18, pady=(10, 16))

        # ── 进度条 ───────────────────────────────────────────────────
        self.ai_bar = ctk.CTkProgressBar(p, mode="indeterminate")
        self.ai_bar.grid(
            row=row_btn + 3, column=0, columnspan=11,
            sticky="ew", padx=18, pady=(0, 12))
        self.ai_bar.stop()

    # ========== PACK PAGE ==========
    def _build_pack_page(self):
        """构建“PyInstaller 打包”标签页（已补回 dist 目录输入框）"""
        p = self.pack_tab
        p.columnconfigure(0, weight=1)
        p.columnconfigure(2, weight=1)

        outer = ctk.CTkFrame(p, fg_color="transparent")
        outer.grid(row=0, column=1, sticky="n", pady=12)
        outer.columnconfigure(1, weight=1)

        row = 0
        # ── 入口脚本 ───────────────────────────────────
        ctk.CTkLabel(outer, text="入口脚本:", font=("", 14)).grid(
            row=row, column=0, sticky="e", pady=8, padx=10)
        self.script_ent = ctk.CTkEntry(outer, placeholder_text="app.py")
        self.script_ent.grid(row=row, column=1, sticky="ew", pady=8)
        ctk.CTkButton(outer, text="浏览", width=90,
                      command=self._browse_script).grid(row=row, column=2,
                                                        sticky="w", padx=10, pady=8)

        # ── 图标文件 ───────────────────────────────────
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

        # ── 输出目录(dist)  ←★ 新增 ─────────────────────
        row += 1
        ctk.CTkLabel(outer, text="输出目录(dist) (可选):", font=("", 12)).grid(
            row=row, column=0, sticky="e", pady=8, padx=10)
        self.dist_ent = ctk.CTkEntry(outer, placeholder_text="dist")
        self.dist_ent.grid(row=row, column=1, columnspan=2, sticky="ew", pady=8)

        # ── 应用名称 ───────────────────────────────────
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
                           sticky="ew", pady=(18, 6))

        # ☆ 自动依赖 + 虚拟环境打包
        row += 1
        self.auto_pack_btn = ctk.CTkButton(outer, text="🤖 自动依赖打包",
                                           height=42, fg_color="#2D7D46",
                                           command=self._start_auto_pack)
        self.auto_pack_btn.grid(row=row, column=0, columnspan=3,
                                sticky="ew", pady=(0, 18))

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
        count = int(self.count_opt.get())          # ← 新增

        self.gen_btn.configure(state="disabled")
        self.ai_bar.start()
        self._status("生成中…")
        threading.Thread(
            target=self._gen_thread,
            args=(prompt, style, size, comp, count),   # ← 多一个参数
            daemon=True
        ).start()

    def _gen_thread(self, prompt, style, size, comp, count):
        try:
            paths = self.icon_gen.generate(
                prompt, style=style, size=size,
                compress_level=comp, convert_to_ico=True,
                n=count                                   # ← 传入数量
            )
            self.generated_icon = paths[0]               # 先显示第一张
            img = Image.open(paths[0])
            cimg = ctk.CTkImage(img, size=(min(420, img.width),
                                           min(420, img.height)))
            self.after(0, lambda: self._show_preview(cimg))

            if count > 1:
                self.after(0, lambda: self._status(
                    f"已批量生成 {count} 张，全部保存在 {Path(paths[0]).parent}"))
        except Exception as e:
            self.after(0, lambda err=e: self._status(f"生成失败: {err}"))
        finally:
            self.after(0, lambda: self.gen_btn.configure(state="normal"))
            self.after(0, self.ai_bar.stop)

    def _import_image(self):
        """导入本地 PNG/JPG 进行预览及圆润处理"""
        path = filedialog.askopenfilename(
            filetypes=[("Image files", "*.png *.jpg *.jpeg")])
        if not path:
            return
        try:
            img = Image.open(path).convert("RGBA")
        except Exception as e:
            messagebox.showerror("错误", f"无法打开图片: {e}")
            return

        self.generated_icon = Path(path)
        cimg = ctk.CTkImage(img, size=(min(420, img.width),
                                       min(420, img.height)))
        self.preview_img = cimg
        self.preview_lbl.configure(image=cimg, text="")
        self.smooth_btn.configure(state="normal")        # 允许圆润
        self._status("已导入外部图片，可执行圆润处理")

    def _show_preview(self, cimg):
        self.preview_lbl.configure(image=cimg, text=""); self.preview_img = cimg
        self._status("生成完成，可前往『打包』页")
        self.smooth_btn.configure(state="normal")  # 启用“圆润处理”

    def _start_auto_pack(self):
        script = self.script_ent.get().strip()
        if not script or not Path(script).exists():
            messagebox.showerror("错误", "请选择有效的入口脚本")
            return

        self.auto_pack_btn.configure(state="disabled")
        self.pack_bar.start()
        self._status("准备自动打包…")
        threading.Thread(target=self._auto_pack_thread,
                         args=(script,), daemon=True).start()

    def _detect_dependencies(self, script: str) -> list[str]:
        """
        粗略扫描入口脚本里出现的第三方顶级模块，并把常见别名映射
        到真正的 PyPI 包名，避免 “PIL / cv2” 之类安装失败。
        """
        stdlib = sys.stdlib_module_names          # 3.10+ 可用
        alias_map = {            # 需要扩展时在这里补充即可
            "PIL": "Pillow",
            "cv2": "opencv-python",
            "skimage": "scikit-image",
            "Crypto": "pycryptodome",
        }

        pattern = re.compile(r'^\s*(?:from|import)\s+([a-zA-Z0-9_\.]+)', re.M)
        txt = Path(script).read_text(encoding="utf-8", errors="ignore")

        pkgs: set[str] = set()
        for mod in pattern.findall(txt):
            root = mod.split('.')[0]
            if root and root not in stdlib:
                pkgs.add(alias_map.get(root, root))   # 应用映射

        return sorted(pkgs)

    def _detect_dependencies_ai(self, script: str) -> list[str]:
        """
        使用 OpenAI GPT 模型分析脚本依赖，生成包名列表并写入
        requirements.txt；失败时回落到静态正则解析。
        """
        fallback = self._detect_dependencies(script)
        try:
            code_text = Path(script).read_text(encoding="utf-8")[:15000]
        except Exception as e:
            self._status(f"读取脚本失败，使用静态分析: {e}")
            self._write_requirements(fallback)
            return fallback

        if self._chat_client is None:
            self._chat_client = OpenAI(
                api_key=self.cfg.get("api_key"),
                base_url=self.cfg.get("base_url") or None,
                timeout=60,
            )

        system_msg = (
            "你是资深 Python 打包专家。请阅读用户给出的代码，并输出它运行所需在 "
            "PyPI 可直接安装的第三方依赖包列表（只给包名，无版本号，按逗号分隔）。"
            "标准库和相对导入请忽略。常见别名需映射到真实包名，例如 PIL→Pillow、"
            "cv2→opencv-python、Crypto→pycryptodome。"
        )

        try:
            rsp = self._chat_client.chat.completions.create(
                model=self.chat_model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": code_text},
                ],
                temperature=0.1,
            )
            reply = rsp.choices[0].message.content.strip()
            pkgs = {re.split(r"[,\s]+", p)[0] for p in reply.split(",") if p.strip()}
            pkgs = sorted(x for x in pkgs if x)       # 过滤空字符串
            self._write_requirements(pkgs)
            return pkgs if pkgs else fallback
        except Exception as e:
            self._status(f"AI 依赖分析失败，使用静态分析: {e}")
            self._write_requirements(fallback)
            return fallback

    def _write_requirements(self, pkgs: list[str]):
        """把依赖写入 requirements.txt（覆盖写，自动过滤空项）"""
        Path("requirements.txt").write_text(
            "\n".join(p for p in pkgs if p), encoding="utf-8")

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

    def _pack_thread(self, script: str, icon_path: Optional[str]):
        """普通打包线程（清理 build 目录 & .spec 文件按 --name 查找）"""
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
                icon=icon_path or None,
                dist_dir=(self.dist_ent.get().strip()
                          if hasattr(self, "dist_ent") and self.dist_ent.get().strip()
                          else None),
                hidden_imports=[x.strip() for x in
                                self.hidden_ent.get().split(",") if x.strip()] or None,
                add_data=[self.data_ent.get().strip()]
                         if self.data_ent.get().strip() else None
            )
            ok = result.returncode == 0
            if ok and self.sw_keep.get():
                shutil.rmtree("build", ignore_errors=True)
                spec_name = (self.name_ent.get().strip() or Path(script).stem) + ".spec"
                if Path(spec_name).exists():
                    Path(spec_name).unlink()
            Path("pack_log.txt").write_text(result.stdout + "\n" + result.stderr,
                                            encoding="utf-8")
            self.after(0, lambda: self._status(
                "打包成功！" if ok else "打包失败！查看 pack_log.txt"))
        except Exception as e:
            self.after(0, lambda err=e: self._status(f"打包异常: {err}"))
        finally:
            self.after(0, lambda: self.pack_btn.configure(state="normal"))
            self.after(0, self.pack_bar.stop)

    def _auto_pack_thread(self, script: str):
        """
        1) 使用 GPT 分析依赖 → requirements.txt
        2) 创建临时 venv (.aipack_venv)
        3) pip install -r requirements.txt
        4) 用 venv/python 调 PyInstaller
        """
        venv_dir = Path(".aipack_venv")
        python_exe = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        try:
            # ── 1. 解析依赖 ─────────────────────────
            pkgs = self._detect_dependencies_ai(script)
            pkgs = [p for p in pkgs if p]        # 再保险过滤空包名
            if not pkgs:
                self.after(0, lambda: self._status("未检测到依赖，改用系统环境打包"))
                self.after(0, self._start_pack)
                return

            # ── 2. 创建 venv ────────────────────────
            if venv_dir.exists():
                shutil.rmtree(venv_dir)
            subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])

            # ── 3. 安装依赖 ─────────────────────────
            self.after(0, lambda: self._status("安装依赖中…"))
            subprocess.check_call([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"])
            subprocess.check_call([str(python_exe), "-m", "pip", "install",
                                   "pyinstaller>=6", *pkgs])

            # ── 4. 调 PyInstaller ───────────────────
            self.after(0, lambda: self._status("依赖安装完成，开始打包…"))
            packer = PyInstallerPacker(
                onefile=self.sw_one.get(),
                windowed=self.sw_win.get(),
                clean=self.sw_clean.get(),
                debug=self.sw_debug.get(),
                upx=self.sw_upx.get(),
                pyinstaller_exe=str(python_exe),
            )
            result = packer.pack(
                script_path=script,
                name=self.name_ent.get().strip() or Path(script).stem,
                icon=self.icon_ent.get().strip() or self.generated_icon or None,
                dist_dir=(self.dist_ent.get().strip()
                          if hasattr(self, "dist_ent") and self.dist_ent.get().strip()
                          else None),
                hidden_imports=None,
                add_data=None,
            )
            ok = result.returncode == 0
            if ok and self.sw_keep.get():
                shutil.rmtree("build", ignore_errors=True)
                spec_name = (self.name_ent.get().strip() or Path(script).stem) + ".spec"
                if Path(spec_name).exists():
                    Path(spec_name).unlink()
            Path("pack_log.txt").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
            self.after(0, lambda: self._status(
                "自动打包成功！" if ok else "自动打包失败！查看 pack_log.txt"))
        except Exception as e:
            self.after(0, lambda err=e: self._status(f"自动打包异常: {err}"))  # 捕获 e
        finally:
            self.after(0, self.pack_bar.stop)
            self.after(0, lambda: self.auto_pack_btn.configure(state="normal"))

    # ---------- 设置 & 状态 ----------
    def apply_settings(self, cfg: dict):
        self.cfg = cfg; self._init_services()
        self.style_opt.configure(values=["(无模板)"] + self.icon_gen.list_templates())
        self.style_opt.set("(无模板)")
        self._status("已加载新配置")

    def _status(self, text): self.status.configure(text=f"状态: {text}")

# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    try:
        AIconPackGUI().mainloop()
    except Exception as e:
        messagebox.showerror("错误", str(e))