import inspect
import unittest

from app.core.config import Settings
from app.main import create_app
from app.services.chat import ChatService


class NoInProcessWorkerTests(unittest.TestCase):
    def test_fastapi_does_not_start_tool_queue_worker(self):
        app = create_app(Settings(app_environment="test", jwt_secret_key="test"))

        startup_names = {
            getattr(handler, "__name__", "")
            for handler in app.router.on_startup
        }
        self.assertNotIn("start_tool_worker", startup_names)
        self.assertNotIn("get_tool_queue_worker", inspect.getsource(create_app))

    def test_chat_does_not_dispatch_tools_after_response(self):
        self.assertNotIn("dispatch_tools", inspect.getsource(ChatService.stream_chat))


if __name__ == "__main__":
    unittest.main()
