import os
from threading import Lock
from typing import Any
from urllib.parse import urlencode, urlparse

import psycopg
import requests
from bs4 import BeautifulSoup
from flask import Flask, redirect, request


SMART_ID_LOGIN_URL = "https://smartid.ssu.ac.kr/Symtra_sso/smln.asp"
U_SAINT_SSO_URL = "https://saint.ssu.ac.kr/webSSO/sso.jsp"
U_SAINT_PORTAL_URL = "https://saint.ssu.ac.kr/webSSUMain/main_student.jsp"
KAKAO_OPENCHAT_URL = "https://open.kakao.com/o/gDVK0oqi"
SUCCESS_MARKER = 'location.href = "/irj/portal";'
class UsaintAuthError(Exception):
    pass


class UsaintParseError(Exception):
    pass


class DatabaseError(Exception):
    pass


_db_init_lock = Lock()
_db_initialized = False


def create_app() -> Flask:
    app = Flask(__name__)
    app.json.ensure_ascii = False

    @app.get("/")
    def index() -> str:
        return (
            "<html><body>"
            "<h1>카카오 오픈채팅방 안내</h1>"
            "<p>이 페이지에는 추후 카카오 오픈채팅방 링크가 연결될 예정입니다.</p>"
            "</body></html>"
        )

    @app.get("/login")
    def login():
        consent = parse_bool(request.args.get("consent"))
        callback_url = build_callback_url(consent=consent)
        query = urlencode({"apiReturnUrl": callback_url})
        return redirect(f"{SMART_ID_LOGIN_URL}?{query}", code=302)

    @app.get("/auth/callback")
    @app.get("/auth/callback/<consent_token>")
    def auth_callback(consent_token: str | None = None):
        s_token = request.args.get("sToken", "").strip()
        s_idno = request.args.get("sIdno", "").strip()

        if not s_token or not s_idno:
            return render_result_page(
                title="인증 실패",
                message="sToken 또는 sIdno가 없습니다.",
                success=False,
                status_code=400,
            )

        try:
            student = fetch_student_info(s_token=s_token, s_idno=s_idno)
            student["service_consent"] = consent_token == "consent-true"
            save_student_info(student)
        except UsaintAuthError as exc:
            return render_result_page(
                title="인증 실패",
                message=str(exc),
                success=False,
                status_code=401,
            )
        except UsaintParseError as exc:
            return render_result_page(
                title="정보 파싱 실패",
                message=str(exc),
                success=False,
                status_code=502,
            )
        except requests.RequestException as exc:
            return render_result_page(
                title="유세인트 요청 실패",
                message=str(exc),
                success=False,
                status_code=502,
            )
        except DatabaseError as exc:
            return render_result_page(
                title="DB 저장 실패",
                message=str(exc),
                success=False,
                status_code=500,
            )

        return render_result_page(
            title="인증 완료",
            message="유세인트 인증과 학생 정보 저장이 완료되었습니다.",
            success=True,
        )

    return app


def ensure_db_initialized() -> None:
    global _db_initialized

    if _db_initialized:
        return

    with _db_init_lock:
        if _db_initialized:
            return

        init_db()
        _db_initialized = True


def build_callback_url(consent: bool = False) -> str:
    base_url = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
    if consent:
        return f"{base_url}/auth/callback/consent-true"

    return f"{base_url}/auth/callback"


def parse_bool(value: str | None) -> bool:
    if value is None:
        return False

    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def render_result_page(
    title: str,
    message: str,
    success: bool,
    status_code: int = 200,
):
    title_text = "본인 인증 완료" if success else title
    desc_text = (
        "인증이 완료되었습니다.<br>아래 버튼을 눌러 오픈채팅방에 입장해 주세요."
        if success
        else message.replace("\n", "<br>")
    )
    button_text = "카카오 오픈채팅방 입장하기" if success else "처음으로 돌아가기"
    button_href = KAKAO_OPENCHAT_URL if success else "/"
    icon_bg = "#0A1931" if success else "#ff4757"
    icon_symbol = """
      <svg width="36" height="36" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
        <polyline class="check-path" points="10,21 17,28 30,13"/>
      </svg>
    """ if success else """
      <svg width="36" height="36" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
        <path class="fail-path" d="M13 13 L27 27 M27 13 L13 27"/>
      </svg>
    """

    html = f"""
    <!doctype html>
    <html lang="ko">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{title}</title>
      <link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css" />
      <style>
        *{{box-sizing:border-box;margin:0;padding:0}}
        body{{background:#1a1a1a}}
        .wrap{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:2rem;font-family:'Pretendard Variable','Pretendard',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:linear-gradient(180deg,#0A1931 0%,#040A14 100%)}}
        .inner{{text-align:center;max-width:340px;width:100%;animation:fade-up 0.5s ease both;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.1);border-radius:24px;padding:2rem 1.5rem;box-shadow:0 18px 42px rgba(0,0,0,0.5)}}
        @keyframes fade-up{{from{{opacity:0;transform:translateY(14px)}}to{{opacity:1;transform:translateY(0)}}}}
        .icon-wrap{{
          width:80px;
          height:80px;
          border-radius: 50%;
          background:{icon_bg};
          display: flex;
          align-items: center;
          justify-content: center;
          margin:0 auto 2rem;
          position: relative;
          box-shadow:0 0 0 6px rgba(255,255,255,0.12), 0 10px 24px rgba(0,0,0,0.1);
        }}
        .ring-anim{{position:absolute;inset:-6px;border-radius:50%;border:3px solid #FCDA05;animation:ring-pulse 1.1s ease-out forwards}}
        @keyframes ring-pulse{{0%{{opacity:0.8;transform:scale(1)}}100%{{opacity:0;transform:scale(1.55)}}}}
        .check-path{{stroke:#fff;stroke-width:3.5;stroke-linecap:round;stroke-linejoin:round;fill:none;stroke-dasharray:60;stroke-dashoffset:60;animation:draw 0.5s ease forwards 0.3s}}
        .fail-path{{stroke:#fff;stroke-width:3.5;stroke-linecap:round;stroke-linejoin:round;fill:none;stroke-dasharray:60;stroke-dashoffset:60;animation:draw 0.5s ease forwards 0.3s}}
        @keyframes draw{{to{{stroke-dashoffset:0}}}}
        .title{{font-size:1.5rem;font-weight:700;color:#ffffff;line-height:1.4;margin-bottom:1rem;letter-spacing:-0.5px}}
        .desc{{font-size:0.875rem;color:#d1d5db;line-height:1.8;margin-bottom:2.35rem}}
        .btn{{display:block;width:100%;padding:16px;background:#FCDA05;color:#0A1931;border:none;border-radius:9999px;font-size:1.125rem;font-weight:700;font-family:'Pretendard Variable','Pretendard',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;cursor:pointer;transition:opacity 0.15s;letter-spacing:-0.3px;text-decoration:none;box-shadow:0 10px 24px rgba(252,218,5,0.4)}}
        .btn:hover{{opacity:0.85}}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="inner">
          <div class="icon-wrap">
            <div class="ring-anim"></div>
            {icon_symbol}
          </div>
          <p class="title">{title_text}</p>
          <p class="desc">{desc_text}</p>
          <a class="btn" href="{button_href}">{button_text}</a>
        </div>
      </div>
    </body>
    </html>
    """
    return html, status_code, {"Content-Type": "text/html; charset=utf-8"}


def get_database_url() -> str:
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        raise DatabaseError("DATABASE_URL environment variable is required.")
    return db_url


def connect_db() -> psycopg.Connection:
    db_url = get_database_url()
    parsed = urlparse(db_url)

    if parsed.scheme.startswith("postgresql") and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return psycopg.connect(
            dbname=(parsed.path or "").lstrip("/"),
            user=parsed.username,
            password=parsed.password,
            host=parsed.hostname or "localhost",
            port=parsed.port or 5432,
        )

    return psycopg.connect(db_url)


def init_db() -> None:
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS usaint_students (
        student_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        major TEXT NOT NULL,
        course_semester TEXT,
        year_semester TEXT,
        service_consent BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """
    add_consent_column_sql = """
    ALTER TABLE usaint_students
    ADD COLUMN IF NOT EXISTS service_consent BOOLEAN NOT NULL DEFAULT FALSE
    """

    try:
        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute(create_table_sql)
                cur.execute(add_consent_column_sql)
            conn.commit()
    except psycopg.Error as exc:
        raise DatabaseError(f"Failed to initialize database: {exc}") from exc


def save_student_info(student: dict[str, Any]) -> None:
    ensure_db_initialized()

    upsert_sql = """
    INSERT INTO usaint_students (
        student_id,
        name,
        major,
        course_semester,
        year_semester,
        service_consent
    ) VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (student_id) DO UPDATE
    SET
        name = EXCLUDED.name,
        major = EXCLUDED.major,
        course_semester = EXCLUDED.course_semester,
        year_semester = EXCLUDED.year_semester,
        service_consent = EXCLUDED.service_consent,
        updated_at = NOW()
    """

    try:
        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    upsert_sql,
                    (
                        student["student_id"],
                        student["name"],
                        student["major"],
                        student["course_semester"],
                        student["year_semester"],
                        student["service_consent"],
                    ),
                )
            conn.commit()
    except psycopg.Error as exc:
        raise DatabaseError(f"Failed to save student info: {exc}") from exc


def fetch_student_info(s_token: str, s_idno: str) -> dict[str, Any]:
    session = requests.Session()

    sso_response = session.get(
        U_SAINT_SSO_URL,
        params={"sToken": s_token, "sIdno": s_idno},
        headers={"Cookie": f"sToken={s_token}; sIdno={s_idno}"},
        timeout=10,
    )
    sso_response.raise_for_status()

    if SUCCESS_MARKER not in sso_response.text:
        raise UsaintAuthError("uSaint authentication failed.")

    portal_response = session.get(U_SAINT_PORTAL_URL, timeout=10)
    portal_response.raise_for_status()

    return parse_student_info(portal_response.text)


def parse_student_info(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    name_box = soup.select_one(".main_box09")
    info_box = soup.select_one(".main_box09_con")

    if name_box is None or info_box is None:
        raise UsaintParseError("Failed to locate the student info blocks.")

    name_span = name_box.find("span")
    if name_span is None:
        raise UsaintParseError("Failed to locate the student name.")

    student_name = name_span.get_text(strip=True)
    student_name = student_name.split("님")[0].strip()
    if not student_name:
        raise UsaintParseError("Student name is empty.")

    student_id = None
    major = None
    status = None
    course_semester = None
    year_semester = None
    raw_fields: dict[str, str] = {}

    for item in info_box.find_all("li"):
        dt = item.find("dt")
        strong = item.find("strong")
        if dt is None or strong is None:
            continue

        key = dt.get_text(strip=True)
        value = strong.get_text(strip=True)
        raw_fields[key] = value

        if key == "학번":
            try:
                student_id = int(value)
            except ValueError as exc:
                raise UsaintParseError("Student id is not a number.") from exc
        elif key in {"소속", "학과", "학부", "전공"}:
            major = value
        elif key in {"과정/학적", "과정/학기", "학적", "과정", "신분"}:
            status = value
            course_semester = value
        elif key in {"학년/학기", "학년", "학기"}:
            year_semester = value

    if student_id is None:
        raise UsaintParseError("Student id was not found.")
    if not major:
        raise UsaintParseError("Student major was not found.")

    return {
        "student_id": student_id,
        "name": student_name,
        "major": major,
        "status": status,
        "course_semester": course_semester,
        "year_semester": year_semester,
        "raw_fields": raw_fields,
    }


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
