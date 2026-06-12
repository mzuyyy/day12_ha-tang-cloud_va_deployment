"""
Production AI Agent - final Day 12 lab.

Includes:
- 12-factor config
- API key authentication
- Redis-backed rate limiting and cost guard
- Redis-backed conversation history
- health/readiness endpoints
- structured JSON logs
- graceful shutdown hooks
"""
import json
import logging
import os
import signal
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

from app.auth import verify_api_key
from app.config import settings
from app.cost_guard import check_budget, get_usage, record_usage
from app.rate_limiter import check_rate_limit
from app.storage import (
    RedisUnavailable,
    append_message,
    delete_session,
    load_history,
    ping_redis,
)
from utils.llm_client import ask as llm_ask, get_llm_mode


logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
)
logger = logging.getLogger(__name__)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
        }
        try:
            message = json.loads(record.getMessage())
            if isinstance(message, dict):
                payload.update(message)
            else:
                payload["message"] = message
        except json.JSONDecodeError:
            payload["message"] = record.getMessage()
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


for handler in logging.getLogger().handlers:
    handler.setFormatter(JsonFormatter())

START_TIME = time.time()
INSTANCE_ID = os.getenv("INSTANCE_ID", f"agent-{uuid.uuid4().hex[:8]}")
_is_ready = False
_request_count = 0
_error_count = 0


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=80)


class AskResponse(BaseModel):
    session_id: str
    question: str
    answer: str
    model: str
    history_length: int
    served_by: str
    timestamp: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _is_ready
    logger.info(json.dumps({
        "event": "startup",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "instance_id": INSTANCE_ID,
    }))

    try:
        ping_redis()
        _is_ready = True
        logger.info(json.dumps({"event": "ready", "redis": "ok"}))
    except RedisUnavailable as exc:
        _is_ready = False
        logger.error(json.dumps({"event": "not_ready", "redis": "unavailable", "error": str(exc)}))

    yield

    _is_ready = False
    logger.info(json.dumps({"event": "shutdown", "instance_id": INSTANCE_ID}))


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    global _request_count, _error_count
    started = time.time()
    _request_count += 1

    try:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Instance-ID"] = INSTANCE_ID
        if "server" in response.headers:
            del response.headers["server"]
        logger.info(json.dumps({
            "event": "request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.time() - started) * 1000, 1),
            "instance_id": INSTANCE_ID,
        }))
        return response
    except Exception:
        _error_count += 1
        logger.exception(json.dumps({
            "event": "request_error",
            "method": request.method,
            "path": request.url.path,
        }))
        raise


@app.get("/", response_class=HTMLResponse, tags=["UI"])
def root():
    return HTMLResponse(content=render_ui())


@app.get("/info", tags=["Info"])
def info():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "instance_id": INSTANCE_ID,
        "endpoints": {
            "ask": "POST /ask (requires X-API-Key)",
            "history": "GET /sessions/{session_id}/history (requires X-API-Key)",
            "health": "GET /health",
            "ready": "GET /ready",
            "metrics": "GET /metrics (requires X-API-Key)",
        },
    }


@app.post("/ask", response_model=AskResponse, tags=["Agent"])
async def ask_agent(
    body: AskRequest,
    request: Request,
    user_id: str = Depends(verify_api_key),
):
    check_rate_limit(user_id)

    input_tokens = estimate_tokens(body.question)
    check_budget(user_id, estimated_cost_usd=estimate_cost(input_tokens, 0))

    session_id = body.session_id or str(uuid.uuid4())
    append_message(session_id, "user", body.question, user_id=user_id)

    logger.info(json.dumps({
        "event": "agent_call",
        "user_id": user_id,
        "session_id": session_id,
        "question_length": len(body.question),
        "client": str(request.client.host) if request.client else "unknown",
    }))

    answer = llm_ask(body.question)
    output_tokens = estimate_tokens(answer)
    check_budget(user_id, estimated_cost_usd=estimate_cost(0, output_tokens))
    record_usage(user_id, input_tokens=input_tokens, output_tokens=output_tokens)

    history = append_message(session_id, "assistant", answer, user_id=user_id)

    return AskResponse(
        session_id=session_id,
        question=body.question,
        answer=answer,
        model=settings.llm_model,
        history_length=len(history),
        served_by=INSTANCE_ID,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/sessions/{session_id}/history", tags=["Agent"])
def get_session_history(session_id: str, user_id: str = Depends(verify_api_key)):
    history = load_history(session_id, user_id=user_id)
    if not history:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return {
        "session_id": session_id,
        "messages": history,
        "count": len(history),
    }


@app.delete("/sessions/{session_id}", tags=["Agent"])
def remove_session(session_id: str, user_id: str = Depends(verify_api_key)):
    delete_session(session_id, user_id=user_id)
    return {"deleted": session_id}


@app.get("/health", tags=["Operations"])
def health():
    redis_ok = True
    try:
        ping_redis()
    except RedisUnavailable:
        redis_ok = False

    return {
        "status": "ok" if redis_ok else "degraded",
        "version": settings.app_version,
        "environment": settings.environment,
        "instance_id": INSTANCE_ID,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "checks": {
            "redis": "ok" if redis_ok else "unavailable",
            "llm": get_llm_mode(),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/ready", tags=["Operations"])
def ready():
    if not _is_ready:
        raise HTTPException(status_code=503, detail="Application is not ready")
    try:
        ping_redis()
    except RedisUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {exc}")
    return {"ready": True, "instance_id": INSTANCE_ID}


@app.get("/metrics", tags=["Operations"])
def metrics(user_id: str = Depends(verify_api_key)):
    usage = get_usage(user_id)
    return {
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "error_count": _error_count,
        "instance_id": INSTANCE_ID,
        "usage": usage,
    }


def estimate_tokens(text: str) -> int:
    return max(1, len(text.split()) * 2)


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    input_cost = (input_tokens / 1000) * settings.price_per_1k_input_tokens
    output_cost = (output_tokens / 1000) * settings.price_per_1k_output_tokens
    return input_cost + output_cost


def _handle_signal(signum, _frame):
    logger.info(json.dumps({"event": "signal", "signum": signum, "instance_id": INSTANCE_ID}))


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def render_ui() -> str:
    ui_api_key = json.dumps(settings.agent_api_key)
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Production AI Agent</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1e252c;
      --muted: #64707d;
      --line: #d9dee5;
      --primary: #0f766e;
      --primary-dark: #0b5f59;
      --danger: #b42318;
      --ok: #087443;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .shell {
      max-width: 1120px;
      margin: 0 auto;
      padding: 24px;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 0 20px;
      border-bottom: 1px solid var(--line);
    }
    h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1.2;
      font-weight: 700;
      letter-spacing: 0;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }
    .pill {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 999px;
      padding: 6px 10px;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      gap: 20px;
      padding-top: 20px;
    }
    section, aside {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 16px;
      line-height: 1.3;
      letter-spacing: 0;
    }
    label {
      display: block;
      margin: 12px 0 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }
    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      font: inherit;
      color: var(--text);
      background: #fff;
    }
    textarea {
      min-height: 132px;
      resize: vertical;
    }
    .row {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    button {
      border: 0;
      border-radius: 6px;
      background: var(--primary);
      color: #fff;
      font: inherit;
      font-weight: 700;
      padding: 10px 14px;
      cursor: pointer;
    }
    button.secondary {
      background: #e8eef2;
      color: var(--text);
    }
    button:hover { background: var(--primary-dark); }
    button.secondary:hover { background: #dce5eb; }
    .status {
      display: grid;
      gap: 10px;
    }
    .status-item {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
      font-size: 14px;
    }
    .status-item:last-child { border-bottom: 0; }
    .value {
      color: var(--muted);
      text-align: right;
      overflow-wrap: anywhere;
    }
    .ok { color: var(--ok); }
    .bad { color: var(--danger); }
    .answer, .history {
      margin-top: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfd;
      padding: 12px;
      min-height: 72px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .history {
      max-height: 320px;
      overflow: auto;
      font-size: 14px;
    }
    .hint {
      color: var(--muted);
      font-size: 13px;
      margin-top: 8px;
    }
    @media (max-width: 860px) {
      .shell { padding: 16px; }
      header { align-items: flex-start; flex-direction: column; }
      .meta { justify-content: flex-start; }
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <h1>Production AI Agent</h1>
      <div class="meta">
        <span class="pill" id="env">env</span>
        <span class="pill" id="instance">instance</span>
      </div>
    </header>
    <div class="grid">
      <main>
        <section>
          <h2>Ask Agent</h2>
          <label for="question">Question</label>
          <textarea id="question">What is Docker?</textarea>
          <div class="row" style="margin-top: 12px;">
            <button id="askBtn">Send</button>
            <button class="secondary" id="clearBtn">Clear</button>
          </div>
          <div class="hint" id="sessionHint">No session yet</div>
          <div class="answer" id="answer">Response will appear here.</div>
        </section>
        <section style="margin-top: 20px;">
          <h2>Conversation History</h2>
          <div class="row">
            <button class="secondary" id="historyBtn">Refresh History</button>
            <button class="secondary" id="deleteBtn">Delete Session</button>
          </div>
          <div class="history" id="history">History will appear here.</div>
        </section>
      </main>
      <aside>
        <h2>Runtime Status</h2>
        <div class="status">
          <div class="status-item"><strong>Health</strong><span class="value" id="health">checking</span></div>
          <div class="status-item"><strong>Ready</strong><span class="value" id="ready">checking</span></div>
          <div class="status-item"><strong>Redis</strong><span class="value" id="redis">checking</span></div>
          <div class="status-item"><strong>Requests</strong><span class="value" id="requests">0</span></div>
          <div class="status-item"><strong>Uptime</strong><span class="value" id="uptime">0s</span></div>
        </div>
      </aside>
    </div>
  </div>
  <script>
    const apiKey = """ + ui_api_key + """;
    const question = document.getElementById("question");
    const answer = document.getElementById("answer");
    const historyBox = document.getElementById("history");
    const sessionHint = document.getElementById("sessionHint");
    let sessionId = localStorage.getItem("agent_session_id") || "";
    updateSessionHint();

    async function refreshStatus() {
      try {
        const health = await fetch("/health").then(r => r.json());
        document.getElementById("health").textContent = health.status;
        document.getElementById("health").className = "value ok";
        document.getElementById("redis").textContent = health.checks.redis;
        document.getElementById("redis").className = "value ok";
        document.getElementById("requests").textContent = health.total_requests;
        document.getElementById("uptime").textContent = `${health.uptime_seconds}s`;
        document.getElementById("env").textContent = health.environment;
        document.getElementById("instance").textContent = health.instance_id;
      } catch (err) {
        document.getElementById("health").textContent = "error";
        document.getElementById("health").className = "value bad";
      }
      try {
        const readyResp = await fetch("/ready");
        document.getElementById("ready").textContent = readyResp.ok ? "true" : "false";
        document.getElementById("ready").className = readyResp.ok ? "value ok" : "value bad";
      } catch (err) {
        document.getElementById("ready").textContent = "error";
        document.getElementById("ready").className = "value bad";
      }
    }

    async function askAgent() {
      answer.textContent = "Sending...";
      const payload = { question: question.value.trim() };
      if (sessionId) payload.session_id = sessionId;
      const resp = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
        body: JSON.stringify(payload)
      });
      const data = await resp.json();
      if (!resp.ok) {
        answer.textContent = JSON.stringify(data, null, 2);
        return;
      }
      sessionId = data.session_id;
      localStorage.setItem("agent_session_id", sessionId);
      updateSessionHint();
      answer.textContent = data.answer;
      await loadHistory();
      await refreshStatus();
    }

    async function loadHistory() {
      if (!sessionId) {
        historyBox.textContent = "No session yet.";
        return;
      }
      const resp = await fetch(`/sessions/${sessionId}/history`, {
        headers: { "X-API-Key": apiKey }
      });
      const data = await resp.json();
      if (!resp.ok) {
        historyBox.textContent = JSON.stringify(data, null, 2);
        return;
      }
      historyBox.textContent = data.messages
        .map(m => `${m.role.toUpperCase()}: ${m.content}`)
        .join("\\n\\n");
    }

    async function deleteSession() {
      if (!sessionId) return;
      await fetch(`/sessions/${sessionId}`, {
        method: "DELETE",
        headers: { "X-API-Key": apiKey }
      });
      sessionId = "";
      localStorage.removeItem("agent_session_id");
      updateSessionHint();
      historyBox.textContent = "History cleared.";
      answer.textContent = "Response will appear here.";
    }

    function updateSessionHint() {
      sessionHint.textContent = sessionId ? `Session: ${sessionId}` : "No session yet";
    }

    document.getElementById("askBtn").addEventListener("click", askAgent);
    document.getElementById("historyBtn").addEventListener("click", loadHistory);
    document.getElementById("deleteBtn").addEventListener("click", deleteSession);
    document.getElementById("clearBtn").addEventListener("click", () => {
      question.value = "";
      answer.textContent = "Response will appear here.";
    });
    refreshStatus();
    setInterval(refreshStatus, 5000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    logger.info(json.dumps({
        "event": "run",
        "host": settings.host,
        "port": settings.port,
        "debug": settings.debug,
    }))
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        timeout_graceful_shutdown=30,
    )
