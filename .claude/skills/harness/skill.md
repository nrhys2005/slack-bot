---
name: harness
description: "이슈 기반 개발 파이프라인 오케스트레이터. Planner→Plan Validator→Developer→Reviewer→PR의 전체 흐름을 관리한다."
---

# Harness — 이슈 기반 개발 파이프라인

Planner, Plan Validator, Developer, Reviewer 에이전트가 협업하여 Jira 이슈를 구현하는 파이프라인.

## 에이전트 구성

| 에이전트 | 파일 | 역할 | 실행 방식 |
|----------|------|------|-----------|
| planner | `.claude/agents/planner.md` | 이슈 분석, 구현 계획 수립 | 메인 컨텍스트 |
| plan-validator | `.claude/agents/plan-validator.md` | 기획 타당성 + 계획 실현 가능성 검증 | 메인 컨텍스트 |
| developer | `.claude/agents/developer.md` | 코드 구현, 테스트 작성 | worktree 격리 |
| reviewer | `.claude/agents/reviewer.md` | 코드 리뷰, QA 검증 | worktree 결과 대상 |

## 파이프라인 흐름

```
Planner
  ↓ .plans/{이슈ID}.md
Plan Validator
  ├─ APPROVE → Developer
  ├─ REFINE → Planner 재호출 (최대 2회)
  └─ ESCALATE → 사용자 판단 요청
Developer
  ↓ worktree 격리 + 커밋 + push
Reviewer
  ├─ APPROVE → PR 생성
  ├─ REQUEST_CHANGES → Developer 수정 (최대 2회)
  └─ REDESIGN → Planner 재호출
```

## 입력

이슈 ID (쉼표 구분으로 복수 가능) + 옵션: $ARGUMENTS

예시: `SB-123` 또는 `SB-123,SB-124` 또는 `SB-123 --auto`

### 옵션

| 옵션 | 설명 |
|------|------|
| `--auto` | 자동 모드. 사용자 승인 없이 전체 파이프라인을 실행. 단, ESCALATE는 항상 사용자에게 보고. |

## 워크플로우

### Phase 0: main 브랜치 최신화

```bash
git checkout main && git pull origin main
```

### Phase 1: 계획 수립

1. 입력된 이슈 ID를 파싱한다
2. 각 이슈에 대해 **Planner 에이전트**를 실행한다:
   - Jira에서 이슈 조회 (`mcp__mcp-server__jira_get_issue`)
   - 관련 코드 분석
   - `.plans/{이슈ID}.md` 파일에 구현 계획 저장

### Phase 1.5: 계획 검증

각 이슈에 대해 **Plan Validator 에이전트**를 실행한다:
- 기획(이슈) 타당성 + 구현 계획 기술적 실현 가능성 검증
- 검증 결과를 `.plans/{이슈ID}.validation.md`에 저장

**판정 분기:**
- **APPROVE** → Phase 2로 진행 (`--auto`면 즉시, 아니면 사용자 승인)
- **REFINE** → Planner에게 피드백 → 계획 수정 → 재검증 (최대 2라운드)
- **ESCALATE** → 사용자에게 기획 문제 보고 (항상 사용자 판단 요청)

### Phase 2: 개발

각 이슈에 대해 **Developer 에이전트**를 실행한다:
- `isolation: "worktree"`로 격리 환경에서 실행
- `.plans/{이슈ID}.md`를 읽고 구현
- 구현 → 테스트 → 린트 → 커밋 → push

### Phase 3: 리뷰

각 이슈에 대해 **Reviewer 에이전트**를 실행한다:

**판정 분기:**
- **APPROVE** → Phase 4로 진행
- **REQUEST_CHANGES** → Developer 수정 → 재리뷰 (최대 2라운드)
- **REDESIGN** → Planner 재호출 (최대 1회)

### Phase 4: PR 생성

리뷰 통과된 이슈에 대해:
1. `gh pr create`로 PR 생성 (base: `main`, head: `feature/{이슈ID}-*`)
2. PR URL을 사용자에게 보고

### Phase 5: Memory Layer 기록

작업 결과를 `.claude/memory.json`에 추가한다.

```json
{
  "issue": "SB-XXX",
  "task": "이슈 제목",
  "result": "success | fail",
  "reason": null,
  "fix": null,
  "tags": ["도메인", "키워드"],
  "pr": "#번호",
  "branch": "feature/SB-XXX-description",
  "date": "YYYY-MM-DD",
  "tests": 0,
  "validation": "APPROVE | REFINE | ESCALATE",
  "review": "APPROVE | REQUEST_CHANGES | REDESIGN",
  "notes": "리뷰 피드백 요약, 특이사항"
}
```

## 에러 핸들링

| 에러 유형 | 전략 |
|-----------|------|
| 이슈를 찾을 수 없음 | 사용자에게 확인 요청 |
| Plan Validator ESCALATE | 항상 사용자에게 보고 |
| 테스트 실패 (Developer) | Developer가 직접 수정 (최대 3회) |
| 리뷰 REQUEST_CHANGES | Developer 수정 → 재리뷰 (최대 2라운드) |
| 리뷰 REDESIGN | Planner 재호출 (최대 1회) |
