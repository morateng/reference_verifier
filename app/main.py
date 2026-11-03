import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.extraction import extract_pdf
from app.verification import verify_reference
from app.reporting import VERSION, evidence, summarize
from app.providers import provider_get, parse_arxiv
from xml.etree.ElementTree import ParseError

STATIC = Path(__file__).parent / "static"
MAX_BYTES = 20 * 1024 * 1024
JOBS: dict[str, dict] = {}
ACTIVE = 0
LIMIT = asyncio.Semaphore(4)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "ReferenceVerifier/0.1 (academic citation audit)"}, follow_redirects=False) as client:
        app.state.client = client
        yield


app = FastAPI(title="Reference Verifier", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": VERSION, "ai_available": bool(os.getenv("REFERENCE_AI_API_KEY") or os.getenv("OPENAI_API_KEY")), "max_upload_mb": 20}


@app.get("/api/diagnostics")
async def diagnostics():
    async def probe(name, url, params):
        try:
            response = await provider_get(app.state.client, url, params=params, timeout=7, attempts=1)
            response.raise_for_status()
            if name == "arXiv":
                valid = parse_arxiv(response.content, "2301.02111") is not None
            else:
                payload = response.json()
                valid = isinstance(payload.get("message"), dict) if name == "Crossref" else isinstance(payload.get("data"), list)
            return {"source": name, "ok": valid, "http_status": response.status_code, "reason": "조회 연결 정상" if valid else "등록기관 응답 형식 확인 실패"}
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            return {"source": name, "ok": False, "http_status": code, "reason": f"HTTP {code}: " + {403: "접근 거부", 429: "요청 제한"}.get(code, "서비스 오류")}
        except httpx.TimeoutException:
            return {"source": name, "ok": False, "http_status": None, "reason": "응답 시간 초과"}
        except (httpx.HTTPError, ValueError, ParseError):
            return {"source": name, "ok": False, "http_status": None, "reason": "연결 또는 응답 실패"}
    sources = await asyncio.gather(
        probe("Crossref", "https://api.crossref.org/works/10.1038/nature14539", {}),
        probe("DataCite", "https://api.datacite.org/dois", {"page[size]": 1}),
        probe("arXiv", "https://export.arxiv.org/api/query", {"id_list": "2301.02111", "max_results": 1}),
    )
    return {"version": VERSION, "sources": sources, "note": "고정된 공개 API 요청으로 연결만 점검합니다. 논문 검증 정확도 평가는 아닙니다."}


def expire_jobs():
    now = time.time()
    for key in list(JOBS):
        job = JOBS[key]
        if job["state"] in {"done", "failed"} and now - job.get("finished_at", job["created_at"]) > 1800:
            del JOBS[key]


async def parse_upload(file: UploadFile) -> dict:
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "PDF는 최대 20MB까지 업로드할 수 있습니다.")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "올바른 PDF 파일을 업로드해 주세요.")
    try:
        return await asyncio.to_thread(extract_pdf, data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/parse")
async def preview_parsing(file: UploadFile = File(...)):
    """Preview local parsing only; no registry or AI requests and no stored job."""
    try:
        return {"version": VERSION, **await parse_upload(file)}
    finally:
        await file.close()


async def run_job(job_id: str, parsed: dict, use_ai: bool):
    global ACTIVE
    job = JOBS[job_id]
    async def one(reference):
        async with LIMIT:
            result = await verify_reference(reference, app.state.client, use_ai)
            job["results"].append(result)
            job["completed"] += 1
    try:
        await asyncio.gather(*(one(r) for r in parsed["references"]))
        job["results"].sort(key=lambda r: r["id"])
        job["state"] = "done"
    except Exception:
        job["state"] = "failed"
        job["error"] = "처리 중 오류가 발생했습니다. 완료된 항목을 확인하고 다시 시도해 주세요."
    finally:
        job["finished_at"] = time.time()
        ACTIVE -= 1


@app.post("/api/jobs", status_code=202)
async def create_job(background: BackgroundTasks, file: UploadFile = File(...), use_ai: bool = Form(False)):
    global ACTIVE
    expire_jobs()
    if ACTIVE >= 2 or len(JOBS) >= 50:
        raise HTTPException(429, "현재 처리 가능한 작업 수를 초과했습니다. 잠시 후 다시 시도해 주세요.")
    ACTIVE += 1
    try:
        parsed = await parse_upload(file)
        job_id = uuid.uuid4().hex
        JOBS[job_id] = {"id": job_id, "version": VERSION, "filename": (file.filename or "paper.pdf")[:200], "created_at": time.time(),
                        "state": "running", "total": len(parsed["references"]), "completed": 0,
                        "page_count": parsed["page_count"], "warnings": parsed["warnings"], "results": [],
                        "parsing": parsed.get("parsing"), "structured_count": parsed.get("structured_count", 0),
                        "scope": "Crossref·DataCite·arXiv 등록정보 대조. 전체 저자의 이니셜·순서, 학술적 타당성, 철회 여부를 완전히 검증하지 않습니다. AI는 초록 기반 예비 검토입니다."}
        background.add_task(run_job, job_id, parsed, use_ai)
        return {"id": job_id}
    except BaseException:
        ACTIVE -= 1
        raise
    finally:
        await file.close()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    expire_jobs()
    if job_id not in JOBS:
        raise HTTPException(404, "결과가 없거나 만료되었습니다. 결과는 완료 후 30분 동안 보관됩니다.")
    job = JOBS[job_id]
    return {**job, "results": [{**r, "evidence": evidence(r)} for r in job["results"]],
            "diagnosis": summarize(job["results"], job["total"])}


@app.get("/api/jobs/{job_id}/report")
def download_report(job_id: str):
    return JSONResponse(get_job(job_id), headers={"Content-Disposition": 'attachment; filename="reference-report.json"'})
