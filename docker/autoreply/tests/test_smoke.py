def test_module_imports_with_test_wiring(app):
    assert app.SERVICE.webhook_token == "test-webhook-token"
