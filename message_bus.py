"""
事件驱动消息总线，对应架构图里的"任务消息队列"。

每条消息结构固定为 {from, to, task_type, payload, status, ts}，
这是我们在协作机制设计里定的自定义协议格式。

与早期版本的关键区别：这个总线是**真正被消费的**，不只是记日志。
它支持 subscribe(task_type, callback)：任何Agent都可以订阅某类事件，
publish 时会把消息**实际派发**给所有订阅者并等待其处理完成。

这就是本平台"对等式/事件驱动协作"的落地基础——
发布事件的Agent（比如筛选Agent发出 paper.rejected）根本不需要知道
谁会响应它；订阅者（追踪预警Agent）独立决定要不要就这条事件发起仲裁申诉。
发布方与订阅方解耦，这正是多Agent架构区别于顺序流水线的地方。

M1~M10 那种纯协议记录消息（task_type 没有订阅者）会正常入 history 但不触发任何回调，
相当于总线在"记录"与"驱动"两种角色上统一起来。
"""

import time


class MessageBus:
    def __init__(self):
        self.history = []
        self._subscribers = {}  # task_type -> [async callback, ...]

    def subscribe(self, task_type: str, callback):
        """注册一个订阅者。callback 是 async def callback(message) 形式。"""
        self._subscribers.setdefault(task_type, []).append(callback)

    async def publish(self, frm: str, to: str, task_type: str, payload: dict, status: str = "pending"):
        message = {
            "from": frm,
            "to": to,
            "task_type": task_type,
            "payload": payload,
            "status": status,
            "ts": time.strftime("%H:%M:%S"),
        }
        self.history.append(message)
        # 真正的派发：把消息交给所有订阅了该 task_type 的Agent处理。
        # 没有订阅者的协议消息（M1~M10）会安静地略过，只留在 history 里。
        for callback in self._subscribers.get(task_type, []):
            await callback(message)
        return message
