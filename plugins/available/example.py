"""Example plugin — minimal template.

Install: lucy install example
"""

DESCRIPTION = "Example plugin that returns a greeting"
ROUTES = {}
PROXY_WHITELIST = {}
PERMISSIONS = []


async def handle_hello(request):
    return {"message": "Hello from example plugin"}


ROUTES["/proxy/example/hello"] = handle_hello
handle_hello._methods = ["GET"]
