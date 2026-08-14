import httpx

_clients: dict[float, httpx.AsyncClient] = {}


def get_http_client(timeout: float = 5.0) -> httpx.AsyncClient:
    if timeout not in _clients:
        _clients[timeout] = httpx.AsyncClient(
            timeout=timeout, limits=httpx.Limits(
                max_connections=100, max_keepalive_connections=20))
    return _clients[timeout]


async def close_http_clients() -> None:
    for client in _clients.values():
        await client.aclose()
    _clients.clear()
