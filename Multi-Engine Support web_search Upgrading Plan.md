# Multi-Engine Support Web Search Upgrading Plan

## 1. Research Summary

### 1.1 Test Results via Proxy (http://127.0.0.1:10808)

| Search Engine | Endpoint | Protocol | Method | Status | Notes |
|---------------|----------|----------|--------|--------|-------|
| **DuckDuckGo** | `html.duckduckgo.com/html/` | curl | POST | ✅ Works | Current implementation |
| **Bing** | `www.bing.com/search` | curl | GET | ✅ Works | Full results returned |
| **Sogou** (搜狗) | `www.sogou.com/web` | curl | GET | ✅ Works | Good for Chinese queries |
| **360搜索** (好搜) | `www.so.com/s` | curl | GET | ✅ Works | Good for Chinese queries |
| **Baidu** (百度) | `www.baidu.com/s` | - | GET | ❌ Blocked | Returns CAPTCHA |
| **Weibo** (微博) | `m.weibo.cn/api/...` | - | GET | ⚠️ Hard | Needs Cookie/API auth |

### 1.2 Key Findings

1. **curl + POST/GET approach works** for most search engines through proxy
2. **Bing/360** are fully accessible - good fallback options
3. **Sogou** works well - good for Chinese-language searches
4. **Baidu** blocks automated requests with CAPTCHA
5. **DuckDuckGo requires POST** - GET returns fewer/empty results
6. **Weibo** requires authentication (Cookie or API key)

### 1.3 Technical Implementation Details

#### DuckDuckGo (Current)
```python
# Endpoint: https://html.duckduckgo.com/html/
# Protocol: curl
# Method: POST
# Key headers:
#   - User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36
#   - Content-Type: application/x-www-form-urlencoded
# Result class: result__a, result__snippet
```

#### Bing
```python
# Endpoint: https://www.bing.com/search?q={query}
# Protocol: curl
# Method: GET
# Result structure: <li class="b_algo">, <h2 class="">
```

#### Sogou (搜狗)
```python
# Endpoint: https://www.sogou.com/web?query={query}
# Protocol: curl
# Method: GET
# Result class: r-sech, result_list
```

#### 360搜索 (好搜)
```python
# Endpoint: https://www.so.com/s?q={query}
# Protocol: curl
# Method: GET
# Result class: result, res-list
```

---

## 2. Implementation Plan: Option 1 (Multi-Engine Support)

### 2.1 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    web_search(query)                             │
│                   (unified interface)                            │
└─────────────────────────┬───────────────────────────────────────┘
                          │
          ┌───────────────┼───────────────┬───────────────┐
          ▼               ▼               ▼               ▼
    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
    │ DuckDuckGo│    │   Bing   │    │  Sogou   │    │  360搜索  │
    │ curl/POST│    │ curl/GET │    │ curl/GET │    │ curl/GET │
    └─────┬─────┘    └─────┬─────┘    └─────┬─────┘    └─────┬─────┘
          │                 │                 │                 │
          └─────────────────┼─────────────────┼─────────────────┘
                            ▼
                   ┌─────────────────┐
                   │ Dynamic Loader  │
                   │ (from config)   │
                   └────────┬────────┘
                            ▼
                   ┌─────────────────┐
                   │ Execute Search  │
                   │ (curl/requests)│
                   └────────┬────────┘
                            ▼
                   ┌─────────────────┐
                   │ Parse HTML      │
                   │ to Results      │
                   └─────────────────┘
```

### 2.2 Configuration (YAML-driven, No Hardcoding)

All parameters are configured via `config/config-misc.yaml`:

```yaml
web_tools:
  # === Primary Engine Configuration ===
  # Engine provider: duckduckgo / bing / sogou / 360
  search_engine: duckduckgo
  
  # === Technical Protocol (how to connect) ===
  # Options: curl / requests
  # - curl: Use subprocess to call curl command (better for anti-bot)
  # - requests: Use Python requests library (faster, no process spawn)
  protocol: curl
  
  # === HTTP Method ===
  # Options: GET / POST
  # - GET: Append query to URL (works for most engines)
  # - POST: Send query in body (required for DuckDuckGo)
  method: POST
  
  # === Fallback Engine Configuration ===
  # If primary engine fails, try this engine
  fallback_engine: bing
  fallback_protocol: curl
  fallback_method: GET
  
  # === Proxy Configuration ===
  # 代理服务器地址 (用于中国大陆访问部分搜索引擎)
  # 格式: http://127.0.0.1:10808
  proxy: "http://127.0.0.1:10808"
  
  # === Wikipedia ===
  enable_wikipedia: true
```

### 2.3 Backend Config Class

```python
@dataclass
class WebToolsConfig:
    # Engine providers
    search_engine: str = "duckduckgo"
    fallback_engine: str = "bing"
    
    # Technical protocol
    protocol: str = "curl"  # curl | requests
    method: str = "POST"    # GET | POST
    
    # Fallback protocol/method
    fallback_protocol: str = "curl"
    fallback_method: str = "GET"
    
    # Other settings
    enable_wikipedia: bool = True
    proxy: Optional[str] = None
```

### 2.4 Engine-Specific Default Mappings (Code)

Engine defaults stored in code but can be overridden by config:

```python
ENGINE_DEFAULTS = {
    "duckduckgo": {
        "endpoint": "https://html.duckduckgo.com/html/",
        "protocol": "curl",
        "method": "POST",
    },
    "bing": {
        "endpoint": "https://www.bing.com/search",
        "protocol": "curl", 
        "method": "GET",
    },
    "sogou": {
        "endpoint": "https://www.sogou.com/web",
        "protocol": "curl",
        "method": "GET",
    },
    "360": {
        "endpoint": "https://www.so.com/s",
        "protocol": "curl",
        "method": "GET",
    },
}
```

### 2.5 File Structure

```
backend/tools/
├── web_tools.py              # Multi-engine search implementation
│   ├── ENGINE_DEFAULTS       # Engine default configs
│   ├── get_engine_config()   # Load from config + defaults
│   ├── execute_search()      # Dynamic curl/requests executor
│   ├── duckduckgo_search()   # Engine-specific search
│   ├── bing_search()
│   ├── sogou_search()
│   ├── search360()           # 360搜索
│   ├── parse_duckduckgo_html()
│   ├── parse_bing_html()
│   ├── parse_sogou_html()
│   ├── parse_360_html()
│   ├── web_search()          # Unified interface
│   └── web_fetch()
```

### 2.6 Implementation Steps

| Step | Description |
|------|-------------|
| **1** | Add `protocol`, `method`, `fallback_protocol`, `fallback_method` to `WebToolsConfig` class |
| **2** | Update `config-misc.yaml` with new configuration fields |
| **3** | Refactor `web_tools.py`: |
| 3.1 | Add `ENGINE_DEFAULTS` dictionary with endpoint/protocol/method |
| 3.2 | Create `get_engine_config(engine_name)` function |
| 3.3 | Create `execute_search(endpoint, query, protocol, method, proxy)` |
| 3.4 | Implement `search360()` function |
| 3.5 | Update `web_search()` to read config dynamically |
| **4** | Add HTML parsing for 360搜索 |
| **5** | Test all 4 engines with different protocol/method combinations |
| **6** | Test fallback mechanism |

### 2.7 Dynamic Executor Logic

```python
def execute_search(endpoint: str, query: str, protocol: str, method: str, 
                   proxy: Optional[str] = None) -> str:
    """Execute search using configured protocol and method."""
    
    if protocol == "curl":
        return _curl_search(endpoint, query, method, proxy)
    elif protocol == "requests":
        return _requests_search(endpoint, query, method, proxy)
    else:
        raise ValueError(f"Unknown protocol: {protocol}")

def _curl_search(endpoint: str, query: str, method: str, 
                 proxy: Optional[str]) -> str:
    """Execute search via curl subprocess."""
    curl_args = ["curl", "-s", "--max-time", "30"]
    
    if proxy:
        curl_args.extend(["-x", proxy])
    
    curl_args.extend([
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    ])
    
    if method == "POST":
        curl_args.extend(["-X", "POST"])
        curl_args.extend(["-H", "Content-Type: application/x-www-form-urlencoded"])
        curl_args.extend(["-d", f"q={urllib.parse.quote(query)}"])
        curl_args.append(endpoint)
    else:  # GET
        curl_args.append(f"{endpoint}?q={urllib.parse.quote(query)}")
    
    result = subprocess.run(curl_args, capture_output=True, text=True, timeout=35)
    return result.stdout

def _requests_search(endpoint: str, query: str, method: str,
                    proxy: Optional[str]) -> str:
    """Execute search via Python requests."""
    import requests
    
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    if method == "POST":
        response = requests.post(
            endpoint, 
            data={"q": query},
            headers=headers,
            proxies=proxies,
            timeout=30
        )
    else:  # GET
        response = requests.get(
            endpoint,
            params={"q": query},
            headers=headers,
            proxies=proxies,
            timeout=30
        )
    
    return response.text
```

---

## 3. Test Matrix

| Engine | Protocol | Method | Expected Status |
|--------|----------|--------|-----------------|
| duckduckgo | curl | POST | ✅ Works (current) |
| duckduckgo | curl | GET | ⚠️ May work but fewer results |
| duckduckgo | requests | POST | ❌ Blocked by anti-bot |
| duckduckgo | requests | GET | ❌ Blocked by anti-bot |
| bing | curl | GET | ✅ Works |
| bing | requests | GET | ✅ Should work |
| sogou | curl | GET | ✅ Works |
| 360 | curl | GET | ✅ Works |
| fallback | - | - | ✅ Works |

---

## 4. References

- Current implementation: `backend/tools/web_tools.py`
- Configuration: `config/config-misc.yaml`
- smolagents tools: `smolagents/default_tools.py`
