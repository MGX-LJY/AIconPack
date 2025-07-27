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
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, Toplevel, Label
from typing import Any, Iterable, List, Literal, Mapping, Optional, Sequence

# ────────────────────  3rd-party  ────────────────────
import customtkinter as ctk
import requests
from PIL import Image, ImageDraw
from openai import OpenAI, APIConnectionError, RateLimitError

# --------------------------------------------------------------------------- #
# 1) AI 生成模块
# --------------------------------------------------------------------------- #

# DALL·E 3 支持的**固定分辨率集合**。
# 说明：
# - OpenAI 的 DALL·E 3 目前只允许使用有限的几个尺寸。
# - 若传入其他尺寸，会报错或被自动调整。因此我们在生成前做一次“白名单校验”。
DALLE3_SIZES: set[str] = {"1024x1024", "1024x1792", "1792x1024"}


class IconGenerator:
    """
    IconGenerator —— 负责与 OpenAI 图像生成接口交互，产出软件图标（PNG/ICO/内存对象等）。

    ★ 设计要点与约束：
    1) **懒加载 OpenAI 客户端**：构造时不强制需要 API Key（GUI 启动时可以没有 Key），
       真正调用 `generate()` 时若发现未设置，则抛出明确的错误。
    2) **模板系统**：支持以 `{prompt}` 为占位符的模板，便于复用风格（如“极简”“拟物”等）。
    3) **尺寸/模型容错**：对 `dall-e-3` 强制限制尺寸到官方支持的集合（否则改为 1024x1024）。
    4) **批量生成**：
       - DALL·E 3 的限制：每次请求 `n=1`，想要 N 张就循环 N 次；
       - 其他模型：允许一次 `n<=10`。
    5) **指数退避重试**：网络波动或限流 (`RateLimitError`) 时，按 2^retries 秒等待重试。
    6) **输出多形态**：支持返回磁盘路径（默认）、PIL 对象、原始字节、base64 字符串。

    参数
    ----
    api_key : str | None
        OpenAI API Key。可留空，稍后从环境变量读取；generate 时会验证。
    base_url : str | None
        自定义 OpenAI API Base URL（例如代理/中转），也可从环境变量读取。
    prompt_templates : Mapping[str, str] | None
        Prompt 模板字典，键为模板名、值为模板字符串（需包含 {prompt}）。
    request_timeout : int
        下载生成图片时的超时时间（秒）。对 `requests.get` 生效。
    max_retries : int
        调用 OpenAI 发生连接/限流错误时的最大重试次数。

    属性
    ----
    templates : dict[str, str]
        可被 GUI 编辑/新增的模板集合。
    _client : OpenAI | None
        懒加载的 OpenAI 客户端实例。只有在持有 API Key 时才会创建。
    timeout : int
        HTTP 下载超时时间；用于拉取图片 URL。
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
        # 优先使用入参，其次 fallback 到环境变量。
        # 这样用户既可以在 GUI 里填写，也可以在 shell 中通过环境变量注入。
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")

        # 将 Mapping 复制为普通 dict，避免外部对象被修改影响内部行为。
        self.templates = dict(prompt_templates or {})

        # 下载图片时的超时设置（单位秒），与 OpenAI SDK 的超时不同。
        self.timeout = request_timeout

        # 发生 APIConnectionError / RateLimitError 时的最大重试次数。
        self.max_retries = max_retries

        # 懒加载客户端（Lazy init）：
        # - 若此时已有 api_key，则立刻构造 client；
        # - 若无，则先置为 None，等第一次 generate() 时再检查/报错。
        self._client: OpenAI | None = None
        if self.api_key:
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    # ---------------- 模板管理 API ---------------- #
    def add_template(self, name: str, template: str, *, overwrite: bool = False) -> None:
        """
        新增（或覆盖）一个 Prompt 模板。

        参数
        ----
        name : str
            模板名，对应 GUI 下拉值。
        template : str
            模板内容，必须包含 `{prompt}` 占位符，例如：
            "Create a minimal, flat icon: {prompt}."
        overwrite : bool
            若为 False 且同名已存在，则抛出 ValueError 防止误覆盖。
        """
        if name in self.templates and not overwrite:
            raise ValueError(f"模板 '{name}' 已存在")
        # 不强制检查是否包含 {prompt}，以免限制过死；
        # 但实际模板建议包含，以便将用户输入拼接进去。
        self.templates[name] = template

    def list_templates(self) -> list[str]:
        """
        返回当前所有模板名称列表（用于 GUI 生成下拉菜单）。
        """
        return list(self.templates)

    # ---------------- 图像生成主流程 ---------------- #
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
            compress_level: int | None = None,  # 0-9，None 表示不压缩
    ) -> List[Any]:
        """
        调用 OpenAI 图像接口生成 icon，并将结果以多种格式返回/保存。

        参数
        ----
        prompt : str
            用户自然语言描述（如“极简扁平风蓝色日历图标”）。
        style : str | None
            模板名称；若提供，将使用模板字符串格式化：template.format(prompt=prompt)。
        extra_keywords : Sequence[str] | None
            额外关键词（英文逗号拼接到 Prompt 尾部），用于快速微调风格。
        size : str
            目标分辨率；对 DALL·E 3 会强制限制在 DALLE3_SIZES 白名单内。
        model : str
            模型名称，默认 "dall-e-3"。
        n : int
            期望生成的图片数量。对 DALL·E 3 会“循环调用”以绕过 n=1 限制。
        output_dir : str | Path
            当 return_format="path" 时，PNG 输出目录。
        filename_prefix : str | None
            输出文件名前缀；未指定则使用 "icon_YYYYmmdd_HHMMSS" 格式。
        return_format : Literal["path", "pil", "bytes", "b64"]
            返回结果类型：
            - "path"  →  [Path, ...]（写入磁盘 PNG/ICO）
            - "pil"   →  [PIL.Image.Image, ...]（仅在内存中，不落盘）
            - "bytes" →  [bytes, ...]（原始字节流）
            - "b64"   →  [str(base64), ...]（Base64 编码字符串）
        convert_to_ico : bool
            保存 PNG 后是否同步产出 ICO（适合 Windows）。
        compress_level : int | None
            PNG 压缩等级 0~9；None 表示不指定（让 Pillow 用默认）。

        返回
        ----
        List[Any]
            与 return_format 对应的列表。元素个数与 n 相同。
            - path：Path 对象列表
            - pil：PIL.Image.Image 列表
            - bytes：bytes 列表
            - b64：str 列表

        可能抛出的异常
        -------------
        RuntimeError：未提供 API Key / 网络重试用尽 / OpenAI SDK 抛出错误。
        """

        # ── 1) 客户端就绪性检查（Lazy init） ──────────────────────────
        # 如果构造器里没有 API Key，这里再查一次；没有就给出友好错误。
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("请先提供 OpenAI API Key")
            # 此处才真正创建客户端；允许用户在 GUI 设置里晚一点再填 Key。
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        # ── 2) 尺寸容错：DALL·E 3 仅支持固定尺寸 ─────────────────────
        # 若用户传入了非白名单尺寸，为避免 API 报错，这里直接回退到 1024x1024。
        if model == "dall-e-3" and size not in DALLE3_SIZES:
            size = "1024x1024"

        # ── 3) 组装 Prompt（模板 + 用户文本 + 额外关键词）─────────────
        # 风格模板：从 self.templates 中查找 style 名称；找不到则退回 "{prompt}"。
        # 注意：模板字符串允许包含更复杂的指令（如语言风格、材质、阴影等）。
        full_prompt = (
            self.templates.get(style, "{prompt}").format(prompt=prompt)
            if style else prompt
        )
        # 附加关键词：常用于快速微调（如 "minimal, flat, blue tones"）。
        if extra_keywords:
            full_prompt += ", " + ", ".join(extra_keywords)

        # ── 4) 调用 OpenAI（考虑 n 的差异与网络重试）─────────────────
        # DALL·E 3 限制：一次请求只能返回 1 张（n=1），因此我们将“请求次数=batches=n”；
        # 其他模型：一次最多 n<=10（保守处理），因此可一次性请求。
        retries = 0  # 已重试次数
        all_data = []  # 用于累积每次请求返回的 data（其中含有 URL）

        if model == "dall-e-3":
            batch_size, batches = 1, n
        else:
            # 对非 DALL·E 3 模型，允许一次请求最多 10 张（避免过大）。
            batch_size = min(max(n, 1), 10)
            batches = 1

        # 使用一个 while True + try/except 的重试包装：
        # - 仅对 APIConnectionError / RateLimitError 做指数退避重试；
        # - 其它异常直接抛出（让调用者知道真实错误）。
        while True:
            try:
                for _ in range(batches):
                    # 这里调用 OpenAI 图像生成接口：
                    # - response_format="url"：得到图片下载地址，随后我们用 requests.get 拉取字节。
                    rsp = self._client.images.generate(
                        model=model,
                        prompt=full_prompt,
                        n=batch_size,
                        size=size,
                        response_format="url",
                    )
                    # rsp.data 是若干个“生成结果”的列表（每个元素含有 url/base64 等字段，取决于 response_format）
                    all_data.extend(rsp.data)

                # 成功则跳出重试循环
                break

            except (APIConnectionError, RateLimitError) as e:
                # 网络抖动 / 速率限制：尝试指数退避重试
                retries += 1
                if retries > self.max_retries:
                    # 超过最大重试次数：抛出更友好的错误，保留原始异常上下文（from e）
                    raise RuntimeError(f"请求失败：{e}") from e
                # 等待时间：2^retries 秒（2, 4, 8, ...）
                time.sleep(2 ** retries)

        # ── 5) 下载/保存/格式化输出 ───────────────────────────────────
        # 说明：
        # - 当 return_format="path"：会将 PNG 保存到 output_dir，并按 prefix 命名；
        #   可选 convert_to_ico=True 时，同步生成 ICO 文件（尺寸 256x256）。
        # - 其他 return_format：只在内存里处理，不在磁盘落盘。
        out_dir = Path(output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        # 若未提供文件前缀，则以时间戳生成，避免覆盖。
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = filename_prefix or f"icon_{ts}"

        results: List[Any] = []

        # 遍历每个“生成结果”元素（其中含有 URL）
        for idx, item in enumerate(all_data, 1):
            # 1) 下载图片二进制
            #    这里我们直接用 requests.get，并设置 self.timeout 防止卡死。
            img_bytes = requests.get(item.url, timeout=self.timeout).content

            # 2) 解码为 Pillow 图像并转为 RGBA（带 alpha 通道，适合图标）
            img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

            # === 返回形式一：内存返回（不写磁盘） ======================
            if return_format == "pil":
                results.append(img)  # 返回 PIL.Image.Image
                continue
            if return_format == "bytes":
                results.append(img_bytes)  # 返回原始字节
                continue
            if return_format == "b64":
                results.append(base64.b64encode(img_bytes).decode())  # 返回 base64 字符串
                continue

            # === 返回形式二：写入磁盘（默认 path） ======================
            # 根据 n 的数量决定是否在文件名后加索引：1 张时不加，>1 时追加 _{idx}
            name = f"{prefix}_{idx}.png" if n > 1 else f"{prefix}.png"
            png_path = out_dir / name

            # 组织 Pillow 的保存参数：
            # - compress_level：PNG 压缩级别 0~9；
            # - optimize=True：让 Pillow 做额外的压缩优化。
            save_kwargs = {}
            if isinstance(compress_level, int):
                save_kwargs.update(
                    optimize=True,
                    compress_level=max(0, min(compress_level, 9))
                )

            # 保存 PNG（RGBA 保留透明度）
            img.save(png_path, format="PNG", **save_kwargs)

            # 如需同时产出 ICO（常见于 Windows 快捷方式/EXE 图标）
            if convert_to_ico:
                # ICO 通常使用 256x256 的图像；这里做一次 resize 并保存 .ico
                img.resize((256, 256)).save(
                    png_path.with_suffix(".ico"),
                    format="ICO"
                )

            # 将“磁盘路径”作为结果返回给调用方
            results.append(png_path)

        return results


# --------------------------------------------------------------------------- #
# 2) 打包模块
# --------------------------------------------------------------------------- #
class PyInstallerPacker:
    """
    PyInstallerPacker —— 对 `pyinstaller` 命令行进行“可编程封装”。

    设计目标
    --------
    1) 用 **Python 列表**构建命令（而非字符串拼接），避免 shell 注入与跨平台转义问题；
    2) 将 PyInstaller 的常用开关参数化（onefile / noconsole / clean / debug / upx / 各类路径）；
    3) 既可 **dry-run** 返回命令数组供调用者调试，也可直接 `subprocess.run()` 执行并返回结果；
    4) 提供 `create_version_file()` 辅助在 Windows 打包时生成 `.version` 文件。

    参数（构造器）
    ------------
    onefile : bool
        映射 `--onefile`。单文件打包（将所有内容打包到一个可执行文件）。
    windowed : bool
        映射 `--noconsole`。Windows/macOS GUI 应用常用，隐藏控制台窗口。
    clean : bool
        映射 `--clean`。在打包前清理 PyInstaller 的临时缓存，加快二次打包定位问题。
    debug : bool
        映射 `--debug`。开启调试模式，生成的程序包含额外的调试信息（体积更大）。
    upx : bool
        启用 UPX 压缩。**注意**：PyInstaller 会尝试自动探测系统 PATH 中的 `upx`；
        若需指定目录，可配合 `upx_dir` 与 `--upx-dir` 使用。
    upx_dir : str | Path | None
        `upx` 可执行所在目录。若提供，将追加 `--upx-dir <dir>`。
        若不提供，**建议不要**传 `--upx-dir`，让 PyInstaller 自行在 PATH 中寻找。
    pyinstaller_exe : str | Path | None
        运行 PyInstaller 的解释器或可执行路径。默认使用当前进程的 `sys.executable`，
        并通过 `-m PyInstaller` 的方式调用。这样能兼容虚拟环境/隔离环境。

    属性
    ----
    pyinstaller_exe : str
        实际用于执行的“python 可执行路径”。随后构建命令为：
        `[pyinstaller_exe, "-m", "PyInstaller", <script_path>, ...]`
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
        # 这些布尔/路径属性会在 build_cmd() 内被翻译成具体 CLI 开关
        self.onefile = onefile
        self.windowed = windowed
        self.clean = clean
        self.debug = debug
        self.upx = upx

        # 规范化 upx_dir 为 Path（或 None）
        self.upx_dir = Path(upx_dir).expanduser() if upx_dir else None

        # 调用 PyInstaller 使用哪个“Python 可执行”：
        # - 默认取当前进程的 Python（sys.executable），然后用 `-m PyInstaller` 方式调用；
        # - 也可以传入某个 venv 的 python 路径，实现隔离打包。
        self.pyinstaller_exe = str(pyinstaller_exe or sys.executable)

    # ---------------------------------------------------------- #
    #  命令构建器：把配置翻译成 `pyinstaller` CLI 参数列表
    # ---------------------------------------------------------- #
    def build_cmd(
            self,
            script_path: str | Path,
            *,
            name: str | None = None,
            icon: str | Path | None = None,
            version_file: str | Path | None = None,
            add_data: Sequence[str] | None = None,  # 约定使用 "src;dest" 作为分隔
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
        将所有参数汇总为“安全的命令列表”（list[str]），
        该列表可直接传给 `subprocess.run(cmd, ...)` 执行。

        关键点
        ------
        - **永远使用列表**：避免 shell 注入与跨平台转义问题；
        - `script_path` 是要打包的 Python 入口脚本（.py）；
        - `name` 映射 `--name`（生成的可执行/目录名称）；
        - `icon` 映射 `--icon`（Windows 推荐 .ico，macOS 推荐 .icns）；
        - `version_file` 仅在 Windows 上追加（`--version-file`）；
        - `add_data` / `add_binary`：这里约定传入 **"src;dest"** 形式的字符串序列；
          （注：PyInstaller 官方在 Windows 用分号，类 Unix 常见是冒号；本项目统一用分号，便于跨平台心智一致；
           如果你的环境需要冒号，可自行在传入前转换）
        - `hidden_imports` / `runtime_hooks` / `exclude_modules`：分别对应多值开关；
        - `dist_dir` / `build_dir` / `workpath` / `spec_path`：控制产物与中间文件位置；
          注意：若同时传入 `build_dir` 与 `workpath`，本实现会 **重复** 添加两次 `--workpath`，
          PyInstaller 最终以**后者**为准（最后一个参数生效）；保持与原逻辑一致。
        - `extra_args`：透传额外的 CLI 片段（最后追加）。
        """
        # 基础：用“python -m PyInstaller <脚本>”调用
        cmd: List[str] = [self.pyinstaller_exe, "-m", "PyInstaller", str(script_path)]

        # 布尔开关类
        if self.onefile:
            cmd.append("--onefile")
        if self.windowed:
            cmd.append("--noconsole")
        if self.clean:
            cmd.append("--clean")
        if self.debug:
            cmd.append("--debug")

        # UPX 相关：
        # - 一般情况下：如果系统 PATH 已能找到 upx，则无需 `--upx-dir`；
        # - 只有在你希望指定特定目录时才加 `--upx-dir <dir>`。
        # **注意**：当前实现当 `self.upx` 为 True 时会先追加 `--upx-dir`，
        # 若 `self.upx_dir` 为 None 则仅追加了标志位 **而无值**，可能导致部分 PyInstaller 版本报错。
        # 若遇到此问题，建议：
        #   1) 要么提供 upx_dir；
        #   2) 要么把 upx=False，让 PyInstaller 自行从 PATH 寻找。
        if self.upx:
            cmd.append("--upx-dir")
            if self.upx_dir:
                cmd.append(str(self.upx_dir))

        # 单值开关
        if name:
            cmd += ["--name", name]
        if icon:
            cmd += ["--icon", str(icon)]
        if version_file and platform.system() == "Windows":
            cmd += ["--version-file", str(version_file)]

        # 多值开关（每个值都要成对拼接：flag value）
        _extend_arg(cmd, "--add-data", add_data)
        _extend_arg(cmd, "--add-binary", add_binary)
        _extend_arg(cmd, "--hidden-import", hidden_imports)
        _extend_arg(cmd, "--runtime-hook", runtime_hooks)
        _extend_arg(cmd, "--exclude-module", exclude_modules)

        # 路径控制
        if key:
            cmd += ["--key", key]
        if dist_dir:
            cmd += ["--distpath", str(dist_dir)]
        if build_dir:
            cmd += ["--workpath", str(build_dir)]  # 注意：与下面 `workpath` 可能重复，后者覆盖前者
        if workpath:
            cmd += ["--workpath", str(workpath)]
        if spec_path:
            cmd += ["--specpath", str(spec_path)]

        # 透传额外参数（例如用户希望追加 `--collect-all some_pkg` 等）
        if extra_args:
            cmd += list(map(str, extra_args))

        return cmd

    # ---------------------------------------------------------- #
    #  执行打包
    # ---------------------------------------------------------- #
    def pack(
            self,
            script_path: str | Path,
            *,
            dry_run: bool = False,
            **kwargs,
    ) -> subprocess.CompletedProcess | list[str]:
        """
        构建命令并（可选）执行。

        参数
        ----
        script_path : str | Path
            入口脚本路径。
        dry_run : bool
            为 True 时仅返回命令数组（list[str]），**不执行**，用于调试/预览；
            为 False 时实际运行并返回 `subprocess.CompletedProcess`。
        **kwargs :
            透传给 `build_cmd()` 的命名参数（见其文档）。

        返回
        ----
        - `dry_run=True`  → `list[str]`
        - `dry_run=False` → `subprocess.CompletedProcess`（含 stdout/stderr/returncode）

        说明
        ----
        这里使用 `capture_output=True, text=True`，因此：
        - `result.stdout` / `result.stderr` 为字符串（而不是字节）；
        - 方便上层 GUI 直接写日志文件或在界面展示。
        """
        cmd = self.build_cmd(script_path, **kwargs)
        if dry_run:
            return cmd
        return subprocess.run(cmd, capture_output=True, text=True)

    # ---------------------------------------------------------- #
    #  辅助：生成 Windows 的 version 信息文件
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
        """
        生成一个可被 PyInstaller（Windows）使用的 version 信息文件（`.version`/`.txt`），
        以便在“文件属性 → 详细信息”中展示公司名、产品名、版本号等信息。

        参数
        ----
        company_name : str
            公司/组织名称。
        file_description : str
            文件描述（一般为应用名称或一句话简介）。
        file_version : str
            文件版本，四段式（例如 "1.2.3.4"）。内部会转为元组 `(1,2,3,4)`。
        product_name : str
            产品名称。
        product_version : str
            产品版本，四段式。
        outfile : str | Path
            输出文件路径（默认 `version_info.txt`）。

        返回
        ----
        Path
            写入后的实际路径。

        用法
        ----
        1) 调用本方法生成 version 文件；
        2) 在 `build_cmd()` 或 `pack()` 中传入 `version_file=该文件路径`（仅 Windows 生效）。
        """
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
    """
    将“多值开关”展开为重复的 `flag value` 片段，并追加到命令列表末尾。

    示例
    ----
    >>> cmd = []
    >>> _extend_arg(cmd, "--hidden-import", ["pkg1", "pkg2"])
    >>> cmd
    ["--hidden-import", "pkg1", "--hidden-import", "pkg2"]

    说明
    ----
    - `values` 为 None 或空时不做任何处理；
    - 本函数纯粹做列表拼接，不做路径/分隔符的合法性校验；
      若你需要对 `--add-data` 的分隔（`;` 或 `:`）做更严格的跨平台处理，
      可在传入本函数之前完成转换。
    """
    if values:
        for v in values:
            cmd += [flag, str(v)]


# --------------------------------------------------------------------------- #
# 3) GUI 模块（customtkinter 实现 · 超详细注释版）
# --------------------------------------------------------------------------- #

# =============== 简易悬浮注解组件 ==========================================
class _ToolTip:
    """
    轻量级悬浮提示工具（鼠标移入显示一小段文本）。

    用法：
        entry = ctk.CTkEntry(...)
        _ToolTip(entry, "这是提示文本")

    设计：
    - 通过绑定 <Enter>/<Leave> 事件，在鼠标移入时创建一个无边框的 Toplevel 并跟随坐标显示；
    - 鼠标离开时销毁该窗口；
    - 仅用于简单文本说明，避免引入第三方“气泡提示”库。
    """

    def __init__(self, widget, text: str):
        self.widget, self.text = widget, text
        self.tip = None  # Toplevel 实例（存在表示已显示）
        # 悬浮进入/离开事件：显示/隐藏提示
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _e):
        """在鼠标位置附近显示提示框。"""
        if self.tip or not self.text:
            return  # 已经显示/没有文本则不重复创建
        self.tip = Toplevel(self.widget)
        # 取消窗口装饰（无标题栏/边框）
        self.tip.wm_overrideredirect(True)
        # 将提示框定位到鼠标右下角 10px 处
        self.tip.wm_geometry(f"+{_e.x_root + 10}+{_e.y_root + 10}")
        # 配置一块深色背景、浅色文字的小标签作为提示载体
        lbl = Label(
            self.tip, text=self.text, justify="left",
            bg="#111", fg="#fff", relief="solid", borderwidth=1,
            font=("Segoe UI", 9)
        )
        lbl.pack(ipadx=6, ipady=2)

    def _hide(self, _e):
        """隐藏并销毁提示框。"""
        if self.tip:
            self.tip.destroy()
            self.tip = None


def _set_tip(widget, text: str):
    """
    语法糖：将 _ToolTip 的创建封装成一个调用，代码更简洁。
    """
    _ToolTip(widget, text)


# =============== 配置文件操作 =============================================
# 设计：
# - 将用户的 API Key、Base URL、Prompt 模板等持久化到用户家目录；
# - 另外**同步导出**一份到程序目录的 `config.json`，便于拷贝/版本管理/CI 使用；
# - JSON 结构示例：
#   { "api_key": "...", "base_url": "https://...", "templates": { "极简": "..." } }
_CFG = Path.home() / ".aiconpack_config.json"

# ☆ 额外导出一份到程序目录，文件名固定 config.json
#   注意：`Path(__file__)` 指向当前脚本所在目录，便于与程序放一起。
CONFIG_EXPORT = Path(__file__).with_name("config.json")  # 也可改成 Path.cwd()/...


def _load_cfg():  # noqa
    """
    读取用户配置（容错）：
    - 若文件存在，尝试解析为 JSON；异常则回退到默认空配置；
    - 若文件不存在，返回默认空配置。
    """
    if _CFG.exists():
        try:
            return json.loads(_CFG.read_text("utf-8"))
        except Exception:
            # 解析失败：不要抛异常影响启动，回退默认配置
            ...
    return {"api_key": "", "base_url": "", "templates": {}}


def _save_cfg(cfg):  # noqa
    """
    将配置写回家目录文件。使用 UTF-8 编码并缩进，便于用户手工编辑。
    """
    _CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


# ☆ 将 cfg 同步导出到程序目录（例如版本控制/分发给同事/CI 用）
def _export_cfg(cfg):  # noqa
    CONFIG_EXPORT.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


# =============== 设置窗口 ===================================================
class SettingsDialog(ctk.CTkToplevel):
    """
    “设置”对话框：
    - 负责配置 OpenAI API Key、Base URL、Prompt 模板（JSON）；
    - 点击“保存”后立刻持久化并通知主窗口更新服务实例。

    生命周期：
    - 使用 `ctk.CTkToplevel`，由主窗体创建与持有；
    - 关闭时只销毁该 Toplevel，不退出整个应用。
    """

    def __init__(self, master: "AIconPackGUI", cfg: dict):
        super().__init__(master)
        self.title("设置")
        self.geometry("520x550")
        self.master: "AIconPackGUI" = master
        self.columnconfigure(0, weight=1)
        self.cfg = cfg  # 当前配置快照（初始填充到输入框）

        # ----------------- API Key -----------------
        ctk.CTkLabel(self, text="OpenAI API Key:", anchor="w", font=("", 14)).grid(
            row=0, column=0, sticky="w", padx=20, pady=(22, 6))
        # 使用 show="•" 隐藏明文
        self.key_ent = ctk.CTkEntry(self, placeholder_text="sk-...", show="•")
        self.key_ent.insert(0, cfg.get("api_key", ""))
        self.key_ent.grid(row=1, column=0, sticky="ew", padx=20)
        _set_tip(self.key_ent, "填写你的 OpenAI 密钥。留空则无法生成图标。")

        # ----------------- Base URL ----------------
        ctk.CTkLabel(self, text="API Base URL (可选):", anchor="w", font=("", 14)).grid(
            row=2, column=0, sticky="w", padx=20, pady=(20, 6))
        self.base_ent = ctk.CTkEntry(self, placeholder_text="https://api.xxx.com/v1")
        self.base_ent.insert(0, cfg.get("base_url", ""))
        self.base_ent.grid(row=3, column=0, sticky="ew", padx=20)
        _set_tip(self.base_ent, "若你使用代理 / 中转服务，可在此配置 Base URL。")

        # ----------------- Prompt 模板（JSON） -----
        ctk.CTkLabel(self, text="Prompt 模板 (JSON):", anchor="w", font=("", 14)).grid(
            row=4, column=0, sticky="w", padx=20, pady=(20, 6))
        self.tpl_txt = ctk.CTkTextbox(self, height=240)
        self.tpl_txt.insert(
            "1.0",
            json.dumps(cfg.get("templates", {}), ensure_ascii=False, indent=2)
        )
        self.tpl_txt.grid(row=5, column=0, sticky="nsew", padx=20)
        # 使文本框所在 row 可扩展，窗口放大时能拉伸
        self.rowconfigure(5, weight=1)
        _set_tip(self.tpl_txt, "键=模板名称，值=模板内容；使用 {prompt} 占位符。")

        # ----------------- 操作按钮 -----------------
        box = ctk.CTkFrame(self, fg_color="transparent")
        box.grid(row=6, column=0, pady=18)
        ctk.CTkButton(box, text="取消", width=110, command=self.destroy).grid(
            row=0, column=0, padx=(0, 12)
        )
        ctk.CTkButton(box, text="保存", width=130, command=self._save).grid(
            row=0, column=1
        )

    def _save(self):
        """
        点击“保存”：
        1) 校验模板 JSON 格式；
        2) 写入家目录与程序目录；
        3) 回调主窗体 `apply_settings` 使配置生效；
        4) 关闭当前设置窗口。
        """
        try:
            text = self.tpl_txt.get("1.0", "end").strip() or "{}"
            tpl_dict = json.loads(text)
            if not isinstance(tpl_dict, dict):
                raise ValueError("模板 JSON 必须是对象")
        except Exception:
            messagebox.showerror("错误", "模板 JSON 格式不正确")
            return

        conf = {
            "api_key": self.key_ent.get().strip(),
            "base_url": self.base_ent.get().strip(),
            "templates": tpl_dict,
        }
        # 持久化 + 导出
        _save_cfg(conf)
        _export_cfg(conf)
        # 通知主窗体刷新内部服务（例如重建 IconGenerator）
        self.master.apply_settings(conf)
        self.destroy()


# =============== 主 GUI =====================================================
class AIconPackGUI(ctk.CTk):
    """
    主窗体（应用入口）：
    - 顶部工具条（标题 + 设置按钮）
    - 两个 Tab：
        1) “AI 生成”页：Prompt 输入、模板选择、尺寸/压缩、数量、生成/圆润/导入/转 ICNS、预览、进度
        2) “PyInstaller 打包”页：脚本/图标选择、输出目录、名称、开关、隐藏导入/数据、打包按钮、自动依赖打包、进度
    - 底部状态栏：显示当前状态文本

    线程与 UI 更新：
    - 生成与打包都在 **后台线程** 运行（threading.Thread, daemon=True）；
    - 回到 UI 的更新统一用 `self.after(0, ...)`，确保在主线程安全执行。

    资源管理：
    - 预览图使用 `self.preview_img` 保持对 `ctk.CTkImage` 的引用，避免被 GC。
    - 生成的最新图标路径缓存于 `self.generated_icon`，便于“打包”页直接引用。
    """

    def __init__(self):
        super().__init__()
        # 设置全局暗色主题（customtkinter 提供）
        ctk.set_appearance_mode("dark")
        self.title("AIconPack · AI 图标生成 & PyInstaller 打包")
        self.geometry("980x720")
        self.minsize(880, 640)

        # ---------- 服务与配置 ----------
        # 读取用户配置（家目录 JSON），随后初始化 IconGenerator
        self.cfg = _load_cfg()
        self._init_services()

        # ---------- 顶部工具条 ----------
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))
        top.columnconfigure(0, weight=1)  # 让标题列撑开
        ctk.CTkLabel(top, text="🪄 AIconPack", font=("Segoe UI", 28, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkButton(
            top, text="⚙️ 设置", width=100,
            command=lambda: SettingsDialog(self, self.cfg)
        ).grid(row=0, column=1, sticky="e")

        # ---------- 主体 Tab ----------
        # 主窗体行列配置：第 1 行承载 Tab，需要可伸缩
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=14, pady=12)
        self.ai_tab = self.tabs.add("AI 生成")
        self.pack_tab = self.tabs.add("PyInstaller 打包")

        # ---------- 状态栏 ----------
        self.status = ctk.CTkLabel(self, text="状态: 就绪", anchor="w")
        self.status.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 10))

        # ---------- 构建各页 ----------
        self._build_ai_page()
        self._build_pack_page()

        # ---------- 运行时状态缓存 ----------
        self.generated_icon: Path | None = None  # 最近生成/导入的图标文件路径
        self.preview_img = None  # 保持对 CTkImage 的引用，防止 GC

    # ---------- 服务 ----------
    def _init_services(self):
        """
        根据 `self.cfg` 初始化/重建服务对象。
        目前只有 IconGenerator；如以后加入更多服务（例如不同图模供应商），在此统一初始化。
        """
        self.icon_gen = IconGenerator(
            api_key=self.cfg.get("api_key"),
            base_url=self.cfg.get("base_url"),
            prompt_templates=self.cfg.get("templates"),
        )

    # ---------- 图标后处理 ----------
    def _smooth_icon(self):
        """
        “圆润处理”：
        - 对当前生成/导入的 PNG 添加圆角 alpha 遮罩（半径=最短边 25%），产生柔和图标效果；
        - 生成新文件，后缀 `_round.png`，并刷新预览；
        - 处理完成后，允许“转为 ICNS”按钮。
        """
        if not self.generated_icon or not Path(self.generated_icon).exists():
            messagebox.showwarning("提示", "请先生成图标")
            return

        img = Image.open(self.generated_icon).convert("RGBA")
        w, h = img.size
        radius = int(min(w, h) * 0.25)  # 圆角半径：最短边的 25%

        # 创建灰度遮罩（L 模式），黑色为透明，白色为不透明
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, w, h), radius=radius, fill=255)

        # 使用遮罩为原图添加 alpha 通道
        img.putalpha(mask)

        # 输出文件名：原名加 _round 后缀
        rounded_path = Path(self.generated_icon).with_stem(
            Path(self.generated_icon).stem + "_round"
        )
        img.save(rounded_path, format="PNG")

        # 刷新状态与预览
        self.generated_icon = rounded_path
        cimg = ctk.CTkImage(img, size=(min(420, w), min(420, h)))
        self.preview_img = cimg  # **持有引用**
        self.preview_lbl.configure(image=cimg, text="")
        self._status("已生成圆润版本")
        self.icns_btn.configure(state="normal")

    # ---------- PNG → ICNS ----------
    def _png_to_icns(self):
        """
        将当前 PNG 转为 macOS 的 .icns 格式：
        - 依赖 Pillow 对 ICNS 的写入支持；
        - 成功后将 `self.generated_icon` 指向 .icns 文件，并提示。
        """
        if not self.generated_icon or not self.generated_icon.suffix.lower() == ".png":
            messagebox.showwarning("提示", "请先生成或导入 PNG 图标")
            return

        try:
            img = Image.open(self.generated_icon)
            icns_path = self.generated_icon.with_suffix(".icns")
            img.save(icns_path)  # Pillow 近期版本支持保存 ICNS
        except Exception as e:
            messagebox.showerror("错误", f"转换失败: {e}")
            return

        self.generated_icon = icns_path
        self._status(f"已生成 {icns_path.name}，可在『打包』页使用")
        messagebox.showinfo("成功", f"已生成 {icns_path}")

    def _browse_icon(self):
        """
        手动选择 .ico / .png 作为打包图标，并填入“图标文件”输入框。
        """
        p = filedialog.askopenfilename(filetypes=[("Icon files", "*.ico *.png")])
        if p:
            self.icon_ent.delete(0, "end")
            self.icon_ent.insert(0, p)

    def _use_generated_icon(self):
        """
        将“最近生成/导入”的图标路径写入“图标文件”输入框，便于直接打包使用。
        """
        if not self.generated_icon:
            messagebox.showwarning("提示", "尚未生成图标")
            return
        self.icon_ent.delete(0, "end")
        self.icon_ent.insert(0, str(self.generated_icon))

    # ========== AI PAGE ==========
    def _build_ai_page(self):
        """
        构建“AI 生成”标签页：
        - Prompt 输入、模板选择、分辨率、PNG 压缩、数量；
        - 操作按钮：生成 / 圆润处理 / 导入图片 / 转为 ICNS；
        - 预览区域与进度条。
        - 布局策略：尽量在窄窗口下也能看到所有控件（将按钮独立成一行）。
        """
        p = self.ai_tab
        p.columnconfigure(1, weight=1)  # 主输入列可伸缩
        p.rowconfigure(5, weight=1)  # 预览区域可伸缩

        # --- Row-0: Prompt --------------------------------------------
        ctk.CTkLabel(p, text="Prompt:", font=("", 14)).grid(
            row=0, column=0, sticky="e", padx=18, pady=(16, 6)
        )
        self.prompt_ent = ctk.CTkEntry(p, placeholder_text="极简扁平风蓝色日历图标")
        self.prompt_ent.grid(
            row=0, column=1, columnspan=10, sticky="ew", padx=18, pady=(16, 6)
        )

        # --- Row-1: 模板 + 尺寸 + 压缩滑块 -----------------------------
        ctk.CTkLabel(p, text="模板:", font=("", 12)).grid(row=1, column=0, sticky="e", padx=6)
        self.style_opt = ctk.CTkOptionMenu(
            p, values=["(无模板)"] + self.icon_gen.list_templates()
        )
        self.style_opt.set("(无模板)")
        self.style_opt.grid(row=1, column=1, padx=6, pady=4)

        ctk.CTkLabel(p, text="分辨率:", font=("", 12)).grid(row=1, column=2, sticky="e", padx=6)
        self.size_opt = ctk.CTkOptionMenu(
            p, values=["1024x1024", "1024x1792", "1792x1024"]
        )
        self.size_opt.set("1024x1024")
        self.size_opt.grid(row=1, column=3, padx=6, pady=4)

        ctk.CTkLabel(p, text="PNG 压缩:", font=("", 12)).grid(row=1, column=4, sticky="e", padx=6)
        self.comp_slider = ctk.CTkSlider(p, from_=0, to=9, number_of_steps=9, width=150)
        self.comp_slider.set(6)  # 默认中等压缩
        self.comp_slider.grid(row=1, column=5, padx=6)

        # --- Row-2: 操作按钮 -------------------------------------------
        row_btn = 2
        self.gen_btn = ctk.CTkButton(p, text="🎨 生成", width=110, command=self._start_generate)
        self.gen_btn.grid(row=row_btn, column=2, padx=6, pady=2)

        self.smooth_btn = ctk.CTkButton(
            p, text="✨ 圆润处理", width=110, command=self._smooth_icon, state="disabled"
        )
        self.smooth_btn.grid(row=row_btn, column=3, padx=6, pady=2)

        self.import_btn = ctk.CTkButton(
            p, text="📂 导入图片", width=110, fg_color="#455A9C", command=self._import_image
        )
        self.import_btn.grid(row=row_btn, column=4, padx=6, pady=2)

        self.icns_btn = ctk.CTkButton(
            p, text="💾 转为 ICNS", width=110, command=self._png_to_icns,
            fg_color="#2D7D46", state="disabled"
        )
        self.icns_btn.grid(row=row_btn, column=5, padx=6, pady=2)

        # --- 预览区域 --------------------------------------------------
        self.preview_lbl = ctk.CTkLabel(
            p, text="预览区域", fg_color="#151515", width=520, height=380, corner_radius=8
        )
        self.preview_lbl.grid(
            row=row_btn + 2, column=0, columnspan=11, sticky="nsew", padx=18, pady=(10, 16)
        )

        # --- 进度条 ----------------------------------------------------
        self.ai_bar = ctk.CTkProgressBar(p, mode="indeterminate")
        self.ai_bar.grid(
            row=row_btn + 3, column=0, columnspan=11, sticky="ew", padx=18, pady=(0, 12)
        )
        self.ai_bar.stop()

    # ========== PACK PAGE ==========
    def _build_pack_page(self):
        """
        构建“PyInstaller 打包”标签页：
        - 入口脚本、图标文件、输出目录（dist）、应用名称；
        - 常用开关：--onefile / --noconsole / --clean / --debug / UPX / 仅保留可执行；
        - hidden-imports / add-data；
        - 打包按钮 & 自动依赖打包按钮；
        - 打包进度条。
        """
        p = self.pack_tab
        p.columnconfigure(0, weight=1)
        p.columnconfigure(2, weight=1)

        outer = ctk.CTkFrame(p, fg_color="transparent")
        outer.grid(row=0, column=1, sticky="n", pady=12)
        outer.columnconfigure(1, weight=1)

        row = 0
        # --- 入口脚本 ---------------------------------------------------
        ctk.CTkLabel(outer, text="入口脚本:", font=("", 14)).grid(
            row=row, column=0, sticky="e", pady=8, padx=10
        )
        self.script_ent = ctk.CTkEntry(outer, placeholder_text="app.py")
        self.script_ent.grid(row=row, column=1, sticky="ew", pady=8)
        ctk.CTkButton(outer, text="浏览", width=90, command=self._browse_script).grid(
            row=row, column=2, sticky="w", padx=10, pady=8
        )

        # --- 图标文件 ---------------------------------------------------
        row += 1
        ctk.CTkLabel(outer, text="图标文件 (可选):", font=("", 12)).grid(
            row=row, column=0, sticky="e", pady=8, padx=10
        )
        self.icon_ent = ctk.CTkEntry(outer, placeholder_text="icon.ico / .png")
        self.icon_ent.grid(row=row, column=1, sticky="ew", pady=8)

        btn_frame = ctk.CTkFrame(outer, fg_color="transparent")
        btn_frame.grid(row=row, column=2, sticky="w")
        ctk.CTkButton(btn_frame, text="选择", width=50, command=self._browse_icon).grid(
            row=0, column=0, padx=(0, 4)
        )
        ctk.CTkButton(btn_frame, text="用生成", width=64, command=self._use_generated_icon).grid(
            row=0, column=1
        )

        # --- 输出目录（dist） ------------------------------------------
        row += 1
        ctk.CTkLabel(outer, text="输出目录(dist) (可选):", font=("", 12)).grid(
            row=row, column=0, sticky="e", pady=8, padx=10
        )
        self.dist_ent = ctk.CTkEntry(outer, placeholder_text="dist")
        self.dist_ent.grid(row=row, column=1, columnspan=2, sticky="ew", pady=8)

        # --- 应用名称 ---------------------------------------------------
        row += 1
        ctk.CTkLabel(outer, text="应用名称:", font=("", 14)).grid(
            row=row, column=0, sticky="e", pady=8, padx=10
        )
        self.name_ent = ctk.CTkEntry(outer, placeholder_text="MyApp")
        self.name_ent.grid(row=row, column=1, columnspan=2, sticky="ew", pady=8)

        # --- 开关（排列为两行三列） ------------------------------------
        row += 1
        swf = ctk.CTkFrame(outer, fg_color="transparent")
        swf.grid(row=row, column=0, columnspan=3, sticky="w", pady=10)
        self.sw_one = ctk.CTkSwitch(swf, text="--onefile")
        self.sw_one.select()
        self.sw_win = ctk.CTkSwitch(swf, text="--noconsole")
        self.sw_win.select()
        self.sw_clean = ctk.CTkSwitch(swf, text="--clean")
        self.sw_clean.select()
        self.sw_debug = ctk.CTkSwitch(swf, text="--debug (可选)")
        self.sw_upx = ctk.CTkSwitch(swf, text="UPX (可选)")
        self.sw_keep = ctk.CTkSwitch(swf, text="仅保留可执行 (可选)")
        for idx, sw in enumerate(
                (self.sw_one, self.sw_win, self.sw_clean, self.sw_debug, self.sw_upx, self.sw_keep)
        ):
            sw.grid(row=idx // 3, column=idx % 3, padx=12, pady=4, sticky="w")

        # --- hidden-imports --------------------------------------------
        row += 1
        ctk.CTkLabel(outer, text="hidden-imports (可选):", font=("", 12)).grid(
            row=row, column=0, sticky="e", pady=8, padx=10
        )
        self.hidden_ent = ctk.CTkEntry(outer, placeholder_text="pkg1,pkg2")
        self.hidden_ent.grid(row=row, column=1, columnspan=2, sticky="ew", pady=8)

        # --- add-data ---------------------------------------------------
        row += 1
        ctk.CTkLabel(outer, text="add-data (可选):", font=("", 12)).grid(
            row=row, column=0, sticky="e", pady=8, padx=10
        )
        self.data_ent = ctk.CTkEntry(outer, placeholder_text="file.txt;data")
        self.data_ent.grid(row=row, column=1, columnspan=2, sticky="ew", pady=8)

        # --- 打包按钮 ---------------------------------------------------
        row += 4
        self.pack_btn = ctk.CTkButton(outer, text="📦  开始打包", height=46, command=self._start_pack)
        self.pack_btn.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(18, 6))

        # --- 自动依赖 + 虚拟环境打包 -----------------------------------
        row += 1
        self.auto_pack_btn = ctk.CTkButton(
            outer, text="🤖 自动依赖打包", height=42, fg_color="#2D7D46",
            command=self._start_auto_pack
        )
        self.auto_pack_btn.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 18))

        # --- 进度条 -----------------------------------------------------
        row += 1
        self.pack_bar = ctk.CTkProgressBar(outer, mode="indeterminate")
        self.pack_bar.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        self.pack_bar.stop()

    # ---------- 生成线程入口 ----------
    def _start_generate(self):
        """
        点击“生成”：
        - 读取 Prompt、模板、分辨率、压缩等级；
        - 禁用按钮、启动进度条；
        - 启动后台线程 `_gen_thread()` 生成单张图标并替换预览。
        """
        prompt = self.prompt_ent.get().strip()
        if not prompt:
            messagebox.showwarning("提示", "请输入 Prompt")
            return

        style = None if self.style_opt.get() == "(无模板)" else self.style_opt.get()
        size = self.size_opt.get()
        comp = int(self.comp_slider.get())

        # 不再读取数量，固定每次只生成 1 张
        self.gen_btn.configure(state="disabled")
        self.ai_bar.start()
        self._status("生成中…")

        threading.Thread(
            target=self._gen_thread,
            args=(prompt, style, size, comp),
            daemon=True
        ).start()

    def _gen_thread(self, prompt, style, size, comp):
        try:
            paths = self.icon_gen.generate(
                prompt,
                style=style,
                size=size,
                compress_level=comp,
                convert_to_ico=True,
                n=1
            )
            # 只需预览第一张
            self.generated_icon = paths[0]
            img = Image.open(paths[0])
            cimg = ctk.CTkImage(img, size=(min(420, img.width), min(420, img.height)))
            self.after(0, lambda: self._show_preview(cimg))
        except Exception as e:
            self.after(0, lambda err=e: self._status(f"生成失败: {err}"))
        finally:
            self.after(0, lambda: self.gen_btn.configure(state="normal"))
            self.after(0, self.ai_bar.stop)

    def _import_image(self):
        """
        导入本地图片（PNG/JPG）作为“当前图标”并在预览区展示。
        允许随后执行“圆润处理”和“转为 ICNS”两步。
        """
        path = filedialog.askopenfilename(filetypes=[("Image files", "*.png *.jpg *.jpeg")])
        if not path:
            return
        try:
            img = Image.open(path).convert("RGBA")
        except Exception as e:
            messagebox.showerror("错误", f"无法打开图片: {e}")
            return

        self.generated_icon = Path(path)
        cimg = ctk.CTkImage(img, size=(min(420, img.width), min(420, img.height)))
        self.preview_img = cimg
        self.preview_lbl.configure(image=cimg, text="")
        self.smooth_btn.configure(state="normal")
        self._status("已导入外部图片，可执行圆润处理")
        self.icns_btn.configure(state="normal")

    def _show_preview(self, cimg):
        """
        将 CTkImage 放入预览区，并启用相关按钮。
        """
        self.preview_lbl.configure(image=cimg, text="")
        self.preview_img = cimg  # 持有引用
        self._status("生成完成，可前往『打包』页")
        self.smooth_btn.configure(state="正常")
        self.smooth_btn.configure(state="normal")
        self.icns_btn.configure(state="normal")

    # ---------- 自动依赖打包入口 ----------
    def _start_auto_pack(self):
        """
        点击“自动依赖打包”：
        - 校验入口脚本；
        - 禁用按钮、启动进度条；
        - 后台线程执行 `_auto_pack_thread()` 。
        """
        script = self.script_ent.get().strip()
        if not script or not Path(script).exists():
            messagebox.showerror("错误", "请选择有效的入口脚本")
            return

        self.auto_pack_btn.configure(state="disabled")
        self.pack_bar.start()
        self._status("准备自动打包…")
        threading.Thread(target=self._auto_pack_thread, args=(script,), daemon=True).start()

    def _detect_dependencies(self, script: str) -> list[str]:
        """
        （备用）依赖检测：基于 AST 扫描 import 并映射为 PyPI 包名，写入 requirements.txt。
        当前自动打包逻辑采用 pipreqs，更健壮；本函数保留作扩展与参考。
        """
        import ast
        import importlib.metadata as _imeta
        from pathlib import Path
        import sys

        # 常见别名到发行包的映射（如 PIL → Pillow）
        alias_map = {
            "PIL": "Pillow", "cv2": "opencv-python", "cv": "opencv-python",
            "skimage": "scikit-image", "sklearn": "scikit-learn",
            "bs4": "beautifulsoup4", "BeautifulSoup": "beautifulsoup4",
            "yaml": "PyYAML", "ruamel": "ruamel.yaml", "ruamel_yaml": "ruamel.yaml",
            "lxml": "lxml", "dateutil": "python-dateutil",
            "jinja2": "Jinja2", "telegram": "python-telegram-bot",
            "serial": "pyserial", "httplib2": "httplib2",
            "tensorflow": "tensorflow", "torch": "torch", "jax": "jax",
            "Crypto": "pycryptodome",
            "OpenGL": "PyOpenGL", "pygame": "pygame", "wx": "wxPython", "gi": "PyGObject",
            "six": "six", "tqdm": "tqdm", "regex": "regex",
        }

        stdlib = sys.stdlib_module_names
        pkgs: set[str] = set()

        # 1) AST 扫描 import / from-import，过滤标准库
        source = Path(script).read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source, filename=script)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root and root not in stdlib:
                        pkgs.add(root)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                root = node.module.split(".")[0]
                if root and root not in stdlib:
                    pkgs.add(root)

        # 2) 映射到发行包名并使用 metadata 补全
        mapped = {alias_map.get(m, m) for m in pkgs}
        top_to_dist = _imeta.packages_distributions()
        for mod in list(mapped):
            if mod in top_to_dist:
                mapped.update(top_to_dist[mod])

        requirements = sorted(mapped)

        # 3) 写出 requirements.txt
        Path("requirements.txt").write_text("\n".join(requirements), encoding="utf-8")
        return requirements

    # ---------- 打包（手动参数） ----------
    def _browse_script(self):
        """
        选择入口脚本 `.py` 并填入输入框。
        """
        p = filedialog.askopenfilename(filetypes=[("Python files", "*.py")])
        if p:
            self.script_ent.delete(0, "end")
            self.script_ent.insert(0, p)

    def _start_pack(self):
        """
        点击“开始打包”：
        - 检查入口脚本有效性；
        - 选择图标路径（优先输入框，其次使用最近生成）；
        - 禁用按钮、启动进度条；
        - 后台线程 `_pack_thread()` 开始打包流程。
        """
        script = self.script_ent.get().strip()
        if not script or not Path(script).exists():
            messagebox.showerror("错误", "请选择有效的入口脚本")
            return

        icon_path = self.icon_ent.get().strip() or self.generated_icon
        self.pack_btn.configure(state="disabled")
        self.pack_bar.start()
        self._status("开始打包…")
        threading.Thread(target=self._pack_thread, args=(script, icon_path), daemon=True).start()

    # ---------- 打包辅助：清理残留 ----------
    def pre_clean_artifacts(
        self,
        project_root: Path,
        app_name: str,
        dist_path: Optional[str] = None
    ) -> None:
        """
        预清理：删除 build/、dist/、<app_name>.spec、.aipack_venv/、requirements.txt.bak
        """
        # 删除 build/
        shutil.rmtree(project_root / "build", ignore_errors=True)
        # 删除 dist/
        if dist_path:
            shutil.rmtree(Path(dist_path), ignore_errors=True)
        else:
            shutil.rmtree(project_root / "dist", ignore_errors=True)
        # 删除 .spec 文件
        (project_root / f"{app_name}.spec").unlink(missing_ok=True)
        # 删除虚拟环境目录
        shutil.rmtree(project_root / ".aipack_venv", ignore_errors=True)
        # 删除依赖备份
        (project_root / "requirements.txt.bak").unlink(missing_ok=True)

    def clean_artifacts(
            self,
            project_root: Path,
            app_name: str
    ) -> None:
        """
        保留 dist：只删除 build/、<app_name>.spec 和 .aipack_venv，保留 dist/ 目录
        """
        # 删除 build/
        shutil.rmtree(project_root / "build", ignore_errors=True)
        # 删除 .spec 文件
        (project_root / f"{app_name}.spec").unlink(missing_ok=True)
        # 删除临时虚拟环境
        shutil.rmtree(project_root / ".aipack_venv", ignore_errors=True)

    # ---------- 普通打包线程 ----------
    def _pack_thread(self, script: str, icon_path: Optional[str]):
        """
        普通打包（使用当前 Python 环境的 PyInstaller）：
        1) 预清理旧产物；
        2) 构建 PyInstallerPacker 并执行；
        3) 可选“仅保留可执行”二次清理；
        4) 写日志 pack_log.txt 并更新状态。
        """
        project_root = Path(script).resolve().parent
        app_name = (self.name_ent.get().strip() or Path(script).stem)
        dist_dir_in = (self.dist_ent.get().strip() or None)
        dist_dir = dist_dir_in or str(project_root / "dist")

        # ① 预清理
        self.pre_clean_artifacts(project_root, app_name)

        # ② 调 PyInstaller
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
                name=app_name,
                icon=icon_path or None,
                dist_dir=dist_dir,
                workpath=str(project_root / "build"),
                spec_path=str(project_root),
                hidden_imports=[
                                   x.strip() for x in self.hidden_ent.get().split(",") if x.strip()
                               ] or None,
                add_data=[self.data_ent.get().strip()] if self.data_ent.get().strip() else None
            )

            ok = (result.returncode == 0)

            # ③ 仅保留可执行（删除 build/ 和 .spec，保留 dist/）
            if ok and self.sw_keep.get():
                self.clean_artifacts(project_root)

            # ④ 写日志并更新状态
            (project_root / "pack_log.txt").write_text(
                result.stdout + "\n" + result.stderr, encoding="utf-8"
            )
            self.after(0, lambda: self._status("打包成功！" if ok else "打包失败！查看 pack_log.txt"))

        except Exception as e:
            self.after(0, lambda err=e: self._status(f"打包异常: {err}"))
        finally:
            self.after(0, lambda: self.pack_btn.configure(state="normal"))
            self.after(0, self.pack_bar.stop)

    # ---------- 自动依赖 + 打包线程 ----------
    def _auto_pack_thread(self, script: str):
        """
        自动依赖打包流程（在隔离 venv 内完成）：
        - 如果项目根目录已有 requirements.txt，则默认使用它；
        - 否则走 pipreqs 扫描流程生成 requirements.txt。
        """
        import platform
        from PIL import Image
        import subprocess
        import shutil
        import sys

        project_root = Path(script).resolve().parent
        app_name = self.name_ent.get().strip() or Path(script).stem

        # 各路径
        dist_dir = project_root / "dist"
        build_dir = project_root / "build"
        spec_dir = project_root
        venv_dir = project_root / ".aipack_venv"
        python_exe = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

        req_path = project_root / "requirements.txt"
        req_backup = project_root / "requirements.txt.bak"

        # 保证 finally 中可用
        using_existing = False
        try:
            # 0) 预清理旧产物
            self.pre_clean_artifacts(project_root, app_name)

            # 1) 如果已有 requirements.txt 就跳过，否则生成
            using_existing = req_path.exists()
            if using_existing:
                self.after(0, lambda: self._status("发现现有 requirements.txt，跳过依赖扫描"))
            else:
                # 备份旧文件（若存在且未备份）
                if req_path.exists() and not req_backup.exists():
                    shutil.copy(req_path, req_backup)

                self.after(0, lambda: self._status("pipreqs 正在分析依赖…"))
                subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pipreqs>=0.4.13"])
                subprocess.check_call([
                    sys.executable, "-m", "pipreqs.pipreqs", str(project_root),
                    "--force", "--savepath", str(req_path), "--use-local"
                ])
                # 追加关键依赖
                with req_path.open("a", encoding="utf-8") as f:
                    f.write("\nPyQt6>=6.6\nPyQt6-Qt6>=6.6\nPyQt6-sip>=13.6\n")
                    f.write("pillow>=10.0\npyinstaller>=6.0\n")

            # 2) 创建隔离 venv 并安装依赖
            if venv_dir.exists():
                shutil.rmtree(venv_dir, ignore_errors=True)
            subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])
            self.after(0, lambda: self._status("安装依赖中，请稍候…"))
            subprocess.check_call([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"])
            subprocess.check_call([str(python_exe), "-m", "pip", "install", "--no-cache-dir", "-r", str(req_path)])

            # 3) macOS：若是 PNG 图标则转 ICNS
            icon_in = self.icon_ent.get().strip() or self.generated_icon
            if icon_in and platform.system() == "Darwin":
                ip = Path(icon_in)
                if ip.suffix.lower() != ".icns":
                    self.after(0, lambda: self._status("转换 icon 为 .icns…"))
                    Image.open(ip).save(ip.with_suffix(".icns"))
                    icon_in = str(ip.with_suffix(".icns"))

            # 4) PyInstaller 打包（使用 venv 内 python）
            packer = PyInstallerPacker(
                onefile=(False if platform.system() == "Darwin" else self.sw_one.get()),
                windowed=self.sw_win.get(),
                clean=self.sw_clean.get(),
                debug=self.sw_debug.get(),
                upx=self.sw_upx.get(),
                pyinstaller_exe=str(python_exe)
            )
            result = packer.pack(
                script_path=script,
                name=app_name,
                icon=icon_in or None,
                dist_dir=str(dist_dir),
                workpath=str(build_dir),
                spec_path=str(spec_dir),
                hidden_imports=["PyQt6"]
            )
            ok = (result.returncode == 0)

            # 5) 仅保留可执行：删除 build/ 和 .spec，保留 dist/
            if ok and self.sw_keep.get():
                self.clean_artifacts(project_root, app_name)

            # 6) 写日志并更新状态
            (project_root / "pack_log.txt").write_text(
                result.stdout + "\n" + result.stderr, encoding="utf-8"
            )
            self.after(0, lambda: self._status(
                "自动打包成功！" if ok else "自动打包失败！查看 pack_log.txt"
            ))

        except subprocess.CalledProcessError as e:
            self.after(0, lambda err=e: self._status(f"自动打包异常: {err}"))
        finally:
            # 恢复备份的 requirements.txt（仅在生成流程中备份过）
            if not using_existing and req_backup.exists():
                shutil.move(req_backup, req_path)
            self.after(0, self.pack_bar.stop)
            self.after(0, lambda: self.auto_pack_btn.configure(state="normal"))

    # ---------- 设置 & 状态 ----------
    def apply_settings(self, cfg: dict):
        """
        “设置”窗口保存后回调：
        - 更新 `self.cfg` 并重建服务；
        - 刷新“模板”下拉可选项；
        - 状态栏提示。
        """
        self.cfg = cfg
        self._init_services()
        self.style_opt.configure(values=["(无模板)"] + self.icon_gen.list_templates())
        self.style_opt.set("(无模板)")
        self._status("已加载新配置")

    def _status(self, text):
        """
        状态栏统一入口。
        """
        self.status.configure(text=f"状态: {text}")


# --------------------------------------------------------------------------- #
# 入口（保留原行为）
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    try:
        AIconPackGUI().mainloop()
    except Exception as e:
        # 捕获未处理异常并以消息框显示，避免应用直接崩溃。
        messagebox.showerror("错误", str(e))
