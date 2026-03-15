"""
Web Search MCP Server for Cadre-AI
Provides web search and weather lookup as standard MCP tools.
Uses Google Custom Search API or fallback scraping.
"""

import base64
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("web-search-mcp")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
# Google Custom Search Engine ID — if not set, uses fallback
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def google_custom_search(query: str, num_results: int = 5) -> list[dict]:
    """Use Google Custom Search JSON API."""
    params = urllib.parse.urlencode({
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "num": min(num_results, 10),
    })
    url = f"https://www.googleapis.com/customsearch/v1?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    results = []
    for item in data.get("items", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })
    return results


def duckduckgo_search(query: str, num_results: int = 5) -> list[dict]:
    """Fallback: use DuckDuckGo instant answer API + HTML search."""
    # DuckDuckGo instant answer API
    params = urllib.parse.urlencode({"q": query, "format": "json", "no_html": "1"})
    url = f"https://api.duckduckgo.com/?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    results = []
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        # Abstract (Wikipedia-style answer)
        if data.get("Abstract"):
            results.append({
                "title": data.get("Heading", "Answer"),
                "url": data.get("AbstractURL", ""),
                "snippet": data["Abstract"],
            })

        # Related topics
        for topic in data.get("RelatedTopics", [])[:num_results]:
            if isinstance(topic, dict) and "Text" in topic:
                results.append({
                    "title": topic.get("Text", "")[:80],
                    "url": topic.get("FirstURL", ""),
                    "snippet": topic.get("Text", ""),
                })
    except Exception:
        pass

    # If no results from instant answer, try DuckDuckGo HTML
    if not results:
        try:
            html_url = f"https://html.duckduckgo.com/html/?{urllib.parse.urlencode({'q': query})}"
            req = urllib.request.Request(html_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            # Simple parsing of DuckDuckGo HTML results
            class DDGParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.results = []
                    self.in_result = False
                    self.in_title = False
                    self.in_snippet = False
                    self.current = {}

                def handle_starttag(self, tag, attrs):
                    attrs_dict = dict(attrs)
                    cls = attrs_dict.get("class", "")
                    if tag == "a" and "result__a" in cls:
                        self.in_title = True
                        self.current["url"] = attrs_dict.get("href", "")
                        self.current["title"] = ""
                    if tag == "a" and "result__snippet" in cls:
                        self.in_snippet = True
                        self.current["snippet"] = ""

                def handle_data(self, data):
                    if self.in_title:
                        self.current["title"] += data
                    if self.in_snippet:
                        self.current["snippet"] = self.current.get("snippet", "") + data

                def handle_endtag(self, tag):
                    if tag == "a" and self.in_title:
                        self.in_title = False
                    if tag == "a" and self.in_snippet:
                        self.in_snippet = False
                        if self.current.get("title"):
                            self.results.append(dict(self.current))
                            self.current = {}

            parser = DDGParser()
            parser.feed(html)
            results = parser.results[:num_results]
        except Exception:
            pass

    return results[:num_results]


def geocode_location(location: str) -> tuple[float, float, str]:
    """Geocode a location name to lat/lon using Open-Meteo geocoding API."""
    # Try full location first, then just the city name (API doesn't like commas)
    queries = [location, location.split(",")[0].strip()]
    for query in queries:
        params = urllib.parse.urlencode({"name": query, "count": 3, "language": "en", "format": "json"})
        url = f"https://geocoding-api.open-meteo.com/v1/search?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        results = data.get("results", [])
        if results:
            r = results[0]
            parts = [r.get('name', location), r.get('admin1', ''), r.get('country', '')]
            display = ", ".join(p for p in parts if p)
            return r["latitude"], r["longitude"], display

    raise ValueError(f"Could not find location: {location}")


# WMO weather code descriptions
WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains", 80: "Slight rain showers", 81: "Moderate rain showers",
    82: "Violent rain showers", 85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def get_weather_data(location: str) -> dict:
    """Get weather using Open-Meteo API (free, no API key, very reliable)."""
    lat, lon, display_name = geocode_location(location)

    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,surface_pressure",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "forecast_days": 3,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    current = data.get("current", {})
    weather_code = current.get("weather_code", 0)

    weather = {
        "location": display_name,
        "temperature_f": current.get("temperature_2m", "N/A"),
        "feels_like_f": current.get("apparent_temperature", "N/A"),
        "condition": WMO_CODES.get(weather_code, f"Code {weather_code}"),
        "humidity": current.get("relative_humidity_2m", "N/A"),
        "wind_mph": current.get("wind_speed_10m", "N/A"),
        "wind_direction_deg": current.get("wind_direction_10m", "N/A"),
        "pressure_mb": current.get("surface_pressure", "N/A"),
        "timezone": data.get("timezone", ""),
    }

    # Add forecast
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    maxs = daily.get("temperature_2m_max", [])
    mins = daily.get("temperature_2m_min", [])
    codes = daily.get("weather_code", [])

    forecast = []
    for i in range(len(dates)):
        forecast.append({
            "date": dates[i],
            "max_f": maxs[i] if i < len(maxs) else "",
            "min_f": mins[i] if i < len(mins) else "",
            "condition": WMO_CODES.get(codes[i], "") if i < len(codes) else "",
        })
    weather["forecast"] = forecast

    return weather


# ── Deep Search Helpers ───────────────────────────────────────────────────────


def extract_page_content(url: str, max_chars: int = 3000) -> str:
    """Fetch a web page and extract its text content (strips HTML)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            # Try utf-8, fall back to latin-1
            try:
                html = raw.decode("utf-8")
            except UnicodeDecodeError:
                html = raw.decode("latin-1", errors="replace")

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
                self._skip = {"script", "style", "nav", "header", "footer", "noscript", "svg"}
                self._depth = 0

            def handle_starttag(self, tag, attrs):
                if tag in self._skip:
                    self._depth += 1

            def handle_endtag(self, tag):
                if tag in self._skip and self._depth > 0:
                    self._depth -= 1

            def handle_data(self, data):
                if self._depth == 0:
                    t = data.strip()
                    if t:
                        self.parts.append(t)

        ext = TextExtractor()
        ext.feed(html)
        text = " ".join(ext.parts)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"[Could not extract: {e}]"


def deep_web_search(query: str, num_results: int = 5) -> dict:
    """Enhanced search: initial results + page content extraction + auto-refine."""
    # Phase 1: initial search
    if GOOGLE_API_KEY and GOOGLE_CSE_ID:
        try:
            results = google_custom_search(query, num_results)
        except Exception:
            results = duckduckgo_search(query, num_results)
    else:
        results = duckduckgo_search(query, num_results)

    if not results:
        return {"query": query, "results": [], "deep": True, "message": "No results found"}

    # Phase 2: extract full page content for top 2 results
    for result in results[:2]:
        url = result.get("url", "")
        if url and url.startswith("http"):
            result["page_content"] = extract_page_content(url)

    # Phase 3: if results are thin, auto-refine with a second search
    avg_snippet = sum(len(r.get("snippet", "")) for r in results) / max(len(results), 1)
    if avg_snippet < 30 and len(results) < 3:
        refined_query = f"{query} detailed information guide"
        try:
            if GOOGLE_API_KEY and GOOGLE_CSE_ID:
                extra = google_custom_search(refined_query, 3)
            else:
                extra = duckduckgo_search(refined_query, 3)
            existing_urls = {r["url"] for r in results}
            for r in extra:
                if r["url"] not in existing_urls:
                    results.append(r)
                    existing_urls.add(r["url"])
        except Exception:
            pass

    return {"query": query, "results": results[:num_results + 2], "deep": True}


# ── Image Search ─────────────────────────────────────────────────────────────


def google_image_search(query: str, num_results: int = 5) -> list[dict]:
    """Google Custom Search with searchType=image. Returns direct image URLs."""
    params = urllib.parse.urlencode({
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "searchType": "image",
        "num": min(num_results, 10),
    })
    url = f"https://www.googleapis.com/customsearch/v1?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    results = []
    for item in data.get("items", []):
        results.append({
            "title": item.get("title", ""),
            "image_url": item.get("link", ""),
            "thumbnail": item.get("image", {}).get("thumbnailLink", ""),
            "source_url": item.get("image", {}).get("contextLink", ""),
            "width": item.get("image", {}).get("width", 0),
            "height": item.get("image", {}).get("height", 0),
        })
    return results


def wikimedia_image_search(query: str, num_results: int = 5) -> list[dict]:
    """Search Wikimedia Commons for images. Free, no API key, reliable direct URLs."""
    params = urllib.parse.urlencode({
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": str(min(num_results + 3, 20)),
        "prop": "imageinfo",
        "iiprop": "url|size|mime",
        "iiurlwidth": "1024",
        "format": "json",
    })
    url = f"https://commons.wikimedia.org/w/api.php?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    results = []
    pages = data.get("query", {}).get("pages", {})
    for pid, page in list(pages.items()):
        if len(results) >= num_results:
            break
        ii = page.get("imageinfo", [{}])[0]
        mime = ii.get("mime", "")
        if not mime.startswith("image/"):
            continue
        title = page.get("title", "").replace("File:", "").rsplit(".", 1)[0].replace("_", " ")
        results.append({
            "title": title,
            "image_url": ii.get("thumburl", ii.get("url", "")),
            "full_url": ii.get("url", ""),
            "width": ii.get("thumbwidth", ii.get("width", 0)),
            "height": ii.get("thumbheight", ii.get("height", 0)),
            "source": "Wikimedia Commons",
        })
    return results


# ── Video Search ─────────────────────────────────────────────────────────────


def youtube_search(query: str, num_results: int = 3) -> list[dict]:
    """Search YouTube by scraping search results page. No API key needed."""
    try:
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # YouTube embeds search data as JSON in ytInitialData
        match = re.search(r"ytInitialData\s*=\s*({.*?});", html)
        if not match:
            return []

        data = json.loads(match.group(1))
        contents = (
            data.get("contents", {})
            .get("twoColumnSearchResultsRenderer", {})
            .get("primaryContents", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
        )

        results = []
        for section in contents:
            items = section.get("itemSectionRenderer", {}).get("contents", [])
            for item in items:
                vr = item.get("videoRenderer", {})
                if not vr:
                    continue
                video_id = vr.get("videoId", "")
                if not video_id:
                    continue
                title = vr.get("title", {}).get("runs", [{}])[0].get("text", "")
                channel = vr.get("ownerText", {}).get("runs", [{}])[0].get("text", "")
                thumb = vr.get("thumbnail", {}).get("thumbnails", [{}])[-1].get("url", "")
                results.append({
                    "title": title,
                    "video_id": video_id,
                    "video_url": f"https://www.youtube.com/watch?v={video_id}",
                    "embed_url": f"https://www.youtube.com/embed/{video_id}",
                    "thumbnail": thumb,
                    "channel": channel,
                })
                if len(results) >= num_results:
                    break
            if results:
                break
        return results
    except Exception as e:
        print(f"[video_search] YouTube error: {e}", flush=True)
        return []


# ── AI Image Generation ──────────────────────────────────────────────────────


def generate_image_impl(prompt: str, aspect_ratio: str = "1:1", negative_prompt: str = "") -> dict:
    """Generate an image using Google Imagen 4."""
    if not GOOGLE_API_KEY:
        return {"error": "GOOGLE_API_KEY not set"}
    try:
        from google import genai
        client = genai.Client(api_key=GOOGLE_API_KEY)
        config = {
            "number_of_images": 1,
            "aspect_ratio": aspect_ratio,
            "output_mime_type": "image/jpeg",
        }
        if negative_prompt:
            config["negative_prompt"] = negative_prompt

        response = client.models.generate_images(
            model="imagen-4.0-generate-001",
            prompt=prompt,
            config=config,
        )
        if response.generated_images:
            img = response.generated_images[0]
            img_b64 = base64.b64encode(img.image.image_bytes).decode("ascii")
            return {
                "success": True,
                "prompt": prompt,
                "image_base64": img_b64,
                "mime_type": "image/jpeg",
                "data_uri": f"data:image/jpeg;base64,{img_b64}",
            }
        return {"error": "No image generated", "prompt": prompt}
    except Exception as e:
        return {"error": str(e), "prompt": prompt}


# ── Academic Search ──────────────────────────────────────────────────────────


def search_semantic_scholar(query: str, limit: int = 5) -> list[dict]:
    """Search Semantic Scholar for academic papers."""
    import time
    params = urllib.parse.urlencode({
        "query": query,
        "limit": limit,
        "fields": "title,abstract,year,authors,citationCount,isOpenAccess,openAccessPdf",
    })
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # Semantic Scholar rate limits at 1 req/sec without API key
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(3)
                continue
            raise
    else:
        return []

    results = []
    for paper in data.get("data", []):
        authors = [a.get("name", "") for a in paper.get("authors", [])[:3]]
        pdf_url = ""
        oa_pdf = paper.get("openAccessPdf")
        if oa_pdf and isinstance(oa_pdf, dict):
            pdf_url = oa_pdf.get("url", "")
        results.append({
            "title": paper.get("title", ""),
            "abstract": (paper.get("abstract") or "")[:500],
            "authors": authors,
            "year": paper.get("year"),
            "citations": paper.get("citationCount", 0),
            "open_access": paper.get("isOpenAccess", False),
            "pdf_url": pdf_url,
            "source": "Semantic Scholar",
        })
    return results


def search_arxiv(query: str, limit: int = 5) -> list[dict]:
    """Search arXiv for papers."""
    params = urllib.parse.urlencode({
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": limit,
        "sortBy": "relevance",
    })
    url = f"http://export.arxiv.org/api/query?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        xml_data = resp.read()

    root = ET.fromstring(xml_data)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    results = []
    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
        abstract = (entry.findtext("atom:summary", "", ns) or "").strip().replace("\n", " ")[:500]
        authors = [a.findtext("atom:name", "", ns) for a in entry.findall("atom:author", ns)[:3]]
        published = entry.findtext("atom:published", "", ns)[:4]  # year
        # Get PDF link
        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            if link.get("title") == "pdf":
                pdf_url = link.get("href", "")
                break
        results.append({
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "year": int(published) if published.isdigit() else None,
            "pdf_url": pdf_url,
            "source": "arXiv",
        })
    return results


def search_papers(query: str, limit: int = 5) -> dict:
    """Combined Semantic Scholar + arXiv search."""
    papers = []
    try:
        papers.extend(search_semantic_scholar(query, limit))
    except Exception as e:
        print(f"[search_papers] Semantic Scholar error: {e}", flush=True)
    try:
        papers.extend(search_arxiv(query, limit))
    except Exception as e:
        print(f"[search_papers] arXiv error: {e}", flush=True)

    # Deduplicate by title similarity
    seen = set()
    unique = []
    for p in papers:
        key = p["title"].lower()[:60]
        if key not in seen:
            seen.add(key)
            unique.append(p)

    # Sort by citations (descending), then year (newest first)
    unique.sort(key=lambda p: (-(p.get("citations") or 0), -(p.get("year") or 0)))
    return {"query": query, "papers": unique[:limit * 2], "total": len(unique)}


# ── Wikipedia ────────────────────────────────────────────────────────────────


def wikipedia_lookup_impl(query: str, full_article: bool = False) -> dict:
    """Get Wikipedia article content."""
    # Search for the best matching article
    params = urllib.parse.urlencode({
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": "3",
        "format": "json",
    })
    url = f"https://en.wikipedia.org/w/api.php?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    results = data.get("query", {}).get("search", [])
    if not results:
        return {"query": query, "error": "No Wikipedia articles found"}

    title = results[0]["title"]

    # Get article summary via REST API
    encoded_title = urllib.parse.quote(title.replace(" ", "_"))
    summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded_title}"
    req = urllib.request.Request(summary_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        summary = json.loads(resp.read())

    result = {
        "title": summary.get("title", title),
        "description": summary.get("description", ""),
        "extract": summary.get("extract", ""),
        "url": summary.get("content_urls", {}).get("desktop", {}).get("page", ""),
        "thumbnail": summary.get("thumbnail", {}).get("source", ""),
    }

    if full_article:
        # Get full article text
        params = urllib.parse.urlencode({
            "action": "query",
            "titles": title,
            "prop": "extracts",
            "explaintext": "1",
            "format": "json",
        })
        url = f"https://en.wikipedia.org/w/api.php?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        pages = data.get("query", {}).get("pages", {})
        for pid, page in pages.items():
            result["full_text"] = (page.get("extract", ""))[:5000]
            break

    return result


# ── Tool Definitions ─────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description="Search the web for information. Returns titles, URLs, and snippets. Set deep=true for enhanced research with full page content extraction from top results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 10)",
                        "default": 5,
                    },
                    "deep": {
                        "type": "boolean",
                        "description": "If true, fetches full page content from top results for deeper analysis. Use for complex research questions.",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="image_search",
            description="Search for images on the web. Returns DIRECT image URLs (jpg/png) that render inline in the chat. Use when the user asks to see images, photos, or visual references. ALWAYS display results using markdown: ![title](image_url)",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search images for (e.g. 'modern residential house', 'steel beam detail')",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of images to return (default 4, max 10)",
                        "default": 4,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="video_search",
            description="Search YouTube for videos. Returns video URLs and embed links that auto-play inline in the chat. Use when the user asks to see videos, tutorials, walkthroughs, or any video content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search videos for (e.g. 'how to frame a wall', 'modern house tour')",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of videos to return (default 3, max 10)",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_weather",
            description="Get current weather conditions and 3-day forecast for any location.",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name or location (e.g. 'Miami, FL' or 'Sandpoint, Idaho')",
                    },
                },
                "required": ["location"],
            },
        ),
        Tool(
            name="generate_image",
            description="Generate an AI image using Google Imagen 4. Creates original images from text descriptions. Use when users ask you to create, design, draw, or visualize something that doesn't exist yet. Do NOT use for finding real photos — use image_search for that.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the image to generate (e.g. 'A modern glass house on a cliff overlooking the ocean at sunset, photorealistic')",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": "Aspect ratio: '1:1' (square), '16:9' (landscape), '9:16' (portrait), '4:3', '3:4'",
                        "default": "1:1",
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Things to avoid in the image (e.g. 'blurry, low quality, text')",
                        "default": "",
                    },
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="search_papers",
            description="Search academic papers on Semantic Scholar and arXiv. Returns titles, abstracts, authors, citation counts, and PDF links. Use for research questions, scientific topics, or finding authoritative sources.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Research topic or question (e.g. 'transformer architecture attention mechanism')",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of papers to return (default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="wikipedia_lookup",
            description="Look up structured Wikipedia content. Returns article summary, description, thumbnail, and optionally full article text. Use for background knowledge, definitions, historical context, or factual information.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topic to look up (e.g. 'reinforced concrete', 'history of steel')",
                    },
                    "full_article": {
                        "type": "boolean",
                        "description": "If true, returns full article text (up to 5000 chars) instead of just the summary",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="deep_research",
            description="Comprehensive multi-source research. Runs web search + academic papers + Wikipedia in parallel, then aggregates into a structured research brief. Use for complex questions that need multiple perspectives and authoritative sources.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Research question or topic (e.g. 'impact of mass timber on building codes')",
                    },
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "web_search":
            query = arguments["query"]
            num = max(1, min(arguments.get("num_results", 5), 10))
            deep = arguments.get("deep", False)

            if deep:
                result = deep_web_search(query, num)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            # Standard search: Google CSE first, fall back to DuckDuckGo
            if GOOGLE_API_KEY and GOOGLE_CSE_ID:
                try:
                    results = google_custom_search(query, num)
                except Exception:
                    results = duckduckgo_search(query, num)
            else:
                results = duckduckgo_search(query, num)

            if not results:
                return [TextContent(type="text", text=json.dumps({"query": query, "results": [], "message": "No results found"}))]

            return [TextContent(type="text", text=json.dumps({"query": query, "results": results}, indent=2))]

        elif name == "image_search":
            query = arguments["query"]
            num = max(1, min(arguments.get("num_results", 4), 10))

            images = []
            if GOOGLE_API_KEY and GOOGLE_CSE_ID:
                try:
                    images = google_image_search(query, num)
                except Exception:
                    images = wikimedia_image_search(query, num)
            else:
                images = wikimedia_image_search(query, num)

            if not images:
                return [TextContent(type="text", text=json.dumps({
                    "query": query, "images": [], "message": "No images found"
                }))]

            result = {
                "query": query,
                "images": images,
                "display_instruction": "DISPLAY each image using: ![title](image_url)",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "video_search":
            query = arguments["query"]
            num = max(1, min(arguments.get("num_results", 3), 10))

            videos = youtube_search(query, num)
            if not videos:
                return [TextContent(type="text", text=json.dumps({
                    "query": query, "videos": [], "message": "No videos found"
                }))]

            result = {
                "query": query,
                "videos": videos,
                "display_instruction": "Videos will auto-embed in the chat. Describe what the user can watch.",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_weather":
            location = arguments["location"]
            weather = get_weather_data(location)
            return [TextContent(type="text", text=json.dumps(weather, indent=2))]

        elif name == "generate_image":
            prompt = arguments["prompt"]
            aspect_ratio = arguments.get("aspect_ratio", "1:1")
            negative_prompt = arguments.get("negative_prompt", "")
            result = generate_image_impl(prompt, aspect_ratio, negative_prompt)
            # Strip the large base64 from the tool response text (server handles it via _enrich)
            # but keep the data_uri for the enricher to pick up
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "search_papers":
            query = arguments["query"]
            num = max(1, min(arguments.get("num_results", 5), 10))
            result = search_papers(query, num)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "wikipedia_lookup":
            query = arguments["query"]
            full = arguments.get("full_article", False)
            result = wikipedia_lookup_impl(query, full)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "deep_research":
            query = arguments["query"]
            # Run all three searches
            brief = {"query": query, "sources": {}}

            # Web search
            try:
                web = deep_web_search(query, 3)
                brief["sources"]["web"] = web.get("results", [])
            except Exception as e:
                brief["sources"]["web"] = [{"error": str(e)}]

            # Academic papers
            try:
                papers = search_papers(query, 5)
                brief["sources"]["academic"] = papers.get("papers", [])
            except Exception as e:
                brief["sources"]["academic"] = [{"error": str(e)}]

            # Wikipedia
            try:
                wiki = wikipedia_lookup_impl(query, full_article=True)
                brief["sources"]["wikipedia"] = wiki
            except Exception as e:
                brief["sources"]["wikipedia"] = {"error": str(e)}

            return [TextContent(type="text", text=json.dumps(brief, indent=2))]

        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    except Exception as e:
        error_msg = str(e)
        # Redact API keys from error messages
        if GOOGLE_API_KEY:
            error_msg = error_msg.replace(GOOGLE_API_KEY, "[REDACTED]")
        return [TextContent(type="text", text=json.dumps({"error": error_msg}))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
