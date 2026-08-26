class CcmError(Exception):
    """ccm 统一异常基类;message 面向用户,单行中文。"""


class ProfileNotFound(CcmError):
    pass


class CredentialsMissing(CcmError):
    pass


class TokenExpired(CcmError):
    pass


class ApiError(CcmError):
    pass


class MigrationAborted(CcmError):
    pass


class LockBusy(CcmError):
    pass


class LayoutBroken(CcmError):
    pass
