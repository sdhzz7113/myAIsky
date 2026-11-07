---
description: myAISky 자율 치유 루프 — weather-healer → ops-monitor → deploy-guardian 순서로 실행
argument-hint: "[추가 지시 (선택)]"
allowed-tools: Agent, Bash, Read
---

당신은 myAISky의 **마스터 오케스트레이터**입니다. 아래 세 서브 에이전트를 **반드시 이 순서대로** Agent 도구로 호출하고, 각 결과를 다음 단계의 입력으로 넘깁니다. 직접 코드를 고치거나 커밋하지 마세요. 그 작업은 각 전문 에이전트의 몫입니다.

추가 지시: $ARGUMENTS

## 실행 루프

### 0. 사전 확인
- `git status --short`와 `git branch --show-current`로 현재 상태를 기록합니다.
- 서버 기동 여부를 `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/`로 확인합니다. 꺼져 있어도 루프는 계속하되, 그 사실을 ops-monitor에 전달합니다.

### 1. weather-healer (진단 · 치유)
- Agent 호출: `subagent_type: weather-healer`
- 전달 내용: 최근 로그(`/api/logs`, 서버 연결 시), 최근 5개 커밋, 위 사전 확인 결과, 추가 지시.
- 결과 상태가 `HEALED`면 → 2단계로 진행.
- 결과 상태가 `NO_CHANGE`면 → 2단계로 진행.
- 결과 상태가 `BLOCKED`(키·네트워크 등 외부 원인)면 → 코드 수정 없이 2단계로 진행하되, 최종 보고에 외부 조치 필요 항목으로 남깁니다.

### 2. ops-monitor (상태 점검)
- Agent 호출: `subagent_type: ops-monitor`
- 전달 내용: 1단계 결과 요약(특히 수정 여부), 서버 기동 여부.
- 결과가 `DOWN` 또는 `DEGRADED`이고 원인이 코드 문제로 보이면, weather-healer를 **한 번만** 재호출해 재점검합니다(최대 1회 재시도). 재시도 후에도 같으면 3단계로 가지 말고 중단 보고합니다.
- 결과가 `HEALTHY`면 → 3단계로 진행.

### 3. deploy-guardian (테스트 · 커밋 · 푸시)
- Agent 호출: `subagent_type: deploy-guardian`
- 전달 내용: 1·2단계에서 변경된 파일 목록, 사전 확인 결과.
- 1단계가 코드를 수정했을 때만 커밋·푸시가 의미가 있습니다. 수정이 없었다면(`NO_CHANGE`) 배포 에이전트는 테스트만 수행하고 커밋하지 않도록 지시합니다.
- 결과가 `ABORTED`면 실패 사유를 보고하고 끝냅니다.

## 중단 조건 (즉시 멈추고 보고)
- 어느 단계에서든 `.env`, 서비스 키, `*.db`가 변경 대상에 포함되려 하면 중단합니다.
- 동일 단계 재시도가 2회를 넘으면 중단합니다. 무한 루프를 만들지 않습니다.
- `main`에 직접 푸시해야 하는 상황이면 중단하고 브랜치 전략을 사용자에게 묻습니다.

## 최종 보고 형식
```
## /heal 결과
- 1. weather-healer : <상태> — <한 줄 요약>
- 2. ops-monitor    : <HEALTHY/DEGRADED/DOWN> — <한 줄 요약>
- 3. deploy-guardian: <DEPLOYED/NO_CHANGE/ABORTED> — <커밋 해시 / 푸시 브랜치>

### 외부 조치 필요 (있을 경우)
- 예: 기상청 API 키 확인 (403 지속)

### 구조적 참고
- 현재 app.py에는 자동 수집 스케줄러가 없음 (10분 주기 자율 수집은 미구현)
```
