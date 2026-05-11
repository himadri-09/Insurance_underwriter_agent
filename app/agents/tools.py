"""
Tools available to Claude during reasoning.
These get registered as Anthropic tool-use functions so the evaluator
can dynamically retrieve more evidence mid-reasoning.
"""

REASONING_TOOLS = [
    {
        "name": "search_appetite_guide",
        "description": "Search the carrier's appetite guide and underwriting manuals for specific information. Use when you need to verify a coverage threshold, check a class code eligibility, find territory restrictions, or look up any underwriting rule.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query. Be specific — include LOB, class code, coverage type, or territory as relevant."
                },
                "search_type": {
                    "type": "string",
                    "enum": ["semantic", "keyword", "hybrid"],
                    "description": "Use 'keyword' for exact codes/endorsements (e.g. IMT.22, SIC 7372), 'semantic' for conceptual questions, 'hybrid' for most queries.",
                    "default": "hybrid"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_by_code",
        "description": "Look up a specific endorsement code, class code, SIC/NAICS code, or IMT number in the knowledge base. Use for exact lookups like 'IMT.22A', 'SIC 7372', 'NAICS 541512'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The exact code to look up (e.g. 'IMT.22A', 'SIC 7372', 'NAICS 541512')"
                }
            },
            "required": ["code"]
        }
    },
    {
        "name": "search_loss_guidelines",
        "description": "Search for loss ratio thresholds, claims frequency guidelines, or loss history evaluation criteria for a specific line of business.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lob": {
                    "type": "string",
                    "description": "Line of business (cyber, property, gl, auto, wc, umbrella)"
                },
                "query": {
                    "type": "string",
                    "description": "What loss-related guideline to find (e.g. 'maximum acceptable loss ratio', 'frequency threshold for referral')"
                }
            },
            "required": ["lob", "query"]
        }
    },
    {
        "name": "search_referral_rules",
        "description": "Search for referral triggers, authority limits, and escalation rules. Use when determining if a submission requires senior underwriter review.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What referral rule to check (e.g. 'limit over 5M', 'new venture', 'adverse loss history')"
                },
                "lob": {
                    "type": "string",
                    "description": "Line of business if relevant",
                    "default": ""
                }
            },
            "required": ["query"]
        }
    },
]
