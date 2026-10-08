"""企业微信自动化对外接口。"""

from ._core import work_wx as _work_wx


def get_session_name():
    """获取当前窗口名称。"""
    return _work_wx.get_session_name()


def get_session_list(session_tag, wheel_times=3):
    """获取指定分组中的会话名称。"""
    return _work_wx.get_session_list(session_tag, int(wheel_times))


def get_chat_msg(sub_num=0, start_time=None, end_time=None):
    """获取当前会话的聊天记录。"""
    return _work_wx.get_chat_msg(sub_num, start_time, end_time)


def find_contact(name, find_match="相等", find_type="群聊", check_unique=False):
    """查找并打开联系人或群聊。"""
    if name is None or not str(name).strip():
        return None
    if find_type not in ("联系人", "群聊"):
        raise ValueError("查找类型必须是 联系人 或 群聊")
    current = _work_wx.get_session_name()
    exact = str(find_match or "") in ("相等", "等于", "精确", "相等匹配", "精确匹配")
    same_name = _work_wx._name_matches(current, name, exact)
    same_type = (find_type == "群聊") == _work_wx.get_session_type()
    if same_name and same_type:
        return current
    return _work_wx.find_contact(name, find_match, find_type, check_unique)


def send_message(name, text, find_match, find_type, send_by_clip=False,
                 notify_all=False, nominee_list=None, check_unique=True):
    """发送文字消息。"""
    return _work_wx.send_message(
        name=name,
        text=text,
        find_match=find_match,
        find_type=find_type,
        send_by_clip=send_by_clip,
        notify_all=notify_all,
        nominee_list=nominee_list,
        check_unique=check_unique,
    )


def send_clip(name, find_match="相等", find_type="群聊", check_unique=True):
    """发送剪贴板中的内容。"""
    return _work_wx.send_clip(name, find_match, find_type, check_unique)


def switch_business(business):
    """切换企业。"""
    return _work_wx.switch_business(business)
