"""Vendored tools from github.com/Jakeschincariol/trader-skill (MIT, see LICENSE).

Copied from commit 5189e79 (2026-10-14). Only these were taken:
rebalance.py, size.py, check.py, news.py, edgar13f.py (+ filers.json,
share_classes.json) and ptr.py. Lock In changes are marked "Lock In:":
  - news.yahoo_symbol keeps exchange suffixes like .SW / .AS / .L
  - check.TICKER accepts exchange suffixes and crypto tickers
  - ptr.pdf_to_text falls back to pdfplumber when pdftotext is missing
"""
