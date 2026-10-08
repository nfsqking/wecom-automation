# wecom-automation

基于 Windows UI Automation（UIA）的企业微信桌面端自动化 Python 包，提供会话查询、聊天记录读取、联系人查找、消息发送和企业切换。

## 运行要求与安装

- Windows、Python 3.9 或更新版本。
- 企业微信桌面客户端已启动并登录。首次连接会尝试激活 UIA 树；该过程会修改运行中的企业微信进程内存，需要相应权限。
- 安装时自动安装 `uiautomation`、`pefile` 和 `pywin32`。

项目目前尚未发布到 PyPI。在项目根目录安装：

```powershell
python -m pip install -e .
```

## 快速开始

```python
from wecom_automation import find_contact, get_session_name, send_message

find_contact("测试群", find_type="群聊")
print(get_session_name())
send_message("测试群", "你好", "相等", "群聊")
```

## 通用约定

- `find_type` 可用值为 `"联系人"`、`"群聊"`。
- `find_match="相等"` 为精确匹配，`"包含"` 为包含匹配。精确匹配还接受 `"等于"`、`"精确"`、`"相等匹配"`、`"精确匹配"`；其他值也会按包含匹配处理，建议只使用前两个值。
- `check_unique=True` 要求匹配结果唯一；`False` 使用第一个匹配结果。
- 这些函数会操作桌面界面，可能改变当前会话、前台窗口、滚动位置或系统剪贴板。窗口不存在、UIA 激活失败、元素未找到等情况会抛出异常。
- UIA 元素定位随企业微信客户端版本变化；发布前请在目标版本完成真实界面测试。

## 公开方法

### `get_session_name()`：获取当前窗口名称

读取当前打开的聊天会话名称。无参数；返回名称字符串。名称元素未找到时返回 `None`，企业微信窗口不存在或 UIA 激活失败时会抛出异常。

```python
from wecom_automation import get_session_name

print(get_session_name())
```

### `get_session_list(session_tag, wheel_times=3)`：获取会话列表

选择指定会话分组，先向上滚动到列表顶部，再逐步向下读取不重复的会话名称。

| 参数 | 说明 |
| --- | --- |
| `session_tag` | 分组名称，例如 `"未读"`；需与界面中的分组名称一致。 |
| `wheel_times` | 每次向下滚动的滚轮次数，默认 `3`。 |

返回 `list[str]`，按读取顺序排列。名称后独立出现的“外部”“内部”“全员”“部门”及其后的描述会被去掉。找不到分组或会话列表时会抛出异常。调用后界面停留在所选分组；最多尝试 100 次滚动，因此不保证能读取尚未加载的所有会话。

```python
from wecom_automation import get_session_list

for name in get_session_list("未读", wheel_times=3):
    print(name)
```

### `get_chat_msg(sub_num=0, start_time=None, end_time=None)`：获取聊天记录

读取当前会话可访问的消息。读取时会向上滚动历史记录，完成后尝试滚回。

| 参数 | 说明 |
| --- | --- |
| `sub_num` | 最近消息的数量；默认 `0`，表示尽量读取所有可访问消息。 |
| `start_time` | 可选起始时间，包含该时间点。 |
| `end_time` | 可选结束时间，包含该时间点。 |

时间格式为 `YYYY-MM-DD HH:MM:SS` 或 `YYYY-MM-DD HH:MM`。只要指定任一时间参数，当前实现就不按 `sub_num` 截取，而是按时间筛选。最多向上滚动 100 次；时间无法解析的记录不会因时间过滤而被排除，请传入有效时间字符串。

返回 `list[dict]`。每条记录包含以下字段：

| 字段 | 说明 |
| --- | --- |
| `sender` | 发送者名称；可能为 `None`。 |
| `business` | 发送者归属，例如 `"内部"`、`"个微"`、`"机器人"`、`"自己"`。 |
| `sender_time` | 消息时间字符串；无法识别时可能为 `None`。 |
| `message_type` | 消息类型，例如 `"消息"`、`"文件"`、`"图片"`、`"引用"`。 |
| `message` | 消息正文；多段文字用换行符连接。 |
| `quote_message_sender`、`quote_message` | 仅引用消息可能包含的被引用发送者和正文。 |

```python
from wecom_automation import get_chat_msg

recent = get_chat_msg(sub_num=20)
within_range = get_chat_msg(
    start_time="2026-10-08 09:00:00",
    end_time="2026-10-08 18:00:00",
)
print(recent[-1] if recent else "没有读取到消息")
```

### `find_contact(name, find_match="相等", find_type="群聊", check_unique=False)`：查找联系人

通过全局搜索打开联系人或群聊。如果当前会话名称和类型已匹配目标，直接返回当前会话名称，不重复搜索。

| 参数 | 说明 |
| --- | --- |
| `name` | 名称；`None` 或空白字符串直接返回 `None`。查找联系人时，也可用手机号做精确匹配。 |
| `find_match` | `"相等"` 或 `"包含"`，默认精确匹配。 |
| `find_type` | `"联系人"` 或 `"群聊"`，默认群聊。 |
| `check_unique` | 是否要求结果唯一，默认 `False`。 |

返回打开的结果名称。没有匹配结果、要求唯一但匹配到多个结果，或搜索元素未找到时会抛出异常。模糊匹配且 `check_unique=False` 时会打开第一个匹配结果。

```python
from wecom_automation import find_contact

find_contact("测试群", find_type="群聊", check_unique=True)
find_contact("张三", find_match="包含", find_type="联系人")
```

### `send_clip(name=None, find_match="相等", find_type="群聊", check_unique=True)`：发送剪贴板内容

如传入 `name`，必要时查找并打开目标会话，发送前核对名称和会话类型；随后在消息输入框粘贴当前系统剪贴板内容并按回车发送。剪贴板内容需为企业微信聊天输入框支持粘贴的格式。

| 参数 | 说明 |
| --- | --- |
| `name` | 目标名称；默认 `None`，直接在当前会话发送。 |
| `find_match`、`find_type` | 查找目标时使用的匹配方式和类型。 |
| `check_unique` | 查找目标时是否要求唯一匹配，默认 `True`。 |

正常执行到发送步骤后返回 `True`，不代表消息最终送达。调用者需提前准备剪贴板内容；使用 `name=None` 前请确认当前会话。

```python
import uiautomation as auto
from wecom_automation import send_clip

auto.SetClipboardText("这段文字来自剪贴板")
send_clip("测试群", find_type="群聊")
```

### `send_message(name, text, find_match, find_type, send_by_clip=False, notify_all=False, nominee_list=None, check_unique=True)`：发送文字消息

必要时先查找目标会话，并在发送前核对当前会话名称和类型；然后输入文字、处理提醒人员并按回车发送。

| 参数 | 说明 |
| --- | --- |
| `name` | 目标名称；`None` 或空白字符串表示使用当前会话。 |
| `text` | 非空文字；连续换行会归一为单个换行。 |
| `find_match`、`find_type` | 目标会话的匹配方式和类型，调用时必填。 |
| `send_by_clip` | 兼容参数，默认 `False`。当前文字始终通过剪贴板粘贴，此参数不会改变发送方式。 |
| `notify_all` | 为 `True` 时尝试输入 `@` 并选择全体成员；优先于 `nominee_list`。 |
| `nominee_list` | 要提醒的成员名称列表，默认不提醒；必须是列表。 |
| `check_unique` | 查找目标时是否要求唯一匹配，默认 `True`。 |

正常执行到发送步骤后返回 `True`，不代表服务端确认送达。内容为空、目标会话与预期不符、输入框未找到等情况会抛出异常。发送文字会改写系统剪贴板。

```python
from wecom_automation import send_message

send_message("测试群", "今天的进度已更新", "相等", "群聊")
send_message(
    "测试群", "请查看消息", "相等", "群聊",
    nominee_list=["张三", "李四"],
)
```

### `switch_business(business)`：切换企业

打开企业列表并选择名称匹配的企业。

- `business`：企业名称，不能为空。
- 正常执行选择操作后返回 `True`；找不到切换按钮、企业列表或目标企业时会抛出异常。返回 `True` 不表示已经再次核对切换后的界面。

```python
from wecom_automation import switch_business

switch_business("示例企业")
```
