# AIconPack · AI 图标生成 & PyInstaller 打包

单文件三大模块（`aiconpack.py`）：

1. **IconGenerator** —— 调用 OpenAI 图像接口生成应用图标（支持模板、批量、压缩、ICO/ICNS）  
2. **PyInstallerPacker** —— 高级 PyInstaller 打包封装（onefile/noconsole/UPX/隐藏依赖等）  
3. **AIconPackGUI** —— 现代化 `customtkinter` GUI，串联「生成图标 → 圆角优化/ICNS → 一键打包」

> 支持 macOS / Windows / Linux（图标格式与打包参数会按平台做差异化处理）。  
> macOS 自动将 PNG 转为 **.icns**；自动打包流程在 macOS 上默认启用 **onedir**，以减少 Gatekeeper 报错。

---

## ✨ 主要特性

- **AI 生成图标**
  - 支持 `OpenAI` 官方或自定义 **Base URL**（代理/中转）
  - **Prompt 模板系统**：JSON 管理模板，`{prompt}` 占位符
  - **批量生成**（GUI“数量”选择）
  - **PNG 压缩**（0–9）与 **ICO/ICNS** 输出
  - 一键**圆角处理**（25% 半径，适合 app 图标）

- **PyInstaller 打包（高级封装）**
  - onefile / noconsole / clean / debug / UPX
  - `--hidden-import`、`--add-data`、`--distpath`、`--workpath`、`--specpath`
  - 生成 **pack_log.txt** 记录 stdout/stderr
  - 选项「仅保留可执行」：打包成功后清理 build/spec

- **自动依赖打包（🤖）**
  - 基于 `pipreqs` 生成 requirements.txt
  - 临时创建隔离 **venv** 安装依赖
  - macOS 自动将 PNG 转 **ICNS**
  - 打包后可选清理 build/spec，并恢复原 `requirements.txt`

- **配置持久化**
  - 用户配置保存在：`~/.aiconpack_config.json`
  - 同步导出到程序目录：`config.json`（便于分享或 CI）

---

## 📦 环境要求

- **Python 3.10+**（用到 `sys.stdlib_module_names` 等）
- 依赖（可用 `pip` 安装）：
  ```bash
  pip install customtkinter openai requests pillow pyinstaller pipreqs
  ```
  - **可选**：UPX（若启用 UPX 压缩，需要系统里可执行的 `upx`）

> 注意：使用 OpenAI 图像 API 将产生接口调用费用；请确认你的账号与配额。

---

## 🚀 运行

```bash
python aiconpack.py
```

首次运行可点击右上角 **⚙️ 设置** 填写：

- `OpenAI API Key`：你的 OpenAI 密钥（也可通过环境变量 `OPENAI_API_KEY` 提供）
- `API Base URL`：如使用代理 / 中转，填入对应地址（也可通过 `OPENAI_BASE_URL` 提供）
- `Prompt 模板（JSON）`：形如
  ```json
  {
    "极简": "Create a minimal, flat icon: {prompt}.",
    "拟物": "Photorealistic style, high detail: {prompt}"
  }
  ```

配置会写入 `~/.aiconpack_config.json` 并同步导出 `config.json`。

---

## 🖌️ 使用指南（GUI）

### 1) 生成图标（AI 生成页）
1. 在 **Prompt** 输入想要的图标描述（如：“极简扁平风蓝色日历图标”）。
2. 选择 **模板**（可选）、**分辨率**、**PNG 压缩**、**数量**。
3. 点击 **🎨 生成**。
4. 生成后可：
   - 点击 **✨ 圆润处理**：自动做 25% 圆角。
   - 点击 **💾 转为 ICNS**（macOS 友好格式）。
   - 或 **📂 导入图片** 进行预览与后续处理。

> 默认图像模型是 `dall-e-3`。DALL·E 3 仅支持分辨率：`1024x1024`、`1024x1792`、`1792x1024`，且每次调用 `n=1`，程序会循环请求来实现批量。

### 2) 打包应用（PyInstaller 打包页）
1. 选择 **入口脚本**（如 `app.py`）。
2. （可选）选择 **图标文件**（`.ico` / `.png` / `.icns`）。也可点击 **用生成** 将上一步生成的图标带入。
3. （可选）设置 **输出目录（dist）**；默认位于脚本旁。
4. 配置 **应用名称** 与其它开关（`--onefile`、`--noconsole`、`--clean`、`--debug`、`UPX`）。
5. （可选）填写 `hidden-imports`（逗号分隔）与 `add-data`（形如 `file.txt;data`）。
6. 点击 **📦 开始打包**，完成后查看状态栏与 `pack_log.txt`。

### 3) 自动依赖打包（🤖）
- 点击 **🤖 自动依赖打包**，程序将：
  1. 用 `pipreqs` 生成 `requirements.txt`（并追加必须依赖，如 PyQt6/Pillow/PyInstaller）
  2. 创建临时 **venv** 并安装依赖
  3. **macOS**：自动将 PNG 图标转换为 `.icns`
  4. 用该 venv 的 `pyinstaller` 打包  
  5. 生成完成后，按需清理 build/spec，并恢复原 `requirements.txt`

---

## 🧩 进阶：以库方式使用

### IconGenerator（程序化）
```python
from aiconpack import IconGenerator

gen = IconGenerator(api_key="sk-...", base_url="https://api.example.com/v1",
                    prompt_templates={"极简": "Minimal style: {prompt}"})
paths = gen.generate(
    prompt="蓝色日历图标",
    style="极简",
    size="1024x1024",
    n=2,
    convert_to_ico=True,
    compress_level=6,        # PNG 压缩 0-9
    return_format="path"     # 也可 "pil" / "bytes" / "b64"
)
print(paths)  # [Path('icons/icon_...png'), ...]
```

### PyInstallerPacker（程序化）
```python
from aiconpack import PyInstallerPacker

packer = PyInstallerPacker(onefile=True, windowed=True, upx=False)
cmd_or_result = packer.pack(
    script_path="app.py",
    name="MyApp",
    icon="icon.icns",             # Windows 用 .ico；macOS 用 .icns
    dist_dir="dist",
    hidden_imports=["PyQt6"],
    add_data=["config.json;."]
)
# 若 dry_run=True 则返回命令行数组；否则返回 CompletedProcess
```

---

## ⚙️ 选项与字段说明

- **Prompt 模板**（设置页，JSON）
  - 键：模板名称（下拉可选）
  - 值：模板内容，需包含 `{prompt}` 占位符
- **PNG 压缩**：0（不压缩/体积较大）~ 9（高压缩/体积小）
- **hidden-imports**：逗号分隔，如 `pkg1,pkg2`
- **add-data**：`"源文件;目标目录"`，跨平台用分号分隔

---

## 🛡️ 安全与合规

- **API Key**：保存在本机 `~/.aiconpack_config.json` 与项目目录 `config.json`。请勿提交到公共仓库。
- **网络**：若使用中转/代理，确认服务可信且遵循相关条款。
- **费用**：OpenAI API 计费按官方规则执行，请注意配额与速率限制。

---

## 🔧 常见问题（FAQ）

**Q1：生成失败 / 超时 / 429？**  
A：多半是网络或速率限制。稍后重试、减小批量、或在设置中配置更稳定的 Base URL。程序带有指数退避重试（默认最多 3 次）。

**Q2：DALL·E 3 大于 1 张如何批量？**  
A：DALL·E 3 每次只能 `n=1`，程序会循环请求实现批量；其它模型则支持 `n≤10`。

**Q3：图标在 Windows/macOS 应该用什么格式？**  
A：Windows 用 **.ico**，macOS 用 **.icns**（GUI 已提供 PNG→ICNS 转换按钮）。

**Q4：打包后运行失败找不到资源？**  
A：把资源放到 `add-data`（形如 `file.txt;data`）并在代码中使用相对路径（或 `sys._MEIPASS` 处理 PyInstaller 的临时目录）。

**Q5：自动依赖打包安装很慢？**  
A：初次会创建 venv 并完整安装依赖，时间较长；后续可以复用或改为普通打包。

**Q6：UPX 不生效？**  
A：需要系统可执行的 `upx`（或配置 `--upx-dir` 指向其目录）。未安装时请关闭此开关。

---

## 🗂️ 生成的文件/目录

- `icons/`：默认图标输出目录
- `dist/`：打包产物目录（可在 GUI 指定）
- `build/`：PyInstaller 中间文件
- `*.spec`：PyInstaller 规格文件
- `pack_log.txt`：打包日志
- `~/.aiconpack_config.json` & `config.json`：应用配置

---

## 🛠️ 开发/调试建议

- 建议创建虚拟环境进行开发：
  ```bash
  python -m venv .venv
  . .venv/bin/activate   # Windows: .venv\Scripts\activate
  pip install -r requirements-dev.txt  # 自行维护
  ```
- 若要替换图像模型或参数，可在 `IconGenerator.generate()` 中调整 `model/size/response_format` 等。
- 日志更详细：可在 `_gen_thread()` / `_pack_thread()` 中增加异常打印或保存到文件。

---

## 📜 许可证

当前仓库未显式声明许可证。请根据你的发布策略选择（例如 MIT/Apache-2.0/Proprietary），并在仓库根目录添加 `LICENSE`。

---

## 🗺️ 路线图（建议）

- [ ] 任务队列与取消
- [ ] 可视化日志窗口（替代纯文件）
- [ ] 自定义圆角半径与阴影/描边
- [ ] 更多图像模型与采样参数支持
- [ ] CLI 模式（无 GUI 一键批量）
