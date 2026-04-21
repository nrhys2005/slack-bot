---
name: validate-plan
description: "이슈의 기획 타당성과 구현 계획의 기술적 실현 가능성을 검증한다."
---

# /validate-plan — 기획 & 계획 검증

## 입력

이슈 ID: $ARGUMENTS

## 전제 조건

- `.plans/{이슈ID}.md` 파일이 존재해야 한다 (없으면 `/plan`을 먼저 실행)

## 워크플로우

1. `.plans/{이슈ID}.md` 계획서를 읽는다
2. Jira에서 이슈를 조회한다 (`mcp__mcp-server__jira_get_issue`)
3. `.claude/agents/plan-validator.md`의 지시에 따라 **Plan Validator 에이전트**를 실행한다
4. 검증 결과를 `.plans/{이슈ID}.validation.md`에 저장한다
5. 판정에 따라 분기:
   - **APPROVE** → 사용자에게 검증 통과 보고
   - **REFINE** → 사용자에게 계획 문제점 보고
   - **ESCALATE** → 사용자에게 기획 문제점 + 수정 제안 보고

## 완료 조건

- `.plans/{이슈ID}.validation.md` 파일 생성
- 판정(APPROVE/REFINE/ESCALATE)과 근거가 명확히 제시됨
