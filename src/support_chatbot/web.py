"""FastAPI entry point for the support chatbot."""

import uvicorn

from support_chatbot.api.app import create_app
from support_chatbot.config import settings


app = create_app()


def main():
    """Run the ASGI application with Uvicorn."""
    print(
        f"Ami is running at http://{settings.host}:{settings.port} (ctrl-c to stop)",
        flush=True,
    )
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
