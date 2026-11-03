from app.factory import create_app


def test_create_app_stores_url_and_registers_routes():
    url = "postgresql+psycopg://u:p@localhost/db"
    app = create_app(url)
    assert app.state.database_url == url
    paths = set(app.openapi()["paths"])
    assert {"/api/health", "/api/todos", "/api/todos/{todo_id}"} <= paths
