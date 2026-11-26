"""pytest 全局配置：把模块级 app 的默认 SQLite 路径引到临时目录，避免污染仓库。"""
import os
import tempfile

os.environ.setdefault(
    "REPRINT_DB", os.path.join(tempfile.gettempdir(), "reprint_api_default_test.db")
)
