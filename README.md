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

로컬 개발 실행 전에는 `DATABASE_URL`을 반드시 설정해야 합니다.

```bash
export DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
python app.py
```

## 현재 코드가 하는 일

- 프론트엔드가 uSaint 로그인 페이지로 직접 이동한 뒤, 로그인 완료 후 `/auth/callback/.../timing/<authStartedAt>/<flowId>` 형태의 콜백에서 `sToken`, `sIdno`를 받습니다.
- uSaint SSO와 포털 페이지를 다시 조회해 학생 정보를 파싱합니다.
- 파싱한 학생 정보를 `usaint_students` 테이블에 upsert 합니다.
- 서비스 이용 동의 여부를 `service_consent` 컬럼에 저장합니다.
- PostgreSQL 연결은 `psycopg_pool` 기반 connection pool을 통해 재사용합니다.
- 앱 시작 시점에 DB 초기화가 한 번 수행되며, 사용자 요청 경로에서는 테이블 초기화 비용이 빠집니다.
- 인증 완료 시 Railway 로그에 전체 인증 시간과 서버 내부 처리 시간을 남깁니다.
- 결과는 성공/실패 HTML 페이지로 반환하며, 성공 시 카카오 오픈채팅방 링크로 이동할 수 있습니다.

## 환경변수

- `DATABASE_URL`: PostgreSQL 접속 문자열. `/auth/callback`에서 학생 정보를 저장할 때 필요
- `USAINT_LOGIN_PAGE_URL`: `/` 시작 페이지와 실패 후 "다시 인증하기" 버튼이 이동할 `gatitashu` 프론트 URL. 실패 시 `?step=auth&consent=true&authError=...`가 자동으로 붙습니다.
- `PORT`: 실행 포트
- `WEB_CONCURRENCY`: gunicorn worker 수. 기본값 `2`
- `DB_POOL_MIN_SIZE`: PostgreSQL connection pool 최소 연결 수. 기본값 `1`
- `DB_POOL_MAX_SIZE`: PostgreSQL connection pool 최대 연결 수. 기본값 `5`
- `DB_POOL_TIMEOUT_SECONDS`: connection pool에서 연결을 기다리는 최대 시간. 기본값 `10`

## 엔드포인트

- `GET /`
- `GET /auth/callback`
- `GET /auth/callback/consent-true`
- `GET /auth/callback/timing/<authStartedAt>/<flowId>`
- `GET /auth/callback/<consent_token>/timing/<authStartedAt>/<flowId>`

## 배포 실행 방식

Railway 배포 시 `Procfile`에 아래 명령이 정의되어 있습니다.

```bash
gunicorn -w ${WEB_CONCURRENCY:-2} -b 0.0.0.0:${PORT:-8000} app:app
```

Railway에서 `custom start command`를 사용 중이면 `Procfile`보다 그 설정이 우선됩니다. 현재 배포에서는 아래 명령을 사용하면 됩니다.

```bash
gunicorn -w ${WEB_CONCURRENCY:-2} -b 0.0.0.0:${PORT:-8000} app:app
```

## 로그

성공 시 아래 형태의 로그가 남습니다.

```text
usaint_auth event=auth_completed flow_id=... student_id=... auth_redirect_elapsed_ms=... fetch_elapsed_ms=... db_save_elapsed_ms=... db_pool_wait_elapsed_ms=... db_query_elapsed_ms=... db_commit_elapsed_ms=... callback_total_elapsed_ms=...
```

- `auth_redirect_elapsed_ms`: 사용자가 "유세인트 로그인하기" 버튼을 누른 시점부터 인증 완료 화면이 뜨기 직전까지의 전체 시간
- `fetch_elapsed_ms`: 서버가 uSaint를 다시 조회하고 학생 정보를 파싱하는 시간
- `db_save_elapsed_ms`: PostgreSQL upsert 시간
- `db_pool_wait_elapsed_ms`: connection pool에서 DB 연결을 얻는 시간
- `db_query_elapsed_ms`: `INSERT ... ON CONFLICT DO UPDATE` 실행 시간
- `db_commit_elapsed_ms`: PostgreSQL commit 시간
- `callback_total_elapsed_ms`: `/auth/callback` 요청이 들어온 뒤 서버가 응답을 만들기까지 걸린 시간
