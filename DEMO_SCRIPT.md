# Cadre-AI Demo Script (60 seconds)

## Setup
- Open https://cadreai.dev (or Cloud Run URL)
- Have a stock chart image ready to drag-drop

## Script

### 1. Voice Greeting (10s)
*Click mic, wait for connection*
> "Hey Cadre, what can you do?"
*Let it respond — shows voice works*

### 2. Financial Data (15s)
> "What's the stock price of Tesla right now?"
*Watch the tool panel light up — financial_mcp runs*
*Response includes real-time price + analysis*

### 3. Image Analysis (15s)
*Drag a chart/screenshot onto the conversation*
> "What do you see in this chart?"
*Shows multimodal — Cadre analyzes the image via voice*

### 4. Web Search (15s)
> "Search for the latest news about AI agents"
*web_search_mcp activates, results appear inline*
*Cadre summarizes the findings by voice*

### 5. Screen Capture (5s)
*Click screen capture button, share screen briefly*
> "What's on my screen?"
*Shows screen capture → analysis pipeline*

## Key Points for Judges
- **Real-time bidirectional audio** — true conversation, not request/response
- **Multimodal** — voice + images + screen capture
- **Live tool use** — financial data, web search visible in tool panel
- **Production deployed** — cadreai.dev with Cloud Run, not localhost
- **Built on Google ADK + Gemini 2.5 Flash Native Audio**
