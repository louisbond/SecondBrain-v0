"""画面文字识别（OCR）：ASR 的兜底，不是主路径。

## 定位

**只在 ASR 没转出东西时才跑**（`[ocr] auto_trigger = "asr_empty"`）。
典型场景：纯字幕视频、BGM 盖过人声、方言太重、无人声的操作演示。

抖音上大量视频是「口播 + 大字字幕」，那种情况下 OCR 拿到的是和 ASR
一模一样的话 —— 纯冗余，还多花十几秒。所以默认不跑。

B 站默认跳过：官方字幕 API 已经给了高质量文本，OCR 无增益。

## 为什么是这套组合

1. **PP-OCRv5/v6 mobile**（RapidOCR 内置，onnxruntime 推理）
   - 比 v4 mobile 强：中英混排 +13%，手写 +20%，支持竖排繁体
   - 单帧 0.3–0.8 秒，纯 CPU，不需要 GPU
2. **分区检测**：底部字幕区优先（快），每 5 帧补一次全图（捞 PPT/书页/图表）
3. **多帧投票**：字幕停留 1–2 秒，同一句话会被采到多次。逐位置字符
   多数表决，把「口播」被识别成「口插」这类随机错字压掉。
4. **LLM 纠错**（可选）：有 ollama 在跑时，把整段 OCR 结果丢给本地模型
   修错字、补标点。没在跑就跳过，不阻塞。

## 不保留采样帧

帧是中间产物：OCR 结果已经带时间码存下来了，帧本身没有长期价值。
删掉视频后如果想重新 OCR，会明确提示「需要重新提供视频」，
而不是默默留 3MB 帧占地方。
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_config

# 水印 / 平台 UI 的高频噪声词。判定条件同时要求「短」，
# 避免把「记得点赞关注不迷路」这种真口播误杀。
_NOISE_WORDS = (
    "点赞",
    "关注",
    "收藏",
    "转发",
    "分享",
    "评论",
    "小红书",
    "抖音",
    "快手",
    "bilibili",
    "哔哩哔哩",
    "剪映",
    "字幕由",
)
# 只有纯符号/数字的行没有语义价值
_MEANINGLESS = re.compile(r"^[\s\d\W_]+$")


@dataclass
class OcrLine:
    """一行画面文字（带时间码）。"""

    start: float
    end: float
    text: str
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "text": self.text,
        }


@dataclass
class OcrResult:
    ok: bool
    lines: list[OcrLine] = field(default_factory=list)
    text: str = ""
    error: str = ""
    frames: int = 0
    elapsed_ms: int = 0
    llm_fixed: bool = False


def _normalize(text: str) -> str:
    """比对用的归一化：去空白和标点，只留字。"""
    return re.sub(r"[\s\W_]+", "", text or "")


def _is_noise(text: str) -> bool:
    """水印、平台 UI、账号名、纯符号。"""
    raw = (text or "").strip()
    if len(raw) < 2:
        return True
    if _MEANINGLESS.match(raw):
        return True
    # 以 @ 开头的是账号名水印（`@某某某`），画面文字里没有语义价值
    if raw.startswith("@") and len(raw) <= 16:
        return True
    core = _normalize(raw)
    if not core:
        return True
    return any(word in raw and len(raw) <= len(word) + 3 for word in _NOISE_WORDS)


def vote_texts(texts: list[str]) -> str:
    """多帧投票：同一句话被识别出多个变体时，逐位置取多数。

    为什么要字符级而不是整行投票：OCR 的错字位置是随机的，
    5 次识别出来 5 个略微不同的串，整行投票永远没有多数。
    但逐位置看，「口播」的「播」可能在 5 次里对了 3 次。

    长度不一致时用 SequenceMatcher 对齐到最长的那条。
    """
    candidates = [t.strip() for t in texts if (t or "").strip()]
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]

    # 出现次数最多的长度做基准，再挑该长度里最长的一条
    counts = Counter(len(c) for c in candidates)
    base_len = counts.most_common(1)[0][0]
    same_len = [c for c in candidates if len(c) == base_len]
    base = max(same_len, key=lambda c: c)

    # 逐位置收集字符
    columns: list[list[str]] = [[] for _ in range(len(base))]
    for text in candidates:
        if text == base:
            for i, ch in enumerate(base):
                columns[i].append(ch)
            continue
        matcher = difflib.SequenceMatcher(None, base, text, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for offset in range(i2 - i1):
                    columns[i1 + offset].append(text[j1 + offset])
            elif tag == "replace":
                # 替换段长度可能不同，按位置就近投票
                span = min(i2 - i1, j2 - j1)
                for offset in range(span):
                    columns[i1 + offset].append(text[j1 + offset])

    voted: list[str] = []
    for i, chars in enumerate(columns):
        if not chars:
            voted.append(base[i])
            continue
        # 平票时优先保留基准字符，避免抖动
        counter = Counter(chars)
        best = counter.most_common(1)[0][0]
        if counter[best] == 1 and base[i] in counter:
            best = base[i]
        voted.append(best)
    return "".join(voted)


def group_lines(
    samples: list[tuple[float, list[tuple[str, float, float]]]]
) -> list[OcrLine]:
    """把「每帧的多条文本」聚成「带时间跨度的行」。

    samples: [(时间秒, [(文本, 置信度, 纵向位置 0~1)])]

    聚类依据是**纵向位置分桶**（画面横向 12 等分的带）+ 文本相似度。
    不能只按文本相似度聚：同一句话在画面上出现两次（标题 + 字幕）
    会被误并成一条。
    """
    if not samples:
        return []

    # 桶 → 槽列表；槽 = {"variants": [...], "score": float, "times": [...]}
    buckets: dict[int, list[dict[str, Any]]] = {}

    for stamp, items in samples:
        for text, score, y_norm in items:
            if _is_noise(text):
                continue
            key = int(max(0.0, min(1.0, y_norm)) * 12)
            slots = buckets.setdefault(key, [])
            core = _normalize(text)
            target = None
            for slot in slots:
                ratio = difflib.SequenceMatcher(
                    None, slot["core"], core, autojunk=False
                ).ratio()
                if ratio >= 0.55:
                    target = slot
                    break
            if target is None:
                buckets[key].append(
                    {
                        "core": core,
                        "variants": [text],
                        "score": score,
                        "times": [stamp],
                    }
                )
            else:
                target["variants"].append(text)
                target["score"] = max(target["score"], score)
                target["times"].append(stamp)

    interval = _frame_interval(samples)
    lines: list[OcrLine] = []
    for slots in buckets.values():
        for slot in slots:
            text = vote_texts(slot["variants"])
            if not text or _is_noise(text):
                continue
            start = min(slot["times"])
            end = max(slot["times"]) + interval
            lines.append(
                OcrLine(start=start, end=end, text=text, score=round(slot["score"], 3))
            )
    lines.sort(key=lambda item: (item.start, item.text))
    return lines


def _frame_interval(samples: list[tuple[float, Any]]) -> float:
    if len(samples) < 2:
        return 2.0
    gaps = [
        samples[i + 1][0] - samples[i][0] for i in range(len(samples) - 1) if samples[i + 1][0] > samples[i][0]
    ]
    if not gaps:
        return 2.0
    gaps.sort()
    return gaps[len(gaps) // 2]


class OcrService:
    """视频画面文字 → 带时间码的行。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self._engine: Any = None
        self._engine_error = ""

    # -- 引擎 -------------------------------------------------------------
    def _get_engine(self) -> Any:
        """懒加载 RapidOCR（模型首次调用才载入，避免拖慢启动）。"""
        if self._engine is not None or self._engine_error:
            return self._engine
        try:
            from rapidocr import RapidOCR

            self._engine = RapidOCR()
        except Exception as exc:  # noqa: BLE001
            self._engine_error = f"{type(exc).__name__}: {exc}"[:200]
            self._engine = None
        return self._engine

    def available(self) -> bool:
        try:
            import rapidocr  # type: ignore  # noqa: F401
        except Exception:
            return False
        return True

    def info(self) -> dict[str, Any]:
        engine = self._get_engine()
        return {
            "available": engine is not None,
            "error": self._engine_error,
            "auto_trigger": self.cfg.ocr.auto_trigger,
            "model_tier": self.cfg.ocr.model_tier,
            "sample_seconds": self.cfg.ocr.sample_seconds,
            "llm_fix": self.cfg.ocr.llm_fix,
        }

    # -- 抽帧 -------------------------------------------------------------
    def _sample_frames(self, video_path: str) -> tuple[list[tuple[float, Any]], int]:
        """按间隔采样帧，返回 [(时间秒, RGB ndarray)] 与视频总帧数。

        顺序解码（不 seek）：短视频占绝大多数，seek 的精度问题
        （关键帧对齐）比多解几十帧更麻烦。长视频自动放宽间隔。
        """
        import av

        width = int(self.cfg.ocr.sample_width or 1280)
        interval = float(self.cfg.ocr.sample_seconds or 2.0)

        samples: list[tuple[float, Any]] = []
        total_frames = 0
        next_at = 0.0
        try:
            with av.open(video_path) as container:
                stream = next(
                    (s for s in container.streams if s.type == "video"), None
                )
                if stream is None:
                    return [], 0
                # 长视频放宽采样间隔，别让 10 分钟的视频 OCR 跑十分钟
                duration = 0.0
                if container.duration:
                    duration = container.duration / 1_000_000
                if duration > 600:
                    interval = max(interval, 4.0)
                elif duration > 300:
                    interval = max(interval, 3.0)

                for frame in container.decode(stream):
                    if not isinstance(frame, av.VideoFrame):
                        continue
                    total_frames += 1
                    if frame.pts is None or stream.time_base is None:
                        stamp = total_frames * 0.04
                    else:
                        stamp = float(frame.pts * stream.time_base)
                    if stamp + 1e-6 < next_at:
                        continue
                    next_at = stamp + interval
                    height = int(width * frame.height / max(1, frame.width))
                    if height % 2:
                        height += 1
                    small = frame.reformat(width=width, height=height, format="rgb24")
                    samples.append((stamp, small.to_ndarray()))
        except Exception:  # noqa: BLE001
            return samples, total_frames
        return samples, total_frames

    # -- 识别 -------------------------------------------------------------
    def _recognize(self, image: Any) -> list[tuple[str, float, float]]:
        """单张图 → [(文本, 置信度, 纵向位置 0~1)]。

        兼容 RapidOCR 2.x（tuple 返回）与 3.x（RapidOCROutput 对象）。
        """
        engine = self._get_engine()
        if engine is None:
            return []
        try:
            out = engine(image)
        except Exception:  # noqa: BLE001
            return []

        texts = getattr(out, "txts", None)
        scores = getattr(out, "scores", None)
        boxes = getattr(out, "boxes", None)
        if texts is None and isinstance(out, tuple):
            # 2.x: (boxes, txts, scores)
            boxes, texts, scores = out[0], out[1], out[2]

        if not texts:
            return []
        height = float(getattr(image, "shape", (0, 0))[0] or 1)
        result: list[tuple[str, float, float]] = []
        for index, text in enumerate(texts):
            score = float(scores[index]) if scores is not None and index < len(scores) else 0.0
            y_norm = 0.5
            if boxes is not None and index < len(boxes):
                box = boxes[index]
                try:
                    ys = [float(point[1]) for point in box]
                    y_norm = (sum(ys) / len(ys)) / height
                except Exception:  # noqa: BLE001
                    y_norm = 0.5
            result.append((str(text), score, y_norm))
        return result

    def _recognize_frame(
        self, image: Any, full: bool
    ) -> list[tuple[str, float, float]]:
        """一帧 → 文本行。

        分区检测：默认只跑底部 40%（字幕区，快得多）；
        每 5 帧跑一次全图（捞 PPT、书页、图表里的字）。
        """
        height = int(getattr(image, "shape", (0, 0))[0] or 0)
        if height <= 0:
            return []
        collected: list[tuple[str, float, float]] = []

        # 底部字幕区（全图高度的 58%~100%）
        top = int(height * 0.58)
        bottom = image[top:, :, :]
        for text, score, y_norm in self._recognize(bottom):
            collected.append((text, score, 0.58 + y_norm * 0.42))

        if full:
            for text, score, y_norm in self._recognize(image):
                # 和底部结果重复的丢掉（同一句话别记两遍）
                core = _normalize(text)
                if any(
                    difflib.SequenceMatcher(
                        None, core, _normalize(existing), autojunk=False
                    ).ratio()
                    > 0.7
                    for existing, _, _ in collected
                ):
                    continue
                collected.append((text, score, y_norm))
        return collected

    # -- 主流程 -----------------------------------------------------------
    def transcribe_video(
        self,
        video_path: str,
        duration_ms: int = 0,
        on_progress: Any = None,
        asr_text: str = "",
    ) -> OcrResult:
        """视频 → 画面文字（带时间码）。

        `asr_text` 用于去重：OCR 出来的行如果和 ASR 结果高度重复，
        说明这句话已经被「听到」了，没必要再记一遍。
        """
        import time as _time

        started = _time.time()
        if not self.available():
            return OcrResult(
                ok=False,
                error="未安装 rapidocr（pip install rapidocr）；这是可选组件，不影响转写",
            )

        # 引擎初始化失败必须说出来。以前这里不检查，`_get_engine()` 返回 None
        # 一路走到「画面里没识别出文字」，把「模型/配置缺失」伪装成
        # 「这个视频没有字幕」，连开发时都看不出来（打包漏了 config.yaml
        # 就这么静默失效过一版）。
        if self._get_engine() is None:
            return OcrResult(
                ok=False,
                error=f"OCR 引擎无法初始化：{self._engine_error or '未知原因'}",
                elapsed_ms=int((_time.time() - started) * 1000),
            )

        samples, total_frames = self._sample_frames(video_path)
        if not samples:
            return OcrResult(
                ok=False,
                error="视频里没有可读取的画面（可能只有音轨）",
                elapsed_ms=int((_time.time() - started) * 1000),
            )

        collected: list[tuple[float, list[tuple[str, float, float]]]] = []
        for index, (stamp, image) in enumerate(samples):
            full = index % 5 == 0
            items = self._recognize_frame(image, full=full)
            if items:
                collected.append((stamp, items))
            if on_progress is not None:
                try:
                    on_progress(index + 1, len(samples))
                except Exception:  # noqa: BLE001
                    pass

        if not collected:
            return OcrResult(
                ok=False,
                error="画面里没识别出文字（纯画面 / 只有人像）",
                frames=len(samples),
                elapsed_ms=int((_time.time() - started) * 1000),
            )

        lines = group_lines(collected)
        lines = self._drop_duplicated_with_asr(lines, asr_text)
        if not lines:
            return OcrResult(
                ok=False,
                error="画面文字与口播内容重复，无需补充",
                frames=len(samples),
                elapsed_ms=int((_time.time() - started) * 1000),
            )

        llm_fixed = False
        if self.cfg.ocr.llm_fix:
            fixed = self._llm_fix([line.text for line in lines])
            if fixed and len(fixed) == len(lines):
                for line, text in zip(lines, fixed, strict=False):
                    if text.strip():
                        line.text = text.strip()
                llm_fixed = True

        body = "\n".join(line.text for line in lines)
        return OcrResult(
            ok=True,
            lines=lines,
            text=body,
            frames=len(samples),
            elapsed_ms=int((_time.time() - started) * 1000),
            llm_fixed=llm_fixed,
        )

    @staticmethod
    def _drop_duplicated_with_asr(lines: list[OcrLine], asr_text: str) -> list[OcrLine]:
        """丢掉已经被 ASR 听到过的行（画面字幕和口播同源时会出现）。"""
        core_asr = _normalize(asr_text)
        if not core_asr:
            return lines
        kept: list[OcrLine] = []
        for line in lines:
            core = _normalize(line.text)
            if not core:
                continue
            # 整句出现在 ASR 里 → 这句话已经听到过
            if core in core_asr:
                continue
            kept.append(line)
        return kept

    # -- LLM 纠错 ---------------------------------------------------------
    def _llm_fix(self, lines: list[str]) -> list[str]:
        """用本地 ollama 修 OCR 错字 / 补标点。失败就原样返回（不阻塞）。"""
        if not lines:
            return []
        cfg = self.cfg
        base = str(getattr(cfg.ai.ollama, "base_url", "") or "").rstrip("/")
        model = str(getattr(cfg.ai.ollama, "model", "") or "")
        if not base or not model:
            return []

        try:
            import httpx

            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是 OCR 文本校对员。用户给的是视频画面里识别出来的文字，"
                            "可能有错别字、缺少标点。请逐行修正，规则："
                            "① 只修错别字和标点，不改写、不合并、不删减；"
                            "② 保持行数和顺序完全一致；"
                            "③ 只输出修正后的文本，每行一条，不要编号、不要解释。"
                        ),
                    },
                    {"role": "user", "content": "\n".join(lines)},
                ],
                "temperature": 0,
                "stream": False,
            }
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(f"{base}/chat/completions", json=payload)
                if resp.status_code != 200:
                    return []
                data = resp.json()
            content = str(
                (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            )
            out = [line.strip() for line in content.splitlines() if line.strip()]
            return out if len(out) == len(lines) else []
        except Exception:  # noqa: BLE001
            return []


_ocr: OcrService | None = None


def get_ocr_service() -> OcrService:
    global _ocr
    if _ocr is None:
        _ocr = OcrService()
    return _ocr
