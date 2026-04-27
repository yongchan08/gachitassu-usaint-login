# gachitassu-usaint-login

uSaint 로그인 후 전달되는 `sToken`, `sIdno`를 이용해 학생 정보를 다시 조회하고 PostgreSQL에 저장하는 Flask 앱입니다.

## 사용 방법

```bash
git clone <REPOSITORY_URL>
cd gachitassu-usaint-login
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
python app.py
```

실행 전에 `DATABASE_URL`을 반드시 설정해야 합니다.

```bash
export DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
python app.py
```

## 현재 코드가 하는 일

- 프론트엔드가 uSaint 로그인 페이지로 직접 이동한 뒤, 로그인 완료 후 `/auth/callback` 또는 `/auth/callback/consent-true`에서 `sToken`, `sIdno`를 받습니다.
- uSaint SSO와 포털 페이지를 다시 조회해 학생 정보를 파싱합니다.
- 파싱한 학생 정보를 `usaint_students` 테이블에 upsert 합니다.
- 서비스 이용 동의 여부를 `service_consent` 컬럼에 저장합니다.
- 인증 완료 시 Railway 로그에 전체 인증 시간과 서버 내부 처리 시간을 남깁니다.
- 결과는 성공/실패 HTML 페이지로 반환하며, 성공 시 카카오 오픈채팅방 링크로 이동할 수 있습니다.

## 환경변수

- `DATABASE_URL`: PostgreSQL 접속 문자열. `/auth/callback`에서 학생 정보를 저장할 때 필요
- `PORT`: 실행 포트

## 엔드포인트

- `GET /`
- `GET /auth/callback`
- `GET /auth/callback/consent-true`

## 로그

성공 시 아래 형태의 로그가 남습니다.

```text
usaint_auth event=auth_completed flow_id=... student_id=... auth_redirect_elapsed_ms=... fetch_elapsed_ms=... db_save_elapsed_ms=... callback_total_elapsed_ms=...
```

- `auth_redirect_elapsed_ms`: 사용자가 "유세인트 로그인하기" 버튼을 누른 시점부터 인증 완료 화면이 뜨기 직전까지의 전체 시간
- `fetch_elapsed_ms`: 서버가 uSaint를 다시 조회하고 학생 정보를 파싱하는 시간
- `db_save_elapsed_ms`: PostgreSQL upsert 시간
- `callback_total_elapsed_ms`: `/auth/callback` 요청이 들어온 뒤 서버가 응답을 만들기까지 걸린 시간
