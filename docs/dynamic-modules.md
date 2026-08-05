# Dynamic Modules

VeloxServer supports two policy module styles:

- Python plugin files listed in `plugin_paths`
- native shared libraries listed in `plugin_paths`

Python hooks:

- `on_request(request)`
- `on_auth_request(request)`
- `on_waf_request(request)`

Return `True` to allow or a dictionary to block:

```python
{"allowed": False, "status": 403, "message": "Blocked"}
```

Native modules can export:

- `velox_module_init()`
- `velox_module_on_request_json(const char *request_json)`
- `velox_module_free(void *ptr)`

The native request hook receives JSON with `method`, `target`, `version`, and `headers`. It returns JSON using the same `allowed/status/message` shape.

Native modules run in-process. Treat them like server code: audit them, pin their versions, and load only trusted binaries.
