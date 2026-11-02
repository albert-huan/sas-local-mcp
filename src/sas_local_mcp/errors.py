"""Stable, client-facing error codes shared by the reader and MCP adapter."""


class DatasetError(Exception):
    def __init__(self, message: str, code: str = "DATASET_ERROR") -> None:
        super().__init__(message)
        self.code = code
