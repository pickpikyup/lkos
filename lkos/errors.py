"""项目内统一使用的异常类型。"""


class LkosError(Exception):
    """所有自定义异常的基类，CLI 捕获后给出友好提示。"""


class ConfigError(LkosError):
    """配置文件缺失字段或取值不合法。"""


class ParseError(LkosError):
    """文件无法解析（损坏、格式不符等）。"""


class EncryptedFileError(ParseError):
    """文件被加密或受密码保护，按规则跳过。"""


class ModelUnavailableError(LkosError):
    """模型或 Provider 不可用。绝不触发自动换模型。"""


class RemoteDisabledError(LkosError):
    """远端调用被隐私设置禁止（remote_enabled=false 或 offline_mode=true）。"""


class EmbeddingMismatchError(LkosError):
    """索引中记录的 Embedding 模型或维度与当前配置不一致，需要重建索引。"""
