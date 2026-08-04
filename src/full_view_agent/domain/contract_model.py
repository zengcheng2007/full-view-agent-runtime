"""所有领域契约共享的 Pydantic 基类。

独立成叶子模块，避免 ``domain.models`` 与 ``domain.analysis_*``
之间出现循环导入：任何模块都可以先依赖这里，再由 ``domain.models``
统一聚合。
"""

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
