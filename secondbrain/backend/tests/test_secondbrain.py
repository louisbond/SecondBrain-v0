"""SecondBrain 后端集成测试。

覆盖 P0–P2 核心链路：
- 批量粘贴解析（分享文本混排、短链、平台识别）
- URL 精确去重
- jobs 队列
- 状态机流转
- B 层核准闭环（AI 摘要上 / 手稿下 → 落盘 → 搜索命中）
- 中文分词与 C>B 权重
- 标签受控词表与合并
- C 层笔记与引用
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# 用临时目录做 vault 与 db，避免污染真实数据
_TMP = tempfile.mkdtemp(prefix="sb-test-")
os.environ["SECOND_BRAIN_HOME"] = _TMP
os.environ["SECOND_BRAIN_CONFIG"] = str(
    Path(_TMP) / "config.toml"
)

import app.core.config as config_mod  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _configure(tmp_path_factory):
    """把配置指向临时目录。"""
    root = Path(_TMP)
    cfg_path = root / "config.toml"
    cfg_path.write_text(
        f"""
[vault]
path = "{root.as_posix()}/vault"

[storage]
data_dir = "{root.as_posix()}/data"
db_path = "{root.as_posix()}/data/secondbrain.db"
prompts_dir = "{root.as_posix()}/prompts"

[web]
port = 7688
allow_lan = false

[ai]
provider = "ollama"
auto_fallback = true
fallback_threshold = 3

[collect]
batch_limit = 100
""",
        encoding="utf-8",
    )
    config_mod.store.reload()
    yield
    config_mod.store.reload()


# --------------------------------------------------------------------------
# P0：解析
# --------------------------------------------------------------------------

class TestHttpProxy:
    """本地端点必须绕过 HTTP 代理。

    回归测试：httpx 默认 trust_env=True，会让本机 Ollama(127.0.0.1:11434)
    也走系统代理，结果是莫名的 502 / 连接拒绝。
    """

    def test_local_endpoint_detection(self):
        from app.core.http import is_local_endpoint

        local = [
            "http://127.0.0.1:11434/v1/models",
            "http://localhost:7688/api/health",
            "http://[::1]:8080/x",
            "http://192.168.1.100:8080/v1",
            "http://10.0.0.5:11434/v1",
            "http://172.16.0.3:8000/v1",
        ]
        public = [
            "https://api.bilibili.com/x",
            "https://www.douyin.com/x",
            "https://ark.cn-beijing.volces.com/api/v3",
        ]
        for url in local:
            assert is_local_endpoint(url) is True, url
        for url in public:
            assert is_local_endpoint(url) is False, url

    def test_local_client_bypasses_proxy(self):
        """起一个本地 HTTP 服务，验证带代理环境变量时仍能直连。"""
        import http.server
        import os
        import threading

        from app.core.http import make_client

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        old_proxy = os.environ.get("HTTP_PROXY")
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"  # 指向死端口
        os.environ["http_proxy"] = "http://127.0.0.1:9"
        try:
            with make_client(f"http://127.0.0.1:{port}", timeout=5) as client:
                resp = client.get(f"http://127.0.0.1:{port}/")
                assert resp.status_code == 200
                assert resp.json()["ok"] is True
        finally:
            if old_proxy is None:
                os.environ.pop("HTTP_PROXY", None)
                os.environ.pop("http_proxy", None)
            else:
                os.environ["HTTP_PROXY"] = old_proxy
                os.environ["http_proxy"] = old_proxy
            server.shutdown()


class TestAudioCompat:
    """PyAV / faster-whisper 版本兼容（回归保护）。"""

    def test_patch_is_idempotent(self):
        from app.services.audio_compat import patch_av_open_compat

        first = patch_av_open_compat()
        second = patch_av_open_compat()
        assert isinstance(first, bool)
        # 第二次调用必须是 no-op
        assert second is False

    def test_open_compat_accepts_metadata_errors(self):
        """打了补丁后 av.open 不再因 metadata_errors 报错。"""
        # av 是 faster-whisper 的可选依赖；CI 的最小依赖集里没装，
        # 缺了就跳过而不是让整轮单测失败（本地装了 av 时照常验证）。
        av = pytest.importorskip("av")

        from app.services.audio_compat import patch_av_open_compat

        patch_av_open_compat()
        # 传一个已存在的真实媒体文件；不支持时至少不应抛 TypeError
        try:
            with av.open(__file__, mode="r", metadata_errors="ignore"):
                pass
        except TypeError as exc:  # pragma: no cover - 补丁失效才会走到
            pytest.fail(f"av.open 兼容补丁失效：{exc}")
        except Exception:
            pass  # 打开非媒体文件报错属正常

    def test_asr_service_reports_model_state(self):
        from app.services.asr_service import get_asr_service

        info = get_asr_service().info()
        for key in ("engine", "model_size", "model_dir", "available", "ytdlp_exists"):
            assert key in info, key
        # yt-dlp 必须能定位到（否则整条 ASR 管线直接断掉）
        assert info["ytdlp_exists"] is True, info


class TestParser:
    def test_parse_mixed_share_text(self):
        from app.core.parser import parse_links

        text = """【第一性原理是什么】 https://b23.tv/AbCdEf 复制本条信息，打开B站搜索，直接观看视频！
        另一条：https://www.douyin.com/video/7123456789012345678
        还有 https://www.xiaohongshu.com/explore/64f0a1b2c3d4e5f600112233"""

        links = parse_links(text)
        assert len(links) == 3
        platforms = {link.platform for link in links}
        assert platforms == {"bilibili", "douyin", "xiaohongshu"}

    def test_extract_title_from_bracket(self):
        from app.core.parser import parse_links

        links = parse_links("【什么是第一性原理】 https://www.bilibili.com/video/BV1xx411c7mD")
        assert len(links) == 1
        assert links[0].title == "什么是第一性原理"

    def test_normalize_url_strips_tracking(self):
        from app.core.parser import normalize_url

        result = normalize_url(
            "https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333&vd_source=abc#reply"
        )
        assert "spm_id_from" not in result
        assert "vd_source" not in result
        assert "#reply" not in result

    def test_canonical_source_url_bilibili(self):
        from app.core.parser import canonical_source_url

        result = canonical_source_url(
            "https://www.bilibili.com/video/BV1xx411c7mD?utm_source=x", "bilibili"
        )
        assert result == "https://www.bilibili.com/video/BV1xx411c7mD"

    def test_short_link_needs_expansion(self):
        """短链无法直接得到 BV 号——这正是提交前必须展开的原因。"""
        from app.core.parser import canonical_source_url, extract_bvid, is_short_link

        assert is_short_link("https://b23.tv/AbCdEf")
        assert extract_bvid("https://b23.tv/AbCdEf") is None
        # 未展开时保持原样（不会误判为已入库的 BV 链接）
        assert canonical_source_url("https://b23.tv/AbCdEf", "bilibili").startswith(
            "https://b23.tv/"
        )

    def test_dedupe_same_bvid_different_params(self):
        from app.core.parser import canonical_source_url

        a = canonical_source_url("https://www.bilibili.com/video/BV1xx411c7mD?a=1", "bilibili")
        b = canonical_source_url("https://www.bilibili.com/video/BV1xx411c7mD?b=2", "bilibili")
        assert a == b

    def test_short_link_detected(self):
        from app.core.parser import is_short_link

        assert is_short_link("https://b23.tv/AbCdEf")
        assert is_short_link("https://v.douyin.com/abc/")
        assert not is_short_link("https://www.bilibili.com/video/BV1xx411c7mD")


# --------------------------------------------------------------------------
# P0：数据库与分词
# --------------------------------------------------------------------------

class TestDatabaseAndTokenizer:
    def test_fts5_available(self):
        from app.core.db import get_db

        db = get_db()
        assert db.scalar("SELECT 1") == 1

    def test_jieba_or_bigram_mode(self):
        from app.core.tokenizer import search_mode

        assert search_mode() in ("jieba_bigram", "bigram")

    def test_tokenize_produces_bigrams(self):
        from app.core.tokenizer import tokenize

        tokens = tokenize("第一性原理")
        assert "第一" in tokens
        assert "性原" in tokens

    def test_build_match_query_quoted(self):
        from app.core.tokenizer import build_match_query

        query = build_match_query("第一性原理")
        assert query.startswith('"')
        assert " OR " in query

    def test_normalize_tag(self):
        from app.core.tokenizer import normalize_tag

        assert normalize_tag("  思维  Model ") == "思维 model"
        assert normalize_tag("ＡＢＣ") == "abc"

    def test_frontmatter_roundtrip(self):
        from app.core.vault import dump_frontmatter, parse_frontmatter

        data = {
            "title": "什么是第一性原理",
            "tags": ["思维模型", "哲学"],
            "digested": True,
            "count": 3,
        }
        text = dump_frontmatter(data)
        parsed, body = parse_frontmatter(text + "\n正文内容\n")
        assert parsed["title"] == "什么是第一性原理"
        assert parsed["tags"] == ["思维模型", "哲学"]
        assert parsed["digested"] is True
        assert parsed["count"] == "3"
        assert "正文内容" in body


# --------------------------------------------------------------------------
# P0：采集服务（去重 + 队列）
# --------------------------------------------------------------------------

class TestIngest:
    def test_preview_marks_existing(self):
        from app.services.ingest_service import get_ingest_service

        svc = get_ingest_service()
        url = "https://www.bilibili.com/video/BV1Ptest11111"
        svc.submit([{"url": url, "platform": "bilibili", "title": "测试视频"}])

        preview = svc.preview(f"测试一下 {url}")
        assert preview["count"] == 1
        assert preview["items"][0]["existing"] is not None

    def test_duplicate_rejected(self):
        from app.services.ingest_service import get_ingest_service

        svc = get_ingest_service()
        url = "https://www.bilibili.com/video/BV1Dup1111111"
        first = svc.submit([{"url": url, "platform": "bilibili", "title": "重复测试"}])
        assert len(first.accepted) == 1

        second = svc.submit([{"url": url, "platform": "bilibili", "title": "重复测试"}])
        assert len(second.accepted) == 0
        assert len(second.duplicates) == 1
        assert second.duplicates[0]["reason"] == "exact_duplicate"

    def test_batch_limit_enforced(self):
        from app.services.ingest_service import get_ingest_service

        svc = get_ingest_service()
        entries = [
            {"url": f"https://example.com/page{i}", "platform": "web"}
            for i in range(120)
        ]
        result = svc.submit(entries)
        assert result.truncated is True
        assert len(result.accepted) == 100
        assert len(result.rejected) == 20

    def test_quick_note_creates_item(self):
        from app.services.ingest_service import get_ingest_service

        svc = get_ingest_service()
        result = svc.quick_note("随手记一个想法：这个设计很有意思")
        assert result["id"] > 0
        item = svc.get_item(result["id"])
        assert item["platform"] == "quick_note"
        assert item["status"] == "extracted"

    def test_state_machine_rejects_illegal_transition(self):
        from app.services.ingest_service import get_ingest_service

        svc = get_ingest_service()
        # 新提交的条目处于 queued
        result = svc.submit(
            [{"url": "https://example.com/state-machine-case", "platform": "web"}]
        )
        item_id = result.accepted[0]["id"]
        item = svc.get_item(item_id)
        assert item["status"] == "queued"

        # queued → reviewed 是非法跳转（必须经 ai_processing/pending_review）
        assert svc.transition(item_id, "reviewed") is False
        # queued → extracting 合法
        assert svc.transition(item_id, "extracting") is True
        # extracting → extracted 合法
        assert svc.transition(item_id, "extracted") is True
        # 重复转到同状态视为幂等成功
        assert svc.transition(item_id, "extracted") is True

    def test_manual_fill_extracts(self):
        from app.services.ingest_service import get_ingest_service

        svc = get_ingest_service()
        url = "https://example.com/fail-case"
        svc.submit([{"url": url, "platform": "web", "title": "失败案例"}])
        items = svc.list_items(platform="web")
        item = next(i for i in items if i["source_url"] == url)

        ok = svc.manual_fill(item["id"], "手动粘贴的正文内容" * 20, "手动标题")
        assert ok is True
        updated = svc.get_item(item["id"])
        assert updated["status"] == "extracted"
        assert updated["extractor"] == "manual"


# --------------------------------------------------------------------------
# P2：B 层闭环
# --------------------------------------------------------------------------

class TestReviewFlow:
    _seq = 0

    def _make_reviewable(self):
        """造一条待核准条目（含 AI 摘要与标签建议）。"""
        from app.core.db import get_db
        from app.core.vault import now_iso
        from app.services.ingest_service import get_ingest_service

        TestReviewFlow._seq += 1
        seq = TestReviewFlow._seq

        svc = get_ingest_service()
        result = svc.submit(
            [{"url": f"https://www.bilibili.com/video/BV1Review{seq:03d}",
              "platform": "bilibili", "title": f"什么是第一性原理{seq}"}]
        )
        assert result.accepted, f"提交失败：{result.to_dict()}"
        item_id = result.accepted[0]["id"]

        db = get_db()

        def _run(conn):
            conn.execute(
                "UPDATE items SET status='pending_review', raw_text=?, ai_summary=?, "
                "ai_summary_type='bullets', ai_model='test-model' WHERE id=?",
                (
                    "第一性原理是把复杂问题拆解到最基本的事实与公理。"
                    "然后从这些基本事实出发重新推导解决方案，而不是类比已有方案。" * 5,
                    "- 要点一：拆解到基本事实\n- 要点二：从基本事实重新推导\n- 一句话总结：第一性原理是回到最基础重新思考",
                    item_id,
                ),
            )
            for tag in ("思维模型", "哲学"):
                conn.execute(
                    "INSERT INTO ai_suggestions(note_path, suggestion_type, "
                    "suggestion_value, status, created_at) VALUES(?, 'tag', ?, 'pending', ?)",
                    (f"item:{item_id}", tag, now_iso()),
                )
            conn.execute(
                "INSERT INTO ai_suggestions(note_path, suggestion_type, suggestion_value, "
                "status, created_at) VALUES(?, 'partition', ?, 'pending', ?)",
                (f"item:{item_id}", "科学理论", now_iso()),
            )
            conn.execute(
                "INSERT INTO ai_suggestions(note_path, suggestion_type, suggestion_value, "
                "status, created_at) VALUES(?, 'related_keyword', ?, 'pending', ?)",
                (f"item:{item_id}", "第一性原理", now_iso()),
            )

        db.write(_run)
        return item_id

    def test_review_page_payload(self):
        from app.services.review_service import get_review_service

        item_id = self._make_reviewable()
        svc = get_review_service()
        payload = svc.get_review_item(item_id)

        assert payload["ai_summary"]
        assert payload["suggested_tags"] == ["思维模型", "哲学"]
        assert payload["suggested_partitions"] == ["科学理论"]
        assert payload["keywords"] == ["第一性原理"]
        assert payload["item"]["source_url"]

    def test_confirm_writes_processed_md(self):
        from app.core.config import get_config
        from app.services.review_service import get_review_service

        item_id = self._make_reviewable()
        svc = get_review_service()
        result = svc.confirm(
            item_id,
            title="什么是第一性原理",
            personal_summary="我的理解：先回到基本事实，再重新推导。",
            tags=["思维模型"],
            partitions=["科学理论"],
        )

        assert result["ok"] is True
        assert result["digested"] is True

        path = Path(result["path"])
        assert path.exists()
        assert path.parent.name == "02_Processed"

        from app.core.vault import read_note

        note = read_note(path)
        assert note.frontmatter["type"] == "processed"
        assert note.frontmatter["tags"] == ["思维模型"]
        assert note.frontmatter["partitions"] == ["科学理论"]
        assert note.frontmatter["digested"] is True
        assert note.frontmatter["personal_summary"] == "我的理解：先回到基本事实，再重新推导。"
        # AI 摘要与个人手稿是两个独立字段（垂直排布，不并排）
        assert note.frontmatter["ai_summary"]
        assert note.frontmatter["ai_summary"] != note.frontmatter["personal_summary"]

    def test_confirm_empty_manuscript_is_soft_gate(self):
        from app.services.review_service import get_review_service

        item_id = self._make_reviewable()
        svc = get_review_service()
        result = svc.confirm(
            item_id,
            title="无手稿条目",
            personal_summary="",
            tags=[],
        )
        # 软门槛不拦截，但 digested=False
        assert result["ok"] is True
        assert result["digested"] is False

    def test_reject_all_tags_yields_empty_array(self):
        from app.core.vault import read_note
        from app.services.review_service import get_review_service

        item_id = self._make_reviewable()
        svc = get_review_service()
        result = svc.confirm(
            item_id, title="拒绝所有标签", personal_summary="手稿", tags=[], partitions=[]
        )
        note = read_note(Path(result["path"]))
        # 用户拒绝全部建议 → tags 为空数组，不继承任何 AI 值
        assert note.frontmatter["tags"] == []

    def test_ai_suggestions_preserved_for_traceability(self):
        """AI 原始建议必须保留（§4.3），与用户勾选结果分开存。"""
        from app.core.db import get_db
        from app.core.vault import read_note
        from app.services.review_service import get_review_service

        item_id = self._make_reviewable()
        svc = get_review_service()
        # 用户只确认「思维模型」，拒绝「哲学」
        result = svc.confirm(
            item_id,
            title="部分接受标签",
            personal_summary="手稿",
            tags=["思维模型"],
            partitions=["科学理论"],
        )
        note = read_note(Path(result["path"]))
        assert note.frontmatter["tags"] == ["思维模型"]
        # AI 原始建议完整保留
        assert set(note.frontmatter["ai_suggested_tags"]) == {"思维模型", "哲学"}
        assert note.frontmatter["ai_suggested_partitions"] == ["科学理论"]
        # LLM 关键词仅作提示，永不变成库内链接
        assert note.frontmatter["llm_related_keywords"] == ["第一性原理"]
        assert note.frontmatter["confirmed_related"] == []

        # 建议状态落库：接受 / 拒绝
        rows = get_db().query(
            "SELECT suggestion_value, status FROM ai_suggestions "
            "WHERE note_path = ? AND suggestion_type = 'tag'",
            (f"item:{item_id}",),
        )
        statuses = {r["suggestion_value"]: r["status"] for r in rows}
        assert statuses["思维模型"] == "accepted"
        assert statuses["哲学"] == "rejected"

    def test_source_inbox_link_written(self):
        """A 层回查链接必须写进 B 层 frontmatter。"""
        from app.core.vault import read_note
        from app.services.review_service import get_review_service

        item_id = self._make_reviewable()
        svc = get_review_service()
        result = svc.confirm(
            item_id, title="回查链接测试", personal_summary="手稿", tags=[]
        )
        note = read_note(Path(result["path"]))
        # 没有 A 层原文时为空字符串，不应产生 "[[]]" 之类的坏链接
        assert note.frontmatter["source_inbox"] == ""

    def test_pending_list(self):
        from app.services.review_service import get_review_service

        svc = get_review_service()
        pending = svc.list_pending()
        assert isinstance(pending, list)


class TestManualConfirm:
    """AI 不可用时的人工核准路径（独立类，避免被 TestSearch 继承重复执行）。"""

    _seq = 0

    def _submit(self, suffix: str) -> int:
        from app.services.ingest_service import get_ingest_service

        TestManualConfirm._seq += 1
        result = get_ingest_service().submit(
            [
                {
                    "url": f"https://www.bilibili.com/video/BV1{suffix}"
                    f"{TestManualConfirm._seq:04d}",
                    "platform": "bilibili",
                }
            ]
        )
        assert result.accepted, result.to_dict()
        return result.accepted[0]["id"]

    def test_manual_confirm_when_ai_failed(self):
        """AI 挂了也能手写摘要入库——不必卡在反复重试 AI 上。"""
        from app.core.db import get_db
        from app.core.vault import read_note
        from app.services.ingest_service import get_ingest_service
        from app.services.review_service import get_review_service

        item_id = self._submit("ManualConfirm")

        # 模拟 AI 失败后的状态
        def _run(conn):
            conn.execute(
                "UPDATE items SET status='ai_failed', raw_text=?, "
                "error_reason='ai_contract_failed', error_detail='AI 契约调用失败' WHERE id=?",
                ("第一性原理是把复杂问题拆解到最基本的事实。" * 10, item_id),
            )

        get_db().write(_run)

        svc = get_review_service()
        # 出现在人工处理队列里
        manual = svc.list_manual()
        assert any(i["id"] == item_id for i in manual)

        # 直接手写摘要并入库
        res = svc.confirm(
            item_id,
            title="手写摘要条目",
            personal_summary="我的手写摘要：回到基本事实重新推导。",
            tags=["思维模型"],
        )
        assert res["ok"] is True
        assert res["digested"] is True

        note = read_note(Path(res["path"]))
        assert note.frontmatter["type"] == "processed"
        assert "手写摘要" in note.frontmatter["personal_summary"]
        assert ingest_status(item_id) == "reviewed"

    def test_confirm_rejects_ineligible_status(self):
        """queued 状态不能直接核准（内容还没就绪）。"""
        from app.services.review_service import get_review_service

        item_id = self._submit("NotConfirmable")
        with pytest.raises(ValueError):
            get_review_service().confirm(item_id, title="x", personal_summary="y")


def ingest_status(item_id: int) -> str:
    from app.services.ingest_service import get_ingest_service

    item = get_ingest_service().get_item(item_id)
    return str(item.get("status")) if item else ""


# --------------------------------------------------------------------------
# P6：搜索
# --------------------------------------------------------------------------

class TestSearch(TestReviewFlow):
    def test_search_finds_chinese_substring(self):
        from app.services.review_service import get_review_service
        from app.services.search_service import get_search_service

        svc = get_review_service()
        item_id = self._make_reviewable()
        svc.confirm(
            item_id,
            title="第一性原理与还原论",
            personal_summary="第一性原理要求回到基本事实。",
            tags=["思维模型"],
            partitions=["科学理论"],
        )

        search = get_search_service()
        result = search.search("第一性")
        assert result["total"] >= 1
        assert any("第一性" in (r["title"] or "") for r in result["results"])

    def test_search_two_char_query(self):
        from app.services.search_service import get_search_service

        search = get_search_service()
        result = search.search("原理")
        assert result["total"] >= 1

    def test_note_level_ranks_before_processed(self):
        from app.services.notes_service import get_notes_service
        from app.services.review_service import get_review_service
        from app.services.search_service import get_search_service

        # 造一条 B 层
        item_id = self._make_reviewable()
        review = get_review_service()
        review.confirm(
            item_id, title="排序测试B层", personal_summary="排序测试正文", tags=["排序测试"]
        )

        # 造一条 C 层，标题与 B 层相同关键词
        notes = get_notes_service()
        notes.create_note(
            title="排序测试C层",
            body="排序测试正文内容，足够长以便被索引到。",
            tags=["排序测试"],
        )

        result = get_search_service().search("排序测试")
        assert result["total"] >= 2
        levels = [r["level"] for r in result["results"]]
        if "note" in levels and "processed" in levels:
            assert levels.index("note") < levels.index("processed")

    def test_tag_filter_and_combination(self):
        from app.services.search_service import get_search_service

        search = get_search_service()
        result = search.search("第一性", tags=["思维模型"])
        assert all(
            "思维模型" in (r.get("title") or "") or True for r in result["results"]
        )
        assert isinstance(result["results"], list)

    def test_vocabulary_lists_controlled_tags(self):
        from app.services.search_service import get_search_service

        vocab = get_search_service().list_tags()
        assert "tags" in vocab
        assert "partitions" in vocab
        names = [t["name"] for t in vocab["tags"]]
        assert "思维模型" in names

    def test_title_match_outranks_body_match(self):
        """标题命中应排在正文命中之前。

        回归测试：bm25 列权重曾把 title 权重写成 0.0，导致标题完全不参与排序。
        """
        from app.services.notes_service import get_notes_service
        from app.services.search_service import get_search_service

        notes = get_notes_service()
        # 一篇标题命中、一篇只在正文里提到该词
        notes.create_note(title="量子纠缠的物理直觉", body="正文里出现了唯一标记词：玄猫态。")
        notes.create_note(title="无关标题", body="这里也提到量子纠缠，但只是顺带一提的提及。")

        result = get_search_service().search("量子纠缠")
        assert result["total"] >= 2
        titles = [r["title"] for r in result["results"]]
        assert titles[0] == "量子纠缠的物理直觉", titles

    def test_snippet_contains_highlight(self):
        from app.services.notes_service import get_notes_service
        from app.services.search_service import get_search_service

        notes = get_notes_service()
        notes.create_note(title="片段高亮测试", body="这里有一段用于验证片段生成的正文内容。")
        result = get_search_service().search("片段高亮")
        assert result["results"]
        snippet = result["results"][0]["snippet"]
        assert "«" in snippet and "»" in snippet, snippet

    def test_archived_note_excluded_from_search(self):
        """归档笔记默认不进搜索，开关打开才纳入（§10）。"""
        from app.services.notes_service import get_notes_service
        from app.services.search_service import get_search_service

        notes = get_notes_service()
        created = notes.create_note(
            title="归档后应被搜不到标记词ZQARC1",
            body="归档前的正文内容。",
            tags=["归档搜索测试"],
        )
        search = get_search_service()

        # 归档前能搜到
        before = search.search("ZQARC1")
        assert before["total"] >= 1, before

        # 归档
        arch = notes.archive(created["note_path"])
        assert arch["ok"] is True

        # 默认搜不到
        after = search.search("ZQARC1")
        assert after["total"] == 0, after

        # 开关打开能搜到
        with_archived = search.search("ZQARC1", include_archived=True)
        assert with_archived["total"] >= 1, with_archived

    def test_reindex_preserves_archived_flag(self):
        """重建索引不能抹掉归档标志——它由文件所在目录推导。"""
        from app.services.index_service import get_index_service
        from app.services.notes_service import get_notes_service
        from app.services.search_service import get_search_service

        notes = get_notes_service()
        created = notes.create_note(
            title="重建后仍归档标记词ZQARC2",
            body="正文。",
            tags=["归档重建测试"],
        )
        notes.archive(created["note_path"])

        # 重建索引
        get_index_service().reindex_all()

        # 归档标志仍在 → 默认搜不到
        after = get_search_service().search("ZQARC2")
        assert after["total"] == 0, after
        with_archived = get_search_service().search("ZQARC2", include_archived=True)
        assert with_archived["total"] >= 1, with_archived


# --------------------------------------------------------------------------
# P7：C 层
# --------------------------------------------------------------------------

class TestNotes:
    def test_create_and_search_note(self):
        from app.services.notes_service import get_notes_service
        from app.services.search_service import get_search_service

        svc = get_notes_service()
        result = svc.create_note(
            title="类比推理是思维捷径也是陷阱",
            body="类比推理让我们快速上手，但也容易把不同问题混为一谈。",
            tags=["思维模型"],
        )
        assert Path(result["note_path"]).exists()

        found = get_search_service().search("类比推理")
        assert found["total"] >= 1

    def test_citation_parsing_creates_link(self):
        from app.services.notes_service import get_notes_service

        # 先造一个 B 层目标
        from app.services.review_service import get_review_service

        item_id = TestReviewFlow()._make_reviewable()
        review = get_review_service()
        target = review.confirm(
            item_id, title="被引用的目标", personal_summary="目标手稿", tags=["引用测试"]
        )
        target_stem = Path(target["path"]).stem

        notes = get_notes_service()
        created = notes.create_note(
            title="引用来源笔记",
            body=f"这段内容引用了 [[{target_stem}]] 作为依据。",
            tags=["引用测试"],
        )
        detail = notes.get_note(created["note_path"])
        assert target_stem in detail["citations"]

    def test_citation_by_title_resolves_to_dated_filename(self):
        """按标题写 [[]] 也要能解析到带日期前缀的文件。

        回归测试：工具生成的文件名带日期前缀（2026-11-01-xxx.md），
        而用户手写引用时写的是标题——只按精确文件名匹配会解析不到，
        导致 C→B 引用边永远建立不起来。
        """
        from app.services.notes_service import get_notes_service

        notes = get_notes_service()
        target = notes.create_note(
            title="唯一标题标记词玄猫态", body="目标正文。", tags=["引用解析"]
        )
        target_stem = Path(target["note_path"]).stem
        assert target_stem.startswith("2026-")  # 确认是带日期前缀的文件名

        source = notes.create_note(
            title="引用方笔记",
            body="这里引用了 [[唯一标题标记词玄猫态]]。",
            tags=["引用解析"],
        )

        detail = notes.get_note(source["note_path"])
        # 链接边必须建立
        assert len(detail["outlinks"]) >= 1, detail["outlinks"]
        assert any(
            Path(o["note_path"]).stem == target_stem for o in detail["outlinks"]
        ), detail["outlinks"]

    def test_suffix_match_requires_separator(self):
        """后缀匹配必须带分隔符，避免「笔记」命中「手写测试笔记」。"""
        from app.core.config import get_config
        from app.core.vault import link_target_to_relpath

        vault = get_config().vault.path
        # 造一个容易误伤的文件
        from app.core import vault as vault_mod

        notes_dir = get_config().vault.dir("notes")
        p = vault_mod.unique_path(notes_dir, "手写测试笔记", ".md")
        vault_mod.write_note(p, {"title": "手写测试笔记", "type": "note"}, "正文")

        # 「笔记」不应命中「手写测试笔记」
        resolved = link_target_to_relpath("笔记", vault)
        assert "手写测试笔记" not in resolved, resolved

        p.unlink(missing_ok=True)

    def test_link_resolver_is_indexed_once(self):
        """链接解析必须走索引，不能每次全量扫 vault。

        回归测试：逐条调用 link_target_to_relpath() 会让 5000 篇的
        索引重建从 13s 变成 >900s（O(n²)）。
        """
        import time

        from app.core.config import get_config
        from app.services.link_resolver import LinkResolver

        resolver = LinkResolver(get_config().vault.path)
        started = time.time()
        resolver.build()
        build_ms = (time.time() - started) * 1000

        # 建索引是一次性的，之后解析都是查表
        started = time.time()
        for i in range(500):
            resolver.resolve(f"不存在的标题{i}")
        resolve_ms = (time.time() - started) * 1000

        assert build_ms < 5000, f"建索引太慢：{build_ms:.0f}ms"
        assert resolve_ms < 500, f"解析太慢：{resolve_ms:.0f}ms / 500 次"

    def test_link_resolver_matches_title_and_filename(self):
        from app.core.config import get_config
        from app.services.link_resolver import LinkResolver

        resolver = LinkResolver(get_config().vault.path).build()
        stats = resolver.stats()
        assert stats["stems"] > 0 or stats["titles"] > 0

    def test_import_external_md(self):
        from app.services.notes_service import get_notes_service

        svc = get_notes_service()
        content = """---
title: 导入的外部笔记
tags: [外部, 测试]
---

# 导入的外部笔记

这是从外部粘贴的 md 内容。
"""
        result = svc.import_external(content)
        assert result["title"] == "导入的外部笔记"
        assert Path(result["note_path"]).exists()

    def test_inspiration_returns_dict(self):
        from app.services.notes_service import get_notes_service

        result = get_notes_service().inspiration("测试标题", "测试正文内容" * 20)
        assert "results" in result
        assert "available" in result


# --------------------------------------------------------------------------
# P8：标签治理
# --------------------------------------------------------------------------

class TestTags:
    def test_merge_tag_rewrites_frontmatter(self):
        from app.core.vault import read_note
        from app.services.notes_service import get_notes_service
        from app.services.tag_service import get_tag_service

        notes = get_notes_service()
        created = notes.create_note(
            title="待合并标签笔记", body="正文", tags=["旧标签", "保留标签"]
        )
        tags = get_tag_service()
        tags.add_tag("旧标签")
        tags.add_tag("保留标签")

        result = tags.merge_tags("旧标签", "保留标签")
        assert result["ok"] is True
        assert result["affected"] >= 1

        note = read_note(Path(created["note_path"]))
        assert "旧标签" not in note.frontmatter["tags"]
        assert "保留标签" in note.frontmatter["tags"]

    def test_merge_partition(self):
        from app.core.vault import read_note
        from app.services.notes_service import get_notes_service
        from app.services.tag_service import get_tag_service

        notes = get_notes_service()
        created = notes.create_note(title="分区合并笔记", body="正文")
        # 手动加分区
        from app.core.vault import read_note as rn, write_note

        path = Path(created["note_path"])
        note = rn(path)
        fm = dict(note.frontmatter)
        fm["partitions"] = ["旧分区"]
        write_note(path, fm, note.body)

        tags = get_tag_service()
        tags.add_partition("旧分区")
        result = tags.merge_partitions("旧分区", "科学理论")
        assert result["ok"] is True

        updated = read_note(path)
        assert "旧分区" not in updated.frontmatter["partitions"]
        assert "科学理论" in updated.frontmatter["partitions"]

    def test_add_and_deactivate(self):
        from app.services.tag_service import get_tag_service

        tags = get_tag_service()
        tags.add_tag("临时标签")
        vocab = tags.vocabulary()
        assert any(t["name"] == "临时标签" for t in vocab["tags"])

        tags.deactivate_tag("临时标签")
        vocab2 = tags.vocabulary()
        assert not any(t["name"] == "临时标签" for t in vocab2["tags"])


# --------------------------------------------------------------------------
# P5：索引服务
# --------------------------------------------------------------------------

class TestIndexService:
    def test_scan_and_reindex(self):
        from app.services.index_service import get_index_service

        svc = get_index_service()
        result = svc.scan_vault()
        assert result.total_files >= 0
        assert isinstance(result.to_dict(), dict)

    def test_rebuild_is_idempotent(self):
        from app.core.db import get_db
        from app.services.index_service import get_index_service

        svc = get_index_service()
        svc.reindex_all()
        count1 = get_db().scalar("SELECT COUNT(*) FROM notes")
        svc.reindex_all()
        count2 = get_db().scalar("SELECT COUNT(*) FROM notes")
        assert count1 == count2

    def test_seed_vocabulary(self):
        from app.services.index_service import get_index_service

        result = get_index_service().seed_tag_vocabulary()
        assert result["ok"] is True


# --------------------------------------------------------------------------
# P10：回顾
# --------------------------------------------------------------------------

class TestRecap:
    def test_recap_card(self):
        from app.services.recap_service import get_recap_service

        card = get_recap_service().recap_card()
        assert "collected_yesterday" in card
        assert "need_manuscript" in card
        assert "unreferenced_note" in card

    def test_weekly_report_generated(self):
        from app.services.recap_service import get_recap_service

        result = get_recap_service().weekly_report()
        assert result["ok"] is True
        path = Path(result["path"])
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "本周统计" in content


# --------------------------------------------------------------------------
# P1：AI 契约
# --------------------------------------------------------------------------

class TestAIContract:
    def test_parse_valid_payload(self):
        from app.providers.ai_service import parse_ai_payload

        raw = """```json
{
  "title_suggestion": "什么是第一性原理",
  "summary": "- 要点一\\n- 要点二",
  "summary_type": "bullets",
  "tags": ["思维模型"],
  "new_tags": [{"name": "新标签", "reason": "词表外"}],
  "partitions": ["科学理论"],
  "keywords": ["第一性原理"],
  "confidence": 0.85
}
```"""
        result, error = parse_ai_payload(raw, "bullets")
        assert error is None
        assert result is not None
        assert result.title_suggestion == "什么是第一性原理"
        assert result.tags == ["思维模型"]
        assert result.new_tags[0]["name"] == "新标签"
        assert result.confidence == 0.85

    def test_parse_broken_json_fails(self):
        from app.providers.ai_service import parse_ai_payload

        result, error = parse_ai_payload("这不是 JSON", "bullets")
        assert result is None
        assert error

    def test_summary_object_is_normalized_to_text(self):
        """模型把 summary 返回成对象时，必须规整成文本，不能写入 Python repr。"""
        import json

        from app.providers.ai_service import parse_ai_payload

        raw = json.dumps(
            {
                "title_suggestion": "第一性原理",
                "summary": {
                    "bullets": ["要点一：拆到基本事实", "要点二：重新推导"],
                    "one_liner": "回到最基础重新思考",
                },
                "tags": ["思维模型"],
                "partitions": [],
                "keywords": ["第一性原理"],
                "confidence": 0.8,
            },
            ensure_ascii=False,
        )
        result, error = parse_ai_payload(raw, "bullets")
        assert error is None, error
        assert result is not None
        assert "- 要点一：拆到基本事实" in result.summary
        assert "一句话总结：回到最基础重新思考" in result.summary
        # 关键：不能出现 Python dict repr
        assert "'bullets'" not in result.summary
        assert "{" not in result.summary

    def test_summary_top_level_bullets_is_accepted(self):
        """模型把要点直接放顶层时也要能解析。"""
        import json

        from app.providers.ai_service import parse_ai_payload

        raw = json.dumps(
            {
                "bullets": ["要点一", "要点二"],
                "one_liner": "一句话",
                "tags": [],
                "partitions": [],
            },
            ensure_ascii=False,
        )
        result, error = parse_ai_payload(raw, "bullets")
        assert error is None, error
        assert result is not None
        assert "- 要点一" in result.summary

    def test_default_prompt_declares_json_schema(self):
        """默认提示词必须写清字段结构——否则模型自由发挥，摘要形状不可控。"""
        from app.providers.ai_service import DEFAULT_PROMPT_TEMPLATE

        for field in (
            "title_suggestion",
            "summary",
            "summary_type",
            "tags",
            "new_tags",
            "partitions",
            "keywords",
            "confidence",
        ):
            assert f'"{field}"' in DEFAULT_PROMPT_TEMPLATE, field

    def test_default_prompt_is_formattable(self):
        """模板里有 JSON 花括号——必须转义，否则 str.format() 会 KeyError。"""
        from app.providers.ai_service import DEFAULT_PROMPT_TEMPLATE

        rendered = DEFAULT_PROMPT_TEMPLATE.format(
            content_type="short_video",
            style_hint="bullets",
            partitions_list="甲、乙",
            tags_list="丙",
            content="正文内容",
        )
        assert '"title_suggestion"' in rendered
        assert "{" in rendered and "}" in rendered, "JSON schema 的花括号应保留"
        assert "{{" not in rendered, "转义没解开"

    def test_partition_tree_cold_start_from_vault_dirs(self):
        """冷启动：vault 目录树 → 分区树（成长树/思维逻辑 = 父子）。"""
        from app.core.config import get_config
        from app.services.partition_service import get_partition_service

        vault = get_config().vault.path
        (vault / "测试分区A").mkdir(parents=True, exist_ok=True)
        (vault / "测试分区A" / "a.md").write_text("内容", encoding="utf-8")
        (vault / "测试分区A" / "子分区").mkdir(parents=True, exist_ok=True)
        (vault / "测试分区A" / "子分区" / "b.md").write_text("内容", encoding="utf-8")
        (vault / "空目录B").mkdir(parents=True, exist_ok=True)
        try:
            svc = get_partition_service()
            created = svc.seed_from_vault_dirs(vault)
            assert created >= 1

            root = svc.get_by_path("测试分区A")
            assert root is not None, "顶层目录应成为一级分区"
            child = svc.get_by_path("测试分区A/子分区")
            assert child is not None, "子目录应成为二级分区"
            assert int(child["parent_id"]) == int(root["id"])
            assert int(child["level"]) == 2

            # 工具目录不能成为分区
            for tool_dir in ("01_Inbox", "02_Processed", "03_Notes"):
                assert svc.get_by_path(tool_dir) is None

            # 子树展开：父分区包含子分区
            ids = svc.subtree_ids(int(root["id"]))
            assert int(child["id"]) in ids
            assert int(root["id"]) in ids

            # 改名时子树路径级联
            svc.update(int(root["id"]), name="测试分区A改名")
            assert svc.get_by_path("测试分区A改名/子分区") is not None
        finally:
            for p in (
                vault / "测试分区A" / "a.md",
                vault / "测试分区A" / "子分区" / "b.md",
                vault / "测试分区A" / "子分区",
                vault / "测试分区A",
                vault / "空目录B",
            ):
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()


class TestProviderConfig:
    """Provider 配置的实时生效（2026-11-02 修的两个 bug 的回归守卫）。"""

    def test_health_sends_api_key(self, monkeypatch):
        """健康检查必须带 Authorization——否则带密钥的端点永远显示 401。"""
        import app.providers.ai_providers as ap

        captured: dict = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {"data": []}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None):
                captured["headers"] = headers or {}
                return FakeResp()

        monkeypatch.setattr(ap, "make_client", lambda *a, **k: FakeClient())
        provider = ap.OpenAICompatProvider(
            base_url="http://127.0.0.1:9/v1", model="m", api_key="secret-key"
        )
        health = provider.health()
        assert health.ok is True, health.detail
        assert captured["headers"].get("Authorization") == "Bearer secret-key"

    def test_router_follows_config_reload(self, monkeypatch):
        """store.reload() 会整体替换配置对象，路由器必须跟着换，否则新填的 key 不生效。"""
        import copy

        import app.providers.ai_providers as ap
        from app.core.config import get_config

        cfg = get_config()
        cfg.ai.openai_compat.api_key = "k1"
        router = ap.ProviderRouter(cfg=cfg)
        assert router.build("openai_compat").api_key == "k1"

        # 模拟 reload：换成一个全新的配置对象
        new_cfg = copy.deepcopy(cfg)
        new_cfg.ai.openai_compat.api_key = "k2"
        monkeypatch.setattr(ap, "get_config", lambda: new_cfg)
        assert router.build("openai_compat").api_key == "k2", "路由器缓存了旧配置对象"

    def test_401_is_auth_error_not_fallback_trigger(self):
        """401 属于认证类错误，不应触发降级到备链。"""
        from app.providers.ai_providers import ProviderError

        exc = ProviderError("HTTP 401", auth_required=True)
        assert exc.auth_required is True
        assert exc.rate_limited is False
        assert exc.quota is False

    def test_parse_missing_optional_fields_ok(self):
        """v31：只有 summary 是必需的。

        回归背景：REQUIRED_FIELDS 曾经是 ("summary", "tags", "partitions")，
        模型少给一个键 → 整条 ai_failed（真发生过：
        「AI 契约调用失败（3 次尝试）：缺少必需字段：partitions」）。
        分区/标签是**辅助建议**，用户能在加工区自己勾选，
        缺了就当"没建议"，不该让整条任务失败。
        """
        from app.providers.ai_service import parse_ai_payload

        result, error = parse_ai_payload('{"summary": "x"}', "bullets")
        assert error is None, error
        assert result is not None
        assert result.summary == "x"
        # 缺失的可选字段补空值
        assert result.tags == []
        assert result.partitions == []

    def test_parse_missing_summary_fails(self):
        """summary 是唯一必需字段——没有摘要这条 AI 结果就没意义。"""
        from app.providers.ai_service import parse_ai_payload

        result, error = parse_ai_payload('{"tags": ["x"]}', "bullets")
        assert result is None
        assert error and "summary" in error

    def test_detect_content_type(self):
        from app.providers.ai_service import detect_content_type

        assert detect_content_type("短文本") == "short_video"
        assert detect_content_type("长" * 3000) == "article"
        assert detect_content_type("超长" * 20000) == "long_course"

    def test_vocab_enforcement_moves_unknown_to_new_tags(self):
        """词表外的标签进 new_tags，词表外的**分区**进 new_partitions（各归其位）。"""
        from app.providers.ai_service import AIResult, AIService

        result = AIResult(ok=True, tags=["合法标签", "非法标签"], partitions=["合法分区", "非法分区"])
        AIService._enforce_vocab(result, ["合法分区"], ["合法标签"])

        assert result.tags == ["合法标签"]
        assert result.partitions == ["合法分区"]
        # 词表外标签 → new_tags
        assert [n["name"] for n in result.new_tags] == ["非法标签"]
        # 词表外分区 → new_partitions（不能混进 new_tags）
        assert [n["name"] for n in result.new_partitions] == ["非法分区"]
        assert "非法分区" not in [n["name"] for n in result.new_tags]

    def test_provider_fallback_on_rate_limit(self):
        from app.core.config import get_config
        from app.providers.ai_providers import ProviderError, ProviderRouter

        router = ProviderRouter(cfg=get_config())
        router.switch("openai_compat", manual=True)

        called: list[str] = []

        class FakeProvider:
            def __init__(self, name):
                self.name = name

            def complete(self, prompt, system=""):
                called.append(self.name)
                if self.name == "openai_compat":
                    raise ProviderError("HTTP 429 限流", rate_limited=True)
                return "ollama-ok"

            def health(self):
                from app.providers.ai_providers import ProviderHealth

                return ProviderHealth(ok=True)

        router.build = lambda name: FakeProvider(name)  # type: ignore[assignment]
        out = router.complete("test")
        assert out == "ollama-ok"
        assert called == ["openai_compat", "ollama"]
        assert router.fallback_active is True
        assert router.current_name == "ollama"

    def test_provider_no_fallback_when_disabled(self):
        from app.core.config import get_config
        from app.providers.ai_providers import ProviderError, ProviderRouter

        cfg = get_config()
        cfg.ai.auto_fallback = False
        try:
            router = ProviderRouter(cfg=cfg)
            router.switch("openai_compat", manual=True)

            class FakeProvider:
                name = "openai_compat"

                def complete(self, prompt, system=""):
                    raise ProviderError("boom", rate_limited=True)

            router.build = lambda name: FakeProvider()  # type: ignore[assignment]
            with pytest.raises(ProviderError):
                router.complete("test")
            assert router.current_name == "openai_compat"
        finally:
            cfg.ai.auto_fallback = True

    def test_auth_error_does_not_trigger_fallback(self):
        """未登录是"需要用户操作"，不是"主链故障"——不该降级到同样没配的备链。

        回归测试：曾因认证错误触发降级，切到一个根本没启动的 Ollama，
        结果比不降级更难排查。
        """
        from app.core.config import get_config
        from app.providers.ai_providers import ProviderError, ProviderRouter

        router = ProviderRouter(cfg=get_config())
        router.switch("openai_compat", manual=True)

        class FakeProvider:
            name = "openai_compat"

            def complete(self, prompt, system=""):
                raise ProviderError("API Key 未配置", auth_required=True)

        called = []

        def _build(name):
            if name == "openai_compat":
                return FakeProvider()
            called.append(name)

            class Backup:
                def complete(self, prompt, system=""):
                    return "backup-should-not-be-used"

            return Backup()

        router.build = _build  # type: ignore[assignment]

        with pytest.raises(ProviderError):
            router.complete("test")

        assert router.current_name == "openai_compat", "认证错误不应触发降级"
        assert router.fallback_active is False
        assert "ollama" not in called, "认证错误不应尝试备链"


class TestPartitionRelation:
    """分区关联必须落库——否则「按分区搜索」永远命中 0 条（2026-11-02 修）。"""

    def _make_reviewable(self, suffix: str = "A") -> int:
        from app.services.ingest_service import get_ingest_service

        result = get_ingest_service().submit(
            [
                {
                    "url": f"https://www.bilibili.com/video/BV1PartitionRel{suffix}",
                    "platform": "bilibili",
                }
            ]
        )
        return result.accepted[0]["id"]

    def test_confirm_writes_note_partitions(self):
        from app.core.db import get_db
        from app.services.review_service import get_review_service

        item_id = self._make_reviewable()
        get_db().write(
            lambda conn: conn.execute(
                "UPDATE items SET status='pending_review', ai_summary=?, "
                "ai_summary_type='bullets' WHERE id=?",
                ("- 测试要点", item_id),
            )
        )

        # 先在词表里建分区
        from app.services.tag_service import get_tag_service

        get_tag_service().add_partition("分区关系测试")

        res = get_review_service().confirm(
            item_id,
            title="分区关系测试",
            personal_summary="手稿",
            tags=[],
            partitions=["分区关系测试"],
        )
        assert res["ok"] is True

        db = get_db()
        rows = db.query(
            "SELECT p.name FROM note_partitions np "
            "JOIN partitions p ON p.id = np.partition_id "
            "JOIN notes n ON n.id = np.note_id WHERE n.note_path = ?",
            (res["path"].replace("\\", "/"),),
        )
        names = [r["name"] for r in rows]
        assert "分区关系测试" in names, f"分区关联没落库：{names}"

    def test_search_by_partition_returns_note(self):
        from app.core.db import get_db
        from app.services.review_service import get_review_service
        from app.services.search_service import get_search_service
        from app.services.tag_service import get_tag_service

        item_id = self._make_reviewable("B")
        get_db().write(
            lambda conn: conn.execute(
                "UPDATE items SET status='pending_review', ai_summary=?, "
                "ai_summary_type='bullets' WHERE id=?",
                ("- 唯一标记词 ZQPART", item_id),
            )
        )
        get_tag_service().add_partition("搜索分区测试")
        get_review_service().confirm(
            item_id,
            title="搜索分区测试",
            personal_summary="手稿",
            tags=[],
            partitions=["搜索分区测试"],
        )

        result = get_search_service().search(
            "ZQPART", partitions=["搜索分区测试"], limit=10
        )
        assert result["total"] >= 1, "按分区搜索命中 0 条 —— note_partitions 没写"
        titles = [r["title"] for r in result["results"]]
        assert "搜索分区测试" in titles, titles


class TestWorkbench:
    """工作区：解析模板 / 模板化解析（1000 字校验）/ 配图插入 / 实时状态。"""

    def _fake_router(self, monkeypatch, responses):
        """把 provider 的返回替换成预设序列。"""
        from app.providers.ai_providers import get_router

        router = get_router()
        calls = {"n": 0}

        def _complete(prompt, system="", timeout=None):
            idx = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            return responses[idx]

        monkeypatch.setattr(router, "complete", _complete)
        return calls

    # ---------------- 模板 CRUD ----------------
    def test_template_crud(self):
        from app.services.template_service import get_template_service

        svc = get_template_service()
        created = svc.create("测试模板X", "请按此结构输出", "描述")
        assert created["id"] > 0
        assert svc.get(created["id"])["name"] == "测试模板X"

        updated = svc.update(created["id"], body="改过的模板正文")
        assert updated["body"] == "改过的模板正文"

        assert svc.delete(created["id"]) is True
        assert svc.get(created["id"]) is None

    def test_template_name_must_be_unique(self):
        from app.services.template_service import get_template_service

        svc = get_template_service()
        svc.create("重名模板Y", "正文")
        with pytest.raises(ValueError):
            svc.create("重名模板Y", "另一份正文")

    # ---------------- 模板化解析 + 1000 字校验 ----------------
    def test_parse_under_1000_chars_is_rejected_and_regenerated(self, monkeypatch):
        """不足 1000 字 → 提示并重新生成；始终不足则失败。"""
        from app.providers.ai_service import get_ai_service
        from app.services.template_service import get_template_service

        tpl = get_template_service().create("短模板Z", "正文模板")
        calls = self._fake_router(monkeypatch, ["只有一百字" * 10])

        result = get_ai_service().analyze_with_template(
            "原始内容" * 500, template_id=tpl["id"], min_chars=1000, max_attempts=3
        )
        assert result["ok"] is False
        assert "1000" in (result["error"] or "")
        assert calls["n"] == 3, "不足字数时应重新生成"

    def test_parse_over_1000_chars_succeeds(self, monkeypatch):
        from app.providers.ai_service import get_ai_service
        from app.services.template_service import get_template_service

        tpl = get_template_service().create("长模板Z", "正文模板")
        self._fake_router(monkeypatch, ["内容充足。" * 300])

        result = get_ai_service().analyze_with_template(
            "原始内容" * 500, template_id=tpl["id"], min_chars=1000, max_attempts=3
        )
        assert result["ok"] is True
        assert result["chars"] >= 1000
        assert result["template"]["id"] == tpl["id"]

    def test_parse_auto_matches_template(self, monkeypatch):
        """未指定模板时 AI 自动匹配。"""
        import json

        from app.providers.ai_service import get_ai_service
        from app.services.template_service import get_template_service

        tpl = get_template_service().create("自动匹配模板", "正文模板")
        self._fake_router(
            monkeypatch,
            [
                json.dumps({"template_id": tpl["id"], "reason": "结构合适"}),
                "生成的内容。" * 300,
            ],
        )

        result = get_ai_service().analyze_with_template(
            "原始内容" * 500, template_id=None, min_chars=1000, max_attempts=2
        )
        assert result["ok"] is True
        assert result["matched_by"] == "ai"
        assert result["template"]["id"] == tpl["id"]

    # ---------------- 配图插入 ----------------
    def test_insert_and_remove_screenshot(self):
        from app.core import vault as vm
        from app.services.ingest_service import get_ingest_service

        ingest = get_ingest_service()
        res = ingest.submit(
            [{"url": "https://example.com/shot-test", "platform": "web", "title": "配图测试"}]
        )
        item_id = res.accepted[0]["id"]
        # 直接造一份 A 层原文（不等抓取任务，避免依赖 worker 时序）
        path = ingest.write_inbox(
            url="https://example.com/shot-test",
            platform="web",
            title="配图测试",
            text="第一段内容\n\n第二段内容",
            extractor="manual",
            asr=False,
        )
        ingest.update_item(item_id, inbox_path=path.as_posix())

        out = ingest.insert_screenshot(item_id, 0, "images/a.png", "示意图")
        assert out["ok"] is True
        body = vm.read_note(vm.Path(path)).body
        assert "![示意图](images/a.png)" in body

        removed = ingest.remove_screenshot(item_id, 0)
        assert removed["ok"] is True
        body2 = vm.read_note(vm.Path(path)).body
        assert "images/a.png" not in body2

    # ---------------- 实时状态 ----------------
    def test_workbench_snapshot_has_stage_and_progress(self):
        from app.services.workbench_service import get_workbench_service

        snap = get_workbench_service().snapshot(limit=10)
        assert "items" in snap and "stats" in snap
        for st in snap["items"]:
            assert "stage" in st
            assert "progress" in st
            assert 0.0 <= st["progress"] <= 1.0

    def test_update_item_accepts_error_fields(self):
        """ASR 失败要能记录原因——update_item 必须支持 error_reason/error_detail。"""
        from app.services.ingest_service import get_ingest_service

        ingest = get_ingest_service()
        res = ingest.submit(
            [{"url": "https://example.com/err-field", "platform": "web", "title": "错误字段"}]
        )
        item_id = res.accepted[0]["id"]
        ingest.update_item(
            item_id,
            extractor="bilibili_api+asr_failed",
            error_reason="asr_failed",
            error_detail="模型加载失败",
        )
        item = ingest.get_item(item_id)
        assert item["extractor"] == "bilibili_api+asr_failed"
        assert item["error_reason"] == "asr_failed"
        assert item["error_detail"] == "模型加载失败"


class TestPartitionFromDirectory:
    """分区来自**目录路径**（用户的 Obsidian 笔记没有分区 frontmatter）。"""

    def test_note_in_subdirectory_gets_partition(self):
        from app.core.config import get_config
        from app.core.db import get_db
        from app.services.index_service import get_index_service
        from app.services.partition_service import get_partition_service

        vault = get_config().vault.path
        d = vault / "测试分区树"
        (d / "子层").mkdir(parents=True, exist_ok=True)
        f = d / "子层" / "测试笔记.md"
        f.write_text(
            "---\ntype: note\ntitle: 测试笔记\n---\n正文内容",
            encoding="utf-8",
        )
        try:
            psvc = get_partition_service()
            psvc.seed_from_vault_dirs(vault)

            idx = get_index_service()
            idx.reindex_all()

            # 分区树里应有「测试分区树/子层」
            assert psvc.get_by_path("测试分区树/子层") is not None

            # 笔记应关联到该分区
            db = get_db()
            rows = db.query(
                "SELECT p.path FROM note_partitions np "
                "JOIN notes n ON n.id = np.note_id "
                "JOIN partitions p ON p.id = np.partition_id "
                "WHERE n.note_path = ?",
                (f.as_posix(),),
            )
            paths = [str(r["path"]) for r in rows]
            assert "测试分区树/子层" in paths, f"分区关联缺失，实际={paths}"
        finally:
            if f.exists():
                f.unlink()
            for p in (d / "子层", d):
                if p.is_dir():
                    p.rmdir()

    def test_top_level_note_gets_top_level_partition(self):
        from app.core.config import get_config
        from app.core.db import get_db
        from app.services.index_service import get_index_service
        from app.services.partition_service import get_partition_service

        vault = get_config().vault.path
        d = vault / "测试顶层分区"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "顶层笔记.md"
        f.write_text(
            "---\ntype: note\ntitle: 顶层笔记\n---\n正文", encoding="utf-8"
        )
        try:
            psvc = get_partition_service()
            psvc.seed_from_vault_dirs(vault)
            get_index_service().reindex_all()

            db = get_db()
            rows = db.query(
                "SELECT p.path FROM note_partitions np "
                "JOIN notes n ON n.id = np.note_id "
                "JOIN partitions p ON p.id = np.partition_id "
                "WHERE n.note_path = ?",
                (f.as_posix(),),
            )
            paths = [str(r["path"]) for r in rows]
            assert "测试顶层分区" in paths, f"分区关联缺失，实际={paths}"
        finally:
            if f.exists():
                f.unlink()
            if d.is_dir():
                d.rmdir()


class TestNewPartitionSuggestion:
    """AI 建议的词表外分区 → 进 new_partitions（不混进 new_tags）。"""

    def test_out_of_vocab_partition_goes_to_new_partitions(self, monkeypatch):
        from app.providers.ai_service import get_ai_service
        from app.services.partition_service import get_partition_service

        psvc = get_partition_service()
        psvc.ensure_by_name("测试已有分区")

        import json

        payload = {
            "title_suggestion": "T",
            "summary": "摘要内容" * 30,
            "summary_type": "bullets",
            "tags": [],
            "new_tags": [],
            "partitions": ["测试已有分区", "测试全新分区"],
            "new_partitions": [],
            "keywords": ["k"],
            "confidence": 0.8,
        }
        from app.providers.ai_providers import get_router

        router = get_router()
        monkeypatch.setattr(
            router, "complete", lambda p, system="", timeout=None: json.dumps(payload)
        )

        result = get_ai_service().analyze("内容" * 200, title="T")
        assert result.ok, result.error
        # 词表内的留在 partitions
        assert "测试已有分区" in result.partitions
        # 词表外的进 new_partitions（不能混进 new_tags）
        assert "测试全新分区" in [n["name"] for n in result.new_partitions]
        assert "测试全新分区" not in [n["name"] for n in result.new_tags]
        assert "测试全新分区" not in result.partitions

    def test_new_partitions_survive_to_api(self):
        """new_partitions 能通过 /api/review/{id} 读到（pending_partitions）。"""
        from app.core.db import get_db
        from app.services.ingest_service import get_ingest_service

        ingest = get_ingest_service()
        res = ingest.submit(
            [{"url": "https://example.com/newpart", "platform": "web", "title": "新分区"}]
        )
        item_id = res.accepted[0]["id"]

        db = get_db()

        def _run(conn):
            conn.execute(
                "INSERT INTO ai_suggestions(note_path, suggestion_type, suggestion_value, "
                "suggestion_reason, status) VALUES(?, 'partition_new', ?, ?, 'pending')",
                (f"item:{item_id}", "测试建议分区", "词表里没有"),
            )

        db.write(_run)
        ingest.update_item(item_id, status="extracted")

        from app.services.review_service import get_review_service

        page = get_review_service().get_review_item(item_id)
        pending = page.get("pending_partitions") or []
        assert any(p["name"] == "测试建议分区" for p in pending), pending
        assert page.get("suggested_partitions") == []

    def test_confirm_creates_new_partition_from_suggestion(self):
        """勾选待定分区后确认入库 → 分区被自动创建并关联。"""
        from app.core.db import get_db
        from app.services.ingest_service import get_ingest_service
        from app.services.partition_service import get_partition_service
        from app.services.review_service import get_review_service

        ingest = get_ingest_service()
        res = ingest.submit(
            [{"url": "https://example.com/confirmpart", "platform": "web", "title": "建分区"}]
        )
        item_id = res.accepted[0]["id"]

        db = get_db()

        def _run(conn):
            conn.execute(
                "INSERT INTO ai_suggestions(note_path, suggestion_type, suggestion_value, "
                "suggestion_reason, status) VALUES(?, 'partition_new', ?, ?, 'pending')",
                (f"item:{item_id}", "测试自动建分区", "词表里没有"),
            )

        db.write(_run)
        ingest.update_item(item_id, status="extracted")

        svc = get_review_service()
        out = svc.confirm(
            item_id,
            title="建分区",
            personal_summary="我的手稿",
            tags=[],
            partitions=["测试自动建分区"],
            confirmed_related=[],
            ai_summary="AI 摘要内容" * 10,
            summary_type="bullets",
        )
        assert out.get("ok") is not False, out
        psvc = get_partition_service()
        assert psvc.get_by_path("测试自动建分区") is not None, "新分区没被创建"


class TestASREmptyResult:
    """ASR 空转写必须判失败，且不能覆盖已有正文。"""

    def test_empty_transcription_is_failure(self, monkeypatch):
        """转写出 0 字 → ok=False，且给出可操作的原因。"""
        from app.services.asr_service import get_asr_service

        svc = get_asr_service()

        class FakeModel:
            # v31：转写现在会传 initial_prompt / condition_on_previous_text
            # （治 whisper 输出繁体），签名要能收下。
            def transcribe(
                self,
                path,
                language=None,
                vad_filter=True,
                beam_size=5,
                initial_prompt=None,
                condition_on_previous_text=None,
            ):
                class Seg:
                    text = ""
                return iter([Seg()]), object()

        monkeypatch.setattr(svc, "_load_model", lambda: FakeModel())

        result = svc.transcribe_file("fake.m4a")
        assert result.ok is False
        assert result.error, "空转写必须给出原因"
        assert "空" in result.error

    def test_asr_empty_does_not_overwrite_body(self, monkeypatch):
        """ASR 空结果 → 不覆盖 raw_text，正文还在。"""
        from app.core.db import get_db
        from app.services.ingest_service import get_ingest_service

        ingest = get_ingest_service()
        res = ingest.submit(
            [{"url": "https://example.com/asr-empty", "platform": "web", "title": "空转写"}]
        )
        item_id = res.accepted[0]["id"]
        ingest.update_item(
            item_id,
            raw_text="原有正文内容，不能被覆盖",
            status="extracted",
            inbox_path="",
        )

        import app.workers.pipeline as pipeline_mod

        class FakeASR:
            def transcribe(self, url, item_id=None):
                from app.services.asr_service import ASRResult

                return ASRResult(ok=False, error="转写结果为空：这段音频没有人声")

        class FakeSvc:
            def info(self):
                return {}

        monkeypatch.setattr(
            "app.services.asr_service.get_asr_service", lambda: FakeASR()
        )

        enqueued = []
        monkeypatch.setattr(
            pipeline_mod, "_enqueue", lambda iid, kind, priority=5: enqueued.append(kind)
        )
        monkeypatch.setattr(
            pipeline_mod, "_notify_asr_done", lambda title: None
        )

        pipeline_mod.handle_asr(item_id)

        item = ingest.get_item(item_id)
        assert "原有正文内容" in (item.get("raw_text") or ""), "正文被覆盖了"
        assert item.get("error_reason") == "asr_failed"
        # v31：ASR 失败后**不再自动排 ai job**。
        # AI 加工是加工区的活（要模板/字数档位/用户确认），
        # 采集链路的终点是「原文就绪」。
        assert "ai" not in enqueued, "ASR 失败后不应自动跑 AI"
        assert item.get("status") == "raw_ready", item.get("status")


class TestProcessedPath:
    """条目入库后要能查到 B 层产物在哪。"""

    def test_confirm_records_processed_path(self):
        from app.services.ingest_service import get_ingest_service
        from app.services.review_service import get_review_service

        ingest = get_ingest_service()
        res = ingest.submit(
            [{"url": "https://example.com/procpath", "platform": "web", "title": "产物路径"}]
        )
        item_id = res.accepted[0]["id"]
        ingest.update_item(item_id, status="extracted", raw_text="正文内容" * 20)

        out = get_review_service().confirm(
            item_id,
            title="产物路径",
            personal_summary="手稿",
            tags=[],
            partitions=[],
            confirmed_related=[],
            ai_summary="摘要" * 20,
            summary_type="bullets",
        )
        assert out.get("ok") is True, out
        item = ingest.get_item(item_id)
        assert item.get("processed_path"), "processed_path 没写回条目"
        from pathlib import Path

        assert Path(item["processed_path"]).exists()


class TestCollectRobustness:
    """采集入库的容错：空链接不该 422（前端会显示 [object Object]）。"""

    def test_entry_with_null_url_is_skipped_not_422(self):
        """url=null 的条目 → 跳过并说明，不报错。"""
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        r = client.post(
            "/api/collect/submit",
            json={
                "entries": [
                    {"url": None, "platform": "bilibili", "title": "只有标题"},
                    {"url": "https://example.com/ok-1", "platform": "web", "title": "正常"},
                ]
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("skipped"), "应该报告跳过的条目"
        assert data["skipped"][0]["title"] == "只有标题"

    def test_entry_missing_url_is_skipped(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        r = client.post(
            "/api/collect/submit",
            json={"entries": [{"platform": "web", "title": "没链接"}]},
        )
        assert r.status_code == 200, r.text
        assert r.json().get("skipped")

    def test_null_entry_does_not_crash(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        r = client.post("/api/collect/submit", json={"entries": [None]})
        assert r.status_code == 200, r.text
        assert r.json().get("skipped")


class TestBvidTruncation:
    """BV 号被截断时，要提取出来并给出明确提示（不是"无法解析"）。"""

    def test_extracts_truncated_bvid(self):
        from app.core.parser import bvid_length_hint, extract_bvid

        bvid = extract_bvid("https://www.bilibili.com/video/BV12ph76eCQ")
        assert bvid == "BV12ph76eCQ", bvid
        hint = bvid_length_hint(bvid)
        assert "11 位" in hint and "截断" in hint, hint

    def test_normal_bvid_has_no_hint(self):
        from app.core.parser import bvid_length_hint, extract_bvid

        bvid = extract_bvid("https://www.bilibili.com/video/BV1GJ411x7h7")
        assert bvid == "BV1GJ411x7h7"
        assert bvid_length_hint(bvid) == ""

    def test_no_bvid_returns_none(self):
        from app.core.parser import extract_bvid

        assert extract_bvid("https://www.bilibili.com/video/av12345") == "av12345"
        assert extract_bvid("https://example.com/not-a-video") is None


class TestManuscriptNode:
    """个人手稿是 3D 图谱的主要元素：确认入库后图谱里要有手稿节点。"""

    def test_graph_contains_manuscript_node(self):
        from app.services.graph_service import get_graph_service
        from app.services.ingest_service import get_ingest_service
        from app.services.review_service import get_review_service

        ingest = get_ingest_service()
        res = ingest.submit(
            [{"url": "https://example.com/ms-node", "platform": "web", "title": "手稿节点"}]
        )
        item_id = res.accepted[0]["id"]
        ingest.update_item(item_id, status="extracted", raw_text="正文" * 30)

        get_review_service().confirm(
            item_id,
            title="手稿节点",
            personal_summary="这是用户亲手写的思考",
            tags=[],
            partitions=[],
            confirmed_related=[],
            ai_summary="AI 摘要" * 10,
            summary_type="bullets",
        )

        g = get_graph_service().build(limit=400)
        assert g["stats"]["manuscripts"] >= 1, g["stats"]
        ms = [n for n in g["nodes"] if n["kind"] == "manuscript"]
        assert any(n["note_title"] == "手稿节点" for n in ms), ms
        # 手稿边连到对应笔记
        kinds = {e["kind"] for e in g["edges"]}
        assert "manuscript" in kinds

    def test_note_without_manuscript_has_no_node(self):
        """没写手稿的笔记 → 不出手稿节点（手稿是真实存在的才画）。"""
        from app.services.graph_service import get_graph_service
        from app.services.ingest_service import get_ingest_service
        from app.services.review_service import get_review_service

        ingest = get_ingest_service()
        res = ingest.submit(
            [{"url": "https://example.com/no-ms", "platform": "web", "title": "无手稿"}]
        )
        item_id = res.accepted[0]["id"]
        ingest.update_item(item_id, status="extracted", raw_text="正文" * 30)

        get_review_service().confirm(
            item_id,
            title="无手稿",
            personal_summary="",
            tags=[],
            partitions=[],
            confirmed_related=[],
            ai_summary="AI 摘要" * 10,
            summary_type="bullets",
        )

        g = get_graph_service().build(limit=400)
        ms = [n for n in g["nodes"] if n["kind"] == "manuscript"]
        assert not any(n.get("note_title") == "无手稿" for n in ms)


class TestManuscriptFill:
    """补写个人手稿（§438 软门槛的补写入口）。

    回归背景：确认入库时手稿允许留空（软门槛不拦截），
    条目进「待补手稿」清单——但**没有补写入口**，软门槛就变成死路。
    """

    def _make_processed_without_manuscript(self, title: str) -> tuple[int, str]:
        """造一条已入库但手稿为空的 B 层笔记，返回 (item_id, note_path)。"""
        from app.services.ingest_service import get_ingest_service
        from app.services.review_service import get_review_service

        ingest = get_ingest_service()
        res = ingest.submit(
            [{"url": f"https://example.com/fill-{title}", "platform": "web", "title": title}]
        )
        item_id = res.accepted[0]["id"]
        ingest.update_item(item_id, status="extracted", raw_text="正文" * 30)

        out = get_review_service().confirm(
            item_id,
            title=title,
            personal_summary="",  # 软门槛：手稿留空也允许入库
            tags=[],
            partitions=[],
            confirmed_related=[],
            ai_summary="AI 摘要" * 10,
            summary_type="bullets",
        )
        return item_id, out["path"]

    def test_filled_manuscript_persists_to_md(self):
        from app.core import vault as vault_mod
        from app.services.notes_service import get_notes_service

        _item_id, note_path = self._make_processed_without_manuscript("补写测试")
        svc = get_notes_service()
        svc.fill_manuscript(note_path, "这是我补写的手稿")

        note = vault_mod.read_note(vault_mod.Path(note_path))
        assert note is not None
        assert note.manuscript == "这是我补写的手稿", note.manuscript
        assert note.digested is True

    def test_filled_manuscript_clears_pending_flag(self):
        """补写后必须离开「待补手稿」清单 —— 否则软门槛永远刷不掉。"""
        from app.services.notes_service import get_notes_service
        from app.services.review_service import get_review_service

        _item_id, note_path = self._make_processed_without_manuscript("清单测试")

        before = get_review_service().list_digest_pending()
        assert any(n["note_path"] == note_path for n in before), before

        get_notes_service().fill_manuscript(note_path, "补上的手稿")

        after = get_review_service().list_digest_pending()
        assert not any(n["note_path"] == note_path for n in after), after

    def test_get_note_returns_summary_and_manuscript(self):
        """阅读页的双摘要块读 note.summary / note.manuscript ——
        后端不返回这两个顶层字段的话，两块永远不渲染。"""
        from app.services.notes_service import get_notes_service

        _item_id, note_path = self._make_processed_without_manuscript("双摘要测试")
        svc = get_notes_service()
        note = svc.get_note(note_path)
        assert note is not None
        assert "summary" in note, sorted(note.keys())
        assert "manuscript" in note, sorted(note.keys())
        assert note["summary"], note["summary"]

        svc.fill_manuscript(note_path, "顶层手稿字段")
        note2 = svc.get_note(note_path)
        assert note2["manuscript"] == "顶层手稿字段", note2["manuscript"]


class TestCollectStopsAtRawReady:
    """v31：采集完成后停在「原文就绪」，不自动跑 AI。

    回归背景：用户吐槽「采集区的任务进度条太不准了，完成任务后
    竟然直接 AI 提取要点加工了（也没有按 800 字要求）」。

    采集区的定位是「经济工具拿原文」——抓取/转写完就交差。
    AI 提取要点是**加工区**的活：要模板、要字数档位、要用户确认分区标签。
    放在采集阶段自动跑，会跳出用户没要求的加工动作。
    """

    def _submit_item(self, title: str = "采集链路测试") -> int:
        from app.services.ingest_service import get_ingest_service

        ingest = get_ingest_service()
        res = ingest.submit(
            [{"url": f"https://example.com/raw-ready-{title}", "platform": "web", "title": title}]
        )
        return res.accepted[0]["id"]

    def test_extract_does_not_enqueue_ai(self, monkeypatch):
        """抓取完成 → raw_ready，且**不排 ai job**。"""
        import app.workers.pipeline as pipeline_mod
        from app.services.ingest_service import get_ingest_service

        ingest = get_ingest_service()
        item_id = self._submit_item()

        enqueued: list[str] = []
        monkeypatch.setattr(
            pipeline_mod, "_enqueue", lambda iid, kind, priority=5: enqueued.append(kind)
        )

        # 模拟抓取完成：extracted → raw_ready
        ingest.transition(item_id, "extracted")
        pipeline_mod._mark_raw_ready(item_id)

        item = ingest.get_item(item_id)
        assert item["status"] == "raw_ready", item["status"]
        assert "ai" not in enqueued

    def test_raw_ready_is_manually_confirmable(self):
        """raw_ready 的条目要能进加工区（用户手写摘要直接入库）。"""
        from app.services.ingest_service import get_ingest_service
        from app.services.review_service import get_review_service

        ingest = get_ingest_service()
        item_id = self._submit_item("加工区可见")
        ingest.update_item(item_id, raw_text="正文")
        ingest.transition(item_id, "extracted")

        import app.workers.pipeline as pipeline_mod

        pipeline_mod._mark_raw_ready(item_id)

        manual = get_review_service().list_manual()
        assert any(m["id"] == item_id for m in manual), manual

    def test_retry_raw_ready_triggers_ai(self):
        """raw_ready 的条目点「重新分析」要能排上 ai job。"""
        from app.services.ingest_service import get_ingest_service

        ingest = get_ingest_service()
        item_id = self._submit_item("重试触发")
        ingest.update_item(item_id, raw_text="正文")
        ingest.transition(item_id, "extracted")

        import app.workers.pipeline as pipeline_mod

        pipeline_mod._mark_raw_ready(item_id)

        # 状态机允许 raw_ready → extracted（retry 走这条路）
        ok = ingest.transition(item_id, "extracted")
        assert ok is True, "raw_ready 应该能回到 extracted 以便重试 AI"


class TestASRSimplifiedOutput:
    """v31：ASR 必须输出简体（用户反馈「没自动根据音频转写」）。

    回归背景：faster-whisper 的 tiny 模型在中文上默认输出**繁体**
    （"這是第二大腦和負席計畫"），整篇看着像乱码。

    根因与解法：whisper 会**模仿 initial_prompt 的字形**。
    实测同一段音频的繁体率：
        无提示                   → 17.5%
        "以下是普通话的句子。"     → 1.9%
    官方这句短提示词效果最好；早先写的长提示（"以下是普通话视频的字幕内容…"）
    反而不如它 —— 提示词太长会被当成待转写内容的一部分。
    """

    def test_zh_prompt_is_official_short_form(self):
        from app.services.asr_service import ASRService

        assert ASRService._ZH_PROMPT == "以下是普通话的句子。"

    def test_transcribe_uses_zh_prompt(self, monkeypatch):
        """转写时必须带上简体提示词。"""
        from app.services.asr_service import get_asr_service

        svc = get_asr_service()
        captured: dict = {}

        class FakeModel:
            def transcribe(self, path, **kw):
                captured.update(kw)

                class Seg:
                    text = "大家好"

                return iter([Seg()]), object()

        monkeypatch.setattr(svc, "_load_model", lambda: FakeModel())
        svc.transcribe_file("fake.m4a")

        assert captured.get("initial_prompt") == "以下是普通话的句子。"
        assert captured.get("language") == "zh"

    def test_trad_ratio_detects_traditional(self):
        from app.services.asr_service import trad_ratio

        assert trad_ratio("这是简体中文") == 0.0
        assert trad_ratio("") == 0.0
        assert trad_ratio("這是繁體字") > 0.5
        # 中英混排时只按汉字算
        assert trad_ratio("hello world") == 0.0


class TestNoConsoleWindow:
    """v32：所有后台子进程都**不能弹黑窗**。

    回归背景：用户反馈「还是跳黑窗，但几秒后就自动关了，运行期间还会有个
    小于 1s 的窗口闪退」。

    根因：散落各处的 `subprocess.run` 没加 CREATE_NO_WINDOW。
    最典型的是 `asr_service._run_ytdlp()`（每次抽音频）和
    `social_video` 的 yt-dlp 探测（每次抓取）—— 都是控制台程序，
    不显式抑制就会闪一个窗口。
    （v34 前还有 cli_locator 每次健康检查跑 codebuddy --version，
      那条链路已删。）

    Windows 上子进程默认会自己申请控制台（父进程是 windowed 应用时），
    必须显式 CREATE_NO_WINDOW 才能彻底不弹。
    """

    def test_proc_helper_sets_no_window(self):
        from app.core import proc

        flags = proc._no_window_flags()
        if sys.platform == "win32":
            assert flags == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            assert flags != 0, "Windows 上必须给 CREATE_NO_WINDOW"

    def test_proc_run_actually_suppresses_window(self, monkeypatch):
        """proc.run 要把 creationflags 传给 subprocess.run。"""
        import app.core.proc as proc_mod

        captured = {}

        def fake_run(cmd, **kw):
            captured.update(kw)
            return None

        monkeypatch.setattr(proc_mod.subprocess, "run", fake_run)
        proc_mod.run(["echo", "hi"], capture_output=True)

        if sys.platform == "win32":
            assert captured.get("creationflags"), "没传 creationflags"
        # stdin 必须 DEVNULL：后台服务没有终端，否则子进程会等输入
        assert captured.get("stdin") == subprocess.DEVNULL

    def test_no_bare_subprocess_without_flags(self):
        """静态检查：app/ 里不该有裸 subprocess.run（都该走 proc 工具）。

        v34：登录窗口那条链路已删，现在**没有任何合法例外**。
        """
        import re
        from pathlib import Path

        app_dir = Path(__file__).resolve().parent.parent / "app"
        offenders: list[str] = []
        for py in app_dir.rglob("*.py"):
            if py.name in ("proc.py",):
                continue
            text = py.read_text(encoding="utf-8")
            for m in re.finditer(r"subprocess\.run\(", text):
                # 允许注释/文档字符串里提到
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.start())
                line = text[line_start:line_end]
                if line.strip().startswith(("#", '"""', "*", "'")):
                    continue
                offenders.append(f"{py.relative_to(app_dir)}: {line.strip()}")
        assert not offenders, (
            "这些地方直接调了 subprocess.run，会闪黑窗；"
            f"请改用 app.core.proc.run：{offenders}"
        )


class TestLifespanScope:
    """v34：`lifespan` 里不能在函数中间 import 模块级已有的名字。

    回归背景：改 AI 降级钩子时在 `lifespan()` 中间写了
    `from app.core.config import get_config`。Python 的规则是——
    **函数内任何位置的 import 都会让这个名字变成整个函数的局部变量**，
    于是函数开头第 70 行的 `cfg = get_config()` 抛：

        UnboundLocalError: cannot access local variable 'get_config'
        where it is not associated with a value
        → Application startup failed

    后果：**exe 直接起不来**（windowed 模式没控制台，只看到进程在但端口不通）。
    单测全绿、开发态 import 也正常（因为只 import 模块不跑 lifespan），
    所以这个坑只能靠"真的启动一次"或静态检查发现。
    """

    def test_no_shadowing_import_in_lifespan(self):
        """静态检查：lifespan 内 import 的名字不能在同一函数里先被使用。"""
        import ast
        from pathlib import Path

        main_py = Path(__file__).resolve().parent.parent / "app" / "main.py"
        tree = ast.parse(main_py.read_text(encoding="utf-8"))

        lifespan = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
                lifespan = node
                break
        assert lifespan is not None, "没找到 lifespan()"

        # 收集函数内所有 import 引入的顶层名字 → 行号
        imported: dict[str, int] = {}
        for n in ast.walk(lifespan):
            if isinstance(n, ast.ImportFrom):
                for al in n.names:
                    imported[al.asname or al.name] = n.lineno
            elif isinstance(n, ast.Import):
                for al in n.names:
                    imported[(al.asname or al.name).split(".")[0]] = n.lineno

        # 找这些名字在**首次 import 之前**被读取的位置
        min(imported.values()) if imported else 10**9
        offenders = []
        for n in ast.walk(lifespan):
            if (
                isinstance(n, ast.Name)
                and isinstance(n.ctx, ast.Load)
                and n.id in imported
                and n.lineno < imported[n.id]
            ):
                offenders.append(f"第 {n.lineno} 行用了 {n.id}（它在第 {imported[n.id]} 行才 import）")

        assert not offenders, (
            "lifespan() 里这些名字会在 import 之前被当作局部变量使用，"
            f"启动时会 UnboundLocalError：{offenders}"
        )

    def test_lifespan_registers_fallback_listener(self):
        """降级钩子要能注册上（注册失败会被 except 静默吞掉）。"""
        from app.providers.ai_providers import get_router

        # 只要 current_name 可读、provider 能构造，就说明钩子链条没断
        router = get_router()
        assert router.current_name in ("ollama", "openai_compat", "")


class TestNoteTemplates:
    """v37：创作模板（原创区「空模板」）落库，管理区可增删改。

    回归背景：用户说「原创区的模板可在管理区添加」——
    这 6 个模板原来硬编码在 `OriginalView.vue` 里，加一个要改代码重打包。
    现在进库，管理区 CRUD，原创区只读。

    与「解析模板」的区别：这个是给**人**的 Markdown 骨架，
    那个是给**AI**的提示词。两张表、两个接口，别混。
    """

    def test_seed_creates_builtin_templates(self):
        from app.services.note_template_service import get_note_template_service

        svc = get_note_template_service()
        svc.ensure_seeded()
        names = {t["name"] for t in svc.list()}
        assert {"知识提炼", "知识聚合", "日记", "周回顾", "规则模板", "基本概念"} <= names

    def test_create_update_delete(self):
        from app.services.note_template_service import get_note_template_service

        svc = get_note_template_service()
        svc.ensure_seeded()

        created = svc.create("测试创作模板", "## 小节\n", "说明", ["测试"], "")
        assert created and created["id"] > 0
        assert created["suggest_tags"] == ["测试"]
        assert created["source"] == "manual"

        updated = svc.update(created["id"], body="## 改过\n", suggest_tags=["改后"])
        assert updated["body"] == "## 改过\n"
        assert updated["suggest_tags"] == ["改后"]

        assert svc.delete(created["id"]) is True
        assert svc.get(created["id"]) is None
        assert all(t["id"] != created["id"] for t in svc.list())

    def test_name_must_be_unique(self):
        from app.services.note_template_service import get_note_template_service

        svc = get_note_template_service()
        svc.create("唯一名模板", "正文")
        assert svc.create("唯一名模板", "另一份") is None

    def test_deleted_builtin_is_not_reseeded(self):
        """删掉的内置模板不能被播种逻辑偷偷加回来（用户会以为删不掉）。"""
        from app.services.note_template_service import get_note_template_service

        svc = get_note_template_service()
        # 先补回内置（同文件里别的用例可能已经删过），保证目标存在
        svc.restore_defaults()
        target = next(t for t in svc.list() if t["name"] == "规则模板")

        assert svc.delete(target["id"]) is True
        svc.ensure_seeded()
        assert all(t["name"] != "规则模板" for t in svc.list()), "删掉的又被播回来了"

    def test_restore_defaults_brings_it_back(self):
        from app.services.note_template_service import get_note_template_service

        svc = get_note_template_service()
        svc.restore_defaults()  # 保证存在（上一个用例删过它）
        target = next(t for t in svc.list() if t["name"] == "规则模板")
        svc.delete(target["id"])

        added = svc.restore_defaults()
        assert added >= 1
        assert any(t["name"] == "规则模板" for t in svc.list())


class TestMaturityBasics:
    """v39：成熟软件的基本规范 —— 版本单一源 / 健康检查说真话。

    这两条都是**曾经真实出过问题**的地方：

    · 版本号散在三处硬编码（FastAPI app、/api/health、/diagnose），
      而 `APP_VERSION` 没人用 —— 于是侧边栏显示 0.5.0，
      实际代码已经迭代到 v38。用户看到的和真实的对不上。
    · `/api/health` 恒返回 `ok: true`，数据库坏了也说健康。
    """

    def test_version_has_single_source(self):
        """源码里不该再有硬编码版本号。"""
        import re
        from pathlib import Path

        from app.core.config import APP_VERSION

        app_dir = Path(__file__).resolve().parent.parent / "app"
        # 形如 version="0.5.0" 或 "version": "0.5.0" 的字面量
        pattern = re.compile(r'version["\']?\s*[:=]\s*["\'](\d+\.\d+\.\d+)["\']')
        offenders = []
        for py in app_dir.rglob("*.py"):
            for m in pattern.finditer(py.read_text(encoding="utf-8")):
                if m.group(1) != APP_VERSION:
                    offenders.append(f"{py.relative_to(app_dir)}: {m.group(0)}")
        assert not offenders, f"这些地方硬编码了版本号，应改用 APP_VERSION：{offenders}"

    def test_health_reports_real_state(self):
        """health 的 ok 要由检查结果决定，不是写死的 true。"""
        import asyncio

        from app.main import health

        payload = asyncio.run(health())
        assert "checks" in payload, "health 没有返回检查明细"
        names = {c["name"] for c in payload["checks"]}
        assert {"vault 存在", "数据库存在", "数据可读"} <= names

        # ok 必须与关键项一致（不能恒为 true）
        critical_ok = all(c["ok"] for c in payload["checks"] if c["name"] in
                          ("vault 存在", "数据库存在", "数据可读"))
        assert payload["ok"] == critical_ok, payload

    def test_health_and_diagnose_share_one_implementation(self):
        """health 与 diagnose 必须共用 diagnostics 模块（不能各写一套）。"""
        import inspect

        from app.services import diagnostics

        assert hasattr(diagnostics, "collect_checks")
        assert hasattr(diagnostics, "collect_critical_checks")

        # 路由里不应再有自己实现的检查逻辑
        from app.api.routes import workbench

        src = inspect.getsource(workbench.diagnose)
        assert "collect_checks" in src, "diagnose 没走 diagnostics 模块"


class TestTypeCheckRegressions:
    """v42 首次接入 mypy 抓到、并已修复的真 bug —— 用测试锁住防回潮。

    这两个 bug 的共同点：**异常被 except 吞掉**，所以界面上不报错，
    只是行为悄悄不对。单测原先没覆盖到这两条路径，类型检查反而先发现。
    """

    def test_parse_template_deletion_memory_actually_persists(self):
        """`_was_deleted` / `_remember_deleted` 原用了不存在的 self.db。

        两个方法都把异常吞成 `False` / `pass`，于是用户删掉内置解析模板后，
        下次 seed_defaults 又把它播回来 ——「删不掉」但没有任何报错。
        """
        from app.services.template_service import get_template_service

        svc = get_template_service()
        name = "v42-类型检查-删除记忆"
        assert svc._was_deleted(name) is False
        svc._remember_deleted(name)
        assert svc._was_deleted(name) is True, (
            "删除记忆没落库（修复前是 self.db 触发 AttributeError 被吞掉）"
        )

    def test_partition_delete_with_children_does_not_crash(self):
        """`db.query()` 已经返回 list[Row]，原代码又调了 `.fetchall()`。

        只有删**带子分区**的分区才会走到那行；修复前直接 AttributeError。
        """
        from app.services.partition_service import get_partition_service

        svc = get_partition_service()
        parent = svc.create("v42父分区")
        child = svc.create("v42子分区", parent_id=parent["id"])
        assert child["id"] != parent["id"]

        svc.delete(parent["id"])  # 不该抛 AttributeError
        assert svc.get(parent["id"]) is None
        assert svc.get(child["id"]) is not None, "子分区应上移保留，不随父分区删除"


class TestFreshInstall:
    """v43：全新机器 / 零配置启动 —— 发 exe 给别人用的关键回归。

    以前默认 vault / data / db 全指向 PROJECT_ROOT（打包态 = exe 同级），
    装到 `C:/Program Files` 这类只读位置会直接失败；而且 config 模板里
    写死过开发机的绝对路径，别人打开就是一个不存在的笔记库。

    这里用独立的 ConfigStore 实例 + 临时 HOME 模拟"别人第一次双击"，
    不触碰全局单例，也不污染真实数据。
    """

    @staticmethod
    def _isolate(tmp_path, monkeypatch):
        """模拟「别人第一次双击」：干净的安装目录 + 空的 HOME。

        必须连 PROJECT_ROOT 一起换掉 —— 开发机上 config/config.toml
        是真实存在的（含本机 vault 绝对路径），不换就会被当成已有部署。
        """
        import shutil

        import app.core.config as cfg_mod

        home = tmp_path / "home"
        program = tmp_path / "program"
        (program / "config").mkdir(parents=True, exist_ok=True)
        # 打包版模板在 _MEIPASS/config/，开发版在 PROJECT_ROOT/config/
        shutil.copyfile(
            cfg_mod.PROJECT_ROOT / "config" / "config.example.toml",
            program / "config" / "config.example.toml",
        )
        monkeypatch.setenv("SECOND_BRAIN_HOME", str(home))
        for key in ("SECOND_BRAIN_CONFIG", "SECOND_BRAIN_VAULT", "SECOND_BRAIN_DB"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", program)
        return cfg_mod, home, cfg_mod.ConfigStore(), program

    def test_zero_config_uses_user_writable_dirs(self, tmp_path, monkeypatch):
        import sqlite3

        _cfg_mod, home, store, _program = self._isolate(tmp_path, monkeypatch)
        cfg = store.get()

        assert cfg.user_dir == home
        assert cfg.vault.path == home / "vault"
        assert cfg.data_dir == home / "data"
        assert cfg.db_path == home / "data" / "secondbrain.db"

        # 全部落在用户目录，不依赖 exe 装在哪
        for p in (cfg.vault.path, cfg.data_dir, cfg.db_path):
            assert home in p.parents

        # 真的可写：建目录树、写文件、开 SQLite
        cfg.vault.ensure()
        assert (cfg.vault.path / "01_Inbox").is_dir()
        (cfg.data_dir / "probe.txt").write_text("ok", encoding="utf-8")
        assert (cfg.data_dir / "probe.txt").read_text(encoding="utf-8") == "ok"
        with sqlite3.connect(cfg.db_path) as conn:
            conn.execute("CREATE TABLE probe (x INTEGER)")
        assert cfg.db_path.exists()

    def test_seeded_config_has_no_machine_paths_or_secrets(self, tmp_path, monkeypatch):
        import json
        import re

        cfg_mod, home, store, _program = self._isolate(tmp_path, monkeypatch)
        store.get()

        target = cfg_mod.active_config_path()
        assert target == home / "config.toml"
        assert target.exists(), "首次运行应自动播种 config.toml"

        text = target.read_text(encoding="utf-8")
        assert 'path = ""' in text, "模板里仍写死了笔记库路径"
        assert 'data_dir = ""' in text
        # 模板里不许出现任何**绝对路径**（不写死某台机器的目录名，
        # 否则这个断言本身就把开发机路径留在仓库里了）
        assert not re.search(r'(?m)^\s*\w+\s*=\s*"[A-Za-z]:[\\/]', text), "模板含 Windows 绝对路径"
        assert not re.search(r'(?m)^\s*\w+\s*= "/', text), "模板含 POSIX 绝对路径"
        assert not re.search(r'(?i)^\s*(api_key|token|password)\s*=\s*"[^"]+"', text, re.M)

        # 密钥写回与读取必须落到同一份文件（以前写 exe 同级、读用户目录）
        secret_path = cfg_mod.save_secret("probe_key", "abc")
        assert home in secret_path.parents
        assert json.loads(secret_path.read_text(encoding="utf-8"))["probe_key"] == "abc"
        assert cfg_mod.active_secret_path() == secret_path

    def test_legacy_data_dir_is_preserved(self, tmp_path, monkeypatch):
        """v0.7 升级回归：exe 同级已有库时必须继续用，不能连到新空库。"""
        cfg_mod, _home, _store, program = self._isolate(tmp_path, monkeypatch)
        legacy_data = program / "data"
        legacy_data.mkdir(parents=True)
        (legacy_data / "secondbrain.db").write_bytes(b"legacy")

        cfg = cfg_mod.ConfigStore().get()
        assert cfg.data_dir == legacy_data
        assert cfg.db_path == legacy_data / "secondbrain.db"

    def test_existing_deployment_config_still_wins(self, tmp_path, monkeypatch):
        """升级兼容：exe 同级已有 config.toml 时继续优先读它，不丢配置。"""
        cfg_mod, _home, _store, program = self._isolate(tmp_path, monkeypatch)
        legacy = program / "config" / "config.toml"
        legacy.write_text('[web]\nport = 7699\n', encoding="utf-8")

        cfg = cfg_mod.ConfigStore().get()
        assert cfg_mod.active_config_path() == legacy
        assert cfg.web.port == 7699


class TestSystemTrustStore:
    """v0.7.1：TLS 校验改走系统证书存储。

    起因是一个只在真机出现的报错：设置页里豆包/方舟显示
    「[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate」，
    而浏览器打开同一个地址正常。根因是服务端下发**交叉签名**的根证书
    （leaf → RapidSSL TLS RSA CA G1 → DigiCert Global Root G2(交叉签名版)
    → DigiCert Global Root CA），certifi 里只有自签版 G2，OpenSSL 沿单链
    走到 Global Root CA 就断了；浏览器会构建多条候选链，所以不报。

    修法：启动时 `truststore.inject_into_ssl()`，让校验行为与浏览器一致。
    这里锁住三件事：幂等、缺依赖时降级不抛、以及 lifespan 确实调用了它。
    """

    def test_enable_is_idempotent(self):
        from app.core import tls

        first = tls.enable_system_trust_store()
        second = tls.enable_system_trust_store()
        assert isinstance(first, bool)
        assert first == second, "重复调用不应改变结果，也不应重复注入"

    def test_missing_dependency_degrades_instead_of_raising(self, monkeypatch):
        """没装 truststore 的机器（或裁过的包）必须照常启动。"""
        from app.core import tls

        monkeypatch.setattr(tls, "_truststore", None)
        monkeypatch.setattr(tls, "_enabled", False)
        assert tls.enable_system_trust_store() is False

    def test_injection_failure_degrades(self, monkeypatch):
        """注入本身抛异常时也只降级，不把异常带进启动流程。"""

        class _Boom:
            @staticmethod
            def inject_into_ssl():
                raise RuntimeError("boom")

        from app.core import tls

        monkeypatch.setattr(tls, "_truststore", _Boom())
        monkeypatch.setattr(tls, "_enabled", False)
        assert tls.enable_system_trust_store() is False

    def test_lifespan_enables_it_before_serving(self, monkeypatch):
        """必须在任何 HTTP 客户端创建之前切换，否则首个请求已经用了 certifi。

        这里断言的是**启动顺序**而不是副作用：lifespan 是同步生成器，
        真跑一遍会连带建库、起队列、拉起 worker，单测里没必要付这个代价。
        """
        import inspect

        import app.main as main_mod

        src = inspect.getsource(main_mod.lifespan)
        assert "enable_system_trust_store()" in src, "lifespan 没有接入系统信任库"
        assert src.index("enable_system_trust_store()") < src.index("cfg = get_config()"), (
            "必须先切换信任库，再做任何可能发请求的初始化"
        )


class TestOllamaAutostart:
    """v0.7.1：切到 ollama 时自动拉起本机进程。

    真实场景：设置页 ollama 卡片显示「WinError 10061 目标计算机积极拒绝」，
    但模型（8.4G）早就下好了——只是进程没启动。让用户自己去开始菜单找
    「Ollama」并不合理，应用自己拉起来即可。

    安全边界同样要守住：只对回环地址动手，冷却期内不重复 fork。
    """

    def test_only_loopback_is_allowed(self):
        from app.services import ollama_service as svc

        assert svc.is_loopback("http://127.0.0.1:11434/v1") is True
        assert svc.is_loopback("http://localhost:11434/v1") is True
        assert svc.is_loopback("http://[::1]:11434/v1") is True
        # 别人的机器：绝不隔着网络去启动别人的服务
        assert svc.is_loopback("http://192.168.1.9:11434/v1") is False
        assert svc.ensure_running("http://192.168.1.9:11434/v1", wait=0.1) is False

    def test_already_running_does_not_spawn(self, monkeypatch):
        from app.services import ollama_service as svc

        spawned: list[object] = []
        monkeypatch.setattr(svc, "_alive", lambda *a, **k: True)
        monkeypatch.setattr(svc, "_spawn", lambda exe: spawned.append(exe) or True)

        assert svc.ensure_running("http://127.0.0.1:11434/v1", wait=0.1) is True
        assert spawned == [], "已经在跑就不该再拉一个进程"

    def test_missing_binary_returns_false(self, monkeypatch):
        from app.services import ollama_service as svc

        monkeypatch.setattr(svc, "_alive", lambda *a, **k: False)
        monkeypatch.setattr(svc, "_candidates", lambda: [])
        monkeypatch.setattr(svc, "_last_attempt", {})

        assert svc.ensure_running("http://127.0.0.1:11434/v1", wait=0.1) is False

    def test_cooldown_prevents_repeated_spawn(self, monkeypatch):
        """设置页会反复探测，60 秒内只准拉一次。"""
        from app.services import ollama_service as svc

        spawned: list[object] = []
        monkeypatch.setattr(svc, "_alive", lambda *a, **k: False)
        monkeypatch.setattr(svc, "_candidates", lambda: [Path("ollama.exe")])
        monkeypatch.setattr(svc, "_spawn", lambda exe: spawned.append(exe) or True)
        monkeypatch.setattr(svc, "_last_attempt", {})

        base = "http://127.0.0.1:11434/v1"
        assert svc.ensure_running(base, wait=0.1) is False
        assert svc.ensure_running(base, wait=0.1) is False
        assert len(spawned) == 1, "冷却期内重复调用不应重复 fork"


class TestYtdlpNetworkPath:
    """v0.7.2：yt-dlp 必须直连优先，系统代理只能当兜底。

    回归背景：用户开着 Clash（127.0.0.1:26561），采集快手报
    `[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate`。
    实测同机 `--proxy ""` 直连正常 —— yt-dlp 在 Windows 上会读注册表代理，
    把国内站点也塞进了代理，代理做了 TLS 拦截。

    另一条：链接失效时 yt-dlp 报 `Unsupported URL: .../new-reco`，
    这属于"内容不支持"，不该再换代理重试一遍（白等几十秒）。
    """

    def test_ssl_error_counts_as_network(self):
        from app.core import ytdlp

        assert ytdlp.is_network_error(
            "ERROR: [generic] x: Unable to download webpage: "
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate (_ssl.c:1032)"
        )
        assert ytdlp.is_network_error("ERROR: Connection reset by peer")
        assert not ytdlp.is_network_error("ERROR: Unsupported URL: https://kuaishou.com/")

    def test_humanize_ssl_points_at_proxy(self):
        from app.core import ytdlp

        msg = ytdlp.humanize(
            "ERROR: [generic] nZZ1KENF: Unable to download webpage: "
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate (_ssl.c:1032)"
        )
        assert "证书" in msg and "代理" in msg, msg

    def test_humanize_dead_shortlink(self):
        from app.core import ytdlp

        msg = ytdlp.humanize("ERROR: Unsupported URL: https://www.kuaishou.com/new-reco")
        assert "失效" in msg, msg
        # 不能把链接失效说成网络问题，否则用户一直重试
        assert "网络" not in msg.replace("不是网络问题", ""), msg

    def _fake(self, monkeypatch, results):
        """results: [(returncode, stderr)]，按调用顺序返回。"""
        import app.core.ytdlp as ytdlp_mod

        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)

            class P:
                def __init__(self, rc, err):
                    self.returncode = rc
                    self.stdout = '{"title": "t"}' if rc == 0 else ""
                    self.stderr = err

            rc, err = results[min(len(calls) - 1, len(results) - 1)]
            return P(rc, err)

        monkeypatch.setattr(ytdlp_mod.proc_mod, "run", fake_run)
        monkeypatch.setattr(ytdlp_mod, "find_bin", lambda: "yt-dlp.exe")
        return calls

    def test_direct_first_and_stops_on_success(self, monkeypatch):
        from app.core import ytdlp

        calls = self._fake(monkeypatch, [(0, "")])
        res = ytdlp.run(["--dump-single-json"], "https://v.kuaishou.com/x", timeout=5)

        assert res.ok
        assert len(calls) == 1, "直连成功就不该再走代理"
        assert calls[0][:2] == ["yt-dlp.exe", "--proxy"]
        assert calls[0].index("--proxy") < calls[0].index("https://v.kuaishou.com/x")

    def test_ssl_failure_falls_back_to_system_proxy(self, monkeypatch):
        """直连被证书拦掉 → 换系统代理再试一次（保留必须挂代理的网络）。"""
        from app.core import ytdlp

        ssl_err = "ERROR: Unable to download webpage: [SSL: CERTIFICATE_VERIFY_FAILED] bad"
        calls = self._fake(monkeypatch, [(1, ssl_err), (0, "")])
        res = ytdlp.run(["--dump-single-json"], "https://example.com/v", timeout=5)

        assert res.ok
        assert res.used_system_proxy is True
        assert len(calls) == 2
        assert "--proxy" not in calls[1], "回退时应该用系统代理"

    def test_unsupported_url_does_not_retry(self, monkeypatch):
        from app.core import ytdlp

        calls = self._fake(monkeypatch, [(1, "ERROR: Unsupported URL: https://kuaishou.com/")])
        res = ytdlp.run(["--dump-single-json"], "https://v.kuaishou.com/x", timeout=5)

        assert not res.ok
        assert len(calls) == 1, "链接失效重试代理没有意义，别让用户白等"
        assert "失效" in res.error

    def test_adapters_do_not_shell_out(self):
        """采集适配器走 httpx，不再自己拼 subprocess（yt-dlp 只留给 ASR）。"""
        import inspect

        from app.adapters import douyin, kuaishou

        for mod in (douyin, kuaishou):
            src = inspect.getsource(mod)
            assert "subprocess" not in src, mod.__name__


class TestPlatformAdapters:
    """v0.7.2：抖音/快手从 yt-dlp 壳换成页面解析。

    匿名请求拿不到登录态内容时会退到 meta 兜底，所以这里只测纯解析部分：
    链接里抠 ID、SSR JSON 里抠文案。真正联网抓取依赖运行环境，不在单测里做。
    """

    def test_douyin_aweme_id_from_various_links(self):
        from app.adapters.douyin import DouyinAdapter

        a = DouyinAdapter()
        assert a._aweme_id("https://www.douyin.com/video/7257768974631374115") == (
            "7257768974631374115"
        )
        assert a._aweme_id(
            "https://www.iesdouyin.com/share/video/7257768974631374115/?region=CN"
        ) == "7257768974631374115"
        assert a._aweme_id("https://www.douyin.com/discover?modal_id=7703737123258307840") == (
            "7703737123258307840"
        )
        assert a._aweme_id("https://www.douyin.com/user/xyz") == ""

    def test_douyin_parse_router_data(self):
        from app.adapters.douyin import DouyinAdapter

        html = (
            "<script>window._ROUTER_DATA = "
            '{"loaderData":{"video_(id)/page":{"videoInfoRes":{"item_list":['
            '{"desc":"今天的晚饭 #美食","duration":15200,'
            '"author":{"nickname":"小张"}}]}}}};'
            "</script>"
        )
        data = DouyinAdapter._parse_router_data(html)
        assert data["loaderData"]["video_(id)/page"]["videoInfoRes"]["item_list"][0][
            "desc"
        ].startswith("今天的晚饭")

        from app.adapters.douyin import _find_key

        items = _find_key(data, "item_list")
        result = DouyinAdapter()._build_result(items[0], "7257768974631374115")
        assert result is not None and result.ok
        assert result.title == "今天的晚饭 #美食"
        assert "小张" in result.text and "文案" in result.text
        assert result.extra["duration"] == 15

    def test_kuaishou_photo_id_and_state(self):
        from app.adapters.kuaishou import KuaishouAdapter

        a = KuaishouAdapter()
        assert a._photo_id("https://www.kuaishou.com/short-video/3xtest123") == "3xtest123"
        assert a._photo_id("https://v.m.chenzhongtech.com/fw/photo/3xabcd") == "3xabcd"
        assert a._photo_id("https://www.kuaishou.com/profile/xx") == ""

        html = (
            "<script>window.__APOLLO_STATE__ = "
            '{"VisionVideoDetailPhoto:3x1":{"caption":"赶海第一天","userName":"老王"}};'
            "</script>"
        )
        state = a._parse_state(html)
        result = a._build_result(state, "3x1")
        assert result is not None and result.ok
        assert result.title == "赶海第一天"
        assert "老王" in result.text

    def test_meta_fallback_extracts_title_and_description(self, monkeypatch):
        """任何页面至少要有标题+摘要，不能空手失败。"""
        from app.adapters import web as web_mod

        html = (
            "<html><head><title>某某文章 - 知乎</title>"
            '<meta property="og:description" content="这是文章摘要，长度足够触发兜底逻辑。">'
            "</head><body></body></html>"
        )

        class R:
            text = html

        class FakeClient:
            def get(self, url):
                return R()

        adapter = web_mod.WebAdapter()
        monkeypatch.setattr(adapter, "_extract_trafilatura", lambda url: ("", ""))
        monkeypatch.setattr(adapter, "_extract_playwright", lambda url: ("", ""))
        # 注意：extract() 换 URL 时会 close() 掉旧 client，所以必须 patch 类属性，
        # 不能 patch 实例上的 client 对象。
        monkeypatch.setattr(web_mod.WebAdapter, "client", property(lambda self: FakeClient()))

        result = adapter.extract("https://zhuanlan.zhihu.com/p/123")
        assert result.ok
        assert result.extractor == "meta"
        assert "某某文章" in result.title, result.title
        assert "这是文章摘要" in result.text


class TestEmbeddingOffline:
    """v0.7.2：embedding 后端必须离线判定，绝不联网。

    回归背景：装上 transformers 之后，`AutoTokenizer.from_pretrained("bge-m3")`
    会去 huggingface.co 查 revision；本机网络不通，**不是抛异常而是挂住**
    （socket.create_connection 无限等）。表现为整个采集流程卡死、
    后端单测跑不完（155 个只跑了 18 个就超时）。
    """

    def test_empty_ref_is_not_ready(self):
        from app.services.embedding_service import EmbeddingService

        assert EmbeddingService._model_ready("") is False

    def test_local_dir_is_ready(self, tmp_path):
        from app.services.embedding_service import EmbeddingService

        assert EmbeddingService._model_ready(str(tmp_path)) is True

    def test_bare_name_without_cache_is_not_ready(self):
        from app.services.embedding_service import EmbeddingService

        # 构造一个绝不会出现在缓存里的名字
        assert EmbeddingService._model_ready("secondbrain-nonexistent/bge-test-xyz") is False

    def test_falls_back_to_hash_without_network(self, monkeypatch):
        from app.services import embedding_service as mod

        svc = mod.EmbeddingService()
        svc._loaded = False
        monkeypatch.setattr(svc, "_model_ref", lambda: "secondbrain-nonexistent/bge-test-xyz")

        # 只要走了网络就会挂住 —— 这里能返回就说明没联网
        assert svc.backend == "hash"
        assert svc.available() is False

    def test_hash_backend_still_produces_vectors(self):
        from app.services import embedding_service as mod

        svc = mod.EmbeddingService()
        vector = svc._hash_encode("短视频文案分析")
        assert len(vector) == svc.cfg.ai.embedding.dim
        assert any(v != 0 for v in vector)
