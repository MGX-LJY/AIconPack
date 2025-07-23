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
    """现代化 GUI，将 IconGenerator + PyInstallerPacker 集成"""

    def __init__(self) -> None:
        super().__init__()
        self.title("AIconPack · AI 打包器")
        self.geometry("820x600")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # ---------- 对象 ----------
        self.icon_gen = IconGenerator()
        self.packer = PyInstallerPacker()

        # ---------- 布局 ----------
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        # 1) 脚本选择
        script_frame = ctk.CTkFrame(self, fg_color="transparent")
        script_frame.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="ew")
        script_frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(script_frame, text="Python 脚本:").grid(row=0, column=0, padx=(0, 10))
        self.script_entry = ctk.CTkEntry(script_frame, placeholder_text="选择要打包的主脚本")
        self.script_entry.grid(row=0, column=1, sticky="ew")
        ctk.CTkButton(script_frame, text="浏览", command=self.browse_script).grid(row=0, column=2, padx=(10, 0))

        # 2) Prompt / Model / Size
        prompt_frame = ctk.CTkFrame(self, fg_color="transparent")
        prompt_frame.grid(row=1, column=0, padx=20, pady=10, sticky="ew")
        prompt_frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(prompt_frame, text="Icon Prompt:").grid(row=0, column=0, padx=(0, 10))
        self.prompt_entry = ctk.CTkEntry(prompt_frame, placeholder_text="极简蓝色日历应用图标")
        self.prompt_entry.grid(row=0, column=1, sticky="ew")

        model_size_frame = ctk.CTkFrame(self, fg_color="transparent")
        model_size_frame.grid(row=2, column=0, padx=20, pady=(0, 10), sticky="ew")

        self.model_menu = ctk.CTkOptionMenu(model_size_frame, values=["dall-e-3", "gpt-image-1"])
        self.model_menu.set("dall-e-3")
        self.model_menu.grid(row=0, column=0, padx=(0, 6))

        self.size_menu = ctk.CTkOptionMenu(model_size_frame, values=["256x256", "512x512", "1024x1024"])
        self.size_menu.set("512x512")
        self.size_menu.grid(row=0, column=1, padx=(0, 6))

        self.gen_icon_btn = ctk.CTkButton(model_size_frame, text="🎨 生成 Icon", command=self.start_generate_icon)
        self.gen_icon_btn.grid(row=0, column=2, padx=(6, 0))

        # 3) 预览
        self.preview_label = ctk.CTkLabel(self, text="预览区", anchor="center")
        self.preview_label.grid(row=3, column=0, padx=20, pady=10, sticky="nsew")

        # 4) 打包按钮 & 状态
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.grid(row=4, column=0, padx=20, pady=(0, 20), sticky="ew")
        bottom_frame.columnconfigure(0, weight=1)

        self.pack_btn = ctk.CTkButton(bottom_frame, text="📦 一键打包", command=self.start_pack, state="disabled")
        self.pack_btn.grid(row=0, column=0, sticky="ew")

        self.status_label = ctk.CTkLabel(bottom_frame, text="状态: 就绪")
        self.status_label.grid(row=1, column=0, pady=(6, 0), sticky="w")

        # ---------- 变量 ----------
        self.generated_icon_path: Path | None = None
        self.preview_image_obj = None  # 保存 CTkImage 引用

    # --------------------------------------------------------------------- #
    #   GUI 事件
    # --------------------------------------------------------------------- #
    def browse_script(self):
        path = filedialog.askopenfilename(filetypes=[("Python Files", "*.py")])
        if path:
            self.script_entry.delete(0, "end")
            self.script_entry.insert(0, path)

    def start_generate_icon(self):
        prompt = self.prompt_entry.get().strip()
        if not prompt:
            messagebox.showwarning("提示", "请先输入 Icon 描述")
            return
        self.status("正在生成 icon…")
        self.gen_icon_btn.configure(state="disabled")

        threading.Thread(
            target=self._thread_generate_icon,
            args=(prompt, self.size_menu.get(), self.model_menu.get()),
            daemon=True,
        ).start()

    def _thread_generate_icon(self, prompt: str, size: str, model: str):
        try:
            path = self.icon_gen.generate(prompt, size=size, model=model)
            self.generated_icon_path = path
            img = Image.open(path)
            preview = ctk.CTkImage(img, size=(min(384, img.width), min(384, img.height)))
            self.after(0, self._show_preview, preview)
        except Exception as e:
            self.after(0, self.status, f"生成失败: {e}")
        finally:
            self.after(0, self.gen_icon_btn.configure, {"state": "normal"})

    def _show_preview(self, preview):
        self.preview_label.configure(image=preview, text="")
        self.preview_image_obj = preview
        self.status("icon 生成完成！可开始打包")
        self.pack_btn.configure(state="normal")

    def start_pack(self):
        script = self.script_entry.get().strip()
        if not script:
            messagebox.showwarning("提示", "请先选择要打包的脚本")
            return
        if not Path(script).exists():
            messagebox.showerror("错误", "脚本文件不存在")
            return
        if not self.generated_icon_path or not self.generated_icon_path.exists():
            messagebox.showwarning("提示", "请先生成 icon")
            return

        self.status("正在打包… (耗时取决于脚本大小)")
        self.pack_btn.configure(state="disabled")

        threading.Thread(
            target=self._thread_pack,
            args=(script, self.generated_icon_path),
            daemon=True,
        ).start()

    def _thread_pack(self, script: str, icon: Path):
        try:
            result = self.packer.pack(script_path=script, icon_path=icon)
            log_path = Path("pack_log.txt")
            log_path.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
            if result.returncode == 0:
                self.after(0, self.status, f"打包成功！dist 目录已生成 (日志: {log_path})")
            else:
                self.after(0, self.status, f"打包失败！请查看 pack_log.txt")
        except Exception as e:
            self.after(0, self.status, f"打包异常: {e}")
        finally:
            self.after(0, self.pack_btn.configure, {"state": "normal"})

    def status(self, text: str):
        self.status_label.configure(text=f"状态: {text}")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    try:
        AIconPackGUI().mainloop()
    except Exception as err:
        messagebox.showerror("错误", str(err))