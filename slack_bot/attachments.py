from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

# 다운로드 상한 — 과도한 디스크/메모리 사용 및 악성 대용량 업로드 방어.
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50MB/파일
MAX_FILES = 10  # 한 메시지당 처리할 최대 첨부 개수
_DOWNLOAD_TIMEOUT = 120  # 세션 전체 타임아웃(초)
_CHUNK = 64 * 1024


@dataclass
class DownloadedFile:
    """Slack에서 임시 디렉토리로 받아온 첨부파일."""

    path: str       # 로컬 절대경로 (claude -p가 Read 할 대상)
    name: str       # Slack 원본 파일명
    mimetype: str   # Slack이 보고한 MIME 타입
    size: int       # 실제로 기록된 바이트 수


def _safe_dest_path(dest_dir: str, name: str) -> str:
    """파일명을 안전하게 정규화하고 디렉토리 내에서 충돌하지 않는 경로를 만든다.

    Slack 파일명은 임의의 문자열이므로 경로 구분자/상위 참조(`../`)를 제거해
    dest_dir 밖으로 새어나가지 못하게 한다.
    """
    base = os.path.basename(name).strip() or "attachment"
    base = base.replace("/", "_").replace("\\", "_")
    candidate = os.path.join(dest_dir, base)
    stem, ext = os.path.splitext(base)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(dest_dir, f"{stem}_{i}{ext}")
        i += 1
    return candidate


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def _download_one(
    session: aiohttp.ClientSession,
    f: dict,
    dest_dir: str,
) -> tuple[DownloadedFile | None, str | None]:
    """단일 Slack 파일을 다운로드. (DownloadedFile|None, 실패사유|None) 반환."""
    name = f.get("name") or f.get("title") or f.get("id") or "attachment"
    # url_private_download는 Content-Disposition: attachment로 원본 바이트를 준다.
    url = f.get("url_private_download") or f.get("url_private")
    if not url:
        return None, f"{name}: 다운로드 URL이 없습니다"

    size = f.get("size") or 0
    if size and size > MAX_FILE_BYTES:
        return None, f"{name}: 파일이 너무 큽니다 ({size} bytes)"

    dest_path = _safe_dest_path(dest_dir, name)
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None, f"{name}: HTTP {resp.status}"
            written = 0
            with open(dest_path, "wb") as out:
                async for chunk in resp.content.iter_chunked(_CHUNK):
                    written += len(chunk)
                    if written > MAX_FILE_BYTES:
                        out.close()
                        _silent_remove(dest_path)
                        return None, f"{name}: 파일이 너무 큽니다 (상한 초과)"
                    out.write(chunk)
        return (
            DownloadedFile(
                path=dest_path,
                name=name,
                mimetype=f.get("mimetype", ""),
                size=written,
            ),
            None,
        )
    except Exception as e:  # noqa: BLE001 - best-effort, 한 파일 실패가 전체를 막지 않도록
        _silent_remove(dest_path)
        logger.warning("첨부파일 다운로드 실패: %s", name, exc_info=True)
        return None, f"{name}: {e}"


async def download_slack_files(
    files: list[dict],
    token: str,
    dest_dir: str | None = None,
) -> tuple[list[DownloadedFile], list[str]]:
    """Slack 메시지의 ``files`` 배열을 임시 디렉토리로 다운로드한다.

    Slack 파일 URL(`url_private*`)은 봇 토큰 Bearer 인증이 있어야 접근 가능하다.
    best-effort — 일부 파일이 실패해도 예외를 던지지 않고 (성공 목록, 실패 사유
    목록)을 함께 돌려준다.
    """
    if not files or not token:
        return [], []

    if dest_dir is None:
        dest_dir = tempfile.mkdtemp(prefix="slackbot_attach_")
    else:
        os.makedirs(dest_dir, exist_ok=True)

    downloaded: list[DownloadedFile] = []
    errors: list[str] = []
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for f in files[:MAX_FILES]:
            result, error = await _download_one(session, f, dest_dir)
            if result is not None:
                downloaded.append(result)
            if error is not None:
                errors.append(error)

    if len(files) > MAX_FILES:
        errors.append(
            f"첨부가 너무 많아 처음 {MAX_FILES}개만 처리했습니다 "
            f"(총 {len(files)}개)"
        )

    return downloaded, errors


def build_attachment_note(
    downloaded: list[DownloadedFile],
    errors: list[str] | None = None,
) -> str:
    """다운로드한 첨부파일을 claude -p 프롬프트에 덧붙일 안내 텍스트로 변환.

    절대경로를 명시해 claude CLI가 Read 도구로 직접 열 수 있게 한다. 이미지/표/
    문서 모두 Read로 처리 가능(Claude는 멀티모달). 첨부가 없고 실패도 없으면
    빈 문자열을 반환해 호출부에서 프롬프트를 바꾸지 않도록 한다.
    """
    if not downloaded and not errors:
        return ""

    lines = [
        "",
        "",
        "[첨부파일] 아래 로컬 파일들이 이 요청과 함께 업로드되었습니다. "
        "Read 도구로 직접 열어 내용(이미지·표·문서 포함)을 확인한 뒤 답하세요:",
    ]
    for d in downloaded:
        meta = d.mimetype or "unknown"
        lines.append(f"- {d.path} ({meta}, 원본명: {d.name})")

    if errors:
        lines.append("다음 첨부는 가져오지 못했습니다:")
        for e in errors:
            lines.append(f"- {e}")

    return "\n".join(lines)
