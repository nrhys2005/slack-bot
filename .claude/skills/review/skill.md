---
name: review
description: "이슈의 구현 결과를 리뷰한다."
---

# /review — 코드 리뷰

## 입력

이슈 ID: $ARGUMENTS

## 워크플로우

1. `git diff main..HEAD`로 변경사항을 확인한다
2. `.claude/agents/reviewer.md`의 지시에 따라 **Reviewer 에이전트**를 실행한다
3. 리뷰 결과를 판정한다 (APPROVE / REQUEST_CHANGES / REDESIGN)

## 완료 조건

- 리뷰 판정 (APPROVE/REQUEST_CHANGES/REDESIGN)과 근거가 명확히 제시됨
