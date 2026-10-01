"""첨부파일 다운로드/프롬프트 주입 테스트."""

from __future__ import annotations

import os

import pytest

from slack_bot import attachments
from slack_bot.attachments import (
    DownloadedFile,
    build_attachment_note,
    download_slack_files,
)


# ----------------------------------------------------------------
# aiohttp.ClientSession 페이크 (실제 네트워크 없이 다운로드 흐름 검증)
# ----------------------------------------------------------------


class _FakeContent:
    def __init__(self, data: bytes):
        self._data = data

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._data), n):
            yield self._data[i : i + n]


class _FakeResp:
    def __init__(self, status: int, data: bytes):
        self.status = status
        self.content = _FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, responses: dict, headers=None, timeout=None):
        self._responses = responses
        self.headers = headers

    def get(self, url: str):
        status, data = self._responses[url]
        return _FakeResp(status, data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _patch_session(monkeypatch, responses: dict) -> dict:
    """attachments.aiohttp.ClientSession을 페이크로 교체하고 생성 kwargs를 캡쳐."""
    captured: dict = {}

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return _FakeSession(responses, **{k: kwargs.get(k) for k in ("headers",)})

    monkeypatch.setattr(attachments.aiohttp, "ClientSession", factory)
    return captured


class TestBuildAttachmentNote:
    def test_empty_returns_empty_string(self):
        assert build_attachment_note([], []) == ""
        assert build_attachment_note([]) == ""

    def test_lists_paths_and_mimetypes(self):
        files = [
            DownloadedFile("/tmp/a/report.xlsx", "report.xlsx", "application/xlsx", 10),
            DownloadedFile("/tmp/a/chart.png", "chart.png", "image/png", 20),
        ]
        note = build_attachment_note(files)
        assert "/tmp/a/report.xlsx" in note
        assert "image/png" in note
        assert "chart.png" in note
        assert "Read" in note  # claude에게 Read 하라는 지시 포함

    def test_includes_errors(self):
        note = build_attachment_note([], ["big.zip: 파일이 너무 큽니다"])
        assert "big.zip" in note
        assert "가져오지 못했습니다" in note


class TestSafeDestPath:
    def test_strips_path_separators(self, tmp_path):
        p = attachments._safe_dest_path(str(tmp_path), "../../etc/passwd")
        # 결과는 반드시 dest_dir 안에 있어야 한다.
        assert os.path.dirname(p) == str(tmp_path)
        assert ".." not in os.path.basename(p)

    def test_dedups_collisions(self, tmp_path):
        first = attachments._safe_dest_path(str(tmp_path), "a.txt")
        open(first, "w").close()
        second = attachments._safe_dest_path(str(tmp_path), "a.txt")
        assert first != second
        assert os.path.basename(second) == "a_1.txt"


class TestDownloadSlackFiles:
    @pytest.mark.asyncio
    async def test_no_files_or_token_returns_empty(self):
        assert await download_slack_files([], "tok") == ([], [])
        assert await download_slack_files([{"url_private": "x"}], "") == ([], [])

    @pytest.mark.asyncio
    async def test_downloads_file_and_sets_auth_header(self, tmp_path, monkeypatch):
        url = "https://files.slack.com/x/report.csv"
        captured = _patch_session(monkeypatch, {url: (200, b"col1,col2\n1,2\n")})

        files = [
            {
                "name": "report.csv",
                "mimetype": "text/csv",
                "url_private_download": url,
                "size": 14,
            }
        ]
        downloaded, errors = await download_slack_files(
            files, "xoxb-tok", dest_dir=str(tmp_path)
        )

        assert errors == []
        assert len(downloaded) == 1
        d = downloaded[0]
        assert d.name == "report.csv"
        assert d.mimetype == "text/csv"
        with open(d.path, "rb") as f:
            assert f.read() == b"col1,col2\n1,2\n"
        # Bearer 인증 헤더가 세션에 설정됐는지
        assert captured["headers"]["Authorization"] == "Bearer xoxb-tok"

    @pytest.mark.asyncio
    async def test_skips_oversized_by_declared_size(self, tmp_path, monkeypatch):
        url = "https://files.slack.com/x/huge.bin"
        _patch_session(monkeypatch, {url: (200, b"x")})
        files = [
            {
                "name": "huge.bin",
                "url_private_download": url,
                "size": attachments.MAX_FILE_BYTES + 1,
            }
        ]
        downloaded, errors = await download_slack_files(
            files, "tok", dest_dir=str(tmp_path)
        )
        assert downloaded == []
        assert any("너무 큽니다" in e for e in errors)

    @pytest.mark.asyncio
    async def test_http_error_recorded_as_failure(self, tmp_path, monkeypatch):
        url = "https://files.slack.com/x/nope.png"
        _patch_session(monkeypatch, {url: (403, b"")})
        files = [{"name": "nope.png", "url_private_download": url}]
        downloaded, errors = await download_slack_files(
            files, "tok", dest_dir=str(tmp_path)
        )
        assert downloaded == []
        assert any("HTTP 403" in e for e in errors)
        # 실패 시 부분 파일이 남지 않아야 한다.
        assert os.listdir(tmp_path) == []

    @pytest.mark.asyncio
    async def test_missing_url_recorded_as_failure(self, tmp_path, monkeypatch):
        _patch_session(monkeypatch, {})
        files = [{"name": "orphan.txt"}]
        downloaded, errors = await download_slack_files(
            files, "tok", dest_dir=str(tmp_path)
        )
        assert downloaded == []
        assert any("URL" in e for e in errors)

    @pytest.mark.asyncio
    async def test_caps_number_of_files(self, tmp_path, monkeypatch):
        responses = {}
        files = []
        for i in range(attachments.MAX_FILES + 3):
            u = f"https://files.slack.com/x/f{i}.txt"
            responses[u] = (200, b"data")
            files.append({"name": f"f{i}.txt", "url_private_download": u})
        _patch_session(monkeypatch, responses)

        downloaded, errors = await download_slack_files(
            files, "tok", dest_dir=str(tmp_path)
        )
        assert len(downloaded) == attachments.MAX_FILES
        assert any("너무 많아" in e for e in errors)
