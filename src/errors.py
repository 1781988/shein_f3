"""自定义异常层次与退出码映射。

所有异常信息都带文件路径、行号或列名，不写「出错了」这类无信息量的话。
"""
from __future__ import annotations

from typing import Optional

EXIT_OK: int = 0
EXIT_UNEXPECTED: int = 1
EXIT_INPUT: int = 2
EXIT_OUTPUT_CONFLICT: int = 3


class F3Error(Exception):
    """项目自定义异常基类。"""

    exit_code: int = EXIT_UNEXPECTED
    label: str = "未预期错误"

    def __init__(
        self,
        message: str,
        *,
        path: Optional[str] = None,
        location: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        self.message = message
        self.path = path
        self.location = location
        self.detail = detail
        parts = [f"[{self.label}]", message]
        if path:
            parts.append(f"路径={path}")
        if location:
            parts.append(f"位置={location}")
        if detail:
            parts.append(f"详情={detail}")
        super().__init__(" ".join(parts))


class DataFileError(F3Error):
    """原始数据文件缺失、损坏、被占用，或结果目录不可写。"""

    exit_code = EXIT_INPUT
    label = "数据文件错误"


class SchemaError(F3Error):
    """工作表缺失、为空、缺必需列、备注条数不对。"""

    exit_code = EXIT_INPUT
    label = "表结构错误"


class DataValidationError(F3Error):
    """数值或时间非法、整表解析失败率超阈值。"""

    exit_code = EXIT_INPUT
    label = "数据校验错误"


class ConfigError(F3Error):
    """命令行参数或配置文件非法。"""

    exit_code = EXIT_INPUT
    label = "配置错误"


class OutputConflictError(F3Error):
    """--strict-no-overwrite 时结果文件已存在。"""

    exit_code = EXIT_OUTPUT_CONFLICT
    label = "输出冲突"


class BuildError(F3Error):
    """报告、看板、曲线等生成期失败，非致命，状态写进 build_manifest.json。"""

    exit_code = EXIT_OK
    label = "生成期问题"


def exit_code_for(exc: BaseException) -> int:
    """返回异常对应的进程退出码；未预期异常一律 1。"""
    if isinstance(exc, F3Error):
        return exc.exit_code
    return EXIT_UNEXPECTED


__all__ = [
    "EXIT_OK", "EXIT_UNEXPECTED", "EXIT_INPUT", "EXIT_OUTPUT_CONFLICT",
    "F3Error", "DataFileError", "SchemaError", "DataValidationError",
    "ConfigError", "OutputConflictError", "BuildError", "exit_code_for",
]
