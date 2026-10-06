"""Reviewer interface: FastAPI routes, Jinja2 pages, and the view functions behind them.

Layers, as in DOC-05:
    routes/      receive the request, call one service or view function, return a page
    views.py     assemble what a page shows; no FastAPI, so it is testable on its own
    rendering.py the Jinja2 environment and the formatting filters
Business rules live in app/control and app/services, never here (ADR-02).
"""
