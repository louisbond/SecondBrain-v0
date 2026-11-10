"""SQLite 存储层。

设计要点（对齐 v0.5.0 §4.5 与 §3.2）：
- WAL 模式 + busy_timeout=5000，避免写冲突；
- **单 writer 队列**：所有写操作通过 `execute_write` 提交到单一后台线程串行执行，
  杜绝 "database is locked"；
- md 文件是唯一真相源，本库所有表均可由 `reindex` 重建；
- FTS5 双列写入：`fts_payload(...)` 存 jieba + bigram_cjk 分词结果，查询对称。
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any, TypeVar

from app.core.config import get_config

T = TypeVar("T")

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- A 层条目（队列与状态机）
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source_url TEXT UNIQUE,
    platform TEXT,
    raw_text TEXT,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
      -- queued/extracting/extracted/extract_failed/ai_processing/ai_failed/pending_review/reviewed
    extractor TEXT,
    asr INTEGER DEFAULT 0,
    error_reason TEXT,
    error_detail TEXT,
    inbox_path TEXT,
    processed_path TEXT,
    media_url TEXT,             -- 视频/音频直链或本地文件路径，供 ASR 转写口播台词
    ai_summary TEXT,
    ai_summary_type TEXT,
    ai_model TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP
);

-- 处理队列任务
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    item_id INTEGER REFERENCES items(id),
    kind TEXT,                  -- extract / asr / ai / embed / reindex
    state TEXT DEFAULT 'pending',  -- pending/running/done/failed/cancelled
    progress REAL,
    priority INTEGER DEFAULT 5,
    last_error TEXT,
    attempts INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);

-- B/C 层笔记元数据镜像（md 为真相源，此表可重建）
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    note_path TEXT UNIQUE,
    level TEXT,                 -- processed / note
    title TEXT,
    form TEXT,
    personal_summary_empty INTEGER DEFAULT 1,
    digested INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    created_at TIMESTAMP,
    reviewed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE,
    source TEXT                 -- seed/ai/manual/merged
);
-- 分区是**树**（最多 3 层）：parent_id 自引用，path 是物化路径 "成长树/思维逻辑"
CREATE TABLE IF NOT EXISTS partitions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES partitions(id) ON DELETE CASCADE,
    level INTEGER NOT NULL DEFAULT 1,
    path TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS note_tags (
    note_id INTEGER,
    tag_id INTEGER
);
CREATE TABLE IF NOT EXISTS note_partitions (
    note_id INTEGER,
    partition_id INTEGER
);

-- 链接边（仅 confirmed_related 与 C 层 citations 解析为边）
CREATE TABLE IF NOT EXISTS links (
    src_note_id INTEGER,
    dst_note_id INTEGER,
    kind TEXT,                  -- confirmed / citation
    PRIMARY KEY (src_note_id, dst_note_id, kind)
);

-- AI 建议（追溯用）
CREATE TABLE IF NOT EXISTS ai_suggestions (
    id INTEGER PRIMARY KEY,
    note_path TEXT,
    suggestion_type TEXT,       -- tag/new_tag/partition/related_keyword
    suggestion_value TEXT,
    suggestion_reason TEXT,
    status TEXT DEFAULT 'pending',  -- pending/accepted/rejected
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 向量（bge-m3, 1024 维）
CREATE TABLE IF NOT EXISTS embeddings (
    note_path TEXT PRIMARY KEY,
    vector BLOB,
    model TEXT,
    updated_at TIMESTAMP
);

-- 标签合并历史（A→B 改写审计）
CREATE TABLE IF NOT EXISTS tag_merges (
    id INTEGER PRIMARY KEY,
    src_name TEXT,
    dst_name TEXT,
    affected INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 用户删掉过的内置模板（避免被重新播种回来）
CREATE TABLE IF NOT EXISTS deleted_templates (
    name TEXT PRIMARY KEY,
    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 解析模板：工作区"按模板生成解析结果"的模板库（可手动增删改）
CREATE TABLE IF NOT EXISTS parse_templates (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    source TEXT DEFAULT 'manual',   -- manual / ai
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创作模板：原创区"空模板"的骨架（v37：从硬编码搬到库里，管理区可增删改）
-- 与 parse_templates 的区别：这个是给**人**看的 Markdown 骨架，
-- 那个是给**AI**看的提示词。别加错表。
CREATE TABLE IF NOT EXISTS note_templates (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    suggest_tags TEXT DEFAULT '',        -- 逗号分隔
    suggest_partition TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0,
    source TEXT DEFAULT 'manual',        -- manual / builtin
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 用户删掉的创作模板（防"删了又被播种回来"）
CREATE TABLE IF NOT EXISTS deleted_note_templates (
    name TEXT PRIMARY KEY,
    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title, summary, manuscript, body, tags,
    level UNINDEXED, note_path UNINDEXED, snippet_src UNINDEXED,
    tokenize='unicode61'
);

CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_platform ON items(platform);
CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_kind ON jobs(kind);
CREATE INDEX IF NOT EXISTS idx_jobs_item ON jobs(item_id);
CREATE INDEX IF NOT EXISTS idx_notes_level ON notes(level);
CREATE INDEX IF NOT EXISTS idx_notes_path ON notes(note_path);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_note_id);
CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst_note_id);
CREATE INDEX IF NOT EXISTS idx_sugg_status ON ai_suggestions(status);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- 迁移版本（幂等迁移用）
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    """SQLite 单例封装：读连接多线程共享，写操作串行化。"""

    def __init__(self, db_path: Path | None = None) -> None:
        cfg = get_config()
        self.db_path = Path(db_path) if db_path else cfg.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_queue: queue.Queue[tuple[Callable[[sqlite3.Connection], Any], Future]] = (
            queue.Queue()
        )
        self._writer_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._init_schema()

    # -- 连接管理 ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None,  # autocommit，手动控制事务
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程的读连接（懒创建）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = self.conn
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._migrate_fts(conn)
        # 幂等迁移（分区树化等）——必须在 schema 之后
        try:
            from app.core.migrations import run_migrations

            done = run_migrations(conn)
            if done:
                print(f"[db] 已应用迁移：{', '.join(done)}")
        except Exception as exc:  # noqa: BLE001
            print(f"[db] 迁移失败（不影响启动）：{exc}")

    @staticmethod
    def _migrate_fts(conn: sqlite3.Connection) -> None:
        """FTS 表结构迁移。

        v0.5.0 初版把全部字段塞进 summary 列，且缺 snippet_src 列；
        现改为每字段各占一列（配合 bm25 列权重），并加 UNINDEXED 的
        snippet_src 用于生成摘要片段。检测到旧结构就重建 FTS 表
        （索引可从 md 重建，见 §10「重建索引」）。
        """
        try:
            rows = conn.execute("PRAGMA table_info(notes_fts)").fetchall()
        except sqlite3.OperationalError:
            return
        if not rows:
            return

        columns = [str(r["name"]) for r in rows]
        expected = [
            "title",
            "summary",
            "manuscript",
            "body",
            "tags",
            "level",
            "note_path",
            "snippet_src",
        ]
        if columns == expected:
            return

        conn.execute("DROP TABLE IF EXISTS notes_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE notes_fts USING fts5("
            "title, summary, manuscript, body, tags, "
            "level UNINDEXED, note_path UNINDEXED, snippet_src UNINDEXED, "
            "tokenize='unicode61')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('fts_layout', 'v2')"
        )

    # -- 单 writer 队列 ---------------------------------------------------
    def _writer_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._write_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            func, future = item
            try:
                conn = self.conn
                result = func(conn)
                if future is not None and not future.cancelled():
                    future.set_result(result)
            except Exception as exc:  # noqa: BLE001 - 写线程必须吞掉异常并回传
                if future is not None and not future.cancelled():
                    future.set_exception(exc)
            finally:
                self._write_queue.task_done()

    def ensure_writer(self) -> None:
        with self._lock:
            if self._writer_thread is None or not self._writer_thread.is_alive():
                self._stop_event.clear()
                self._writer_thread = threading.Thread(
                    target=self._writer_loop,
                    name="sb-sqlite-writer",
                    daemon=True,
                )
                self._writer_thread.start()

    def execute_write(self, func: Callable[[sqlite3.Connection], T]) -> Future:
        """提交一个写操作到单 writer 队列，返回 Future。

        写函数接收一个连接，需自行管理事务（默认 autocommit，多语句可用 with conn:）。
        """
        self.ensure_writer()
        future: Future = Future()
        self._write_queue.put((func, future))
        return future

    def write(self, func: Callable[[sqlite3.Connection], T]) -> T:
        """同步执行写操作（阻塞直到完成）。"""
        return self.execute_write(func).result(timeout=120)

    def write_tx(self, func: Callable[[sqlite3.Connection], T]) -> T:
        """同步执行写操作，并把整个函数包在一个显式事务里。

        连接使用 autocommit（isolation_level=None），批量 executemany 若不显式
        BEGIN，会变成"每行一次提交"——5000 条索引重建会产生 5000 次 WAL 提交，
        这是重建超时的根因。批量写必须走这个方法。
        """
        def _run(conn: sqlite3.Connection) -> T:
            conn.execute("BEGIN")
            try:
                result = func(conn)
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            conn.execute("COMMIT")
            return result

        return self.write(_run)

    # -- 读操作 -----------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return row[0] if row is not None else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """同步执行写 SQL（走 writer 队列，保证与其它写串行）。"""
        def _run(conn: sqlite3.Connection) -> sqlite3.Cursor:
            return conn.execute(sql, params)

        return self.write(_run)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        rows = list(seq)

        def _run(conn: sqlite3.Connection) -> None:
            conn.executemany(sql, rows)

        self.write(_run)

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        self._stop_event.set()
        try:
            self._write_queue.put_nowait((None, None))  # type: ignore[arg-type]
        except Exception:
            pass
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=5)
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
        # WAL 落盘必须用**独立连接**：self._local 是 threading.local，
        # close() 只看得见当前线程的连接；writer 线程 join 之后锁已释放，
        # 此时 TRUNCATE 才拿得到排他锁。不做这步 -wal 会一路涨
        # （实测关闭后仍有 2.4MB），下次启动要整段重放。
        try:
            tmp = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
            try:
                tmp.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                tmp.close()
        except Exception:
            pass


_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = Database()
    return _db


def reset_db() -> None:
    """测试用：重置单例。"""
    global _db
    with _db_lock:
        if _db is not None:
            _db.close()
        _db = None
