"""Run the reviewer interface locally:  python -m app.web  (then open http://127.0.0.1:8000)."""

import uvicorn

from app.web.main import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
