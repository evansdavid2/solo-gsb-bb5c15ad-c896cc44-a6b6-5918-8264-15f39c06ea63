class APIError(Exception):
    """统一业务异常，返回 {"error": {"code", "message", ...}}。"""

    def __init__(self, status_code: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}
