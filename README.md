# iDRAC Batch Manager

> Dell 서버의 **iDRAC Service Tag 수집 → Static IP 일괄 변경 → IPMI enable / 계정 변경**을  웹 브라우저에서 처리하는 자동화 도구

![Python](https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688?style=flat-square&logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

---

## 개요

Dell iDRAC Manager는 다수의 Dell 서버를 한 번에 설정해야 하는 환경에서 수작업을 자동화하기 위해 만든 웹 기반 관리 도구입니다.

사전에 DHCP 서버를 이용해 임시 IP를 할당합니다. 
이 도구는 해당 대역을 스캔해 각 서버의 Service Tag를 수집하고, 미리 작성해둔 CSV 파일(Service Tag ↔ Static IP 매핑)을 불러와 racadm 명령으로 IP를 일괄 변경합니다.
이후 IPMI Over LAN 활성화, Power Hot Spare 설정, root 계정 변경까지 한 화면에서 처리할 수 있습니다.

---

## 주요 기능

| 메뉴 | 기능 |
|------|------|
| ⚙ **환경 설정** | iDRAC 로그인 계정 · 스캔 대역(CIDR) 설정 |
| 📡 **태그 수집** | 대역 전체 병렬 스캔 → Service Tag 수집 · CSV 저장 |
| 🔧 **Static IP** | CSV Import · 매핑 편집 · 서브넷/게이트웨이 설정 · 일괄/개별 IP 변경 |
| 🛠 **iDRAC 관리** | IPMI Over LAN · Power Hot Spare · root 계정 일괄 변경 |

- 최대 **256개 IP 동시 병렬 스캔**
- 모든 작업 결과를 **실시간 SSE 스트리밍 로그**로 확인
- 외부 JS 라이브러리 의존성 없는 **단일 HTML 파일** 프론트엔드

---

## 요구 사항

- Python 3.9 이상
- `racadm` CLI가 PATH에 등록되어 있어야 합니다
  - [Dell EMC OpenManage DRAC Tools](https://www.dell.com/support/home/ko-kr/drivers/driversdetails?driverid=72j5w) 설치 필요
- iDRAC 접근이 가능한 네트워크 환경

---

## 설치 및 실행

### 1. 저장소 클론

```bash
git clone https://github.com/<your-username>/idrac-manager.git
cd idrac-manager
```

### 2. 의존성 설치

```bash
pip install -r requirements.txt
```

**requirements.txt**
```
fastapi
uvicorn[standard]
python-multipart
```

### 3. 서버 실행

```bash
python server.py
```

실행 시 포트를 자동으로 탐색하여 브라우저에서 접속 주소를 출력합니다.

```
✔ iDRAC Manager running → http://localhost:8888
```

브라우저에서 출력된 주소로 접속합니다.

---

## 사용 방법

### STEP 01 — DHCP 서버 IP 할당 확인
<img width="1219" height="644" alt="2_DHCP 서버 IP 할당 " src="https://github.com/user-attachments/assets/71ded5b8-6955-43d3-b299-14de0e77be6e" />

서버 전원을 켜고 iDRAC 포트를 네트워크 스위치에 연결합니다.  
DHCP 서버(원격 부팅 관리 툴 등)에서 자동 할당된 IP 목록을 확인합니다.

### STEP 02 — 환경 설정
<img width="1900" height="811" alt="image" src="https://github.com/user-attachments/assets/e064280e-4638-4472-aade-9e3f20f7fc3b" />

```
계정명  : root
비밀번호: calvin   ← iDRAC 초기 기본값
스캔 대역: 192.168.0.0/24
```

좌측 메뉴 **환경 설정**에서 입력 후 **저장**을 클릭합니다.

### STEP 03 — 태그 수집 (스캔)
<img width="1653" height="706" alt="image" src="https://github.com/user-attachments/assets/f975f198-667d-4ea4-8e25-750d2a8ea883" />

좌측 메뉴 **태그 수집** → **스캔 시작**을 클릭합니다.  
실시간 로그에서 진행 상황을 확인하고, 스캔 완료 시 **Static IP 패널에 자동 반영**됩니다.

### STEP 04 — Static IP 설정
<img width="1653" height="716" alt="image" src="https://github.com/user-attachments/assets/630d33a0-0726-4d78-8ebd-00207a035af8" />

**① CSV 파일 작성**

아래 형식으로 CSV 파일을 작성합니다.

| A (StaticIP) | B (ServiceTag) |
|---|---|
| 10.101.107.132 | 8NFKTB4 |
| 10.101.107.133 | J267ZH3 |
| 10.101.107.134 | FTY3K54 |
| 10.101.107.135 | 21ZSL54 |

- 열 순서 무관 — `ServiceTag,StaticIP` 순서도 허용
- 헤더 행 있어도 없어도 자동 감지
- Service Tag: 대문자+숫자 7자리 (예: `8NFKTB4`)

**② CSV 업로드 및 서브넷 설정**
<img width="1654" height="710" alt="image" src="https://github.com/user-attachments/assets/9b2a172d-e6da-4cc9-a9db-2338945b8570" />

1. **Static IP** 패널 → 상단 섹션 펼치기
2. **공통 네트워크 설정**: 서브넷 마스크 · 게이트웨이 입력
3. **📤 CSV 파일 업로드** 클릭 → 작성한 파일 선택

**③ 전체 IP 변경 실행**
<img width="1655" height="705" alt="image" src="https://github.com/user-attachments/assets/1ebfd6fe-06e9-4177-994e-0f4e93504715" />

**🚀 전체 IP 변경** 버튼 클릭 → 확인 모달에서 **예** 클릭  
실시간 로그로 서버별 진행 상황을 모니터링합니다.

### STEP 05 — iDRAC 관리
<img width="1915" height="904" alt="image" src="https://github.com/user-attachments/assets/807d86f4-98c1-4cc9-b288-d59789a30598" />

좌측 메뉴 **iDRAC 관리**에서 아래 작업을 일괄 처리합니다.

| 기능 | 설명 |
|------|------|
| IPMI Over LAN | 활성화 / 비활성화 선택 후 실행 |
| Power Hot Spare | 활성화 / 비활성화 선택 후 실행 |
| ROOT 계정 변경 | 새 UserName · Password 입력 후 **🔑 변경 실행** |

> ⚠️ 계정 변경 후에는 **환경 설정**의 계정 정보도 함께 업데이트하세요.

---

## 프로젝트 구조

```
idrac-manager/
├── server.py          # FastAPI 백엔드 (REST + SSE)
├── index.html         # 단일 파일 웹 UI (Vanilla JS)
├── requirements.txt
├── README.md 
```

---

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| `GET` | `/api/state` | 현재 상태 조회 |
| `POST` | `/api/settings` | 계정 / 대역 저장 |
| `GET` | `/api/results` | 스캔 결과 목록 |
| `GET` | `/api/mapping` | 매핑 목록 |
| `POST` | `/api/mapping/entry` | 매핑 항목 수정 |
| `DELETE` | `/api/mapping/entry/{tag}` | 매핑 항목 삭제 |
| `POST` | `/api/mapping/load-scan` | 스캔 결과 → 매핑 로드 |
| `POST` | `/api/mapping/import-csv` | CSV Import |
| `GET` | `/api/mapping/export-csv` | 매핑 CSV 다운로드 |
| `GET` | `/api/results/export-csv` | 스캔 결과 CSV 다운로드 |
| `GET` | `/api/scan/stream` | 스캔 SSE 스트림 |
| `GET` | `/api/apply/stream` | 일괄 IP 변경 SSE |
| `GET` | `/api/apply/single/stream` | 단일 IP 변경 SSE |
| `POST` | `/api/manage/stream-start` | 관리 작업 SSE 채널 생성 |
| `GET` | `/api/manage/stream/{cid}` | 관리 작업 SSE 스트림 |

---

## 기술 스택

- **Backend**: [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/)
- **실시간 통신**: Server-Sent Events (SSE)
- **Frontend**: Vanilla JS — 외부 라이브러리 의존성 없음
- **racadm**: Dell DRAC Tools CLI (별도 설치 필요)

---

## 주의 사항

- 본 도구는 `racadm` CLI를 subprocess로 호출합니다. racadm이 설치되지 않은 환경에서는 스캔 및 IP 변경 기능이 동작하지 않습니다.
- 서버 상태(매핑, 스캔 결과)는 **메모리에만 저장**됩니다. 서버 재시작 시 초기화되므로 작업 전 **💾 매핑 저장**으로 CSV 백업을 권장합니다.
- IP 변경 작업은 되돌릴 수 없습니다. 변경 전 매핑 테이블을 반드시 확인하세요.

---

## License

MIT
