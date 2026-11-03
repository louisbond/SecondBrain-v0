# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（P10：单目录 + 内嵌 yt-dlp/ffmpeg bin，离线部署）。

用法：
    cd backend
    pyinstaller secondbrain.spec --noconfirm

产物：
    dist/SecondBrain/SecondBrain.exe   （内含前端 dist 与 bin/ 下的 yt-dlp、ffmpeg）

注意：
- 单目录（--onedir）而非单文件：启动更快，且内嵌二进制更容易被调用；
- 数据目录（vault、db）在 exe 之外，用 SECOND_BRAIN_HOME 或 config.toml 指定。
"""

import os
import sys
from pathlib import Path

# SPECPATH 是 PyInstaller 运行时的"当前工作目录"，不一定是 spec 所在目录；
# 优先用 __file__ 定位，保证从任何目录执行都能找到项目根。
_here = Path(__file__).resolve() if "__file__" in globals() else Path(SPECPATH).resolve()
BACKEND_DIR = _here            # secondbrain/backend（本 spec 所在目录）
PROJECT_ROOT = _here.parent    # secondbrain

# 内嵌二进制目录（yt-dlp / ffmpeg）
BIN_DIR = PROJECT_ROOT / "bin"

# 前端构建产物
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"

datas = []
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # 延迟导入的运行时依赖（静态分析看不到）
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "av",
    "jieba",
    "trafilatura",
    "httpx",
    "multipart",
    # 内嵌窗口（桌面模式）：Windows 走 EdgeChromium 后端（WebView2）
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "webview.platforms.mshtml",
    "clr",
    "clr_loader",
    "pythonnet",
]

# 前端静态资源
if FRONTEND_DIST.exists():
    datas.append((str(FRONTEND_DIST), "frontend/dist"))
else:
    print(f"[spec] 警告：未找到前端构建产物 {FRONTEND_DIST}，请先执行 npm run build")

# 内嵌 yt-dlp / ffmpeg
if BIN_DIR.exists():
    for item in BIN_DIR.iterdir():
        if item.is_file():
            datas.append((str(item), "bin"))

# prompts 模板
prompts_dir = PROJECT_ROOT / "prompts"
if prompts_dir.exists():
    datas.append((str(prompts_dir), "prompts"))

# 配置模板（首次运行时复制到 exe 同级，供用户修改）
example_toml = PROJECT_ROOT / "config" / "config.example.toml"
if example_toml.exists():
    datas.append((str(example_toml), "config"))

# jieba 词典：由 pyinstaller-hooks-contrib 的 jieba hook 自动收集。
# 不要在这里手动加 —— 会和 hook 的目标路径冲突（目录/文件同名导致 COLLECT 失败）。

a = Analysis(
    [str(BACKEND_DIR / "run.py")],
    pathex=[str(BACKEND_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy.f2py",
        "pytest",
        # 本机未安装，但显式排除可显著减小体积
        "torch",
        "torchvision",
        "torchaudio",
        "transformers",
        "sentence_transformers",
        "datasets",
        "IPython",
        "jupyter",
        "notebook",
        "tensorboard",
        "gradio",
        "modelscope",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SecondBrain",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 无小黑窗：日志写入 data/logs/app.log，崩溃弹窗告知路径
    disable_windowed_tracebacks=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SecondBrain",
)
