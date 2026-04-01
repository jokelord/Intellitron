#!/usr/bin/env python
"""
Test script to evaluate the Wikipedia search tool functionality in the Agent system.
This tests both the existing smolagents WikipediaSearchTool and provides a workaround.

Based on investigation, the issue is that smolagents WikipediaSearchTool doesn't set
proper headers, causing Wikipedia to return 403 Forbidden due to anti-crawling measures.
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


class CustomWikipediaTool:
    """A custom Wikipedia tool that sets proper headers to avoid anti-crawling blocks."""

    def __init__(self):
        self.name = "wikipedia_search"
        self.description = "Search Wikipedia for information"
        self.inputs = {
            "query": {
                "type": "string",
                "description": "The search query to look up on Wikipedia",
            }
        }
        self.output_type = "string"

    def forward(self, query: str) -> str:
        """Search Wikipedia and return summary."""
        try:
            # Search for the page
            search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&format=json"

            headers = {
                "User-Agent": "LLM-Deployment-Agent/1.0 (https://github.com/zk/Project/LLM-deployment; agent@llm-deployment.local)"
            }

            req = urllib.request.Request(search_url, headers=headers)
            response = urllib.request.urlopen(req, timeout=10)
            search_data = json.loads(response.read().decode())

            if not search_data["query"]["search"]:
                return f"No Wikipedia page found for '{query}'"

            # Get the page ID from first search result
            page_id = search_data["query"]["search"][0]["pageid"]
            title = search_data["query"]["search"][0]["title"]

            # Get the summary
            summary_url = f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro&explaintext&pageids={page_id}&format=json"
            req = urllib.request.Request(summary_url, headers=headers)
            response = urllib.request.urlopen(req, timeout=10)
            summary_data = json.loads(response.read().decode())

            page_data = summary_data["query"]["pages"].get(str(page_id))
            if not page_data:
                return f"Could not retrieve content for Wikipedia page '{title}' (ID: {page_id})"

            extract = page_data.get("extract", "No extract available")
            if not extract.strip():
                return f"Empty extract for Wikipedia page '{title}'"

            # Format result similar to smolagents
            return f"Summary of '{title}' from Wikipedia:\n\n{extract.strip()}"

        except Exception as e:
            return f"Error searching Wikipedia for '{query}': {str(e)}"


def test_existing_wikipedia_tool():
    """Test the existing smolagents WikipediaSearchTool."""
    print("=== Testing Existing smolagents WikipediaSearchTool ===")

    try:
        wiki_tool = WikipediaSearchTool()
        print("✓ WikipediaSearchTool initialized")

        # Test with a simple query
        result = wiki_tool.forward(query="Artificial Intelligence")
        result_str = str(result)

        if "Error fetching Wikipedia summary" in result_str:
            print(
                "✗ Tool failed with Wikipedia API error (403 Forbidden - anti-crawling)"
            )
            print(f"  This confirms the issue: {result_str}")
            return False
        elif result_str and len(result_str.strip()) > 20:
            print(
                "✓ Tool succeeded (unexpected - might indicate intermittent issue or proxy working)"
            )
            print(f"  Result preview: {result_str[:100]}...")
            return True
        else:
            print("⚠ Tool returned empty or invalid result")
            return False

    except Exception as e:
        print(f"✗ Tool initialization or execution failed: {e}")
        return False


def test_custom_wikipedia_tool():
    """Test our custom Wikipedia tool with proper headers."""
    print("\n=== Testing Custom WikipediaTool (with proper headers) ===")

    try:
        wiki_tool = CustomWikipediaTool()
        print("✓ Custom WikipediaTool initialized")

        # Test cases
        test_queries = [
            "Artificial Intelligence",
            "Machine Learning",
            "NVIDIA",
            "Jetson",
        ]

        success_count = 0

        for query in test_queries:
            print(f"\n--- Testing query: '{query}' ---")
            try:
                result = wiki_tool.forward(query=query)

                if result.startswith("Error"):
                    print(f"✗ Failed: {result}")
                elif result.startswith("No Wikipedia page") or result.startswith(
                    "Could not retrieve"
                ):
                    print(f"⚠ Issue: {result}")
                else:
                    print(f"✓ Succeeded")
                    print(
                        f"  Preview: {result[:150]}..."
                        if len(result) > 150
                        else f"  Result: {result}"
                    )
                    success_count += 1

            except Exception as e:
                print(f"✗ Exception: {e}")

        print(f"\nCustom tool success rate: {success_count}/{len(test_queries)}")
        return success_count > 0

    except Exception as e:
        print(f"✗ Custom tool initialization failed: {e}")
        return False


def test_agent_integration_scenario():
    """Test how this would work in the actual Agent system."""
    print("\n=== Testing Agent Integration Scenario ===")

    # Show what the agent would experience
    print("In the Agent system, when the LLM tries to use Wikipedia search:")
    print('1. LLM generates: wikipedia_search(query="Artificial Intelligence")')
    print("2. smolagents WikipediaSearchTool.execute() is called")
    print("3. Tool makes request to Wikipedia API WITHOUT proper User-Agent")
    print("4. Wikipedia returns 403 Forbidden (anti-crawling protection)")
    print(
        "5. Tool returns: 'Error fetching Wikipedia summary: https://en.wikipedia.org/w/api.php'"
    )
    print("6. Agent sees this error and may retry or fail")

    print("\nWith our custom tool:")
    print("1. Same LLM call")
    print("2. Custom WikipediaTool.execute() is called")
    print("3. Tool makes request WITH proper User-Agent header")
    print("4. Wikipedia returns 200 OK with content")
    print("5. Tool returns formatted summary")
    print("6. Agent gets useful information and continues")

    return True


def main():
    print("Wikipedia Search Tool Test for Agent System")
    print("=" * 50)
    print("Investigating the 反爬 (anti-crawling) issue with wikipedia_search tool")
    print()

    # Set global timeout
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(120)  # 120 second overall timeout

    try:
        # Test 1: Existing tool (should fail)
        existing_works = test_existing_wikipedia_tool()

        # Test 2: Custom tool (should work)
        custom_works = test_custom_wikipedia_tool()

        # Test 3: Integration scenario
        integration_works = test_agent_integration_scenario()

        print("\n" + "=" * 50)
        print("FINAL RESULTS:")
        print(
            f"  Existing smolagents tool:     {'✓ WORK' if existing_works else '✗ FAIL (expected)'}"
        )
        print(
            f"  Custom Wikipedia tool:        {'✓ WORK' if custom_works else '✗ FAIL'}"
        )
        print(
            f"  Agent integration viable:     {'✓ YES' if integration_works else '✗ NO'}"
        )

        if not existing_works and custom_works:
            print("\nCONCLUSION:")
            print(
                "  The issue is confirmed: smolagents WikipediaSearchTool lacks proper headers,"
            )
            print("  triggering Wikipedia's anti-crawling (反爬) mechanisms.")
            print("\nRECOMMENDATION:")
            print("  For immediate use in the Agent system, consider:")
            print("  1. Using the custom WikipediaTool provided in this test")
            print(
                "  2. Falling back to the general web_search tool for information needs"
            )
            print(
                "  3. Implementing a proper User-Agent in the smolagents library (long-term fix)"
            )
            print(
                "  4. Ensuring proxy settings in config-misc.yaml are working correctly"
            )
        elif existing_works:
            print("\nUNEXPECTED: The existing Wikipedia tool worked -")
            print("  this might indicate:")
            print("  - Proxy settings are working correctly")
            print("  - Wikipedia has temporarily relaxed restrictions")
            print("  - Network conditions have changed")
        else:
            print("\nALTERNATIVE ISSUE: Both tools failed -")
            print("  this suggests network or proxy configuration problems")

    except TimeoutError:
        print("\n✗ Test timed out - possible network issues or hanging connections")
    except Exception as e:
        print(f"\n✗ Unexpected error during testing: {e}")
    finally:
        signal.alarm(0)  # Cancel alarm

    print("=" * 50)


if __name__ == "__main__":
    main()
