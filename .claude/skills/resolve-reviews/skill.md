---
name: resolve-reviews
description: "이슈 번호로 관련 PR의 리뷰 코멘트를 확인하고, 충돌이 있으면 rebase로 해결하고, 수정이 필요하면 코드를 수정한 뒤 코멘트에 답변+resolve 처리한다."
---

# Resolve Reviews — PR 리뷰 코멘트 처리

이슈 번호를 입력받아, 관련 PR에 달린 리뷰 코멘트를 확인하고 수정/답변/resolve 처리하는 스킬.

## 입력

이슈 ID: $ARGUMENTS

## 워크플로우

### Phase 1: PR 식별
```bash
gh pr list --search "{이슈ID}" --json number,title,headRefName,state --jq '.[]'
```

### Phase 2: 리뷰 코멘트 수집
GraphQL로 미해결 review thread를 조회한다.

### Phase 3: 코멘트 분석 및 분류

| 분류 | 설명 | 액션 |
|------|------|------|
| **FIX** | 코드 수정이 필요 | 코드 수정 → 답변 → resolve |
| **ACK** | 타당하지만 범위 밖 | 답변만 → resolve |
| **SKIP** | 이미 수정됨 | 답변만 → resolve |

### Phase 3.5: 충돌 해결
PR이 `CONFLICTING` 상태이면 코드 수정 전에 먼저 충돌을 해결한다.
```bash
# PR 브랜치의 워크트리로 이동
cd {worktree_path}
git fetch origin main
git stash  # 미커밋 변경이 있으면
git rebase origin/main
# 충돌 파일 수동 해결 → git add → git rebase --continue
# uv.lock 충돌 시: git checkout --theirs uv.lock && uv lock
```
충돌 해결 후 FIX 항목 수정을 진행한다.

### Phase 4: 코드 수정 (FIX 항목)
수정 후 테스트/린트 통과 확인 → 커밋 & push

### Phase 5: 리뷰어 검증 (FIX 항목이 있는 경우)
Reviewer 에이전트 실행 → APPROVE 확인 (최대 2라운드)

### Phase 6: 코멘트 답변 및 Resolve
```bash
gh api repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies -f body="{답변}"
gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "{thread_id}"}) { thread { isResolved } } }'
```

### Phase 7: 요약 코멘트
PR에 전체 처리 내용을 요약하는 코멘트를 남긴다.

### Phase 8: Memory 기록
`.claude/memory.json`에 처리 내역을 반영한다.
