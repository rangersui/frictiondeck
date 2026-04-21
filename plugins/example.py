# plugins/example.py — elastik Tier 1 plugin specimen (template).
# Not auto-loaded. Install via:
#   curl -X PUT http://localhost:3005/lib/example \
#     -H "Authorization: Bearer $ELASTIK_TOKEN" \
#     --data-binary @plugins/example.py
#   curl -X PUT http://localhost:3005/lib/example/state \
#     -H "Authorization: Bearer $ELASTIK_APPROVE_TOKEN" \
#     --data-binary "active"

AUTH = "none"
ROUTES = ["/example"]

async def handle(method, body, params):
    return {"hello": "from example plugin",
            "echo": body if isinstance(body, str) else body.decode("utf-8", "replace")}
