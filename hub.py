#!/usr/bin/env python3
"""GPU 모니터 hub — 에이전트 스냅샷을 받아 대시보드로 흘려보낸다.

스냅샷 내용을 해석하지 않는다. 받아서 보관하고 그대로 흘려보낼 뿐이라 에이전트가
무엇을 보내든(GPU 점유자·런 진행률·아직 없는 필드) hub 를 고칠 일이 없다.

에이전트가 POST /ingest 로 밀어 넣고, 브라우저는 GET /events (SSE)로 실시간
수신한다. SSE 가 막히면 대시보드가 GET /api/all 폴링으로 자동 폴백한다.

머신을 추가할 때 hub 는 건드리지 않는다 — 새 머신에서 agent.py 를 새 --label 로
띄우면 그 자리에서 카드가 하나 늘어난다.

사용:
  MONITOR_TOKEN=<공유토큰> python3 hub.py --port 8787
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import queue
import re
import shlex
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.join(HERE, "dashboard.html")
# GPU 서버에서 한 줄로 에이전트를 깔 때 install.sh 가 hub 에서 받아 가는 파일들.
INSTALL_SH = os.path.join(HERE, "install.sh")
DIST_FILES = ("agent.py", "run-agent.sh", "run-tunnel.sh", "watchdog.sh", "install.sh")
# 화면 언어(en 기본 | ko). 대시보드의 기본 언어가 되고, 설치 한 줄로 붙는 에이전트에도 전해진다.
LANG = os.environ.get("MONITOR_LANG") if os.environ.get("MONITOR_LANG") in ("en", "ko") else "en"


# install.sh hub 가 적어 둔 이 hub 의 주소들(쉼표로 구분).
PUBLIC_URLS = [u.strip().rstrip("/") for u in (os.environ.get("MONITOR_PUBLIC_URLS") or "").split(",")
               if re.fullmatch(r"https?://[A-Za-z0-9.\-:\[\]]+", u.strip().rstrip("/"))]


def say(en, ko):
    return ko if LANG == "ko" else en

# 배치 설정 파일. 토큰을 코드에 박지 않기 위해 여기서 읽는다.
CONFIG_FILE = os.path.join(HERE, "monitor.env")


def read_token(explicit, config_file=CONFIG_FILE):
    """토큰을 정한다. 우선순위: --token > $MONITOR_TOKEN > 설정 파일.

    설정 파일을 마지막 수단이 아니라 실질적 기본으로 두는 이유가 있다. tmux 로
    띄우면 클라이언트가 export 한 환경변수는 세션에 넘어가지 않는다 — tmux 는
    이미 떠 있는 서버의 환경을 쓴다. 명령줄에 토큰을 실으면 같은 머신의 누구나
    ps 로 본다. 파일 경로가 둘 다 피하는 유일한 길이다.
    """
    if explicit:
        return explicit
    from_env = os.environ.get("MONITOR_TOKEN")
    if from_env:
        return from_env
    try:
        with open(config_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "MONITOR_TOKEN":
                    return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


# 이 시간 넘게 소식이 없으면 offline 로 본다.
OFFLINE_AFTER_SEC = 12.0
# 머신별 시계열 보관 개수(2초 주기 기준 약 10분).
HISTORY_POINTS = 300
# 같은 요청(머신·종류·작업)은 이 간격 안에 한 번만 받는다. AI 식별은 한 번에 $0.1~0.2 든다.
COMMAND_COOLDOWN_SEC = 60.0
# SSE 큐가 이만큼 밀리면 그 구독자는 느린 것으로 보고 끊는다.
SSE_QUEUE_MAX = 32
# 구독자가 이 시간 동안 한 덩어리도 못 받아 가면(잠든 노트북·죽은 연결) 끊는다.
SSE_WRITE_TIMEOUT_SEC = 30.0
# 대시보드로 내보내는 주기. 머신이 보고할 때마다 보내면 3대일 때 2초에 3번 전체를
# 다시 그린다. 이 주기로 모아서, 그 사이 바뀐 게 있을 때만 한 번 보낸다.
BROADCAST_SEC = 2.0
# 실시간(SSE) 방송에는 시계열을 지난 방송 뒤 새 점만 싣는다(history_mode=delta).
# 전체(머신마다 300점)를 2초마다 보내면 탭 하나에 2초마다 230KB, 시간당 400MB 였다
# (2026-10-15). 처음 붙을 때와 /api/all 은 전체(history_mode=full)를 준다.


class Store:
    def __init__(self, token):
        self.token = token
        self._lock = threading.Lock()
        self._machines: dict[str, dict] = {}
        self._history: dict[str, list[dict]] = {}
        # 대시보드가 에이전트에게 보내는 요청(다시 식별 등). 에이전트는 NAT 뒤에 있을 수
        # 있어 hub 가 먼저 연결하지 못한다 — 다음 보고의 응답에 실어 보낸다.
        self._commands: dict[str, list[dict]] = {}
        self._command_at: dict[tuple, float] = {}
        self._subscribers: list[queue.Queue] = []
        self.started_at = time.time()
        self.ingest_count = 0
        # 응답 도중 클라이언트가 끊은 횟수(탭 닫기·새로고침·네트워크 끊김).
        self.client_drop_count = 0
        self._dirty = False
        self._sent_upto = None  # 지난 방송에 실은 시계열의 끝 시각
        threading.Thread(target=self._broadcast_loop, daemon=True).start()

    def _broadcast_loop(self):
        last = 0.0
        while True:
            time.sleep(BROADCAST_SEC)
            with self._lock:
                # 보고가 전부 끊겨도 가끔은 보낸다 — 그래야 '오프라인' 전환이 화면에 뜬다.
                if not self._dirty and time.time() - last < OFFLINE_AFTER_SEC / 2:
                    continue
                self._dirty = False
                since, self._sent_upto = self._sent_upto, time.time()
                payload = self._render_locked(since)
            last = time.time()
            self._broadcast(payload)

    def note_client_drop(self):
        with self._lock:
            self.client_drop_count += 1
            return self.client_drop_count

    # -- 에이전트 입력 ----------------------------------------------------
    def request(self, machine_id, command):
        """대시보드의 요청을 맡아 둔다. 'ok' | 'unknown' | 'cooldown'."""
        key = (machine_id, command.get("type"), command.get("job_id"))
        now = time.time()
        with self._lock:
            if machine_id not in self._machines:
                return "unknown"
            if now - self._command_at.get(key, 0) < COMMAND_COOLDOWN_SEC:
                return "cooldown"
            self._command_at[key] = now
            queue_ = self._commands.setdefault(machine_id, [])
            if command not in queue_:
                queue_.append(command)
            return "ok"

    def take_commands(self, machine_id):
        with self._lock:
            return self._commands.pop(machine_id, [])

    def ingest(self, snapshot):
        machine_id = str(snapshot.get("machine_id") or snapshot.get("label") or "unknown")
        now = time.time()
        with self._lock:
            self.ingest_count += 1
            snapshot["received_at"] = now
            self._machines[machine_id] = snapshot
            point = self._history_point(snapshot, now)
            series = self._history.setdefault(machine_id, [])
            series.append(point)
            if len(series) > HISTORY_POINTS:
                del series[: len(series) - HISTORY_POINTS]
            self._dirty = True

    @staticmethod
    def _history_point(snapshot, now):
        """시계열 한 점. GPU 는 장 수만큼 담는다.

        예전에는 gpus[0] 하나만 담아서 2장 이상인 머신의 나머지 카드는 스파크라인이
        통째로 비었다. 인덱스별로 담아 어느 장이든 추이가 남게 한다.
        """
        cpu = snapshot.get("cpu") or {}
        memory = snapshot.get("memory") or {}
        gpus = snapshot.get("gpus")
        gpus = gpus if isinstance(gpus, list) else []
        return {
            "t": now,
            "cpu_util": cpu.get("util_pct"),
            "cpu_temp": cpu.get("temp_c"),
            "ram_pct": memory.get("pct"),
            "gpus": [
                {"i": g.get("index"), "u": g.get("util_pct"),
                 "c": g.get("temp_c"), "v": g.get("vram_pct")}
                for g in gpus if isinstance(g, dict)
            ],
        }

    # -- 출력 ------------------------------------------------------------
    def _render_locked(self, since=None):
        """since 를 주면 시계열은 그 뒤 점만 싣는다 — 받는 쪽이 이어 붙인다."""
        now = time.time()
        machines = []
        for machine_id, snapshot in sorted(self._machines.items()):
            item = dict(snapshot)
            age = now - float(snapshot.get("received_at") or now)
            item["age_sec"] = age
            item["online"] = age <= OFFLINE_AFTER_SEC
            series = self._history.get(machine_id, [])
            item["history"] = (series if since is None
                               else [p for p in series if p["t"] > since])
            machines.append(item)
        return {
            "history_mode": "full" if since is None else "delta",
            "history_points": HISTORY_POINTS,
            "server_time": now,
            "hub_uptime_sec": now - self.started_at,
            "ingest_count": self.ingest_count,
            "offline_after_sec": OFFLINE_AFTER_SEC,
            "machines": machines,
        }

    def render(self):
        with self._lock:
            return self._render_locked()

    # -- SSE -------------------------------------------------------------
    @staticmethod
    def sse_chunk(payload):
        return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")

    def subscribe(self):
        q: queue.Queue = queue.Queue(maxsize=SSE_QUEUE_MAX)
        with self._lock:
            self._subscribers.append(q)
            snapshot = self._render_locked()  # 처음 한 번은 시계열 전체
        q.put_nowait(self.sse_chunk(snapshot))
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def is_subscribed(self, q):
        with self._lock:
            return q in self._subscribers

    def _broadcast(self, payload):
        with self._lock:
            targets = list(self._subscribers)
        if not targets:
            return
        chunk = self.sse_chunk(payload)  # 구독자 수와 상관없이 한 번만 직렬화한다
        for q in targets:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                self.unsubscribe(q)

    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gpu-monitor-hub/2.0"
    store: Store = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 요청마다 찍으면 시끄럽다
        pass

    def handle_one_request(self):  # noqa: N802
        # 클라이언트가 응답 도중 끊는 것은 정상이다(탭 닫기·새로고침). 이 예외가
        # 여기까지 올라가면 socketserver 가 트레이스백을 통째로 뱉으므로 여기서 끊는다.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
            self._note_drop()

    def _note_drop(self):
        n = self.store.note_client_drop()
        print(f"[hub] 클라이언트가 응답 중 끊음 ({self.client_address[0]}, 누적 {n})",
              flush=True)

    # -- 공통 ------------------------------------------------------------
    # CORS 헤더는 일부러 없다. 대시보드는 hub 가 같은 오리진에서 서빙하므로
    # 필요가 없고, Access-Control-Allow-Origin: * 를 달면 이 주소에 닿는 브라우저가
    # 연 아무 웹페이지나 /api/all 을 읽어간다(머신·계정·커맨드라인·경로까지).
    # 2026-09-23 제거. 되돌릴 일이 생기면 오리진을 명시해서 좁게 달아라.
    def _send(self, code, body, content_type="application/json; charset=utf-8",
              extra_headers=()):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in extra_headers:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
            self._note_drop()

    # -- 라우팅 ----------------------------------------------------------
    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") == "/api/reidentify":
            self._post_reidentify()
            return
        if self.path.rstrip("/") != "/ingest":
            self._send(404, json.dumps({"error": "not found"}))
            return
        if not hmac.compare_digest(self.headers.get("X-Monitor-Token") or "", self.store.token):
            self._send(403, json.dumps({"error": "bad token"}))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            snapshot = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, json.dumps({"error": f"bad payload: {exc}"}))
            return
        self.store.ingest(snapshot)
        machine_id = str(snapshot.get("machine_id") or snapshot.get("label") or "unknown")
        self._send(200, json.dumps({"ok": True, "commands": self.store.take_commands(machine_id)}))

    def _post_reidentify(self):
        """대시보드의 '다시 식별'. 인증이 없는 대시보드에서 오므로 두 가지로 막는다.
        JSON 만 받는다(다른 웹페이지가 폼·단순 요청으로 몰래 보내지 못한다), Origin 이
        있으면 이 hub 여야 한다. 같은 작업은 COMMAND_COOLDOWN_SEC 에 한 번만 받는다."""
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != (self.headers.get("Host") or ""):
            self._send(403, json.dumps({"error": "origin"}))
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._send(415, json.dumps({"error": "json only"}))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                raise ValueError("too large")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            machine_id, job_id = str(body["machine_id"])[:200], str(body["job_id"])[:200]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._send(400, json.dumps({"error": "bad request"}))
            return
        result = self.store.request(machine_id, {"type": "reidentify", "job_id": job_id})
        code = {"ok": 200, "unknown": 404, "cooldown": 429}[result]
        self._send(code, json.dumps({"ok": result == "ok", "result": result}))

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        path = path.rstrip("/") or "/"
        if path in ("/", "/index.html", "/dashboard.html"):
            self._serve_dashboard()
        elif path == "/install.sh":
            self._serve_install(query)
        elif path.startswith("/dist/") and path[len("/dist/"):] in DIST_FILES:
            self._serve_file(os.path.join(HERE, path[len("/dist/"):]), "text/plain; charset=utf-8")
        elif path == "/api/all":
            self._send(200, json.dumps(self.store.render(), ensure_ascii=False))
        elif path == "/api/health":
            self._send(200, json.dumps({
                "ok": True,
                "machines": len(self.store.render()["machines"]),
                "subscribers": self.store.subscriber_count(),
                "ingest_count": self.store.ingest_count,
                "client_drop_count": self.store.client_drop_count,
            }))
        elif path == "/events":
            self._serve_events()
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def _serve_install(self, query):
        """에이전트 설치 스크립트. 이 hub 의 주소와 토큰을 채워서 내준다.

        토큰을 맞게 준 요청에만 내준다 — 아니면 이 주소에 닿는 누구나 토큰을 얻는다.
        """
        # 거절도 200 + 오류를 알리는 스크립트로 준다. curl -f 는 4xx 본문을 버려서
        # 사용자가 이유를 못 본다.
        shell = "text/x-shellscript; charset=utf-8"
        given = (parse_qs(query).get("t") or [""])[0]
        if self.store.token and not hmac.compare_digest(given, self.store.token):
            msg = say("Wrong token — paste the exact line that ./install.sh hub printed on the hub server",
                      "토큰이 맞지 않습니다 — hub 서버에서 ./install.sh hub 가 알려 준 줄을 그대로 붙여 넣으세요")
            self._send(200, f"echo {shlex.quote(msg)} >&2; exit 1\n", shell)
            return
        host = self.headers.get("Host") or ""
        if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,255}", host):
            msg = say("Bad Host header", "Host 헤더가 올바르지 않습니다")
            self._send(200, f"echo {shlex.quote(msg)} >&2; exit 1\n", shell)
            return
        try:
            with open(INSTALL_SH, "r", encoding="utf-8") as fh:
                script = fh.read()
        except OSError:
            msg = say("install.sh is missing on the hub", "hub 에 install.sh 가 없습니다")
            self._send(500, f"echo {shlex.quote(msg)} >&2; exit 1\n", shell)
            return
        # 받아 간 주소를 맨 앞에, hub 의 다른 주소(사내망·Tailscale)를 뒤에 싣는다.
        # 에이전트는 닿는 쪽으로 보낸다 — 노트북처럼 망을 오가는 서버도 끊기지 않는다.
        urls = ["http://" + host] + [u for u in PUBLIC_URLS if u != "http://" + host]
        script = script.replace('SERVED_HUB=""', f"SERVED_HUB={shlex.quote(','.join(urls))}", 1)
        script = script.replace('SERVED_TOKEN=""', f"SERVED_TOKEN={shlex.quote(self.store.token)}", 1)
        script = script.replace('SERVED_LANG=""', f"SERVED_LANG={LANG}", 1)
        self._send(200, script, shell)

    def _serve_file(self, path, content_type):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send(404, json.dumps({"error": "not found"}))
            return
        self._send(200, body, content_type)

    def _serve_dashboard(self):
        try:
            with open(DASHBOARD, "rb") as fh:
                body = fh.read()
            body = body.replace(b'data-default-lang="en"', f'data-default-lang="{LANG}"'.encode(), 1)
        except OSError:
            self._send(500, say("dashboard.html not found", "dashboard.html 을 찾을 수 없습니다"),
                       "text/plain; charset=utf-8")
            return
        self._send(200, body, "text/html; charset=utf-8")

    def _serve_events(self):
        q = self.store.subscribe()
        # 여기서 나가면 소켓을 닫는다. keep-alive 로 열어 두면 브라우저는 끊긴 줄 모르고
        # 옛 화면에 멈춘다 — 닫아야 EventSource 가 오류를 받고 다시 붙는다.
        self.close_connection = True
        try:
            self.connection.settimeout(SSE_WRITE_TIMEOUT_SEC)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            # 큐가 밀려 구독이 해제됐으면 밀린 옛 스냅샷을 보내지 말고 바로 끊는다.
            while self.store.is_subscribed(q):
                try:
                    payload = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # 프록시/브라우저 타임아웃 방지
                    self.wfile.flush()
                    continue
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.store.unsubscribe(q)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--token", default="",
        help="에이전트와 맞춰야 하는 공유 토큰 "
             "(생략하면 $MONITOR_TOKEN, 그다음 monitor.env)")
    args = parser.parse_args()

    token = read_token(args.token)
    Handler.store = Store(token)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"[hub] http://{args.host}:{args.port}/  (대시보드)", flush=True)
    print("[hub] POST /ingest  GET /api/all  GET /events  GET /install.sh", flush=True)
    if not token:
        # 토큰을 코드에 박아두면 그게 곧 공개된 비밀이다. 비어 있으면 그 사실을
        # 숨기지 않고 알린다 — 사설망 안에서 쓰는 것을 전제한다.
        print("[hub] 경고: 토큰이 비었다. 이 주소에 닿는 누구나 push 할 수 있다 "
              "($MONITOR_TOKEN 을 설정해라)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[hub] 종료", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
