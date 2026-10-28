#!/usr/bin/env python3
"""GPU 모니터 에이전트 — 머신 상태와 GPU 점유자를 hub 로 push 한다.

특정 프로젝트에 묶이지 않는다. GPU 를 잡고 있는 것이 무엇이든(학습·추론·
렌더링·다른 사용자 세션·컨테이너) nvidia-smi 와 /proc 에서 실제 점유자를 찾아
이름·사용자·명령줄째로 보고한다. "GPU 가 비었나, 아니면 무엇이 물고 있나"에
답하는 것이 이 에이전트의 전부다.

학습 진행률은 선택 기능이다. --watch 로 런 디렉터리를 주면 그 안의 JSONL 로그를
필드 별칭으로 읽는다. 로그 스키마를 미리 알 필요가 없다 — step/loss/lr 류는
별칭으로 찾고, 나머지 수치 키는 전부 '지표'로 그대로 올린다.

표준 라이브러리만 쓴다 — 감시 대상 머신의 파이썬 환경을 건드리지 않는다.

push 방식인 이유: NAT·방화벽 뒤(WSL2, 사무실 LAN)의 머신도 밖으로 나가는 방향은
열려 있다. 머신을 추가할 때 포트포워딩·방화벽 작업이 필요 없다.

사용:
  python3 agent.py --hub http://HOST:8787 --label gpu-a
  python3 agent.py --hub http://HOST:8787 --label gpu-a --watch '~/runs/*'
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

AGENT_VERSION = "2.12.0"

# 화면에 보이는 문구의 언어: en(기본) | ko. 에이전트가 만드는 문구(빈 GPU 사유·AI 상태)와
# AI 가 쓰는 작업 설명이 이 언어로 나온다. main() 이 --lang / $MONITOR_LANG 으로 정한다.
LANG = "en"
MSG = {
    "workers": ("{n} workers", "워커 {n}"),
    "others": ("+{n} more", "외 {n}개"),
    "graphics": ("graphics {who}", "그래픽 {who}"),
    "no_proc": ("in use, but no process found", "점유 중이지만 프로세스를 찾지 못했습니다"),
    "free": ("available", "사용 가능"),
    "free_graphics": ("available ({n} graphics, VRAM {mb:.0f}MB)", "사용 가능 (그래픽 {n}개, VRAM {mb:.0f}MB)"),
    "free_opaque": ("available (excluding {mb:.0f}MB always in use)", "사용 가능 (상시 점유 {mb:.0f}MB 제외)"),
    "paused": (" · paused (SIGSTOP)", " · 일시정지(정지 신호)"),
    "idle_now": (" — GPU idle right now (util {u:.0f}%, VRAM {mb:.0f}MB)",
                 " — 지금 GPU는 쉬는 중 (util {u:.0f}%, VRAM {mb:.0f}MB)"),
    "free_guess": ("available (guess) · ", "사용 가능(추정) · "),
    "driver_mismatch": ("driver mismatch · reboot needed", "드라이버 불일치 · 재부팅 필요"),
    "smi_failed": ("nvidia-smi failed: {text}", "nvidia-smi 실패: {text}"),
    "smi_no_gpu": ("nvidia-smi reports no GPU", "nvidia-smi가 GPU를 보고하지 않습니다"),
    "ai_checking": ("checking", "확인 중"),
    "ai_failed": ("identification failed: {err}", "식별 실패: {err}"),
    "ai_cmd_missing": ("no claude at MONITOR_AI_CMD: {path}", "MONITOR_AI_CMD에 claude가 없습니다: {path}"),
    "ai_need_install": ("Claude Code is not installed — curl -fsSL https://claude.ai/install.sh | bash",
                        "Claude Code가 없습니다 — 설치: curl -fsSL https://claude.ai/install.sh | bash"),
    "ai_not_installed": ("Claude Code is not installed (turns on by itself once installed and logged in)",
                         "Claude Code가 없습니다 (설치하고 로그인하면 자동으로 켜집니다)"),
    "ai_no_key": ("MONITOR_AI_AUTH=api_key but ANTHROPIC_API_KEY is not set",
                  "MONITOR_AI_AUTH=api_key인데 ANTHROPIC_API_KEY가 없습니다"),
    "ai_api_key": ("API key", "API 키"),
    "ai_need_login": ("Claude login needed — on this server run: claude auth login",
                      "Claude 로그인이 필요합니다 — 이 서버에서 claude auth login"),
    "ai_sub": ("claude.ai subscription ({plan})", "claude.ai 구독 ({plan})"),
    "ai_login": ("claude.ai login", "claude.ai 로그인"),
    "ai_too_old": ("Claude Code is too old — on this server run: claude update",
                   "Claude Code가 오래됐습니다 — 이 서버에서 claude update"),
    "ai_off": ("turned off (MONITOR_AI=off)", "꺼 둠 (MONITOR_AI=off)"),
    "ai_no_json": ("no result JSON in the answer", "답에서 결과 JSON을 찾지 못했습니다"),
    "ai_exit": ("claude exited with code {code}", "claude 종료 코드 {code}"),
    "ai_error": ("error", "오류"),
    "ai_unknown": ("unknown error", "알 수 없는 오류"),
}


def T(key, **kw):
    en, ko = MSG[key]
    return (ko if LANG == "ko" else en).format(**kw)
# 스냅샷 스키마. 2 부터 학습 전용 필드(train_processes/eval_processes)가 없고
# GPU 점유자 목록(gpu_processes)이 정본이다.
SCHEMA = 2

# --------------------------------------------------------------------------
# GPU 를 '비어있다'고 부르기 위한 기준
#
# 질문은 "완전히 0인가"가 아니라 "지금 작업을 띄울 수 있나"다. 점유자를 이름으로
# 특정할 수 있으면 그 목록이 판정의 근거고, VRAM·util 기준선은 특정에 실패했을
# 때의 최후 수단이다. 그래서 기준선은 '프로세스를 열거할 수 있나'로 갈린다.
#   - 열거 가능(네이티브 리눅스): 유휴 VRAM 이 100MB 수준이라 좁게 잡는다.
#   - 열거 불가(WSL2): 창 관리자·컴포지터가 상시 1GB 내외를 쓰는데 그걸 점유로
#     읽으면 GPU 가 영원히 busy 로 보인다. 기준선을 올리고 util 로 판단한다.
# --------------------------------------------------------------------------
FREE_VRAM_MB = 512.0
FREE_VRAM_MB_OPAQUE = 2560.0
FREE_UTIL_PCT = 25.0

# GPU 디바이스 노드를 열고 있으면 그 프로세스는 GPU 사용자다. nvidia-smi 가
# 프로세스를 열거하지 못하는 환경(WSL2)과 그래픽 전용 프로세스를 이 경로로 잡는다.
# /dev/dri 는 일부러 뺐다 — iGPU 사용자까지 섞여 들어와 엉뚱한 카드에 붙는다.
GPU_DEV_PREFIXES = ("/dev/nvidia", "/dev/dxg", "/dev/nvgpu")
# CUDA/컴퓨트는 UVM 을 연다. 이 노드가 없으면 그래픽 전용으로 본다 —
# 데스크톱 컴포지터가 '작업 점유'로 오인되지 않게 하는 갈림길이다.
COMPUTE_DEV_HINTS = ("/dev/nvidia-uvm", "/dev/dxg", "/dev/nvgpu")
# fd 스캔 비용 상한. 프로세스 하나가 fd 를 수천 개 물고 있어도 여기서 끊는다.
FD_SCAN_MAX_FDS = 256
FD_SCAN_BUDGET_SEC = 0.5
# /proc 전체를 훑는 주기. 그 사이에는 지난 스캔에서 찾은 점유자만 다시 확인한다.
# 네이티브 리눅스의 새 컴퓨트 점유자는 nvidia-smi 가 바로 잡으므로, 늦게 잡히는 것은
# 그래픽 프로세스와 WSL2 의 새 점유자뿐이다(늦어도 이 시간).
FD_SCAN_FULL_SEC = 10.0
# 한 GPU 에 붙여 보낼 점유자 수 상한.
MAX_PROCS_PER_GPU = 12

# WSL2 에서 Windows 실행 파일은 PATH 에 없을 수 있다(tmux 등 비로그인 셸).
WINDOWS_POWERSHELL_CANDIDATES = (
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/Windows/system32/WindowsPowerShell/v1.0/powershell.exe",
    "powershell.exe",
)

# --------------------------------------------------------------------------
# 런 로그(선택 기능) — 스키마를 모르는 JSONL 을 별칭으로 읽는다
# --------------------------------------------------------------------------
# --watch 가 디렉터리를 가리킬 때 그 안에서 찾을 로그 파일명. --run-log 로 추가.
RUN_LOG_NAMES = ("log.jsonl", "train_log.jsonl", "metrics.jsonl",
                 "history.jsonl", "train.jsonl", "log.json")
# 글롭이 파일을 직접 가리킬 때 로그로 받아들일 확장자. 이 문턱이 없으면 런
# 디렉터리 옆에 있는 queue.log·nohup.out 까지 런으로 잡혀 빈 카드가 생긴다.
# 런 로그는 한 줄씩 쌓이는 JSONL 이다. `.json` 은 일부러 뺐다 — 지표 산출물
# (artifacts/*_metrics.json)이 통째로 런으로 잡혔다. 파일명을 정확히 아는 경우는
# RUN_LOG_NAMES 나 --run-log 로 지정하면 된다.
RUN_LOG_SUFFIXES = (".jsonl", ".ndjson")
STEP_KEYS = ("step", "global_step", "iter", "iteration", "batch_idx")
MAX_STEP_KEYS = ("max_steps", "total_steps", "max_iter", "max_iters",
                 "num_train_steps", "total_iters")
LOSS_KEYS = ("loss_total", "total_loss", "loss", "train_loss", "train/loss")
LR_KEYS = ("lr", "learning_rate", "train/lr")
EPOCH_KEYS = ("epoch", "ep")
TIME_KEYS = ("time", "timestamp", "wall_time", "t")
# 진행 상황을 나타내는 키는 '지표'에서 뺀다 — 진행 칸에 이미 있다.
PROGRESS_KEYS = frozenset(STEP_KEYS + MAX_STEP_KEYS + LOSS_KEYS + LR_KEYS
                          + EPOCH_KEYS + TIME_KEYS)
DONE_WORDS = ("completed", "complete", "done", "finished", "failed", "error",
              "aborted", "killed", "interrupted")

# 로그 꼬리에서 읽는 바이트.
TAIL_BYTES = 256 * 1024
# loss 곡선 점 개수 상한(넘으면 stride 를 늘려 절반으로 솎는다).
LOSS_HISTORY_POINTS = 400
# 이 시간 안에 로그가 갱신되면 '진행 중'으로 본다.
RUNNING_STALE_SEC = 90.0
# 머신당 올려보낼 런 개수 상한.
MAX_RUNS = 10
# --watch 글롭을 다시 훑는 주기(watched_log_paths 참고).
RUN_SCAN_SEC = 30.0

# 에이전트 자신이 쓸 CPU 상한(코어 하나 대비). 한 번 수집이 이보다 무거우면 주기를
# 늘려 평균을 맞춘다 — 감시 대상이 불어나도 모니터가 머신을 느리게 만들지 않게.
# 주기는 hub 의 오프라인 판정(12초)보다 짧게 묶는다.
AGENT_CPU_BUDGET = 0.05
MAX_INTERVAL_SEC = 8.0
# 지표 키 개수 상한(로그가 수십 개 키를 뿜어도 화면이 안 무너지게).
MAX_METRICS = 32

# 디스크 자동 선택에서 뺄 파일시스템. 스냅 루프백·램디스크는 볼 이유가 없다.
SKIP_FSTYPES = frozenset({
    "squashfs", "tmpfs", "devtmpfs", "overlay", "ramfs", "proc", "sysfs",
    "cgroup", "cgroup2", "devpts", "securityfs", "debugfs", "tracefs",
    "fusectl", "configfs", "pstore", "bpf", "autofs", "mqueue", "hugetlbfs",
    "binfmt_misc", "efivarfs", "nsfs", "fuse.snapfuse", "fuse.portal",
})
# /dev/ 로 시작하지 않아도 실제 디스크인 것들(WSL2 의 Windows 드라이브 등).
KEEP_FSTYPES = frozenset({"drvfs", "9p", "virtiofs"})
# WSL2 가 드라이버·wslg 를 파일시스템으로 걸어둔다. 디스크가 아니라 시스템 배관이다.
SKIP_MOUNT_PREFIXES = ("/snap/", "/var/snap/", "/var/lib/docker", "/boot", "/run/",
                       "/usr/lib/wsl", "/mnt/wsl", "/mnt/wslg")
MAX_AUTO_DISKS = 4
# 이보다 작은 마운트는 부트 파티션 같은 조각이라 자동 선택에서 뺀다.
MIN_AUTO_DISK_BYTES = 4 * 1024 ** 3


# --------------------------------------------------------------------------
# 유틸
# --------------------------------------------------------------------------
# 배치 설정 파일. 토큰을 코드에 박지 않기 위해 여기서 읽는다.
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.env")


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


def safe(fn, default=None):
    """수집기 하나가 깨져도 에이전트 전체가 죽지 않게 한다."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"} if default is None else default


def run_cmd(args, timeout=5.0):
    out = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    if out.returncode != 0:
        # nvidia-smi 는 오류를 stdout 에 쓰기도 한다(NVML 초기화 실패 등).
        detail = (out.stderr.strip() or out.stdout.strip())[:200]
        raise RuntimeError(f"{args[0]} rc={out.returncode}: {detail}")
    return out.stdout


# nvidia-smi 가 값을 모를 때 넣는 자리표시자들. 숫자로도, 이름으로도 쓰면 안 된다 —
# 그대로 두면 화면에 프로세스 이름이 "[Not Found]" 로 뜬다.
NVIDIA_PLACEHOLDERS = frozenset({
    "[N/A]", "[Not Supported]", "[Unknown Error]", "[Not Found]",
    "[Insufficient Permissions]", "N/A", "-",
})


def to_num(text):
    text = (text or "").strip()
    if not text or text in NVIDIA_PLACEHOLDERS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def is_number(value):
    # NaN·inf 는 숫자로 치지 않는다. json 에 NaN 으로 실리면 브라우저 JSON.parse 가
    # 통째로 실패해 대시보드가 옛 화면에 멈춘다(학습이 발산해 loss 가 nan 일 때).
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def pick(record, keys):
    """별칭 목록에서 먼저 나오는 수치 값."""
    for key in keys:
        if is_number(record.get(key)):
            return record[key]
    return None


def deep_pick(obj, keys, depth=4):
    """중첩된 dict 안에서 별칭 키를 찾는다 (config.train.max_steps 처럼)."""
    if depth < 0 or not isinstance(obj, dict):
        return None
    for key in keys:
        if is_number(obj.get(key)):
            return obj[key]
    for value in obj.values():
        if isinstance(value, dict):
            found = deep_pick(value, keys, depth - 1)
            if found is not None:
                return found
    return None


def is_wsl():
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


# --------------------------------------------------------------------------
# GPU
# --------------------------------------------------------------------------
def norm_bus_id(bus):
    """PCI bus id 표기를 맞춘다.

    nvidia-smi 는 `00000000:01:00.0`, /proc/driver/nvidia 는 `0000:01:00.0` 로
    도메인 자릿수가 다르다.
    """
    text = str(bus or "").strip().lower()
    parts = text.split(":")
    if len(parts) == 3:
        parts[0] = parts[0][-4:].rjust(4, "0")
        return ":".join(parts)
    return text


def read_minor_to_bus():
    """디바이스 마이너 번호 -> PCI bus id.

    /dev/nvidia0 을 열고 있는 프로세스를 어느 GPU 것으로 볼지 정하는 데 쓴다.
    마이너 번호가 곧 nvidia-smi 인덱스라는 보장이 없어서 PCI 를 거쳐 맞춘다.
    """
    mapping = {}
    for info_path in glob.glob("/proc/driver/nvidia/gpus/*/information"):
        bus = norm_bus_id(os.path.basename(os.path.dirname(info_path)))
        try:
            with open(info_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for line in text.splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "Device Minor":
                value = value.strip()
                if value.isdigit():
                    mapping[int(value)] = bus
    return mapping


def gpus_from_driver(error):
    """nvidia-smi 가 실패할 때 드라이버가 노출하는 정보로 GPU 목록만 만든다.

    자동 업데이트로 라이브러리만 바뀌고 커널 드라이버는 그대로면(재부팅 전) NVML 이
    'Driver/library version mismatch' 로 죽는다. 그래도 학습(CUDA)은 도는 경우가 있어서,
    GPU 를 통째로 없는 것으로 치면 점유자를 어디에도 붙이지 못한다. 지표는 비우고
    이름·UUID·bus id 로 점유자 귀속과 AI 식별이 계속 돌게 한다.
    """
    gpus = []
    for info_path in sorted(glob.glob("/proc/driver/nvidia/gpus/*/information")):
        info = {}
        try:
            with open(info_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    key, _, value = line.partition(":")
                    info[key.strip()] = value.strip()
        except OSError:
            continue
        minor = info.get("Device Minor", "")
        gpus.append({
            "index": int(minor) if minor.isdigit() else len(gpus),
            "name": info.get("Model"),
            "uuid": info.get("GPU UUID"),
            "bus_id": norm_bus_id(info.get("Bus Location") or os.path.basename(os.path.dirname(info_path))),
            "util_pct": None, "vram_used_mb": None, "vram_total_mb": None, "vram_pct": None,
            "temp_c": None, "power_w": None, "power_limit_w": None, "fan_pct": None,
            "clock_mhz": None, "vram_free_mb": None,
            "driver_error": error,
        })
    return gpus


def collect_gpus():
    try:
        gpus = _collect_gpus_smi()
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        error = (T("driver_mismatch") if "mismatch" in text.lower()
                 else T("smi_failed", text=text[:120]))
        return gpus_from_driver(error)
    return gpus or gpus_from_driver(T("smi_no_gpu"))


def _collect_gpus_smi():
    fields = [
        "index", "name", "utilization.gpu", "memory.used", "memory.total",
        "temperature.gpu", "power.draw", "power.limit", "fan.speed", "clocks.sm",
        "uuid", "pci.bus_id",
    ]
    text = run_cmd(
        ["nvidia-smi", f"--query-gpu={','.join(fields)}",
         "--format=csv,noheader,nounits"]
    )
    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(fields):
            continue
        used, total = to_num(parts[3]), to_num(parts[4])
        gpus.append({
            "index": int(to_num(parts[0]) or 0),
            "name": parts[1],
            "util_pct": to_num(parts[2]),
            "vram_used_mb": used,
            "vram_total_mb": total,
            "vram_pct": (used / total * 100.0) if used is not None and total else None,
            "temp_c": to_num(parts[5]),
            "power_w": to_num(parts[6]),
            "power_limit_w": to_num(parts[7]),
            "fan_pct": to_num(parts[8]),
            "clock_mhz": to_num(parts[9]),
            "uuid": parts[10],
            "bus_id": norm_bus_id(parts[11]),
            "vram_free_mb": (total - used) if used is not None and total else None,
        })
    return gpus


_USER_CACHE: dict[int, str] = {}


def uid_name(uid):
    if uid in _USER_CACHE:
        return _USER_CACHE[uid]
    try:
        name = pwd.getpwuid(uid).pw_name
    except (KeyError, OSError):
        name = str(uid)
    _USER_CACHE[uid] = name
    return name


# 인터프리터·런처는 그 자체로 정체를 말해주지 않는다. 뒤에 오는 스크립트/모듈
# 이름까지 붙여야 'python3' 가 아니라 무슨 작업인지 보인다.
_LAUNCHERS = re.compile(
    r"^(python[\d.]*|pypy[\d.]*|uv|uvx|bash|sh|zsh|dash|env|nohup|sudo|time|"
    r"torchrun|accelerate|deepspeed|mpirun|mpiexec|srun|node|ruby|perl|java)$"
)


def command_label(comm, cmdline):
    """화면에 한 줄로 띄울 프로세스 이름.

    `python3` 만 보여주면 GPU 를 뭐가 물고 있는지 알 수 없다. 프로젝트를 몰라도
    정체가 드러나게 실제 스크립트·모듈 이름까지 붙인다.
    """
    tokens = [t for t in (cmdline or "").split() if t]
    base = comm or (os.path.basename(tokens[0]) if tokens else "?")
    if not tokens or not _LAUNCHERS.match(os.path.basename(tokens[0])):
        return base
    # `-m pkg.mod` 는 모듈 경로가 가장 정확한 이름이다.
    if "-m" in tokens:
        idx = tokens.index("-m")
        if idx + 1 < len(tokens):
            return f"{base} -m {tokens[idx + 1]}"
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        name = os.path.basename(token)
        if name and name != base and not _LAUNCHERS.match(name):
            return f"{base} {name}"
    return base


_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def proc_info(pid, boot_time=None):
    """/proc 에서 프로세스 한 개를 읽는다. 못 읽으면 None."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return None
    info: dict = {"pid": pid, "cmdline": cmdline[:600]}
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8", errors="replace") as fh:
            info["comm"] = fh.read().strip()
    except OSError:
        info["comm"] = None
    info["name"] = command_label(info.get("comm"), cmdline)
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("State:"):
                    # T = 정지 신호(SIGSTOP)로 세워 둔 프로세스. 일시정지를 고장과 가른다.
                    info["state"] = line.split()[1]
                elif line.startswith("Uid:"):
                    info["user"] = uid_name(int(line.split()[1]))
                    break
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
            fields = fh.read().rsplit(") ", 1)[1].split()
        info["rss_bytes"] = float(fields[21]) * _PAGE_SIZE
        # 데이터로더 워커는 주 프로세스와 프로세스그룹을 공유한다. 그걸 알면
        # 'GPU 를 5개가 물고 있다' 대신 '작업 1개 + 워커 4' 로 읽어줄 수 있다.
        info["ppid"] = int(fields[1])
        info["pgid"] = int(fields[2])
        # 부팅 뒤 틱 수. started_at 과 달리 스냅샷마다 흔들리지 않아 프로세스 식별에 쓴다.
        info["start_ticks"] = int(fields[19])
        if boot_time is not None:
            info["started_at"] = boot_time + float(fields[19]) / _CLK_TCK
            info["elapsed_sec"] = time.time() - info["started_at"]
    except (OSError, IndexError, ValueError):
        pass
    # cwd 는 그 프로세스가 어느 작업 트리에서 떴는지 알려준다 — 라벨보다 강한
    # 단서인데, 남의 프로세스면 읽을 수 없다(그때는 그냥 비운다).
    try:
        info["cwd"] = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
    return info


def scan_gpu_device_users(minor_to_bus, boot_time):
    """GPU 디바이스 노드를 열고 있는 프로세스를 /proc 에서 찾는다.

    nvidia-smi 의 compute-apps 열거를 못 믿는 두 경우를 메운다.
      - WSL2: 열거 자체가 안 된다(빈 목록이 온다).
      - 그래픽 전용 프로세스: compute app 이 아니라서 애초에 안 나온다.
    CLAUDE.md 지침대로 pgrep -f 는 쓰지 않는다 — 감시 명령 자신을 매칭해 오판한다.
    """
    me = os.getpid()
    deadline = time.time() + FD_SCAN_BUDGET_SEC
    found = []
    truncated = False
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        if time.time() > deadline:
            truncated = True
            break
        pid = int(entry)
        if pid == me:
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # 남의 프로세스거나 이미 죽었다
        buses, compute, hit = set(), False, False
        for fd in fds[:FD_SCAN_MAX_FDS]:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if not target.startswith(GPU_DEV_PREFIXES):
                continue
            hit = True
            if target.startswith(COMPUTE_DEV_HINTS):
                compute = True
            match = re.match(r"^/dev/nvidia(\d+)$", target)
            if match:
                bus = minor_to_bus.get(int(match.group(1)))
                if bus:
                    buses.add(bus)
        if not hit:
            continue
        info = proc_info(pid, boot_time)
        if info is None:
            continue
        info["source"] = "dev-fd"
        info["kind"] = "compute" if compute else "graphics"
        info["bus_ids"] = sorted(buses)
        found.append(info)
    return found, truncated


_FD_SCAN: dict = {"at": None, "found": [], "truncated": False}


def scan_gpu_device_users_cached(minor_to_bus, boot_time):
    """/proc 전체 fd 스캔은 FD_SCAN_FULL_SEC 마다만 한다.

    전체 스캔은 모든 프로세스의 fd 를 readlink 하므로 프로세스가 많은 머신에서
    한 번에 수십 ms 가 든다. 그 사이에는 지난번에 찾은 점유자만 /proc 에서 다시
    읽는다 — 끝난 프로세스는 빠지고, 정지 상태 같은 값은 새로 반영된다.
    """
    tick = time.monotonic()
    if _FD_SCAN["at"] is None or tick - _FD_SCAN["at"] >= FD_SCAN_FULL_SEC:
        found, truncated = scan_gpu_device_users(minor_to_bus, boot_time)
        _FD_SCAN.update(at=tick, found=found, truncated=truncated)
        return [dict(p) for p in found], truncated
    fresh = []
    for old in _FD_SCAN["found"]:
        info = proc_info(old["pid"], boot_time)
        # 같은 pid 를 다른 프로세스가 물려받았으면 그건 다음 전체 스캔이 판단한다.
        if info is None or info.get("start_ticks") != old.get("start_ticks"):
            continue
        info.update(source=old["source"], kind=old["kind"], bus_ids=old["bus_ids"])
        fresh.append(info)
    return fresh, _FD_SCAN["truncated"]


def collect_gpu_processes(minor_to_bus, fd_scan=True):
    """GPU 를 점유한 프로세스. nvidia-smi 열거 + 디바이스 fd 스캔을 합친다.

    두 경로를 합치는 이유는 어느 한쪽이 항상 부족하기 때문이다. nvidia-smi 는
    compute app 만 보고 WSL2 에서는 아무것도 못 보며, fd 스캔은 다른 사용자의
    프로세스를 못 읽는다. 합쳐야 '무엇이 GPU 를 쓰는가'에 가장 가까워진다.
    """
    by_pid: dict[int, dict] = {}
    try:
        boot_time = time.time() - collect_uptime()
    except OSError:
        boot_time = None

    try:
        text = run_cmd(
            ["nvidia-smi",
             "--query-compute-apps=gpu_uuid,pid,used_memory,process_name",
             "--format=csv,noheader,nounits"]
        )
    except Exception:  # noqa: BLE001  — 열거 미지원 환경(WSL2 등)
        text = ""
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        pid = to_num(parts[1])
        if pid is None:
            continue
        pid = int(pid)
        found = {
            "pid": pid,
            "gpu_uuid": parts[0] or None,
            "vram_mb": to_num(parts[2]),
            "source": "nvidia-smi",
            "kind": "compute",
        }
        detail = proc_info(pid, boot_time)
        if detail:
            detail.update(found)
            by_pid[pid] = detail
        else:
            # /proc 에서 못 읽는 경우: 컨테이너의 다른 PID 네임스페이스, 권한, 또는
            # nvidia-smi 호출과 /proc 읽기 사이에 프로세스가 끝난 경우.
            # nvidia-smi 가 준 실행 파일 경로가 유일한 이름이지만, 그것도 자리표시자
            # 일 수 있다("[Not Found]"). 그때는 이름을 지어내지 말고 pid 만 보여준다.
            exe = (parts[3] if len(parts) > 3 else "").strip()
            if exe in NVIDIA_PLACEHOLDERS:
                exe = ""
            found["exe"] = exe or None
            found["name"] = os.path.basename(exe) if exe else f"pid {pid}"
            found["proc_unreadable"] = True
            by_pid[pid] = found

    scan_truncated = False
    if fd_scan:
        scanned, scan_truncated = safe(
            lambda: scan_gpu_device_users_cached(minor_to_bus, boot_time), ([], False))
        for info in scanned or []:
            existing = by_pid.get(info["pid"])
            if existing is None:
                by_pid[info["pid"]] = info
            else:
                # nvidia-smi 가 이미 잡은 프로세스 — GPU 귀속 정보만 보탠다.
                if not existing.get("bus_ids"):
                    existing["bus_ids"] = info.get("bus_ids") or []
                existing["source"] = "nvidia-smi+dev-fd"

    procs = sorted(by_pid.values(),
                   key=lambda p: (p.get("kind") != "compute",
                                  -(p.get("vram_mb") or 0.0)))
    return procs, scan_truncated


def attach_processes(gpus, procs, opaque):
    """GPU 하나하나에 점유자와 '지금 쓸 수 있나' 판정을 붙인다.

    판정을 에이전트에서 확정해 대시보드가 해석을 새로 하지 않게 한다.

    상태:
      free         — 지금 던져도 된다
      busy         — 컴퓨트 점유자를 특정했다 (processes 에 누가/무엇이 들어 있다)
      busy_unknown — 점유는 보이는데 프로세스를 특정하지 못했다

    그래픽 전용 프로세스(컴포지터·브라우저)는 목록에는 올리되 busy 로 만들지
    않는다. 그것 때문에 GPU 를 못 쓰는 것은 아니기 때문이다.
    """
    procs = [p for p in (procs or []) if isinstance(p, dict)]
    single = len(gpus) == 1
    vram_floor = FREE_VRAM_MB_OPAQUE if opaque else FREE_VRAM_MB
    claimed: set[int] = set()

    for gpu in gpus:
        uuid, bus = gpu.get("uuid"), gpu.get("bus_id")
        mine = []
        for proc in procs:
            if proc.get("gpu_uuid") and uuid and proc["gpu_uuid"] == uuid:
                mine.append(proc)
            elif bus and bus in (proc.get("bus_ids") or []):
                mine.append(proc)
            elif single and not proc.get("gpu_uuid") and not proc.get("bus_ids"):
                # GPU 가 한 장이면 귀속할 곳이 하나뿐이다.
                mine.append(proc)
        mine.sort(key=lambda p: (p.get("kind") != "compute",
                                 -(p.get("vram_mb") or 0.0)))
        mine = mine[:MAX_PROCS_PER_GPU]
        claimed.update(p["pid"] for p in mine)

        used = gpu.get("vram_used_mb") or 0.0
        util = gpu.get("util_pct") or 0.0
        compute = [p for p in mine if p.get("kind") == "compute"]
        graphics = [p for p in mine if p.get("kind") != "compute"]

        def who(proc):
            name = proc.get("name") or f"pid {proc['pid']}"
            user = proc.get("user")
            return f"{name} ({user})" if user else name

        def group_suffix(procs):
            """맨 앞 프로세스 뒤에 붙일 '워커 N · 외 M개'.

            데이터로더 워커 4개를 별개 작업 4개로 세면 GPU 를 몇 개가 쓰는지
            읽을 수 없다. 프로세스그룹이 같으면 한 작업의 일부로 본다.
            """
            head = procs[0]
            same = [p for p in procs[1:]
                    if p.get("pgid") is not None
                    and p.get("pgid") == head.get("pgid")]
            others = len(procs) - 1 - len(same)
            parts = []
            if same:
                parts.append(T("workers", n=len(same)))
            if others:
                parts.append(T("others", n=others))
            return (" · " + " · ".join(parts)) if parts else ""

        if compute:
            state = "busy"
            reason = who(compute[0]) + group_suffix(compute)
        elif util > FREE_UTIL_PCT or used > vram_floor:
            state = "busy_unknown"
            metric = (f"util {util:.0f}%" if util > FREE_UTIL_PCT
                      else f"VRAM {used:.0f}MB")
            if graphics:
                state = "busy"
                reason = f"{metric} · {T('graphics', who=who(graphics[0]))}{group_suffix(graphics)}"
            else:
                reason = f"{metric} — {T('no_proc')}"
        else:
            state = "free"
            if graphics:
                reason = T("free_graphics", n=len(graphics), mb=used)
            elif opaque and used > FREE_VRAM_MB:
                # 창 관리자 상시 점유는 작업을 막지 않는다 — 오해를 없앤다.
                reason = T("free_opaque", mb=used)
            else:
                reason = T("free")

        # 작업 본체가 정지 신호로 서 있으면 로그가 안 쌓여도 고장이 아니라 일시정지다.
        # 사람이 '잠깐 멈춰'로 세워 둔 학습이 '멈춤 · N시간째 변화 없음'으로 보이면
        # 죽은 것과 구분이 안 된다.
        lead = gpu_leader({"processes": compute})
        paused = bool(lead and lead.get("state") == "T")
        if paused:
            reason += T("paused")

        # 프로세스는 살아 있는데 GPU 지표가 조용한 구간은 이유를 적어 준다.
        # 데이터 로드·체크포인트 저장·CPU 전처리가 모두 여기 해당한다. 이 문구가
        # 없으면 'GPU 0% 인데 점유 중' 이 오작동으로 보인다.
        if (compute and not paused and util <= FREE_UTIL_PCT and used <= vram_floor
                and not gpu.get("driver_error")):  # 지표를 못 읽었으면 '쉬는 중'이라 말할 수 없다
            reason += T("idle_now", u=util, mb=used)

        if gpu.get("driver_error"):
            # 지표를 못 읽으니 '사용 가능'도 프로세스 목록만 보고 한 추정이다.
            reason = (reason + " · " if state != "free" else T("free_guess")) + gpu["driver_error"]
        gpu["state"] = state
        gpu["state_reason"] = reason
        gpu["paused"] = paused
        gpu["is_free"] = state == "free"
        gpu["processes"] = mine
        gpu["compute_process_count"] = len(compute)
        gpu["graphics_process_count"] = len(graphics)

    # 어느 GPU 에도 붙지 못한 점유자 — 멀티 GPU 에서 귀속에 실패한 경우다.
    return [p for p in procs if p["pid"] not in claimed]


# --------------------------------------------------------------------------
# CPU / 메모리 / 디스크
# --------------------------------------------------------------------------
class CpuSampler:
    """/proc/stat 델타로 CPU 사용률을 낸다(첫 호출은 None)."""

    def __init__(self):
        self._prev = None

    def read(self):
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("cpu "):
                    values = [float(v) for v in line.split()[1:]]
                    break
            else:
                return None
        idle = values[3] + (values[4] if len(values) > 4 else 0.0)
        total = sum(values)
        prev, self._prev = self._prev, (total, idle)
        if prev is None:
            return None
        d_total, d_idle = total - prev[0], idle - prev[1]
        if d_total <= 0:
            return None
        return max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))


def newest_interop_socket():
    """살아 있을 가능성이 가장 높은 WSL interop 소켓.

    tmux 처럼 오래 사는 프로세스는 자기를 띄운 wsl.exe 세션이 끝난 뒤에도 죽은
    WSL_INTEROP 값을 물고 있어서 Windows 실행 파일 호출이 그대로 멈춘다.
    호출 시점에 가장 최근 소켓으로 갈아끼워 성공 확률을 높인다.
    """
    best = None
    try:
        for name in os.listdir("/run/WSL"):
            if not name.endswith("_interop"):
                continue
            path = os.path.join("/run/WSL", name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, path)
    except OSError:
        return None
    return best[1] if best else None


class CpuTemp:
    """CPU 온도. 네이티브 리눅스는 /sys, WSL2 는 Windows WMI 를 거친다.

    WSL2 에는 thermal zone 이 없어 powershell.exe 를 부르는데, 이 호출은 WSL
    interop 상태에 따라 수 초씩 멈출 수 있다. 수집을 별도 스레드로 돌려서
    온도 하나 때문에 push 주기가 밀리지 않게 한다 — 모니터가 늦게 오는 것보다
    온도가 비는 편이 낫다.
    """

    FAIL_BACKOFF_SEC = 300.0
    # 어느 센서를 CPU 패키지 온도로 볼지. 앞에 있는 것이 우선이다.
    PREFERRED_ZONES = ("x86_pkg_temp", "k10temp", "coretemp", "cpu-thermal",
                       "soc_thermal", "acpitz")

    def __init__(self, wsl, interval=15.0):
        self.wsl = wsl
        self.interval = interval
        self._value = None
        self._source = None
        self._at = 0.0
        self._lock = threading.Lock()
        self._fails = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                value, source = self._probe()
            except Exception:  # noqa: BLE001
                value, source = None, None
            with self._lock:
                if value is not None:
                    self._value, self._source, self._fails = value, source, 0
                else:
                    self._fails += 1
                    if self._fails >= 3:
                        # 이 머신에서는 안 되는 것으로 보고 자주 두드리지 않는다.
                        self._value, self._source = None, None
                self._at = time.time()
                wait = self.interval if self._fails < 3 else self.FAIL_BACKOFF_SEC
            time.sleep(wait)

    def _probe(self):
        return self._from_windows() if self.wsl else self._from_sysfs()

    def read(self):
        with self._lock:
            return self._value, self._source

    def _from_sysfs(self):
        zones = {}
        best = None
        for type_path in glob.glob("/sys/class/thermal/thermal_zone*/type"):
            try:
                with open(type_path, "r", encoding="utf-8") as fh:
                    kind = fh.read().strip()
                temp_path = os.path.join(os.path.dirname(type_path), "temp")
                with open(temp_path, "r", encoding="utf-8") as fh:
                    celsius = float(fh.read().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            zones.setdefault(kind, celsius)
            if best is None or celsius > best[0]:
                best = (celsius, kind)
        for kind in self.PREFERRED_ZONES:
            if kind in zones:
                return zones[kind], kind
        if best:
            return best
        for path in glob.glob("/sys/class/hwmon/hwmon*/temp1_input"):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return float(fh.read().strip()) / 1000.0, "hwmon"
            except (OSError, ValueError):
                continue
        return None, None

    def _from_windows(self):
        # MSAcpi_ThermalZoneTemperature 는 decikelvin(1/10 K)로 준다.
        script = ("(Get-CimInstance -Namespace root/WMI "
                  "-ClassName MSAcpi_ThermalZoneTemperature).CurrentTemperature")
        env = dict(os.environ)
        socket_path = newest_interop_socket()
        if socket_path:
            env["WSL_INTEROP"] = socket_path
        for exe in WINDOWS_POWERSHELL_CANDIDATES:
            if exe.startswith("/") and not os.path.exists(exe):
                continue
            try:
                out = subprocess.run(
                    [exe, "-NoProfile", "-Command", script],
                    capture_output=True, text=True, timeout=6.0, check=False, env=env,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if out.returncode != 0:
                continue
            values = [to_num(v) for v in out.stdout.replace("\r", "").split("\n")]
            values = [v for v in values if v]
            if values:
                return max(values) / 10.0 - 273.15, "MSAcpi_ThermalZone(WMI)"
        return None, None


def collect_memory():
    info = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            info[key] = float(rest.split()[0]) * 1024.0
    total = info.get("MemTotal", 0.0)
    available = info.get("MemAvailable", 0.0)
    swap_total = info.get("SwapTotal", 0.0)
    swap_free = info.get("SwapFree", 0.0)
    return {
        "total_bytes": total,
        "used_bytes": total - available,
        "available_bytes": available,
        "pct": ((total - available) / total * 100.0) if total else None,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_total - swap_free,
    }


def pick_mounts(lines, size_of):
    """/proc/mounts 줄들에서 볼 만한 마운트 지점을 고른다.

    size_of 를 밖에서 받는 이유는 이 판단을 다른 머신의 마운트 목록으로도
    검증할 수 있게 하려는 것이다 — WSL2 는 걸러야 할 가짜 마운트가 많다.
    """
    # 같은 디바이스가 여러 곳에 걸려 있으면(WSL2 는 / 와 /mnt/wslg/distro 가 같은
    # /dev/sdd 다) 한 번만 센다 — 같은 용량이 두 줄로 보이면 읽는 사람이 헷갈린다.
    by_dev: dict[str, tuple[str, int]] = {}
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mount, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
        if fstype in SKIP_FSTYPES or dev.startswith("/dev/loop"):
            continue
        if not dev.startswith("/dev/") and fstype not in KEEP_FSTYPES:
            continue
        if mount.startswith(SKIP_MOUNT_PREFIXES):
            continue
        total = size_of(mount)
        if total is None or total < MIN_AUTO_DISK_BYTES:
            continue
        seen = by_dev.get(dev)
        # 같은 디바이스면 더 짧은(= 더 위쪽) 마운트 지점을 대표로 쓴다.
        if seen is None or len(mount) < len(seen[0]):
            by_dev[dev] = (mount, total)
    ranked = sorted(by_dev.values(), key=lambda item: -item[1])
    return [mount for mount, _ in ranked[:MAX_AUTO_DISKS]]


def default_disk_paths():
    """--disk 를 안 줬을 때 볼 마운트 지점을 스스로 고른다.

    경로를 박아두면 그 머신에서만 맞는다. 실제 블록 디바이스(와 WSL2 의
    Windows 드라이브)만 골라 큰 것부터 몇 개 본다.
    """
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        lines = []

    def size_of(mount):
        try:
            return shutil.disk_usage(mount).total
        except OSError:
            return None

    return pick_mounts(lines, size_of) or [os.path.expanduser("~")]


def collect_disks(paths):
    disks = []
    for path in paths:
        path = os.path.expanduser(path)
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        disks.append({
            "path": path,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "pct": usage.used / usage.total * 100.0 if usage.total else None,
        })
    return disks


def collect_uptime():
    with open("/proc/uptime", "r", encoding="utf-8") as fh:
        return float(fh.read().split()[0])


# --------------------------------------------------------------------------
# 런 진행률 (선택 기능) — 로그 스키마를 모른 채 읽는다
# --------------------------------------------------------------------------
_HEADER_CACHE: dict[str, tuple[float, dict]] = {}
# 런별 loss 곡선 누적 상태: {로그경로: {offset, stride, seen, points}}
_LOSS_CURVES: dict[str, dict] = {}
# 런별 마지막 지표 (꼬리 창 밖으로 밀려나도 유지)
_LAST_METRICS: dict[str, dict] = {}


def read_header(path):
    """Read the latest run configuration, including headers appended on resume.

    Scan new complete lines incrementally; do not reuse the original max_steps
    after a batch-size or schedule change. Keep the first dict as a fallback.
    """
    key = os.path.abspath(path)
    try:
        size = os.path.getsize(path)
        offset, header = _HEADER_CACHE.get(key, (0, None))
        if size < offset:
            offset, header = 0, None
        with open(path, "rb") as fh:
            fh.seek(offset)
            while True:
                start = fh.tell()
                line = fh.readline()
                if not line or not line.endswith(b"\n"):
                    offset = start
                    break
                offset = fh.tell()
                if header is not None and b'"config"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(record, dict):
                    if header is None or isinstance(record.get("config"), dict):
                        header = record
    except OSError:
        return None
    _HEADER_CACHE[key] = (offset, header)
    return header


def read_tail_records(path, nbytes=TAIL_BYTES):
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
            fh.readline()  # 잘린 첫 줄 버리기
        chunk = fh.read()
    records = []
    for line in chunk.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, size


_LOSS_HINTS = tuple(f'"{key}"' for key in LOSS_KEYS)
_STEP_HINTS = tuple(f'"{key}"' for key in STEP_KEYS)


def loss_curve(path, size):
    """처음부터의 loss 곡선. 첫 호출만 파일을 통째로 훑고, 그 뒤로는 마지막으로
    읽은 위치부터 새로 쌓인 줄만 이어 읽는다. 점이 너무 많아지면 stride 를 두
    배로 늘려 절반으로 솎는다."""
    state = _LOSS_CURVES.get(path)
    # 파일이 줄었다 = 같은 이름으로 런을 새로 시작했다 -> 처음부터 다시.
    if state is None or size < state["offset"]:
        state = {"offset": 0, "stride": 1, "seen": 0, "points": []}
        _LOSS_CURVES[path] = state
    if size <= state["offset"]:
        return state["points"]

    with open(path, "rb") as fh:
        fh.seek(state["offset"])
        chunk = fh.read(size - state["offset"])
    cut = chunk.rfind(b"\n")
    if cut < 0:  # 아직 한 줄도 안 끝났다 — 다음 턴에 이어 읽는다
        return state["points"]
    state["offset"] += cut + 1

    for line in chunk[:cut].decode("utf-8", "replace").splitlines():
        # json 파싱은 비싸다. step·loss 줄이 아닌 게 확실하면 건너뛴다.
        if not any(hint in line for hint in _LOSS_HINTS):
            continue
        if not any(hint in line for hint in _STEP_HINTS):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        step, loss = pick(record, STEP_KEYS), pick(record, LOSS_KEYS)
        if step is None or loss is None:
            continue
        keep = state["seen"] % state["stride"] == 0
        state["seen"] += 1
        if not keep:
            continue
        # 유효숫자 5자리면 그래프에 충분하다. 17자리 그대로 보내면 곡선 크기가 두 배다.
        state["points"].append({"step": step, "loss": float(f"{loss:.5g}")})
        if len(state["points"]) > LOSS_HISTORY_POINTS * 2:
            state["stride"] *= 2
            state["points"] = state["points"][::2]
    return state["points"]


def latest_metrics(records):
    """꼬리에서 본 모든 수치 키의 마지막 값. 'at' 은 그 줄의 시각이다.

    지표 이름을 미리 알 필요가 없다는 것이 요점이다. 로그가 recall 을 쓰든
    mAP 를 쓰든 psnr 을 쓰든, 수치로 찍히면 그대로 올라간다.
    """
    out: dict[str, dict] = {}
    for record in records:
        step = pick(record, STEP_KEYS)
        stamp = pick(record, TIME_KEYS)
        for key, value in record.items():
            if key in PROGRESS_KEYS or not is_number(value):
                continue
            out[key] = {"value": value, "step": step, "at": stamp}
    if len(out) > MAX_METRICS:
        # 잘라야 하면 최근 step 에 찍힌 것을 남긴다.
        keys = sorted(out, key=lambda k: -(out[k].get("step") or 0))[:MAX_METRICS]
        out = {k: out[k] for k in keys}
    return out


# 로그가 그대로면(크기·mtime 이 같으면) 다시 파싱하지 않는다: {로그경로: (서명, 파싱 결과)}.
# 2초마다 런 10개의 꼬리 256KB 씩을 json 으로 다시 읽던 것이 수집 비용의 큰 몫이었다
# (2026-10-15). 끝난 런·멈춘 런의 로그는 바뀌지 않는다.
_RUN_PARSED: dict[str, tuple[tuple[int, int], dict]] = {}
# 경로별 마지막으로 읽은 시각. 오래 안 쓴 경로의 캐시를 비우는 데 쓴다.
_RUN_USED: dict[str, float] = {}
RUN_CACHE_KEEP_SEC = 3600.0


def prune_run_caches(now):
    """한동안 화면에 안 오른 로그의 캐시를 버린다(몇 달 돌아도 메모리가 안 붓게)."""
    for path in [p for p, at in _RUN_USED.items() if now - at > RUN_CACHE_KEEP_SEC]:
        for cache in (_RUN_USED, _RUN_PARSED, _HEADER_CACHE, _LOSS_CURVES, _LAST_METRICS):
            cache.pop(path, None)
        _HEADER_CACHE.pop(os.path.abspath(path), None)


def describe_run(log_path, now):
    stat = os.stat(log_path)
    sig = (stat.st_size, stat.st_mtime_ns)
    _RUN_USED[log_path] = now
    cached = _RUN_PARSED.get(log_path)
    if cached is None or cached[0] != sig:
        cached = (sig, parse_run_log(log_path))
        _RUN_PARSED[log_path] = cached
    run = dict(cached[1])

    # 여기부터는 시간이 흐르면 바뀌는 값이라 매번 낸다.
    age = now - stat.st_mtime
    status = run.pop("done_status") or ("running" if age <= RUNNING_STALE_SEC else "idle")
    eta_sec = None
    step_no, max_steps, steps_per_sec = run["step"], run["max_steps"], run["steps_per_sec"]
    if status == "running" and steps_per_sec and max_steps and step_no is not None:
        remaining = max_steps - step_no
        if remaining > 0:
            eta_sec = remaining / steps_per_sec
    run.update({
        "_log_path": log_path,  # loss 곡선용(attach_loss_histories). 보내기 전에 뗀다
        "status": status,
        "log_age_sec": age,
        "eta_sec": eta_sec,
        "metrics": {k: {"value": m["value"], "step": m["step"],
                        "age_sec": (now - m["at"]) if m["at"] else None}
                    for k, m in run["metrics"].items()},
    })
    return run


def attach_loss_histories(runs, shown):
    """loss 곡선은 화면이 그리는 런(지금 GPU 에 붙은 런)에만 붙인다.

    곡선은 첫 호출에 로그를 처음부터 끝까지 읽는다(학습 로그 하나가 30MB 쯤 된다).
    끝난 런 곡선까지 만들면 에이전트가 뜰 때마다 CPU 를 5초 넘게 썼고, 어차피
    스냅샷에서 떼어 버렸다.
    """
    for run in runs:
        log_path = run.pop("_log_path", None)
        if not log_path or run.get("dir") not in shown:
            continue
        points = list(safe(lambda: loss_curve(log_path, run["log_size_bytes"]), []) or [])
        step_no, loss_total = run.get("step"), run.get("loss_total")
        if loss_total is not None and step_no is not None:
            if not points or points[-1]["step"] != step_no:
                points.append({"step": step_no, "loss": loss_total})
        run["loss_history"] = points


def parse_run_log(log_path):
    """로그 내용에서만 나오는 값. 로그가 안 바뀌면 다시 부르지 않는다."""
    header = read_header(log_path)
    records, size = read_tail_records(log_path)
    run_dir = os.path.dirname(log_path)

    stepped = [r for r in records if pick(r, STEP_KEYS) is not None]

    def latest(keys):
        """뒤에서부터 그 값이 실제로 찍힌 줄을 찾는다.

        마지막 한 줄만 보면 안 된다 — 요약·검증 줄에는 loss 나 lr 이 없어서
        런이 끝나자마자 값이 전부 '—' 로 비어 버린다.
        """
        for record in reversed(stepped):
            value = pick(record, keys)
            if value is not None:
                return value
        return None

    step_no = latest(STEP_KEYS)
    max_steps = latest(MAX_STEP_KEYS)
    if max_steps is None:
        max_steps = deep_pick(header or {}, MAX_STEP_KEYS)

    # 진행 속도: 꼬리에 남은 줄로 계산한다. 로그에 시각이 없으면 포기한다.
    steps_per_sec = None
    if len(stepped) >= 2:
        first, final = stepped[0], stepped[-1]
        d_step = (pick(final, STEP_KEYS) or 0) - (pick(first, STEP_KEYS) or 0)
        d_time = (pick(final, TIME_KEYS) or 0) - (pick(first, TIME_KEYS) or 0)
        if d_step > 0 and d_time > 0:
            steps_per_sec = d_step / d_time

    # 로그가 스스로 끝을 알리면 그 말을 따른다 (status/state/result 어느 키든).
    done_status = None
    for record in reversed(records):
        word = next((str(record[k]).lower() for k in ("status", "state", "result")
                     if isinstance(record.get(k), str)), None)
        if word and any(done in word for done in DONE_WORDS):
            done_status = "failed" if ("fail" in word or "error" in word) else "completed"
            break

    loss_total = latest(LOSS_KEYS)

    metrics = latest_metrics(records)
    if metrics:
        _LAST_METRICS[log_path] = metrics
    else:
        metrics = _LAST_METRICS.get(log_path) or {}

    return {
        "name": os.path.basename(run_dir) or os.path.basename(log_path),
        "dir": run_dir,
        "log": os.path.basename(log_path),
        "done_status": done_status,
        "log_size_bytes": size,
        "step": step_no,
        "max_steps": max_steps,
        "progress_pct": (step_no / max_steps * 100.0)
                        if step_no is not None and max_steps else None,
        "epoch": latest(EPOCH_KEYS),
        "lr": latest(LR_KEYS),
        "loss_total": loss_total,
        "steps_per_sec": steps_per_sec,
        "metrics": metrics,
        "started_at": pick(header or {}, TIME_KEYS),
    }


def run_log_paths(patterns, names):
    """--watch 글롭에서 읽을 로그 파일 경로.

    글롭이 파일을 직접 가리켜도(`.../log.jsonl`), 디렉터리를 가리켜도 된다.
    """
    seen: set[str] = set()
    paths = []
    for pattern in patterns:
        for match in sorted(glob.glob(os.path.expanduser(pattern))):
            if os.path.isdir(match):
                candidates = [os.path.join(match, name) for name in names]
            elif os.path.isfile(match) and (
                    os.path.basename(match) in names
                    or match.endswith(RUN_LOG_SUFFIXES)):
                candidates = [match]
            else:
                continue
            for candidate in candidates:
                # 없는 파일부터 거른다. realpath 는 경로 마디마다 lstat 을 해서,
                # 디렉터리마다 후보 파일명 6개에 다 부르면 그것만으로 비쌌다.
                if not os.path.isfile(candidate):
                    continue
                real = os.path.realpath(candidate)
                if real in seen:
                    break
                seen.add(real)
                paths.append(real)
                break  # 디렉터리당 로그 하나
    return paths


_RUN_SCAN: dict = {"at": None, "key": None, "paths": []}


def watched_log_paths(patterns, names, now):
    """run_log_paths 결과를 RUN_SCAN_SEC 동안 재사용한다.

    글롭은 끝난 런까지 다 잡아서 시간이 갈수록 불어난다(한 서버에서 디렉터리
    1,400개를 넘었다). 이걸 2초마다 다시 훑은 것이 에이전트가 코어 하나의 10% 를 넘게 먹은
    주원인이었다. 새 런 디렉터리는 드물게 생기고, GPU 를 쓰는 런은 점유자에서 곧바로
    찾으므로(discover_gpu_run_logs) 목록이 조금 늦어도 화면은 늦지 않는다.
    """
    key = (tuple(patterns), tuple(names))
    tick = time.monotonic()
    if (_RUN_SCAN["key"] != key or _RUN_SCAN["at"] is None
            or tick - _RUN_SCAN["at"] >= RUN_SCAN_SEC):
        _RUN_SCAN.update(at=tick, key=key, paths=run_log_paths(patterns, names))
    return _RUN_SCAN["paths"]


def collect_runs(patterns, names, now):
    """글롭에 걸린 런 중 최근 것만 읽어 올린다.

    글롭이 수십 개 디렉터리를 잡는 것은 흔하다(끝난 런이 쌓인다). 전부 파싱하면
    2초마다 로그 꼬리를 수십 MB 씩 읽게 되므로, 파싱 전에 mtime 으로 자른다 —
    화면에 어차피 MAX_RUNS 개만 올라간다.
    """
    candidates = []
    for log_path in watched_log_paths(patterns, names, now):
        try:
            candidates.append((os.path.getmtime(log_path), log_path))
        except OSError:
            continue
    candidates.sort(reverse=True)

    runs = []
    for _, log_path in candidates[:MAX_RUNS]:
        try:
            run = describe_run(log_path, now)
            # 진행도 지표도 없는 파일은 런이 아니다. 확장자만으로는 못 걸러지는
            # 것(설정 덤프, 한 덩어리 JSON)을 여기서 조용히 버린다.
            if run["step"] is None and run["loss_total"] is None and not run["metrics"]:
                continue
            runs.append(run)
        except Exception as exc:  # noqa: BLE001
            runs.append({
                "name": os.path.basename(os.path.dirname(log_path)),
                "dir": os.path.dirname(log_path),
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })
    rank = {"running": 0, "error": 1, "failed": 1, "completed": 2, "idle": 3}
    runs.sort(key=lambda r: (rank.get(r.get("status"), 9), r.get("log_age_sec") or 1e18))
    return runs


# 설정 파일에서 '출력 위치'로 읽을 키. 데이터 경로까지 훑으면 엉뚱한 폴더를 런으로 본다.
_OUTPUT_KEY = re.compile(r"out|save|log|work|run|exp|ckpt|checkpoint", re.I)
CONFIG_MAX_BYTES = 1024 * 1024


def config_output_dirs(path, cwd=None):
    """명령줄로 받은 JSON 설정에서 출력 디렉터리 경로를 뽑는다.

    stdout 을 런 폴더 밖에 쓰는 학습(`--config x.json` 에 train.output 만 적는 식)은
    열린 파일로는 런 폴더를 알 수 없다. 설정이 그 답을 들고 있다.
    점유자(주 프로세스·워커)마다 2초마다 불리므로 파일이 그대로면 지난 답을 쓴다.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return []
    key, sig = (path, cwd), (stat.st_size, stat.st_mtime_ns)
    cached = _CONFIG_DIRS.get(key)
    if cached is None or cached[0] != sig:
        if len(_CONFIG_DIRS) > 256:
            _CONFIG_DIRS.clear()
        cached = (sig, _read_config_output_dirs(path, cwd, stat.st_size))
        _CONFIG_DIRS[key] = cached
    return [d for d in cached[1] if os.path.isdir(d)]


_CONFIG_DIRS: dict[tuple, tuple] = {}


def _read_config_output_dirs(path, cwd, size):
    if size > CONFIG_MAX_BYTES:
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError):
        return []
    found = []

    def walk(obj, key="", depth=0):
        if depth > 4:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, str(k), depth + 1)
        elif isinstance(obj, str) and _OUTPUT_KEY.search(key) and obj:
            # 디렉터리인지는 부르는 쪽이 매번 본다 — 학습이 나중에 만드는 폴더도 있다.
            target = os.path.join(cwd, obj) if cwd and not obj.startswith("/") else obj
            found.append(os.path.normpath(target))
    walk(config)
    return found


def process_paths(proc):
    """프로세스가 어느 디렉터리에서 일하는지 보여주는 경로들.

    열린 일반 파일(stdout 리다이렉트된 console.log, 로그·체크포인트)이 가장 강한
    단서다. cwd 와 명령줄의 경로 인자는 보조 단서로 붙인다.
    한 번 수집 안에서 같은 프로세스를 여러 번 묻는다(런 찾기·런 잇기) — 처음 답을 쓴다.
    """
    memo_key = (proc["pid"], proc.get("cwd"), proc.get("cmdline"))
    if memo_key in _PATHS_MEMO:
        return _PATHS_MEMO[memo_key]
    paths = []
    fd_dir = f"/proc/{proc['pid']}/fd"
    try:
        for fd in os.listdir(fd_dir)[:FD_SCAN_MAX_FDS]:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("/") and not target.startswith(("/dev/", "/proc/")):
                paths.append(target)
    except OSError:
        pass  # 남의 프로세스거나 이미 죽었다
    cwd = proc.get("cwd")
    if cwd:
        paths.append(cwd)
    for token in (proc.get("cmdline") or "").split():
        if token.startswith("/"):
            paths.append(token)
        if token.endswith(".json"):
            full = os.path.join(cwd, token) if cwd and not token.startswith("/") else token
            paths.extend(config_output_dirs(full, cwd))
    _PATHS_MEMO[memo_key] = paths
    return paths


# process_paths 의 수집 1회 동안의 답. build_snapshot 이 매번 비운다.
_PATHS_MEMO: dict[tuple, list] = {}


def discover_gpu_run_logs(gpus, names):
    """GPU 컴퓨트 점유자가 쓰고 있는 런 로그를 --watch 글롭과 상관없이 찾는다.

    런 디렉터리 깊이는 실험마다 다르다(workdirs/실험/런, workdirs/실험/조건/단계 …).
    글롭을 한 단계 늘리는 식으로 쫓아가면 새 배치마다 빠진다. 대신 점유자가 열고
    있는 파일(stdout 리다이렉트된 console.log 등)·cwd·명령줄 경로의 디렉터리에서
    런 로그 파일명을 찾는다.
    """
    found = []
    folders: set[str] = set()  # 주 프로세스와 워커가 같은 폴더를 물고 있다 — 한 번만 본다
    for gpu in gpus:
        for proc in gpu.get("processes") or []:
            if proc.get("kind") != "compute":
                continue
            for path in process_paths(proc):
                if path in folders:
                    continue
                folder = path if os.path.isdir(path) else os.path.dirname(path)
                if folder != path and folder in folders:
                    continue
                folders.update((path, folder))
                for name in names:
                    candidate = os.path.join(folder, name)
                    if os.path.isfile(candidate):
                        candidate = os.path.realpath(candidate)
                        if candidate not in found:
                            found.append(candidate)
                        break
    return found


def link_runs_to_gpus(gpus, runs):
    """GPU 마다 그 GPU 에서 돌고 있는 학습 런을 붙인다 (gpu["run"]).

    컴퓨트 점유자가 런 디렉터리 안의 파일을 열고 있거나, 그 안에서 떴거나,
    명령줄에 그 경로를 실었으면 그 런의 주인이다. 아무 단서도 없더라도 돌고 있는
    런과 컴퓨트 점유 GPU 가 각각 딱 하나면 그 둘을 잇는다. 추론·렌더링처럼
    런에 묶이지 않는 점유자는 run 이 None 으로 남는다 — 대시보드는 그걸 보고
    '학습 중이 아니다'라고 판단한다.
    """
    # 멈춘(idle) 런도 후보다. 프로세스가 아직 그 런의 파일을 물고 있다면 그 GPU 는
    # 그 학습 때문에 잡혀 있는 것이고, 화면은 '멈춤'이라고 알려야 한다.
    live = [r for r in runs if r.get("status") in ("running", "idle") and r.get("dir")]
    running = [r for r in live if r.get("status") == "running"]
    for gpu in gpus:
        gpu["run"] = None
    if not live:
        return
    busy = [g for g in gpus
            if any(p.get("kind") == "compute" for p in g.get("processes") or [])]
    taken: set[str] = set()
    for gpu in busy:
        paths = [path for proc in gpu["processes"] if proc.get("kind") == "compute"
                 for path in process_paths(proc)]
        # 단서가 여러 런을 가리킬 수 있다 — branch 단계는 명령줄에 stem 체크포인트
        # (--resume .../stem/step.pt)를 싣는다. 로그가 살아 있는 쪽이 지금 도는 런이다.
        matches = [run for run in live
                   if any(path == run["dir"].rstrip("/")
                          or path.startswith(run["dir"].rstrip("/") + "/") for path in paths)]
        if matches:
            gpu["run"] = min(matches, key=lambda r: (r.get("status") != "running",
                                                     r.get("log_age_sec") or 0))
            taken.add(gpu["run"]["dir"].rstrip("/"))
    unlinked = [g for g in busy if g["run"] is None]
    spare = [r for r in running if r["dir"].rstrip("/") not in taken]
    if len(unlinked) == 1 and len(spare) == 1 and not taken:
        unlinked[0]["run"] = spare[0]
    for gpu in gpus:
        if gpu["run"] is not None:
            gpu["run"] = run_brief(gpu["run"])


def run_brief(run):
    """GPU 에 붙일 런 요약. 무거운 loss 곡선·지표는 runs 에 이미 있다."""
    return {k: run.get(k) for k in (
        "name", "dir", "status", "log_age_sec", "step", "max_steps",
        "progress_pct", "steps_per_sec", "eta_sec")}


# --------------------------------------------------------------------------
# AI 식별 (선택 기능) — GPU 점유자가 바뀌면 Claude Code 에게 '이게 무슨 작업인가'를
# 한 번 묻는다
#
# 규칙(열린 파일·설정 키)만으로는 새 실험 구조가 들어올 때마다 빗나간다. 그렇다고
# AI 를 2 초마다 부를 수는 없다. 그래서 역할을 나눈다.
#   - AI: 점유자가 바뀌었을 때 한 번. 무슨 실험인지, 어느 로그를 보면 되는지 답한다.
#   - 코드: 그 답(로그 경로)을 받아 2 초마다 진행률·ETA 를 결정적으로 계산한다.
# AI 는 읽기 전용 도구만 받는다. 답은 캐시 파일에 남아 에이전트가 재시작해도 다시
# 묻지 않는다.
# --------------------------------------------------------------------------
AI_STABLE_SEC = 45.0        # 점유자가 이만큼 살아 있어야 묻는다(스모크·순간 프로세스 거르기)
AI_TIMEOUT_SEC = 360.0
AI_RETRY_SEC = 30 * 60.0    # 실패하면 이만큼 뒤에 다시
AI_RECHECK_SEC = 10 * 60.0  # 답한 로그가 멈췄는데 GPU 는 바쁘면, 답이 이만큼 묵었을 때 다시
AI_CACHE_DIR = os.path.expanduser("~/.cache/gpu-monitor/ai")
AI_CACHE_KEEP_SEC = 14 * 86400.0
# AI 는 읽기만 한다. 세 겹으로 막는다.
#   1. --permission-mode dontAsk: 허용 목록 밖은 묻지 않고 거부한다. 이걸 명시하지 않으면
#      사용자 설정(defaultMode=bypassPermissions 등)을 그대로 따라 무엇이든 실행된다.
#   2. --tools: 쓸 수 있는 도구 자체를 넷으로 줄인다(웹·편집·하위 에이전트 없음).
#   3. 허용 Bash 는 출력이 메타데이터뿐인 명령만. 파일 내용은 Read·Grep·Glob 으로 읽는다.
#      find 는 -delete·-exec 로 지울 수 있었고(2026-10-28 실제로 지워졌다), cat·tail 은
#      아래 읽기 금지 경로를 피해 갈 수 있어서 뺐다. nvidia-smi 는 -f 로 파일을 쓴다.
AI_TOOLS = "Read,Glob,Grep,Bash"
AI_ALLOW = ("Read,Glob,Grep,Bash(ls:*),Bash(ps:*),Bash(readlink:*),Bash(stat:*),"
            "Bash(wc:*)")
# 읽기 금지 경로. AI 는 남이 띄운 작업 폴더의 README·로그도 읽고 그 답은 대시보드에 뜬다.
# 그 파일에 숨은 지시문이 열쇠·토큰을 끌어내지 못하게 한다. Bash 의 cat 등에도 걸린다.
AI_DENY_READ = (
    "~/.ssh/**", "~/.gnupg/**", "~/.aws/**", "~/.azure/**", "~/.kube/**", "~/.docker/**",
    "~/.config/gcloud/**", "~/.config/gh/**", "~/.claude/**", "~/.claude.json", "~/.codex/**",
    "~/.netrc", "~/.git-credentials", "~/.pypirc", "~/.npmrc", "~/.cache/huggingface/token",
    "~/.huggingface/token", "~/.bash_history", "~/.zsh_history", "~/.python_history",
    "//**/.env", "//**/.env.*", "//**/monitor.env", "//**/*.pem", "//**/*.key",
    "//**/id_rsa*", "//**/id_ed25519*", "//**/id_ecdsa*")
AI_DENY = "Edit,Write,NotebookEdit," + ",".join(f"Read({p})" for p in AI_DENY_READ)
# 있으면 붙이는 옵션. 옛 Claude Code 에 없는 옵션을 넘기면 실행 자체가 실패하므로
# --help 에 보이는 것만 쓴다.
AI_OPTIONAL_FLAGS = (
    ("--permission-prompts", ["--permission-prompts", "none"]),  # 물을 일이 생기면 거부
    ("--no-session-persistence", ["--no-session-persistence"]),  # 받는 사람의 대화 기록을 어지르지 않는다
    ("--safe-mode", ["--safe-mode"]),        # 받는 사람의 훅·MCP·CLAUDE.md 가 끼어들지 않는다
    ("--max-budget-usd", ["--max-budget-usd", "1"]),  # 한 번 식별 상한(보통 $0.2)
)
AI_KINDS = ("training", "evaluation", "inference", "other")
AI_MODEL = "claude-sonnet-5-5"
# 답 형식이 바뀌면 올린다. 이 값이 다른 캐시는 버리고 다시 묻는다.
AI_SCHEMA = 3
AI_EFFORT = "low"
# 기본은 구독(claude.ai 로그인)으로 부른다. 이 변수들이 환경에 있으면 Claude Code 가
# API 과금 쪽으로 붙으므로 자식 프로세스에서 지운다. MONITOR_AI_AUTH=api_key 면 남긴다.
AI_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")
# claude 설치·로그인 상태를 이 주기로 다시 본다(로그인 만료는 조용히 온다).
AI_STATUS_CHECK_SEC = 300.0
# systemd 서비스는 로그인 셸의 PATH 를 못 받는다. 흔한 설치 위치를 직접 본다.
CLAUDE_CANDIDATES = ("~/.local/bin/claude", "~/.claude/local/claude", "/usr/local/bin/claude",
                     "/opt/homebrew/bin/claude", "~/.npm-global/bin/claude", "/usr/bin/claude")


def find_claude(hint=None):
    """claude 실행 파일 경로. hint(MONITOR_AI_CMD)를 주면 그것만 본다."""
    if hint:
        return shutil.which(os.path.expanduser(hint))
    for cand in (shutil.which("claude"),) + CLAUDE_CANDIDATES:
        if cand:
            path = os.path.expanduser(cand)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return None

AI_PROMPT = """You identify jobs for a GPU monitor. The process below is using a GPU. Find out exactly
what this job is, and which log its progress can be read from.

Machine: {label} / GPU: {gpu}
Process holding the GPU (main process):
{facts}

Steps:
1. Read the config files on the command line, the working directory, open files and parent processes
   (queue or runner scripts). Also look at plan/receipt/status/README-like files in the experiment folder.
2. Find the JSONL training log this process is writing now (a file that keeps growing with per-step records).
   Check with stat that it was modified recently. For the last line, count lines with wc -l and read only the
   end with Read's offset (never read a big log from the start). If there is none, null.
   If the job is not training and has no step records (data generation, capture, evaluation, extraction ...),
   find what its progress can be counted by: a file that grows by one line per unit, or a progress number
   printed to a log (e.g. "records=682"). Read the target count from config or schedule files (the target of
   the current stage or block).
3. If it is a pipeline of several stages (e.g. smoke -> pretrain -> finetune -> eval), give the current stage and
   the remaining ones. Tell whether the next job is actually scheduled (a parent runner, queue or chain script
   will run it) or only planned in documents (plan/README/NEXT.md).
4. If remaining stages are scheduled by a chain, list them in order from the one after the current stage to the
   end of the chain, and estimate each one's duration in seconds. Leave out the current stage, even its
   remaining part — code computes that from the log and adds it. Basis, in order of preference:
   (a) the actual duration of stages already finished in the same chain with the same settings (time between
       the first and last log lines),
   (b) remaining steps / the current stage's speed (same model and resolution),
   (c) for short stages such as evaluation or scoring, the duration of a previous run.
   If there is no basis, null.
Only read files. Do not modify, run or stop anything.

Write every human-readable value (title, summary, stage, next, next_short, rest_stages names, rest_basis)
in {language}.

Output exactly one JSON object as the final answer (no explanation, no code fences):
{{"kind": "training|evaluation|inference|other",
  "title": "short name for the screen, at most {title_max} (e.g. {title_example})",
  "summary": "one or two sentences on what experiment this is and what it tries to find out. Leave out the current step and progress (code computes them)",
  "run_dir": "absolute path of the run directory, or null",
  "log_path": "absolute path of the JSONL log to read progress from, or null",
  "stage": "name of the current stage, or null",
  "next": "one line on the work left after this stage, or null",
  "next_short": "very short name of the next job, at most {next_max} (e.g. {next_example}), or null",
  "next_source": "chain (a script or queue will actually run it next) | plan (only planned in documents) | null",
  "rest_stages": [{{"name": "a stage after the current one", "sec": estimated seconds (integer)}}, ...] to the end of the chain; [] if nothing is scheduled or you can't estimate,
  "rest_basis": "one line on what the estimate is based on, or null",
  "total_steps": integer read from the config only when the log lacks the total step count, otherwise null,
  "progress": only for jobs without a step log, how to count progress, otherwise null.
    {{"kind": "lines", "path": "absolute path of a file that grows by one line per unit", "total": target count of the current stage}}
    or {{"kind": "regex", "path": "absolute path of the log file", "pattern": "Python regex with one group capturing the progress count (e.g. records=(\\d+))", "total": target count of the current stage}}}}
"""
# 답의 언어. 캐시에 언어를 같이 남겨, 언어를 바꾸면 다시 묻는다.
AI_LANG = {
    "en": dict(language="English", title_max="32 characters", title_example="ViT-B/16 pretraining",
               next_max="20 characters", next_example="finetune, eval"),
    "ko": dict(language="Korean; write sentences in the polite form ending in ~합니다",
               title_max="24 Korean characters", title_example="ViT-B/16 사전학습",
               next_max="16 Korean characters", next_example="파인튜닝, eval·채점"),
}


def _read_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return None


def _parent_chain(pid, depth=4):
    chain = []
    for _ in range(depth):
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
                pid = int(fh.read().rsplit(") ", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 1:
            break
        cmd = _read_cmdline(pid)
        if cmd:
            chain.append(f"pid {pid}: {cmd[:400]}")
    return chain


def gpu_leader(gpu):
    """GPU 컴퓨트 점유자 중 '작업 본체' 한 개 — 워커들의 부모."""
    compute = [p for p in gpu.get("processes") or [] if p.get("kind") == "compute"]
    if not compute:
        return None
    pids = {p["pid"] for p in compute}
    roots = [p for p in compute if p.get("ppid") not in pids]
    return min(roots or compute, key=lambda p: p["pid"])


PROGRESS_WINDOW_SEC = 600.0   # 속도는 최근 이만큼의 증가로 잰다
PROGRESS_MIN_SPAN_SEC = 45.0  # 이만큼은 지켜봐야 속도를 낸다


class ProgressCounter:
    """AI 가 알려 준 '세는 법'으로 진행 개수를 읽고, 늘어나는 속도를 직접 잰다.

    데이터 캡처·평가처럼 step·시각이 찍히지 않는 작업용이다. 로그에 시각이 없어도
    에이전트가 2 초마다 개수를 보므로 속도와 남은 시간을 낼 수 있다.
    """
    def __init__(self):
        self.lines: dict[str, tuple[int, int]] = {}
        self.samples: dict[str, collections.deque] = {}
        self.changed_at: dict[str, float] = {}

    def _count_lines(self, path):
        size = os.path.getsize(path)
        offset, count = self.lines.get(path, (0, 0))
        if size < offset:  # 같은 이름으로 새로 썼다
            offset, count = 0, 0
        with open(path, "rb") as fh:
            fh.seek(offset)
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                count += chunk.count(b"\n")
                offset += len(chunk)
        self.lines[path] = (offset, count)
        return count

    @staticmethod
    def _last_match(path, pattern):
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 65536))
            text = fh.read().decode("utf-8", "replace")
        found = re.findall(pattern, text)
        if not found:
            return None
        last = found[-1][0] if isinstance(found[-1], tuple) else found[-1]
        return float(last)

    def measure(self, recipe, now):
        """(완료 개수, 초당 개수 또는 None, 마지막으로 늘어난 뒤 지난 초, 개수가 줄었나)"""
        path, kind = recipe.get("path"), recipe.get("kind")
        if not isinstance(path, str) or not os.path.isfile(path):
            return None
        if kind == "lines":
            done = self._count_lines(path)
        elif kind == "regex" and isinstance(recipe.get("pattern"), str) and len(recipe["pattern"]) <= 200:
            done = self._last_match(path, recipe["pattern"])
        else:
            return None
        if done is None:
            return None
        key = f"{kind}:{path}:{recipe.get('pattern')}"
        q = self.samples.setdefault(key, collections.deque())
        reset = bool(q) and done < q[-1][1]
        if reset:  # 줄었다 = 다음 블록·새 실행. 처음부터 잰다
            q.clear()
        if not q or done != q[-1][1]:
            self.changed_at[key] = now
        q.append((now, done))
        while len(q) > 2 and now - q[0][0] > PROGRESS_WINDOW_SEC:
            q.popleft()
        t0, d0 = q[0]
        rate = (done - d0) / (now - t0) if now - t0 >= PROGRESS_MIN_SPAN_SEC and done > d0 else None
        return done, rate, now - self.changed_at.get(key, now), reset


class AiIdentifier:
    def __init__(self, command_hint, label, auth="subscription", required=False):
        self.command_hint = command_hint
        self.command: str | None = None
        self.label = label
        self.auth = auth
        self.required = required  # MONITOR_AI=on: claude 가 없으면 '꺼짐'이 아니라 오류
        self.results: dict[str, dict] = {}
        self.first_seen: dict[str, float] = {}
        self.failed_at: dict[str, float] = {}
        self.running: str | None = None
        self.lock = threading.Lock()
        self.counter = ProgressCounter()
        self._stale: set[int] = set()
        self._extra_flags: list[str] | None = None
        # 상태: 준비됐나(claude 있음·읽기 전용 가능·로그인) / 아니면 왜 / 최근 식별 실패
        self.ready = False
        self.off = False
        self.problem = T("ai_checking")
        self.account = ""
        self.last_error: str | None = None
        self._checked_at = 0.0
        self._checking = False
        os.makedirs(AI_CACHE_DIR, exist_ok=True)
        self._prune_cache()

    def _env(self):
        if self.auth == "api_key":
            return dict(os.environ)
        return {k: v for k, v in os.environ.items() if k not in AI_STRIP_ENV}

    def _claude_flags(self):
        """이 Claude Code 가 아는 옵션으로 안전 옵션을 고른다. 읽기 전용을 보장할 수 없으면 오류."""
        if self._extra_flags is not None:
            return self._extra_flags
        out = subprocess.run([self.command, "--help"], capture_output=True, text=True,
                             timeout=30, stdin=subprocess.DEVNULL)
        text = out.stdout + out.stderr
        if "--permission-mode" not in text or "dontAsk" not in text:
            raise RuntimeError(T("ai_too_old"))
        flags = []
        for name, args in AI_OPTIONAL_FLAGS:
            if name in text:
                flags += args
        if "--tools " in text:
            flags += ["--tools", AI_TOOLS]
        self._extra_flags = flags
        return flags

    # 상태 -----------------------------------------------------------------
    def brief(self):
        """화면에 띄울 AI 상태 한 줄."""
        with self.lock:
            if self.off:
                return {"state": "off", "message": self.problem}
            if not self.ready and self._checked_at == 0.0 and not self.last_error:
                return {"state": "checking", "message": T("ai_checking")}  # 켜자마자 오류로 띄우지 않는다
            if not self.ready:
                return {"state": "error", "message": self.problem}
            if self.last_error:
                return {"state": "error", "message": T("ai_failed", err=self.last_error)}
            return {"state": "ok", "message": self.account}

    def _maybe_check(self, now):
        with self.lock:
            if self._checking or now - self._checked_at < AI_STATUS_CHECK_SEC:
                return
            self._checking = True
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        """claude 가 있고, 읽기 전용으로 묶을 수 있고, 로그인돼 있나."""
        ready, off, problem, account = False, False, "", ""
        try:
            command = find_claude(self.command_hint)
            if command is None:
                if self.command_hint:
                    problem = T("ai_cmd_missing", path=self.command_hint)
                elif self.required:
                    problem = T("ai_need_install")
                else:
                    off, problem = True, T("ai_not_installed")
            else:
                if command != self.command:
                    self.command, self._extra_flags = command, None
                self._claude_flags()
                if self.auth == "api_key":
                    ready = bool(os.environ.get("ANTHROPIC_API_KEY"))
                    problem = "" if ready else T("ai_no_key")
                    account = T("ai_api_key")
                else:
                    out = subprocess.run([command, "auth", "status", "--json"], capture_output=True,
                                         text=True, timeout=30, stdin=subprocess.DEVNULL,
                                         env=self._env())
                    try:
                        info = json.loads(out.stdout)
                    except ValueError:
                        info = {}
                    if not isinstance(info, dict):
                        info = {}
                    if info.get("loggedIn") is False:
                        problem = T("ai_need_login")
                    else:
                        ready = True
                        plan = info.get("subscriptionType")
                        account = T("ai_sub", plan=plan) if plan else T("ai_login")
        except Exception as exc:  # noqa: BLE001
            problem = str(exc)[:200] or type(exc).__name__
        with self.lock:
            if ready and not self.ready:
                # 막 준비됐다(설치·로그인 직후) — 그동안 실패한 식별을 바로 다시 한다.
                self.failed_at.clear()
                self.last_error = None
            self.ready, self.off, self.problem, self.account = ready, off, problem, account
            self._checked_at = time.time()
            self._checking = False

    # 캐시 -----------------------------------------------------------------
    @staticmethod
    def _cache_path(key):
        return os.path.join(AI_CACHE_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")

    def _prune_cache(self):
        cutoff = time.time() - AI_CACHE_KEEP_SEC
        for path in glob.glob(os.path.join(AI_CACHE_DIR, "*.json")):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass

    def _load(self, key):
        if key in self.results:
            return self.results[key]
        try:
            with open(self._cache_path(key), "r", encoding="utf-8") as fh:
                result = json.load(fh)
        except (OSError, ValueError):
            return None
        if result.get("schema") != AI_SCHEMA:
            return None
        if result.get("lang", "ko") != LANG:  # 언어를 바꿨다 — 그 언어로 다시 묻는다(옛 캐시는 한국어)
            return None
        self.results[key] = result
        return result

    # 식별 -----------------------------------------------------------------
    @staticmethod
    def fingerprint(gpu, leader):
        # started_at 은 (지금 - uptime) 으로 매번 다시 계산해 몇 ms 씩 흔들린다. 정각 초에
        # 뜬 프로세스는 int() 가 앞뒤 초를 오가서 키가 계속 바뀌었고, '45초 살아 있음'을
        # 영영 못 채워 식별이 시작되지 않았다. 틱 수는 고정이다.
        start = leader.get("start_ticks")
        if start is None:
            start = int(leader.get("started_at") or 0)
        return f"{gpu.get('uuid') or gpu.get('index')}-{leader['pid']}-{start}"

    def _facts(self, leader):
        lines = [f"pid {leader['pid']} (user {leader.get('user')}, "
                 f"running for {int(leader.get('elapsed_sec') or 0)}s)",
                 f"command line: {leader.get('cmdline')}",
                 f"working directory: {leader.get('cwd')}"]
        opened = [p for p in process_paths({"pid": leader["pid"]})]
        if opened:
            lines.append("open files: " + ", ".join(opened[:20]))
        chain = _parent_chain(leader["pid"])
        if chain:
            lines.append("parent processes:\n  " + "\n  ".join(chain))
        return "\n".join(lines)

    def _ask(self, key, gpu_name, leader):
        started = time.time()
        result = None
        error = None
        try:
            # 프롬프트도 try 안에서 만든다 — 여기서 죽으면 running 이 풀리지 않아 식별이 영영 멈춘다.
            prompt = AI_PROMPT.format(label=self.label, gpu=gpu_name, facts=self._facts(leader),
                                      **AI_LANG.get(LANG, AI_LANG["en"]))
            proc = subprocess.run(
                [self.command, "-p", prompt, "--output-format", "json",
                 "--model", AI_MODEL, "--effort", AI_EFFORT, "--max-turns", "30",
                 "--permission-mode", "dontAsk", *self._claude_flags(),
                 "--allowedTools", AI_ALLOW, "--disallowedTools", AI_DENY],
                capture_output=True, text=True, timeout=AI_TIMEOUT_SEC,
                cwd=leader.get("cwd") if leader.get("cwd") and os.path.isdir(leader["cwd"])
                else os.path.expanduser("~"),
                stdin=subprocess.DEVNULL,
                env=self._env())
            try:
                outer = json.loads(proc.stdout)
            except ValueError:
                outer = None
            if not isinstance(outer, dict):
                lines = (proc.stderr or proc.stdout or "").strip().splitlines()
                raise RuntimeError(lines[-1][:200] if lines else T("ai_exit", code=proc.returncode))
            if outer.get("is_error"):
                raise RuntimeError(str(outer.get("result") or outer.get("terminal_reason")
                                       or outer.get("subtype") or T("ai_error"))[:200])
            text = outer.get("result") or ""
            match = re.search(r"\{.*\}", text, re.S)
            answer = json.loads(match.group(0)) if match else None
            if not (isinstance(answer, dict) and answer.get("title")):
                raise RuntimeError(T("ai_no_json"))
            if isinstance(answer, dict) and answer.get("title"):
                if answer.get("kind") not in AI_KINDS:
                    answer["kind"] = "other"
                answer["identified_at"] = time.time()
                answer["schema"] = AI_SCHEMA
                answer["lang"] = LANG
                if answer.get("next_source") not in ("chain", "plan"):
                    answer["next_source"] = None
                # 남은 단계 합계는 코드가 낸다. 단계 하나라도 추정이 비면 합계도 모른다고 본다.
                stages = answer.get("rest_stages")
                stages = [x for x in stages if isinstance(x, dict)] if isinstance(stages, list) else []
                secs = [x.get("sec") for x in stages]
                answer["rest_stages"] = stages
                answer["rest_sec"] = (sum(secs) if stages and all(
                    isinstance(v, (int, float)) and v >= 0 for v in secs) else None)
                answer["cost_usd"] = outer.get("total_cost_usd")
                answer["took_sec"] = round(time.time() - started, 1)
                result = answer
        except Exception as exc:  # noqa: BLE001
            error = f"{exc}" if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
            print(f"[agent] AI 식별 실패 ({key}): {error}", flush=True)
        with self.lock:
            self.running = None
            if result is None:
                self.failed_at[key] = time.time()
                self.last_error = error or T("ai_unknown")
                self._checked_at = 0.0  # 로그인 만료 같은 원인을 곧바로 다시 본다
                return
            self.results[key] = result
            self.last_error = None
        try:
            with open(self._cache_path(key), "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=1)
        except OSError:
            pass
        print(f"[agent] AI 식별: {result.get('title')} ({result.get('took_sec')}s, "
              f"${result.get('cost_usd')})", flush=True)

    def forget(self, key):
        with self.lock:
            self.results.pop(key, None)
        try:
            os.remove(self._cache_path(key))
        except OSError:
            pass

    # 스냅샷에 반영 ----------------------------------------------------------
    def apply(self, gpus, runs, now):
        """GPU 마다 AI 답을 붙이고, 답이 가리킨 로그로 진행률을 잡는다."""
        self._maybe_check(now)
        alive = set()
        for gpu in gpus:
            leader = gpu_leader(gpu)
            if leader is None:
                continue
            key = self.fingerprint(gpu, leader)
            alive.add(key)
            self.first_seen.setdefault(key, now)
            result = self._load(key)
            if result is None:
                self._maybe_start(key, gpu, leader, now)
                gpu["ai"] = {"status": "pending" if self.running == key else "waiting"}
                continue
            gpu["ai"] = {"status": "done", **{k: result.get(k) for k in (
                "kind", "title", "summary", "stage", "next", "next_short", "next_source",
                "rest_sec", "rest_stages", "rest_basis", "run_dir", "log_path",
                "identified_at")}}
            run = self._run_for(result, runs, now)
            if id(result) in self._stale:
                self._stale.discard(id(result))
                self.forget(key)
            if run is not None:
                gpu["run"] = run_brief(run)
                busy = (gpu.get("util_pct") or 0) > FREE_UTIL_PCT
                if (run.get("status") == "idle" and busy
                        and now - (result.get("identified_at") or 0) > AI_RECHECK_SEC):
                    # 답한 로그는 멈췄는데 GPU 는 일한다 = 같은 프로세스가 다음 단계로
                    # 넘어갔거나 답이 틀렸다. 다시 묻는다.
                    self.forget(key)
        for key in list(self.first_seen):
            if key not in alive:
                self.first_seen.pop(key, None)
                self.results.pop(key, None)

    def _maybe_start(self, key, gpu, leader, now):
        with self.lock:
            if self.running is not None or not self.ready:
                return
            if now - self.first_seen.get(key, now) < AI_STABLE_SEC:
                return
            if now - self.failed_at.get(key, 0) < AI_RETRY_SEC:
                return
            self.running = key
        threading.Thread(target=self._ask, args=(key, gpu.get("name"), leader),
                         daemon=True).start()

    def _run_for(self, result, runs, now):
        run = None
        log_path = result.get("log_path")
        if isinstance(log_path, str) and os.path.isfile(log_path):
            real = os.path.realpath(log_path)
            run = next((r for r in runs if r.get("dir") and r.get("log") and
                        os.path.realpath(os.path.join(r["dir"], r["log"])) == real), None)
            if run is None:
                run = safe(lambda: describe_run(real, now))
                if not isinstance(run, dict) or "error" in run:
                    run = None
                else:
                    runs.insert(0, run)
        recipe = result.get("progress")
        if (run is None or run.get("step") is None) and isinstance(recipe, dict):
            return self._run_from_counter(result, recipe, run, runs, now)
        if run is None:
            return None
        total = result.get("total_steps")
        if run.get("max_steps") is None and isinstance(total, (int, float)) and total > 0:
            run["max_steps"] = total
            if run.get("step") is not None:
                run["progress_pct"] = run["step"] / total * 100.0
                if run.get("status") == "running" and run.get("steps_per_sec"):
                    run["eta_sec"] = max(0.0, total - run["step"]) / run["steps_per_sec"]
        return run

    def _run_from_counter(self, result, recipe, run, runs, now):
        measured = safe(lambda: self.counter.measure(recipe, now))
        if not isinstance(measured, tuple):
            return run
        done, rate, quiet, reset = measured
        if reset and now - (result.get("identified_at") or now) > 60:
            # 같은 프로세스가 다음 블록으로 넘어갔다. 남은 단계 추정이 묵었으니 다시 묻는다.
            self._stale.add(id(result))
        total = recipe.get("total")
        total = total if isinstance(total, (int, float)) and total > 0 else None
        status = "running" if quiet <= RUNNING_STALE_SEC else "idle"
        if run is None:  # runs 에 넣어야 대시보드가 같은 dir 로 찾는다
            path = recipe["path"]
            run = {"name": os.path.basename(os.path.dirname(path)) or os.path.basename(path),
                   "dir": os.path.dirname(path), "log": os.path.basename(path)}
            runs.insert(0, run)
        run.update({
            "status": status, "log_age_sec": quiet, "step": done, "max_steps": total,
            "progress_pct": min(100.0, done / total * 100.0) if total else None,
            "steps_per_sec": rate,
            "eta_sec": (max(0.0, total - done) / rate
                        if total and rate and status == "running" else None),
        })
        return run


# --------------------------------------------------------------------------
# 스냅샷
# --------------------------------------------------------------------------
def build_snapshot(args, cpu_sampler, cpu_temp, wsl, minor_to_bus):
    now = time.time()
    _PATHS_MEMO.clear()
    temp_c, temp_source = cpu_temp.read()
    gpus = safe(collect_gpus, [])
    if not isinstance(gpus, list):
        gpus = []
    procs, scan_truncated = safe(
        lambda: collect_gpu_processes(minor_to_bus, not args.no_fd_scan), ([], False))
    if not isinstance(procs, list):
        procs = []
    # VRAM 기준선을 올릴지는 'WSL 인가'가 아니라 '점유자를 하나라도 찾았는가'로
    # 정한다. 찾았다면 열거가 되는 환경이므로 좁은 기준선이 맞고, 하나도 못 찾은
    # WSL2 라면 창 관리자 상시 점유와 실제 작업을 구분할 수 없으니 올려야 한다.
    opaque = wsl and not procs
    unattributed = safe(lambda: attach_processes(gpus, procs, opaque), []) or []
    free_gpus = [g["index"] for g in gpus
                 if isinstance(g, dict) and g.get("is_free")]
    runs = (safe(lambda: collect_runs(args.watch, args.run_log, now), [])
            if args.watch else []) or []
    # 글롭이 못 잡은, 지금 GPU 가 쓰고 있는 런을 앞에 보탠다.
    known = {os.path.realpath(os.path.join(r["dir"], r["log"]))
             for r in runs if r.get("dir") and r.get("log")}
    for log_path in safe(lambda: discover_gpu_run_logs(gpus, args.run_log), []) or []:
        if log_path not in known:
            run = safe(lambda: describe_run(log_path, now))
            if isinstance(run, dict) and "error" not in run:
                runs.insert(0, run)
    safe(lambda: link_runs_to_gpus(gpus, runs))
    if getattr(args, "ai", None) is not None:
        safe(lambda: args.ai.apply(gpus, runs, now))
    # loss 곡선은 지금 GPU 에서 도는 런 것만 싣는다. 화면은 그것만 그리는데, 끝난 런
    # 곡선까지 실으면 스냅샷이 머신당 200KB 를 넘었다(2초마다).
    # 지표(metrics)도 같다 — 런마다 키 32개씩, 끝난 런 9개 몫이 스냅샷의 40% 였다.
    shown = {g["run"]["dir"] for g in gpus
             if isinstance(g, dict) and isinstance(g.get("run"), dict) and g["run"].get("dir")}
    attach_loss_histories(runs, shown)
    for run in runs:
        if run.get("dir") not in shown:
            run.pop("metrics", None)
    prune_run_caches(now)
    return {
        "schema": SCHEMA,
        "agent_version": AGENT_VERSION,
        "machine_id": args.id,
        "label": args.label,
        "hostname": socket.gethostname(),
        "time": now,
        "platform": "WSL2" if wsl else "linux",
        "cpu": {
            "cores": os.cpu_count(),
            "util_pct": cpu_sampler.read(),
            "temp_c": temp_c,
            "temp_source": temp_source,
            "load": safe(lambda: list(os.getloadavg()), None),
        },
        "memory": safe(collect_memory),
        "disks": safe(lambda: collect_disks(args.disk), []),
        "uptime_sec": safe(collect_uptime, None),
        "gpus": gpus,
        "gpu_processes": procs,
        "unattributed_processes": unattributed,
        "proc_scan_truncated": bool(scan_truncated),
        "free_gpu_indices": free_gpus,
        "free_gpu_count": len(free_gpus),
        "watching": bool(args.watch),
        "runs": runs,
        "notes": args.note or None,
        "ai_status": (args.ai.brief() if getattr(args, "ai", None) is not None
                      else {"state": "off", "message": T("ai_off")}),
    }


# hub 로 내보내지 않는 프로세스 필드. 화면은 쓰지 않고, 명령줄에는 키·토큰이 인자로
# 실리기도 한다. hub 의 /api/all 은 인증 없이 열려 있다. 에이전트 안(AI 식별)에서는 쓴다.
PRIVATE_PROC_KEYS = ("cmdline", "cwd")


def _strip_procs(procs):
    return [{k: v for k, v in p.items() if k not in PRIVATE_PROC_KEYS} if isinstance(p, dict) else p
            for p in procs or []]


def public_snapshot(snapshot):
    """hub 로 보낼 사본. 원본은 AI 식별 스레드가 아직 읽고 있을 수 있어 건드리지 않는다."""
    out = dict(snapshot)
    for key in ("gpu_processes", "unattributed_processes"):
        if key in out:
            out[key] = _strip_procs(out[key])
    out["gpus"] = [dict(g, processes=_strip_procs(g.get("processes")))
                   if isinstance(g, dict) and "processes" in g else g
                   for g in out.get("gpus") or []]
    return out


def push(hub, token, snapshot, timeout=8.0):
    body = json.dumps(public_snapshot(snapshot)).encode("utf-8")
    request = urllib.request.Request(
        hub.rstrip("/") + "/ingest",
        data=body,
        headers={"Content-Type": "application/json", "X-Monitor-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", required=True, help="예: http://HOST:8787")
    parser.add_argument("--label", required=True, help="화면에 표시할 이름 (예: gpu-a)")
    parser.add_argument("--id", help="머신 고유 id (기본: label)")
    parser.add_argument("--token", default="",
                        help="hub 와 맞춰야 하는 공유 토큰 "
                             "(생략하면 $MONITOR_TOKEN, 그다음 monitor.env)")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument(
        "--watch", action="append", default=[],
        help="런 디렉터리/로그 파일 glob. 주면 진행률까지 본다. 여러 번 지정 가능",
    )
    parser.add_argument(
        "--run-log", action="append", default=[],
        help=f"런 디렉터리 안에서 찾을 로그 파일명 (기본: {', '.join(RUN_LOG_NAMES)})",
    )
    parser.add_argument(
        "--disk", action="append", default=[],
        help="사용량을 볼 경로. 생략하면 마운트 지점을 스스로 고른다",
    )
    parser.add_argument(
        "--no-fd-scan", action="store_true",
        help="GPU 디바이스 fd 스캔을 끈다 (nvidia-smi 열거만 믿는다)",
    )
    parser.add_argument("--note", help="화면에 함께 띄울 짧은 메모")
    parser.add_argument(
        "--lang", default=os.environ.get("MONITOR_LANG") or "en",
        help="화면 문구·AI 작업 설명의 언어: en(기본) | ko  (기본: $MONITOR_LANG)",
    )
    parser.add_argument(
        "--ai", dest="ai_mode", default=os.environ.get("MONITOR_AI") or "auto",
        help="AI 식별: auto(claude 가 있으면 켠다, 기본) | on | off  (기본: $MONITOR_AI)",
    )
    parser.add_argument(
        "--ai-cmd", default=os.environ.get("MONITOR_AI_CMD") or None,
        help="Claude Code 실행 파일 (기본: $MONITOR_AI_CMD, 없으면 자동으로 찾는다)",
    )
    parser.add_argument(
        "--ai-auth", default=os.environ.get("MONITOR_AI_AUTH") or "subscription",
        help="subscription(claude 로그인 계정, 기본) | api_key($ANTHROPIC_API_KEY)  (기본: $MONITOR_AI_AUTH)",
    )
    parser.add_argument("--once", action="store_true", help="한 번만 수집해 출력(디버그)")
    args = parser.parse_args()

    args.id = args.id or args.label
    args.token = read_token(args.token)
    args.run_log = args.run_log or list(RUN_LOG_NAMES)
    if not args.disk:
        args.disk = default_disk_paths()
    if args.lang not in ("en", "ko"):
        parser.error(f"MONITOR_LANG 은 en|ko 중 하나다: {args.lang}")
    global LANG
    LANG = args.lang
    if args.ai_mode not in ("auto", "on", "off"):
        parser.error(f"MONITOR_AI 는 auto|on|off 중 하나다: {args.ai_mode}")
    if args.ai_auth not in ("subscription", "api_key"):
        parser.error(f"MONITOR_AI_AUTH 는 subscription|api_key 중 하나다: {args.ai_auth}")
    args.ai = (None if args.ai_mode == "off" else
               AiIdentifier(args.ai_cmd, args.label, args.ai_auth, required=args.ai_mode == "on"))

    wsl = is_wsl()
    minor_to_bus = safe(read_minor_to_bus, {}) or {}
    cpu_sampler = CpuSampler()
    cpu_temp = CpuTemp(wsl)
    cpu_sampler.read()  # 첫 델타 기준점

    if args.once:
        time.sleep(0.3)
        print(json.dumps(
            build_snapshot(args, cpu_sampler, cpu_temp, wsl, minor_to_bus),
            indent=2, ensure_ascii=False))
        return 0

    print(f"[agent] {args.label} -> {args.hub}  interval={args.interval}s "
          f"platform={'WSL2' if wsl else 'linux'} "
          f"disks={','.join(args.disk)} watch={len(args.watch)} "
          f"token={'설정됨' if args.token else '없음'} "
          f"ai={args.ai_mode}/{args.ai_auth}", flush=True)
    fails = 0
    # 최근 수집 1회마다 든 CPU 초. 첫 수집은 뺀다 — 로그를 처음부터 한 번 읽는 비용이라
    # 그걸로 주기를 늘리면 켜자마자 몇 분 동안 화면이 굼떠진다.
    cpu_recent: collections.deque = collections.deque(maxlen=15)
    first = True
    stretched = False
    while True:
        started = time.time()
        cpu_started = time.process_time()
        try:
            snapshot = build_snapshot(args, cpu_sampler, cpu_temp, wsl, minor_to_bus)
            push(args.hub, args.token, snapshot)
            if fails:
                print(f"[agent] hub 재연결 (실패 {fails}회 뒤)", flush=True)
            fails = 0
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            fails += 1
            if fails in (1, 5, 30) or fails % 120 == 0:
                print(f"[agent] push 실패 {fails}회: {exc}", flush=True)
        except Exception as exc:  # noqa: BLE001
            fails += 1
            print(f"[agent] 예상치 못한 오류: {type(exc).__name__}: {exc}", flush=True)
        # 실패가 이어지면 백오프(최대 15초)로 hub 를 두드린다.
        delay = args.interval if fails == 0 else min(15.0, args.interval * min(fails, 8))
        # CPU 예산: 수집이 무거워지면(감시 글롭이 불어남 등) 주기를 늘려 평균을 맞춘다.
        if not first:
            cpu_recent.append(time.process_time() - cpu_started)
        first = False
        cpu_avg = sum(cpu_recent) / len(cpu_recent) if cpu_recent else 0.0
        need = min(MAX_INTERVAL_SEC, cpu_avg / AGENT_CPU_BUDGET)
        if need > delay:
            delay = need
            if not stretched:
                print(f"[agent] 수집이 무겁다 (CPU {cpu_avg * 1000:.0f}ms/회) — "
                      f"주기를 {delay:.1f}초로 늘린다", flush=True)
            stretched = True
        elif stretched and need < args.interval * 0.8:
            print(f"[agent] 수집이 다시 가볍다 — 주기 {args.interval:g}초", flush=True)
            stretched = False
        time.sleep(max(0.0, delay - (time.time() - started)))


if __name__ == "__main__":
    sys.exit(main())
