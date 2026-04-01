#!/usr/bin/env python
"""
Detailed test for Wikipedia search to diagnose the 反爬 (anti-crawling) issue.
Tests both the smolagents WikipediaSearchTool and a manual workaround.
"""

import sys
import os
import signal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smolagents import WikipediaSearchTool
import urllib.request
import urllib.error
import json


def timeout_handler(signum, frame):
    raise TimeoutError("Operation timed out")


def test_smolagents_wikipedia_tool():
    """Test the smolagents WikipediaSearchTool as-is"""
    print("=== Testing smolagents WikipediaSearchTool ===")

    try:
        wiki_tool = WikipediaSearchTool()
        print("✓ WikipediaSearchTool initialized")

        # Test with a simple query
        result = wiki_tool.forward(query="Artificial Intelligence")
        result_str = str(result)

        if "Error fetching Wikipedia summary" in result_str:
            print("✗ Tool failed with Wikipedia API error (likely 403 Forbidden)")
            print(f"  Error message: {result_str}")
            return False
        elif result_str and len(result_str.strip()) > 10:
            print("✓ Tool succeeded")
            print(f"  Result preview: {result_str[:100]}...")
            return True
        else:
            print("⚠ Tool returned empty or invalid result")
            return False

    except Exception as e:
        print(f"✗ Tool initialization or execution failed: {e}")
        return False


def test_manual_wikipedia_api():
    """Test manual Wikipedia API calls with proper headers"""
    print("\n=== Testing Manual Wikipedia API Calls ===")

    def fetch_wikipedia_summary(query):
        """Fetch Wikipedia summary using proper headers"""
        try:
            # Search for the page
            search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&format=json"

            headers = {
                "User-Agent": "LLM-Deployment-Agent/1.0 (https://github.com/your-repo; contact@your-domain.com)"
            }

            req = urllib.request.Request(search_url, headers=headers)
            response = urllib.request.urlopen(req, timeout=10)
            search_data = json.loads(response.read().decode())

            if not search_data["query"]["search"]:
                return f"No Wikipedia page found for '{query}'"

            # Get the page ID from first search result
            page_id = search_data["query"]["search"][0]["pageid"]

            # Get the summary
            summary_url = f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro&explaintext&pageids={page_id}&format=json"
            req = urllib.request.Request(summary_url, headers=headers)
            response = urllib.request.urlopen(req, timeout=10)
            summary_data = json.loads(response.read().decode())

            page_data = summary_data["query"]["pages"].get(str(page_id))
            if not page_data:
                return f"Could not retrieve content for page ID {page_id}"

            extract = page_data.get("extract", "No extract available")
            if not extract.strip():
                return f"Empty extract for '{query}'"

            return extract.strip()

        except Exception as e:
            return f"Error fetching Wikipedia data: {e}"

    import urllib.parse

    test_queries = ["Artificial Intelligence", "Machine Learning", "NVIDIA"]
    success_count = 0

    for query in test_queries:
        print(f"\n--- Testing query: '{query}' ---")
        try:
            result = fetch_wikipedia_summary(query)
            if result.startswith("Error"):
                print(f"✗ Manual API call failed: {result}")
            elif result.startswith("No Wikipedia page") or result.startswith(
                "Could not retrieve"
            ):
                print(f"⚠ Manual API call issue: {result}")
            else:
                print(f"✓ Manual API call succeeded")
                print(
                    f"  Extract preview: {result[:150]}..."
                    if len(result) > 150
                    else f"  Extract: {result}"
                )
                success_count += 1
        except Exception as e:
            print(f"✗ Manual API call exception: {e}")

    print(f"\nManual API success rate: {success_count}/{len(test_queries)}")
    return success_count > 0


def test_with_proxy():
    """Test Wikipedia access through the configured proxy"""
    print("\n=== Testing Wikipedia Access Through Proxy ===")

    # Set proxy from config
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:10808"
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:10808"

    def fetch_via_proxy(query):
        try:
            search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&format=json"

            headers = {
                "User-Agent": "LLM-Deployment-Agent/1.0 (https://github.com/your-repo; contact@your-domain.com)"
            }

            req = urllib.request.Request(search_url, headers=headers)
            response = urllib.request.urlopen(req, timeout=15)
            search_data = json.loads(response.read().decode())

            if not search_data["query"]["search"]:
                return f"No Wikipedia page found for '{query}'"

            page_id = search_data["query"]["search"][0]["pageid"]
            summary_url = f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro&explaintext&pageids={page_id}&format=json"
            req = urllib.request.Request(summary_url, headers=headers)
            response = urllib.request.urlopen(req, timeout=15)
            summary_data = json.loads(response.read().decode())

            page_data = summary_data["query"]["pages"].get(str(page_id))
            if not page_data:
                return f"Could not retrieve content for page ID {page_id}"

            extract = page_data.get("extract", "No extract available")
            return extract.strip() if extract.strip() else "Empty extract"

        except Exception as e:
            return f"Error: {e}"

    import urllib.parse

    result = fetch_via_proxy("Artificial Intelligence")
    if result.startswith("Error"):
        print(f"✗ Proxied access failed: {result}")
        return False
    else:
        print(f"✓ Proxied access succeeded")
        print(
            f"  Extract preview: {result[:150]}..."
            if len(result) > 150
            else f"  Extract: {result}"
        )
        return True


def main():
    print("Wikipedia Search Diagnostic Test")
    print("=" * 50)

    # Set global timeout
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(60)  # 60 second overall timeout

    try:
        # Test 1: smolagents tool (likely to fail)
        smolagents_work = test_smolagents_wikipedia_tool()

        # Test 2: Manual API call (should work)
        manual_work = test_manual_wikipedia_api()

        # Test 3: Proxied manual API call
        proxied_work = test_with_proxy()

        print("\n" + "=" * 50)
        print("SUMMARY:")
        print(
            f"  smolagents WikipediaSearchTool: {'✓ WORK' if smolagents_work else '✗ FAIL'}"
        )
        print(
            f"  Manual Wikipedia API:           {'✓ WORK' if manual_work else '✗ FAIL'}"
        )
        print(
            f"  Manual Wikipedia API w/ Proxy:  {'✓ WORK' if proxied_work else '✗ FAIL'}"
        )

        if not smolagents_work and (manual_work or proxied_work):
            print("\nDIAGNOSIS:")
            print("  The issue is with the smolagents WikipediaSearchTool -")
            print("  it's likely not setting proper headers, triggering Wikipedia's")
            print("  anti-crawling (反爬) mechanisms (403 Forbidden).")
            print("\nPOSSIBLE SOLUTIONS:")
            print(
                "  1. Modify the smolagents WikipediaSearchTool to set a proper User-Agent"
            )
            print("  2. Implement a custom Wikipedia search tool with correct headers")
            print(
                "  3. Use the web_search tool as an alternative for general information"
            )
            print(
                "  4. Configure proxies correctly if required in your network environment"
            )
        elif not smolagents_work and not manual_work and not proxied_work:
            print("\nDIAGNOSIS:")
            print("  All Wikipedia access methods failed - this suggests:")
            print("  - Network connectivity issues to Wikipedia")
            print("  - Wikipedia API might be blocking your IP range")
            print(
                "  - Proxy configuration is incorrect or the proxy server is not reachable"
            )
        elif smolagents_work:
            print("\nRESULT: Wikipedia search is working correctly!")

    except TimeoutError:
        print("\n✗ Test timed out - possible network issues or hanging connections")
    except Exception as e:
        print(f"\n✗ Unexpected error during testing: {e}")
    finally:
        signal.alarm(0)  # Cancel alarm

    print("=" * 50)


if __name__ == "__main__":
    main()
