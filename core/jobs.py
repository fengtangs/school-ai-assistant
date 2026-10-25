"""后台任务注册表：为耗时生成任务提供可轮询的实时进度。

为什么需要它
------------
备课 / 教案 / 课件 / AI 出题 都是一次几十秒到几分钟的阻塞调用（大模型 + 多张配图），
普通 HTTP 请求在前端只能一直转圈，用户不知道进行到哪一步，很容易以为卡死。
这里把生成放到后台线程，前端按 job_id 轮询百分比与阶段文案。

设计要点
--------
· 状态放进程内存，零依赖；服务重启后任务丢失，属可接受代价（前端会提示重新发起）。
· `Ticker` 在长时间阻塞（大模型调用）期间按指数曲线缓慢推进百分比，
  保证进度条一直在动，又永远不会越过该阶段的真实上限。
· 阶段划分对用户可见，文案描述「正在做什么」，而不是「正在调用 API」。
"""
import threading
import time
import uuid

# 任务保留时长（秒）：够前端取到结果，又不至于无限占内存
_TTL = 3600

_jobs = {}
_lock = threading.Lock()


class Job:
    """一次后台生成任务的状态。"""

    def __init__(self, kind: str = ""):
        self.id = uuid.uuid4().hex[:16]
        self.kind = kind
        self.status = "running"      # running / done / error
        self.pct = 0.0
        self.stage = "准备中"
        self.detail = ""
        self.result = None
        self.error = None
        self.error_status = 500
        self.created_at = time.time()
        self.finished_at = None

    # ---- 供业务代码调用 ----
    def update(self, pct: float = None, stage: str = None, detail: str = None):
        """推进进度。百分比只增不减，避免不同阶段来回抖动。"""
        with _lock:
            if pct is not None:
                self.pct = max(self.pct, min(100.0, float(pct)))
            if stage is not None:
                self.stage = stage
            if detail is not None:
                self.detail = detail

    def snapshot(self) -> dict:
        end = self.finished_at or time.time()
        return {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "pct": round(self.pct, 1),
            "stage": self.stage,
            "detail": self.detail,
            "elapsed": round(end - self.created_at, 1),
            "result": self.result,
            "error": self.error,
            "error_status": self.error_status,
        }


class Ticker:
    """在阻塞调用期间把进度从 start 缓慢推向 cap。

    用幂律曲线 `1 - 1/(1 + (t/tau)^0.8)` 逼近 cap：
    开头涨得快（用户立刻看到反应），之后越走越慢但**一直在动**——
    这是相对指数曲线的好处：一次 200 秒的超长调用不会让进度条卡在某个数字上装死。
    传入 job=None 时全部方法为空操作，方便同步调用路径复用同一段业务代码。
    """

    def __init__(self, job, start: float, cap: float, stage: str = None,
                 interval: float = 0.5, tau: float = 12.0, power: float = 0.8):
        self.job = job
        # 注意：不能用 self.start / self.cap 这类名字，会和 start() 方法冲突
        self._from = float(start)
        self._to = float(cap)
        self.stage = stage
        self.interval = interval
        self.tau = tau
        self.power = power
        self._stop = threading.Event()
        self._thread = None

    def _loop(self):
        t0 = time.time()
        while not self._stop.wait(self.interval):
            dt = max(0.001, time.time() - t0)
            frac = 1.0 - 1.0 / (1.0 + (dt / self.tau) ** self.power)
            pct = self._from + (self._to - self._from) * frac
            self.job.update(pct, self.stage)

    def start(self):
        if self.job is None:
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self.job is None:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def _purge_locked():
    """清理超时已完成任务（调用方需已持有 _lock）。"""
    now = time.time()
    for jid in [k for k, v in _jobs.items()
                if v.finished_at and now - v.finished_at > _TTL]:
        _jobs.pop(jid, None)


def create(kind: str = "") -> Job:
    job = Job(kind)
    with _lock:
        _purge_locked()
        _jobs[job.id] = job
    return job


def get(job_id: str):
    with _lock:
        return _jobs.get(job_id)


def submit(kind: str, fn) -> Job:
    """后台执行 fn(job)，立即返回 Job。异常会被捕获并记录为 error。"""
    job = create(kind)

    def runner():
        try:
            result = fn(job)
            with _lock:
                job.result = result
                job.status = "done"
                job.pct = 100.0
                job.stage = "完成"
                job.detail = ""
                job.finished_at = time.time()
        except BaseException as e:  # noqa: BLE001  线程里必须兜住所有异常
            detail = getattr(e, "detail", None) or str(e) or e.__class__.__name__
            with _lock:
                job.error = str(detail)
                job.error_status = int(getattr(e, "status_code", 500) or 500)
                job.status = "error"
                job.stage = "失败"
                job.finished_at = time.time()

    threading.Thread(target=runner, daemon=True).start()
    return job
