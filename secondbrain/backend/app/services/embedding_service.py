"""Embedding 服务：bge-m3 CPU 编码、余弦召回。

对齐 v0.5.0 §6.4 / §9 / P9：
- B 层笔记入库时用（title + ai_summary + 手稿前 500 字）编码，存 `embeddings`；
- 召回：全库余弦 Top-5（排除自身与 A 层）→ `embedding_related`；
- LLM keywords 作为第二查询信号补充召回；
- 核准页用户勾选 → `confirmed_related`；
- embedding 不可用 → 相关召回降级为关键词匹配，不阻塞主流程。

实现：优先用 sentence-transformers / onnxruntime 的 bge-m3；
都不可用时退化为**确定性哈希向量**（可跑通流程但召回质量下降，明确降级告警）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any

from app.core.config import get_config
from app.core.db import get_db

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]+")


class EmbeddingService:
    """语义向量服务。"""

    def __init__(self) -> None:
        self.cfg = get_config()
        self.db = get_db()
        self._model: Any = None
        # ONNX 后端状态（不依赖 torch）
        self._onnx_session: Any = None
        self._onnx_tok: Any = None
        self._onnx_inputs: set[str] = set()
        self._onnx_dim = 0
        self._backend = "none"
        self._lock = threading.Lock()
        self._loaded = False

    # -- 后端加载 ---------------------------------------------------------
    def _load(self) -> str:
        """加载 embedding 后端，返回后端名。

        顺序：本地模型目录（离线）→ sentence-transformers → transformers → 哈希降级。

        **离线纪律**：这里是纯本地应用，绝不能因为"想加载模型"而联网。
        transformers 的 `from_pretrained("bge-m3")` 会去 huggingface.co
        查 revision，网络不通时**不是失败而是挂住**（实测 socket 建连无限等），
        整个采集流程与单测都卡死在这里。所以先用 `_model_ready()` 判断
        本地到底有没有模型，没有就直接走哈希降级。
        """
        with self._lock:
            if self._loaded:
                return self._backend
            self._loaded = True
            model_ref = self._model_ref()

            if not self._model_ready(model_ref):
                self._backend = "hash"
                return self._backend

            # ONNX 优先：bge-m3 需要 torch + 2GB 权重，打包版塞不进去；
            # bge-small-zh 的 int8 ONNX 只有 24MB，CPU 单条 10ms 级，够用。
            if self._try_load_onnx(model_ref):
                self._backend = "onnx"
                return self._backend

            # 有本地模型：强制离线，避免任何一次 hub 往返
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

            try:
                from sentence_transformers import SentenceTransformer  # type: ignore

                self._model = SentenceTransformer(model_ref, device="cpu")
                self._backend = "sentence-transformers"
                return self._backend
            except Exception:
                self._model = None
            try:
                from transformers import AutoModel, AutoTokenizer  # type: ignore

                self._tokenizer = AutoTokenizer.from_pretrained(
                    model_ref, local_files_only=True
                )
                self._torch_model = AutoModel.from_pretrained(
                    model_ref, local_files_only=True
                )
                self._torch_model.eval()
                self._backend = "transformers"
                return self._backend
            except Exception:
                self._backend = "hash"
                return self._backend

    def _try_load_onnx(self, ref: str) -> bool:
        """加载本地 ONNX 编码器（torch-free 路径）。

        目录里需要：`tokenizer.json` + 任一 `model_quantized/model_int8/model.onnx`。
        维度优先读同目录 `config.json` 的 hidden_size，读不到再看输出张量。
        """
        base = Path(ref)
        if not base.is_dir():
            return False
        model_file = None
        for name in ("model_quantized.onnx", "model_int8.onnx", "model.onnx"):
            cand = base / name
            if cand.exists():
                model_file = cand
                break
        tok_file = base / "tokenizer.json"
        if model_file is None or not tok_file.exists():
            return False
        try:
            import onnxruntime as ort  # type: ignore
            from tokenizers import Tokenizer  # type: ignore

            tok = Tokenizer.from_file(str(tok_file))
            tok.enable_truncation(max_length=512)
            sess = ort.InferenceSession(
                str(model_file), providers=["CPUExecutionProvider"]
            )
            dim = 0
            cfg_file = base / "config.json"
            if cfg_file.exists():
                try:
                    dim = int(
                        json.loads(cfg_file.read_text(encoding="utf-8")).get(
                            "hidden_size"
                        )
                        or 0
                    )
                except Exception:  # noqa: BLE001
                    dim = 0
            if dim <= 0:
                shape = sess.get_outputs()[0].shape
                if isinstance(shape[-1], int):
                    dim = shape[-1]
            self._onnx_tok = tok
            self._onnx_session = sess
            self._onnx_inputs = {i.name for i in sess.get_inputs()}
            self._onnx_dim = dim or 512
            return True
        except Exception:  # noqa: BLE001  加载失败就退回原路径
            self._onnx_session = None
            self._onnx_tok = None
            return False

    def _onnx_encode(self, text: str) -> list[float] | None:
        """BGE 系列用 CLS pooling + L2 归一化（与 sentence-transformers 一致）。"""
        try:
            import numpy as np  # type: ignore

            enc = self._onnx_tok.encode(text)
            ids = np.array([enc.ids], dtype=np.int64)
            feeds: dict[str, Any] = {}
            if "input_ids" in self._onnx_inputs or not self._onnx_inputs:
                feeds["input_ids"] = ids
            else:
                feeds[next(iter(self._onnx_inputs))] = ids
            if "attention_mask" in self._onnx_inputs:
                feeds["attention_mask"] = np.ones_like(ids)
            if "token_type_ids" in self._onnx_inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            out = self._onnx_session.run(None, feeds)[0]
            vec = np.asarray(out)[0, 0, :]  # [batch, seq, hidden] → CLS
            norm = float(np.linalg.norm(vec))
            if norm <= 0.0:
                return None
            return [float(x) for x in (vec / norm).tolist()]
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _model_ready(ref: str) -> bool:
        """本地到底有没有这个模型（有才允许尝试加载）。

        - `ref` 是目录 → 直接看目录在不在（用户自己放的模型）；
        - `ref` 是裸模型名 → 看 HF 缓存里有没有，没有就别去联网。
        """
        if not ref:
            return False
        if Path(ref).exists():
            return True
        try:
            from huggingface_hub import constants  # type: ignore

            cache = Path(constants.HF_HUB_CACHE)
        except Exception:
            cache = Path.home() / ".cache" / "huggingface" / "hub"
        return (cache / f"models--{ref.replace('/', '--')}").exists()

    def _model_ref(self) -> str:
        """模型引用：优先本地目录（离线可用），否则用模型名。"""
        local_dir = self.cfg.ai.embedding.model_dir
        if local_dir and Path(local_dir).exists():
            return str(local_dir)
        return self.cfg.ai.embedding.model

    def available(self) -> bool:
        """是否可用真实模型（hash 后端仍可用于流程测试）。"""
        backend = self._load()
        return backend in ("sentence-transformers", "transformers", "onnx")

    @property
    def backend(self) -> str:
        return self._load()

    # -- 编码 -------------------------------------------------------------
    def encode(self, text: str) -> list[float] | None:
        """编码为向量。返回 None 表示不可用。"""
        text = (text or "").strip()
        if not text:
            return None
        backend = self._load()
        if backend == "onnx":
            return self._onnx_encode(text)
        if backend == "sentence-transformers":
            try:
                vec = self._model.encode(text, normalize_embeddings=True)
                return [float(x) for x in vec]
            except Exception:
                return None
        if backend == "transformers":
            try:
                import torch  # type: ignore

                with torch.no_grad():
                    inputs = self._tokenizer(
                        text, return_tensors="pt", truncation=True, max_length=512
                    )
                    output = self._torch_model(**inputs)
                    cls = output.last_hidden_state[:, 0]
                    vec = torch.nn.functional.normalize(cls, dim=-1)[0]
                return [float(x) for x in vec.tolist()]
            except Exception:
                return None
        return self._hash_encode(text)

    def _hash_encode(self, text: str) -> list[float]:
        """确定性哈希向量（降级后端）。

        用字符二元组哈希投影到固定维度，语义相关性弱，但流程可跑通。
        """
        dim = self.cfg.ai.embedding.dim
        tokens = _TOKEN_RE.findall(text.lower())
        if not tokens:
            tokens = list(text[:200])
        vec = [0.0] * dim
        grams: list[str] = []
        for tok in tokens:
            grams.append(tok)
        joined = "".join(tokens)
        grams.extend(joined[i : i + 2] for i in range(len(joined) - 1))
        for gram in grams:
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    # -- 存取 -------------------------------------------------------------
    @staticmethod
    def pack(vector: list[float]) -> bytes:
        return struct.pack(f"<{len(vector)}f", *vector)

    @staticmethod
    def unpack(blob: bytes) -> list[float]:
        count = len(blob) // 4
        return list(struct.unpack(f"<{count}f", blob[: count * 4]))

    def save_vector(self, note_path: str, vector: list[float]) -> None:
        from app.core.vault import now_iso

        def _run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO embeddings(note_path, vector, model, updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(note_path) DO UPDATE SET "
                "vector=excluded.vector, model=excluded.model, updated_at=excluded.updated_at",
                (
                    note_path,
                    self.pack(vector),
                    f"{self.cfg.ai.embedding.model}@{self.backend}",
                    now_iso(),
                ),
            )

        self.db.write(_run)

    def load_all_vectors(self) -> list[tuple[str, list[float]]]:
        rows = self.db.query("SELECT note_path, vector FROM embeddings")
        out: list[tuple[str, list[float]]] = []
        for row in rows:
            blob = row["vector"]
            if not blob:
                continue
            out.append((str(row["note_path"]), self.unpack(blob)))
        return out

    # -- 召回 -------------------------------------------------------------
    def _cosine(self, a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def search_vector(
        self, vector: list[float], top_k: int = 5, exclude: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """余弦 Top-K 召回。"""
        exclude = exclude or set()
        candidates = self.load_all_vectors()
        if not candidates:
            return []

        # 排除 A 层（只有 B/C 层参与召回）
        valid_paths = {
            str(r["note_path"])
            for r in self.db.query("SELECT note_path FROM notes")
        }

        scored: list[tuple[float, str]] = []
        for path, vec in candidates:
            if path in exclude:
                continue
            if valid_paths and path not in valid_paths:
                continue
            score = self._cosine(vector, vec)
            if score > 0:
                scored.append((score, path))

        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[dict[str, Any]] = []
        for score, path in scored[:top_k]:
            row = self.db.query_one(
                "SELECT note_path, title, level FROM notes WHERE note_path = ?", (path,)
            )
            results.append(
                {
                    "note_path": path,
                    "title": str(row["title"]) if row else path,
                    "level": str(row["level"]) if row else "",
                    "score": round(score, 4),
                    "wikilink": f"[[{Path(path).stem}]]",
                    "reason": "embedding 相似度",
                }
            )
        return results

    # -- 业务封装 ---------------------------------------------------------
    def related_for_item(self, item_id: int, top_k: int = 5) -> list[dict[str, Any]]:
        """B 层入库时召回相关笔记（§6.4）。"""
        row = self.db.query_one(
            "SELECT title, ai_summary, raw_text, inbox_path FROM items WHERE id = ?",
            (item_id,),
        )
        if row is None:
            return []
        text = " ".join(
            [
                str(row["title"] or ""),
                str(row["ai_summary"] or ""),
                str(row["raw_text"] or "")[:500],
            ]
        ).strip()
        if not text:
            return []
        vector = self.encode(text)
        if vector is None:
            return []
        return self.search_vector(vector, top_k=top_k)

    def similar_notes(self, note_path: str, top_k: int = 10) -> list[dict[str, Any]]:
        """找相似笔记。"""
        row = self.db.query_one(
            "SELECT vector FROM embeddings WHERE note_path = ?", (note_path,)
        )
        if row is None or not row["vector"]:
            return []
        vector = self.unpack(row["vector"])
        return self.search_vector(vector, top_k=top_k, exclude={note_path})

    def similar_to_text(self, text: str, top_k: int = 5) -> list[dict[str, Any]]:
        """文本 → 相似笔记（C 层灵感推荐）。"""
        vector = self.encode(text)
        if vector is None:
            return []
        return self.search_vector(vector, top_k=top_k)

    def similar_to_incoming(
        self, url: str, title: str, top_k: int = 3
    ) -> list[dict[str, Any]]:
        """入库前的向量软提示（相似度 > 0.85）。"""
        if not title:
            return []
        vector = self.encode(title)
        if vector is None:
            return []
        return self.search_vector(vector, top_k=top_k)

    def embed_note_by_item(self, item_id: int) -> None:
        """为某条目生成 embedding（入库后异步）。"""
        # **items 表没有 personal_summary 列**（手稿存在 notes 表和 B 层 md 里），
        # 原来查这一列必然抛 OperationalError → 每次入库的 embed 任务都失败，
        # 「相关笔记推荐」永远是空的。
        row = self.db.query_one(
            "SELECT title, ai_summary, processed_path FROM items WHERE id = ?",
            (item_id,),
        )
        if row is None:
            return

        # 手稿从本条目对应的 B 层 md 里读
        manuscript = ""
        processed_path = str(row["processed_path"] or "").strip()
        if processed_path:
            try:
                from app.core import vault as vault_mod

                note = vault_mod.read_note(Path(processed_path))
                if note is not None:
                    manuscript = str(note.frontmatter.get("personal_summary") or "")[:500]
            except Exception:  # noqa: BLE001 - 读不到手稿不该让整个 embed 失败
                manuscript = ""

        text = " ".join(
            [
                str(row["title"] or ""),
                str(row["ai_summary"] or ""),
                manuscript,
            ]
        ).strip()
        if not text:
            return
        vector = self.encode(text)
        if vector is None:
            return

        # **必须写回本条目自己的 B 层笔记**。
        # 原来取的是「最近入库的任意一篇」（ORDER BY reviewed_at DESC LIMIT 1），
        # 连续入库两条就会互相覆盖对方的向量。
        if not processed_path:
            return
        self.save_vector(processed_path, vector)

    def rebuild_all(self, progress: Any = None) -> dict[str, Any]:
        """全库向量重建。"""
        rows = self.db.query(
            "SELECT n.note_path, n.title FROM notes n WHERE n.level IN ('processed','note')"
        )
        count = 0
        for index, row in enumerate(rows):
            path = str(row["note_path"])
            note = None
            try:
                from app.core import vault as vault_mod

                note = vault_mod.read_note(Path(path))
            except Exception:
                note = None
            if note is None:
                continue
            text = " ".join(
                [
                    note.title,
                    note.summary,
                    note.manuscript[:500],
                    note.body[:1500],
                ]
            ).strip()
            if not text:
                continue
            vector = self.encode(text)
            if vector is None:
                break
            self.save_vector(path, vector)
            count += 1
            if progress:
                progress(index + 1, len(rows))
        return {"ok": True, "count": count, "backend": self.backend}

    def embed_note(self, note_path: str) -> bool:
        """为单个笔记生成向量。"""

        from app.core import vault as vault_mod

        note = vault_mod.read_note(Path(note_path))
        if note is None:
            return False
        text = " ".join(
            [note.title, note.summary, note.manuscript[:500], note.body[:1500]]
        ).strip()
        if not text:
            return False
        vector = self.encode(text)
        if vector is None:
            return False
        self.save_vector(note_path, vector)
        return True


_embedding: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    global _embedding
    if _embedding is None:
        _embedding = EmbeddingService()
    return _embedding
