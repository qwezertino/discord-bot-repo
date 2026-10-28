"""Mount the entire application under one explicit, validated URL prefix."""
from starlette.responses import JSONResponse, RedirectResponse


class PathPrefixMiddleware:
    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http', 'websocket'}:
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        if path == self.prefix and scope['type'] == 'http':
            await RedirectResponse(self.prefix + '/', status_code=308)(scope, receive, send)
            return
        if not path.startswith(self.prefix + '/'):
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
            else:
                await JSONResponse({'detail': 'Not Found'}, status_code=404)(scope, receive, send)
            return
        child = dict(scope)
        child['path'] = path[len(self.prefix):]
        child['root_path'] = scope.get('root_path', '') + self.prefix
        raw = scope.get('raw_path')
        if raw and raw.startswith(self.prefix.encode()):
            child['raw_path'] = raw[len(self.prefix):]
        await self.app(child, receive, send)
