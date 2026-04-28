[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/weberg619-cadre-ai-badge.png)](https://mseep.ai/app/weberg619-cadre-ai)

# CADRE·AI

**Voice-controlled AI agent for architects, engineers, and business professionals.**

[![Gemini Live API](https://img.shields.io/badge/Gemini-Live%20API-4285F4?logo=google&logoColor=white)](https://ai.google.dev/)
[![Google ADK](https://img.shields.io/badge/Google-ADK-34A853?logo=google&logoColor=white)](https://google.github.io/adk-docs/)
[![Cloud Run](https://img.shields.io/badge/Cloud%20Run-Ready-4285F4?logo=googlecloud&logoColor=white)](https://cloud.google.com/run)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Built for the [Gemini Live Agent Challenge](https://googleai.devpost.com/) — March 2026

<p align="center">
  <img src="assets/scene_03_tool_execution.png" alt="Cadre-AI Tool Execution" width="720">
</p>

---

## What It Does

Cadre-AI lets you **talk** to your building model, financial data, and the web — all through natural voice conversation. Ask "How many rooms on Level 1?" and hear the answer instantly while watching MCP tools execute in real-time. Say "Create a wall on Level 2" and see it appear in Revit.

This is **the first voice-controlled BIM automation agent** — nobody else has real-time Revit integration through voice.

---

## Architecture

<p align="center">
  <img src="architecture.svg" alt="Cadre-AI Architecture" width="800">
</p>

**Local mode:** All 3 MCP toolsets active, including Revit via named pipe.
**Cloud mode:** Financial + Web Search active. Revit disabled (`REVIT_ENABLED=false`).

---

## Features

### Architecture & BIM (25+ tools)
- Query levels, rooms, walls, doors, windows, views, sheets
- Create walls, doors, windows, rooms
- Place views on sheets, add dimensions
- Run QA/QC validation and compliance checks
- Generate schedules and reports

### Financial Intelligence (13 tools)
- Real-time stock quotes and market overview
- Technical analysis (RSI, MACD, moving averages)
- Fundamental analysis (P/E, revenue, earnings)
- Portfolio tracking and risk analysis
- News sentiment and Fear & Greed Index

### Web Search & Weather
- Web search via DuckDuckGo (no API key required)
- Weather forecasts via Open-Meteo
- Building codes, material specs, industry news

---

## Quick Start

### Prerequisites
- Python 3.11+
- [Google API Key](https://aistudio.google.com/apikey) with Gemini API enabled

### Local Setup (with Revit)

```bash
# Clone
git clone https://github.com/WeberG619/cadre-ai.git
cd cadre-ai

# Install
pip install -r requirements.txt

# Configure
cp .env.template .env
# Edit .env → add your GOOGLE_API_KEY

# Generate SSL certs (required for browser mic access)
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
  -days 365 -nodes -subj '/CN=localhost'

# Run
python server.py
# Open https://localhost:8443
```

For Revit integration: install [RevitMCPBridge2026](https://github.com/bimopsstudio/RevitMCPBridge2026) plugin in Revit 2026.

### Local Setup (without Revit)

```bash
REVIT_ENABLED=false python server.py
```

---

## Cloud Run Deployment

### Quick deploy (source-based)

```bash
gcloud run deploy cadre-ai \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars "GOOGLE_API_KEY=your-key,REVIT_ENABLED=false,CADRE_MODEL=gemini-2.5-flash-native-audio-latest"
```

### Terraform (IaC)

```bash
cd cloud/terraform
terraform init
terraform apply -var="project_id=your-project" -var="google_api_key=your-key"
```

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_API_KEY` | Yes | — | Gemini API key |
| `CADRE_MODEL` | No | `gemini-2.5-flash` | Model ID (`gemini-2.5-flash-native-audio-latest` for voice) |
| `REVIT_ENABLED` | No | `true` | Enable Revit MCP proxy |
| `PORT` | No | `8443` | Server port (Cloud Run sets this to 8080) |
| `FINNHUB_API_KEY` | No | — | Enhanced financial news |
| `ALPHA_VANTAGE_KEY` | No | — | Extended technical data |
| `FRED_API_KEY` | No | — | Federal Reserve economic data |
| `GOOGLE_CSE_ID` | No | — | Google Custom Search (falls back to DuckDuckGo) |

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| AI Model | Gemini 2.5 Flash (Native Audio) |
| Agent Framework | Google ADK (Agent Development Kit) |
| Tool Protocol | MCP (Model Context Protocol) via stdio |
| Server | FastAPI + Uvicorn |
| Audio | WebSocket bidirectional streaming (16kHz in, 24kHz out) |
| BIM Bridge | Named pipes → RevitMCPBridge2026 |
| Financial Data | yfinance, Finnhub, Alpha Vantage, FRED |
| Web Search | DuckDuckGo + Open-Meteo weather |
| Deployment | Cloud Run, Terraform |
| Frontend | Vanilla JS, Web Audio API, Canvas visualizer |

---

## Project Structure

```
cadre-ai/
├── cadre/
│   ├── __init__.py
│   └── agent.py            # Agent definition, MCP toolsets, system instruction
├── financial_mcp/
│   ├── __init__.py
│   └── server.py           # Financial MCP server (13 tools)
├── web_search_mcp/
│   ├── __init__.py
│   └── server.py           # Web search + weather MCP
├── revit_proxy_mcp/
│   └── server.py           # Revit named pipe proxy (25+ tools)
├── cloud/
│   └── terraform/
│       ├── main.tf          # Cloud Run + IAM
│       └── variables.tf     # Deployment variables
├── server.py               # FastAPI WebSocket server
├── voice_client.html       # Browser UI with visualizer
├── Dockerfile              # Cloud Run container
├── requirements.txt
├── .env.template
└── README.md
```

---

## How It Works

1. **Browser** captures microphone audio at 48kHz, resamples to 16kHz PCM
2. **WebSocket** streams audio chunks to the FastAPI server
3. **Google ADK Runner** feeds audio into **Gemini Live API** (bidirectional streaming)
4. Gemini processes speech, decides to use tools, and generates audio responses
5. **MCP tools** execute via stdio subprocess (financial queries, web search, or Revit commands)
6. Tool results feed back to Gemini, which generates a spoken response
7. **Audio response** streams back through WebSocket to the browser at 24kHz
8. **UI** shows real-time conversation transcript and tool activity with timing

---

## Hackathon

Built for the **Gemini Live Agent Challenge** (Google AI + Devpost, March 2026).

- **Innovation/UX:** Voice-first BIM automation — a genuinely new capability
- **Technical:** ADK + MCP + Named Pipes bridging WSL2 → Windows → Revit
- **Real-world:** Architects spend hours clicking through Revit menus. Voice commands collapse that to seconds.

---

## Author

**Weber Gouin** — [BIM Ops Studio](https://bimopsstudio.com)

Principal / BIM Specialist. Building the bridge between AI and architecture.

---

## License

MIT
