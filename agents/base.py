import time


class BaseAgent:
    name = "BaseAgent"

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] [{self.name}] {msg}")

    async def handle(self, task: dict) -> dict:
        raise NotImplementedError
