# Lab 12 — Complete Production Agent

Kết hợp TẤT CẢ những gì đã học trong 1 project hoàn chỉnh.

## Checklist Deliverable

- [x] Dockerfile (multi-stage, < 500 MB)
- [x] docker-compose.yml (agent + redis)
- [x] Nginx load balancer
- [x] .dockerignore
- [x] Health check endpoint (`GET /health`)
- [x] Readiness endpoint (`GET /ready`)
- [x] API Key authentication
- [x] Rate limiting
- [x] Cost guard
- [x] Conversation history in Redis
- [x] Stateless design
- [x] Config từ environment variables
- [x] Structured logging
- [x] Graceful shutdown
- [x] Public URL ready (Railway / Render config)

---

## Cấu Trúc

```
06-lab-complete/
├── app/
│   ├── main.py         # Entry point — kết hợp tất cả
│   ├── config.py       # 12-factor config
│   ├── auth.py         # API Key auth
│   ├── rate_limiter.py # Redis rate limiting
│   ├── cost_guard.py   # Redis budget protection
│   └── storage.py      # Redis session history
├── Dockerfile          # Multi-stage, production-ready
├── docker-compose.yml  # Full stack: nginx + agent + redis
├── nginx.conf          # Load balancer config
├── railway.toml        # Deploy Railway
├── render.yaml         # Deploy Render
├── .env.example        # Template
├── .dockerignore
└── requirements.txt
```

---

## Chạy Local

```bash
# 1. Setup
cp .env.example .env.local

# 2. Chạy với Docker Compose (scale 3 agent instances nếu muốn test LB)
docker compose up --scale agent=3

# 3. Test
curl http://localhost:8000/health
curl http://localhost:8000/ready

# 4. Lấy API key từ .env.local, test endpoint
API_KEY=$(grep AGENT_API_KEY .env.local | cut -d= -f2)
curl -H "X-API-Key: $API_KEY" \
     -X POST http://localhost:8000/ask \
     -H "Content-Type: application/json" \
     -d '{"question": "What is deployment?"}'

# 5. Tiếp tục conversation bằng session_id trả về ở response
curl -H "X-API-Key: $API_KEY" \
     http://localhost:8000/sessions/<session_id>/history
```

---

## Deploy Railway (< 5 phút)

```bash
# Cài Railway CLI
npm i -g @railway/cli

# Login và deploy
railway login
railway init
railway variables set OPENAI_API_KEY=sk-...
railway variables set AGENT_API_KEY=your-secret-key
railway up

# Nhận public URL!
railway domain
```

---

## Deploy Render

1. Push repo lên GitHub
2. Render Dashboard → New → Blueprint
3. Connect repo → Render đọc `render.yaml`
4. Set secrets: `OPENAI_API_KEY`, `AGENT_API_KEY`
5. Deploy → Nhận URL!

---

## Kiểm Tra Production Readiness

```bash
python3 check_production_ready.py
```

Script này kiểm tra tất cả items trong checklist và báo cáo những gì còn thiếu.
