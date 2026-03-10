#!/usr/bin/env python3
"""
iDRAC Batch Manager — Web Server (FastAPI + SSE)
racadm 기반 서버 일괄 관리 도구
"""

from __future__ import annotations

import asyncio
import csv
import ipaddress
import json
import re
import uuid
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── 상수 ──────────────────────────────────────────────────────────────────────
DEFAULT_USER   = "root"
DEFAULT_PASS   = "calvin"
MAX_CONCURRENT = 256

STATUS_NONE  = "pending"
STATUS_SAVED = "saved"
STATUS_OK    = "done"
STATUS_FAIL  = "fail"

# ── 전역 상태 ─────────────────────────────────────────────────────────────────
state = {
    "rac_user": DEFAULT_USER,
    "rac_pass": DEFAULT_PASS,
    "subnet":   "",
    "results":  [],          # list of {"dhcp_ip": str, "tag": str}
    "mapping":  {},          # {tag: {"dhcp_ip","static_ip","status"}}
    "scan_running":  False,
    "apply_running": False,
}

# SSE 이벤트 큐: 채널별
_sse_queues: dict[str, asyncio.Queue] = {}


def new_channel() -> str:
    cid = str(uuid.uuid4())
    _sse_queues[cid] = asyncio.Queue()
    return cid


async def push(channel: str, event: str, data: dict) -> None:
    q = _sse_queues.get(channel)
    if q:
        await q.put({"event": event, "data": data})


async def push_all(event: str, data: dict) -> None:
    for q in list(_sse_queues.values()):
        await q.put({"event": event, "data": data})


# ── 유틸 ──────────────────────────────────────────────────────────────────────
def expand_cidr(cidr: str) -> list[str]:
    net = ipaddress.ip_network(cidr, strict=False)
    if net.prefixlen >= 31:
        return [str(h) for h in net.hosts()] or [str(net.network_address)]
    return [str(h) for h in net.hosts()]


async def racadm_getsvctag(ip: str, user: str, password: str) -> tuple[str, str] | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "racadm", "-r", ip, "-u", user, "-p", password,
            "getsvctag", "--nocertwarn",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill(); return None
        m = re.search(r'\b[A-Z0-9]{7}\b', stdout.decode(errors="replace"))
        if m:
            return (ip, m.group())
    except Exception:
        pass
    return None


async def racadm_set_static_ip(
    dhcp_ip: str, user: str, password: str,
    static_ip: str, subnet: str, gateway: str,
) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "racadm", "-r", dhcp_ip, "-u", user, "-p", password,
            "setniccfg", "-s", static_ip, subnet, gateway, "--nocertwarn",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill(); return False
        return proc.returncode == 0
    except Exception:
        return False


async def racadm_exec(ip: str, user: str, password: str, *args) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "racadm", "-r", ip, "-u", user, "-p", password,
            "--nocertwarn", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
            combined = (out + err).decode(errors="replace").strip()
            ok = proc.returncode == 0 or "successfully" in combined.lower()
            return ok, combined
        except asyncio.TimeoutError:
            proc.kill(); return False, "타임아웃"
    except Exception as e:
        return False, str(e)


# ── FastAPI 앱 ─────────────────────────────────────────────────────────────────
app = FastAPI(title="iDRAC Batch Manager")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ── Pydantic 모델 ──────────────────────────────────────────────────────────────
class SettingsBody(BaseModel):
    rac_user: str
    rac_pass: str
    subnet: str

class MappingEntry(BaseModel):
    tag: str
    static_ip: str

class ApplyAllBody(BaseModel):
    subnet_mask: str
    gateway: str

class ManageBody(BaseModel):
    targets: list[str]   # IP 목록 (비면 results 전체)
    action: str          # ipmi_on / ipmi_off / hotspare_on / hotspare_off / change_account
    new_user: str = ""
    new_pw: str = ""


# ── REST 엔드포인트 ────────────────────────────────────────────────────────────

@app.get("/api/state")
async def get_state():
    return {
        "rac_user": state["rac_user"],
        "rac_pass": state["rac_pass"],
        "subnet":   state["subnet"],
        "scan_running":  state["scan_running"],
        "apply_running": state["apply_running"],
        "result_count":  len(state["results"]),
        "mapping_count": len(state["mapping"]),
    }

@app.post("/api/settings")
async def save_settings(body: SettingsBody):
    if body.rac_user:
        state["rac_user"] = body.rac_user
    if body.rac_pass:
        state["rac_pass"] = body.rac_pass
    if body.subnet:
        try:
            hosts = expand_cidr(body.subnet)
            state["subnet"] = body.subnet
        except ValueError as e:
            raise HTTPException(400, f"잘못된 CIDR: {e}")
    return {"ok": True, "rac_user": state["rac_user"], "subnet": state["subnet"]}

@app.get("/api/results")
async def get_results():
    return state["results"]

@app.get("/api/mapping")
async def get_mapping():
    return [
        {"tag": tag, **v}
        for tag, v in state["mapping"].items()
    ]

@app.post("/api/mapping/entry")
async def update_mapping_entry(body: MappingEntry):
    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', body.static_ip):
        raise HTTPException(400, "올바른 IP 형식이 아닙니다")
    if body.tag not in state["mapping"]:
        raise HTTPException(404, "태그 없음")
    m = state["mapping"][body.tag]
    old_st = m["status"]
    state["mapping"][body.tag]["static_ip"] = body.static_ip
    state["mapping"][body.tag]["status"] = old_st if old_st == STATUS_OK else STATUS_SAVED
    return {"ok": True}

@app.delete("/api/mapping/entry/{tag}")
async def delete_mapping_entry(tag: str):
    if tag not in state["mapping"]:
        raise HTTPException(404, "태그 없음")
    del state["mapping"][tag]
    return {"ok": True}

@app.post("/api/mapping/load-scan")
async def load_scan_to_mapping():
    if not state["results"]:
        raise HTTPException(400, "스캔 결과 없음")
    for r in state["results"]:
        tag = r["tag"]; dhcp_ip = r["dhcp_ip"]
        if tag not in state["mapping"]:
            state["mapping"][tag] = {"dhcp_ip": dhcp_ip, "static_ip": "", "status": STATUS_NONE}
        else:
            state["mapping"][tag]["dhcp_ip"] = dhcp_ip
    return {"ok": True, "count": len(state["mapping"])}

@app.post("/api/mapping/import-csv")
async def import_csv(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        text  = raw.decode("utf-8-sig", errors="replace")
        lines = [l for l in text.splitlines() if l.strip()]
        delim = "\t" if "\t" in lines[0] else ","
        reader = csv.reader(lines, delimiter=delim)
        ip_col = tag_col = -1
        imported = 0
        for row in reader:
            if len(row) < 2: continue
            c0 = row[0].strip()
            if ip_col == -1:
                h0 = c0.lower()
                if "tag" in h0 or "svctag" in h0 or "servicetag" in h0:
                    tag_col, ip_col = 0, 1
                elif "ip" in h0 or "static" in h0 or "addr" in h0:
                    ip_col, tag_col = 0, 1
                else:
                    tag_col, ip_col = (0, 1) if re.match(r"^[A-Z0-9]{7}$", c0) else (1, 0)
                    t2 = row[tag_col].strip(); s2 = row[ip_col].strip()
                    if re.match(r"^[A-Z0-9]{7}$", t2) and re.match(r"^\d+\.\d+\.\d+\.\d+$", s2):
                        dh = state["mapping"][t2]["dhcp_ip"] if t2 in state["mapping"] else "(미스캔)"
                        state["mapping"][t2] = {"dhcp_ip": dh, "static_ip": s2, "status": STATUS_SAVED}
                        imported += 1
                continue
            t3 = row[tag_col].strip(); s3 = row[ip_col].strip()
            if not re.match(r"^[A-Z0-9]{7}$", t3): continue
            if not re.match(r"^\d+\.\d+\.\d+\.\d+$", s3): continue
            dh = state["mapping"][t3]["dhcp_ip"] if t3 in state["mapping"] else "(미스캔)"
            state["mapping"][t3] = {"dhcp_ip": dh, "static_ip": s3, "status": STATUS_SAVED}
            imported += 1
        return {"ok": True, "imported": imported}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get("/api/mapping/export-csv")
async def export_mapping_csv():
    out = StringIO()
    w = csv.writer(out)
    w.writerow(["ServiceTag", "DHCP_IP", "StaticIP", "Status"])
    for tag, v in state["mapping"].items():
        w.writerow([tag, v["dhcp_ip"], v["static_ip"], v["status"]])
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=mapping.csv"},
    )

@app.get("/api/results/export-csv")
async def export_results_csv():
    out = StringIO()
    w = csv.writer(out)
    w.writerow(["DHCP_IP", "ServiceTag"])
    for r in state["results"]:
        w.writerow([r["dhcp_ip"], r["tag"]])
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=scan_results.csv"},
    )


# ── SSE: 스캔 ─────────────────────────────────────────────────────────────────
@app.get("/api/scan/stream")
async def scan_stream():
    if state["scan_running"]:
        raise HTTPException(409, "스캔이 이미 실행 중입니다")
    if not state["subnet"]:
        raise HTTPException(400, "스캔 대역을 먼저 설정하세요")

    cid = new_channel()

    async def run():
        state["scan_running"] = True
        state["results"] = []
        user = state["rac_user"]; pw = state["rac_pass"]
        subnet = state["subnet"]

        try:
            ips = expand_cidr(subnet)
        except ValueError as e:
            await push(cid, "error", {"msg": str(e)})
            state["scan_running"] = False
            _sse_queues.pop(cid, None)
            return

        total = len(ips)
        done = 0; found = 0
        await push(cid, "start", {"total": total, "subnet": subnet})
        sem = asyncio.Semaphore(MAX_CONCURRENT)

        async def task(ip: str):
            nonlocal done, found
            async with sem:
                result = await racadm_getsvctag(ip, user, pw)
                done += 1
                if result:
                    ip_r, tag = result
                    found += 1
                    state["results"].append({"dhcp_ip": ip_r, "tag": tag})
                    await push(cid, "found", {
                        "no": found, "dhcp_ip": ip_r, "tag": tag,
                        "done": done, "total": total,
                    })
                else:
                    await push(cid, "progress", {"done": done, "total": total, "found": found})

        await asyncio.gather(*[task(ip) for ip in ips])
        await push(cid, "done", {"found": found, "total": total})
        state["scan_running"] = False

    asyncio.create_task(run())

    async def event_generator() -> AsyncGenerator[str, None]:
        q = _sse_queues[cid]
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=60)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
                    if msg["event"] in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            _sse_queues.pop(cid, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── SSE: Static IP 일괄 적용 ──────────────────────────────────────────────────
@app.get("/api/apply/stream")
async def apply_stream(subnet_mask: str, gateway: str):
    if state["apply_running"]:
        raise HTTPException(409, "이미 실행 중입니다")
    ready = {tag: v for tag, v in state["mapping"].items() if v["static_ip"]}
    if not ready:
        raise HTTPException(400, "Static IP가 입력된 항목이 없습니다")

    cid = new_channel()

    async def run():
        state["apply_running"] = True
        user = state["rac_user"]; pw = state["rac_pass"]
        total = len(ready); success = 0; fail = 0
        await push(cid, "start", {"total": total})
        sem = asyncio.Semaphore(10)

        async def apply_one(tag: str, v: dict):
            nonlocal success, fail
            async with sem:
                await push(cid, "progress", {
                    "tag": tag, "dhcp_ip": v["dhcp_ip"],
                    "static_ip": v["static_ip"], "status": "running",
                })
                ok = await racadm_set_static_ip(
                    v["dhcp_ip"], user, pw, v["static_ip"], subnet_mask, gateway
                )
                if ok:
                    success += 1
                    state["mapping"][tag]["status"] = STATUS_OK
                    await push(cid, "result", {
                        "tag": tag, "static_ip": v["static_ip"],
                        "ok": True, "success": success, "fail": fail,
                        "done": success + fail, "total": total,
                    })
                else:
                    fail += 1
                    state["mapping"][tag]["status"] = STATUS_FAIL
                    await push(cid, "result", {
                        "tag": tag, "static_ip": v["static_ip"],
                        "ok": False, "success": success, "fail": fail,
                        "done": success + fail, "total": total,
                    })

        await asyncio.gather(*[apply_one(tag, v) for tag, v in ready.items()])
        await push(cid, "done", {"success": success, "fail": fail, "total": total})
        state["apply_running"] = False

    asyncio.create_task(run())

    async def event_generator() -> AsyncGenerator[str, None]:
        q = _sse_queues[cid]
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=60)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
                    if msg["event"] in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            _sse_queues.pop(cid, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── SSE: 단일 IP 변경 ─────────────────────────────────────────────────────────
@app.get("/api/apply/single/stream")
async def apply_single_stream(tag: str, static_ip: str, subnet_mask: str, gateway: str):
    if tag not in state["mapping"]:
        raise HTTPException(404, "태그 없음")
    if not re.match(r'^\d+\.\d+\.\d+\.\d+$', static_ip):
        raise HTTPException(400, "잘못된 IP 형식")
    dhcp_ip = state["mapping"][tag]["dhcp_ip"]
    cid = new_channel()

    async def run():
        await push(cid, "start", {"tag": tag, "dhcp_ip": dhcp_ip, "static_ip": static_ip})
        ok = await racadm_set_static_ip(
            dhcp_ip, state["rac_user"], state["rac_pass"], static_ip, subnet_mask, gateway
        )
        state["mapping"][tag]["static_ip"] = static_ip
        state["mapping"][tag]["status"] = STATUS_OK if ok else STATUS_FAIL
        await push(cid, "done", {"tag": tag, "ok": ok, "static_ip": static_ip})

    asyncio.create_task(run())

    async def event_generator() -> AsyncGenerator[str, None]:
        q = _sse_queues[cid]
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
                    if msg["event"] in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            _sse_queues.pop(cid, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── SSE: iDRAC 관리 ───────────────────────────────────────────────────────────
@app.post("/api/manage/stream-start")
async def manage_stream_start(body: ManageBody):
    targets = body.targets
    if not targets:
        targets = [r["dhcp_ip"] for r in state["results"]]
    if not targets:
        raise HTTPException(400, "대상 없음")

    cid = new_channel()

    async def run():
        user = state["rac_user"]; pw = state["rac_pass"]
        sem = asyncio.Semaphore(10)
        total = len(targets); success = 0; fail = 0
        await push(cid, "start", {"total": total, "action": body.action})

        async def task(ip: str):
            nonlocal success, fail
            async with sem:
                if body.action == "ipmi_on":
                    ok, msg = await racadm_exec(ip, user, pw, "set", "iDRAC.IPMILan.Enable", "1")
                elif body.action == "ipmi_off":
                    ok, msg = await racadm_exec(ip, user, pw, "set", "iDRAC.IPMILan.Enable", "0")
                elif body.action == "hotspare_on":
                    ok, msg = await racadm_exec(ip, user, pw, "set", "System.Power.Hotspare.Enable", "1")
                elif body.action == "hotspare_off":
                    ok, msg = await racadm_exec(ip, user, pw, "set", "System.Power.Hotspare.Enable", "0")
                elif body.action == "change_account":
                    eff = user
                    ok = True; msg = ""
                    if body.new_user:
                        ok, msg = await racadm_exec(ip, user, pw, "set", "iDRAC.Users.2.UserName", body.new_user)
                        if ok: eff = body.new_user
                    if body.new_pw and ok:
                        ok, msg = await racadm_exec(ip, eff, pw, "set", "iDRAC.Users.2.Password", body.new_pw)
                else:
                    ok, msg = False, "알 수 없는 액션"

                if ok: success += 1
                else:  fail += 1
                await push(cid, "result", {
                    "ip": ip, "ok": ok, "msg": msg[:80],
                    "success": success, "fail": fail,
                    "done": success + fail, "total": total,
                })

        await asyncio.gather(*[task(ip) for ip in targets])
        await push(cid, "done", {"success": success, "fail": fail, "total": total})

    asyncio.create_task(run())
    return {"channel": cid}

@app.get("/api/manage/stream/{cid}")
async def manage_stream(cid: str):
    if cid not in _sse_queues:
        raise HTTPException(404, "채널 없음")

    async def event_generator() -> AsyncGenerator[str, None]:
        q = _sse_queues[cid]
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=60)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
                    if msg["event"] in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            _sse_queues.pop(cid, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── HTML 서빙 ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import logging
    import socket
    import uvicorn

    # 사용 가능한 포트 자동 탐색
    CANDIDATES = [8080, 8888, 9090, 7070, 5000, 3000]

    def is_port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return True
            except OSError:
                return False

    chosen = next((p for p in CANDIDATES if is_port_free(p)), None)
    if chosen is None:
        with socket.socket() as s:
            s.bind(("", 0))
            chosen = s.getsockname()[1]

    # uvicorn 로그 포맷 커스터마이징:
    # "Uvicorn running on http://0.0.0.0:PORT" 줄을 가로채
    # "http://localhost:PORT" 로 교체 출력
    _original_info = logging.Logger.info

    def _patched_info(self, msg, *args, **kwargs):
        if isinstance(msg, str) and "Uvicorn running on" in msg:
            port_str = str(chosen)
            print(f"INFO:     Uvicorn running on http://localhost:{port_str} (Press CTRL+C to quit)")
            return
        _original_info(self, msg, *args, **kwargs)

    logging.Logger.info = _patched_info

    print(f"\n{'='*52}")
    print(f"  iDRAC Batch Manager Web UI")
    print(f"  http://localhost:{chosen}")
    print(f"  CTRL+C 로 종료")
    print(f"{'='*52}\n")

    uvicorn.run(app, host="0.0.0.0", port=chosen, reload=False)
