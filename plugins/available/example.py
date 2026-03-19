"""Example plugin — minimal template.

Install: lucy install example
Handler signature: async def handler(method, body, params) -> dict
"""

DESCRIPTION = "Example plugin that returns a greeting"
ROUTES = {}


async def handle_hello(method, body, params):
    return {"message": "Hello from example plugin"}


ROUTES["/proxy/example/hello"] = handle_hello
