from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import Response
from urllib.parse import quote


router = APIRouter()


def service(request: Request):
    return request.app.state.service


@router.get("/session")
def session(request: Request):
    return {"token": request.app.state.token, "api_base": "/api", "timezone": "Asia/Shanghai"}


@router.get("/{resource}")
def listing(request: Request, resource: str, cursor: str | None = None, limit: int = 50):
    if resource not in {"datasets", "strategies", "experiments", "runs", "jobs"}:
        raise KeyError(resource)
    return asdict(service(request).list_resource(resource, cursor, limit))


@router.get("/{resource}/{identity}")
def detail(request: Request, resource: str, identity: str):
    if resource not in {"datasets", "strategies", "experiments", "runs", "jobs"}:
        raise KeyError(resource)
    if resource == "datasets":
        return service(request).dataset_detail(identity)
    return service(request).get_resource(resource, identity)


@router.post("/datasets")
def create_dataset(request: Request, body: dict):
    if set(body) != {"upload_id"}:
        raise ValueError("upload_id required")
    return service(request).submit_job("import", body)


@router.post("/datasets/preflight")
def dataset_preflight(request: Request, body: dict):
    if set(body) != {"upload_id"}:
        raise ValueError("upload_id required")
    return service(request).preflight_upload(body["upload_id"])


@router.post("/strategies")
def create_strategy(request: Request, body: dict):
    if set(body) != {"manifest", "files"} or not isinstance(body["manifest"], dict) or not isinstance(body["files"], dict) or not all(
        isinstance(name, str) and isinstance(content, str) for name, content in body["files"].items()):
        raise ValueError("manifest and files required")
    result = service(request).store.register_strategy({name: content.encode("utf-8") for name, content in body["files"].items()}, body["manifest"])
    return service(request)._record(result)


@router.post("/experiments")
def create_experiment(request: Request, body: dict):
    if set(body) != {"payload"} or not isinstance(body["payload"], dict):
        raise ValueError("payload required")
    return service(request)._record(service(request).store.save_experiment(None, None, body["payload"]))


@router.put("/experiments/{identity}")
def update_experiment(request: Request, identity: str, body: dict):
    if set(body) != {"expected_revision", "payload"} or type(body["expected_revision"]) is not int or not isinstance(body["payload"], dict):
        raise ValueError("expected_revision and payload required")
    return service(request)._record(service(request).store.save_experiment(identity, body["expected_revision"], body["payload"]))


@router.post("/runs")
def submit_run(request: Request, body: dict):
    if not {"experiment_id", "expected_revision", "idempotency_key"} <= set(body) or type(body["expected_revision"]) is not int:
        raise ValueError("submission fields required")
    return service(request).submit(body["experiment_id"], body["expected_revision"], body["idempotency_key"], body.get("source", "web"))


@router.post("/runs/batch")
def submit_batch(request: Request, body: dict):
    if not isinstance(body.get("items"), list) or not 1 <= len(body["items"]) <= 100:
        raise ValueError("batch size must be between 1 and 100")
    if any(not isinstance(item, dict) or set(item) != {"experiment_id", "expected_revision"}
           or not isinstance(item["experiment_id"], str) or type(item["expected_revision"]) is not int
           for item in body["items"]):
        raise ValueError("invalid batch item")
    return {"items": service(request).submit_batch(body["items"], body.get("idempotency_key", ""), body.get("source", "web"))}


@router.post("/runs/{identity}/cancel")
def cancel_run(request: Request, identity: str):
    return service(request).cancel_run(identity)


@router.post("/runs/{identity}/clone")
def clone_run(request: Request, identity: str):
    return service(request).clone_run(identity)


@router.post("/runs/{identity}/rerun")
def rerun(request: Request, identity: str, body: dict):
    return service(request).rerun(identity, body.get("idempotency_key", ""), body.get("source", "web"))


@router.get("/runs/{identity}/logs")
def run_logs(request: Request, identity: str, cursor: str | None = None, limit: int = 100, tail: bool = False):
    run = service(request).store.get_run(identity)
    return asdict(service(request).logs(run.job_id, cursor, limit, tail))


@router.get("/runs/{identity}/tables/{name}")
def table(request: Request, identity: str, name: str, cursor: str | None = None, limit: int = 100,
          date_from: str | None = None, date_to: str | None = None, symbol: str | None = None,
          context_date: str | None = None):
    return asdict(service(request).table(identity, name, cursor, limit, date_from=date_from, date_to=date_to,
                                         symbol=symbol, context_date=context_date))


@router.get("/runs/{identity}/charts/{name}")
def chart(request: Request, identity: str, name: str, date_from: str | None = None,
          date_to: str | None = None, max_points: int = 1200):
    return service(request).chart(identity, name, date_from, date_to, max_points)


@router.get("/runs/{identity}/results")
def results(request: Request, identity: str):
    return service(request).results(identity)


@router.get("/runs/{identity}/exports/{format}")
def export(request: Request, identity: str, format: str):
    content, suffix = service(request).export(identity, format)
    media = "text/html" if format == "html" else "application/zip"
    filename = quote(f"回测-{identity}-{format}.{suffix}")
    return Response(content, media_type=media, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})


@router.post("/comparisons")
def comparison(request: Request, body: dict):
    run_ids = body.get("run_ids")
    if not isinstance(run_ids, list) or not 2 <= len(run_ids) <= 20 or not all(
        isinstance(run_id, str) and run_id for run_id in run_ids
    ):
        raise ValueError("run_ids must contain 2 to 20 run IDs")
    return service(request).comparison(run_ids)


@router.post("/uploads")
async def upload(request: Request, file: UploadFile = File(...)):
    identity = service(request).upload(file.filename or "", await file.read())
    return {"upload_id": identity}


@router.post("/jobs/data")
def data_job(request: Request, body: dict):
    if body.get("kind") not in {"demo", "fetch", "import"}:
        raise ValueError("invalid data job kind")
    return service(request).submit_job(body["kind"], body.get("payload", {}))


@router.post("/jobs/strategy-validation")
def strategy_validation(request: Request, body: dict):
    return service(request).submit_job("validate_strategy", body)


@router.get("/jobs/{identity}/logs")
def job_logs(request: Request, identity: str, cursor: str | None = None, limit: int = 100, tail: bool = False):
    return asdict(service(request).logs(identity, cursor, limit, tail))


@router.post("/jobs/{identity}/cancel")
def cancel_job(request: Request, identity: str):
    service(request).store.request_cancel(identity)
    return service(request).get_resource("jobs", identity)
