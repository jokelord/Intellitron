#!/usr/bin/env python
"""
Test Wikipedia search using the WikipediaSearchTool from smolagents.
Tests the agent's ability to use Wikipedia search as a tool.
"""

import sys
import os
import signal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smolagents import WikipediaSearchTool


def timeout_handler(signum, frame):
    raise TimeoutError("Operation timed out")


def test_wikipedia_search():
    print("=== Testing WikipediaSearchTool ===")

    # Set timeout for the entire test
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(30)  # 30 second timeout

    try:
        # Initialize the Wikipedia search tool
        wiki_tool = WikipediaSearchTool()
        print("✓ WikipediaSearchTool initialized successfully")

        # Test cases with simple queries
        test_queries = ["Artificial Intelligence", "Machine Learning", "NVIDIA"]

        success_count = 0
        total_count = len(test_queries)

        for query in test_queries:
            print(f"\n--- Testing query: '{query}' ---")
            try:
                # Set timeout for individual search
                signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(15)  # 15 second timeout per query

                # The tool expects a dictionary with 'query' parameter
                result = wiki_tool.forward(query=query)
                signal.alarm(0)  # Cancel alarm

                result_str = str(result)
                # Check if we got a meaningful result vs an error message
                if result_str and len(result_str.strip()) > 0:
                    if "Error fetching Wikipedia summary" in result_str:
                        print(f"✗ Wikipedia API Error: {result_str}")
                    else:
                        print(f"✓ Success: Got result ({len(result_str)} chars)")
                        print(
                            f"  Preview: {result_str[:150]}..."
                            if len(result_str) > 150
                            else f"  Preview: {result_str}"
                        )
                        success_count += 1
                else:
                    print("⚠ Warning: Empty result received")

            except TimeoutError:
                print(
                    f"✗ Timeout: Query '{query}' took too long (possible network/refusal issue)"
                )
            except Exception as e:
                print(f"✗ Error: {type(e).__name__}: {e}")
            finally:
                signal.alarm(0)  # Ensure alarm is cancelled

        print(f"\n=== Test Summary ===")
        print(f"Successful queries: {success_count}/{total_count}")
        if success_count == 0:
            print("⚠ All Wikipedia searches failed. This could be due to:")
            print("  - Network connectivity issues")
            print("  - Wikipedia API blocking/refetching restrictions (反爬 mechanism)")
            print("  - Proxy configuration interfering with requests")
            print("  - Changes to the Wikipedia API or smolagents library")
        elif success_count < total_count:
            print("⚠ Some Wikipedia searches failed - intermittent issues possible")
        else:
            print("✓ All Wikipedia searches successful")

        print("=== Test completed ===")

    except Exception as e:
        print(f"✗ Failed to initialize WikipediaSearchTool: {e}")
    finally:
        signal.alarm(0)  # Cancel any remaining alarm


if __name__ == "__main__":
    test_wikipedia_search()
