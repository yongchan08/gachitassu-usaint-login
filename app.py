import atexit
import logging
import os
import time
import uuid
from threading import Lock
from typing import Any

import psycopg
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, send_file
from markupsafe import escape
from psycopg_pool import ConnectionPool


U_SAINT_SSO_URL = "https://saint.ssu.ac.kr/webSSO/sso.jsp"
U_SAINT_PORTAL_URL = "https://saint.ssu.ac.kr/webSSUMain/main_student.jsp"
KAKAO_OPENCHAT_URL = "https://open.kakao.com/o/gNa8YUri"
SUCCESS_MARKER = 'location.href = "/irj/portal";'

class UsaintAuthError(Exception):
    pass


class UsaintParseError(Exception):
    pass


class DatabaseError(Exception):
    pass


_db_init_lock = Lock()
_db_initialized = False
_db_pool_lock = Lock()
_db_pool: ConnectionPool | None = None


def close_db_pool() -> None:
    global _db_pool

    if _db_pool is None:
        return

    with _db_pool_lock:
        if _db_pool is None:
            return

        _db_pool.close()
        _db_pool = None


atexit.register(close_db_pool)


def create_app() -> Flask:
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.logger.setLevel(logging.INFO)
    ensure_db_initialized()

    @app.get("/")
    def index() -> str:
        return render_start_page()

    @app.get("/openchat")
    def openchat_page() -> str:
        return render_openchat_page()

    @app.get("/assets/copy-icon.png")
    def copy_icon() -> Any:
        return send_file(
            os.path.join(app.root_path, "copy-icon.png"),
            mimetype="image/png",
            max_age=3600,
        )

    @app.get("/auth/callback")
    @app.get("/auth/callback/<consent_token>")
    @app.get("/auth/callback/timing/<auth_started_at_value>/<flow_id_value>")
    @app.get("/auth/callback/<consent_token>/timing/<auth_started_at_value>/<flow_id_value>")
    def auth_callback(
        consent_token: str | None = None,
        auth_started_at_value: str | None = None,
        flow_id_value: str | None = None,
    ):
        callback_started_at = time.perf_counter()
        s_token = request.args.get("sToken", "").strip()
        s_idno = request.args.get("sIdno", "").strip()
        flow_id = get_flow_id(flow_id_value)
        auth_started_at_ms = parse_auth_started_at_ms(
            auth_started_at_value or request.args.get("authStartedAt")
        )

        if not s_token or not s_idno:
            log_auth_event(
                app,
                flow_id,
                "callback_missing_credentials",
                total_elapsed_ms=elapsed_ms(callback_started_at),
                auth_redirect_elapsed_ms=get_auth_redirect_elapsed_ms(auth_started_at_ms),
            )
            return render_result_page(
                title="인증 실패",
                message="sToken 또는 sIdno가 없습니다.",
                success=False,
                status_code=400,
            )

        try:
            fetch_started_at = time.perf_counter()
            student = fetch_student_info(s_token=s_token, s_idno=s_idno)
            fetch_elapsed = elapsed_ms(fetch_started_at)
            student["service_consent"] = consent_token == "consent-true"
            save_started_at = time.perf_counter()
            db_metrics = save_student_info(student)
            save_elapsed = elapsed_ms(save_started_at)
        except UsaintAuthError as exc:
            log_auth_event(
                app,
                flow_id,
                "usaint_auth_failed",
                total_elapsed_ms=elapsed_ms(callback_started_at),
                auth_redirect_elapsed_ms=get_auth_redirect_elapsed_ms(auth_started_at_ms),
                error=str(exc),
            )
            return render_result_page(
                title="인증 실패",
                message=str(exc),
                success=False,
                status_code=401,
            )
        except UsaintParseError as exc:
            log_auth_event(
                app,
                flow_id,
                "usaint_parse_failed",
                total_elapsed_ms=elapsed_ms(callback_started_at),
                auth_redirect_elapsed_ms=get_auth_redirect_elapsed_ms(auth_started_at_ms),
                error=str(exc),
            )
            return render_result_page(
                title="정보 파싱 실패",
                message=str(exc),
                success=False,
                status_code=502,
            )
        except requests.RequestException as exc:
            log_auth_event(
                app,
                flow_id,
                "usaint_request_failed",
                total_elapsed_ms=elapsed_ms(callback_started_at),
                auth_redirect_elapsed_ms=get_auth_redirect_elapsed_ms(auth_started_at_ms),
                error=str(exc),
            )
            return render_result_page(
                title="유세인트 요청 실패",
                message=str(exc),
                success=False,
                status_code=502,
            )
        except DatabaseError as exc:
            log_auth_event(
                app,
                flow_id,
                "db_save_failed",
                total_elapsed_ms=elapsed_ms(callback_started_at),
                auth_redirect_elapsed_ms=get_auth_redirect_elapsed_ms(auth_started_at_ms),
                error=str(exc),
            )
            return render_result_page(
                title="DB 저장 실패",
                message=str(exc),
                success=False,
                status_code=500,
            )

        total_elapsed = elapsed_ms(callback_started_at)
        auth_redirect_elapsed = get_auth_redirect_elapsed_ms(auth_started_at_ms)
        log_auth_event(
            app,
            flow_id,
            "auth_completed",
            student_id=student["student_id"],
            total_elapsed_ms=total_elapsed,
            auth_redirect_elapsed_ms=auth_redirect_elapsed,
            fetch_elapsed_ms=fetch_elapsed,
            db_save_elapsed_ms=save_elapsed,
            db_pool_wait_elapsed_ms=db_metrics["db_pool_wait_elapsed_ms"],
            db_query_elapsed_ms=db_metrics["db_query_elapsed_ms"],
            db_commit_elapsed_ms=db_metrics["db_commit_elapsed_ms"],
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


def render_result_page(
    title: str,
    message: str,
    success: bool,
    status_code: int = 200,
):
    title_text = "본인 인증 완료" if success else title
    desc_text = (
        "인증이 완료되었습니다.<br><span class=\"desc-inline\">아래 계좌로 안내된 금액을 송금해 주세요.</span>"
        if success
        else message.replace("\n", "<br>")
    )
    transfer_notice = """
          <div class="account-card">
            <p class="account-label">입금 계좌</p>
            <div class="account-value-row">
              <div class="account-value-block">
                <span class="account-bank-name">카카오뱅크</span>
                <span class="account-value">79423230510</span>
                <span class="account-owner">예금주 양O원</span>
              </div>
              <button type="button" class="account-copy-button" data-copy-account="카카오뱅크 79423230510" aria-label="계좌번호 복사">
                <img class="account-copy-icon" src="/assets/copy-icon.png" alt="" />
              </button>
            </div>
            <div class="account-guide-box">
              <p class="account-guide-label">중요</p>
              <p class="account-guide">송금자명은 반드시 학번으로 변경해 주세요.</p>
              <p class="account-guide-example">ex) 20261234</p>
            </div>
            <div class="account-price">
              <p class="account-price-original">정가 4900원</p>
              <div class="account-price-next">
                <span class="account-price-arrow" aria-hidden="true">⤷</span>
                <div class="account-price-detail">
                  <p class="account-amount">할인가 2000원</p>
                  <p class="account-price-note">(서비스는 6/14까지 운영됩니다)</p>
                </div>
              </div>
            </div>
          </div>
    """ if success else ""
    button_text = "송금 완료 후 오픈채팅방 입장하기" if success else "다시 인증하기"
    button_href = "/openchat" if success else get_retry_url(message)
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
        .desc{{font-size:0.875rem;color:#d1d5db;line-height:1.8;margin-bottom:1rem}}
        .desc-inline{{white-space:nowrap}}
        .account-card{{margin-bottom:1.45rem;padding:1rem 1.1rem;border-radius:20px;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.12);box-shadow:0 12px 28px rgba(0,0,0,0.16)}}
        .account-label{{font-size:0.8rem;font-weight:700;letter-spacing:0.08em;color:#8fb7ff;text-transform:uppercase;margin-bottom:0.55rem}}
        .account-value-row{{position:relative;display:flex;align-items:center;justify-content:center;gap:0.5rem;padding:1.1rem 3.9rem 1.1rem 1rem;border-radius:16px;background:rgba(10,25,49,0.72)}}
        .account-value-block{{display:flex;flex-direction:column;align-items:center;gap:0.2rem;text-align:center}}
        .account-bank-name{{font-size:1.25rem;font-weight:800;line-height:1.4;color:#ffffff;letter-spacing:-0.02em}}
        .account-value{{font-size:1.25rem;font-weight:800;line-height:1.4;color:#ffffff;letter-spacing:-0.02em}}
        .account-owner{{font-size:0.8rem;font-weight:700;line-height:1.4;color:#8fb7ff;white-space:nowrap}}
        .account-copy-button{{position:absolute;right:0.7rem;top:50%;transform:translateY(-50%);display:inline-flex;align-items:center;justify-content:center;width:48px;height:48px;padding:0;border:none;border-radius:10px;background:transparent;cursor:pointer;transition:transform 0.15s ease, background 0.15s ease;flex:0 0 auto}}
        .account-copy-button:hover{{transform:translateY(calc(-50% - 1px));background:rgba(255,255,255,0.08)}}
        .account-copy-icon{{width:32px;height:32px;display:block;flex:0 0 auto;filter:brightness(0) invert(1)}}
        .account-guide-box{{margin-top:0.8rem;padding:0.85rem 0.8rem;border-radius:16px;background:rgba(252,218,5,0.14);border:1px solid rgba(252,218,5,0.42);box-shadow:0 10px 24px rgba(252,218,5,0.1)}}
        .account-guide-label{{font-size:0.75rem;font-weight:800;letter-spacing:0.12em;color:#FCDA05;text-transform:uppercase;margin-bottom:0.35rem}}
        .account-guide{{font-size:0.78rem;font-weight:800;line-height:1.35;color:#ffffff;letter-spacing:-0.03em;white-space:nowrap}}
        .account-guide-example{{margin-top:0.35rem;font-size:0.72rem;font-weight:700;line-height:1.4;color:#f5e7a1}}
        .account-price{{margin-top:0.7rem;display:flex;flex-direction:column;align-items:center;gap:0.05rem;width:fit-content;margin-left:auto;margin-right:auto}}
        .account-price-original{{align-self:flex-start;font-size:0.9rem;font-weight:600;line-height:1.4;color:#9ca3af;text-decoration:line-through;transform:translateX(-1.75rem)}}
        .account-price-next{{position:relative;display:flex;align-items:flex-start;justify-content:center;margin-top:-0.05rem}}
        .account-price-arrow{{position:absolute;left:-0.65rem;top:0.02rem;font-size:1.15rem;font-weight:800;line-height:1;color:#d1d5db}}
        .account-price-detail{{display:flex;flex-direction:column;align-items:center;gap:0.05rem;text-align:center}}
        .account-amount{{font-size:1.05rem;font-weight:800;line-height:1.4;color:#FCDA05}}
        .account-price-note{{margin-top:0.36rem;font-size:0.68rem;font-weight:700;line-height:1.35;color:#9ca3af}}
        .btn{{display:block;width:100%;padding:16px;background:#FCDA05;color:#0A1931;border:none;border-radius:9999px;font-size:1.125rem;font-weight:700;font-family:'Pretendard Variable','Pretendard',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;cursor:pointer;transition:opacity 0.15s;letter-spacing:-0.3px;text-decoration:none;box-shadow:0 10px 24px rgba(252,218,5,0.4)}}
        .btn:hover{{opacity:0.85}}
        .toast{{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(16px);padding:0.85rem 1.1rem;border-radius:9999px;background:rgba(10,25,49,0.96);border:1px solid rgba(252,218,5,0.32);color:#ffffff;font-size:0.9rem;font-weight:700;line-height:1.4;box-shadow:0 16px 40px rgba(0,0,0,0.35);opacity:0;pointer-events:none;transition:opacity 0.2s ease, transform 0.2s ease}}
        .toast.is-visible{{opacity:1;transform:translateX(-50%) translateY(0)}}
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
          {transfer_notice}
          <a class="btn" href="{button_href}">{button_text}</a>
        </div>
      </div>
      <div class="toast" data-copy-toast aria-live="polite"></div>
      <script>
        const copyButton = document.querySelector('[data-copy-account]');
        const copyToast = document.querySelector('[data-copy-toast]');
        let copyToastTimer;

        if (copyButton && copyToast) {{
          const showToast = (message) => {{
            copyToast.textContent = message;
            copyToast.classList.add('is-visible');

            if (copyToastTimer) {{
              window.clearTimeout(copyToastTimer);
            }}

            copyToastTimer = window.setTimeout(() => {{
              copyToast.classList.remove('is-visible');
            }}, 2200);
          }};

          copyButton.addEventListener('click', async () => {{
            const accountText = copyButton.getAttribute('data-copy-account') || '';

            try {{
              await navigator.clipboard.writeText(accountText);
              showToast('계좌번호가 복사되었습니다.');
            }} catch (error) {{
              showToast('복사에 실패했습니다. 직접 길게 눌러 복사해 주세요.');
            }}
          }});
        }}
      </script>
    </body>
    </html>
    """
    return html, status_code, {"Content-Type": "text/html; charset=utf-8"}


def render_openchat_page():
    html = f"""
    <!doctype html>
    <html lang="ko">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>오픈채팅방 입장 안내</title>
      <link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css" />
      <style>
        *{{box-sizing:border-box;margin:0;padding:0}}
        body{{background:#1a1a1a}}
        .wrap{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:2rem;font-family:'Pretendard Variable','Pretendard',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:linear-gradient(180deg,#0A1931 0%,#040A14 100%)}}
        .inner{{text-align:center;max-width:340px;width:100%;animation:fade-up 0.5s ease both;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.1);border-radius:24px;padding:2rem 1.5rem;box-shadow:0 18px 42px rgba(0,0,0,0.5)}}
        @keyframes fade-up{{from{{opacity:0;transform:translateY(14px)}}to{{opacity:1;transform:translateY(0)}}}}
        .icon-wrap{{width:80px;height:80px;border-radius:50%;background:#0A1931;display:flex;align-items:center;justify-content:center;margin:0 auto 2rem;position:relative;box-shadow:0 0 0 6px rgba(255,255,255,0.12), 0 10px 24px rgba(0,0,0,0.1)}}
        .ring-anim{{position:absolute;inset:-6px;border-radius:50%;border:3px solid #FCDA05;animation:ring-pulse 1.1s ease-out forwards}}
        @keyframes ring-pulse{{0%{{opacity:0.8;transform:scale(1)}}100%{{opacity:0;transform:scale(1.55)}}}}
        .check-path{{stroke:#fff;stroke-width:3.5;stroke-linecap:round;stroke-linejoin:round;fill:none;stroke-dasharray:60;stroke-dashoffset:60;animation:draw 0.5s ease forwards 0.3s}}
        @keyframes draw{{to{{stroke-dashoffset:0}}}}
        .title{{font-size:1.5rem;font-weight:700;color:#ffffff;line-height:1.4;margin-bottom:1rem;letter-spacing:-0.5px}}
        .desc{{font-size:0.875rem;color:#d1d5db;line-height:1.8;margin-bottom:1rem}}
        .password-card{{margin-bottom:2rem;padding:1rem 1.1rem 1.05rem;border-radius:20px;background:linear-gradient(135deg,rgba(252,218,5,0.2),rgba(252,218,5,0.08));border:1px solid rgba(252,218,5,0.45);box-shadow:0 12px 28px rgba(252,218,5,0.12)}}
        .password-label{{font-size:0.8rem;font-weight:700;letter-spacing:0.08em;color:#FCDA05;text-transform:uppercase;margin-bottom:0.45rem}}
        .password-value{{font-size:2rem;font-weight:800;line-height:1;color:#ffffff;letter-spacing:0.08em}}
        .btn{{display:block;width:100%;padding:16px;background:#FCDA05;color:#0A1931;border:none;border-radius:9999px;font-size:1.125rem;font-weight:700;font-family:'Pretendard Variable','Pretendard',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;cursor:pointer;transition:opacity 0.15s;letter-spacing:-0.3px;text-decoration:none;box-shadow:0 10px 24px rgba(252,218,5,0.4)}}
        .btn:hover{{opacity:0.85}}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="inner">
          <div class="icon-wrap">
            <div class="ring-anim"></div>
            <svg width="36" height="36" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
              <polyline class="check-path" points="10,21 17,28 30,13"/>
            </svg>
          </div>
          <p class="title">오픈채팅방 입장 안내</p>
          <p class="desc">아래 비밀번호를 확인한 뒤<br>카카오톡 오픈채팅방에 입장해 주세요.</p>
          <div class="password-card">
            <p class="password-label">오픈채팅방 비밀번호</p>
            <p class="password-value">5353</p>
          </div>
          <a class="btn" href="{KAKAO_OPENCHAT_URL}">카카오 오픈채팅방 입장하기</a>
        </div>
      </div>
    </body>
    </html>
    """
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


def get_usaint_login_page_url() -> str:
    return os.environ.get("USAINT_LOGIN_PAGE_URL", "").strip()


def get_retry_url(message: str) -> str:
    login_page_url = get_usaint_login_page_url()
    if not login_page_url:
        return "/"

    retry_url = requests.PreparedRequest()
    retry_url.prepare_url(
        login_page_url,
        {
            "step": "auth",
            "consent": "true",
        },
    )
    return retry_url.url or login_page_url


def render_start_page():
    login_page_url = get_usaint_login_page_url()
    login_button = (
        f'<a class="btn" href="{escape(login_page_url)}">유세인트 로그인하기</a>'
        if login_page_url
        else '<div class="notice">`USAINT_LOGIN_PAGE_URL` 환경변수를 설정하면 로그인 버튼이 활성화됩니다.</div>'
    )

    html = f"""
    <!doctype html>
    <html lang="ko">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>유세인트 인증 시작</title>
      <link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css" />
      <style>
        *{{box-sizing:border-box;margin:0;padding:0}}
        body{{background:#1a1a1a}}
        .wrap{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:2rem;font-family:'Pretendard Variable','Pretendard',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:linear-gradient(180deg,#0A1931 0%,#040A14 100%)}}
        .inner{{text-align:center;max-width:360px;width:100%;animation:fade-up 0.5s ease both;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.1);border-radius:24px;padding:2rem 1.5rem;box-shadow:0 18px 42px rgba(0,0,0,0.5)}}
        @keyframes fade-up{{from{{opacity:0;transform:translateY(14px)}}to{{opacity:1;transform:translateY(0)}}}}
        .icon-wrap{{width:80px;height:80px;border-radius:50%;background:#0A1931;display:flex;align-items:center;justify-content:center;margin:0 auto 2rem;position:relative;box-shadow:0 0 0 6px rgba(255,255,255,0.12), 0 10px 24px rgba(0,0,0,0.1)}}
        .ring-anim{{position:absolute;inset:-6px;border-radius:50%;border:3px solid #FCDA05;animation:ring-pulse 1.1s ease-out forwards}}
        @keyframes ring-pulse{{0%{{opacity:0.8;transform:scale(1)}}100%{{opacity:0;transform:scale(1.55)}}}}
        .title{{font-size:1.5rem;font-weight:700;color:#ffffff;line-height:1.4;margin-bottom:1rem;letter-spacing:-0.5px}}
        .desc{{font-size:0.9375rem;color:#d1d5db;line-height:1.8;margin-bottom:2.35rem}}
        .btn{{display:block;width:100%;padding:16px;background:#FCDA05;color:#0A1931;border:none;border-radius:9999px;font-size:1.125rem;font-weight:700;font-family:'Pretendard Variable','Pretendard',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;cursor:pointer;transition:opacity 0.15s;letter-spacing:-0.3px;text-decoration:none;box-shadow:0 10px 24px rgba(252,218,5,0.4)}}
        .btn:hover{{opacity:0.85}}
        .notice{{font-size:0.875rem;color:#d1d5db;line-height:1.7;padding:1rem;border-radius:16px;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.08)}}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="inner">
          <div class="icon-wrap">
            <div class="ring-anim"></div>
            <svg width="36" height="36" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
              <path d="M13 15h14M13 20h14M13 25h9" stroke="#fff" stroke-width="3.5" stroke-linecap="round"/>
            </svg>
          </div>
          <p class="title">유세인트 인증 시작</p>
          <p class="desc">아래 버튼을 눌러 유세인트 로그인 후 본인 인증을 진행해 주세요.</p>
          {login_button}
        </div>
      </div>
    </body>
    </html>
    """
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


def get_flow_id(flow_id_value: str | None = None) -> str:
    raw_flow_id = (flow_id_value or request.args.get("flowId", "")).strip()
    return raw_flow_id or uuid.uuid4().hex[:12]


def parse_auth_started_at_ms(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None

    raw_value = raw_value.strip()
    if not raw_value:
        return None

    try:
        return int(raw_value)
    except ValueError:
        return None


def elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def get_auth_redirect_elapsed_ms(auth_started_at_ms: int | None) -> int | None:
    if auth_started_at_ms is None:
        return None

    now_ms = int(time.time() * 1000)
    elapsed = now_ms - auth_started_at_ms
    return elapsed if elapsed >= 0 else None


def log_auth_event(
    app: Flask,
    flow_id: str,
    event: str,
    *,
    student_id: int | None = None,
    total_elapsed_ms: int | None = None,
    auth_redirect_elapsed_ms: int | None = None,
    fetch_elapsed_ms: int | None = None,
    db_save_elapsed_ms: int | None = None,
    db_pool_wait_elapsed_ms: int | None = None,
    db_query_elapsed_ms: int | None = None,
    db_commit_elapsed_ms: int | None = None,
    error: str | None = None,
) -> None:
    parts = [f"event={event}", f"flow_id={flow_id}"]

    if student_id is not None:
        parts.append(f"student_id={student_id}")
    if auth_redirect_elapsed_ms is not None:
        parts.append(f"auth_redirect_elapsed_ms={auth_redirect_elapsed_ms}")
    if fetch_elapsed_ms is not None:
        parts.append(f"fetch_elapsed_ms={fetch_elapsed_ms}")
    if db_save_elapsed_ms is not None:
        parts.append(f"db_save_elapsed_ms={db_save_elapsed_ms}")
    if db_pool_wait_elapsed_ms is not None:
        parts.append(f"db_pool_wait_elapsed_ms={db_pool_wait_elapsed_ms}")
    if db_query_elapsed_ms is not None:
        parts.append(f"db_query_elapsed_ms={db_query_elapsed_ms}")
    if db_commit_elapsed_ms is not None:
        parts.append(f"db_commit_elapsed_ms={db_commit_elapsed_ms}")
    if total_elapsed_ms is not None:
        parts.append(f"callback_total_elapsed_ms={total_elapsed_ms}")
    if error:
        parts.append(f'error="{error}"')

    app.logger.info("usaint_auth %s", " ".join(parts))


def get_database_url() -> str:
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        raise DatabaseError("DATABASE_URL environment variable is required.")
    return db_url


def get_db_pool() -> ConnectionPool:
    global _db_pool

    if _db_pool is not None:
        return _db_pool

    with _db_pool_lock:
        if _db_pool is not None:
            return _db_pool

        _db_pool = ConnectionPool(
            conninfo=get_database_url(),
            min_size=int(os.environ.get("DB_POOL_MIN_SIZE", "1")),
            max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "5")),
            timeout=float(os.environ.get("DB_POOL_TIMEOUT_SECONDS", "10")),
        )
        return _db_pool


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
        with get_db_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(create_table_sql)
                cur.execute(add_consent_column_sql)
            conn.commit()
    except psycopg.Error as exc:
        raise DatabaseError(f"Failed to initialize database: {exc}") from exc


def save_student_info(student: dict[str, Any]) -> dict[str, int]:
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
        pool_wait_started_at = time.perf_counter()
        with get_db_pool().connection() as conn:
            pool_wait_elapsed = elapsed_ms(pool_wait_started_at)
            query_started_at = time.perf_counter()
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
            query_elapsed = elapsed_ms(query_started_at)
            commit_started_at = time.perf_counter()
            conn.commit()
            commit_elapsed = elapsed_ms(commit_started_at)
    except psycopg.Error as exc:
        raise DatabaseError(f"Failed to save student info: {exc}") from exc

    return {
        "db_pool_wait_elapsed_ms": pool_wait_elapsed,
        "db_query_elapsed_ms": query_elapsed,
        "db_commit_elapsed_ms": commit_elapsed,
    }


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
