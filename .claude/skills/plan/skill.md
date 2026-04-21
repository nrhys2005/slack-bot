---
name: plan
description: "이슈의 구현 계획을 수립한다."
---

# /plan — 구현 계획 수립

## 입력

이슈 ID: $ARGUMENTS

## 워크플로우

1. Jira에서 이슈를 조회한다 (`mcp__mcp-server__jira_get_issue`)
2. `.claude/agents/planner.md`의 지시에 따라 **Planner 에이전트**를 실행한다
3. `.plans/{이슈ID}.md` 파일에 구현 계획을 저장한다

## 완료 조건

- `.plans/{이슈ID}.md` 파일 생성
- 요구사항 요약, 영향 범위 분석, 구현 단계, 테스트 전략이 포함됨
