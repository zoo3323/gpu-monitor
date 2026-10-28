"""hub 에서 한 머신을 기다려 확인한다.  python check.py LABEL PLATFORM [--gpus N] [--jobs SPEC] [--offline]

SPEC: "0,1:1;1:1" = 작업 두 개 — GPU0,1 에 프로세스 1개, GPU1 에 프로세스 1개
"""
import argparse, json, sys, time, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("label"); ap.add_argument("platform")
ap.add_argument("--gpus", type=int); ap.add_argument("--jobs"); ap.add_argument("--offline", action="store_true")
ap.add_argument("--hub", default="http://127.0.0.1:8787"); ap.add_argument("--wait", type=float, default=40)
a = ap.parse_args()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline, m = time.time() + a.wait, None
while time.time() < deadline:
    try:
        ms = json.loads(opener.open(a.hub + "/api/all", timeout=5).read())["machines"]
        m = next((x for x in ms if x["machine_id"] == a.label), None)
        if m and (m["online"] != a.offline) and (not a.jobs or m.get("jobs")):
            break
    except Exception as exc:  # noqa: BLE001
        print("wait:", exc)
    time.sleep(2)
if m is None:
    sys.exit(f"FAIL: {a.label} never reported")
summary = {k: m.get(k) for k in ("label", "online", "platform", "agent_version", "hostname")}
summary["gpus"] = [(g.get("index"), g.get("name"), g.get("state"), g.get("util_pct")) for g in m.get("gpus", [])]
summary["jobs"] = [(j.get("gpus"), j.get("pids"), j.get("label")) for j in m.get("jobs") or []]
summary["memory_gb"] = round((m.get("memory") or {}).get("total_bytes", 0) / 2**30, 1)
summary["disks"] = [d.get("path") for d in m.get("disks") or []]
summary["cpu"] = (m.get("cpu") or {}).get("util_pct")
summary["uptime_h"] = round((m.get("uptime_sec") or 0) / 3600, 1)
print(json.dumps(summary, indent=1, ensure_ascii=False))
errors = []
if m["online"] == a.offline: errors.append(f"online={m['online']}")
if not a.offline:
    if m.get("platform") != a.platform: errors.append(f"platform {m.get('platform')}")
    if summary["memory_gb"] <= 0: errors.append("memory")
    if not summary["disks"]: errors.append("disks")
    if a.gpus is not None and len(summary["gpus"]) != a.gpus: errors.append(f"gpus {len(summary['gpus'])}")
    if a.jobs:
        want = sorted((tuple(int(i) for i in part.split(":")[0].split(",")), int(part.split(":")[1])) for part in a.jobs.split(";"))
        got = sorted((tuple(j["gpus"]), len(j["pids"])) for j in m.get("jobs") or [])
        if want != got: errors.append(f"jobs {got} != {want}")
if errors:
    sys.exit("FAIL: " + ", ".join(errors))
print("OK")
