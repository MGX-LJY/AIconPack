#!/usr/bin/env python3
# aiconpack.py
"""
AIconPack
~~~~~~~~~
单文件三大模块：
1. IconGenerator —— 调用 OpenAI 生成应用 icon
2. PyInstallerPacker —— 调用 PyInstaller 打包可执行文件
3. AIconPackGUI —— 现代化 GUI，串联生成 + 打包

依赖：
    pip install openai requests pillow customtkinter pyinstaller
    # Windows 如需打包需装 pyinstaller-windows==<与你 Python 版本匹配的版本>

环境变量：
    OPENAI_API_KEY="sk-..."
"""

from __future__ import annotations

import subprocess
import sys
import threading

import customtkinter as ctk
from tkinter import filedialog, messagebox
import io
import os
import time
import base64
from pathlib import Path
from datetime import datetime
from typing import Literal, Sequence, Mapping, Any, Optional, List, Iterable

import requests
from PIL import Image
from openai import OpenAI, APIConnectionError, RateLimitError


# --------------------------------------------------------------------------- #
# 1) AI 生成模块
# --------------------------------------------------------------------------- #
class IconGenerator:
    """
    生成软件图标的高级封装

    Parameters
    ----------
    api_key : str | None
        OpenAI API Key，默认读取 env `OPENAI_API_KEY`
    base_url : str | None
        自定义接口中转地址，例如 `https://api.myproxy.com/v1`
    prompt_templates : dict[str, str] | None
        预设模板，如 {"flat": "{prompt}, flat style, minimal"}；调用时 `style="flat"`
    request_timeout : int
        下载图片时的 seconds
    max_retries : int
        OpenAI 网络/限流重试次数
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
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("环境变量 OPENAI_API_KEY 未设置")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        self.client = (
            OpenAI(api_key=self.api_key, base_url=self.base_url)
            if self.base_url
            else OpenAI(api_key=self.api_key)
        )
        self.templates: dict[str, str] = dict(prompt_templates or {})
        self.timeout = request_timeout
        self.max_retries = max_retries

    # ------------------------------------------------------------------ #
    # 模板管理
    # ------------------------------------------------------------------ #
    def add_template(self, name: str, template: str, overwrite: bool = False) -> None:
        if name in self.templates and not overwrite:
            raise ValueError(f"模板 '{name}' 已存在 (可设 overwrite=True 覆盖)")
        self.templates[name] = template

    def list_templates(self) -> list[str]:
        return list(self.templates.keys())

    # ------------------------------------------------------------------ #
    # 核心生成
    # ------------------------------------------------------------------ #
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
        """
        生成 icon 并返回多种格式

        Parameters
        ----------
        prompt : str
            基础提示词
        style : str | None
            引用 add_template / prompt_templates 里的键
        extra_keywords : list[str] | None
            附加关键词 (自动用逗号拼接)
        n : int
            生成张数 (最大 10)
        return_format : "path" | "pil" | "bytes" | "b64"
            控制返回格式
        convert_to_ico : bool
            若 True 则 .ico 和 .png 均保存 (需 return_format = "path")

        Returns
        -------
        list[Path | PIL.Image | bytes | str]
            根据 return_format 返回路径 / Image / 原始 bytes / base64 字符串
        """
        # ---- 构造 Prompt ----
        full_prompt = prompt
        if style and style in self.templates:
            full_prompt = self.templates[style].format(prompt=prompt)
        if extra_keywords:
            full_prompt += ", " + ", ".join(map(str, extra_keywords))

        # ---- 请求 OpenAI (带重试) ----
        attempts = 0
        while True:
            try:
                response = self.client.images.generate(
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
                    raise RuntimeError(f"请求失败超过重试次数: {e}") from e
                time.sleep(2 * attempts)  # 指数退避
            except Exception as e:
                raise RuntimeError(f"OpenAI 请求异常: {e}") from e

        # ---- 下载 / 处理 ----
        output_dir = Path(output_dir).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = filename_prefix or f"icon_{timestamp}"

        results: list[Any] = []
        for idx, data in enumerate(response.data, start=1):
            img_bytes = requests.get(data.url, timeout=self.timeout).content
            img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

            if return_format == "pil":
                results.append(img)
                continue
            if return_format == "bytes":
                results.append(img_bytes)
                continue
            if return_format == "b64":
                results.append(base64.b64encode(img_bytes).decode())
                continue

            # save to file
            png_name = f"{prefix}_{idx}.png" if n > 1 else f"{prefix}.png"
            png_path = output_dir / png_name
            img.save(png_path, format="PNG")

            if convert_to_ico:
                ico_path = png_path.with_suffix(".ico")
                img.resize((256, 256)).save(ico_path, format="ICO")

            results.append(png_path)

        return results

# --------------------------------------------------------------------------- #
# 2) 打包模块
# --------------------------------------------------------------------------- #
class PyInstallerPacker:
    """
    封装 PyInstaller 打包流程（可丰富配置给 GUI 调用）

    公用方法
    --------
    • build_cmd(...)  -> list[str]            构建 PyInstaller 命令，不执行
    • pack(...)       -> subprocess.CompletedProcess | list[str]
                                            执行打包；dry_run=True 返回命令

    辅助静态方法
    ------------
    • create_version_file(...) -> Path       生成 Windows .version 文件
    """

    def __init__(
        self,
        *,
        onefile: bool = True,
        windowed: bool = True,
        clean: bool = True,
        debug: bool = False,
        upx: bool = False,
        pyinstaller_exe: Optional[str] = None,
    ) -> None:
        self.onefile = onefile
        self.windowed = windowed          # True -> --noconsole
        self.clean = clean
        self.debug = debug
        self.upx = upx
        self.pyinstaller_exe = pyinstaller_exe or sys.executable

    # ========================  PUBLIC API  ======================== #
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
        """
        根据参数拼接 PyInstaller 命令
        """
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
        if version_file and sys.platform.system() == "Windows":
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

    def pack(
        self,
        script_path: str | Path,
        *,
        dry_run: bool = False,
        **kwargs,
    ):
        """
        调用 PyInstaller 执行打包；dry_run=True 时仅返回命令
        """
        cmd = self.build_cmd(script_path, **kwargs)
        if dry_run:
            return cmd
        return subprocess.run(cmd, check=False, capture_output=True, text=True)

    # ========================  UTILITIES  ======================== #
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
        """
        快速生成 Windows version-file 模板；返回路径
        """
        content = f"""# UTF-8
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
        path.write_text(content, encoding="utf-8")
        return path


# ========================  HELPER  ======================== #
def _extend_arg(cmd: list[str], flag: str, values: Iterable[str] | None):
    if values:
        for val in values:
            cmd += [flag, str(val)]

# --------------------------------------------------------------------------- #
# 3) GUI 模块
# --------------------------------------------------------------------------- #
class AIconPackGUI(ctk.CTk):
    """优雅美观的 AIconPack 图形界面"""

    # --------------------------------------------------------------------- #
    def __init__(self) -> None:
        super().__init__()
        # ---------- Window ---------- #
        self.title("AIconPack · 一键图标生成与打包")
        self.geometry("900x650")
        self.minsize(800, 560)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # ---------- Back-end ---------- #
        self.icon_gen = IconGenerator()
        self.packer = PyInstallerPacker()

        # ---------- Layout root ---------- #
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # ── Heading
        title = ctk.CTkLabel(
            self,
            text="🪄  AIconPack",
            font=("Segoe UI", 28, "bold"),
            anchor="w",
        )
        title.grid(row=0, column=0, sticky="w", padx=28, pady=(22, 8))

        # ── Script Select + Pack Options
        self._build_script_frame()

        # ── Icon Prompt + Generate
        self._build_prompt_frame()

        # ── Preview Area
        self._build_preview_area()

        # ── Bottom (Pack Button + Status + ProgressBar)
        self._build_bottom_bar()

        # ---------- State ---------- #
        self.generated_icon_path: Path | None = None
        self.preview_img_obj = None

    # ===================================================================== #
    #   UI – SCRIPT
    # ===================================================================== #
    def _build_script_frame(self):
        frame = ctk.CTkFrame(self, corner_radius=12)
        frame.grid(row=1, column=0, padx=20, pady=6, sticky="ew")
        frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(frame, text="主脚本:", font=("", 14)).grid(row=0, column=0, padx=(16, 10), pady=16)
        self.script_entry = ctk.CTkEntry(frame, placeholder_text="请选择需打包的 Python 主脚本 (*.py)")
        self.script_entry.grid(row=0, column=1, sticky="ew", pady=16)
        ctk.CTkButton(frame, text="浏览", width=80, command=self.browse_script).grid(row=0, column=2, padx=(10, 16))

        # ---- 打包选项 (switches) ---- #
        switch_frame = ctk.CTkFrame(frame, fg_color="transparent")
        switch_frame.grid(row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 14))

        self.opt_onefile   = ctk.CTkSwitch(switch_frame, text="单文件 (--onefile)", progress_color="#3B82F6")
        self.opt_windowed  = ctk.CTkSwitch(switch_frame, text="窗口模式 (--noconsole)", progress_color="#3B82F6")
        self.opt_clean     = ctk.CTkSwitch(switch_frame, text="清理临时 (--clean)", progress_color="#3B82F6")
        self.opt_debug     = ctk.CTkSwitch(switch_frame, text="调试信息 (--debug)", progress_color="#3B82F6")
        self.opt_onefile.select()
        self.opt_windowed.select()
        self.opt_clean.select()

        for i, sw in enumerate((self.opt_onefile, self.opt_windowed, self.opt_clean, self.opt_debug)):
            sw.grid(row=0, column=i, padx=(0, 24))

    # ===================================================================== #
    #   UI – PROMPT
    # ===================================================================== #
    def _build_prompt_frame(self):
        frame = ctk.CTkFrame(self, corner_radius=12)
        frame.grid(row=2, column=0, sticky="nsew", padx=20, pady=(6, 0))
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(1, weight=1)

        # ---- Prompt 输入 ----
        ctk.CTkLabel(frame, text="Icon 描述 Prompt:", font=("", 14)).grid(row=0, column=0, padx=(16, 10), pady=(16, 10))
        self.prompt_entry = ctk.CTkEntry(frame, placeholder_text="如: 极简扁平风蓝色日历应用图标")
        self.prompt_entry.grid(row=0, column=1, sticky="ew", pady=(16, 10), padx=(0, 10))

        # ---- 模板 & Size ----
        self.style_menu = ctk.CTkOptionMenu(frame, values=["(无模板)"] + self.icon_gen.list_templates())
        self.style_menu.set("(无模板)")
        self.style_menu.grid(row=0, column=2, padx=(0, 10))

        self.size_menu = ctk.CTkOptionMenu(frame, values=["256x256", "512x512", "1024x1024"])
        self.size_menu.set("512x512")
        self.size_menu.grid(row=0, column=3, padx=(0, 16))

        # ---- 生成按钮 ----
        self.btn_generate = ctk.CTkButton(frame, text="🎨 生成 Icon", width=140, command=self.start_generate_icon)
        self.btn_generate.grid(row=0, column=4, padx=(4, 16))

        # ---- 预览 Label 容器 ----
        self.preview_label = ctk.CTkLabel(frame, text="预览区", width=480, height=360, corner_radius=8, fg_color="#1a1a1a")
        self.preview_label.grid(row=1, column=0, columnspan=5, sticky="nsew", padx=16, pady=(0, 16))

    # ===================================================================== #
    #   UI – BOTTOM
    # ===================================================================== #
    def _build_bottom_bar(self):
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=3, column=0, sticky="ew", padx=20, pady=12)
        bottom.columnconfigure(0, weight=1)

        self.btn_pack = ctk.CTkButton(bottom, text="📦 打包应用", height=42, command=self.start_pack, state="disabled")
        self.btn_pack.grid(row=0, column=0, sticky="ew")

        self.progress = ctk.CTkProgressBar(bottom, mode="indeterminate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        self.progress.stop()

        self.status = ctk.CTkLabel(bottom, text="状态: 就绪", anchor="w")
        self.status.grid(row=2, column=0, sticky="w")

    # ===================================================================== #
    #   EVENTS
    # ===================================================================== #
    def browse_script(self):
        path = filedialog.askopenfilename(filetypes=[("Python files", "*.py")])
        if path:
            self.script_entry.delete(0, "end")
            self.script_entry.insert(0, path)

    # ---- ICON GENERATION
    def start_generate_icon(self):
        prompt = self.prompt_entry.get().strip()
        if not prompt:
            messagebox.showwarning("提示", "请输入 Icon Prompt")
            return
        style = self.style_menu.get()
        style = None if style == "(无模板)" else style
        size = self.size_menu.get()

        self.btn_generate.configure(state="disabled")
        self.status.configure(text="状态: 正在生成 Icon…")
        self.progress.start()

        threading.Thread(
            target=self._thread_generate_icon,
            args=(prompt, style, size),
            daemon=True,
        ).start()

    def _thread_generate_icon(self, prompt, style, size):
        try:
            result_paths = self.icon_gen.generate(
                prompt,
                style=style,
                size=size,
                n=1,
                convert_to_ico=True,
            )
            self.generated_icon_path = result_paths[0]
            img = Image.open(self.generated_icon_path)
            preview = ctk.CTkImage(img, size=(min(420, img.width), min(420, img.height)))
            self.after(0, self._show_preview, preview)
        except Exception as e:
            self.after(0, self._update_status, f"生成失败: {e}")
        finally:
            self.after(0, self._reset_generate_btn)

    def _show_preview(self, image_obj):
        self.preview_label.configure(image=image_obj, text="")
        self.preview_img_obj = image_obj
        self._update_status("Icon 已生成，准备打包")
        self.btn_pack.configure(state="normal")

    def _reset_generate_btn(self):
        self.btn_generate.configure(state="normal")
        self.progress.stop()

    # ---- PACKING
    def start_pack(self):
        script = self.script_entry.get().strip()
        if not script or not Path(script).exists():
            messagebox.showerror("错误", "请选择有效的 Python 主脚本")
            return
        if not self.generated_icon_path:
            messagebox.showwarning("提示", "请先生成 Icon")
            return

        self.btn_pack.configure(state="disabled")
        self.progress.start()
        self._update_status("正在打包… (耗时取决于脚本大小和依赖)")

        threading.Thread(
            target=self._thread_pack,
            args=(script,),
            daemon=True,
        ).start()

    def _thread_pack(self, script):
        try:
            result = self.packer.pack(
                script_path=script,
                icon=self.generated_icon_path,
                onefile=self.opt_onefile.get(),
                windowed=self.opt_windowed.get(),
                clean=self.opt_clean.get(),
                debug=self.opt_debug.get(),
            )
            log = Path("pack_log.txt")
            log.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
            if result.returncode == 0:
                self.after(0, self._update_status, "打包成功！输出已生成 dist/ 目录")
            else:
                self.after(0, self._update_status, f"打包失败！请查看 {log}")
        except Exception as e:
            self.after(0, self._update_status, f"打包异常: {e}")
        finally:
            self.after(0, self._reset_pack_btn)

    def _reset_pack_btn(self):
        self.btn_pack.configure(state="normal")
        self.progress.stop()

    # ---- UTIL
    def _update_status(self, msg: str):
        self.status.configure(text=f"状态: {msg}")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    try:
        AIconPackGUI().mainloop()
    except Exception as err:
        messagebox.showerror("错误", str(err))