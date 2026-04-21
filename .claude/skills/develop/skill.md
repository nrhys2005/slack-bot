---
name: develop
description: "이슈의 구현 계획에 따라 코드를 구현한다."
---

# /develop — 코드 구현

## 입력

이슈 ID: $ARGUMENTS

## 전제 조건

- `.plans/{이슈ID}.md` 파일이 존재해야 한다 (없으면 `/plan`을 먼저 실행)

## 워크플로우

1. `.plans/{이슈ID}.md` 계획서를 읽는다
2. `.claude/agents/developer.md`의 지시에 따라 **Developer 에이전트**를 실행한다
3. `isolation: "worktree"`로 격리 환경에서 실행
4. 구현 → 테스트 → 린트 → 커밋 → push

## 완료 조건

- 계획서의 모든 구현 단계 완료
- 테스트 통과
- 린트 클린
- `feature/{이슈ID}-*` 브랜치에 커밋 및 push 완료
