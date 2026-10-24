import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "test" / "test_searxng_engine_diagnostics.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("searxng_engine_diagnostics", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_default_cli_arguments():
    module = _load_script_module()

    args = module.parse_args([])

    assert args.query == "RLHF tool framework HuggingFace TRL"
    assert args.max_results == 20
    assert args.fetch_limit == 3
    assert args.pages == 3


def test_diagnose_engine_reports_pages_and_fetches(monkeypatch):
    module = _load_script_module()
    calls = []

    def fake_search(engine, query, pageno, timeout, base_url, language, safe_search):
        calls.append((engine, pageno))
        if pageno == 1:
            return {
                "http_status": 200,
                "elapsed_seconds": 0.1,
                "results": [
                    {
                        "title": "Result A",
                        "url": "https://example.com/a",
                        "engine": engine,
                        "category": "general",
                        "score": 1.0,
                        "content": "Alpha",
                    }
                ],
                "unresponsive_engines": [],
            }
        return {
            "http_status": 200,
            "elapsed_seconds": 0.2,
            "results": [],
            "unresponsive_engines": [[engine, "CAPTCHA"]],
        }

    def fake_fetch(url, timeout):
        return {
            "url": url,
            "status": "success",
            "elapsed_seconds": 0.3,
            "content_length": 42,
            "error": None,
            "preview": "Fetched text",
        }

    monkeypatch.setattr(module, "search_engine_page", fake_search)
    monkeypatch.setattr(module, "fetch_url", fake_fetch)

    report = module.diagnose_engine(
        engine="google",
        query="demo",
        max_results=20,
        pages=2,
        fetch_limit=1,
        timeout=5,
        base_url="http://searxng.test",
        language="all",
        safe_search=0,
    )

    assert calls == [("google", 1), ("google", 2)]
    assert report["engine"] == "google"
    assert len(report["pages"]) == 2
    assert report["pages"][1]["unresponsive_engines"] == [["google", "CAPTCHA"]]
    assert report["fetches"][0]["status"] == "success"
    assert report["summary"]["search_results"] == 1
    assert report["summary"]["fetch_success"] == 1
