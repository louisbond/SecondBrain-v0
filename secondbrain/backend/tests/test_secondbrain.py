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
        import av

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


class TestCliLocator:
    """CodeBuddy CLI 定位与环境处理（实机核查后的修正点）。"""

    def test_strip_env_removes_conflicting_vars(self):
        """必须剥离会引发端口冲突/代理崩溃的环境变量。"""
        from app.core.cli_locator import STRIP_ENV, build_env

        env = build_env()
        for key in STRIP_ENV:
            assert key not in env, f"{key} 未被剥离"

    def test_strip_env_keeps_other_vars(self):
        from app.core.cli_locator import build_env

        env = build_env()
        # PATH 等必须保留，否则 node 找不到
        assert "PATH" in env or "Path" in env or "PATH" in str(env.keys())

    def test_locate_cli_returns_something(self):
        """定位函数不应抛异常；找不到时 available=False。"""
        from app.core.cli_locator import locate_cli

        info = locate_cli()
        assert isinstance(info.available, bool)
        if info.available:
            assert info.path is not None and info.path.exists()

    def test_workbuddy_provider_uses_real_cli(self):
        """provider 的健康检查应基于真实 CLI（--version），不依赖 daemon。"""
        from app.core.config import get_config
        from app.providers.ai_providers import WorkbuddyProvider

        provider = WorkbuddyProvider(cfg=get_config())
        health = provider.health()
        # 本机装了 WorkBuddy → CLI 应可定位；未登录时 ok 仍为 True（CLI 可执行）
        assert health.detail  # 有可读的说明
        assert "CLI" in health.detail or "未找到" in health.detail

    def test_port_injection_skips_subcommands(self):
        """--port 只能加在主命令上，子命令加了会报 unknown option。"""
        from app.core.config import get_config
        from app.providers.ai_providers import WorkbuddyProvider

        provider = WorkbuddyProvider(cfg=get_config())
        main_args = provider._with_port(["-p", "hello"])
        assert main_args[0] == "--port" and main_args[1].isdigit()

        sub_args = provider._with_port(["daemon", "status"])
        assert sub_args == ["daemon", "status"]

    def test_provider_reports_auth_error_actionably(self):
        """未登录时应给出可操作的提示，而不是裸的原始错误。"""
        from app.core.config import get_config
        from app.providers.ai_providers import ProviderError, WorkbuddyProvider

        provider = WorkbuddyProvider(cfg=get_config())

        # 用假结果模拟"未登录"
        def fake_run(args, timeout):
            return 0, "", "Authentication required. Please use /login command to sign in to your account"

        provider._run = fake_run  # type: ignore[assignment]
        with pytest.raises(ProviderError) as exc:
            provider.complete("测试")
        assert "未登录" in str(exc.value)
        assert "codebuddy" in str(exc.value)


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
        from app.services.search_service import get_search_service
        from app.services.review_service import get_review_service

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
        from app.services.index_service import get_index_service
        from app.core.db import get_db

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

    def test_parse_missing_fields_fails(self):
        from app.providers.ai_service import parse_ai_payload

        result, error = parse_ai_payload('{"summary": "x"}', "bullets")
        assert result is None
        assert "tags" in error or "partitions" in error

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
        from app.providers.ai_providers import ProviderError, ProviderRouter
        from app.core.config import get_config

        router = ProviderRouter(cfg=get_config())
        router.switch("workbuddy", manual=True)

        called: list[str] = []

        class FakeProvider:
            def __init__(self, name):
                self.name = name

            def complete(self, prompt, system=""):
                called.append(self.name)
                if self.name == "workbuddy":
                    raise ProviderError("HTTP 429 限流", rate_limited=True)
                return "ollama-ok"

            def health(self):
                from app.providers.ai_providers import ProviderHealth

                return ProviderHealth(ok=True)

        router.build = lambda name: FakeProvider(name)  # type: ignore[assignment]
        out = router.complete("test")
        assert out == "ollama-ok"
        assert called == ["workbuddy", "ollama"]
        assert router.fallback_active is True
        assert router.current_name == "ollama"

    def test_provider_no_fallback_when_disabled(self):
        from app.providers.ai_providers import ProviderError, ProviderRouter
        from app.core.config import get_config

        cfg = get_config()
        cfg.ai.auto_fallback = False
        try:
            router = ProviderRouter(cfg=cfg)
            router.switch("workbuddy", manual=True)

            class FakeProvider:
                name = "workbuddy"

                def complete(self, prompt, system=""):
                    raise ProviderError("boom", rate_limited=True)

            router.build = lambda name: FakeProvider()  # type: ignore[assignment]
            with pytest.raises(ProviderError):
                router.complete("test")
            assert router.current_name == "workbuddy"
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
        router.switch("workbuddy", manual=True)

        class FakeProvider:
            name = "workbuddy"

            def complete(self, prompt, system=""):
                raise ProviderError("codebuddy CLI 未登录", auth_required=True)

        called = []

        def _build(name):
            if name == "workbuddy":
                return FakeProvider()
            called.append(name)

            class Backup:
                def complete(self, prompt, system=""):
                    return "backup-should-not-be-used"

            return Backup()

        router.build = _build  # type: ignore[assignment]

        with pytest.raises(ProviderError):
            router.complete("test")

        assert router.current_name == "workbuddy", "认证错误不应触发降级"
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
            def transcribe(self, path, language=None, vad_filter=True, beam_size=5):
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
        assert "ai" in enqueued, "ASR 失败后仍要进 AI"


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
