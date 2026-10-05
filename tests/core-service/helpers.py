async def create_portfolio(client, headers, name="Test Portfolio", **extra):
    response = await client.post(
        "/api/v1/portfolios", json={
            "name": name,
            **extra
        }, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]
