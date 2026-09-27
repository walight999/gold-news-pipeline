"""Offline research: historical release dataset + learned reaction statistics.

Nothing here runs in the live news pipeline. The output that the pipeline
consumes is a single committed JSON (config/release_stats.json) produced by
research.learn. Raw downloads are cached under research/data/ (gitignored).
"""
