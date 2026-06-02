"""trader-mcp: an AI-native crypto trading platform delivered as an MCP server.

This package exposes a Model Context Protocol (MCP) server that lets an AI client
drive the full strategy lifecycle (research -> author -> backtest -> paper/testnet
-> gated live) across Coinbase, Kraken, Gemini, and Crypto.com through a single CCXT adapter.

See ``PRD.md`` for the authoritative architecture and phased delivery plan.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.0.0"
