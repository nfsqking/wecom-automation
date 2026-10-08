"""不启动企业微信即可运行的接口与数据处理检查。"""

import unittest
from datetime import datetime

import wecom_automation
from wecom_automation._core import SendFailed, WorkWxHandler


class PackageTests(unittest.TestCase):
    def test_public_api(self):
        self.assertEqual(wecom_automation.__all__, (
            "get_session_name", "get_session_list", "get_chat_msg",
            "find_contact", "send_clip", "send_message", "switch_business",
        ))
        self.assertTrue(callable(WorkWxHandler.get_session_type))
        with self.assertRaises(ValueError):
            wecom_automation.find_contact("张三", find_type="未知")

    def test_message_text(self):
        record = WorkWxHandler._flat_message_record(
            "张三@微信  9/18 15:34:43 第一行\n第二行 0 人已读"
        )
        self.assertEqual(record, {
            "sender": "张三",
            "business": "个微",
            "sender_time": "{}-9-18 15:34:43".format(datetime.now().year),
            "message_type": "消息",
            "message": "第一行\n第二行",
        })
        self.assertEqual(WorkWxHandler._search_result_name(
            "测试群 (14) 外部 包含: 用户 (列表项目)", "群聊"
        ), "测试群")

    def test_session_list_name(self):
        for raw in (
            "测试群 外部 3 条未读",
            "测试群 全员 12 人 3 条未读",
            "测试群 部门 研发部 2 分钟前",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(WorkWxHandler._session_list_name(raw), "测试群")
        self.assertEqual(
            WorkWxHandler._session_list_name("全员通知群 研发部门群"),
            "全员通知群 研发部门群",
        )

    def test_send_checks_same_name_chat_type(self):
        class FakeHandler(WorkWxHandler):
            def __init__(self, switch_type):
                self.is_group = True
                self.switch_type = switch_type
                self.searches = 0

            def get_session_name(self):
                return "同名"

            def get_session_type(self):
                return self.is_group

            def find_contact(self, name, find_match, find_type, check_unique):
                self.searches += 1
                if self.switch_type:
                    self.is_group = False
                return name

            def _send(self, *args, **kwargs):
                raise AssertionError("目标未确认前不得发送")

        switched = FakeHandler(switch_type=True)
        switched._ensure_target("同名", "相等", "联系人", True)
        self.assertEqual(switched.searches, 1)

        for send in (
            lambda handler: handler.send_message("同名", "你好", find_type="联系人"),
            lambda handler: handler.send_clip("同名", find_type="联系人"),
        ):
            with self.subTest(send=send):
                with self.assertRaises(SendFailed):
                    send(FakeHandler(switch_type=False))


if __name__ == "__main__":
    unittest.main()
