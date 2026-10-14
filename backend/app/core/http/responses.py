"""项目普通 JSON 成功响应构造；不修改业务字段或 HTTP 状态码。"""


def success(data):
    """所有成功统一 code=0；分页游标保留在 data，跟踪地址通过 Location 头提供。"""
    return {"code": 0, "msg": "success", "data": data}
