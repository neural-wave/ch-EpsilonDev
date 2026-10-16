"""Run the API: python -m nl2sparql [--host 0.0.0.0] [--port 8080]"""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the NL-to-SPARQL API.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run("nl2sparql.api:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
