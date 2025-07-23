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

import io
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal, Sequence

import requests
from PIL import Image
from openai import OpenAI

import customtkinter as ctk
from tkinter import filedialog, messagebox


# --------------------------------------------------------------------------- #
# 1) AI 生成模块
# --------------------------------------------------------------------------- #
class IconGenerator:
    """负责和 OpenAI 通信，生成并保存 icon"""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY 未设置")
        self.client = OpenAI(api_key=self.api_key)

    def generate(
        self,
        prompt: str,
        *,
        size: Literal["256x256", "512x512", "1024x1024"] = "1024x1024",
        model: str = "dall-e-3",
        output_dir: str | Path = "icons",
        filename: str | None = None,
    ) -> Path:
        """向 OpenAI 请求生成一张 PNG icon 并保存"""
        response = self.client.images.generate(model=model, prompt=prompt, n=1, size=size)
        url = response.data[0].url
        img_bytes = requests.get(url, timeout=60).content
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

        output_dir = Path(output_dir).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        if filename is None:
            filename = f"icon_{datetime.now():%Y%m%d_%H%M%S}.png"
        path = output_dir / filename
        img.save(path, format="PNG")
        return path


# --------------------------------------------------------------------------- #
# 2) 打包模块
# --------------------------------------------------------------------------- #
class PyInstallerPacker:
    """封装 PyInstaller 打包流程"""

    def __init__(self, onefile: bool = True, clean: bool = True, noconsole: bool = False) -> None:
        self.onefile = onefile
        self.clean = clean
        self.noconsole = noconsole

    def pack(
        self,
        script_path: str | Path,
        *,
        icon_path: str | Path | None = None,
        add_data: Sequence[str] | None = None,
        output_dir: str | Path | None = None,
        extra_args: Sequence[str] | None = None,
    ) -> subprocess.CompletedProcess:
        """
        调用 PyInstaller 打包。
        - script_path: 主启动脚本
        - icon_path: ICO / PNG（PyInstaller 会自动转换）文件
        - add_data: 额外资源，如 ["data.txt;data"]
        - output_dir: dist 目录
        """
        cmd = [sys.executable, "-m", "PyInstaller", str(script_path)]

        if self.onefile:
            cmd.append("--onefile")
        if self.clean:
            cmd.append("--clean")
        if self.noconsole:
            cmd.append("--noconsole")
        if icon_path:
            cmd += ["--icon", str(icon_path)]
        if add_data:
            for item in add_data:
                cmd += ["--add-data", item]
        if output_dir:
            cmd += ["--distpath", str(output_dir)]
        if extra_args:
            cmd += list(extra_args)

        return subprocess.run(cmd, check=False, capture_output=True, text=True)


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