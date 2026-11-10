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
    # OCR（v0.8）：延迟导入，静态分析看不到
    "rapidocr",
    "rapidocr.main",
    "rapidocr.utils",
    "cv2",
    "pyclipper",
    "shapely",
    # TLS：让校验走操作系统证书存储（交叉签名证书链，如火山方舟）。
    # tls.py 里是 try/except 可选导入，静态分析会跳过，必须显式列出。
    "truststore",
    "truststore._api",
    "truststore._windows",
    "truststore._openssl",
    "truststore._ssl_constants",
    # ASR 繁转简（v0.9.1）：asr_service.to_simplified() 里的函数内导入，
    # 打包时若漏收，会静默回退成"不转换"，用户只看到错字却不知道为什么。
    "zhconv",
    # ONNX embedding 的分词器（同上，函数内导入）
    "tokenizers",
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

# trafilatura 的运行时配置（v0.7.2 修）
#
# 坑：`trafilatura.settings.use_config()` 是 `configparser.read(<pkg>/settings.cfg)`，
# 而 PyInstaller 只把 .py 打进 PYZ，**数据文件不会自动收集**。
# 结果：打包后 config 是空的 → `extract()` 里读 MIN_EXTRACTED_SIZE 直接抛
# NoOptionError → 适配器吞掉异常 → 正文抽取**静默降级成 meta 摘要**。
# 源码运行时完全正常，只有打包版坏，属于最容易漏掉的那类问题。
try:
    import trafilatura as _trafilatura

    _traf_cfg = Path(_trafilatura.__file__).parent / "settings.cfg"
    if _traf_cfg.exists():
        datas.append((str(_traf_cfg), "trafilatura"))
        print(f"[spec] 已包含 trafilatura settings.cfg: {_traf_cfg}")
except Exception as _exc:  # noqa: BLE001
    print(f"[spec] 警告：未能定位 trafilatura settings.cfg（{_exc}）")

# justext 的停用词表（v0.8.2 修，和上面同一个坑）
#
# trafilatura 的正文抽取会调 justext，后者按语言加载 stoplists/<lang>.txt。
# 这些 .txt 不是 .py，PyInstaller 不会收集 → 打包后报
# `[WinError 3] 系统找不到指定的路径: ...\_internal\justext\stoplists`
# → 通用网页抽取（WebAdapter，也是抖音/快手的降级路径）直接失效。
try:
    import justext as _justext

    _stoplists = Path(_justext.__file__).parent / "stoplists"
    if _stoplists.is_dir():
        datas.append((str(_stoplists), "justext/stoplists"))
        print(f"[spec] 已包含 justext stoplists {len(list(_stoplists.glob('*.txt')))} 个")
    else:
        print(f"[spec] 警告：justext/stoplists 不存在（{_stoplists}）")
except Exception as _exc:  # noqa: BLE001
    print(f"[spec] 警告：未能定位 justext stoplists（{_exc}）")

# faster-whisper 的运行时资源（v31 修）
#
# 坑：silero VAD 模型（silero_vad_v6.onnx）在源码运行时由
# faster_whisper 自己定位，但**打包后路径变了** →
#   ONNXRuntimeError: NO_SUCHFILE: Load model from
#   _internal/faster_whisper/assets/silero_vad_v6.onnx failed
# → ASR 在 exe 里**完全不能用**（vad_filter=True 需要它）。
#
# 修法：把 assets 目录原样打进包（保持 faster_whisper/ 下的相对路径）。
try:
    import faster_whisper

    fw_assets = Path(faster_whisper.__file__).parent / "assets"
    if fw_assets.exists():
        datas.append((str(fw_assets), "faster_whisper/assets"))
        print(f"[spec] 已包含 faster-whisper assets: {fw_assets}")
except Exception as _exc:  # noqa: BLE001
    print(f"[spec] 警告：未能定位 faster_whisper assets（{_exc}）")

# jieba 词典：由 pyinstaller-hooks-contrib 的 jieba hook 自动收集。
# 不要在这里手动加 —— 会和 hook 的目标路径冲突（目录/文件同名导致 COLLECT 失败）。

# rapidocr 的 onnx 模型（v0.8）：det / cls / rec 三个，约 16MB。
# 坑 1：rapidocr 会去 `rapidocr/models/` 找模型，打包后那个目录不存在 →
#       它会**尝试联网下载** → 离线环境下 OCR 直接不可用，而且用户看到的是
#       一段「下载失败」的英文报错。
# 坑 2（更隐蔽）：`RapidOCR()` 初始化时要读 `rapidocr/config.yaml`
#       （main.py 里 `DEFAULT_CFG_PATH = Path(__file__).parent / "config.yaml"`）。
#       PyInstaller 只把 .py 放进 PYZ，非 .py 的包数据不会自动带上 ——
#       于是初始化直接抛 FileNotFoundError。而 ocr_service 把这个异常吞进
#       `_engine_error`，对外只说「画面里没识别出文字」，看着像视频没字幕。
#       实测就是这么漏掉一版的：开发环境好、装出来永远识别不出字。
try:
    import rapidocr as _rapidocr

    _ocr_pkg = Path(_rapidocr.__file__).parent
    for _name in ("config.yaml", "default_models.yaml"):
        _f = _ocr_pkg / _name
        if _f.exists():
            datas.append((str(_f), "rapidocr"))
    print(f"[spec] 已包含 rapidocr 配置: {_ocr_pkg}")

    _ocr_models = _ocr_pkg / "models"
    if _ocr_models.exists():
        datas.append((str(_ocr_models), "rapidocr/models"))
        _count = len(list(_ocr_models.glob("*.onnx")))
        print(f"[spec] 已包含 rapidocr 模型 {_count} 个: {_ocr_models}")
    else:
        print(f"[spec] 警告：rapidocr/models 不存在（{_ocr_models}），OCR 将不可离线使用")
except Exception as _exc:  # noqa: BLE001
    print(f"[spec] 警告：未能定位 rapidocr 模型（{_exc}）")

# 公开分发防御：真实配置、密钥、数据库、日志绝不能被打进包。
# datas 是显式清单，这里再兜一层，防止后来有人误加整个 config/ 目录。
_FORBIDDEN_NAMES = {"config.toml", "secrets.json", "secondbrain.db", "app.log"}
for _src, _dst in datas:
    if Path(_src).name.lower() in _FORBIDDEN_NAMES:
        raise RuntimeError(f"[spec] 打包清单混入用户数据：{_src}")

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

# ---- 许可与用户文档 ----
# PyInstaller 只收 .py 和 datas 里登记的文件；这三个是**产物根**（exe 同级）的
# 随包文档，不能塞进 _internal。MIT 要求在分发副本里保留版权与许可声明，
# 之前几版靠手工拷贝，漏一次就是合规缺口。
import shutil as _shutil
from pathlib import Path as _Path

_out_dir = _Path(DISTPATH) / coll.name
for _name in ("LICENSE.txt", "THIRD_PARTY_NOTICES.md", "使用说明.md"):
    _src = PROJECT_ROOT / _name
    if _src.exists():
        _out_dir.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(_src, _out_dir / _name)
        print(f"[spec] 已放入产物根: {_name}")
