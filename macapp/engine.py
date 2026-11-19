"""Entry point of the engine bundled inside Screen Organizer.app.

    engine ui --port N --no-browser   the editor's local server (the app starts this)
    engine mcp                        the MCP server, for AI assistants
    engine <cli command>              everything `homescreen` does
"""

import multiprocessing
import sys


def main() -> None:
    multiprocessing.freeze_support()
    if sys.argv[1:2] == ["mcp"]:
        del sys.argv[1]
        from homescreen.server import main as mcp_main
        mcp_main()
    else:
        from homescreen.cli import main as cli_main
        cli_main()


if __name__ == "__main__":
    main()
