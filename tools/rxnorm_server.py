# FastMCP-based MCP server for RxNorm API
# Tools:
#   1) search_drugs(query, limit=5)
#   2) get_drug_properties(rxcui)

import json
from typing import List, Dict, Any, Optional

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("rxnorm")

def _clip_limit(n: Optional[int], lo: int = 1, hi: int = 50, default: int = 5) -> int:
    try:
        n = int(n) if n is not None else default
        return max(lo, min(n, hi))
    except Exception:
        return default

@mcp.tool()
def search_drugs(query: str, limit: int = 5) -> str:
    """
    Search RxNorm for drug concepts by brand or generic name.
    Args:
      query: e.g., "Tylenol" or "acetaminophen"
      limit: max number of results to return (1–50, default 5)
    Returns:
      Pretty-printed JSON string: {"query": "...", "results": [ {...}, ... ]}
    """
    q = (query or "").strip()
    if not q:
        return json.dumps({"error": "query is required"}, indent=2)

    lim = _clip_limit(limit)

    # RxNorm "drugs" endpoint groups results in conceptGroup[].conceptProperties[]
    url = "https://rxnav.nlm.nih.gov/REST/drugs.json"
    try:
        r = requests.get(url, params={"name": q}, timeout=20)
        r.raise_for_status()
        data = r.json() or {}
    except requests.RequestException as e:
        return json.dumps({"error": f"HTTP error contacting RxNorm: {e}"}, indent=2)

    results: List[Dict[str, Any]] = []
    drug_group = (data.get("drugGroup") or {})
    for grp in (drug_group.get("conceptGroup") or []):
        for c in (grp.get("conceptProperties") or []):
            results.append({
                "rxcui": c.get("rxcui"),
                "name": c.get("name"),
                "synonym": c.get("synonym"),
                "tty": c.get("tty"),
            })

    return json.dumps({"query": q, "results": results[:lim]}, indent=2)

@mcp.tool()
def get_drug_properties(rxcui: str) -> str:
    """
    Fetch RxNorm properties for a given RXCUI.
    Args:
      rxcui: RxNorm Concept Unique Identifier (string or int)
    Returns:
      Pretty-printed JSON string with RxNorm properties, or an error message.
    """
    rx = str(rxcui or "").strip()
    if not rx:
        return json.dumps({"error": "rxcui is required"}, indent=2)

    url = f"https://rxnav.nlm.nih.gov/REST/rxcui/{rx}/properties.json"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json() or {}
    except requests.RequestException as e:
        return json.dumps({"error": f"HTTP error contacting RxNorm: {e}"}, indent=2)

    props = (data.get("properties") or {})
    return json.dumps({"rxcui": rx, "properties": props}, indent=2)

if __name__ == "__main__":
    # Same transport your research server uses.
    mcp.run(transport="stdio")
